"""SafeFill employee vault: encrypted local profile, migration and confirmations."""
from __future__ import annotations

import base64
import copy
import json
import os
import secrets
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

import collection
import secure_io

FORMAT_V2 = "yintian-vault/2"
FORMAT_V1 = "yintian-vault/1"
CONFIRMATION_FORMAT = "yintian-confirmation/1"
CONFIRMATION_TTL_SECONDS = 30 * 60
MAX_BYTES = 32 * 1024 * 1024
MAX_CONFIRMATION_BYTES = 40 * 1024 * 1024
MAX_ENTRIES = 100
VAULT_FILENAME = "vault.yintian-vault"
KEY_FILENAME = "vault.key"
MIGRATION_MARKER = ".legacy-import.json"
SOURCE_KINDS = {"manual", "openvino-ocr", "openvino-vlm"}


def _home() -> Path:
    return secure_io.checked_path(Path.home())


def default_data_dir() -> Path:
    override = os.environ.get("YINTIAN_VAULT_DIR", "").strip()
    if override:
        return secure_io.checked_path(override)
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA", "").strip()
        if not root:
            raise RuntimeError("VAULT_LOCATION_UNAVAILABLE: Windows 缺少 LOCALAPPDATA")
        return secure_io.checked_path(root) / "SafeFill" / "vault"
    if sys.platform == "darwin":
        return _home() / "Library" / "Application Support" / "SafeFill" / "vault"
    root = os.environ.get("XDG_DATA_HOME", "").strip()
    return (secure_io.checked_path(root) if root else _home() / ".local" / "share") / "safefill" / "vault"


def default_key_dir() -> Path:
    override = os.environ.get("YINTIAN_VAULT_KEY_DIR", "").strip()
    if override:
        return secure_io.checked_path(override)
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA", "").strip()
        if not root:
            raise RuntimeError("VAULT_LOCATION_UNAVAILABLE: Windows 缺少 LOCALAPPDATA")
        return secure_io.checked_path(root) / "SafeFill" / "keys"
    if sys.platform == "darwin":
        return _home() / "Library" / "Application Support" / "SafeFill" / "keys"
    root = os.environ.get("XDG_CONFIG_HOME", "").strip()
    return (secure_io.checked_path(root) if root else _home() / ".config") / "safefill" / "keys"


def default_vault_path() -> Path:
    return default_data_dir() / VAULT_FILENAME


def default_key_path() -> Path:
    return default_key_dir() / KEY_FILENAME


def legacy_data_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "data"


def legacy_vault_path() -> Path:
    return legacy_data_dir() / VAULT_FILENAME


def legacy_key_path() -> Path:
    return legacy_data_dir() / KEY_FILENAME


def _check_private_file(path: Path, what: str) -> None:
    if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise RuntimeError(f"VAULT_PERMISSIONS: {what}权限宽于 0600: {path}")


def _check_private_dir(path: Path, what: str = "数据目录") -> None:
    if not path.is_dir():
        raise RuntimeError(f"VAULT_PATH_INVALID: {what}不是目录: {path}")
    if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise RuntimeError(f"VAULT_PERMISSIONS: {what}权限宽于 0700: {path}")


def ensure_private_dir(path: Path) -> Path:
    """Create only the SafeFill leaf; never chmod an existing directory."""
    path = secure_io.checked_path(path)
    created = not path.exists()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if created and os.name != "nt":
        os.chmod(path, 0o700)
    _check_private_dir(path)
    return path


def load_or_create_key(key_path: Path, *, create: bool = False) -> str:
    key_path = secure_io.checked_path(key_path)
    if not key_path.is_file():
        if not create:
            raise RuntimeError("VAULT_KEY_MISSING: 本机保险柜密钥缺失，无法解锁保险柜")
        ensure_private_dir(key_path.parent)
        secret = secrets.token_urlsafe(32)
        secure_io.atomic_write(key_path, secret.encode("ascii"), overwrite=False)
        return secret
    _check_private_dir(key_path.parent, "密钥目录")
    _check_private_file(key_path, "保险柜密钥")
    secret = secure_io.read_bytes(key_path, 256).decode("ascii").strip()
    if len(secret) < 32:
        raise RuntimeError("VAULT_KEY_INVALID: 本机保险柜密钥无效")
    return secret


