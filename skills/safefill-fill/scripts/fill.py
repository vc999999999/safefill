"""SafeFill · 填写端：读取 Agent 请求包，优先用本机保险柜取值后产出 .yintian 密文。

只在员工自己的电脑上运行：业务操作纯本地、不联网；仅显式 vlm-setup 安装模型时联网；只读显式指定的文件；
默认 AI 请求包使用 yintian-request/1，提交使用 yintian-submission/4；旧表单协议仅作兼容。
保险柜静态加密存于系统用户数据目录，本机密钥存于独立用户密钥目录；
无保险柜或缺字段时由 Agent 对话补齐，再经保险柜确认流程提交。
"""
from __future__ import annotations

import argparse
import base64
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
import evidence_routing  # noqa: E402
import fill_extract  # noqa: E402
import secure_io  # noqa: E402
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


def _require_private_answers(values_path: Path) -> None:
    if not values_path.is_file():
        raise FillError("ANSWERS_INVALID: 临时明文路径必须是普通文件")
    if os.name != "nt" and (stat.S_IMODE(values_path.stat().st_mode) & 0o077
                            or stat.S_IMODE(values_path.parent.stat().st_mode) & 0o077):
        raise FillError("ANSWERS_PERMISSIONS: 临时明文必须位于 0700 目录且文件权限为 0600")


def reply_filename(name: str) -> str:
    cleaned = collection.safe_filename_component(name, "reply")
    return f"{cleaned}-{collection.random_id('', 6)}.yintian"


def seal_data(form, values, attachments, out_path, *, confirmed=False, invite_id=None):
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


def cmd_inspect(args) -> dict[str, Any]:
    return inspect_info(load_form(args.form))


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


def _storage_paths(args) -> tuple[Path, Path, dict[str, Any] | None]:
    vault_override = getattr(args, "vault", None)
    key_override = getattr(args, "key_file", None)
    if bool(vault_override) != bool(key_override):
        raise FillError("VAULT_STORAGE_INVALID: 自定义 --vault 必须同时提供 --key-file")
    if vault_override:
        vault_path, key_path = secure_io.checked_path(vault_override), secure_io.checked_path(key_override)
        if vault_path == key_path:
            raise FillError("VAULT_STORAGE_INVALID: 保险柜与密钥路径必须不同")
        return vault_path, key_path, None
    try:
        return vault.resolve_default_storage()
    except (RuntimeError, ValueError) as exc:
        raise FillError(str(exc)) from exc


def _read_temp_answers(path_str) -> dict[str, Any]:
    """读取 0700 目录中的 0600 临时 JSON；无论成败都删除。"""
    path = secure_io.checked_path(path_str)
    try:
        _require_private_answers(path)
        data = json.loads(secure_io.read_bytes(path, collection.MAX_ENVELOPE_BYTES))
        if not isinstance(data, dict):
            raise FillError("VAULT_ANSWERS_INVALID: 临时 JSON 必须是对象")
        return data
    finally:
        if path.is_file():
            path.unlink()


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
    vault_path, key_path, migration = _storage_paths(args)
    try:
        if vault_path.is_file() and vault.vault_format(vault_path) == vault.FORMAT_V1:
            raise RuntimeError("VAULT_MIGRATION_REQUIRED: 请先用 vault-migrate 迁移旧保险柜")
        key = vault.load_or_create_key(key_path, create=create_key)
    except (RuntimeError, ValueError) as exc:
        raise FillError(str(exc)) from exc
    return vault_path, key_path, key, migration


