"""SafeFill 协议层：常量、校验、加密与信封处理，两端逐字节共享。

收集端的任务管理、数据库、Excel 导出与 CLI 在 collect 侧 collector.py；
填写端 fill.py/vault.py 只依赖本模块的协议函数。
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import re
import secrets
import string
import sys
import threading
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import secure_io

SUBMISSION_FORMAT_VERSION = "yintian-submission/5"
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
OPEN_INVITE_ID_RE = re.compile(r"^OPEN-[0-9A-F]{32}$")
MAX_REVISION = 2**31 - 1
SIGNATURE_DOMAIN = b"SafeFill submission v5\0"
FIELD_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
ILLEGAL_XML_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")
NOTICE_KEYS = ("title", "purpose", "deadline", "retention_until", "contact", "correction")
_PRIVATE_OUTPUT_LOCK = threading.RLock()


def _flush_native_output():
    """Flush C stdio while its descriptors still point to the intended destination."""
    import ctypes

    flushed = False
    for library in ("ucrtbase", "msvcrt") if os.name == "nt" else (None,):
        try:
            flush = ctypes.CDLL(library).fflush
        except (OSError, AttributeError):
            continue
        flush.argtypes = [ctypes.c_void_p]
        flush.restype = ctypes.c_int
        flush(None)
        flushed = True
    if not flushed:
        raise RuntimeError("PRIVATE_OUTPUT_UNAVAILABLE: 无法控制本地运行库输出")


@contextmanager
def suppress_private_output():
    """Suppress SDK Python/native output without writing sensitive text to a temporary file."""
    # ponytail: output redirection is process-wide; use worker processes if parallel inference is needed.
    with _PRIVATE_OUTPUT_LOCK, open(os.devnull, "w") as sink:
        saved = []
        prior_logging = logging.root.manager.disable
        try:
            sys.stdout.flush()
            sys.stderr.flush()
            _flush_native_output()
            for fd in (1, 2):
                saved.append((fd, os.dup(fd)))
                os.dup2(sink.fileno(), fd)
            logging.disable(logging.CRITICAL)
            with redirect_stdout(sink), redirect_stderr(sink):
                yield
        finally:
            try:
                _flush_native_output()
            finally:
                for fd, original in reversed(saved):
                    os.dup2(original, fd)
                    os.close(original)
                logging.disable(prior_logging)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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
        raise ValueError(f"SCHEMA_INVALID: fields 必须包含 1 到 {MAX_FIELDS} 个字段")
    seen, labels, fields = set(), set(), []
    for raw in raw_fields:
        if not isinstance(raw, dict):
            raise ValueError("SCHEMA_INVALID: 每个字段定义必须是对象")
        field = dict(raw)
        field_id, field_type = field.get("id", ""), field.get("type", "")
        if not isinstance(field_id, str) or not FIELD_ID_RE.fullmatch(field_id) or field_id in seen:
            raise ValueError(f"SCHEMA_INVALID: 字段 id 无效或重复: {field_id}")
        if not isinstance(field_type, str) or field_type not in ALLOWED_TYPES:
            raise ValueError(f"SCHEMA_INVALID: 不支持的字段类型: {field_type}")
        if not isinstance(field.get("label"), str) or not field["label"] or len(field["label"]) > MAX_LABEL_CHARS or ILLEGAL_XML_RE.search(field["label"]):
            raise ValueError(f"SCHEMA_INVALID: 字段缺少 label: {field_id}")
        if field["label"] in labels:
            raise ValueError(f"SCHEMA_INVALID: 字段 label 重复: {field['label']}")
        if any(key in field and not isinstance(field[key], bool) for key in ("required", "sensitive")):
            raise ValueError(f"SCHEMA_INVALID: 字段 required/sensitive 必须是布尔值: {field_id}")
        if field_type == "single_choice":
            options = field.get("options")
            if not isinstance(options, list) or not (1 <= len(options) <= MAX_OPTIONS) or any(not isinstance(value, str) or not value or len(value) > MAX_LABEL_CHARS or ILLEGAL_XML_RE.search(value) for value in options) or len(set(options)) != len(options):
                raise ValueError(f"SCHEMA_INVALID: 单选字段 options 无效: {field_id}")
        elif "options" in field:
            raise ValueError(f"SCHEMA_INVALID: 非单选字段不能包含 options: {field_id}")
        if "multiple" in field and (field_type not in ATTACHMENT_TYPES or not isinstance(field["multiple"], bool)):
            raise ValueError(f"SCHEMA_INVALID: multiple 只允许附件字段使用布尔值: {field_id}")
        field["required"] = bool(field.get("required"))
        field["sensitive"] = bool(field.get("sensitive", field_type in {"phone_cn", "cn_id", "address"} or field_type in ATTACHMENT_TYPES))
        if field_type in ATTACHMENT_TYPES:
            field["multiple"] = bool(field.get("multiple", False))
        fields.append(field)
        seen.add(field_id)
        labels.add(field["label"])
    definitions = {field["id"]: field for field in fields}
    if definitions.get("name", {}).get("type") != "text" or definitions["name"].get("required") is not True:
        raise ValueError("SCHEMA_INVALID: 任务字段必须包含必填 text 字段 id=name")
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


def derive_invite_id(task_id: str, pubkey_b64: str) -> str:
    if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
        raise ValueError("SIGNATURE_INVALID: 签名身份的任务编号无效")
    try:
        public_bytes = base64.b64decode(pubkey_b64, validate=True)
    except (ValueError, TypeError):
        raise ValueError("SIGNATURE_INVALID: 签名公钥无效") from None
    if len(public_bytes) != 32 or base64.b64encode(public_bytes).decode("ascii") != pubkey_b64:
        raise ValueError("SIGNATURE_INVALID: 签名公钥无效")
    digest = hashlib.sha256(b"SafeFill identity v1\0" + task_id.encode("utf-8") + b"\0" + public_bytes)
    return OPEN_INVITE_PREFIX + digest.digest()[:16].hex().upper()


def sign_envelope(envelope: dict[str, Any], private_key) -> dict[str, Any]:
    from cryptography.hazmat.primitives import serialization

    public = private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    if envelope.get("sender_public_key_b64") != base64.b64encode(public).decode("ascii"):
        raise ValueError("SIGNATURE_INVALID: 签名私钥与信封身份不匹配")
    unsigned = {key: value for key, value in envelope.items() if key != "signature_b64"}
    signature = private_key.sign(SIGNATURE_DOMAIN + canonical(unsigned))
    return {**unsigned, "signature_b64": base64.b64encode(signature).decode("ascii")}


def validate_envelope_header(envelope: dict[str, Any], task: dict[str, Any]) -> str:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    required = {"format_version", "task_id", "invite_id", "schema_hash", "key_id", "algorithms", "encrypted_key_b64", "iv_b64", "ciphertext_b64", "sender_public_key_b64", "revision", "signature_b64"}
    if not isinstance(envelope, dict) or set(envelope) != required:
        raise ValueError("提交包字段集合无效")
    for key in required - {"algorithms", "revision"}:
        if not isinstance(envelope.get(key), str) or not envelope[key]:
            raise ValueError(f"提交包缺少字段: {key}")
    if envelope["format_version"] != SUBMISSION_FORMAT_VERSION or envelope["format_version"] != task["format_version"] or envelope["task_id"] != task["task_id"]:
        raise ValueError("提交包属于其他任务或格式版本")
    if envelope.get("algorithms") != {"content": "AES-256-GCM", "key_wrap": "RSA-OAEP-3072-SHA256"}:
        raise ValueError("提交包算法套件不受支持")
    if envelope["schema_hash"] != task["schema_hash"] or envelope["key_id"] != task["key_id"]:
        raise ValueError("提交包字段模板或公钥不匹配")
    invite_id = envelope["invite_id"]
    if not OPEN_INVITE_ID_RE.fullmatch(invite_id):
        raise ValueError("回执编号格式无效")
    if type(envelope["revision"]) is not int or not 1 <= envelope["revision"] <= MAX_REVISION:
        raise ValueError("REVISION_INVALID: 更正序号必须为有效正整数")
    for key in ("encrypted_key_b64", "iv_b64", "ciphertext_b64"):
        value = base64.b64decode(envelope[key], validate=True)
        if base64.b64encode(value).decode("ascii") != envelope[key]:
            raise ValueError("提交包编码不规范")
        if (key == "encrypted_key_b64" and len(value) != 384) or (key == "iv_b64" and len(value) != 12) or (key == "ciphertext_b64" and not 16 <= len(value) <= MAX_ENVELOPE_BYTES):
            raise ValueError("提交包密文长度无效")
    if derive_invite_id(envelope["task_id"], envelope["sender_public_key_b64"]) != invite_id:
        raise ValueError("SIGNATURE_INVALID: 回执编号与签名身份不匹配")
    try:
        signature = base64.b64decode(envelope["signature_b64"], validate=True)
        if len(signature) != 64 or base64.b64encode(signature).decode("ascii") != envelope["signature_b64"]:
            raise ValueError("signature")
        public = Ed25519PublicKey.from_public_bytes(base64.b64decode(envelope["sender_public_key_b64"], validate=True))
        unsigned = {key: value for key, value in envelope.items() if key != "signature_b64"}
        public.verify(signature, SIGNATURE_DOMAIN + canonical(unsigned))
    except (ValueError, TypeError, InvalidSignature):
        raise ValueError("SIGNATURE_INVALID: 回执签名无效") from None
    return invite_id


def aad_for(envelope: dict[str, Any]) -> bytes:
    return canonical([envelope["format_version"], envelope["task_id"], envelope["invite_id"], envelope["schema_hash"], envelope["key_id"], envelope["revision"], envelope["sender_public_key_b64"]])


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
    """只输出协议错误码和静态说明，异常原文可能含明文值、路径或第三方输出。"""
    allowed = {
        "ANSWERS_INVALID", "ANSWERS_PERMISSIONS", "ATTACHMENTS_INVALID", "ATTACHMENT_EXPORT_UNAVAILABLE",
        "ATTACHMENT_INVALID", "ATTACHMENT_LIMIT", "CANCELLED", "CIPHERTEXT_CHANGED", "CONFIRMATION_CONSUME_FAILED",
        "CONFIRMATION_EXISTS", "CONFIRMATION_EXPIRED", "CONFIRMATION_INVALID", "CONFIRMATION_LIMIT", "CONFIRMATION_STALE",
        "CONFIG_INVALID", "CONFIG_MISSING_FIELDS", "DATABASE_MISSING", "DATABASE_WRITE_FAILED", "DEADLINE_INVALID", "EVIDENCE_LIMIT", "EVIDENCE_MISSING", "EXPORT_VALIDATION_FAILED", "FILE_LIMIT", "FORM_INVALID",
        "GUI_UNAVAILABLE", "IDENTITY_INVALID", "IDENTITY_MISSING", "IMAGE_MISSING", "INBOX_LIMIT", "INGEST_IO", "KEY_MISMATCH", "KEY_UNLOCK_FAILED",
        "LEGACY_TASK_UNSUPPORTED", "LOCAL_KEY_INVALID", "LOCAL_KEY_UNAVAILABLE", "LOCAL_OCR_UNAVAILABLE",
        "MANUAL_NOT_ALLOWED", "MANUAL_REVIEW_UNAVAILABLE", "MAPPING_INVALID", "NOTICE_INVALID", "OCR_BINDING_INVALID",
        "OPEN_INVITE_LIMIT", "OPEN_KEY_PACKAGE_INVALID", "OPEN_REQUEST_INVALID", "OPERATOR_INVALID", "OUTPUT_EXISTS",
        "PATH_OUTSIDE", "PATH_UNSAFE", "PAYLOAD_FIELDS_INVALID", "PAYLOAD_INVALID", "PREVIOUS_INVALID", "RECEIPT_INVALID",
        "PRIVATE_OUTPUT_UNAVAILABLE", "RECOVERY_CONFLICT", "RETENTION_INVALID", "SCHEMA_INVALID", "REPLY_ROLLBACK_FAILED", "REVISION_CONFLICT", "REVISION_INVALID", "REVISION_LIMIT", "SCHEMA_UNSUPPORTED",
        "SECRET_TTY_REQUIRED", "SIGNATURE_INVALID", "STATE_CHANGED", "TASK_BUSY", "TASK_EXPIRED", "TASK_INVALID", "TASK_STORAGE_LIMIT",
        "TIME_ZONE_REQUIRED", "TEXT_INPUT_INVALID", "TEXT_EXTRACTION_INVALID", "TEXT_FIELDS_EMPTY", "TEXT_VALUES_INVALID",
        "VALUES_INVALID", "VAULT_ANSWERS_INVALID", "VAULT_ENTRY_INVALID", "VAULT_FIELDS_MISSING",
        "VAULT_INVALID", "VAULT_KEY_INVALID", "VAULT_KEY_MISSING", "VAULT_LIMIT", "VAULT_LOCATION_UNAVAILABLE",
        "VAULT_MISSING", "VAULT_PATH_COLLISION", "VAULT_PATH_INVALID", "VAULT_PERMISSIONS", "VAULT_ROLLBACK_FAILED",
        "VAULT_STORAGE_INVALID", "VAULT_UNLOCK_FAILED", "VAULT_WRITE_FAILED", "VLM_MODEL_REQUIRED", "VLM_UNAVAILABLE",
    }
    match = re.match(r"([A-Z][A-Z0-9_]{1,63}):", str(exc))
    code = match.group(1) if match and match.group(1) in allowed else type(exc).__name__
    messages = {
        "CONFIG_INVALID": "任务配置必须是 JSON 对象，告知内容和模板版本须为非空文本；请检查 collection-config 参考。",
        "CONFIG_MISSING_FIELDS": "任务配置缺少必填项；请检查 title、purpose、deadline、retention_until、contact、correction、template_version 和 fields。",
        "DEADLINE_INVALID": "deadline 必须晚于当前时间；请使用带时区的未来时间。",
        "RETENTION_INVALID": "retention_until 必须晚于 deadline；请调整保存期限。",
        "TIME_ZONE_REQUIRED": "deadline 和 retention_until 须为有效 ISO 日期时间，包含 T 和时区偏移或 Z。",
        "SCHEMA_INVALID": "字段定义无效；请检查唯一 id/label、受支持的类型、选项和布尔属性，并包含必填 text 字段 id=name。",
        "OCR_BINDING_INVALID": "ocr_fields 只允许用于附件，且须引用不重复的已有值字段。",
        "PATH_UNSAFE": "请使用不含 ..、符号链接或重解析点的真实路径；保险柜可用 YINTIAN_VAULT_DIR / YINTIAN_VAULT_KEY_DIR 指定。",
        "DATABASE_WRITE_FAILED": "任务数据库写入失败，已停止导出；请保留原始回执，修复数据库或磁盘后重新收件，勿改用空目录导出旧记录。",
        "PRIVATE_OUTPUT_UNAVAILABLE": "无法控制本地运行库输出，已停止推理；请检查本机运行环境。",
        "SIGNATURE_INVALID": "回执签名或归属无效，请由原保险柜重新生成。",
        "PREVIOUS_INVALID": "旧回执无法验证或不属于当前保险柜，请使用本人原始回执。",
        "REVISION_INVALID": "更正序号无效，请重新预览并生成回执。",
        "REVISION_LIMIT": "本身份的更正序号已达上限，请新建任务。",
        "REVISION_CONFLICT": "同一更正序号存在不同回执，请持有人生成更高序号的补正回执。",
        "LEGACY_TASK_UNSUPPORTED": "旧任务须由原版本完成；本版本只接受新建任务，不迁移旧任务。",
        "SCHEMA_UNSUPPORTED": "任务数据库版本不受支持，请使用创建该任务的版本。",
        "OUTPUT_EXISTS": "输出已存在，请选择新的输出位置。",
        "CONFIRMATION_STALE": "预览后内容或提交状态已变化，请重新预览确认。",
        "CONFIRMATION_EXPIRED": "确认已过期，请重新预览确认。",
        "TASK_EXPIRED": "任务已超过保存期限，请新建请求包。",
        "TASK_INVALID": "任务元数据无效或与摘要不一致；请恢复完整任务备份或重新导入可信交接包，勿手工改写摘要。",
        "TASK_BUSY": "任务正在被其他操作占用，请稍后重试。",
        "TASK_STORAGE_LIMIT": "已验证回执无法保存，汇总已停止；请检查任务容量上限后重试。",
        "INGEST_IO": "已验证回执写入失败；请检查磁盘和权限，将原始回执放回收件目录后重试。",
        "STATE_CHANGED": "当前回执或任务状态已变化，请刷新状态后重试。",
        "VAULT_KEY_MISSING": "保险柜密钥缺失，请恢复本人备份。",
        "VAULT_UNLOCK_FAILED": "保险柜无法解锁，请核对本人密钥与备份。",
        "VAULT_WRITE_FAILED": "保险柜状态保存失败，未完成提交；请检查存储后重新预览确认。",
        "VAULT_ROLLBACK_FAILED": "保险柜回滚失败，提交状态不确定；请停止重试并保留保险柜、密钥和已经生成的回执。",
        "REPLY_ROLLBACK_FAILED": "回执无法撤回，操作未完整完成；请停止重试并保留现场，核对现有回执后再处理。",
        "TEXT_INPUT_INVALID": "文本入口须提供 --request、--model 和可读取的 UTF-8 .txt，最大 16 KiB；不与 --answers 混用。",
        "TEXT_EXTRACTION_INVALID": "本地模型输出不符合字段约束或引用了原文之外的值；请在原文本中明确标注字段后重试。",
        "TEXT_FIELDS_EMPTY": "本地文本未提取到可用字段；请补充请求中的字段标签和对应值。",
        "TEXT_VALUES_INVALID": "提取值未通过请求字段校验；请在原文本中检查手机号、证件号、日期和选项后重试。",
        "VLM_UNAVAILABLE": "本地模型不可用；请检查模型环境、已安装模型和设备。文本入口需要 LLMPipeline 兼容模型，不自动转云端或读取原文到会话。",
        "CANCELLED": "操作已取消。",
    }
    if code == "SECRET_TTY_REQUIRED":
        return {"ok": False, "error": code, "message": "无法打开安全控制终端；请在本机交互终端执行，密码不会写入日志。"}
    return {"ok": False, "error": code, "message": messages.get(code, "操作未完成，请根据错误码检查输入或运行 doctor；详细内容未输出以保护隐私。")}
