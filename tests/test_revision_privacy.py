"""Signed corrections and HR output boundaries, using synthetic employee data."""
import base64
import copy
import json
import logging
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from openpyxl import load_workbook
from PIL import Image

from test_fill_flow import make_request, private, private_json, run

import collection
import collector
import fill
import vault

PERSON = 'SYNTHETIC_PRIVATE_PERSON'
PHONE = '13800138000'
ID_NUMBER = '11010519491231002X'


def update(work, entries, stem):
    answers = private_json(work / f'{stem}.json', {'entries': entries})
    confirmation = work / f'{stem}.confirm'
    result = run(['vault-stage', '--answers', str(answers), '--confirmation-out', str(confirmation)])
    run(['vault-apply', '--confirmation', result['confirmation']])


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    monkeypatch.setenv('YINTIAN_VAULT_DIR', str(private(tmp_path / 'vault')))
    monkeypatch.setenv('YINTIAN_VAULT_KEY_DIR', str(private(tmp_path / 'keys')))
    work = private(tmp_path / 'work')
    request, task = make_request(tmp_path, [
        {'id': 'name', 'label': '姓名', 'type': 'text', 'required': True},
        {'id': 'phone', 'label': '手机', 'type': 'phone_cn', 'required': True},
        {'id': 'id_number', 'label': '证件号', 'type': 'cn_id', 'required': True},
        {'id': 'photo', 'label': '照片', 'type': 'image_attachment'},
    ])
    update(work, {'name': {'type': 'text', 'value': PERSON},
                  'phone': {'type': 'phone_cn', 'value': PHONE},
                  'id_number': {'type': 'cn_id', 'value': ID_NUMBER}}, 'initial')
    return request, task, work


def preview(request, work, stem, *extra):
    confirmation = work / f'{stem}.confirm'
    result = run(['vault-preview', str(request), *extra, '--confirmation-out', str(confirmation)])
    return result, confirmation


def submit(request, work, stem, *extra):
    _, confirmation = preview(request, work, stem, *extra)
    return run(['vault-fill', str(request), *extra, '--confirmation', str(confirmation), '--out-dir', str(work / 'out')])


def signing_key(request):
    profile = vault.load_vault(vault.default_vault_path(), vault.load_or_create_key(vault.default_key_path()))
    state = profile['submission_identities'][fill.load_form(request)['task_id']]
    return vault.identity_key(state['identities'][state['current']])


def excel_rows(result):
    book = load_workbook(result['xlsx'], read_only=True)
    try:
        return list(book.active.values)
    finally:
        book.close()


def test_order_replay_fork_recovery_and_returned_head(scenario, tmp_path):
    request, task, work = scenario
    first = submit(request, work, 'first')
    update(work, {'phone': {'type': 'phone_cn', 'value': '13900139000'}}, 'correction')
    second = submit(request, work, 'second')
    assert first['invite_id'] == second['invite_id'] and second['revision'] == 2
    incoming = private(tmp_path / 'incoming')
    (incoming / 'z-original.yintian').write_bytes(Path(first['out']).read_bytes())
    (incoming / 'a-corrected.yintian').write_bytes(Path(second['out']).read_bytes())
    result = collector.collect_task(task, incoming, tmp_path / 'first.xlsx')
    assert result['rows'] == 1 and excel_rows(result)[1][2] == '13900139000'
    head = collector.progress_rows(task)[0]
    assert head['revision'] == 2 and head['version'] == 1  # Signed order differs from receipt order.
    second_envelope = json.loads(Path(second['out']).read_bytes())
    (incoming / 'formatted.yintian').write_text(json.dumps(second_envelope, indent=4), encoding='utf8')
    replay = collector.ingest_task(task, incoming)
    assert replay['accepted'] == 0 and replay['duplicates'] == 3
    fill.seal_data(fill.load_form(request), {'name': PERSON, 'phone': '13700137000', 'id_number': ID_NUMBER}, {},
                   incoming / 'fork.yintian', signing_key=signing_key(request), revision=2)
    conflict = collector.collect_task(task, incoming, tmp_path / 'conflict.xlsx')
    assert conflict['rows'] == 0 and conflict['exclusions'][0]['status'] == 'revision_conflict'
    third = submit(request, work, 'third')
    (incoming / 'third.yintian').write_bytes(Path(third['out']).read_bytes())
    recovered = collector.collect_task(task, incoming, tmp_path / 'recovered.xlsx')
    assert recovered['rows'] == 1 and collector.progress_rows(task)[0]['revision'] == 3
    with closing(collector.connect_db(task)) as db, db:
        db.execute("UPDATE submissions SET status='returned' WHERE revision=3")
    returned = collector.collect_task(task, incoming, tmp_path / 'returned.xlsx')
    assert returned['rows'] == 0 and returned['exclusions'][0]['status'] == 'returned'
    args = collector.build_parser().parse_args(['notice', str(task), third['invite_id'], '--out', str(work / 'notice')])
    notice = args.func(args)
    assert notice['revision'] == 3 and notice['status'] == 'returned'
    # Inspection must not adopt the unauthenticated notice counter.
    raw = json.loads((work / 'notice').read_bytes())
    raw['revision'] = 123456
    (work / 'notice').write_text(json.dumps(raw), encoding='utf8')
    run(['inspect', str(work / 'notice')])
    assert preview(request, work, 'after-notice')[0]['revision'] == 4
    invalid = json.loads(Path(third['out']).read_bytes())
    invalid['revision'] = 4
    invalid = collection.sign_envelope(invalid, signing_key(request))  # Signature is valid, encrypted payload is not.
    (incoming / 'invalid-current.yintian').write_text(json.dumps(invalid), encoding='utf8')
    rejected_current = collector.collect_task(task, incoming, tmp_path / 'invalid-current.xlsx')
    assert rejected_current['rows'] == 0 and rejected_current['exclusions'][0]['status'] == 'invalid'
    assert collector.progress_rows(task)[0]['revision'] == 4


