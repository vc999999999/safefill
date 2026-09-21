import json
import os
import sys
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "safefill-fill" / "scripts"
COLLECT_SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "safefill-collect" / "scripts"
sys.path.insert(0, str(COLLECT_SCRIPTS))
sys.path.insert(0, str(SCRIPTS))

import collection  # noqa: E402
import collector  # noqa: E402
import fill  # noqa: E402
import vault  # noqa: E402


def private(path: Path) -> Path:
    path.mkdir(mode=0o700)
    if os.name != "nt":
        path.chmod(0o700)
    return path


def private_json(path: Path, data) -> Path:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
    return path


@pytest.fixture
def isolated_vault(tmp_path, monkeypatch):
    monkeypatch.setenv("YINTIAN_VAULT_DIR", str(private(tmp_path / "vault")))
    monkeypatch.setenv("YINTIAN_VAULT_KEY_DIR", str(private(tmp_path / "keys")))


def run(argv):
    args = fill.build_parser().parse_args(argv)
    return args.func(args)


def feed_stdin(monkeypatch, data) -> None:
    payload = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode("utf-8")
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=BytesIO(payload)))


def make_request(tmp_path: Path, fields: list[dict]):
    config = {
        "title": "员工资料",
        "purpose": "入职登记",
        "deadline": "2098-12-31T23:59:59Z",
        "retention_until": "2099-01-31T23:59:59Z",
        "contact": "hr@example.com",
        "correction": "使用本人旧回执更正",
        "template_version": "1.0",
        "fields": fields,
    }
    config_path = tmp_path / "collection.json"
    collector.dump_json(config_path, config)
    args = collector.build_parser().parse_args(
        ["create-request", "--config", str(config_path), "--out", str(tmp_path / "tasks")])
    created = args.func(args)
    return Path(created["request"]), Path(created["task_dir"])


def stage_via_stdin(tmp_path, monkeypatch, entries: dict, name="change"):
    work = tmp_path / "private"
    if not work.exists():
        private(work)
    confirmation = work / f"{name}.yintian-confirmation"
    feed_stdin(monkeypatch, {"entries": entries})
    return run(["vault-stage", "--answers", "-", "--confirmation-out", str(confirmation)]), confirmation


def test_vault_stage_reads_answers_from_stdin(tmp_path, isolated_vault, monkeypatch):
    preview, confirmation = stage_via_stdin(
        tmp_path, monkeypatch, {"phone": {"type": "phone_cn", "value": "13800138000"}})
    assert confirmation.is_file()
    assert [change["id"] for change in preview["changes"]] == ["phone"]
    assert preview["changes"][0]["new_value"] == "13800138000"
    assert not list(confirmation.parent.glob("*.json"))

    applied = run(["vault-apply", "--confirmation", str(confirmation)])
    assert applied["created"] == ["phone"] and not confirmation.exists()
    key = vault.load_or_create_key(vault.default_key_path())
    assert vault.load_vault(vault.default_vault_path(), key)["entries"]["phone"]["value"] == "13800138000"


def test_vault_stage_stdin_invalid_json(tmp_path, isolated_vault, monkeypatch):
    work = private(tmp_path / "private")
    feed_stdin(monkeypatch, b"not json")
    with pytest.raises(fill.FillError, match="VAULT_ANSWERS_INVALID"):
        run(["vault-stage", "--answers", "-", "--confirmation-out", str(work / "c")])

    feed_stdin(monkeypatch, ["not", "a", "dict"])
    with pytest.raises(fill.FillError, match="VAULT_ANSWERS_INVALID"):
        run(["vault-stage", "--answers", "-", "--confirmation-out", str(work / "c")])
    assert not (work / "c").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_vault_stage_file_mode_rejects_wide_permissions(tmp_path, isolated_vault):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    answers = private_json(shared / "answers.json", {"entries": {"name": {"type": "text", "value": "张三"}}})
    work = private(tmp_path / "private")
    with pytest.raises(fill.FillError, match="ANSWERS_PERMISSIONS"):
        run(["vault-stage", "--answers", str(answers), "--confirmation-out", str(work / "c")])
    assert not answers.exists()


def test_vault_stage_file_mode_deletes_plaintext_on_success(tmp_path, isolated_vault):
    work = private(tmp_path / "private")
    answers = private_json(work / "answers.json", {"entries": {"name": {"type": "text", "value": "张三"}}})
    confirmation = work / "change.yintian-confirmation"
    run(["vault-stage", "--answers", str(answers), "--confirmation-out", str(confirmation)])
    assert not answers.exists() and confirmation.is_file()