def cmd_vault_status(args) -> dict[str, Any]:
    form = load_form(args.request) if getattr(args, "request", None) else None
    vault_path, key_path, migration = _storage_paths(args)
    if not vault_path.is_file():
        result: dict[str, Any] = {"vault": False, "vault_path": str(vault_path), "key_path": str(key_path)}
        if form is not None:
            result["fields"] = [
                {"id": field["id"], "label": field["label"], "type": field["type"],
                 "required": bool(field.get("required")), "match": None, "status": "missing"}
                for field in form["fields"]
            ]
        return result
    try:
        if vault.vault_format(vault_path) == vault.FORMAT_V1:
            return {"vault": True, "format": vault.FORMAT_V1, "vault_path": str(vault_path),
                    "migration_required": True, "migration_target": str(vault.default_vault_path())}
    except ValueError as exc:
        raise FillError(str(exc)) from exc
    vault_path, key_path, key, migration = _unlock(args)
    try:
        profile = vault.load_vault(vault_path, key)
    except ValueError as exc:
        raise FillError(str(exc)) from exc
    result = vault.status_view(profile, form)
    result.update(vault_path=str(vault_path), key_path=str(key_path))
    if migration:
        result["storage_migration"] = migration
    return result


def _entry_preview(entry: dict[str, Any] | None) -> Any:
    if entry is None:
        return None
    if entry["type"] in collection.ATTACHMENT_TYPES:
        return [{"name": item["name"], "size": item["size"], "sha256": item["sha256"]}
                for item in entry.get("attachments", [])]
    return entry.get("value", "")


def cmd_vault_stage(args) -> dict[str, Any]:
    answers_path = secure_io.checked_path(args.answers)
    try:
        vault_path, key_path, _migration = _storage_paths(args)
        confirmation_path = secure_io.checked_path(args.confirmation_out)
    except Exception:
        if answers_path.is_file():
            answers_path.unlink()
        raise
    if answers_path in {vault_path, key_path}:
        raise FillError("VAULT_PATH_COLLISION: 临时文件、确认文件、保险柜和密钥路径必须分开")
    if confirmation_path in {vault_path, key_path}:
        if answers_path.is_file():
            answers_path.unlink()
        raise FillError("VAULT_PATH_COLLISION: 临时文件、确认文件、保险柜和密钥路径必须分开")
    entries = _build_entries(_entry_specs(_read_temp_answers(str(answers_path))))
    try:
        vault.ensure_private_dir(vault_path.parent)
        vault.ensure_private_dir(key_path.parent)
        vault.ensure_private_dir(confirmation_path.parent)
    except (RuntimeError, ValueError) as exc:
        raise FillError(str(exc)) from exc
    vault_path, key_path, key, _migration = _unlock(args, create_key=True)
    with secure_io.file_lock(str(vault_path) + ".lock"):
        try:
            if vault_path.is_file() and vault.vault_format(vault_path) == vault.FORMAT_V1:
                raise FillError("VAULT_MIGRATION_REQUIRED: 请先用 vault-migrate 迁移旧保险柜")
            profile = (vault.load_vault(vault_path, key) if vault_path.is_file()
                       else {"format": vault.FORMAT_V2, "entries": {}})
        except (RuntimeError, ValueError) as exc:
            raise FillError(str(exc)) from exc
        candidate = {"format": vault.FORMAT_V2, "entries": {**profile["entries"], **entries}}
        if len(candidate["entries"]) > vault.MAX_ENTRIES:
            raise FillError("VAULT_LIMIT: 保险柜条目超过数量上限")
        try:
            vault.validate_profile(candidate)
            confirmation = vault.seal_confirmation(
                confirmation_path, key, "vault-change",
                {"vault_path": str(vault_path), "key_path": str(key_path),
                 "base_vault_sha256": vault.file_digest(vault_path) if vault_path.is_file() else None,
                 "entries": entries})
        except (FileExistsError, RuntimeError, ValueError) as exc:
            raise FillError(str(exc)) from exc
    changes = [
        {"id": entry_id, "type": entry["type"], "action": "update" if entry_id in profile["entries"] else "add",
         "old_value": _entry_preview(profile["entries"].get(entry_id)), "new_value": _entry_preview(entry),
         "source": entry["source"]["kind"]}
        for entry_id, entry in entries.items()
    ]
    return {"vault_path": str(vault_path), "changes": changes, **confirmation,
            "instruction": "请把以上完整新旧值交给员工确认；确认后才运行 vault-apply。"}


