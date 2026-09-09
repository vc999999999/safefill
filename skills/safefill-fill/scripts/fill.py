"""SafeFill · 填写端：读取 Agent 请求包，优先用本机保险柜取值后产出 .yintian 密文。

只在员工自己的电脑上运行：纯本地、不联网；只读显式指定的文件；
默认 AI 请求包使用 yintian-request/1，提交使用 yintian-submission/4；旧表单协议仅作兼容。
保险柜（vault-*.yintian-vault）静态加密存于 SKILL_ROOT/data/，本机密钥解锁；
无保险柜或缺字段时回退到对话收集（submit）。
"""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import re
import secrets
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any

FORM_FORMAT = "yintian-form/1"
LEGACY_OPEN_FORM_FORMAT = "yintian-form/3"
OPEN_REQUEST_FORMAT = "yintian-request/1"
GROUP_INVITE_PREFIX = "GRP-"
IMAGE_MIME_BY_SUFFIX = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
PDF_SUFFIX = ".pdf"
PDF_MIME = "application/pdf"
NOTICE_KEYS = ("title", "purpose", "deadline", "retention_until", "contact", "correction")
REQUIRED_FORM_KEYS = ("task_id", "format_version", "template_version", "fields", "public_key_pem", "key_id", "schema_hash", "notice_hash") + NOTICE_KEYS
VERIFY_HINT = "填写前请与发放人核对任务编号与公钥指纹（key_id）；不一致请勿填写。"


class FillError(Exception):
    """填写端可预期错误：信息面向员工，命令以非零退出。"""


def _bootstrap_repo_scripts() -> None:
    scripts = Path(__file__).resolve().parent
    if not all((scripts / name).is_file() for name in ('collection.py', 'ocr_matcher.py', 'secure_io.py', 'evidence_routing.py', 'vault.py')):
        raise RuntimeError("填写者 Skill 安装不完整，请重新复制整个 safefill-fill 文件夹")
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))


_bootstrap_repo_scripts()
import collection  # noqa: E402
import fill_extract  # noqa: E402
import secure_io  # noqa: E402
import evidence_routing  # noqa: E402
import vault  # noqa: E402


def _public_key_fingerprint(public_key_pem: str) -> str:
    import hashlib

    from cryptography.hazmat.primitives import serialization

    public_key = serialization.load_pem_public_key(public_key_pem.encode("utf-8"))
    public_der = public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(public_der).hexdigest()[:24]


def load_form(form_path: str | Path) -> dict[str, Any]:
    """读取机器请求包；兼容旧定向、群发和开放表单协议，不修改文件。"""
    path = secure_io.checked_path(form_path)
    if not path.is_file():
        raise FillError(f"信息请求包不存在: {form_path}")
    try:
        form = json.loads(secure_io.read_bytes(path, 1024 * 1024))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FillError("信息请求包不是有效 JSON") from exc
    if not isinstance(form, dict) or form.get("format") not in {FORM_FORMAT, "yintian-form/2", LEGACY_OPEN_FORM_FORMAT, OPEN_REQUEST_FORMAT}:
        raise FillError("不是受支持的 SafeFill 机器请求包")
    mode = form.get("mode")
    if mode not in ("group", "directed", "open"):
        raise FillError("信息请求包 mode 无效（应为 group、directed 或 open）")
    expected_formats = {"directed": {FORM_FORMAT}, "group": {"yintian-form/2"}, "open": {LEGACY_OPEN_FORM_FORMAT, OPEN_REQUEST_FORMAT}}[mode]
    if form.get("format") not in expected_formats:
        raise FillError("REQUEST_MODE_MISMATCH: 请求文件格式与 mode 不匹配")
    if mode == "group" and (form.get("format") != "yintian-form/2" or form.get("submission_auth") != collection.AUTH_VERSION or form.get("format_version") != collection.GROUP_FORMAT_VERSION):
        raise FillError("GROUP_AUTH_REQUIRED: 旧群发模板没有个人认证，请向 HR 索取新版模板及个人凭据")
    if mode == "open" and form.get("format_version") != collection.OPEN_FORMAT_VERSION:
        raise FillError("OPEN_REQUEST_INVALID: 开放请求包格式无效")
    if form.get("format") == OPEN_REQUEST_FORMAT and (form.get("kind") != "agent_request" or form.get("target_skill") != "safefill-fill"):
        raise FillError("OPEN_REQUEST_INVALID: 不是发给 safefill-fill 的 Agent 请求包")
    missing = [key for key in REQUIRED_FORM_KEYS if not form.get(key)]
    if missing:
        raise FillError(f"信息请求包缺少必要字段: {', '.join(missing)}")
    for key in (*NOTICE_KEYS, "template_version"):
        if not isinstance(form[key], str) or len(form[key]) > collection.MAX_VALUE_CHARS:
            raise FillError(f"FORM_INVALID: {key} 必须是长度受限的文本")
    if not collection.TASK_ID_RE.fullmatch(str(form["task_id"])):
        raise FillError("信息请求包 task_id 无效")
    if not isinstance(form["public_key_pem"], str):
        raise FillError("FORM_INVALID: public_key_pem 必须是文本")
    if not re.fullmatch(r"[0-9a-f]{24}", str(form["key_id"])) or any(not re.fullmatch(r"[0-9a-f]{64}", str(form[key])) for key in ("schema_hash", "notice_hash")):
        raise FillError("FORM_INVALID: 公钥或摘要标识格式无效")
    fields = form["fields"]
    try:
        if collection.validate_field_definitions(fields, mode) != fields:
            raise ValueError("字段定义未规范化")
    except ValueError as exc:
        raise FillError(f"FORM_INVALID: {exc}") from exc
    if collection.sha256_bytes(collection.canonical(fields)) != form["schema_hash"]:
        raise FillError("字段清单与 schema_hash 不匹配，文件可能被篡改，请向发放人重新索取")
    notice = {key: form[key] for key in NOTICE_KEYS}
    if collection.sha256_bytes(collection.canonical(notice)) != form["notice_hash"]:
        raise FillError("告知内容与 notice_hash 不匹配，文件可能被篡改，请向发放人重新索取")
    if "notice" in form and isinstance(form["notice"], dict):
        if collection.sha256_bytes(collection.canonical(form["notice"])) != form["notice_hash"]:
            raise FillError("notice 摘要与 notice_hash 不匹配，文件可能被篡改，请向发放人重新索取")
    try:
        fingerprint = _public_key_fingerprint(form["public_key_pem"])
    except Exception as exc:
        raise FillError("FORM_INVALID: 公钥格式无效") from exc
    form["_key_fingerprint"] = fingerprint
    form["_path"] = str(path)
    expected_format = collection.GROUP_FORMAT_VERSION if mode == "group" else collection.OPEN_FORMAT_VERSION if mode == "open" else collection.FORMAT_VERSION
    if form["format_version"] != expected_format:
        raise FillError(f"需求格式文件要求的提交版本不受支持: {form['format_version']}")
    if mode == "directed":
        if not collection.INVITE_ID_RE.fullmatch(str(form.get("invite_id", ""))):
            raise FillError("directed 模式需求格式文件缺少有效 invite_id")
        token = form.get("invite_token") or form.get("token")
        if not isinstance(token, str) or not token:
            raise FillError("directed 模式需求格式文件缺少 invite_token")
        form["_token"] = token
    elif mode == "group":
        if any(key in form for key in ("invite_id", "invite_token", "token", "name", "employee_id")):
            raise FillError("group 模式公共模板不应包含个人身份或邀请信息")
    elif any(key in form for key in ("invite_id", "invite_token", "token", "name", "employee_id")):
        raise FillError("open 模式需求格式文件不应包含个人身份或邀请信息")
    return form