def test_vault_stage_reports_existing_confirmation(tmp_path, isolated_vault, monkeypatch):
    entries = {"name": {"type": "text", "value": "张三"}}
    _preview, confirmation = stage_via_stdin(tmp_path, monkeypatch, entries)
    feed_stdin(monkeypatch, {"entries": entries})
    with pytest.raises(fill.FillError, match="CONFIRMATION_EXISTS") as excinfo:
        run(["vault-stage", "--answers", "-", "--confirmation-out", str(confirmation)])
    message = str(excinfo.value)
    assert str(confirmation) in message and "删除该文件" in message and "--confirmation-out" in message


def test_vault_preview_reports_existing_confirmation(tmp_path, isolated_vault):
    request, _task_dir = make_request(tmp_path, [
        {"id": "name", "label": "姓名", "type": "text", "required": True, "sensitive": True},
    ])
    work = private(tmp_path / "private")
    answers = private_json(work / "answers.json", {"entries": {"name": {"type": "text", "value": "张三"}}})
    staged = work / "staged.yintian-confirmation"
    run(["vault-stage", "--answers", str(answers), "--confirmation-out", str(staged)])
    run(["vault-apply", "--confirmation", str(staged)])

    confirmation = work / "submit.yintian-confirmation"
    run(["vault-preview", str(request), "--confirmation-out", str(confirmation)])
    with pytest.raises(fill.FillError, match="CONFIRMATION_EXISTS") as excinfo:
        run(["vault-preview", str(request), "--confirmation-out", str(confirmation)])
    message = str(excinfo.value)
    assert str(confirmation) in message and "删除该文件" in message and "--confirmation-out" in message


def _fill_once(tmp_path, request, work, name="submit", extra=None):
    confirmation = work / f"{name}.yintian-confirmation"
    run(["vault-preview", str(request), *(extra or []), "--confirmation-out", str(confirmation)])
    return run(["vault-fill", str(request), *(extra or []), "--confirmation", str(confirmation),
                "--out-dir", str(work / "out")])


def test_registry_records_submission_and_reuses_invite_id(tmp_path, isolated_vault):
    request, _task_dir = make_request(tmp_path, [
        {"id": "name", "label": "姓名", "type": "text", "required": True, "sensitive": True},
    ])
    work = private(tmp_path / "private")
    stage_via_answers = work / "answers.json"
    private_json(stage_via_answers, {"entries": {"name": {"type": "text", "value": "张三"}}})
    run(["vault-stage", "--answers", str(stage_via_answers), "--confirmation-out", str(work / "c.yintian-confirmation")])
    run(["vault-apply", "--confirmation", str(work / "c.yintian-confirmation")])

    first = _fill_once(tmp_path, request, work, "s1")
    vault_dir = Path(os.environ["YINTIAN_VAULT_DIR"])
    records = vault.load_registry(vault_dir)
    assert [record["invite_id"] for record in records] == [first["invite_id"]]
    assert records[0]["task_id"] == json.loads(request.read_text(encoding="utf-8"))["task_id"]
    assert records[0]["path"] == first["out"]

    preview_path = work / "s2.yintian-confirmation"
    preview = run(["vault-preview", str(request), "--confirmation-out", str(preview_path)])
    assert preview["previous"] == first["out"] and preview["previous_source"] == "registry"
    second = run(["vault-fill", str(request), "--confirmation", str(preview_path), "--out-dir", str(work / "out")])
    assert second["invite_id"] == first["invite_id"]
    assert second["previous_source"] == "registry"
    assert "warning" not in second
    assert len(vault.load_registry(vault_dir)) == 2