def cmd_vault_apply(args) -> dict[str, Any]:
    confirmation_path = secure_io.checked_path(args.confirmation)
    vault_path, key_path, key, _migration = _unlock(args)
    if confirmation_path in {vault_path, key_path}:
        raise FillError("VAULT_PATH_COLLISION: 确认文件、保险柜和密钥路径必须分开")
    verified = False
    try:
        staged = vault.open_confirmation(confirmation_path, key, "vault-change")
        verified = True
        if staged.get("vault_path") != str(vault_path) or staged.get("key_path") != str(key_path):
            raise FillError("CONFIRMATION_STALE: 确认文件不属于当前保险柜或密钥")
        entries = {entry_id: vault.validate_entry(entry) for entry_id, entry in staged.get("entries", {}).items()}
        with secure_io.file_lock(str(vault_path) + ".lock"):
            current_digest = vault.file_digest(vault_path) if vault_path.is_file() else None
            if current_digest != staged.get("base_vault_sha256"):
                raise FillError("CONFIRMATION_STALE: 保险柜已变化，请重新预览")
            old_blob = secure_io.read_bytes(vault_path, vault.MAX_BYTES) if vault_path.is_file() else None
            profile = (vault.load_vault(vault_path, key) if vault_path.is_file()
                       else {"format": vault.FORMAT_V2, "entries": {}})
            created = sorted(set(entries) - set(profile["entries"]))
            updated = sorted(set(entries) & set(profile["entries"]))
            profile["entries"].update(entries)
            if len(profile["entries"]) > vault.MAX_ENTRIES:
                raise FillError("VAULT_LIMIT: 保险柜条目超过数量上限")
            try:
                vault.ensure_private_dir(vault_path.parent)
                vault.save_vault(vault_path, key, profile, create=old_blob is None)
                vault.load_vault(vault_path, key)
                confirmation_path.unlink()
            except Exception:
                try:
                    if old_blob is None:
                        vault_path.unlink(missing_ok=True)
                    else:
                        secure_io.atomic_write(vault_path, old_blob)
                except Exception as rollback_exc:
                    raise FillError("VAULT_ROLLBACK_FAILED: 变更失败且保险柜回滚失败，请停止操作并保留现场") from rollback_exc
                raise
    except FillError:
        if verified:
            try:
                confirmation_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    except (RuntimeError, ValueError) as exc:
        if verified:
            try:
                confirmation_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise FillError(str(exc)) from exc
    return {"vault": True, "vault_path": str(vault_path), "created": created, "updated": updated,
            "entry_count": len(profile["entries"])}


def _read_password_file(path_str: str) -> str:
    path = secure_io.checked_path(path_str)
    try:
        _require_private_answers(path)
        password = secure_io.read_bytes(path, 4096).decode("utf-8").rstrip("\r\n")
        if not password:
            raise FillError("VAULT_PASSWORD_INVALID: 旧保险柜密码为空")
        return password
    finally:
        if path.is_file():
            path.unlink()


def cmd_vault_migrate(args) -> dict[str, Any]:
    password_path = secure_io.checked_path(args.password_file)
    try:
        source = secure_io.checked_path(args.source_vault) if args.source_vault else vault.legacy_vault_path()
        target = secure_io.checked_path(args.target_vault) if args.target_vault else vault.default_vault_path()
        target_key = secure_io.checked_path(args.target_key) if args.target_key else vault.default_key_path()
    except Exception:
        if password_path.is_file():
            password_path.unlink()
        raise
    if password_path in {source, target, target_key}:
        raise FillError("VAULT_PATH_COLLISION: 密码临时文件、源保险柜、目标保险柜和密钥路径必须分开")
    if len({source, target, target_key}) != 3:
        if password_path.is_file():
            password_path.unlink()
        raise FillError("VAULT_PATH_COLLISION: 密码临时文件、源保险柜、目标保险柜和密钥路径必须分开")
    password = _read_password_file(str(password_path))
    if bool(args.target_vault) != bool(args.target_key):
        raise FillError("VAULT_STORAGE_INVALID: 自定义 --target-vault 必须同时提供 --target-key")
    if source == target:
        raise FillError("VAULT_MIGRATION_INVALID: 迁移目标必须与旧保险柜不同，以保留原文件")
    try:
        return vault.migrate_v1(source, password, target, target_key)
    except (RuntimeError, ValueError) as exc:
        raise FillError(str(exc)) from exc


