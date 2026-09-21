"""针对交互设计修复的回归：隐式 OCR 绑定、时区、预览缺项、排除明细、结构化错误等。"""
import json
import os
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "safefill-fill" / "scripts"
COLLECT_SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "safefill-collect" / "scripts"
sys.path.insert(0, str(COLLECT_SCRIPTS))
sys.path.insert(0, str(SCRIPTS))

import collection  # noqa: E402
import collector  # noqa: E402
import fill  # noqa: E402
import fill_extract  # noqa: E402


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


def base_config(fields, **overrides):
    config = {
        "title": "员工资料", "purpose": "入职登记", "deadline": "2098-12-31T23:59:59Z",
        "retention_until": "2099-01-31T23:59:59Z", "contact": "hr@example.com",
        "correction": "使用本人旧回执更正", "template_version": "1.0", "fields": fields,
    }
    config.update(overrides)
    return config


def make_request(tmp_path: Path, fields, **overrides):
    config_path = tmp_path / "collection.json"
    collector.dump_json(config_path, base_config(fields, **overrides))
    args = collector.build_parser().parse_args(
        ["create-request", "--config", str(config_path), "--out", str(tmp_path / "tasks")])
    created = args.func(args)
    return created, Path(created["request"]), Path(created["task_dir"])


def stage(tmp_path: Path, entries: dict, name="change"):
    work = tmp_path / "private"
    if not work.exists():
        private(work)
    answers = private_json(work / f"{name}.json", {"entries": entries})
    confirmation = work / f"{name}.yintian-confirmation"
    run(["vault-stage", "--answers", str(answers), "--confirmation-out", str(confirmation)])
    return run(["vault-apply", "--confirmation", str(confirmation)])


NAME = {"id": "name", "label": "姓名", "type": "text", "required": True, "sensitive": True}


def test_ocr_fields_binding_is_explicit_only(monkeypatch):
    """字段名含 front 不隐式触发比对；只有显式 ocr_fields 才走 OCR。"""
    fields = [NAME, {"id": "id_number", "label": "身份证号", "type": "cn_id"},
              {"id": "id_front", "label": "身份证正面", "type": "image_attachment"},
              {"id": "id_card_front", "label": "证件正面", "type": "image_attachment", "ocr_fields": ["name"]}]
    payload = {"values": {"name": "张三"}}
    items = [{"field_id": "id_front"}, {"field_id": "id_card_front"}]
    calls = []
    monkeypatch.setattr(collector, "ocr_attachment",
                        lambda item: calls.append(item["field_id"]) or ("姓名: 张三", []))
    assert collector.compare_ocr(payload, items, fields) == []
    assert calls == ["id_card_front"]


def test_create_request_reports_retention_and_ocr_bound_fields(tmp_path):
    created, request, _ = make_request(tmp_path, [
        NAME, {"id": "id_number", "label": "身份证号", "type": "cn_id", "required": True},
        {"id": "id_front", "label": "身份证正面", "type": "image_attachment", "required": True},
    ])
    assert created["ocr_bound_fields"] == [] and "decide" not in created["reminder"]
    assert created["retention_until"] in created["reminder"]

    created, _, _ = make_request(tmp_path / "bound", [
        NAME, {"id": "id_number", "label": "身份证号", "type": "cn_id", "required": True},
        {"id": "id_front", "label": "身份证正面", "type": "image_attachment", "required": True, "ocr_fields": ["id_number"]},
    ])
    assert created["ocr_bound_fields"] == ["id_front"] and "decide" in created["reminder"]
    info = fill.inspect_info(fill.load_form(created["request"]))
    assert next(f for f in info["fields"] if f["id"] == "id_front")["ocr_fields"] == ["id_number"]


@pytest.mark.parametrize("value", ["2098-12-31", "2098-12-31T23:59:59", "2098-12-31 23:59:59"])
def test_deadline_without_timezone_is_rejected(tmp_path, value):
    with pytest.raises(ValueError, match="TIME_ZONE_REQUIRED"):
        collector.validate_config(base_config([NAME], deadline=value))
    assert not collection.explicit_timezone(value)
    assert collection.explicit_timezone("2098-12-31T23:59:59+08:00") and collection.explicit_timezone("2098-12-31T23:59:59Z")