def test_fresh_bypasses_registry_and_explicit_previous_wins(tmp_path, isolated_vault):
    request, _task_dir = make_request(tmp_path, [
        {"id": "name", "label": "姓名", "type": "text", "required": True, "sensitive": True},
    ])
    work = private(tmp_path / "private")
    private_json(work / "answers.json", {"entries": {"name": {"type": "text", "value": "张三"}}})
    run(["vault-stage", "--answers", str(work / "answers.json"), "--confirmation-out", str(work / "c.yintian-confirmation")])
    run(["vault-apply", "--confirmation", str(work / "c.yintian-confirmation")])
    first = _fill_once(tmp_path, request, work, "s1")

    fresh_confirmation = work / "fresh.yintian-confirmation"
    fresh_preview = run(["vault-preview", str(request), "--fresh", "--confirmation-out", str(fresh_confirmation)])
    assert "previous" not in fresh_preview
    fresh = run(["vault-fill", str(request), "--fresh", "--confirmation", str(fresh_confirmation),
                 "--out-dir", str(work / "out")])
    assert fresh["invite_id"] != first["invite_id"]

    explicit_confirmation = work / "explicit.yintian-confirmation"
    explicit_preview = run(["vault-preview", str(request), "--previous", first["out"],
                            "--confirmation-out", str(explicit_confirmation)])
    assert explicit_preview["previous"] == first["out"] and explicit_preview["previous_source"] == "explicit"


def test_unrelated_vault_change_does_not_stale_submission_confirmation(tmp_path, isolated_vault):
    request, _task_dir = make_request(tmp_path, [
        {"id": "name", "label": "姓名", "type": "text", "required": True, "sensitive": True},
    ])
    work = private(tmp_path / "private")
    private_json(work / "answers.json", {"entries": {"name": {"type": "text", "value": "张三"}}})
    run(["vault-stage", "--answers", str(work / "answers.json"), "--confirmation-out", str(work / "c.yintian-confirmation")])
    run(["vault-apply", "--confirmation", str(work / "c.yintian-confirmation")])

    confirmation = work / "submit.yintian-confirmation"
    run(["vault-preview", str(request), "--confirmation-out", str(confirmation)])
    private_json(work / "other.json", {"entries": {"email": {"type": "text", "value": "a@b.c"}}})
    run(["vault-stage", "--answers", str(work / "other.json"), "--confirmation-out", str(work / "c2.yintian-confirmation")])
    run(["vault-apply", "--confirmation", str(work / "c2.yintian-confirmation")])
    result = run(["vault-fill", str(request), "--confirmation", str(confirmation), "--out-dir", str(work / "out")])
    assert result["invite_id"].startswith("OPEN-")


def test_changed_selected_value_still_stales_confirmation(tmp_path, isolated_vault):
    request, _task_dir = make_request(tmp_path, [
        {"id": "name", "label": "姓名", "type": "text", "required": True, "sensitive": True},
    ])
    work = private(tmp_path / "private")
    private_json(work / "answers.json", {"entries": {"name": {"type": "text", "value": "张三"}}})
    run(["vault-stage", "--answers", str(work / "answers.json"), "--confirmation-out", str(work / "c.yintian-confirmation")])
    run(["vault-apply", "--confirmation", str(work / "c.yintian-confirmation")])

    confirmation = work / "submit.yintian-confirmation"
    run(["vault-preview", str(request), "--confirmation-out", str(confirmation)])
    private_json(work / "other.json", {"entries": {"name": {"type": "text", "value": "李四"}}})
    run(["vault-stage", "--answers", str(work / "other.json"), "--confirmation-out", str(work / "c2.yintian-confirmation")])
    run(["vault-apply", "--confirmation", str(work / "c2.yintian-confirmation")])
    with pytest.raises(fill.FillError, match="CONFIRMATION_STALE"):
        run(["vault-fill", str(request), "--confirmation", str(confirmation), "--out-dir", str(work / "out")])


def test_vault_apply_per_entry_staleness(tmp_path, isolated_vault):
    work = private(tmp_path / "private")
    private_json(work / "a1.json", {"entries": {"phone": {"type": "phone_cn", "value": "13800138000"}}})
    run(["vault-stage", "--answers", str(work / "a1.json"), "--confirmation-out", str(work / "c1.yintian-confirmation")])
    run(["vault-apply", "--confirmation", str(work / "c1.yintian-confirmation")])

    # stage 修改 phone；确认签发后 phone 被另一次 apply 改掉 → 同条目 STALE
    private_json(work / "a2.json", {"entries": {"phone": {"type": "phone_cn", "value": "13900139000"}}})
    run(["vault-stage", "--answers", str(work / "a2.json"), "--confirmation-out", str(work / "c2.yintian-confirmation")])
    private_json(work / "a3.json", {"entries": {"phone": {"type": "phone_cn", "value": "13700137000"}}})
    run(["vault-stage", "--answers", str(work / "a3.json"), "--confirmation-out", str(work / "c3.yintian-confirmation")])
    run(["vault-apply", "--confirmation", str(work / "c3.yintian-confirmation")])
    with pytest.raises(fill.FillError, match="CONFIRMATION_STALE"):
        run(["vault-apply", "--confirmation", str(work / "c2.yintian-confirmation")])

    # 只新增无关条目不使待应用确认失效
    private_json(work / "a4.json", {"entries": {"email": {"type": "text", "value": "a@b.c"}}})
    run(["vault-stage", "--answers", str(work / "a4.json"), "--confirmation-out", str(work / "c4.yintian-confirmation")])
    private_json(work / "a5.json", {"entries": {"city": {"type": "text", "value": "北京"}}})
    run(["vault-stage", "--answers", str(work / "a5.json"), "--confirmation-out", str(work / "c5.yintian-confirmation")])
    run(["vault-apply", "--confirmation", str(work / "c5.yintian-confirmation")])
    applied = run(["vault-apply", "--confirmation", str(work / "c4.yintian-confirmation")])
    assert applied["created"] == ["email"]