def cmd_vault_scan(args) -> dict[str, Any]:
    scans = []
    for image in args.images:
        path = secure_io.checked_path(image)
        if not path.is_file():
            raise FillError(f"图片不存在: {image}")
        digest = collection.sha256_bytes(secure_io.read_bytes(path, collection.MAX_FILE_BYTES))
        backend, extracted = "openvino-ocr", None
        if getattr(args, "vlm", False):
            if not args.model:
                raise FillError("VLM_MODEL_REQUIRED: 使用 --vlm 时必须用 --model 指定兼容模型")
            try:
                import vlm_extract

                extracted = vlm_extract.extract_fields(path, args.model, args.revision)
                backend = "openvino-vlm"
            except vlm_extract.VlmUnavailable as exc:
                raise FillError(f"VLM_UNAVAILABLE: {exc}；请由 Agent 改用核心 Python 重新运行不带 --vlm 的 vault-scan") from exc
        if extracted is None:
            extracted = extract_ocr_fields(path)
        scans.append({
            "image": path.name,
            "sha256": digest,
            "backend": backend,
            "fields": extracted["fields"],
            "candidates": extracted["candidates"],
            "ambiguous": extracted["ambiguous"],
        })
    return {"scans": scans, "hint": "候选仅供本人确认；含 needs_review 的字段必须人工逐字核对；确认后用 vault-stage/vault-apply 写入保险柜。"}


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


def _mapping(path_str: str | None) -> dict[str, str]:
    if not path_str:
        return {}
    try:
        data = json.loads(secure_io.read_bytes(secure_io.checked_path(path_str), 64 * 1024))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FillError("MAPPING_INVALID: 映射文件不是有效 JSON") from exc
    if not isinstance(data, dict):
        raise FillError("MAPPING_INVALID: 映射必须是对象")
    return data


def _prepare_vault_selection(form, profile, mapping):
    try:
        values, attachments, missing, matches = vault.select_fields(form, profile, mapping)
    except ValueError as exc:
        raise FillError(str(exc)) from exc
    if not values.get("name") and form.get("name"):
        values["name"] = form["name"]
        missing = [field_id for field_id in missing if field_id != "name"]
    if form.get("_employee_id") and not values.get("employee_id"):
        values["employee_id"] = form["_employee_id"]
        missing = [field_id for field_id in missing if field_id != "employee_id"]
    labels = {field["id"]: field["label"] for field in form["fields"]}
    required_missing = [field["id"] for field in form["fields"] if field.get("required") and field["id"] in missing]
    if required_missing:
        raise FillError("VAULT_FIELDS_MISSING: 保险柜缺少必填字段，请询问本人后用 vault-stage/vault-apply 补录: "
                        + ", ".join(f"{labels[field_id]}（{field_id}）" for field_id in required_missing))
    attachment_problems = _check_vault_attachments(form, attachments)
    if attachment_problems:
        raise FillError("ATTACHMENTS_INVALID: " + "; ".join(attachment_problems))
    return values, attachments, missing, matches


def _optional_digest(path_str: str | None, limit: int) -> str | None:
    return vault.file_digest(secure_io.checked_path(path_str), limit) if path_str else None


