"""Reproducible synthetic CLI/GUI integration. Pauses for actual host vision.
Run with Python 3.11 and --out OUT. No transcript or password is written to disk.
"""
import argparse
import base64
import importlib.util
import io
import json
import os
from pathlib import Path
import pty
import re
import select
import shutil
import sys
import time
import zipfile

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / 'scripts'), str(REPO.parent / 'safefill-fill/scripts')]
import collection
import secure_io
from PIL import Image, ImageDraw, ImageFont
from openpyxl import load_workbook


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    root = Path(args.out).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.umask(0o077)
    events = []
    def record(step, **checks):
        event = {'step': step, **checks}
        events.append(event)
        secure_io.atomic_write(root / 'flow-evidence.json', collection.canonical(events))
        print(json.dumps(event, ensure_ascii=False), flush=True)
    def cli(name, arguments, replies=(), gui=False, secret_heading=None):
        script = REPO / 'tests/gui_cli.py' if gui else REPO / 'scripts/collection.py' if name == 'hr' else REPO.parent / 'safefill-fill/scripts/fill.py'
        pid, fd = pty.fork()
        if pid == 0:
            os.environ['PYTHONUNBUFFERED'] = '1'
            os.execv(sys.executable, [sys.executable, str(script), *map(str, arguments)])
        buffer = b''
        index, offset = 0, 0
        start = time.monotonic()
        try:
            while True:
                if time.monotonic() - start > 100:
                    os.kill(pid, 9)
                    raise AssertionError('CLI_TIMEOUT: ' + str(arguments[0]))
                ready, _, _ = select.select([fd], [], [], .1)
                if ready:
                    try:
                        chunk = os.read(fd, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    buffer += chunk
                text = buffer.decode('utf-8', 'replace').replace('\r', '')
                if index < len(replies):
                    prompt, reply = replies[index]
                    position = text.find(prompt, offset)
                    if position >= 0:
                        os.write(fd, (reply + '\n').encode())
                        offset = position + len(prompt)
                        index += 1
            _, status = os.waitpid(pid, 0)
        finally:
            os.close(fd)
        code = os.waitstatus_to_exitcode(status)
        if code:
            # Only error code/type; never copy a private terminal transcript into a test log.
            error = re.search(r'([A-Z][A-Z_]{3,}):', text)
            raise AssertionError(f'CLI_FAILED {arguments[0]} {code} ' + (error.group(1) if error else 'see local synthetic reproduction'))
        assert index == len(replies), 'UNUSED_SYNTHETIC_INPUT'
        result = None
        for match in re.finditer(r'(?m)^\{', text):
            try:
                candidate, _ = json.JSONDecoder().raw_decode(text[match.start():])
                result = candidate
            except json.JSONDecodeError:
                pass
        assert result is not None, 'MISSING_JSON_RESULT'
        if secret_heading:
            secret = text.split(secret_heading, 1)[1].strip().splitlines()[0]
            return result, secret
        return result

    assert importlib.util.find_spec('rapidocr_openvino') is None
    assert importlib.util.find_spec('openvino') is None
    record('environment', python=sys.version.split()[0], rapidocr=False, openvino=False, api_key_required=False)
    config = collection.default_config()
    config.update(title='SafeFill 合成流程测试', purpose='只用于合成软件验证', contact='合成HR', correction='合成任务重填',
                  deadline='2098-01-01', retention_until='2099-01-01')
    config['fields'] = [f for f in config['fields'] if f['id'] != 'id_back']
    group_config = {**config, 'fields': [{'id': 'employee_id', 'label': '工号', 'type': 'text', 'required': True}, *config['fields']]}
    secure_io.atomic_write(root / 'group-config.json', collection.canonical(group_config))
    secure_io.atomic_write(root / 'roster.csv', 'employee_id,name\nS001,合成甲\n'.encode())
    group, password = cli('hr', ['create', '--mode', 'group', '--roster', root / 'roster.csv', '--config', root / 'group-config.json', '--out', root / 'hr'], secret_heading='任务密码（仅显示一次，丢失不可恢复）：')
    task = Path(group['task_dir'])
    employee = root / 'employee'
    employee.mkdir(mode=0o700)
    form = employee / 'FORM.yintian-form'
    credential = employee / 'PERSONAL.yintian-credential'
    shutil.copyfile(task / 'FORM.yintian-form', form)
    shutil.copyfile(task / 'credentials/GRP-S001.yintian-credential', credential)
    info = cli('fill', ['inspect', form, '--json'])
    image = Image.new('RGB', (1000, 500), '#f0f4fa')
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype('/System/Library/Fonts/STHeiti Medium.ttc', 36)
    identity = '99000019900101001'
    weights = [7,9,10,5,8,4,2,1,6,3,7,9,10,5,8,4,2]
    identity += '10X98765432'[sum(int(v)*w for v,w in zip(identity, weights)) % 11]
    address, phone = '合成市演示区测试路七号', '13900000007'
    for y, text in [(35, '合成测试资料 · 非真实身份证'), (140, '姓名 合成甲'), (230, '公民身份号码 ' + identity), (320, '住址 ' + address)]:
        draw.text((35, y), text, font=font, fill='#14253f')
    image_path = employee / 'synthetic-evidence.png'
    image.save(image_path)
    secret_note = 'SYNTHETIC_VAULT_UNREQUESTED_6e838715'
    personal_config = dict(group_config)
    personal_config['fields'] = [*group_config['fields'], {'id': 'private_note', 'type': 'text', 'label': '不应提交的保险柜备注', 'required': False}]
    personal_form = collection.load_json(form)
    personal_form['fields'] = personal_config['fields']
    personal_form['schema_hash'] = collection.sha256_bytes(collection.canonical(personal_form['fields']))
    secure_io.atomic_write(employee / 'personal-profile.yintian-form', collection.canonical(personal_form))
    vault = employee / 'personal.yintian-vault'
    vault_password = 'synthetic-test-vault-password-350'
    cli('fill', ['vault-init', employee / 'personal-profile.yintian-form', '--vault', vault], [
        ('保险柜密码（至少 12 个字符）: ', vault_password), ('再次输入保险柜密码: ', vault_password),
        ('工号（输入隐藏', 'S001'), ('姓名（输入隐藏', '合成甲'), ('手机号（输入隐藏', phone),
        ('身份证号（输入隐藏', identity), ('住址（输入隐藏', address), ('身份证正面：输入文件路径', str(image_path)),
        ('不应提交的保险柜备注（输入隐藏', secret_note), ('输入 SAVE: ', 'SAVE')])
    record('create_and_vault_init', public_template=True, private_credential=True, extra_vault_field=True)
    ready = {'form': str(form), 'image': str(image_path), 'agent_result': str(employee / 'agent-result.json'),
             'task_dir': str(task)}
    secure_io.atomic_write(root / 'ready.json', collection.canonical(ready))
    record('waiting_for_host_vision', ready=str(root / 'ready.json'))
    started = time.monotonic()
    while not (employee / 'agent-result.json').exists():
        if time.monotonic() - started > 1800:
            raise AssertionError('EXTERNAL_SYNTHETIC_STEP_TIMEOUT')
        time.sleep(1)
    incoming = root / 'incoming'
    incoming.mkdir(mode=0o700)
    replies = [('保险柜密码: ', vault_password), ('核对的公钥指纹: ', info['key_id']), ('输入 AGENT: ', 'AGENT'), (f"输入任务编号 {info['task_id']}: ", info['task_id'])]
    sealed = cli('fill', ['fill', form, '--vault', vault, '--credential', credential, '--agent-ocr', employee / 'agent-result.json', '--out', incoming / 'v1.yintian'], replies)
    assert sealed['fields'] == 5
    assert cli('hr', ['ingest', task, incoming])['accepted'] == 1
    assert cli('hr', ['ingest', task, incoming])['duplicates'] == 1
    review = cli('hr', ['review', task], [('任务密码: ', password)])
    assert review['needs_review'] == 1 and review['verified'] == 0
    assert collection.report_rows(task)[0]['recognition_sources'][0]['source'] == 'agent'
    record('agent_fill_ingest_review', sealed_fields=5, duplicate_ignored=True, status='needs_review', source='agent')
    confirmed = cli('hr', ['decide', task, 'GRP-S001', '--version', '1', '--action', 'confirm', '--operator', 'synthetic.hr'], [('任务密码: ', password)], gui=True)
    assert confirmed['status'] == 'verified_manual'
    exported = cli('hr', ['export-clear', task, '--fields', 'phone,id_number,address', '--out', root / 'approved.xlsx', '--purpose', '合成测试', '--recipient', '合成HR'], [('任务密码: ', password), ('以确认导出明文: ', info['task_id'])])
    assert exported['rows'] == 1
    workbook = load_workbook(exported['xlsx'], read_only=True)
    cells = [tuple(row) for row in workbook.active.values]
    workbook.close()
    assert phone in cells[1] and identity in cells[1] and address in cells[1]
    assert secret_note not in str(cells)
    record('real_tk_confirmation_and_excel', state='verified_manual', rows=1, values_match=True, extra_vault_field_excluded=True)
    # Newest pending must block export while preserving the older approved version.
    manual_replies = [('保险柜密码: ', vault_password), ('核对的公钥指纹: ', info['key_id']), (f"输入任务编号 {info['task_id']}: ", info['task_id'])]
    cli('fill', ['fill', form, '--vault', vault, '--credential', credential, '--out', incoming / 'v2.yintian'], manual_replies)
    cli('hr', ['ingest', task, incoming])
    row = collection.report_rows(task)[0]
    assert row['approved_version'] == 1 and row['latest_version'] == 2 and row['pending_count'] == 1
    cli('hr', ['review', task], [('任务密码: ', password)])
    row = collection.report_rows(task)[0]
    assert row['recognition_sources'][0]['source'] == 'manual'
    pending_export = cli('hr', ['export-clear', task, '--fields', 'phone', '--out', root / 'pending-excluded.xlsx', '--purpose', '合成测试', '--recipient', '合成HR'], [('任务密码: ', password), ('以确认导出明文: ', info['task_id'])])
    assert pending_export['rows'] == 0
    cli('hr', ['decide', task, 'GRP-S001', '--version', '2', '--action', 'return', '--operator', 'synthetic.hr'], [('任务密码: ', password), (f"输入任务 ID {info['task_id']}: ", info['task_id'])])
    assert cli('hr', ['review', task, '--retry-needs-review'], [('任务密码: ', password)]) == {'verified': 0, 'needs_review': 0, 'invalid': 0}
    record('resubmit_return_retry', newest_version=2, previous_approved=1, pending_export_rows=0, manual_resolution_preserved=True)
    report = cli('hr', ['report', task])
    package, handoff = cli('hr', ['export-task', task, '--out', root / 'handoff.yintian-task'], secret_heading='交接密码（仅显示一次，丢失不可恢复）：')
    imported = cli('hr', ['import-task', root / 'handoff.yintian-task', '--out', root / 'imported'], [('交接密码: ', handoff)])
    assert collection.status_task(imported['task_dir'])['counts'] == collection.status_task(task)['counts']
    # Scan non-sensitive boundaries. Original employee inputs and authorized clear exports are intentional.
    needles = [phone.encode(), identity.encode(), address.encode(), secret_note.encode(), password.encode(), vault_password.encode()]
    scanned = 0
    for folder in (task, root / 'incoming', root / 'imported'):
        for path in folder.rglob('*'):
            if not path.is_file():
                continue
            data = path.read_bytes()
            if path.suffix == '.xlsx':
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    data = b'\n'.join(archive.read(n) for n in archive.namelist())
            assert not any(needle in data for needle in needles), 'SENSITIVE_BOUNDARY_FAILURE'
            scanned += 1
    assert not any(needle in (root / 'flow-evidence.json').read_bytes() for needle in needles)
    record('handoff_and_privacy_scan', imported=True, boundary_files_scanned=scanned, plaintext_leaks=0)
    record('complete', hardware_tested=False, commercial_cloud_tested=False, host_vision_actual=True)

if __name__ == '__main__':
    main()
