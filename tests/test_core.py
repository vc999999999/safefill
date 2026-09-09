import json
import os
import stat
import sys
import types
from pathlib import Path

import pytest
from openpyxl import load_workbook

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "safefill-fill" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import collection  # noqa: E402
import fill  # noqa: E402
import vault  # noqa: E402
import vlm_extract  # noqa: E402


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


def stage(tmp_path: Path, entries: dict, name="change"):
    work = tmp_path / "private"
    if not work.exists():
        private(work)
    answers = private_json(work / f"{name}.json", {"entries": entries})
    confirmation = work / f"{name}.yintian-confirmation"
    preview = run(["vault-stage", "--answers", str(answers), "--confirmation-out", str(confirmation)])
    assert not answers.exists()
    applied = run(["vault-apply", "--confirmation", str(confirmation)])
    assert not confirmation.exists()
    return preview, applied


def test_open_roundtrip_uses_only_exact_ids_and_explicit_mapping(tmp_path, isolated_vault):
    request, task_dir = make_request(tmp_path, [
        {"id": "name", "label": "姓名", "type": "text", "required": True, "sensitive": True},
        {"id": "hire_date", "label": "入职日期", "type": "date", "required": True, "sensitive": True},
    ])
    stage(tmp_path, {
        "name": {"type": "text", "value": "张三"},
        "start_date": {"type": "date", "value": "2026-09-01"},
    })

    status = run(["vault-status", "--request", str(request)])
    assert {item["id"]: item["status"] for item in status["fields"]} == {
        "name": "matched", "hire_date": "missing"}

    mapping = private_json(tmp_path / "private" / "mapping.json", {"hire_date": "start_date"})
    confirmation = tmp_path / "private" / "submit.yintian-confirmation"
    preview = run(["vault-preview", str(request), "--mapping", str(mapping),
                   "--confirmation-out", str(confirmation)])
    assert next(item for item in preview["fields"] if item["id"] == "hire_date")["value"] == "2026-09-01"

    incoming = private(tmp_path / "incoming")
    reply = Path(run(["vault-fill", str(request), "--confirmation", str(confirmation),
                      "--out-dir", str(incoming)])["out"])
    assert reply.name.startswith("张三-") and reply.suffix == ".yintian"

    result = collection.collect_open(task_dir, incoming, tmp_path / "result.xlsx")
    book = load_workbook(result["xlsx"], read_only=True)
    rows = list(book.active.values)
    book.close()
    assert result["rows"] == 1 and rows[1][:2] == ("张三", "2026-09-01")


def test_confirmation_rejects_changed_request_tampering_and_reuse(tmp_path, isolated_vault):
    request, _task_dir = make_request(tmp_path, [
        {"id": "name", "label": "姓名", "type": "text", "required": True, "sensitive": True},
    ])
    stage(tmp_path, {"name": {"type": "text", "value": "张三"}})
    work = tmp_path / "private"

    stale = work / "stale.yintian-confirmation"
    run(["vault-preview", str(request), "--confirmation-out", str(stale)])
    original = request.read_bytes()
    request.write_text(json.dumps(json.loads(original), ensure_ascii=False, indent=2), encoding="utf-8")
    with pytest.raises(fill.FillError, match="CONFIRMATION_STALE"):
        run(["vault-fill", str(request), "--confirmation", str(stale), "--out-dir", str(work)])
    request.write_bytes(original)

    tampered = work / "tampered.yintian-confirmation"
    run(["vault-preview", str(request), "--confirmation-out", str(tampered)])
    tampered.write_bytes(tampered.read_bytes()[:-1] + b"x")
    with pytest.raises(fill.FillError, match="CONFIRMATION_INVALID"):
        run(["vault-fill", str(request), "--confirmation", str(tampered), "--out-dir", str(work)])

    valid = work / "valid.yintian-confirmation"
    run(["vault-preview", str(request), "--confirmation-out", str(valid)])
    run(["vault-fill", str(request), "--confirmation", str(valid), "--out-dir", str(work)])
    with pytest.raises(fill.FillError, match="CONFIRMATION_INVALID"):
        run(["vault-fill", str(request), "--confirmation", str(valid), "--out-dir", str(work)])


