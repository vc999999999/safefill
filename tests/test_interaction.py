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

    # vault-fill 仍然严格：缺必填不能生成回执
    with pytest.raises(fill.FillError, match="VAULT_FIELDS_MISSING"):
        fill._prepare_vault_selection(fill.load_form(request), {"format": vault.FORMAT, "entries": {}}, {})


def test_collect_reports_exclusions_late_and_duplicate_names(tmp_path, isolated_vault):
    _, request, task_dir = make_request(tmp_path, [NAME])
    stage(tmp_path, {"name": {"type": "text", "value": "张三"}})
    incoming = private(tmp_path / "incoming")
    for index in range(2):
        confirmation = tmp_path / "private" / f"submit{index}.yintian-confirmation"
        run(["vault-preview", str(request), "--confirmation-out", str(confirmation)])
        run(["vault-fill", str(request), "--confirmation", str(confirmation), "--out-dir", str(incoming)])
    (incoming / "坏掉-XXXXXX.yintian").write_text(json.dumps({
        "format_version": collection.SUBMISSION_FORMAT_VERSION, "task_id": json.loads(request.read_text())["task_id"],
        "invite_id": "OPEN-AAAAAAAAAAAAAAAA", "schema_hash": json.loads(request.read_text())["schema_hash"],
        "key_id": json.loads(request.read_text())["key_id"],
        "algorithms": {"content": "AES-256-GCM", "key_wrap": "RSA-OAEP-3072-SHA256"},
        "encrypted_key_b64": "AAAA", "iv_b64": "AAAA", "ciphertext_b64": "AAAA"}), encoding="utf-8")

    result = collector.collect_task(task_dir, incoming, tmp_path / "result.xlsx")

    assert result["rows"] == 2 and result["excluded"] == 1 and result["late"] == 0
    assert result["duplicate_names"] == ["张三"] and "--previous" in result["warning"]
    assert result["exclusions"][0]["status"] == "invalid" and "重新生成" in result["exclusions"][0]["next_action"]


def test_exclusion_details_next_actions():
    rows = [
        {"name": "甲", "invite_id": "OPEN-1", "status": "verified", "late": True, "missing_fields": [], "conflict_fields": []},
        {"name": "乙", "invite_id": "OPEN-2", "status": "needs_review", "late": False, "missing_fields": [], "conflict_fields": ["ocr:conflict:id_number"]},
        {"name": "丙", "invite_id": "OPEN-3", "status": "needs_review", "late": False, "missing_fields": [], "conflict_fields": ["runtime:ocr:id_front"]},
        {"name": "丁", "invite_id": "OPEN-4", "status": "needs_review", "late": True, "missing_fields": ["phone"], "conflict_fields": []},
    ]
    details = {item["name"]: item for item in collector.exclusion_details(rows)}
    assert set(details) == {"乙", "丙", "丁"}
    assert "decide" in details["乙"]["next_action"]
    assert "requirements-ocr" in details["丙"]["next_action"]
    assert "--previous" in details["丁"]["next_action"] and details["丁"]["late"] is True


def test_error_report_extracts_code():
    assert collection.error_report(RuntimeError("CONFIRMATION_STALE: 保险柜已变化")) == {
        "ok": False, "error": "CONFIRMATION_STALE", "message": "保险柜已变化"}
    assert collection.error_report(FileNotFoundError("不是有效任务目录"))["error"] == "FileNotFoundError"


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
