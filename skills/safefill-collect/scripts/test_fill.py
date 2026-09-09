"""填写端回归检查（pytest 可收集，也可直接运行）。

每个 test_ 函数用独立的 tmp_path 隔离；文件底部的 main() 依次调用全部
test_ 函数，使 `python scripts/test_fill.py` 仍可脱离 pytest 运行。
需求格式文件直接使用收集端 create_task 落盘的真实 .yintian-form。
"""
from __future__ import annotations

import base64
import contextlib
import csv
import inspect
import io
import json
import os
import stat
import sys
import tempfile
import types
from pathlib import Path

import collection
import fill
import fill_extract

import collection

PASSWORD = "FillSideCorrectHorse-2026"
VALID_ID = "110105198503071230"
PNG_1X1 = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


def assert_raises(expected, action, message: str) -> None:
    try:
        action()
    except expected:
        return
    raise AssertionError(message)


def make_task(tmp: Path, mode: str = "directed") -> types.SimpleNamespace:
    config = collection.default_config()
    config["purpose"] = "为依法办理员工商业保险收集必要身份资料"
    config["contact"] = "人事部王老师，内线 8001"
    config["correction"] = "在截止日前联系人事部王老师撤回原提交并重新提交"
    config["deadline"] = "2098-12-31T23:59:59Z"
    config["retention_until"] = "2099-12-31T23:59:59Z"
    if mode == "group":
        config["fields"].insert(0, {"id": "employee_id", "label": "工号", "type": "text", "required": True, "sensitive": False})
    config_path = tmp / "config.json"
    collection.dump_json(config_path, config)
    roster = tmp / "roster.csv"
    with roster.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["employee_id", "name"])
        writer.writerow(["E001", "张三"])
    created = collection.create_task(roster, config_path, tmp / "tasks", PASSWORD, require_terminal=False, mode=mode)
    task_dir = Path(created["task_dir"])
    if mode == "group":
        invite_id = "GRP-E001"
        token = collection.load_json(task_dir / "credentials/GRP-E001.yintian-credential")["invite_token"]
        form_src = task_dir / "FORM.yintian-form"
    else:
        invite_id = next(csv.DictReader((task_dir / "invite-index.csv").open(encoding="utf-8-sig")))["invite_id"]
        form_src = task_dir / "invites" / f"{invite_id}.yintian-form"
        token = json.loads(form_src.read_text(encoding="utf-8"))["invite_token"]
    return types.SimpleNamespace(tmp=tmp, task_dir=task_dir, task_id=created["task_id"], mode=mode, invite_id=invite_id, token=token, form_src=form_src)