def vault_format(vault_path: Path) -> str:
    _check_private_dir(vault_path.parent)
    _check_private_file(vault_path, "保险柜")
    try:
        envelope = json.loads(secure_io.read_bytes(vault_path, MAX_BYTES))
    except Exception:
        raise ValueError("VAULT_INVALID: 保险柜文件损坏或不是 JSON") from None
    fmt = envelope.get("format") if isinstance(envelope, dict) else None
    if fmt not in {FORMAT_V1, FORMAT_V2}:
        raise ValueError("VAULT_INVALID: 不支持的保险柜格式")
    return fmt


def _migration_marker_path(vault_path: Path) -> Path:
    return vault_path.parent / MIGRATION_MARKER


def _legacy_v2_marker(source_vault: Path, source_key: Path) -> dict:
    return {
        "format": "safefill-legacy-import/1",
        "source_format": FORMAT_V2,
        "source": str(source_vault),
        "source_vault_sha256": file_digest(source_vault),
        "source_key_sha256": file_digest(source_key, 256),
    }


def _legacy_v1_marker(source_vault: Path) -> dict:
    return {
        "format": "safefill-legacy-import/1",
        "source_format": FORMAT_V1,
        "source": str(source_vault),
        "source_vault_sha256": file_digest(source_vault),
    }


def _validate_marker(marker: Path, expected: dict, target_vault: Path, target_key: Path) -> None:
    try:
        _check_private_file(marker, "迁移标记")
        if json.loads(secure_io.read_bytes(marker, 4096)) != expected:
            raise ValueError
        load_vault(target_vault, load_or_create_key(target_key))
    except Exception:
        raise RuntimeError("VAULT_MIGRATION_CONFLICT: 迁移标记损坏、来源已变化或目标无法解密") from None


def migrate_legacy_v2_install() -> dict | None:
    """Copy an old in-skill v2 vault to per-user storage without deleting the source."""
    source_vault, source_key = legacy_vault_path(), legacy_key_path()
    if not source_vault.is_file() or vault_format(source_vault) != FORMAT_V2:
        return None
    target_vault, target_key = default_vault_path(), default_key_path()
    marker = _migration_marker_path(target_vault)
    if not source_key.is_file():
        raise RuntimeError("VAULT_KEY_MISSING: 旧版 v2 保险柜缺少配套密钥，未迁移")
    _check_private_file(source_vault, "旧保险柜")
    _check_private_file(source_key, "旧保险柜密钥")
    expected_marker = _legacy_v2_marker(source_vault, source_key)
    ensure_private_dir(target_vault.parent)
    ensure_private_dir(target_key.parent)
    with secure_io.file_lock(str(target_vault) + ".migration.lock"):
        target_state = (target_vault.exists(), target_key.exists())
        if marker.is_file():
            if target_state != (True, True):
                raise RuntimeError("VAULT_MIGRATION_CONFLICT: 迁移标记存在但目标保险柜或密钥缺失")
            _validate_marker(marker, expected_marker, target_vault, target_key)
            return {"migrated": False, "source": str(source_vault), "target": str(target_vault)}
        if target_state != (False, False):
            if target_state == (True, True):
                same = (secure_io.read_bytes(target_vault, MAX_BYTES) == secure_io.read_bytes(source_vault, MAX_BYTES)
                        and secure_io.read_bytes(target_key, 256) == secure_io.read_bytes(source_key, 256))
                if same:
                    secure_io.atomic_write(marker, collection.canonical(expected_marker), overwrite=False)
                    return {"migrated": True, "source": str(source_vault), "target": str(target_vault)}
            raise RuntimeError("VAULT_MIGRATION_CONFLICT: 新存储位置已有不同或不完整数据，未覆盖")
        key = load_or_create_key(source_key)
        profile = load_vault(source_vault, key)
        secure_io.atomic_write(target_key, secure_io.read_bytes(source_key, 256), overwrite=False)
        try:
            secure_io.atomic_write(target_vault, secure_io.read_bytes(source_vault, MAX_BYTES), overwrite=False)
            if collection.canonical(load_vault(target_vault, load_or_create_key(target_key))) != collection.canonical(profile):
                raise RuntimeError("VAULT_MIGRATION_VERIFY_FAILED: 迁移后解密校验不一致")
            secure_io.atomic_write(marker, collection.canonical(expected_marker), overwrite=False)
        except Exception:
            target_vault.unlink(missing_ok=True)
            target_key.unlink(missing_ok=True)
            raise
    return {"migrated": True, "source": str(source_vault), "target": str(target_vault)}