def bind_credential(form, credential_path=None):
    if form['mode'] == 'directed':
        if credential_path:
            raise FillError('CREDENTIAL_UNEXPECTED: 定向邀请不需要额外凭据')
        return form
    if form['mode'] == 'open':
        if credential_path:
            raise FillError('CREDENTIAL_UNEXPECTED: 开放请求包不需要个人凭据')
        return form
    if not credential_path:
        raise FillError('CREDENTIAL_REQUIRED: 群发填写需要 HR 私下发给本人的 .yintian-credential')
    credential = json.loads(secure_io.read_bytes(credential_path, 16 * 1024))
    if not isinstance(credential, dict) or credential.get('format') != collection.CREDENTIAL_FORMAT:
        raise FillError('CREDENTIAL_INVALID: 个人凭据格式无效')
    if any(credential.get(key) != form.get(key) for key in ('task_id', 'schema_hash', 'key_id')):
        raise FillError('CREDENTIAL_MISMATCH: 凭据与任务或收集方不符')
    employee_id = collection.parse_group_employee_id(credential.get('invite_id', ''))
    token = credential.get('invite_token')
    if credential.get('employee_id') != employee_id or not credential.get('name') or not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', token):
        raise FillError('CREDENTIAL_INVALID: 个人凭据内容无效')
    return {**form, '_token': token, '_employee_id': employee_id, 'name': credential['name'], 'invite_id': credential['invite_id']}


def inspect_info(form: dict[str, Any]) -> dict[str, Any]:
    info = {
        "format": form["format"],
        "mode": form["mode"],
        "task_id": form["task_id"],
        "title": form["title"],
        "purpose": form["purpose"],
        "deadline": form["deadline"],
        "retention_until": form["retention_until"],
        "contact": form["contact"],
        "correction": form["correction"],
        "key_id": form["key_id"],
        "key_fingerprint": form["_key_fingerprint"],
        "key_id_match": form["_key_fingerprint"] == form["key_id"],
        "schema_hash": form["schema_hash"],
        "notice_hash": form["notice_hash"],
        "fields": [
            {
                "id": field["id"],
                "label": field["label"],
                "type": field["type"],
                "required": bool(field.get("required")),
                **({"ocr_backend": field.get("ocr_backend", "local"), "ocr_fields": evidence_routing.bindings(field, form["fields"])} if field["type"] in collection.ATTACHMENT_TYPES else {}),
                "sensitive": bool(field.get("sensitive")),
                **({"options": field["options"]} if field.get("type") == "single_choice" and field.get("options") else {}),
                **({"multiple": bool(field.get("multiple"))} if field["type"] in collection.ATTACHMENT_TYPES else {}),
            }
            for field in form["fields"]
        ],
        "warning": VERIFY_HINT,
        "credential_required": form["mode"] == "group",
        "identity_assurance": "self_declared" if form["mode"] == "open" else "credential_bound",
        "expired": collection.task_expired(form),
    }
    if form["mode"] == "directed":
        info["invite_id"] = form["invite_id"]
        if form.get("name"):
            info["name"] = form["name"]
    return info


def print_inspect(info: dict[str, Any]) -> None:
    info = {key: collection.terminal_text(value) if isinstance(value, str) else value for key, value in info.items()}
    info['fields'] = [{**field, 'label': collection.terminal_text(field['label']),
                       **({'options': [collection.terminal_text(value) for value in field['options']]} if 'options' in field else {})} for field in info['fields']]
    mode_label = {"directed": "directed 定向邀请", "group": "group 群组模式（需要个人凭据）", "open": "open AI 请求包（姓名由本人填写）"}[info["mode"]]
    print(f"SafeFill 请求包：{info['format']} · {mode_label}")
    print(f"标题：{info['title']}")
    print(f"用途：{info['purpose']}")
    print(f"截止时间：{info['deadline']}")
    print(f"保存期限：{info['retention_until']}")
    print(f"联系人：{info['contact']}")
    print(f"更正方式：{info['correction']}")
    print(f"任务编号：{info['task_id']}")
    if info["mode"] == "directed":
        print(f"邀请编号：{info['invite_id']}")
        if info.get("name"):
            print(f"预填姓名：{info['name']}")
    match_label = "与内嵌公钥一致" if info["key_id_match"] else "与内嵌公钥不一致！请勿填写，向发放人重新索取"
    print(f"公钥指纹（key_id）：{info['key_id']}（{match_label}）")
    print("字段清单：")
    for field in info["fields"]:
        marks = "必填" if field["required"] else "选填"
        extra = f"，选项：{'/'.join(field['options'])}" if field.get("options") else ""
        extra += "，可多选文件" if field.get("multiple") else ""
        print(f"  - {field['id']}：{field['label']}（{field['type']}，{marks}{extra}）")
    print(info["warning"])