def test_foreign_previous_and_forged_high_revision_cannot_replace(scenario, tmp_path, monkeypatch):
    request, task, work = scenario
    first = submit(request, work, 'first')
    collector.collect_task(task, work / 'out', tmp_path / 'valid.xlsx')
    with monkeypatch.context() as foreign:
        foreign.setenv('YINTIAN_VAULT_DIR', str(private(tmp_path / 'foreign-vault')))
        foreign.setenv('YINTIAN_VAULT_KEY_DIR', str(private(tmp_path / 'foreign-keys')))
        update(work, {'name': {'type': 'text', 'value': 'SYNTHETIC_OTHER'},
                      'phone': {'type': 'phone_cn', 'value': PHONE},
                      'id_number': {'type': 'cn_id', 'value': ID_NUMBER}}, 'foreign')
        with pytest.raises(fill.FillError, match='PREVIOUS_INVALID'):
            preview(request, work, 'attack', '--previous', first['out'])
    assert not (work / 'attack.confirm').exists()
    forged = json.loads(Path(first['out']).read_bytes())
    forged['revision'] = 999999
    (work / 'out' / 'unsigned-change.yintian').write_text(json.dumps(forged), encoding='utf8')
    attacker = Ed25519PrivateKey.generate()
    forged['sender_public_key_b64'] = base64.b64encode(attacker.public_key().public_bytes_raw()).decode()
    forged = collection.sign_envelope(forged, attacker)
    (work / 'out' / 'wrong-owner.yintian').write_text(json.dumps(forged), encoding='utf8')
    result = collector.collect_task(task, work / 'out', tmp_path / 'after-attack.xlsx')
    assert result['rows'] == 1 and result['ingest']['rejected'] == 2
    assert collector.progress_rows(task)[0]['revision'] == 1


def test_preview_concurrency_deleted_receipt_and_fresh_abandon(scenario):
    request, _, work = scenario

    def attempt(stem):
        try:
            return preview(request, work, stem)
        except RuntimeError as exc:
            assert 'TASK_BUSY' in str(exc)
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ['left', 'right']))
    results = [result or preview(request, work, stem) for result, stem in zip(results, ['left', 'right'])]
    assert results[0][0]['invite_id'] == results[1][0]['invite_id']
    first = run(['vault-fill', str(request), '--confirmation', str(results[0][1]), '--out-dir', str(work / 'out')])
    with pytest.raises(fill.FillError, match='CONFIRMATION_STALE'):
        run(['vault-fill', str(request), '--confirmation', str(results[1][1]), '--out-dir', str(work / 'out')])
    Path(first['out']).unlink()
    abandoned, _ = preview(request, work, 'abandoned', '--fresh')
    next_receipt = submit(request, work, 'normal')
    assert next_receipt['invite_id'] == first['invite_id'] != abandoned['invite_id']
    assert next_receipt['revision'] == 2
    with pytest.raises(SystemExit):
        fill.build_parser().parse_args(['vault-preview', str(request), '--fresh', '--previous', next_receipt['out'],
                                        '--confirmation-out', str(work / 'invalid')])


def test_failed_output_does_not_reuse_reserved_revision(scenario, monkeypatch):
    request, _, work = scenario
    pending, confirmation = preview(request, work, 'fail')
    with monkeypatch.context() as failing:
        def fail(*args, **kwargs):
            raise OSError('SYNTHETIC_DISK_FAILURE')
        failing.setattr(fill, 'seal_data', fail)
        with pytest.raises(OSError):
            run(['vault-fill', str(request), '--confirmation', str(confirmation), '--out-dir', str(work / 'out')])
    resumed = submit(request, work, 'resumed')
    assert resumed['invite_id'] == pending['invite_id'] and resumed['revision'] == pending['revision'] + 1