def resolve_default_storage() -> tuple[Path, Path, dict | None]:
    target_vault, target_key = default_vault_path(), default_key_path()
    source = legacy_vault_path()
    if target_vault.is_file() or target_key.is_file():
        if target_vault.is_file() and not target_key.is_file():
            raise RuntimeError("VAULT_STORAGE_INCOMPLETE: 新存储位置的保险柜与密钥不完整")
        if not target_vault.is_file() and source.is_file():
            raise RuntimeError("VAULT_MIGRATION_CONFLICT: 新密钥已存在但旧保险柜尚未迁移")
        if target_vault.is_file() and target_key.is_file() and source.is_file():
            if vault_format(source) == FORMAT_V2:
                migrated = migrate_legacy_v2_install()
                return target_vault, target_key, migrated if migrated and migrated["migrated"] else None
            marker = _migration_marker_path(target_vault)
            if not marker.is_file():
                raise RuntimeError("VAULT_MIGRATION_CONFLICT: 新存储位置和旧版 v1 保险柜同时存在，未覆盖")
            _validate_marker(marker, _legacy_v1_marker(source), target_vault, target_key)
        return target_vault, target_key, None
    if source.is_file():
        if vault_format(source) == FORMAT_V1:
            return source, target_key, {"migration_required": True, "source": str(source), "target": str(target_vault)}
        migrated = migrate_legacy_v2_install()
        return target_vault, target_key, migrated
    return target_vault, target_key, None


def _validate_source(source) -> dict:
    if not isinstance(source, dict) or source.get("kind") not in SOURCE_KINDS:
        raise ValueError("VAULT_INVALID: 条目来源无效（manual/openvino-ocr/openvino-vlm）")
    digest = source.get("sha256")
    if digest is not None and not (isinstance(digest, str) and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)):
        raise ValueError("VAULT_INVALID: 条目来源 sha256 无效")
    return {"kind": source["kind"], **({"sha256": digest} if digest else {})}


def _validate_attachments(items) -> list:
    if not isinstance(items, list) or len(items) > 20:
        raise ValueError("VAULT_INVALID: 条目附件数量无效")
    total = 0
    for item in items:
        try:
            data = base64.b64decode(item["data_b64"], validate=True)
            total += len(data)
            if len(data) > collection.MAX_FILE_BYTES or total > collection.MAX_TOTAL_BYTES:
                raise ValueError("VAULT_LIMIT: 保险柜附件超过大小上限")
            if len(data) != item["size"] or collection.sha256_bytes(data) != item["sha256"]:
                raise ValueError("VAULT_INVALID: 保险柜附件校验失败")
        except (KeyError, TypeError, ValueError):
            raise ValueError("VAULT_INVALID: 保险柜附件结构或摘要无效") from None
    return items