def test_inspect_reports_past_deadline_but_not_expired(tmp_path):
    _, request, _ = make_request(tmp_path, [NAME])
    form = json.loads(request.read_text(encoding="utf-8"))
    form["deadline"] = "2000-01-01T00:00:00Z"
    notice = {key: form[key] for key in fill.NOTICE_KEYS}
    form["notice_hash"] = collection.sha256_bytes(collection.canonical(notice))
    request.write_text(json.dumps(form, ensure_ascii=False), encoding="utf-8")

    info = fill.inspect_info(fill.load_form(request))
    assert info["past_deadline"] is True and info["expired"] is False
    assert "迟交" in info["deadline_notice"] and "stop_reason" not in info


def test_vault_preview_returns_full_picture_instead_of_error_when_required_missing(tmp_path, isolated_vault):
    _, request, _ = make_request(tmp_path, [
        NAME, {"id": "phone", "label": "本人手机号", "type": "phone_cn", "required": True},
        {"id": "hire_date", "label": "入职日期", "type": "date", "required": True},
        {"id": "note", "label": "备注", "type": "text", "required": False},
    ])
    stage(tmp_path, {"name": {"type": "text", "value": "张三"},
                     "mobile": {"type": "phone_cn", "value": "13800138000", "label": "手机"}})
    confirmation = tmp_path / "private" / "submit.yintian-confirmation"

    preview = run(["vault-preview", str(request), "--confirmation-out", str(confirmation)])

    assert preview["ready"] is False and not confirmation.exists() and "confirmation" not in preview
    assert [item["id"] for item in preview["required_missing"]] == ["phone", "hire_date"]
    assert preview["optional_missing"] == ["note"]
    assert next(item for item in preview["fields"] if item["id"] == "name")["value"] == "张三"
    assert preview["same_type_entries"] == {"phone": [{"entry": "mobile", "label": "手机"}]}

    status = run(["vault-status", "--request", str(request)])
    assert status["same_type_entries"]["phone"][0]["entry"] == "mobile"
    assert status["entries"]["name"]["label"] == "name"  # 空标签回退为条目 id

    mapping = private_json(tmp_path / "private" / "map.json", {"phone": "mobile"})
    stage(tmp_path, {"hire_date": {"type": "date", "value": "2026-09-01"}}, name="fix")
    ready = run(["vault-preview", str(request), "--mapping", str(mapping), "--confirmation-out", str(confirmation)])
    assert ready["ready"] is True and confirmation.exists() and ready["confirmation"] == str(confirmation)


def test_collect_reports_rejections_late_and_duplicate_name_groups(tmp_path, isolated_vault):
    _, request, task_dir = make_request(tmp_path, [NAME])
    stage(tmp_path, {"name": {"type": "text", "value": "张三"}})
    incoming = private(tmp_path / "incoming")
    receipt_ids = []
    for index in range(2):
        confirmation = tmp_path / "private" / f"submit{index}.yintian-confirmation"
        run(["vault-preview", str(request), "--fresh", "--confirmation-out", str(confirmation)])
        reply = run(["vault-fill", str(request), "--fresh", "--confirmation", str(confirmation), "--out-dir", str(incoming)])
        receipt_ids.append(reply["invite_id"])
    (incoming / "坏掉-XXXXXX.yintian").write_text(json.dumps({
        "format_version": collection.SUBMISSION_FORMAT_VERSION, "task_id": json.loads(request.read_text(encoding="utf-8"))["task_id"],
        "invite_id": "OPEN-AAAAAAAAAAAAAAAA", "schema_hash": json.loads(request.read_text(encoding="utf-8"))["schema_hash"],
        "key_id": json.loads(request.read_text(encoding="utf-8"))["key_id"],
        "algorithms": {"content": "AES-256-GCM", "key_wrap": "RSA-OAEP-3072-SHA256"},
        "encrypted_key_b64": "AAAA", "iv_b64": "AAAA", "ciphertext_b64": "AAAA"}), encoding="utf-8")

    result = collector.collect_task(task_dir, incoming, tmp_path / "result.xlsx")

    assert result["rows"] == 2 and result["excluded"] == 0 and result["late"] == 0
    assert len(result["duplicate_name_groups"]) == 1
    assert set(result["duplicate_name_groups"][0]) == set(receipt_ids)
    assert result["ingest"]["rejected"] == 1 and result["exclusions"] == []
    assert result["ingest"]["errors"][0]["code"] == "SUBMISSION_REJECTED"
    assert "张三" not in json.dumps(result, ensure_ascii=False)