def test_identity_corruption_fails_closed_without_losing_existing_entries(scenario):
    request, _, work = scenario
    preview(request, work, 'identity')
    profile = vault.load_vault(vault.default_vault_path(), vault.load_or_create_key(vault.default_key_path()))
    assert profile['entries']['name']['value'] == PERSON
    for broken in (None, [], {'bad-task': {}}, {fill.load_form(request)['task_id']: {'current': 'missing', 'identities': {}}}):
        candidate = {**copy.deepcopy(profile), 'submission_identities': broken}
        with pytest.raises(ValueError, match='VAULT_INVALID'):
            vault.validate_profile(candidate)
    task_id = fill.load_form(request)['task_id']
    state = profile['submission_identities'][task_id]
    for bad_counter in (True, -1, collection.MAX_REVISION + 1):
        broken_profile = copy.deepcopy(profile)
        broken_profile['submission_identities'][task_id]['identities'][state['current']]['last_reserved_revision'] = bad_counter
        with pytest.raises(ValueError, match='VAULT_INVALID'):
            vault.validate_profile(broken_profile)


def test_legacy_request_and_task_are_rejected_without_migration(scenario):
    request, task, _ = scenario
    for path in (request, task / 'task.json'):
        original = json.loads(path.read_bytes())
        original['format_version'] = 'yintian-submission/4'
        path.write_text(json.dumps(original), encoding='utf8')
    database_before = (task / 'state.sqlite3').read_bytes()
    with pytest.raises(fill.FillError, match='OPEN_REQUEST_INVALID'):
        fill.load_form(request)
    with pytest.raises(ValueError, match='LEGACY_TASK_UNSUPPORTED'):
        collector.load_task(task)
    assert (task / 'state.sqlite3').read_bytes() == database_before


def test_hr_export_keeps_values_out_of_tool_output_and_names(scenario, tmp_path, capfd, monkeypatch):
    request, task, work = scenario
    image = work / f'{PERSON}-{PHONE}.png'
    Image.new('RGB', (12, 12), 'white').save(image)
    update(work, {'photo': {'type': 'image_attachment', 'paths': [str(image)]}}, 'photo')
    receipt = submit(request, work, 'private')
    assert PERSON not in Path(receipt['out']).name
    monkeypatch.setattr(sys, 'argv', ['collector.py', 'collect', str(task), str(work / 'out'), '--out', str(tmp_path / 'result.xlsx')])
    collector.main()
    captured = capfd.readouterr()
    result = json.loads(captured.out)
    assert excel_rows(result)[1][:4] == (PERSON, receipt['invite_id'], PHONE, ID_NUMBER)
    visible = captured.out + captured.err + json.dumps(collector.progress_rows(task), ensure_ascii=False)
    visible += ''.join(str(path.relative_to(tmp_path)) for path in Path(result['attachments_dir']).rglob('*'))
    for marker in (PERSON, PHONE, ID_NUMBER):
        assert marker not in visible
    # A raw library error cannot echo private content through the CLI's error JSON.
    def fail_export(*args, **kwargs):
        raise OSError(f'{PERSON} {PHONE} {ID_NUMBER}')
    monkeypatch.setattr(collector, 'write_excel', fail_export)
    monkeypatch.setattr(sys, 'argv', ['collector.py', 'collect', str(task), str(work / 'out'), '--out', str(tmp_path / 'failed.xlsx')])
    with pytest.raises(SystemExit) as failed_exit:
        collector.main()
    assert failed_exit.value.code == 1
    failed = capfd.readouterr()
    assert not (tmp_path / 'failed.xlsx').exists()
    for marker in (PERSON, PHONE, ID_NUMBER):
        assert marker not in failed.out + failed.err


def test_private_dependency_output_is_suppressed_and_restored(capfd):
    with pytest.raises(RuntimeError):
        with collection.suppress_private_output():
            print(PERSON)
            print(PHONE, file=sys.stderr)
            os.write(1, ID_NUMBER.encode())
            os.write(2, PERSON.encode())
            logging.warning(PERSON)
            raise RuntimeError('synthetic failure')
    captured = capfd.readouterr()
    assert not captured.out and not captured.err
    print('OUTPUT_RESTORED')
    assert 'OUTPUT_RESTORED' in capfd.readouterr().out


def test_native_buffered_output_is_flushed_before_restoring_descriptors():
    scripts = Path(collection.__file__).parent
    script = f'''
import ctypes, os, sys
sys.path.insert(0, {str(scripts)!r})
from collection import suppress_private_output
runtime = ctypes.CDLL("ucrtbase" if os.name == "nt" else None)
fdopen = runtime._fdopen if os.name == "nt" else runtime.fdopen
fdopen.argtypes = [ctypes.c_int, ctypes.c_char_p]
fdopen.restype = ctypes.c_void_p
stream = fdopen(1, b"wb")
assert stream
runtime.fwrite.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_size_t, ctypes.c_void_p]
runtime.fwrite.restype = ctypes.c_size_t
runtime.fflush.argtypes = [ctypes.c_void_p]
with suppress_private_output():
    raw = b"PRIVATE_BUFFER_NORMAL"
    assert runtime.fwrite(raw, 1, len(raw), stream) == len(raw)
try:
    with suppress_private_output():
        raw = b"PRIVATE_BUFFER_FAILURE"
        assert runtime.fwrite(raw, 1, len(raw), stream) == len(raw)
        raise RuntimeError("synthetic failure")
except RuntimeError:
    pass
runtime.fflush(None)
print("OUTPUT_RESTORED")
'''
    result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, check=True)
    assert result.stdout == 'OUTPUT_RESTORED\n' and not result.stderr