def mask_value(field: str, value: str) -> str:
    if field == "id_number" and len(value) >= 8:
        return value[:3] + "*" * (len(value) - 7) + value[-4:]
    if field == "phone" and len(value) >= 8:
        return value[:3] + "****" + value[-4:]
    return value[:1] + "***"


def _ocr_texts(image_path: Path) -> list[str]:
    from openvino_runtime import install_rapidocr_device_patch
    from rapidocr_openvino import RapidOCR

    install_rapidocr_device_patch()
    engine = RapidOCR()
    result, _stages = engine(str(image_path))
    return [text for _box, text, _score, *_ in (result or [])]


def extract_ocr_fields(path: Path) -> dict[str, Any]:
    """对显式指定的证件图做本地 OCR 并提取候选（未遮罩）；OCR 依赖缺失时给出去向明确的错误。"""
    try:
        texts = _ocr_texts(path)
    except (ImportError, RuntimeError) as exc:
        raise FillError(
            "LOCAL_OCR_UNAVAILABLE: rapidocr-openvino 为可选依赖，可执行 pip install -r requirements-ocr.txt；也可使用本人授权的宿主 Agent 识别或手工填写，无需 API Key"
        ) from exc
    return fill_extract.extract_fields("\n".join(texts), source=path.name)


def scan_idcard(image: str | Path) -> dict[str, Any]:
    """对显式指定的这一张证件图做本地 OCR 并提取候选；输出一律遮罩。"""
    path = secure_io.checked_path(image)
    if not path.is_file():
        raise FillError(f"图片不存在: {image}")
    extracted = extract_ocr_fields(path)
    candidates = {
        field: [{"value": mask_value(field, item["value"]), "confidence": item["confidence"]} for item in items]
        for field, items in extracted["candidates"].items()
    }
    return {"image": path.name, "candidates": candidates, "ambiguous": extracted["ambiguous"]}


def _resolve_attachment(raw: Any) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise FillError("附件路径无效（values.json 的 attachments 值应为文件路径字符串或字符串数组）")
    if ".." in PurePosixPath(raw.replace("\\", "/")).parts:
        raise FillError(f"附件路径不允许包含 ..: {raw}")
    path = secure_io.checked_path(raw)
    if not path.is_file():
        raise FillError(f"附件文件不存在: {raw}")
    return path


