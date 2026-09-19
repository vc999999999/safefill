"""SafeFill 协议层：常量、校验、加密与信封处理，两端逐字节共享。

收集端的任务管理、数据库、Excel 导出与 CLI 在 collect 侧 collector.py；
填写端 fill.py/vault.py 只依赖本模块的协议函数。
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import secrets
import string
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import secure_io

SUBMISSION_FORMAT_VERSION = "yintian-submission/4"
REQUEST_FORMAT_VERSION = "yintian-request/1"
NOTICE_FORMAT_VERSION = "yintian-notice/1"
KEY_ENVELOPE_VERSION = "yintian-key/1"
OPEN_INVITE_PREFIX = "OPEN-"
SCRYPT_N = 2**16
SCRYPT_R = 8
SCRYPT_P = 1
ALLOWED_TYPES = {"text", "phone_cn", "cn_id", "date", "address", "single_choice", "image_attachment", "pdf_attachment"}
ATTACHMENT_TYPES = {"image_attachment", "pdf_attachment"}
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 15 * 1024 * 1024
MAX_PDF_PAGES = 20
PDF_RENDER_SCALE = 2
MAX_ENVELOPE_BYTES = 32 * 1024 * 1024
MAX_PACKAGE_ENVELOPE_BYTES = 200 * 1024 * 1024
MAX_PACKAGE_FILES = 20_000
MAX_PACKAGE_BYTES = 2 * 1024 * 1024 * 1024
MAX_PACKAGE_MEMBER_BYTES = 128 * 1024 * 1024
MAX_IMAGE_PIXELS = int(os.getenv("YINTIAN_MAX_IMAGE_PIXELS", "40000000"))
MAX_FIELDS = 100
MAX_LABEL_CHARS = 200
MAX_VALUE_CHARS = 10_000
MAX_NAME_CHARS = 200
MAX_OPTIONS = 100
MAX_ATTACHMENTS = 60


def positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


TASK_ID_RE = re.compile(r"^YT-[0-9]{8}-[A-Z0-9]{6}$")
OPEN_INVITE_ID_RE = re.compile(r"^OPEN-[A-Z0-9]{16}$")
FIELD_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
ILLEGAL_XML_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")
NOTICE_KEYS = ("title", "purpose", "deadline", "retention_until", "contact", "correction")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def terminal_text(value):
    return ''.join(char if unicodedata.category(char) not in {'Cc', 'Cf'} else ' ' for char in str(value))


def safe_filename_component(value: Any, fallback: str = "item", max_chars: int = 60, max_bytes: int = 120) -> str:
    text = unicodedata.normalize("NFKC", terminal_text(value))
    text = re.sub(r'[<>:"/\\|?*]+', "_", text).strip(" ._")[:max_chars].strip(" ._")
    while len(text.encode("utf-8")) > max_bytes:
        text = text[:-1]
    return text or fallback


def parse_time(value: str) -> datetime:
    value = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        value += "T23:59:59+00:00"
    elif value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def explicit_timezone(value: Any) -> bool:
    """True only for ISO 时间且带 Z 或 ±HH:MM 偏移；纯日期或裸时间会被不同时区的两端理解成不同时刻。"""
    if not isinstance(value, str):
        return False
    text = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) or "T" not in text:
        return False
    try:
        return datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text).tzinfo is not None
    except ValueError:
        return False


def normalize_submitted_at(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = parse_time(value)
    except (TypeError, ValueError):
        return None
    return parsed.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def valid_date(value: str) -> bool:
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d") == value
    except (TypeError, ValueError):
        return False


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(secure_io.read_bytes(path, MAX_PACKAGE_MEMBER_BYTES))


def atomic_write(path: Path, data: bytes) -> None:
    secure_io.atomic_write(path, data)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(secure_io.read_bytes(path, MAX_ENVELOPE_BYTES))


def dump_json(path: Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")


def random_id(prefix: str, length: int) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return prefix + "".join(secrets.choice(alphabet) for _ in range(length))


def task_expired(task: dict[str, Any]) -> bool:
    return datetime.now(timezone.utc) > parse_time(task["retention_until"])


def expired_message(task: dict[str, Any], action: str) -> str:
    return (f"TASK_EXPIRED: 任务已于 {task['retention_until']} 超过告知员工的保存期限，按承诺不再{action}；"
            "如仍需收集，请新建请求包并重新发给员工，旧任务目录可用 purge 清理")


def task_late(task: dict[str, Any], received_at: str) -> bool:
    return parse_time(received_at) > parse_time(task["deadline"])


def validate_field_definitions(raw_fields: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_fields, list) or not (1 <= len(raw_fields) <= MAX_FIELDS):
        raise ValueError(f"fields 必须包含 1 到 {MAX_FIELDS} 个字段")
    seen, labels, fields = set(), set(), []
    for raw in raw_fields:
        if not isinstance(raw, dict):
            raise ValueError("每个字段定义必须是对象")
        field = dict(raw)
        field_id, field_type = field.get("id", ""), field.get("type", "")
        if not isinstance(field_id, str) or not FIELD_ID_RE.fullmatch(field_id) or field_id in seen:
            raise ValueError(f"字段 id 无效或重复: {field_id}")
        if field_type not in ALLOWED_TYPES:
            raise ValueError(f"不支持的字段类型: {field_type}")
        if not isinstance(field.get("label"), str) or not field["label"] or len(field["label"]) > MAX_LABEL_CHARS or ILLEGAL_XML_RE.search(field["label"]):
            raise ValueError(f"字段缺少 label: {field_id}")
        if field["label"] in labels:
            raise ValueError(f"字段 label 重复: {field['label']}")
        if any(key in field and not isinstance(field[key], bool) for key in ("required", "sensitive")):
            raise ValueError(f"字段 required/sensitive 必须是布尔值: {field_id}")
        if field_type == "single_choice":
            options = field.get("options")
            if not isinstance(options, list) or not (1 <= len(options) <= MAX_OPTIONS) or any(not isinstance(value, str) or not value or len(value) > MAX_LABEL_CHARS or ILLEGAL_XML_RE.search(value) for value in options) or len(set(options)) != len(options):
                raise ValueError(f"单选字段 options 无效: {field_id}")
        elif "options" in field:
            raise ValueError(f"非单选字段不能包含 options: {field_id}")
        if "multiple" in field and (field_type not in ATTACHMENT_TYPES or not isinstance(field["multiple"], bool)):
            raise ValueError(f"multiple 只允许附件字段使用布尔值: {field_id}")
        field["required"] = bool(field.get("required"))
        field["sensitive"] = bool(field.get("sensitive", field_type in {"phone_cn", "cn_id", "address"} or field_type in ATTACHMENT_TYPES))
        if field_type in ATTACHMENT_TYPES:
            field["multiple"] = bool(field.get("multiple", False))
        fields.append(field)
        seen.add(field_id)
        labels.add(field["label"])
    definitions = {field["id"]: field for field in fields}
    if definitions.get("name", {}).get("type") != "text" or definitions["name"].get("required") is not True:
        raise ValueError("任务字段必须包含必填 text 字段 id=name")
    scalar_fields = {field["id"] for field in fields if field["type"] not in ATTACHMENT_TYPES}
    for field in fields:
        if "ocr_fields" in field:
            bindings = field["ocr_fields"]
            if field["type"] not in ATTACHMENT_TYPES or not isinstance(bindings, list) or any(not isinstance(key, str) or key not in scalar_fields for key in bindings) or len(set(bindings)) != len(bindings):
                raise ValueError("OCR_BINDING_INVALID: ocr_fields 必须引用不重复的表单值字段")
    return fields


def derive_scrypt_key(password: str, salt: bytes, n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(password.encode("utf-8"))


def validate_kdf_params(kdf: Any) -> tuple[bytes, int, int, int]:
    if not isinstance(kdf, dict) or kdf.get("name") != "scrypt":
        raise ValueError("KDF 参数不受支持")
    try:
        salt = base64.b64decode(kdf.get("salt", ""), validate=True)
        n, r, p = int(kdf["n"]), int(kdf["r"]), int(kdf["p"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("KDF 参数无效") from exc
    if len(salt) != 16 or not (2**10 <= n <= 2**17 and n & (n - 1) == 0) or not (1 <= r <= 8 and 1 <= p <= 2):
        raise ValueError("KDF 参数超出允许范围")
    return salt, n, r, p


def aes_gcm_seal(format_name: str, plaintext: bytes, password: str, aad: bytes | None = None) -> dict[str, Any]:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    key = derive_scrypt_key(password, salt)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad if aad is not None else format_name.encode("utf-8"))
    return {
        "format": format_name,
        "kdf": {"name": "scrypt", "salt": base64.b64encode(salt).decode("ascii"), "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P},
        "cipher": "AES-256-GCM",
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }


def aes_gcm_open(envelope: dict[str, Any], password: str, aad: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not isinstance(envelope, dict) or envelope.get("cipher") != "AES-256-GCM":
        raise ValueError("加密信封格式不受支持")
    salt, n, r, p = validate_kdf_params(envelope.get("kdf"))
    try:
        nonce = base64.b64decode(envelope["nonce"], validate=True)
        ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("加密信封缺少 nonce 或密文") from exc
    if len(nonce) != 12:
        raise ValueError("加密信封 nonce 无效")
    key = derive_scrypt_key(password, salt, n, r, p)
    return AESGCM(key).decrypt(nonce, ciphertext, aad)


def encrypt_private_key(private_der: bytes, password: str) -> bytes:
    envelope = aes_gcm_seal(KEY_ENVELOPE_VERSION, private_der, password)
    return json.dumps(envelope, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"


def decrypt_private_key(envelope: dict[str, Any], password: str) -> bytes:
    if envelope.get("format") != KEY_ENVELOPE_VERSION:
        raise ValueError("私钥信封格式不受支持")
    return aes_gcm_open(envelope, password, KEY_ENVELOPE_VERSION.encode("utf-8"))


def valid_private_key_blob(data: bytes) -> bool:
    blob = data.lstrip()
    if not blob.startswith(b"{"):
        return False
    try:
        envelope = json.loads(blob)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    return isinstance(envelope, dict) and envelope.get("format") == KEY_ENVELOPE_VERSION


def generate_keys(password: str) -> tuple[bytes, bytes, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    public = private.public_key()
    public_pem = public.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    public_der = public.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    private_der = private.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return public_pem, encrypt_private_key(private_der, password), hashlib.sha256(public_der).hexdigest()[:24]


def validate_envelope_header(envelope: dict[str, Any], task: dict[str, Any]) -> str:
    required = {"format_version", "task_id", "invite_id", "schema_hash", "key_id", "algorithms", "encrypted_key_b64", "iv_b64", "ciphertext_b64"}
    if not isinstance(envelope, dict) or set(envelope) != required:
        raise ValueError("提交包字段集合无效")
    for key in required - {"algorithms"}:
        if not isinstance(envelope.get(key), str) or not envelope[key]:
            raise ValueError(f"提交包缺少字段: {key}")
    if envelope["format_version"] != task["format_version"] or envelope["task_id"] != task["task_id"]:
        raise ValueError("提交包属于其他任务或格式版本")
    if envelope.get("algorithms") != {"content": "AES-256-GCM", "key_wrap": "RSA-OAEP-3072-SHA256"}:
        raise ValueError("提交包算法套件不受支持")
    if envelope["schema_hash"] != task["schema_hash"] or envelope["key_id"] != task["key_id"]:
        raise ValueError("提交包字段模板或公钥不匹配")
    invite_id = envelope["invite_id"]
    if not OPEN_INVITE_ID_RE.fullmatch(invite_id):
        raise ValueError("回执编号格式无效")
    for key in ("encrypted_key_b64", "iv_b64", "ciphertext_b64"):
        base64.b64decode(envelope[key], validate=True)
    return invite_id


def aad_for(envelope: dict[str, Any]) -> bytes:
    return canonical([envelope["format_version"], envelope["task_id"], envelope["invite_id"], envelope["schema_hash"], envelope["key_id"]])


def normalize_value(field_type: str, value: str) -> str:
    value = str(value or "").strip()
    if field_type == "phone_cn":
        value = re.sub(r"[\s-]", "", value)
        value = re.sub(r"^\+?86", "", value)
    if field_type == "cn_id":
        value = value.upper()
    return value


def validate_scalar_values(values: Any, allowed_ids: set[str]) -> None:
    if not isinstance(values, dict) or len(values) > MAX_FIELDS:
        raise ValueError("values 必须是字段数量受限的对象")
    if set(values) - allowed_ids:
        raise ValueError("PAYLOAD_FIELDS_INVALID: 提交含模板外字段")
    for field_id, value in values.items():
        limit = MAX_NAME_CHARS if field_id == "name" else MAX_VALUE_CHARS
        if not isinstance(value, str) or len(value) > limit or ILLEGAL_XML_RE.search(value):
            raise ValueError(f"字段必须是长度不超过 {limit} 的文本: {field_id}")


def validate_payload(task: dict[str, Any], payload: dict[str, Any]) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    from ocr_matcher import validate_chinese_id

    required = {"notice_hash", "template_version", "submitted_at", "consent_confirmed", "values", "attachments"}
    if not isinstance(payload, dict) or not required.issubset(payload):
        raise ValueError("解密载荷字段集合无效")
    missing, conflicts, attachment_items = [], [], []
    if payload.get("notice_hash") != task["notice_hash"]:
        conflicts.append("notice_hash")
    if payload.get("template_version") != task["template_version"]:
        conflicts.append("template_version")
    if payload.get("consent_confirmed") is not True:
        conflicts.append("consent")
    if normalize_submitted_at(payload.get("submitted_at")) is None:
        conflicts.append("submitted_at")
    values, attachments = payload.get("values", {}), payload.get("attachments", {})
    if not isinstance(attachments, dict) or len(attachments) > MAX_FIELDS:
        raise ValueError("values/attachments 格式无效")
    value_ids = {f['id'] for f in task['fields'] if f['type'] not in ATTACHMENT_TYPES}
    attachment_ids = {f['id'] for f in task['fields'] if f['type'] in ATTACHMENT_TYPES}
    validate_scalar_values(values, value_ids)
    if set(attachments) - attachment_ids:
        raise ValueError('PAYLOAD_FIELDS_INVALID: 提交含模板外字段')
    total_size = 0
    total_attachments = 0
    for field in task["fields"]:
        field_id, field_type = field["id"], field["type"]
        if field_type in ATTACHMENT_TYPES:
            items = attachments.get(field_id, [])
            if not isinstance(items, list) or len(items) > 20:
                raise ValueError(f"附件字段格式无效: {field_id}")
            total_attachments += len(items)
            if total_attachments > MAX_ATTACHMENTS:
                raise ValueError(f"附件总数超过 {MAX_ATTACHMENTS} 个")
            if field.get("required") and not items:
                missing.append(field_id)
            if not field.get("multiple") and len(items) > 1:
                conflicts.append(field_id)
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError(f"附件项目格式无效: {field_id}")
                name = item.get("name", "")
                if not isinstance(name, str) or len(name) > 255:
                    raise ValueError(f"附件文件名无效: {field_id}")
                raw = base64.b64decode(item.get("data_b64", ""), validate=True)
                mime = item.get("type", "")
                if len(raw) != int(item.get("size", -1)) or len(raw) > MAX_FILE_BYTES or sha256_bytes(raw) != item.get("sha256"):
                    raise ValueError(f"附件大小或哈希无效: {field_id}")
                if field_type == "image_attachment" and mime not in {"image/jpeg", "image/png", "image/webp"}:
                    raise ValueError(f"图片类型不支持: {mime}")
                if field_type == "pdf_attachment" and mime != "application/pdf":
                    raise ValueError("PDF 附件类型无效")
                if field_type == "image_attachment":
                    from PIL import Image

                    try:
                        with Image.open(io.BytesIO(raw)) as image:
                            expected = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}[mime]
                            if image.format != expected or image.width * image.height > MAX_IMAGE_PIXELS:
                                raise ValueError(f"图片内容或像素尺寸无效: {field_id}")
                            image.verify()
                    except (OSError, Image.DecompressionBombError):
                        raise ValueError('ATTACHMENT_INVALID: 图片数据无效') from None
                elif not raw.startswith(b"%PDF-"):
                    raise ValueError("PDF 文件头无效")
                total_size += len(raw)
                attachment_items.append({"field_id": field_id, "name": name, "type": mime, "data": raw})
        else:
            value = normalize_value(field_type, values.get(field_id, ""))
            if field.get("required") and not value:
                missing.append(field_id)
            if value and field_type == "phone_cn" and not re.fullmatch(r"1[3-9]\d{9}", value):
                conflicts.append(field_id)
            if value and field_type == "cn_id" and not validate_chinese_id(value):
                conflicts.append(field_id)
            if value and field_type == "date" and not valid_date(value):
                conflicts.append(field_id)
            if value and field_type == "single_choice" and value not in field.get("options", []):
                conflicts.append(field_id)
    if total_size > MAX_TOTAL_BYTES:
        raise ValueError("附件总大小超过 15MB")
    return sorted(set(missing)), sorted(set(conflicts)), attachment_items


def error_report(exc: BaseException) -> dict[str, Any]:
    """错误与成功输出同为 JSON：code 取消息中的大写错误码，便于 Agent 直接分支处理。"""
    message = terminal_text(exc)
    match = re.match(r"([A-Z][A-Z0-9_]+):\s*(.*)", message, re.S)
    code = match.group(1) if match else type(exc).__name__
    detail = match.group(2).strip() if match else message
    return {"ok": False, "error": code, "message": detail}