def validate_entry(entry) -> dict:
    if not isinstance(entry, dict):
        raise ValueError("VAULT_INVALID: 条目结构无效")
    entry_type = entry.get("type")
    if entry_type not in collection.ALLOWED_TYPES:
        raise ValueError("VAULT_INVALID: 条目类型无效")
    label = entry.get("label", "")
    if not isinstance(label, str) or len(label) > 100:
        raise ValueError("VAULT_INVALID: 条目标签无效")
    result = {"type": entry_type, "label": label, "source": _validate_source(entry.get("source"))}
    updated_at = entry.get("updated_at")
    if updated_at is not None:
        if not isinstance(updated_at, str) or len(updated_at) > 64:
            raise ValueError("VAULT_INVALID: 条目更新时间无效")
        result["updated_at"] = updated_at
    if entry_type in collection.ATTACHMENT_TYPES:
        if "value" in entry:
            raise ValueError("VAULT_INVALID: 附件条目不应含标量值")
        result["attachments"] = _validate_attachments(entry.get("attachments", []))
    else:
        value = entry.get("value")
        if not isinstance(value, str) or len(value) > collection.MAX_VALUE_CHARS:
            raise ValueError("VAULT_INVALID: 条目值无效")
        result["value"] = value
    return result


def validate_profile(profile) -> dict:
    if not isinstance(profile, dict) or profile.get("format") != FORMAT_V2:
        raise ValueError("VAULT_INVALID: 不支持的保险柜内容")
    entries = profile.get("entries")
    if not isinstance(entries, dict) or len(entries) > MAX_ENTRIES:
        raise ValueError("VAULT_INVALID: 保险柜条目数量无效")
    if any(not isinstance(key, str) or not collection.FIELD_ID_RE.fullmatch(key) for key in entries):
        raise ValueError("VAULT_INVALID: 保险柜条目 id 无效")
    profile["entries"] = {key: validate_entry(entry) for key, entry in entries.items()}
    return profile


def load_vault(vault_path: Path, key: str) -> dict:
    try:
        _check_private_dir(vault_path.parent)
        _check_private_file(vault_path, "保险柜")
        envelope = json.loads(secure_io.read_bytes(vault_path, MAX_BYTES))
        if envelope.get("format") == FORMAT_V1:
            raise RuntimeError("VAULT_MIGRATION_REQUIRED: 旧版保险柜必须先运行 vault-migrate")
        if envelope.get("format") != FORMAT_V2:
            raise ValueError("format")
        raw = collection.aes_gcm_open(envelope, key, FORMAT_V2.encode())
        return validate_profile(json.loads(raw))
    except RuntimeError:
        raise
    except Exception:
        raise ValueError("VAULT_UNLOCK_FAILED: 密钥不符或保险柜已损坏") from None


def _decode_v1(envelope: dict, password: str) -> dict:
    try:
        old = json.loads(collection.aes_gcm_open(envelope, password, FORMAT_V1.encode()))
        if (not isinstance(old, dict) or old.get("version") != 1
                or not isinstance(old.get("types"), dict)
                or not isinstance(old.get("values", {}), dict)
                or not isinstance(old.get("attachments", {}), dict)):
            raise ValueError
    except Exception:
        raise ValueError("VAULT_UNLOCK_FAILED: 旧保险柜密码错误或已损坏") from None
    entries = {}
    for entry_id, entry_type in old.get("types", {}).items():
        entry = {"type": entry_type, "label": "", "source": {"kind": "manual"}}
        if entry_type in collection.ATTACHMENT_TYPES:
            entry["attachments"] = old.get("attachments", {}).get(entry_id, [])
        else:
            entry["value"] = old.get("values", {}).get(entry_id, "")
        entries[entry_id] = entry
    return validate_profile({"format": FORMAT_V2, "entries": entries})