def test_corrupt_confirmation_auto_cleaned(tmp_path, isolated_vault, monkeypatch):
    work = private(tmp_path / "private")
    confirmation = work / "c.yintian-confirmation"
    confirmation.write_bytes(b"corrupt")
    if os.name != "nt":
        confirmation.chmod(0o600)
    feed_stdin(monkeypatch, {"entries": {"name": {"type": "text", "value": "张三"}}})
    result = run(["vault-stage", "--answers", "-", "--confirmation-out", str(confirmation)])
    assert result["confirmation"] == str(confirmation)


def test_inspect_recognizes_notice(tmp_path, isolated_vault):
    notice_path = tmp_path / "n.yintian-notice"
    private_json(notice_path, {
        "format": "yintian-notice/1", "task_id": "YT-20260101-ABCDEF", "invite_id": "OPEN-0123456789ABCDEF0123456789ABCDEF",
        "status": "returned", "late": False, "reasons": ["phone"], "next_action": "补齐后带 --previous 重交",
        "contact": "hr@example.com", "generated_at": "2026-01-01T00:00:00Z"})
    result = run(["inspect", str(notice_path)])
    assert result["kind"] == "notice" and result["invite_id"] == "OPEN-0123456789ABCDEF0123456789ABCDEF"
    assert result["reasons"] == ["phone"] and result["contact"] == "hr@example.com"


def test_inspect_rejects_notice_with_extra_fields(tmp_path, isolated_vault):
    notice_path = tmp_path / "n.yintian-notice"
    private_json(notice_path, {
        "format": "yintian-notice/1", "task_id": "YT-20260101-ABCDEF", "invite_id": "OPEN-0123456789ABCDEF0123456789ABCDEF",
        "status": "returned", "next_action": "x", "reasons": [], "payload": "evil"})
    with pytest.raises(fill.FillError, match="NOTICE_INVALID"):
        run(["inspect", str(notice_path)])


def test_receipt_inspect_reads_header_and_registry(tmp_path, isolated_vault):
    request, _task_dir = make_request(tmp_path, [
        {"id": "name", "label": "姓名", "type": "text", "required": True, "sensitive": True},
    ])
    work = private(tmp_path / "private")
    private_json(work / "answers.json", {"entries": {"name": {"type": "text", "value": "张三"}}})
    run(["vault-stage", "--answers", str(work / "answers.json"), "--confirmation-out", str(work / "c.yintian-confirmation")])
    run(["vault-apply", "--confirmation", str(work / "c.yintian-confirmation")])
    first = _fill_once(tmp_path, request, work, "s1")

    info = run(["receipt-inspect", first["out"]])
    assert info["invite_id"] == first["invite_id"] and info["format_version"] == collection.SUBMISSION_FORMAT_VERSION
    assert info["registry"]["path"] == first["out"]

    with pytest.raises(fill.FillError, match="RECEIPT_INVALID"):
        run(["receipt-inspect", str(request)])


def test_inspect_omits_static_boilerplate(tmp_path, isolated_vault):
    request, _task_dir = make_request(tmp_path, [
        {"id": "name", "label": "姓名", "type": "text", "required": True, "sensitive": True},
    ])
    info = run(["inspect", str(request)])
    assert info["key_id_match"] is True and len(info["key_fingerprint"]) == 24
    assert not {"verify_hint", "expects", "key_id"} & set(info)