def cmd_vault_preview(args) -> dict[str, Any]:
    form = bind_credential(load_form(args.form), getattr(args, "credential", None))
    if collection.task_expired(form):
        raise FillError("TASK_EXPIRED: 请求包已过期，请向 HR 索取新请求包")
    vault_path, key_path, key, _migration = _unlock(args)
    if not vault_path.is_file():
        raise FillError("VAULT_MISSING: 保险柜不存在，请先 vault-stage/vault-apply")
    if vault.vault_format(vault_path) == vault.FORMAT_V1:
        raise FillError("VAULT_MIGRATION_REQUIRED: 请先用 vault-migrate 迁移旧保险柜")
    mapping = _mapping(getattr(args, "mapping", None))
    confirmation_path = secure_io.checked_path(args.confirmation_out)
    if confirmation_path in {vault_path, key_path}:
        raise FillError("VAULT_PATH_COLLISION: 确认文件、保险柜和密钥路径必须分开")
    with secure_io.file_lock(str(vault_path) + ".lock"):
        try:
            profile = vault.load_vault(vault_path, key)
            values, attachments, missing, matches = _prepare_vault_selection(form, profile, mapping)
            invite_id = _previous_invite_id(form, getattr(args, "previous", None))
            vault.ensure_private_dir(confirmation_path.parent)
            confirmation = vault.seal_confirmation(
                confirmation_path, key, "submission",
                {"request_sha256": vault.file_digest(Path(form["_path"]), 1024 * 1024),
                 "vault_path": str(vault_path), "key_path": str(key_path),
                 "vault_sha256": vault.file_digest(vault_path), "mapping": mapping,
                 "selection_sha256": collection.sha256_bytes(collection.canonical({"values": values, "attachments": attachments})),
                 "previous_sha256": _optional_digest(getattr(args, "previous", None), collection.MAX_ENVELOPE_BYTES),
                 "credential_sha256": _optional_digest(getattr(args, "credential", None), 16 * 1024),
                 "invite_id": invite_id})
        except (FileExistsError, RuntimeError, ValueError) as exc:
            raise FillError(str(exc)) from exc
        preview = []
        for field in form["fields"]:
            field_id = field["id"]
            if field_id in attachments:
                content = [{"name": item["name"], "size": item["size"], "sha256": item["sha256"]}
                           for item in attachments[field_id]]
            else:
                content = values.get(field_id)
            preview.append({"id": field_id, "label": field["label"], "type": field["type"],
                            "value": content, "source_entry": matches.get(field_id),
                            "source": (profile["entries"][matches[field_id]]["source"]["kind"]
                                       if field_id in matches else "request_identity" if field_id in values else None)})
    return {"fields": preview, "mapping": mapping, "optional_missing": missing, **confirmation,
            "instruction": "请在员工私有会话中逐项展示以上完整值；员工确认后才运行 vault-fill。"}


def cmd_vault_fill(args) -> dict[str, Any]:
    vault_path, key_path, key, _migration = _unlock(args)
    if not vault_path.is_file():
        raise FillError("VAULT_MISSING: 保险柜不存在，请先 vault-stage/vault-apply")
    confirmation_path = secure_io.checked_path(args.confirmation)
    if confirmation_path in {vault_path, key_path}:
        raise FillError("VAULT_PATH_COLLISION: 确认文件、保险柜和密钥路径必须分开")
    verified = False
    with secure_io.file_lock(str(vault_path) + ".lock"):
        try:
            approved = vault.open_confirmation(confirmation_path, key, "submission")
            verified = True
            mapping = approved.get("mapping")
            if not isinstance(mapping, dict):
                raise FillError("CONFIRMATION_INVALID: 确认文件中的映射无效")
            try:
                form = bind_credential(load_form(args.form), getattr(args, "credential", None))
                if collection.task_expired(form):
                    raise FillError("TASK_EXPIRED: 请求包已过期，请向 HR 索取新请求包")
                profile = vault.load_vault(vault_path, key)
                values, attachments, _missing, matches = _prepare_vault_selection(form, profile, mapping)
                expected = {
                    "request_sha256": vault.file_digest(Path(form["_path"]), 1024 * 1024),
                    "vault_path": str(vault_path),
                    "key_path": str(key_path),
                    "vault_sha256": vault.file_digest(vault_path),
                    "selection_sha256": collection.sha256_bytes(collection.canonical({"values": values, "attachments": attachments})),
                    "previous_sha256": _optional_digest(getattr(args, "previous", None), collection.MAX_ENVELOPE_BYTES),
                    "credential_sha256": _optional_digest(getattr(args, "credential", None), 16 * 1024),
                    "invite_id": _previous_invite_id(form, getattr(args, "previous", None)),
                }
            except FillError as exc:
                if str(exc).startswith("TASK_EXPIRED"):
                    raise
                raise FillError("CONFIRMATION_STALE: 请求、保险柜、取值、凭据或旧回执已变化，请重新预览") from exc
            except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
                raise FillError("CONFIRMATION_STALE: 请求、保险柜、取值、凭据或旧回执已变化，请重新预览") from exc
            if any(approved.get(key_name) != expected_value for key_name, expected_value in expected.items()):
                raise FillError("CONFIRMATION_STALE: 请求、保险柜、取值、凭据或旧回执已变化，请重新预览")
        except FillError:
            if verified:
                try:
                    confirmation_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise
        except (RuntimeError, ValueError) as exc:
            if verified:
                try:
                    confirmation_path.unlink(missing_ok=True)
                except OSError:
                    pass
            message = str(exc) if str(exc).startswith("CONFIRMATION_") else "CONFIRMATION_INVALID: 确认文件无效"
            raise FillError(message) from exc
        out = args.out
        if not out:
            out = secure_io.checked_path(args.out_dir) / reply_filename(values.get("name", ""))
        result = seal_data(form, values, attachments, out, confirmed=True, invite_id=approved.get("invite_id"))
        try:
            confirmation_path.unlink()
        except OSError as exc:
            try:
                Path(result["out"]).unlink()
            except OSError as rollback_exc:
                raise FillError("REPLY_ROLLBACK_FAILED: 确认文件无法消费且新回执无法撤回，请停止重试") from rollback_exc
            raise FillError("CONFIRMATION_CONSUME_FAILED: 确认文件无法消费，未保留回执") from exc
        result["matched"] = matches
        out_path = Path(result["out"])
        earlier = [path for path in out_path.parent.glob("*.yintian") if path != out_path]
        if earlier:
            result["warning"] = (f"输出目录已存在 {len(earlier)} 份回执，"
                                 "如其中已有本任务回执且已提交，请用 --previous 重新生成以免产生重复记录")
    return result