def migrate_v1(source_vault: Path, password: str, target_vault: Path, target_key: Path) -> dict:
    if vault_format(source_vault) != FORMAT_V1:
        raise ValueError("VAULT_MIGRATION_NOT_REQUIRED: 源保险柜不是 v1")
    _check_private_file(source_vault, "旧保险柜")
    profile = _decode_v1(json.loads(secure_io.read_bytes(source_vault, MAX_BYTES)), password)
    ensure_private_dir(target_vault.parent)
    ensure_private_dir(target_key.parent)
    marker = (_migration_marker_path(target_vault)
              if source_vault == legacy_vault_path() and target_vault == default_vault_path() else None)
    with secure_io.file_lock(str(target_vault) + ".migration.lock"):
        if target_vault.exists() or target_key.exists() or (marker is not None and marker.exists()):
            raise RuntimeError("VAULT_MIGRATION_CONFLICT: 目标保险柜或密钥已存在，未覆盖")
        key = load_or_create_key(target_key, create=True)
        try:
            save_vault(target_vault, key, profile, create=True)
            if collection.canonical(load_vault(target_vault, key)) != collection.canonical(profile):
                raise RuntimeError("VAULT_MIGRATION_VERIFY_FAILED: 迁移后解密校验不一致")
            if marker is not None:
                secure_io.atomic_write(marker, collection.canonical(_legacy_v1_marker(source_vault)), overwrite=False)
        except Exception:
            target_vault.unlink(missing_ok=True)
            target_key.unlink(missing_ok=True)
            if marker is not None:
                marker.unlink(missing_ok=True)
            raise
    return {"migrated": True, "source": str(source_vault), "target": str(target_vault), "entry_count": len(profile["entries"])}


def save_vault(vault_path: Path, key: str, profile: dict, *, create: bool = False) -> None:
    validate_profile(profile)
    blob = collection.canonical(collection.aes_gcm_seal(FORMAT_V2, collection.canonical(profile), key, FORMAT_V2.encode()))
    if len(blob) > MAX_BYTES:
        raise ValueError("VAULT_LIMIT: 保险柜超过大小上限")
    secure_io.atomic_write(vault_path, blob, overwrite=not create)


def seal_confirmation(path: Path, key: str, operation: str, payload: dict) -> dict:
    now = int(datetime.now(timezone.utc).timestamp())
    body = {"format": CONFIRMATION_FORMAT, "operation": operation, "issued_at": now,
            "expires_at": now + CONFIRMATION_TTL_SECONDS, "nonce": secrets.token_hex(16), **payload}
    blob = collection.canonical(collection.aes_gcm_seal(
        CONFIRMATION_FORMAT, collection.canonical(body), key, CONFIRMATION_FORMAT.encode()))
    if len(blob) > MAX_CONFIRMATION_BYTES:
        raise ValueError("CONFIRMATION_LIMIT: 确认文件超过大小上限")
    secure_io.atomic_write(path, blob, overwrite=False)
    return {"confirmation": str(path), "expires_at": body["expires_at"]}


def open_confirmation(path: Path, key: str, operation: str) -> dict:
    try:
        _check_private_file(path, "确认文件")
        envelope = json.loads(secure_io.read_bytes(path, MAX_CONFIRMATION_BYTES))
        if envelope.get("format") != CONFIRMATION_FORMAT:
            raise ValueError
        body = json.loads(collection.aes_gcm_open(envelope, key, CONFIRMATION_FORMAT.encode()))
        if body.get("format") != CONFIRMATION_FORMAT or body.get("operation") != operation:
            raise ValueError
        now = int(datetime.now(timezone.utc).timestamp())
        if not isinstance(body.get("issued_at"), int) or not isinstance(body.get("expires_at"), int):
            raise ValueError
        if body["issued_at"] > now + 60 or body["expires_at"] <= now or body["expires_at"] - body["issued_at"] != CONFIRMATION_TTL_SECONDS:
            raise RuntimeError("CONFIRMATION_EXPIRED: 确认已过期，请重新预览")
        return body
    except RuntimeError:
        raise
    except Exception:
        raise ValueError("CONFIRMATION_INVALID: 确认文件无效、被篡改或密钥不符") from None


def file_digest(path: Path | None, limit: int = MAX_BYTES) -> str | None:
    return collection.sha256_bytes(secure_io.read_bytes(path, limit)) if path else None