def _sniff_mime(path: Path, raw: bytes) -> str | None:
    suffix = path.suffix.lower()
    if suffix in IMAGE_MIME_BY_SUFFIX:
        mime = IMAGE_MIME_BY_SUFFIX[suffix]
        magic_ok = (
            (mime == "image/jpeg" and raw[:3] == b"\xff\xd8\xff")
            or (mime == "image/png" and raw[:8] == b"\x89PNG\r\n\x1a\n")
            or (mime == "image/webp" and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP")
        )
        return mime if magic_ok else None
    if suffix == PDF_SUFFIX:
        return PDF_MIME if raw[:5] == b"%PDF-" else None
    return None


def _check_values(form: dict[str, Any], values: Any) -> tuple[dict[str, str], list[str]]:
    """逐字段确定性校验，规则与收集端 validate_payload 完全一致；返回归一化值与全部问题。"""
    problems: list[str] = []
    if not isinstance(values, dict):
        raise FillError("values.json 的 values 必须是 {字段id: 值} 对象")
    if len(values) > collection.MAX_FIELDS:
        raise FillError("values 字段数量超过安全上限")
    labels = {field["id"]: field["label"] for field in form["fields"]}
    allowed_ids = {field["id"] for field in form["fields"] if field["type"] not in collection.ATTACHMENT_TYPES}
    unknown_ids = set(values) - allowed_ids
    problems.extend(f"未知字段: {key}（需求格式文件中不存在）" for key in sorted(unknown_ids))
    try:
        collection.validate_scalar_values({key: value for key, value in values.items() if key in allowed_ids}, allowed_ids)
    except ValueError as exc:
        raise FillError(str(exc)) from exc
    normalized: dict[str, str] = {}
    for field in form["fields"]:
        field_id, field_type = field["id"], field["type"]
        if field_type in collection.ATTACHMENT_TYPES:
            continue
        value = collection.normalize_value(field_type, values.get(field_id, ""))
        normalized[field_id] = value
        label = labels[field_id]
        if field.get("required") and not value:
            problems.append(f"必填缺失: {label}（{field_id}）")
        if value and field_type == "phone_cn" and not re.fullmatch(r"1[3-9]\d{9}", value):
            problems.append(f"手机号格式无效: {label}（{field_id}）")
        if value and field_type == "cn_id" and not fill_extract.validate_chinese_id(value):
            problems.append(f"身份证号格式、日期或校验码错误: {label}（{field_id}）")
        if value and field_type == "date" and not collection.valid_date(value):
            problems.append(f"日期无效: {label}（{field_id}）")
        if value and field_type == "single_choice" and value not in field.get("options", []):
            problems.append(f"取值不在选项内: {label}（{field_id}）")
    if form.get('name') and normalized.get('name') != form['name']:
        problems.append('NAME_MISMATCH: 姓名与个人邀请不符')
    if form.get('_employee_id') and normalized.get('employee_id') != form['_employee_id']:
        problems.append('IDENTITY_MISMATCH: 工号与个人凭据不符')
    return normalized, problems


def _build_attachments(form: dict[str, Any], specs: Any) -> tuple[dict[str, list], list[str]]:
    problems: list[str] = []
    if not isinstance(specs, dict):
        raise FillError("values.json 的 attachments 必须是 {字段id: 文件路径} 对象")
    labels = {field["id"]: field["label"] for field in form["fields"]}
    attachment_fields = {field["id"]: field for field in form["fields"] if field["type"] in collection.ATTACHMENT_TYPES}
    for key in specs:
        if key not in attachment_fields:
            problems.append(f"未知或非附件字段: {key}")
    payload: dict[str, list] = {}
    total_size = 0
    total_count = 0
    for field_id, field in attachment_fields.items():
        raw_spec = specs.get(field_id, [])
        raws = [raw_spec] if isinstance(raw_spec, str) else list(raw_spec) if isinstance(raw_spec, list) else None
        if raws is None:
            raise FillError(f"附件字段取值应为文件路径或路径数组: {field_id}")
        label = field["label"]
        if field.get("required") and not raws:
            problems.append(f"必填附件缺失: {label}（{field_id}）")
        if not field.get("multiple") and len(raws) > 1:
            problems.append(f"该附件字段不允许多个文件: {label}（{field_id}）")
        if len(raws) > 20:
            raise FillError('ATTACHMENT_LIMIT: 单字段最多 20 个附件')
        total_count += len(raws)
        if total_count > collection.MAX_ATTACHMENTS:
            raise FillError(f'ATTACHMENT_LIMIT: 附件总数最多 {collection.MAX_ATTACHMENTS} 个')
        items = []
        for raw_path in raws:
            path = _resolve_attachment(raw_path)
            if path.stat().st_size > collection.MAX_FILE_BYTES:
                raise FillError('ATTACHMENT_LIMIT: 单文件最多 5MB')
            data = secure_io.read_bytes(path, collection.MAX_FILE_BYTES)
            mime = _sniff_mime(path, data)
            if mime is None:
                problems.append(f"附件类型不支持或内容与扩展名不符: {label}（{path.name}）")
                continue
            if field["type"] == "image_attachment" and not mime.startswith("image/"):
                problems.append(f"图片字段收到非图片文件: {label}（{path.name}）")
                continue
            if field["type"] == "pdf_attachment" and mime != PDF_MIME:
                problems.append(f"PDF 字段收到非 PDF 文件: {label}（{path.name}）")
                continue
            if len(data) > collection.MAX_FILE_BYTES:
                problems.append(f"附件超过 5MB 上限: {label}（{path.name}）")
                continue
            total_size += len(data)
            if total_size > collection.MAX_TOTAL_BYTES:
                raise FillError('ATTACHMENT_LIMIT: 附件总大小最多 15MB')
            items.append(
                {
                    "name": path.name,
                    "type": mime,
                    "size": len(data),
                    "sha256": collection.sha256_bytes(data),
                    "data_b64": base64.b64encode(data).decode("ascii"),
                }
            )
        payload[field_id] = items
    if total_size > collection.MAX_TOTAL_BYTES:
        problems.append("附件总大小超过 15MB")
    return payload, problems


def _warn_values_permissions(values_path: Path) -> None:
    if os.name == "nt":
        return
    if stat.S_IMODE(values_path.stat().st_mode) & 0o077:
        print(f"警告: {values_path} 含明文且权限宽于 0600，建议执行 chmod 600，并在用后删除。", file=sys.stderr)


def _require_private_answers(values_path: Path) -> None:
    if os.name != "nt" and stat.S_IMODE(values_path.stat().st_mode) & 0o077:
        raise FillError("ANSWERS_PERMISSIONS: 临时明文必须使用 0600 权限")


def reply_filename(name: str) -> str:
    cleaned = collection.safe_filename_component(name, "reply")
    return f"{cleaned}-{collection.random_id('', 6)}.yintian"


def seal_form(form_path, values_path, out_path, *, confirmed=False, credential_path=None):
    if confirmed is not True:
        raise FillError('CONSENT_REQUIRED: 未确认时不读取填写资料')
    form = bind_credential(load_form(form_path), credential_path)
    values_file = secure_io.checked_path(values_path)
    _warn_values_permissions(values_file)
    data = json.loads(secure_io.read_bytes(values_file, collection.MAX_ENVELOPE_BYTES))
    if not isinstance(data, dict):
        raise FillError("VALUES_INVALID: 填写值必须是 JSON 对象")
    attachments, problems = _build_attachments(form, data.get("attachments", {}))
    if problems:
        raise FillError("ATTACHMENTS_INVALID: " + "; ".join(problems))
    return seal_data(form, data.get("values", {}), attachments, out_path, confirmed=confirmed)


def seal_data(form, values, attachments, out_path, *, confirmed=False, agent_ocr=None, agent_confirmed=False, invite_id=None):
    """No files/passwords/terminal I/O: consume the exact in-memory data approved by the caller."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if confirmed is not True:
        raise FillError("CONSENT_REQUIRED: 必须先由本人确认本次字段及附件")
    if collection.task_expired(form):
        raise FillError("TASK_EXPIRED: 已超过保存期限，请向 HR 索取新请求包")
    if form["_key_fingerprint"] != form["key_id"]:
        raise FillError("KEY_MISMATCH: 公钥指纹不一致")
    if form["mode"] != "open" and not form.get("_token"):
        raise FillError("CREDENTIAL_REQUIRED: 缺少个人认证凭据")
    normalized, problems = _check_values(form, values)
    if problems:
        raise FillError("VALUES_INVALID: " + "; ".join(problems))
    invite_id = invite_id or (collection.random_id(collection.OPEN_INVITE_PREFIX, 16) if form["mode"] == "open" else form["invite_id"])
    if form["mode"] == "open" and not collection.OPEN_INVITE_ID_RE.fullmatch(invite_id):
        raise FillError("PREVIOUS_INVALID: 更正回执编号无效")

    payload = {
        "format_version": form.get("format_version", collection.FORMAT_VERSION),
        "task_id": form["task_id"],
        "invite_id": invite_id,
        "schema_hash": form["schema_hash"],
        "notice_hash": form["notice_hash"],
        "template_version": str(form.get("template_version", "1.0")),
        "submitted_at": collection.now_iso(),
        "consent_confirmed": True,
        "values": normalized,
        "attachments": attachments,
    }
    if form["mode"] != "open":
        payload["invite_token"] = form["_token"]
    if agent_ocr is not None:
        if agent_confirmed is not True:
            raise FillError('AGENT_CONSENT_REQUIRED: 本人尚未确认使用宿主 Agent 识别候选')
        payload.update(agent_ocr=agent_ocr, agent_ocr_confirmed=True)
    envelope: dict[str, Any] = {
        "format_version": form.get("format_version", collection.FORMAT_VERSION),
        "task_id": form["task_id"],
        "invite_id": invite_id,
        "schema_hash": form["schema_hash"],
        "key_id": form["key_id"],
        "algorithms": {"content": "AES-256-GCM", "key_wrap": "RSA-OAEP-3072-SHA256"},
    }
    token = form.get("_token", "")
    invite = {"name": form.get("name", normalized.get("name", "")), "employee_id": form.get("_employee_id", ""),
              "token_hash": collection.sha256_bytes(token.encode()) if token else ""}
    missing, conflicts, _ = collection.validate_payload(form, invite, payload)
    if missing or conflicts:
        raise FillError("PAYLOAD_INVALID: " + ",".join(missing + conflicts))
    aes_key = AESGCM.generate_key(bit_length=256)
    iv = secrets.token_bytes(12)
    aad = collection.canonical([envelope["format_version"], envelope["task_id"], envelope["invite_id"], envelope["schema_hash"], envelope["key_id"]])
    plaintext = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ciphertext = AESGCM(aes_key).encrypt(iv, plaintext, aad)
    public_key = serialization.load_pem_public_key(str(form["public_key_pem"]).encode("utf-8"))
    wrapped = public_key.encrypt(aes_key, padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    envelope.update(
        encrypted_key_b64=base64.b64encode(wrapped).decode("ascii"),
        iv_b64=base64.b64encode(iv).decode("ascii"),
        ciphertext_b64=base64.b64encode(ciphertext).decode("ascii"),
    )
    if form["mode"] == "group":
        envelope["auth_tag"] = collection.submission_auth_tag(envelope, collection.sha256_bytes(form["_token"].encode()))
    blob = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(blob) > collection.MAX_ENVELOPE_BYTES:
        raise FillError("加密信封超过 32MB 上限，未生成文件")
    out = Path(out_path).expanduser()
    if out.suffix != ".yintian":
        out = out.with_suffix(out.suffix + ".yintian") if out.suffix else out.with_suffix(".yintian")
    secure_io.atomic_write(out, blob, overwrite=False)
    if os.name != "nt":
        os.chmod(out, 0o600)
    return {
        "out": str(out.resolve()),
        "task_id": form["task_id"],
        "invite_id": invite_id,
        "mode": form["mode"],
        "fields": len(normalized),
        "attachments": sum(len(items) for items in attachments.values()),
        "bytes": len(blob),
    }


def cmd_inspect(args) -> dict[str, Any] | None:
    info = inspect_info(load_form(args.form))
    if args.json:
        return info
    print_inspect(info)
    return None


def cmd_scan_idcard(args) -> dict[str, Any] | None:
    result = scan_idcard(args.image)
    if args.json:
        return result
    print(f"证件扫描候选（已遮罩）：{result['image']}")
    if not result["candidates"]:
        print("未识别到姓名、身份证号或手机号候选；请检查图片清晰度或改为手动填写。")
    for field, items in result["candidates"].items():
        for item in items:
            print(f"  - {field}: {item['value']}（{item['confidence']}）")
    if result["ambiguous"]:
        print(f"存在歧义字段: {', '.join(result['ambiguous'])}；请人工核对后在 values.json 中填写正确取值。")
    print("以上仅为候选；请将确认后的真实取值自行写入 values.json，工具不会代写。")
    return None


def cmd_openvino_status(_args) -> dict[str, Any]:
    from openvino_runtime import runtime_info

    return runtime_info()


def prepare_agent_result(form, attachments, path):
    if path is None:
        return None
    result = evidence_routing.load_result(path)
    items = [{'field_id': field_id, 'data': base64.b64decode(item['data_b64'], validate=True)}
             for field_id, group in attachments.items() for item in group]
    return evidence_routing.validate_result(form, items, result)


def confirm_submission(form, values, attachments, agent_ocr=None):
    collection.require_tty("fill confirmation")
    print_inspect(inspect_info(form))
    if input("输入通过独立渠道与 HR 核对的公钥指纹: ").strip() != form["key_id"]:
        raise FillError("KEY_UNCONFIRMED: 收集方公钥未确认")
    print("本次提供的信息（仅本人终端显示）：")
    for field in form["fields"]:
        field_id = field["id"]
        value = f"{len(attachments.get(field_id, []))} 个附件" if field["type"] in collection.ATTACHMENT_TYPES else values.get(field_id, "")
        print(f"{collection.terminal_text(field['label'])}: {value!r}")
    if agent_ocr is not None:
        print('本次包含宿主 Agent 识别候选；若宿主使用云端模型，所选原始附件已由云端处理。候选不会自动放行。')
        if input('确认是本人授权识别的附件，并同意随密文交回候选，输入 AGENT: ').strip() != 'AGENT':
            raise FillError('AGENT_CONSENT_REQUIRED: 已取消，不生成文件')
    expected = form["task_id"]
    if input(f"确认收集用途及以上内容，同意生成加密文件，请输入任务编号 {expected}: ").strip() != expected:
        raise FillError("CONSENT_REQUIRED: 已取消，不生成文件")


def cmd_seal(args):
    collection.require_tty("seal")
    form = bind_credential(load_form(args.form), getattr(args, "credential", None))
    path = secure_io.checked_path(args.values)
    _warn_values_permissions(path)
    data = json.loads(secure_io.read_bytes(path, collection.MAX_ENVELOPE_BYTES))
    if not isinstance(data, dict):
        raise FillError("VALUES_INVALID: 填写值格式无效")
    attachments, problems = _build_attachments(form, data.get("attachments", {}))
    values, value_problems = _check_values(form, data.get("values", {}))
    if problems or value_problems:
        raise FillError("VALUES_INVALID: " + "; ".join(problems + value_problems))
    agent_ocr = prepare_agent_result(form, attachments, getattr(args, 'agent_ocr', None))
    if agent_ocr is not None:
        confirm_submission(form, values, attachments, agent_ocr)
    else:
        confirm_submission(form, values, attachments)
    summary = seal_data(form, values, attachments, args.out, confirmed=True,
                        agent_ocr=agent_ocr, agent_confirmed=agent_ocr is not None)
    print("只交回 .yintian；手工 values.json 仍含明文，请自行清理。", file=sys.stderr)
    return summary


def cmd_submit(args):
    form = bind_credential(load_form(args.form), getattr(args, "credential", None))
    path = secure_io.checked_path(args.answers)
    try:
        _require_private_answers(path)
        data = json.loads(secure_io.read_bytes(path, collection.MAX_ENVELOPE_BYTES))
        if not isinstance(data, dict) or data.get("consent_confirmed") is not True:
            raise FillError("CONSENT_REQUIRED: 员工必须确认本次字段及附件后才能生成密文")
        attachments, attachment_problems = _build_attachments(form, data.get("attachments", {}))
        values, value_problems = _check_values(form, data.get("values", {}))
        if attachment_problems or value_problems:
            raise FillError("VALUES_INVALID: " + "; ".join(attachment_problems + value_problems))
        out = args.out
        if not out:
            out = secure_io.checked_path(args.out_dir) / reply_filename(values.get("name", ""))
        invite_id = _previous_invite_id(form, getattr(args, "previous", None))
        return seal_data(form, values, attachments, out, confirmed=True, invite_id=invite_id)
    finally:
        path.unlink(missing_ok=True)


def _previous_invite_id(form, previous):
    if not previous:
        return None
    if form["mode"] != "open":
        raise FillError("PREVIOUS_UNEXPECTED: 仅开放请求包可沿用旧回执进行更正")
    try:
        envelope = json.loads(secure_io.read_bytes(previous, collection.MAX_ENVELOPE_BYTES))
        return collection.validate_envelope_header(envelope, form)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise FillError("PREVIOUS_INVALID: 旧回执不属于本请求包或已损坏") from exc


def _vault_path(args) -> Path:
    override = getattr(args, "vault", None)
    return secure_io.checked_path(override) if override else vault.default_vault_path()


def _read_temp_answers(path_str) -> dict[str, Any]:
    """读取 0700 目录中的 0600 临时 JSON；无论成败都删除，与 submit 同一纪律。"""
    path = secure_io.checked_path(path_str)
    try:
        _require_private_answers(path)
        data = json.loads(secure_io.read_bytes(path, collection.MAX_ENVELOPE_BYTES))
        if not isinstance(data, dict):
            raise FillError("VAULT_ANSWERS_INVALID: 临时 JSON 必须是对象")
        return data
    finally:
        path.unlink(missing_ok=True)


def _build_vault_attachments(entry_id: str, entry_type: str, paths: list) -> list:
    pseudo = {"fields": [{"id": entry_id, "label": entry_id, "type": entry_type, "required": False, "multiple": True}]}
    built, errors = _build_attachments(pseudo, {entry_id: paths})
    if errors:
        raise FillError("VAULT_ENTRY_INVALID: " + "; ".join(errors))
    return built[entry_id]


def _entry_specs(data: dict[str, Any]) -> dict[str, Any]:
    specs = data.get("entries")
    if not isinstance(specs, dict) or not specs:
        raise FillError('VAULT_ANSWERS_INVALID: 临时 JSON 需要非空 entries 对象：{"entries": {"phone": {"type": "phone_cn", "value": "..."}}}')
    if any(not collection.FIELD_ID_RE.fullmatch(str(entry_id)) for entry_id in specs):
        raise FillError("VAULT_ENTRY_INVALID: 条目 id 必须是小写英文/数字/下划线")
    return specs


def _build_entries(specs: dict[str, Any]) -> dict[str, Any]:
    entries = {}
    for entry_id, spec in specs.items():
        try:
            entries[entry_id] = vault.build_entry(entry_id, spec, _build_vault_attachments)
        except ValueError as exc:
            raise FillError(str(exc)) from exc
    return entries


def _unlock(args, *, create_key=False):
    vault_path = _vault_path(args)
    data_dir = vault.ensure_data_dir(vault_path.parent)
    key = vault.load_or_create_key(data_dir, create=create_key)
    return vault_path, key


def cmd_vault_status(args) -> dict[str, Any]:
    form = load_form(args.request) if getattr(args, "request", None) else None
    vault_path = _vault_path(args)
    if not vault_path.is_file():
        result: dict[str, Any] = {"vault": False, "vault_path": str(vault_path)}
        if form is not None:
            result["fields"] = [
                {"id": field["id"], "label": field["label"], "type": field["type"],
                 "required": bool(field.get("required")), "match": None, "status": "missing"}
                for field in form["fields"]
            ]
        return result
    vault_path, key = _unlock(args)
    try:
        profile = vault.load_vault(vault_path, key)
    except ValueError as exc:
        raise FillError(str(exc)) from exc
    return vault.status_view(profile, form)


def cmd_vault_init(args) -> dict[str, Any]:
    vault_path = _vault_path(args)
    if vault_path.exists():
        raise FillError("VAULT_EXISTS: 保险柜已存在，请用 vault-add 增补条目")
    entries = _build_entries(_entry_specs(_read_temp_answers(args.answers)))
    profile = vault.validate_profile({"format": vault.FORMAT_V2, "entries": entries})
    vault_path, key = _unlock(args, create_key=True)
    with secure_io.file_lock(str(vault_path) + ".lock"):
        try:
            vault.save_vault(vault_path, key, profile, create=True)
        except FileExistsError:
            raise FillError("VAULT_EXISTS: 保险柜已存在，请用 vault-add 增补条目") from None
    return {"vault": True, "vault_path": str(vault_path), "entry_count": len(entries)}


def cmd_vault_add(args) -> dict[str, Any]:
    vault_path = _vault_path(args)
    if not vault_path.is_file():
        raise FillError("VAULT_MISSING: 保险柜不存在，请先 vault-init")
    entries = _build_entries(_entry_specs(_read_temp_answers(args.answers)))
    vault_path, key = _unlock(args)
    with secure_io.file_lock(str(vault_path) + ".lock"):
        try:
            profile = vault.load_vault(vault_path, key)
        except ValueError as exc:
            raise FillError(str(exc)) from exc
        profile["entries"].update(entries)
        if len(profile["entries"]) > vault.MAX_ENTRIES:
            raise FillError("VAULT_LIMIT: 保险柜条目超过数量上限")
        try:
            vault.save_vault(vault_path, key, profile)
        except ValueError as exc:
            raise FillError(str(exc)) from exc
    return {"vault": True, "updated": sorted(entries), "entry_count": len(profile["entries"])}


def cmd_vault_scan(args) -> dict[str, Any]:
    scans = []
    for image in args.images:
        path = secure_io.checked_path(image)
        if not path.is_file():
            raise FillError(f"图片不存在: {image}")
        digest = collection.sha256_bytes(secure_io.read_bytes(path, collection.MAX_FILE_BYTES))
        backend, note, extracted = "openvino-ocr", None, None
        if getattr(args, "vlm", False):
            try:
                import vlm_extract

                extracted = vlm_extract.extract_fields(path)
                backend = "openvino-vlm"
            except Exception as exc:
                note = f"VLM 不可用，已回退本地 OCR: {exc}"
        if extracted is None:
            try:
                extracted = extract_ocr_fields(path)
            except FillError as exc:
                if note:
                    raise FillError(f"{note}；本地 OCR 也不可用: {exc}") from exc
                raise
        scans.append({
            "image": path.name,
            "sha256": digest,
            "backend": backend,
            **({"note": note} if note else {}),
            "fields": extracted["fields"],
            "candidates": extracted["candidates"],
            "ambiguous": extracted["ambiguous"],
        })
    return {"scans": scans, "hint": "候选仅供本人确认；确认后用 vault-add 写入保险柜（source.kind 记为对应 backend）。"}


def _check_vault_attachments(form: dict[str, Any], attachments: dict[str, list]) -> list[str]:
    problems: list[str] = []
    attachment_fields = {field["id"]: field for field in form["fields"] if field["type"] in collection.ATTACHMENT_TYPES}
    for key in attachments:
        if key not in attachment_fields:
            problems.append(f"未知或非附件字段: {key}")
    total_size = 0
    for field_id, items in attachments.items():
        field = attachment_fields.get(field_id)
        if field is None:
            continue
        label = field["label"]
        if field.get("required") and not items:
            problems.append(f"必填附件缺失: {label}（{field_id}）")
        if not field.get("multiple") and len(items) > 1:
            problems.append(f"该附件字段不允许多个文件: {label}（{field_id}）")
        for item in items:
            mime = str(item.get("type", ""))
            if field["type"] == "image_attachment" and not mime.startswith("image/"):
                problems.append(f"图片字段的保险柜附件不是图片: {label}（{item.get('name', '?')}）")
            if field["type"] == "pdf_attachment" and mime != PDF_MIME:
                problems.append(f"PDF 字段的保险柜附件不是 PDF: {label}（{item.get('name', '?')}）")
            total_size += int(item.get("size", 0))
            if total_size > collection.MAX_TOTAL_BYTES:
                problems.append("附件总大小超过 15MB")
    return problems


def cmd_vault_fill(args) -> dict[str, Any]:
    if not args.confirmed:
        raise FillError("CONSENT_REQUIRED: 需本人在对话中确认本次内容后，Agent 才能加 --confirmed 生成回执")
    form = bind_credential(load_form(args.form), getattr(args, "credential", None))
    if collection.task_expired(form):
        raise FillError("TASK_EXPIRED: 请求包已过期，请向 HR 索取新请求包")
    vault_path = _vault_path(args)
    if not vault_path.is_file():
        raise FillError("VAULT_MISSING: 保险柜不存在，请先 vault-init")
    mapping = {}
    if getattr(args, "mapping", None):
        mapping = json.loads(secure_io.read_bytes(secure_io.checked_path(args.mapping), 64 * 1024))
    vault_path, key = _unlock(args)
    try:
        profile = vault.load_vault(vault_path, key)
        values, attachments, missing, matches = vault.select_fields(form, profile, mapping)
    except ValueError as exc:
        raise FillError(str(exc)) from exc
    if not values.get("name") and form.get("name"):
        values["name"] = form["name"]
    if form.get("_employee_id") and not values.get("employee_id"):
        values["employee_id"] = form["_employee_id"]
    labels = {field["id"]: field["label"] for field in form["fields"]}
    required_missing = [field_id for field_id in missing
                        if next(field for field in form["fields"] if field["id"] == field_id).get("required")]
    if required_missing:
        raise FillError("VAULT_FIELDS_MISSING: 保险柜缺少必填字段，请询问本人后用 vault-add 补录: "
                        + ", ".join(f"{labels[field_id]}（{field_id}）" for field_id in required_missing))
    attachment_problems = _check_vault_attachments(form, attachments)
    if attachment_problems:
        raise FillError("ATTACHMENTS_INVALID: " + "; ".join(attachment_problems))
    out = args.out
    if not out:
        out = secure_io.checked_path(args.out_dir) / reply_filename(values.get("name", ""))
    invite_id = _previous_invite_id(form, getattr(args, "previous", None))
    result = seal_data(form, values, attachments, out, confirmed=True, invite_id=invite_id)
    result["matched"] = matches
    return result


def cmd_vault_edit(args) -> dict[str, Any]:
    collection.require_tty("vault-edit")
    vault_path = _vault_path(args)
    if not vault_path.is_file():
        raise FillError("VAULT_MISSING: 保险柜不存在，请由 Agent 通过 vault-init 创建")
    vault_path, key = _unlock(args)
    with secure_io.file_lock(str(vault_path) + ".lock"):
        try:
            profile = vault.load_vault(vault_path, key)
        except ValueError as exc:
            raise FillError(str(exc)) from exc
        print("当前条目（标量值只显示首字；附件条目请由 Agent 用 vault-add 维护）：")
        for entry_id, entry in profile["entries"].items():
            shown = f"{len(entry['attachments'])} 个附件" if entry["type"] in collection.ATTACHMENT_TYPES else (entry["value"][:1] + "***" if entry.get("value") else "（空）")
            print(f"  - {entry_id}（{entry['type']}）: {shown}")
        while True:
            entry_id = input("输入要修改的条目 id（回车结束）: ").strip()
            if not entry_id:
                break
            if not collection.FIELD_ID_RE.fullmatch(entry_id):
                print("条目 id 无效，需小写英文/数字/下划线")
                continue
            old = profile["entries"].get(entry_id)
            raw = getpass.getpass(f"{entry_id} 新值（输入隐藏；- 删除该条目；回车保留）: ")
            if raw == "-":
                profile["entries"].pop(entry_id, None)
            elif raw:
                entry_type = old["type"] if old else input("新条目类型（text/phone_cn/cn_id/date/address）: ").strip()
                try:
                    profile["entries"][entry_id] = vault.build_entry(
                        entry_id, {"type": entry_type, "value": raw}, _build_vault_attachments)
                except ValueError as exc:
                    print(f"未保存: {collection.terminal_text(exc)}")
        if input("保存修改？输入 SAVE: ").strip() != "SAVE":
            raise FillError("CANCELLED: 已取消保存")
        vault.save_vault(vault_path, key, profile)
        return {"saved": True, "entry_count": len(profile["entries"])}


def cmd_vlm_setup(args) -> dict[str, Any]:
    try:
        import vlm_extract

        return vlm_extract.setup(model_id=getattr(args, "model", None))
    except Exception as exc:
        raise FillError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SafeFill · 填写端：本机填写需求格式文件并产出 .yintian 密文")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("inspect", help="查看需求格式文件的告知内容、字段清单与公钥指纹（不修改文件）")
    p.add_argument("form", metavar="REQUEST.yintian-request")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_inspect)
    p = sub.add_parser("openvino-status", help="查看 OpenVINO 版本、可用设备和当前设备选择")
    p.set_defaults(func=cmd_openvino_status)
    p = sub.add_parser("scan-idcard", help="本地 OCR 扫描一张证件图，遮罩输出候选（可选依赖）")
    p.add_argument("image", metavar="IMAGE")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_scan_idcard)
    p = sub.add_parser("seal", help="校验 values.json 并加密产出 .yintian 提交文件")
    p.add_argument("form", metavar="REQUEST.yintian-request")
    p.add_argument("--credential", help="HR 私下发放的个人凭据（群发必需）")
    p.add_argument("--agent-ocr", help="本人已授权宿主 Agent 生成的附件候选 JSON；不调用 API")
    p.add_argument("--values", required=True, help="填写值 JSON：{\"values\": {...}, \"attachments\": {...}}")
    p.add_argument("--out", required=True, help="输出 .yintian 路径")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_seal)
    p = sub.add_parser("submit", help="读取机器请求包，并在字段齐全且本人确认后生成加密提交")
    p.add_argument("form", metavar="REQUEST.yintian-request")
    p.add_argument("--answers", required=True, help="仅供本次加密使用的 0600 临时 JSON")
    output = p.add_mutually_exclusive_group(required=True)
    output.add_argument("--out", help="兼容入口：显式指定输出 .yintian 路径")
    output.add_argument("--out-dir", help="推荐入口：在目录中自动生成 姓名-短码.yintian")
    p.add_argument("--previous", help="更正时指定本人上一次开放请求回执，以替换同一条记录")
    p.add_argument("--credential", help="旧 group 模式的个人凭据；open 模式不需要")
    p.set_defaults(func=cmd_submit)
    p = sub.add_parser("vault-status", help="查看本机保险柜摘要及与请求包的字段匹配预览（不输出条目值）")
    p.add_argument("--request", metavar="REQUEST.yintian-request", help="可选：按该请求包字段预览 match/missing")
    p.add_argument("--vault", help="覆盖默认保险柜路径（默认 SKILL_ROOT/data/vault.yintian-vault）")
    p.set_defaults(func=cmd_vault_status)
    p = sub.add_parser("vault-init", help="首次创建本机加密保险柜（0600 临时 JSON 提供初始条目，用后删除）")
    p.add_argument("--answers", required=True, help='0700 目录内的 0600 临时 JSON：{"entries": {"phone": {"type": "phone_cn", "value": "..."}}}')
    p.add_argument("--vault", help="覆盖默认保险柜路径")
    p.set_defaults(func=cmd_vault_init)
    p = sub.add_parser("vault-add", help="向保险柜增补或更新条目（0600 临时 JSON，用后删除）")
    p.add_argument("--answers", required=True, help="与 vault-init 相同的临时 JSON 结构")
    p.add_argument("--vault", help="覆盖默认保险柜路径")
    p.set_defaults(func=cmd_vault_add)
    p = sub.add_parser("vault-scan", help="OpenVINO 本地识别本人明确指定的证件图，输出候选供确认后写入保险柜")
    p.add_argument("images", nargs="+", metavar="IMAGE")
    p.add_argument("--vlm", action="store_true", help="优先使用本地 VLM（需先 vlm-setup 并安装 requirements-vlm.txt），不可用自动回退 OCR")
    p.set_defaults(func=cmd_vault_scan)
    p = sub.add_parser("vault-fill", help="用保险柜匹配请求包字段，本人确认后生成 姓名-短码.yintian 回执")
    p.add_argument("form", metavar="REQUEST.yintian-request")
    p.add_argument("--mapping", help="请求字段 id → 保险柜条目 id 的映射 JSON（由 Agent 生成、本人确认）")
    output = p.add_mutually_exclusive_group(required=True)
    output.add_argument("--out", help="显式指定输出 .yintian 路径")
    output.add_argument("--out-dir", help="在目录中自动生成 姓名-短码.yintian")
    p.add_argument("--confirmed", action="store_true", help="本人已在对话中确认本次取值与映射")
    p.add_argument("--previous", help="更正时指定本人上一次开放请求回执，以替换同一条记录")
    p.add_argument("--credential", help="旧 group 模式的个人凭据；open 模式不需要")
    p.add_argument("--vault", help="覆盖默认保险柜路径")
    p.set_defaults(func=cmd_vault_fill)
    p = sub.add_parser("vault-edit", help="本人终端交互修改保险柜标量条目（人工回退通道）")
    p.add_argument("--vault", help="覆盖默认保险柜路径")
    p.set_defaults(func=cmd_vault_edit)
    p = sub.add_parser("vlm-setup", help="下载本地 VLM 模型到 data/models/（可选增强，不随 skill 分发）")
    p.add_argument("--model", help="Hugging Face 仓库 id（默认 OpenVINO 官方 int4 VLM）")
    p.set_defaults(func=cmd_vlm_setup)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = args.func(args)
        if result is not None:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(f"错误: {collection.terminal_text(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