def cmd_vlm_setup(args) -> dict[str, Any]:
    try:
        import vlm_extract

        return vlm_extract.setup(args.model, args.revision, source=args.source)
    except Exception as exc:
        raise FillError(str(exc)) from exc


def _importable(module: str) -> bool:
    try:
        __import__(module)
        return True
    except Exception:
        return False


def _ocr_report() -> dict[str, Any]:
    install_hint = "pip install -r requirements-ocr.txt"
    python_too_new = sys.version_info >= (3, 12)
    if python_too_new:
        install_hint = ("本地 OCR 需 Python 3.11 环境后执行 pip install -r requirements-ocr.txt"
                        "（rapidocr-openvino 1.4.4 钉死的 openvino 2024.0.0 wheel 上限 cp311）")
    rapidocr_ok = _importable("rapidocr_openvino")
    openvino_ok = _importable("openvino")
    devices: list[str] = []
    details: list[str] = []
    if openvino_ok:
        try:
            from openvino import Core

            devices = sorted(str(device) for device in Core().available_devices)
        except Exception:
            details.append("OpenVINO 设备探测失败")
    try:
        import config

        configured = config.OCR_DEVICE
    except Exception:
        configured = "AUTO"
        details.append("YINTIAN_OCR_DEVICE 配置无效，按 AUTO 处理")
    # openvino 缺失时无法探测设备，device_ok 置 True 表示"不适用"，避免误报设备不匹配
    device_ok = configured == "AUTO" or not openvino_ok or configured in devices
    missing = [name for name, available in (("rapidocr_openvino", rapidocr_ok), ("openvino", openvino_ok)) if not available]
    if missing:
        details.insert(0, "缺少模块: " + ", ".join(missing))
    if devices:
        details.append("可用设备: " + ", ".join(devices))
    if configured != "AUTO" and devices and configured not in devices:
        details.append(f"配置的 OCR 设备 {configured} 不在可用设备中")
    if python_too_new:
        details.append("当前 Python 版本无 rapidocr-openvino 1.4.4 可用 wheel，本地 OCR 需 Python 3.11 环境")
    if not details:
        details.append(f"OCR 本地推理可用（设备配置 {configured}）")
    return {"ok": not missing, "detail": "；".join(details), "install_hint": install_hint if missing else None,
            "devices": devices, "device_ok": device_ok}