def test_exclusion_details_next_actions(tmp_path):
    rows = [
        {"name": "甲", "invite_id": "OPEN-1", "status": "verified", "version": 1, "revision": 1, "late": True, "missing_fields": [], "conflict_fields": []},
        {"name": "乙", "invite_id": "OPEN-2", "status": "needs_review", "version": 2, "revision": 2, "late": False, "missing_fields": [], "conflict_fields": ["ocr:conflict:id_number"]},
        {"name": "丙", "invite_id": "OPEN-3", "status": "needs_review", "version": 1, "revision": 1, "late": False, "missing_fields": [], "conflict_fields": ["runtime:ocr:id_front"]},
        {"name": "丁", "invite_id": "OPEN-4", "status": "needs_review", "version": 3, "revision": 3, "late": True, "missing_fields": ["phone"], "conflict_fields": []},
    ]
    details = {item["invite_id"]: item for item in collector.exclusion_details(tmp_path, rows)}
    assert set(details) == {"OPEN-2", "OPEN-3", "OPEN-4"}
    assert all("name" not in item for item in details.values())
    assert "decide" in details["OPEN-2"]["next_action"]
    assert details["OPEN-2"]["version"] == 2
    assert "OPEN-2" in details["OPEN-2"]["decide_command"] and "--version 2" in details["OPEN-2"]["decide_command"]
    assert "decide_command" not in details["OPEN-3"]
    assert "doctor" in details["OPEN-3"]["next_action"]
    assert "本人原保险柜" in details["OPEN-4"]["next_action"] and details["OPEN-4"]["late"] is True
    assert "--previous" not in details["OPEN-4"]["next_action"]


def test_error_report_extracts_code():
    report = collection.error_report(RuntimeError("CONFIRMATION_STALE: PRIVATE_VALUE_13800138000"))
    assert report["ok"] is False and report["error"] == "CONFIRMATION_STALE"
    assert "重新预览" in report["message"] and "PRIVATE_VALUE_13800138000" not in json.dumps(report)
    missing = collection.error_report(FileNotFoundError("PRIVATE_ATTACHMENT_NAME.pdf"))
    assert missing["error"] == "PATH_MISSING"
    assert "PRIVATE_ATTACHMENT_NAME" not in json.dumps(missing)


def test_every_raised_code_has_static_message_and_no_bare_raises():
    """每个带前缀的错误码都要有静态说明；脚本对外 raise 必须带错误码，否则 Agent 只会收到异常类名。"""
    import re
    scripts = [*SCRIPTS.glob("*.py"), *COLLECT_SCRIPTS.glob("*.py")]
    source = "\n".join(p.read_text(encoding="utf-8") for p in scripts)
    raised = set(re.findall(r'(?:Error|Unavailable)\(f?"([A-Z][A-Z0-9_]+):', source))
    for code in raised:
        report = collection.error_report(RuntimeError(f"{code}: PRIVATE_DETAIL"))
        assert report["error"] == code and "PRIVATE_DETAIL" not in report["message"], code
    generic = collection.error_report(RuntimeError("no code"))["message"]
    assert all(collection.error_report(RuntimeError(f"{c}: x"))["message"] != generic for c in raised)
    bare = [(p.name, m.group(0)) for p in scripts for m in re.finditer(
        r'raise (FillError|VlmUnavailable)\(f?"[^A-Z]', p.read_text(encoding="utf-8"))]
    assert bare == []
    for exc, code in ((NotADirectoryError("x"), "PATH_MISSING"), (PermissionError("x"), "PERMISSION_DENIED"),
                      (FileExistsError("x"), "OUTPUT_EXISTS"), (OSError("x"), "IO_ERROR")):
        assert collection.error_report(exc)["error"] == code