def build_entry(entry_id: str, spec: dict, build_attachments) -> dict:
    if not isinstance(spec, dict):
        raise ValueError(f"VAULT_ENTRY_INVALID: 条目 {entry_id} 必须是对象")
    entry_type = spec.get("type")
    if entry_type not in collection.ALLOWED_TYPES:
        raise ValueError(f"VAULT_ENTRY_INVALID: 条目 {entry_id} 类型无效")
    label = spec.get("label", "")
    if not isinstance(label, str) or len(label) > 100:
        raise ValueError(f"VAULT_ENTRY_INVALID: 条目 {entry_id} 标签无效")
    source = _validate_source(spec.get("source") or {"kind": "manual"})
    entry = {"type": entry_type, "label": label, "source": source, "updated_at": datetime.now(timezone.utc).isoformat()}
    if entry_type in collection.ATTACHMENT_TYPES:
        paths = spec.get("paths")
        if not isinstance(paths, list) or not paths or any(not isinstance(item, str) for item in paths):
            raise ValueError(f"VAULT_ENTRY_INVALID: 附件条目 {entry_id} 需要非空 paths 列表")
        entry["attachments"] = build_attachments(entry_id, entry_type, paths)
    else:
        value = spec.get("value")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"VAULT_ENTRY_INVALID: 条目 {entry_id} 需要非空文本值")
        value = collection.normalize_value(entry_type, value.strip())
        if len(value) > collection.MAX_VALUE_CHARS:
            raise ValueError(f"VAULT_LIMIT: 条目 {entry_id} 值超过长度上限")
        if entry_type == "phone_cn":
            import re
            if not re.fullmatch(r"1[3-9]\d{9}", value):
                raise ValueError(f"VAULT_ENTRY_INVALID: 条目 {entry_id} 手机号格式无效")
        if entry_type == "cn_id":
            from ocr_matcher import validate_chinese_id
            if not validate_chinese_id(value):
                raise ValueError(f"VAULT_ENTRY_INVALID: 条目 {entry_id} 身份证号校验失败")
        if entry_type == "date" and not collection.valid_date(value):
            raise ValueError(f"VAULT_ENTRY_INVALID: 条目 {entry_id} 日期无效")
        entry["value"] = value
    return entry


def select_fields(form, profile, mapping=None):
    """Select only explicit mappings or exact IDs; never infer semantics from a type."""
    mapping = mapping or {}
    fields = {field["id"]: field for field in form["fields"]}
    if not isinstance(mapping, dict) or any(not isinstance(key, str) or key not in fields for key in mapping):
        raise ValueError("MAPPING_INVALID: 映射只能引用本次请求字段")
    if any(not isinstance(source, str) for source in mapping.values()):
        raise ValueError("MAPPING_INVALID: 映射取值必须是保险柜条目 id")
    entries = profile["entries"]
    values, attachments, missing, matches = {}, {}, [], {}
    for key, field in fields.items():
        source = mapping.get(key)
        if source is not None:
            entry = entries.get(source)
            if entry is None or entry["type"] != field["type"]:
                raise ValueError(f"MAPPING_INVALID: 字段 {key} 映射的条目不存在或类型不同")
        else:
            exact = entries.get(key)
            source = key if exact is not None and exact["type"] == field["type"] else None
        entry = entries.get(source) if source else None
        if field["type"] in collection.ATTACHMENT_TYPES:
            if entry is not None and entry.get("attachments"):
                attachments[key] = copy.deepcopy(entry["attachments"])
                matches[key] = source
            else:
                missing.append(key)
        elif entry is not None and entry.get("value"):
            values[key] = entry["value"]
            matches[key] = source
        else:
            missing.append(key)
    return values, attachments, missing, matches


def status_view(profile: dict, form=None) -> dict:
    result = {
        "vault": True,
        "format": FORMAT_V2,
        "entry_count": len(profile["entries"]),
        "entries": {
            entry_id: {"type": entry["type"], "label": entry.get("label", ""),
                       "has_content": bool(entry.get("value") or entry.get("attachments")),
                       "source": entry["source"]["kind"]}
            for entry_id, entry in profile["entries"].items()
        },
    }
    if form is not None:
        _values, _attachments, missing, matches = select_fields(form, profile)
        result["fields"] = [
            {"id": field["id"], "label": field["label"], "type": field["type"],
             "required": bool(field.get("required")), "match": matches.get(field["id"]),
             "status": "matched" if field["id"] in matches else "missing"}
            for field in form["fields"]
        ]
        result["missing"] = missing
    return result