def _vlm_report() -> dict[str, Any]:
    install_hint = "在独立环境 pip install -r requirements-vlm.txt 后运行 vlm-setup --model MODEL"
    missing = [name for name in ("openvino", "openvino_genai", "huggingface_hub") if not _importable(name)]
    models: list[str] = []
    try:
        import vlm_extract

        root = vlm_extract.model_root()
        if root.is_dir():
            models = sorted(path.name for path in root.iterdir() if (path / vlm_extract.MANIFEST_NAME).is_file())
    except Exception:
        pass
    if missing:
        detail = "缺少模块: " + ", ".join(missing)
    else:
        detail = "VLM 依赖可用；已下载模型: " + (", ".join(models) if models else "无")
    return {"ok": not missing, "detail": detail, "install_hint": install_hint if missing else None, "models": models}


def _storage_report(args) -> dict[str, Any]:
    vault_override = getattr(args, "vault", None)
    key_override = getattr(args, "key_file", None)
    if bool(vault_override) != bool(key_override):
        return {"ok": False, "detail": "自定义 --vault 必须同时提供 --key-file", "install_hint": None}
    try:
        if vault_override:
            vault_path, key_path = secure_io.checked_path(vault_override), secure_io.checked_path(key_override)
            if vault_path == key_path:
                return {"ok": False, "detail": "保险柜与密钥路径必须不同", "install_hint": None}
        else:
            vault_path, key_path = vault.default_vault_path(), vault.default_key_path()
    except (RuntimeError, ValueError) as exc:
        return {"ok": False, "detail": f"无法解析保险柜路径: {exc}", "install_hint": None}
    problems: list[str] = []
    if os.name != "nt":
        for path, what in ((vault_path, "保险柜"), (key_path, "密钥")):
            if path.is_file() and stat.S_IMODE(path.stat().st_mode) & 0o077:
                problems.append(f"{what}权限宽于 0600: {path}")
            if path.parent.is_dir() and stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
                problems.append(f"{what}目录权限宽于 0700: {path.parent}")
    if problems:
        return {"ok": False, "detail": "；".join(problems), "install_hint": None}
    state = "已就绪" if vault_path.is_file() else "尚未创建（首次写入时自动创建）"
    return {"ok": True, "detail": f"保险柜{state}: {vault_path}", "install_hint": None}


def _environment_report(args) -> dict[str, Any]:
    python_ok = sys.version_info >= (3, 11)
    core_missing = [name for name in ("cryptography", "PIL") if not _importable(name)]
    return {
        "python": {
            "ok": python_ok,
            "detail": f"Python {sys.version.split()[0]} ({sys.executable})",
            "install_hint": None if python_ok else "安装 Python 3.11 或更高版本",
        },
        "core": {
            "ok": not core_missing,
            "detail": "核心依赖可用" if not core_missing else "缺少模块: " + ", ".join(core_missing),
            "install_hint": "pip install -r requirements.txt" if core_missing else None,
        },
        "ocr": _ocr_report(),
        "vlm": _vlm_report(),
        "storage": _storage_report(args),
    }


def cmd_doctor(args) -> dict[str, Any]:
    """只读环境检测：不联网、不写盘、不创建任何目录或文件。"""
    return _environment_report(args)