def write_form(ns: types.SimpleNamespace, mutate=None, name: str = "form.yintian-form") -> Path:
    form = json.loads(ns.form_src.read_text(encoding="utf-8"))
    if mutate:
        mutate(form)
    path = ns.tmp / name
    path.write_text(json.dumps(form, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_values(tmp: Path, values: dict, attachments: dict | None = None, employee_id: str | None = None, name: str = "values.json") -> Path:
    data: dict = {"values": values, "attachments": attachments or {}}
    if employee_id is not None:
        data["employee_id"] = employee_id
    path = tmp / name
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if os.name != "nt":
        os.chmod(path, 0o600)
    return path


def make_images(tmp: Path) -> dict[str, str]:
    front, back = tmp / "front.png", tmp / "back.png"
    front.write_bytes(PNG_1X1)
    back.write_bytes(PNG_1X1)
    return {"id_front": str(front), "id_back": str(back)}


def good_values(mode: str = "directed") -> dict[str, str]:
    values = {"name": "张三", "phone": "13800138000", "id_number": VALID_ID, "address": "北京市朝阳区"}
    if mode == "group":
        values["employee_id"] = "E001"
    return values


def decrypt_payload(ns: types.SimpleNamespace, envelope_path: Path) -> dict:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    private_key = collection.unlock_private_key(ns.task_dir, PASSWORD)
    aes_key = private_key.decrypt(
        base64.b64decode(envelope["encrypted_key_b64"], validate=True),
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    plaintext = AESGCM(aes_key).decrypt(
        base64.b64decode(envelope["iv_b64"], validate=True),
        base64.b64decode(envelope["ciphertext_b64"], validate=True),
        collection.aad_for(envelope),
    )
    return json.loads(plaintext)


def seal_into_incoming(ns: types.SimpleNamespace, form_path: Path, values_path: Path, filename: str = "submit.yintian") -> Path:
    incoming = ns.tmp / "incoming"
    incoming.mkdir(exist_ok=True)
    out = incoming / filename
    credential = ns.task_dir / "credentials" / (ns.invite_id + ".yintian-credential") if ns.mode == "group" else None
    fill.seal_form(form_path, values_path, out, confirmed=True, credential_path=credential)
    return out


def ingest_review_verified(ns: types.SimpleNamespace) -> dict:
    original_ocr = collection.ocr_attachment  # venv 无 OCR 依赖，注入与填写值一致的确定性 OCR 结果
    collection.ocr_attachment = lambda item: (f"姓名 张三\n公民身份号码 {VALID_ID}\n联系电话 13800138000\n住址 北京市朝阳区\n", [])
    try:
        assert collection.ingest_task(ns.task_dir, ns.tmp / "incoming")["accepted"] == 1
        return collection.review_task(ns.task_dir, PASSWORD)
    finally:
        collection.ocr_attachment = original_ocr


def review_payload(ns: types.SimpleNamespace) -> dict:
    with collection.connect_db(ns.task_dir) as db:
        row = db.execute("SELECT path FROM submissions").fetchone()
    return decrypt_payload(ns, ns.task_dir / row["path"])


def assert_envelope_structure(envelope: dict, invite_id: str) -> None:
    assert list(envelope) == ["format_version", "task_id", "invite_id", "schema_hash", "key_id", "algorithms", "encrypted_key_b64", "iv_b64", "ciphertext_b64"] + (["auth_tag"] if invite_id.startswith("GRP-") else [])
    assert envelope["algorithms"] == {"content": "AES-256-GCM", "key_wrap": "RSA-OAEP-3072-SHA256"}
    assert envelope["invite_id"] == invite_id
    assert len(base64.b64decode(envelope["iv_b64"])) == 12


def test_directed_end_to_end_roundtrip(tmp_path: Path) -> None:
    """最高价值：create_task 落盘真实 directed form → seal → ingest → review 通过且明文值一致。"""
    ns = make_task(tmp_path)
    form_path = write_form(ns)
    values_path = write_values(tmp_path, good_values(), make_images(tmp_path))
    out = seal_into_incoming(ns, form_path, values_path)
    assert_envelope_structure(json.loads(out.read_text(encoding="utf-8")), ns.invite_id)
    reviewed = ingest_review_verified(ns)
    assert reviewed["verified"] == 1, reviewed
    task = collection.load_json(ns.task_dir / "task.json")
    payload = review_payload(ns)
    assert payload["values"] == good_values()
    assert payload["invite_token"] == ns.token
    assert payload["consent_confirmed"] is True and payload["notice_hash"] == task["notice_hash"]
    assert payload["format_version"] == collection.FORMAT_VERSION and payload["template_version"] == task["template_version"]
    assert sorted(payload["attachments"]) == ["id_back", "id_front"]
    assert all(item["type"] == "image/png" and item["sha256"] for items in payload["attachments"].values() for item in items)


def test_tampered_ciphertext_invalid(tmp_path: Path) -> None:
    ns = make_task(tmp_path)
    form_path = write_form(ns)
    values_path = write_values(tmp_path, good_values(), make_images(tmp_path))
    out = seal_into_incoming(ns, form_path, values_path)
    envelope = json.loads(out.read_text(encoding="utf-8"))
    raw = bytearray(base64.b64decode(envelope["ciphertext_b64"]))
    raw[-1] ^= 1
    envelope["ciphertext_b64"] = base64.b64encode(raw).decode()
    out.write_text(json.dumps(envelope), encoding="utf-8")
    assert collection.ingest_task(ns.task_dir, ns.tmp / "incoming")["accepted"] == 1
    assert collection.review_task(ns.task_dir, PASSWORD)["invalid"] == 1


def test_wrong_token_invalid(tmp_path: Path) -> None:
    ns = make_task(tmp_path)
    form_path = write_form(ns, mutate=lambda form: form.update(invite_token=form["invite_token"][:-2] + "Aa"))
    values_path = write_values(tmp_path, good_values(), make_images(tmp_path))
    seal_into_incoming(ns, form_path, values_path)
    assert collection.ingest_task(ns.task_dir, ns.tmp / "incoming")["accepted"] == 1
    assert collection.review_task(ns.task_dir, PASSWORD)["invalid"] == 1


def test_group_end_to_end_roundtrip(tmp_path: Path) -> None:
    """group 模式：invite_id=GRP-E001、载荷无 token；收集端已落地 group ingest 兼容，走真实端到端。"""
    ns = make_task(tmp_path, mode="group")
    form_path = write_form(ns)
    values_path = write_values(tmp_path, good_values(mode="group"), make_images(tmp_path))
    out = seal_into_incoming(ns, form_path, values_path, "group.yintian")
    assert_envelope_structure(json.loads(out.read_text(encoding="utf-8")), "GRP-E001")
    reviewed = ingest_review_verified(ns)
    assert reviewed["verified"] == 1, reviewed
    payload = review_payload(ns)
    assert payload["invite_id"] == "GRP-E001"
    assert payload["invite_token"] == ns.token
    assert payload["values"]["employee_id"] == "E001" and payload["values"]["id_number"] == VALID_ID


def test_inspect_output(tmp_path: Path) -> None:
    ns = make_task(tmp_path)
    form_path = write_form(ns)
    info = fill.inspect_info(fill.load_form(form_path))
    assert info["mode"] == "directed" and info["invite_id"] == ns.invite_id and info["name"] == "张三"
    assert info["purpose"] == "为依法办理员工商业保险收集必要身份资料"
    assert info["key_id"] == info["key_fingerprint"] and info["key_id_match"] is True
    assert [field["id"] for field in info["fields"]] == ["name", "phone", "id_number", "address", "id_front", "id_back"]
    capture = io.StringIO()
    with contextlib.redirect_stdout(capture):
        assert fill.main(["inspect", str(form_path)]) == 0
    text = capture.getvalue()
    assert "填写前请与发放人核对" in text and ns.task_id in text and "directed" in text
    capture = io.StringIO()
    with contextlib.redirect_stdout(capture):
        assert fill.main(["inspect", str(form_path), "--json"]) == 0
    parsed = json.loads(capture.getvalue())
    assert parsed["task_id"] == ns.task_id and parsed["key_id"] == info["key_id"]
    ns_group = make_task(tmp_path / "g", mode="group")
    capture = io.StringIO()
    with contextlib.redirect_stdout(capture):
        assert fill.main(["inspect", str(ns_group.form_src)]) == 0
    assert "group 群组模式" in capture.getvalue()


def test_inspect_detects_tampered_form(tmp_path: Path) -> None:
    ns = make_task(tmp_path)
    bad = write_form(ns, mutate=lambda form: form.update(purpose="篡改后的用途"), name="bad.yintian-form")
    assert_raises(fill.FillError, lambda: fill.load_form(bad), "notice_hash 不匹配必须拒绝")
    wrong_key = write_form(ns, mutate=lambda form: form.update(key_id="0" * 24), name="wrong-key.yintian-form")
    assert_raises(fill.FillError, lambda: fill.seal_form(wrong_key, write_values(tmp_path, good_values(), make_images(tmp_path)), tmp_path / "o.yintian", confirmed=True), "公钥指纹不一致必须拒绝")
    info = fill.inspect_info(fill.load_form(wrong_key))
    assert info["key_id_match"] is False


def test_seal_validation_rejects_and_writes_nothing(tmp_path: Path) -> None:
    ns = make_task(tmp_path)
    form_path = write_form(ns)
    images = make_images(tmp_path)
    out = tmp_path / "should-not-exist.yintian"
    cases = [
        ({key: value for key, value in good_values().items() if key != "phone"}, "必填缺失"),
        ({**good_values(), "id_number": "123456789012345678"}, "校验码"),
        ({**good_values(), "phone": "12345"}, "手机号"),
        ({**good_values(), "unknown_field": "x"}, "未知字段"),
    ]
    for values, keyword in cases:
        values_path = write_values(tmp_path, values, images)
        try:
            fill.seal_form(form_path, values_path, out, confirmed=True)
            raise AssertionError(f"校验应拒绝: {keyword}")
        except fill.FillError as exc:
            assert keyword in str(exc), str(exc)
        assert not out.exists()


def test_seal_lists_all_problems(tmp_path: Path) -> None:
    ns = make_task(tmp_path)
    form_path = write_form(ns)
    values = {**good_values(), "id_number": "123", "phone": "999"}
    del values["address"]
    values_path = write_values(tmp_path, values, make_images(tmp_path))
    try:
        fill.seal_form(form_path, values_path, tmp_path / "out.yintian", confirmed=True)
        raise AssertionError("应拒绝")
    except fill.FillError as exc:
        message = str(exc)
    assert "address" in message and "id_number" in message and "phone" in message  # 一次列全


def test_seal_attachment_path_guard(tmp_path: Path) -> None:
    ns = make_task(tmp_path)
    form_path = write_form(ns)
    images = make_images(tmp_path)
    values_path = write_values(tmp_path, good_values(), {"id_front": "../escape.png", "id_back": images["id_back"]})
    assert_raises(fill.FillError, lambda: fill.seal_form(form_path, values_path, tmp_path / "a.yintian", confirmed=True), "附件路径含 .. 必须明确报错")
    values_path = write_values(tmp_path, good_values(), {"id_front": str(tmp_path / "missing.png"), "id_back": images["id_back"]})
    assert_raises(fill.FillError, lambda: fill.seal_form(form_path, values_path, tmp_path / "b.yintian", confirmed=True), "附件不存在必须明确报错")
    assert not (tmp_path / "a.yintian").exists() and not (tmp_path / "b.yintian").exists()


def test_values_permission_warning(tmp_path: Path) -> None:
    if os.name == "nt":
        return
    ns = make_task(tmp_path)
    form_path = write_form(ns)
    values_path = write_values(tmp_path, good_values(), make_images(tmp_path))
    os.chmod(values_path, 0o644)
    capture = io.StringIO()
    with contextlib.redirect_stderr(capture):
        fill.seal_form(form_path, values_path, tmp_path / "ok.yintian", confirmed=True)
    assert "权限宽于 0600" in capture.getvalue()
    assert stat.S_IMODE((tmp_path / "ok.yintian").stat().st_mode) == 0o600


def test_scan_idcard_dependency_graceful(tmp_path: Path) -> None:
    image = tmp_path / "id.png"
    image.write_bytes(PNG_1X1)
    original = fill._ocr_texts
    fill._ocr_texts = lambda path: (_ for _ in ()).throw(ImportError("No module named 'rapidocr_openvino'"))
    try:
        try:
            fill.scan_idcard(image)
            raise AssertionError("依赖缺失应友好报错")
        except fill.FillError as exc:
            assert "rapidocr-openvino" in str(exc) and "pip install" in str(exc)
    finally:
        fill._ocr_texts = original
    assert_raises(fill.FillError, lambda: fill.scan_idcard(tmp_path / "nope.png"), "不存在的图片应明确报错")


def test_scan_idcard_masked_output(tmp_path: Path) -> None:
    image = tmp_path / "id.png"
    image.write_bytes(PNG_1X1)
    original = fill._ocr_texts
    fill._ocr_texts = lambda path: ["姓名 张三", f"公民身份号码 {VALID_ID}", "联系电话 13800138000"]
    try:
        result = fill.scan_idcard(image)
    finally:
        fill._ocr_texts = original
    shown = json.dumps(result, ensure_ascii=False)
    assert VALID_ID not in shown and "13800138000" not in shown and "张三" not in shown  # 输出一律遮罩
    assert result["candidates"]["id_number"][0]["value"] == VALID_ID[:3] + "*" * 11 + VALID_ID[-4:]
    assert result["candidates"]["phone"][0]["value"] == "138****8000"
    assert result["ambiguous"] == []


def test_scan_idcard_applies_openvino_device_patch() -> None:
    calls = []
    runtime = types.ModuleType("openvino_runtime")
    runtime.install_rapidocr_device_patch = lambda: calls.append("patch")
    rapidocr = types.ModuleType("rapidocr_openvino")

    class FakeRapidOCR:
        def __init__(self):
            assert calls == ["patch"]
            calls.append("engine")

        def __call__(self, _path):
            return [([], "姓名 张三", 0.99)], []

    rapidocr.RapidOCR = FakeRapidOCR
    sentinel = object()
    previous_runtime = sys.modules.get("openvino_runtime", sentinel)
    previous_rapidocr = sys.modules.get("rapidocr_openvino", sentinel)
    sys.modules["openvino_runtime"] = runtime
    sys.modules["rapidocr_openvino"] = rapidocr
    try:
        assert fill._ocr_texts(Path("id.png")) == ["姓名 张三"]
    finally:
        for name, previous in (("openvino_runtime", previous_runtime), ("rapidocr_openvino", previous_rapidocr)):
            if previous is sentinel:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
    assert calls == ["patch", "engine"]


def test_fill_extract_text_samples() -> None:
    text = f"姓名 张三\n性别 男\n公民身份号码 {VALID_ID}\n联系电话 13800138000\n住址 北京市朝阳区建国路 1 号\n"
    result = fill_extract.extract_fields(text)
    assert result["fields"]["id_number"] == {"value": VALID_ID, "confidence": "validated"}
    assert result["fields"]["name"]["value"] == "张三"
    assert result["fields"]["phone"]["value"] == "13800138000"
    assert result["ambiguous"] == []

    second_valid_id = "110105198503071249"  # 与 VALID_ID 同日期同地区但顺序码不同
    assert fill_extract.validate_chinese_id(second_valid_id)
    ambiguous = fill_extract.extract_fields(f"身份号码 {VALID_ID}\n另一证件 {second_valid_id}\n")
    assert ambiguous["ambiguous"] == ["id_number"]  # 多候选标歧义，不静默挑选
    assert {item["value"] for item in ambiguous["candidates"]["id_number"]} == {VALID_ID, second_valid_id}

    invalid = fill_extract.extract_fields("公民身份号码 110105198503071231\n")
    assert invalid["fields"]["id_number"]["confidence"] == "invalid_checksum"  # 校验失败明确标注
    assert fill_extract.extract_fields("没有任何可提取内容")["fields"] == {}


def main() -> None:
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_") and callable(fn)]
    for name, fn in tests:
        if "tmp_path" in inspect.signature(fn).parameters:
            with tempfile.TemporaryDirectory() as tmp:
                fn(Path(tmp))
        else:
            fn()
    print(f"PASS: safefill-fill workflow ({len(tests)} checks)")


if __name__ == "__main__":
    main()