def test_cli_errors_carry_protocol_codes(tmp_path, isolated_vault, capsys):
    assert fill.main(["inspect", str(tmp_path / "missing.yintian-request")]) == 1
    assert json.loads(capsys.readouterr().err)["error"] == "REQUEST_MISSING"
    bad = tmp_path / "bad.yintian-request"
    bad.write_text("garbage", encoding="utf-8")
    assert fill.main(["inspect", str(bad)]) == 1
    assert json.loads(capsys.readouterr().err)["error"] == "REQUEST_INVALID"
    _, request, task_dir = make_request(tmp_path, [NAME])
    args = collector.build_parser().parse_args(
        ["notice", str(task_dir), "OPEN-" + "0" * 32, "--out", str(tmp_path / "n.yintian-notice")])
    with pytest.raises(ValueError, match="INVITE_NOT_FOUND"):
        args.func(args)
    args.invite_id = "OPEN-short"
    with pytest.raises(ValueError, match="INVITE_ID_INVALID"):
        args.func(args)
    with pytest.raises(NotADirectoryError):
        collector.ingest_task(task_dir, tmp_path / "no_inbox")


def test_create_request_fills_documented_defaults(tmp_path):
    config = {"purpose": "团建订票", "deadline": "2098-12-31T18:00:00+08:00", "contact": "hr@example.com",
              "fields": [{"id": "phone", "label": "本人手机号", "type": "phone_cn", "required": True}]}
    result = collector.validate_config(config)
    assert result["title"] == "团建订票" and result["template_version"] == "1.0" and result["correction"]
    assert result["retention_until"] == "2099-01-30T18:00:00+08:00"
    assert result["fields"][0]["type"] == "text" and result["fields"][0]["required"] is True
    assert [f["id"] for f in result["fields"]] == ["name", "phone"]
    assert "name" not in json.dumps(config["fields"])  # 不改写调用方对象
    with pytest.raises(ValueError, match="CONFIG_MISSING_FIELDS"):
        collector.validate_config({"purpose": "x", "deadline": "2098-12-31T18:00:00+08:00", "fields": [NAME]})
    with pytest.raises(ValueError, match="SCHEMA_INVALID"):
        collector.validate_config({**config, "fields": [{"id": "name", "label": "姓名", "type": "text", "required": False}]})


def test_inspect_and_request_omit_static_boilerplate(tmp_path, isolated_vault):
    _, request, _ = make_request(tmp_path, [NAME, {"id": "room_type", "label": "房型", "type": "text"}])
    assert "expects" not in json.loads(request.read_text(encoding="utf-8"))
    info = run(["inspect", str(request)])
    assert not {"verify_hint", "expects", "key_id"} & set(info) and info["key_id_match"] is True
    stage(tmp_path, {"name": {"type": "text", "label": "姓名", "value": "张三"},
                     "hobby": {"type": "text", "label": "爱好", "value": "跑步"}})
    status = run(["vault-status", "--request", str(request)])
    assert status["missing"] == ["room_type"] and status["same_type_entries"] == {}


def test_expired_message_names_retention_and_next_step():
    message = collection.expired_message({"retention_until": "2020-01-01T00:00:00Z"}, "接收新提交")
    assert message.startswith("TASK_EXPIRED") and "2020-01-01T00:00:00Z" in message and "新建请求包" in message


def test_fill_extract_uses_standard_ids_and_derives_birth_date():
    text = "姓名: 张三\n住址: 北京市海淀区中关村大街1号\n公民身份号码 11010519491231002X\n电话 13800138000"
    result = fill_extract.extract_fields(text)
    assert result["fields"]["id_number"]["value"] == "11010519491231002X"
    assert result["fields"]["birth_date"] == {"value": "1949-12-31", "confidence": "derived"}
    assert result["fields"]["gender"]["value"] == "女"
    assert result["fields"]["address"]["value"].startswith("北京市")
    assert set(result["fields"]) <= set(fill_extract.EXTRACT_FIELDS)


def test_symlink_error_names_offending_segment(tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX symlink")
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    import secure_io
    with pytest.raises(ValueError, match="PATH_UNSAFE: 路径中的 .*link 是符号链接"):
        secure_io.checked_path(link / "vault.yintian-vault")