def _add_storage_args(parser) -> None:
    parser.add_argument("--vault", help="覆盖默认保险柜路径；必须同时提供 --key-file")
    parser.add_argument("--key-file", help="覆盖默认保险柜密钥路径；必须同时提供 --vault")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SafeFill · 填写端：本机填写需求格式文件并产出 .yintian 密文")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("doctor", help="只读检查 Python、核心/OCR/VLM 依赖与保险柜存储状态（不联网、不写盘）")
    _add_storage_args(p)
    p.set_defaults(func=cmd_doctor)
    p = sub.add_parser("inspect", help="查看需求格式文件的告知内容、字段清单与公钥指纹（不修改文件）")
    p.add_argument("form", metavar="REQUEST.yintian-request")
    p.set_defaults(func=cmd_inspect)
    p = sub.add_parser("vault-status", help="查看本机保险柜摘要及与请求包的字段匹配预览（不输出条目值）")
    p.add_argument("--request", metavar="REQUEST.yintian-request", help="可选：按该请求包字段预览 match/missing")
    _add_storage_args(p)
    p.set_defaults(func=cmd_vault_status)
    p = sub.add_parser("vault-stage", help="暂存新增或更新条目并输出完整新旧值；不立即修改保险柜")
    p.add_argument("--answers", required=True, help='0700 目录内的 0600 临时 JSON：{"entries": {"phone": {"type": "phone_cn", "value": "..."}}}')
    p.add_argument("--confirmation-out", required=True, help="写入 0600 加密确认文件；路径必须位于 0700 目录")
    _add_storage_args(p)
    p.set_defaults(func=cmd_vault_stage)
    p = sub.add_parser("vault-apply", help="应用员工已确认的保险柜变更")
    p.add_argument("--confirmation", required=True, help="vault-stage 生成的确认文件")
    _add_storage_args(p)
    p.set_defaults(func=cmd_vault_apply)
    p = sub.add_parser("vault-migrate", help="用 0600 临时密码文件把 v1 保险柜无损迁移到 v2")
    p.add_argument("--password-file", required=True, help="旧密码临时文件；无论成败都会删除")
    p.add_argument("--source-vault", help="旧 v1 保险柜；默认读取旧 Skill data 目录")
    p.add_argument("--target-vault", help="新 v2 保险柜；默认系统用户数据目录")
    p.add_argument("--target-key", help="新 v2 密钥；默认系统用户密钥目录")
    p.set_defaults(func=cmd_vault_migrate)
    p = sub.add_parser("vault-scan", help="OpenVINO 本地识别本人明确指定的证件图，输出候选供确认后写入保险柜")
    p.add_argument("images", nargs="+", metavar="IMAGE")
    p.add_argument("--vlm", action="store_true", help="使用独立 VLM 环境；不可用时由 Agent 改用核心环境重试普通 OCR")
    p.add_argument("--model", help="使用 --vlm 时指定兼容的 Hugging Face 模型")
    p.add_argument("--revision", help="可选模型 revision；不指定时使用模型仓库默认版本")
    p.set_defaults(func=cmd_vault_scan)
    p = sub.add_parser("vault-preview", help="展示本次将提交的完整值并生成 30 分钟有效的加密确认文件")
    p.add_argument("form", metavar="REQUEST.yintian-request")
    p.add_argument("--mapping", help="请求字段 id → 保险柜条目 id 的显式映射 JSON")
    p.add_argument("--confirmation-out", required=True, help="写入 0600 加密确认文件；路径必须位于 0700 目录")
    p.add_argument("--previous", help="更正时指定本人上一次开放请求回执")
    p.add_argument("--credential", help="旧 group 模式的个人凭据；open 模式不需要")
    _add_storage_args(p)
    p.set_defaults(func=cmd_vault_preview)
    p = sub.add_parser("vault-fill", help="用保险柜匹配请求包字段，本人确认后生成 姓名-短码.yintian 回执")
    p.add_argument("form", metavar="REQUEST.yintian-request")
    p.add_argument("--confirmation", required=True, help="vault-preview 生成的确认文件")
    output = p.add_mutually_exclusive_group(required=True)
    output.add_argument("--out", help="显式指定输出 .yintian 路径")
    output.add_argument("--out-dir", help="在目录中自动生成 姓名-短码.yintian")
    p.add_argument("--previous", help="更正时指定本人上一次开放请求回执，以替换同一条记录")
    p.add_argument("--credential", help="旧 group 模式的个人凭据；open 模式不需要")
    _add_storage_args(p)
    p.set_defaults(func=cmd_vault_fill)
    p = sub.add_parser("vlm-setup", help="下载用户指定的兼容模型（安装时联网，推理离线）")
    p.add_argument("--model", required=True, help="Hugging Face 或 ModelScope 模型 ID")
    p.add_argument("--revision", help="可选模型 revision；不指定时使用模型仓库默认版本")
    p.add_argument("--source", choices=["huggingface", "modelscope"], default="huggingface",
                   help="模型下载来源；modelscope 为可选备选，需先 pip install modelscope")
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