def test_v1_migration_preserves_source_and_deletes_password(tmp_path):
    source_dir, target_dir, key_dir = private(tmp_path / "old"), private(tmp_path / "new"), private(tmp_path / "keys")
    source = source_dir / "vault.yintian-vault"
    old = {"version": 1, "types": {"name": "text"}, "values": {"name": "张三"}, "attachments": {}}
    source.write_bytes(collection.canonical(collection.aes_gcm_seal(
        vault.FORMAT_V1, collection.canonical(old), "password", vault.FORMAT_V1.encode())))
    password = source_dir / "password.txt"
    password.write_text("password", encoding="utf-8")
    if os.name != "nt":
        source.chmod(0o600)
        password.chmod(0o600)
    target, key = target_dir / vault.VAULT_FILENAME, key_dir / vault.KEY_FILENAME

    run(["vault-migrate", "--password-file", str(password), "--source-vault", str(source),
         "--target-vault", str(target), "--target-key", str(key)])
    profile = vault.load_vault(target, vault.load_or_create_key(key))
    assert source.exists() and not password.exists() and profile["entries"]["name"]["value"] == "张三"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_existing_parent_permissions_are_not_changed(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    work = private(tmp_path / "private")
    answers = private_json(work / "answers.json", {"entries": {"name": {"type": "text", "value": "张三"}}})
    with pytest.raises(fill.FillError, match="VAULT_PERMISSIONS"):
        run(["vault-stage", "--answers", str(answers), "--confirmation-out", str(work / "confirmation"),
             "--vault", str(shared / "vault"), "--key-file", str(work / "key")])
    assert stat.S_IMODE(shared.stat().st_mode) == 0o755 and not answers.exists()


def test_vlm_setup_uses_the_model_selected_by_user(tmp_path, monkeypatch):
    root = private(tmp_path / "models")
    monkeypatch.setattr(vlm_extract, "model_root", lambda: root)
    calls = []

    def download(**kwargs):
        calls.append(kwargs)
        Path(kwargs["local_dir"], "model.bin").write_bytes(b"model")

    monkeypatch.setitem(sys.modules, "huggingface_hub", types.SimpleNamespace(snapshot_download=download))
    result = vlm_extract.setup("any-compatible/model")
    assert calls[0]["repo_id"] == "any-compatible/model" and "revision" not in calls[0]
    assert vlm_extract.verify_model("any-compatible/model", target=Path(result["path"]))


def test_doctor_core_ok_shape():
    report = run(["doctor"])
    assert set(report) == {"python", "core", "ocr", "vlm", "storage"}
    for item in report.values():
        assert isinstance(item["ok"], bool) and isinstance(item["detail"], str)
        assert item["install_hint"] is None or isinstance(item["install_hint"], str)
    assert report["python"]["ok"] is True
    assert report["core"]["ok"] is True
    assert isinstance(report["ocr"]["devices"], list) and isinstance(report["ocr"]["device_ok"], bool)


def test_doctor_reports_missing_ocr_and_device_mismatch(monkeypatch):
    import config

    monkeypatch.setitem(sys.modules, "rapidocr_openvino", None)
    monkeypatch.setitem(sys.modules, "openvino", None)
    report = run(["doctor"])
    assert report["ocr"]["ok"] is False
    assert "requirements-ocr.txt" in report["ocr"]["install_hint"]

    fake_openvino = types.SimpleNamespace(Core=lambda: types.SimpleNamespace(available_devices=["CPU"]))
    monkeypatch.setitem(sys.modules, "openvino", fake_openvino)
    monkeypatch.setattr(config, "OCR_DEVICE", "GPU")
    report = run(["doctor"])
    assert report["ocr"]["devices"] == ["CPU"]
    assert report["ocr"]["device_ok"] is False
    assert "GPU" in report["ocr"]["detail"]


def test_doctor_device_ok_not_applicable_when_openvino_missing(monkeypatch):
    import config

    monkeypatch.setitem(sys.modules, "openvino", None)
    monkeypatch.setattr(config, "OCR_DEVICE", "GPU")
    report = run(["doctor"])
    assert report["ocr"]["ok"] is False
    assert report["ocr"]["device_ok"] is True  # 依赖缺失时设备匹配不适用，不误报


def test_doctor_storage_overrides(tmp_path):
    report = run(["doctor", "--vault", str(tmp_path / "v.yintian-vault")])
    assert report["storage"]["ok"] is False  # 只给 --vault 不给 --key-file
    custom_vault = tmp_path / "custom" / "v.yintian-vault"
    report = run(["doctor", "--vault", str(custom_vault), "--key-file", str(tmp_path / "custom" / "k.key")])
    assert report["storage"]["ok"] is True
    assert str(custom_vault) in report["storage"]["detail"]


def test_doctor_ocr_warns_python_312_requires_311(monkeypatch):
    monkeypatch.setattr(sys, "version_info", (3, 12, 0))
    monkeypatch.setitem(sys.modules, "rapidocr_openvino", None)
    monkeypatch.setitem(sys.modules, "openvino", None)
    report = run(["doctor"])
    assert report["ocr"]["ok"] is False
    assert "3.11" in report["ocr"]["detail"]
    assert "3.11" in report["ocr"]["install_hint"]
    assert "requirements-ocr.txt" in report["ocr"]["install_hint"]


def test_doctor_reports_current_interpreter_path():
    report = run(["doctor"])
    assert sys.executable in report["python"]["detail"]


def test_vlm_setup_resumes_partial_download(tmp_path, monkeypatch):
    root = private(tmp_path / "models")
    monkeypatch.setattr(vlm_extract, "model_root", lambda: root)
    calls = []

    def flaky_download(**kwargs):
        calls.append(kwargs)
        local = Path(kwargs["local_dir"])
        (local / ".cache").mkdir(exist_ok=True)
        if len(calls) == 1:
            (local / ".cache" / "chunk.bin").write_bytes(b"half")
            raise RuntimeError("network down")
        (local / "model.bin").write_bytes(b"model")

    monkeypatch.setitem(sys.modules, "huggingface_hub", types.SimpleNamespace(snapshot_download=flaky_download))
    with pytest.raises(vlm_extract.VlmUnavailable, match="断点续传"):
        vlm_extract.setup("any-compatible/model")
    partial = Path(calls[0]["local_dir"])
    assert partial.name.endswith(".partial") and partial.is_dir()

    result = vlm_extract.setup("any-compatible/model")
    assert len(calls) == 2 and Path(calls[1]["local_dir"]) == partial  # 复用同一 .partial 续传
    assert not partial.exists()
    assert vlm_extract.verify_model("any-compatible/model", target=Path(result["path"]))


def test_vlm_candidates_flag_needs_review_for_unvalidated_fields(tmp_path, monkeypatch):
    text = json.dumps({"name": "张三", "id_number": "110105194912310020",
                       "phone": "13800138000", "address": "北京市海淀区中关村大街1号"})
    monkeypatch.setattr(vlm_extract, "_run_model", lambda *args, **kwargs: text)
    result = vlm_extract.extract_fields(tmp_path / "id.png", "any-compatible/model")
    candidates = {item["field"]: item for item in result["candidates"]}
    assert candidates["name"]["valid"] is None and candidates["name"]["needs_review"] is True
    assert candidates["address"]["valid"] is None and candidates["address"]["needs_review"] is True
    assert candidates["id_number"]["valid"] is False and "needs_review" not in candidates["id_number"]
    assert candidates["phone"]["valid"] is True and "needs_review" not in candidates["phone"]


def test_vault_fill_warns_about_existing_receipts(tmp_path, isolated_vault):
    request, _task_dir = make_request(tmp_path, [
        {"id": "name", "label": "姓名", "type": "text", "required": True, "sensitive": True},
    ])
    stage(tmp_path, {"name": {"type": "text", "value": "张三"}})
    confirmation = tmp_path / "private" / "submit.yintian-confirmation"
    run(["vault-preview", str(request), "--confirmation-out", str(confirmation)])
    incoming = private(tmp_path / "incoming")
    (incoming / "张三-AAAAAA.yintian").write_bytes(b"old-receipt")
    result = run(["vault-fill", str(request), "--confirmation", str(confirmation), "--out-dir", str(incoming)])
    assert "--previous" in result["warning"] and "重复" in result["warning"]


def test_vlm_setup_modelscope_source(tmp_path, monkeypatch):
    root = private(tmp_path / "models")
    monkeypatch.setattr(vlm_extract, "model_root", lambda: root)
    calls = []

    def download(**kwargs):
        calls.append(kwargs)
        Path(kwargs["local_dir"], "model.bin").write_bytes(b"model")

    monkeypatch.setitem(sys.modules, "modelscope", types.SimpleNamespace(snapshot_download=download))
    result = run(["vlm-setup", "--model", "any-compatible/model", "--source", "modelscope"])
    assert calls[0]["model_id"] == "any-compatible/model" and "revision" not in calls[0]
    assert vlm_extract.verify_model("any-compatible/model", target=Path(result["path"]))

    with pytest.raises(SystemExit):
        fill.build_parser().parse_args(["vlm-setup", "--model", "m", "--source", "unknown"])
