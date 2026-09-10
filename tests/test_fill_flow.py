import json
import os
import sys
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "safefill-fill" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import collection  # noqa: E402
import fill  # noqa: E402


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
    collection.dump_json(config_path, config)
    args = collection.build_parser().parse_args(
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
