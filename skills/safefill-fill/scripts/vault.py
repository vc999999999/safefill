"""SafeFill · 员工本机加密保险柜：请求无关的通用档案，本机密钥静态加密。

保险柜与密钥默认位于 SKILL_ROOT/data/（0700）：vault.yintian-vault 与
vault.key 均为 0600。密钥只存本机本账户；保险柜静态加密，回执仍对收集方
端到端加密。任何命令都不把保险柜内容发往网络。条目可带 source 溯源
（manual / openvino-ocr / openvino-vlm），候选经本人确认后才写入。
"""
import base64
import copy
import getpass
import json
import os
import secrets
import stat
from datetime import datetime, timezone
from pathlib import Path

import collection
import secure_io

FORMAT_V2 = 'yintian-vault/2'
FORMAT_V1 = 'yintian-vault/1'
MAX_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 100
VAULT_FILENAME = 'vault.yintian-vault'
KEY_FILENAME = 'vault.key'
SOURCE_KINDS = {'manual', 'openvino-ocr', 'openvino-vlm'}
AUTO_MATCH_TYPES = {'phone_cn', 'cn_id', 'address', 'date'}


def default_data_dir() -> Path:
    override = os.environ.get('YINTIAN_VAULT_DIR', '').strip()
    if override:
        return secure_io.checked_path(override)
    return Path(__file__).resolve().parent.parent / 'data'


def default_vault_path() -> Path:
    return default_data_dir() / VAULT_FILENAME


def _check_private(path: Path, what: str) -> None:
    if os.name != 'nt' and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise RuntimeError(f'VAULT_PERMISSIONS: {what}权限宽于 0600，请执行 chmod 600 {path}')


def ensure_data_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != 'nt':
        os.chmod(path, 0o700)
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise RuntimeError(f'VAULT_PERMISSIONS: 数据目录权限宽于 0700，请执行 chmod 700 {path}')
    return path


def load_or_create_key(data_dir: Path, *, create=False) -> str:
    key_path = data_dir / KEY_FILENAME
    if not key_path.is_file():
        if not create:
            raise RuntimeError('VAULT_KEY_MISSING: 本机保险柜密钥缺失，无法解锁保险柜')
        secret = secrets.token_urlsafe(32)
        secure_io.atomic_write(key_path, secret.encode('ascii'), overwrite=False)
        return secret
    _check_private(key_path, '保险柜密钥')
    return secure_io.read_bytes(key_path, 256).decode('ascii').strip()


def _validate_source(source) -> dict:
    if not isinstance(source, dict) or source.get('kind') not in SOURCE_KINDS:
        raise ValueError('VAULT_INVALID: 条目来源无效（manual/openvino-ocr/openvino-vlm）')
    digest = source.get('sha256')
    if digest is not None and not (isinstance(digest, str) and len(digest) == 64 and all(c in '0123456789abcdef' for c in digest)):
        raise ValueError('VAULT_INVALID: 条目来源 sha256 无效')
    return {'kind': source['kind'], **({'sha256': digest} if digest else {})}


def _validate_attachments(items) -> list:
    if not isinstance(items, list) or len(items) > 20:
        raise ValueError('VAULT_INVALID: 条目附件数量无效')
    total = 0
    for item in items:
        data = base64.b64decode(item['data_b64'], validate=True)
        total += len(data)
        if len(data) > collection.MAX_FILE_BYTES or total > collection.MAX_TOTAL_BYTES:
            raise ValueError('VAULT_LIMIT: 保险柜附件超过大小上限')
        if len(data) != item['size'] or collection.sha256_bytes(data) != item['sha256']:
            raise ValueError('VAULT_INVALID: 保险柜附件校验失败')
    return items


def validate_entry(entry) -> dict:
    if not isinstance(entry, dict):
        raise ValueError('VAULT_INVALID: 条目结构无效')
    entry_type = entry.get('type')
    if entry_type not in collection.ALLOWED_TYPES:
        raise ValueError('VAULT_INVALID: 条目类型无效')
    label = entry.get('label', '')
    if not isinstance(label, str) or len(label) > 100:
        raise ValueError('VAULT_INVALID: 条目标签无效')
    result = {'type': entry_type, 'label': label, 'source': _validate_source(entry.get('source'))}
    if entry_type in collection.ATTACHMENT_TYPES:
        if 'value' in entry:
            raise ValueError('VAULT_INVALID: 附件条目不应含标量值')
        result['attachments'] = _validate_attachments(entry.get('attachments', []))
    else:
        value = entry.get('value')
        if not isinstance(value, str) or len(value) > collection.MAX_VALUE_CHARS:
            raise ValueError('VAULT_INVALID: 条目值无效')
        result['value'] = value
    return result


def validate_profile(profile) -> dict:
    if not isinstance(profile, dict) or profile.get('format') != FORMAT_V2:
        raise ValueError('VAULT_INVALID: 不支持的保险柜内容')
    entries = profile.get('entries')
    if not isinstance(entries, dict) or len(entries) > MAX_ENTRIES:
        raise ValueError('VAULT_INVALID: 保险柜条目数量无效')
    if any(not isinstance(key, str) or not collection.FIELD_ID_RE.fullmatch(key) for key in entries):
        raise ValueError('VAULT_INVALID: 保险柜条目 id 无效')
    profile['entries'] = {key: validate_entry(entry) for key, entry in entries.items()}
    return profile


def load_vault(vault_path: Path, key: str) -> dict:
    try:
        envelope = json.loads(secure_io.read_bytes(vault_path, MAX_BYTES))
        fmt = envelope.get('format')
        if fmt == FORMAT_V2:
            raw = collection.aes_gcm_open(envelope, key, FORMAT_V2.encode())
            return validate_profile(json.loads(raw))
        if fmt == FORMAT_V1:
            return _migrate_v1(vault_path, envelope)
        raise ValueError('format')
    except RuntimeError:
        raise
    except Exception:
        raise ValueError('VAULT_UNLOCK_FAILED: 密钥不符或保险柜已损坏') from None


def _migrate_v1(vault_path: Path, envelope) -> dict:
    """v1 密码保险柜一次性迁移：本人终端输入旧密码，转为 v2 本机密钥格式。"""
    collection.require_tty('vault migrate')
    password = getpass.getpass('检测到旧版密码保险柜，输入原保险柜密码完成一次性迁移: ')
    try:
        old = json.loads(collection.aes_gcm_open(envelope, password, FORMAT_V1.encode()))
        if old.get('version') != 1:
            raise ValueError
    except Exception:
        raise ValueError('VAULT_UNLOCK_FAILED: 旧保险柜密码错误或已损坏') from None
    entries = {}
    for entry_id, entry_type in old.get('types', {}).items():
        entry = {'type': entry_type, 'label': '', 'source': {'kind': 'manual'}}
        if entry_type in collection.ATTACHMENT_TYPES:
            entry['attachments'] = old.get('attachments', {}).get(entry_id, [])
        else:
            entry['value'] = old.get('values', {}).get(entry_id, '')
        entries[entry_id] = entry
    profile = validate_profile({'format': FORMAT_V2, 'entries': entries})
    data_dir = ensure_data_dir(default_data_dir())
    key = load_or_create_key(data_dir, create=True)
    save_vault(default_vault_path(), key, profile)
    if secure_io.checked_path(vault_path) != default_vault_path():
        print(f'已迁移并保存到 {default_vault_path()}；旧密码保险柜 {vault_path} 请自行删除。')
    return profile


def save_vault(vault_path: Path, key: str, profile: dict, *, create=False) -> None:
    validate_profile(profile)
    raw = collection.canonical(profile)
    envelope = collection.aes_gcm_seal(FORMAT_V2, raw, key, FORMAT_V2.encode())
    blob = collection.canonical(envelope)
    if len(blob) > MAX_BYTES:
        raise ValueError('VAULT_LIMIT: 保险柜超过大小上限')
    secure_io.atomic_write(vault_path, blob, overwrite=not create)


def build_entry(entry_id: str, spec: dict, build_attachments) -> dict:
    """把 answers JSON 中的一条录入规范化为保险柜条目；build_attachments 由 fill 层注入。"""
    if not isinstance(spec, dict):
        raise ValueError(f'VAULT_ENTRY_INVALID: 条目 {entry_id} 必须是对象')
    entry_type = spec.get('type')
    if entry_type not in collection.ALLOWED_TYPES:
        raise ValueError(f'VAULT_ENTRY_INVALID: 条目 {entry_id} 类型无效')
    label = spec.get('label', '')
    if not isinstance(label, str) or len(label) > 100:
        raise ValueError(f'VAULT_ENTRY_INVALID: 条目 {entry_id} 标签无效')
    source = _validate_source(spec.get('source') or {'kind': 'manual'})
    entry = {'type': entry_type, 'label': label, 'source': source,
             'updated_at': datetime.now(timezone.utc).isoformat()}
    if entry_type in collection.ATTACHMENT_TYPES:
        paths = spec.get('paths')
        if not isinstance(paths, list) or not paths or any(not isinstance(p, str) for p in paths):
            raise ValueError(f'VAULT_ENTRY_INVALID: 附件条目 {entry_id} 需要非空 paths 列表')
        built = build_attachments(entry_id, entry_type, paths)
        entry['attachments'] = built
    else:
        value = spec.get('value')
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'VAULT_ENTRY_INVALID: 条目 {entry_id} 需要非空文本值')
        value = collection.normalize_value(entry_type, value.strip())
        if len(value) > collection.MAX_VALUE_CHARS:
            raise ValueError(f'VAULT_LIMIT: 条目 {entry_id} 值超过长度上限')
        if entry_type == 'phone_cn':
            import re
            if not re.fullmatch(r'1[3-9]\d{9}', value):
                raise ValueError(f'VAULT_ENTRY_INVALID: 条目 {entry_id} 手机号格式无效')
        if entry_type == 'cn_id':
            from ocr_matcher import validate_chinese_id
            if not validate_chinese_id(value):
                raise ValueError(f'VAULT_ENTRY_INVALID: 条目 {entry_id} 身份证号校验失败')
        if entry_type == 'date' and not collection.valid_date(value):
            raise ValueError(f'VAULT_ENTRY_INVALID: 条目 {entry_id} 日期无效')
        entry['value'] = value
    return entry


def select_fields(form, profile, mapping=None):
    """显式 mapping → id 精确 → 类型唯一（phone_cn/cn_id/address/date）；绝不猜测。"""
    mapping = mapping or {}
    fields = {field['id']: field for field in form['fields']}
    if not isinstance(mapping, dict) or any(not isinstance(key, str) or key not in fields for key in mapping):
        raise ValueError('MAPPING_INVALID: 映射只能引用本次请求字段')
    if any(not isinstance(source, str) for source in mapping.values()):
        raise ValueError('MAPPING_INVALID: 映射取值必须是保险柜条目 id')
    entries = profile['entries']
    values, attachments, missing, matches = {}, {}, [], {}
    for key, field in fields.items():
        source = mapping.get(key)
        if source is not None:
            entry = entries.get(source)
            if entry is None or entry['type'] != field['type']:
                raise ValueError(f'MAPPING_INVALID: 字段 {key} 映射的条目不存在或类型不同')
        else:
            exact = entries.get(key)
            if exact is not None and exact['type'] == field['type']:
                source = key
            elif field['type'] in AUTO_MATCH_TYPES:
                candidates = [eid for eid, entry in entries.items() if entry['type'] == field['type']]
                source = candidates[0] if len(candidates) == 1 else None
        entry = entries.get(source) if source else None
        if field['type'] in collection.ATTACHMENT_TYPES:
            if entry is not None and entry.get('attachments'):
                attachments[key] = copy.deepcopy(entry['attachments'])
                matches[key] = source
            else:
                missing.append(key)
        else:
            if entry is not None and entry.get('value'):
                values[key] = entry['value']
                matches[key] = source
            else:
                missing.append(key)
    return values, attachments, missing, matches


def status_view(profile: dict, form=None) -> dict:
    """面向 Agent 的摘要：只暴露条目 id/类型/来源与匹配结果，绝不输出条目值。"""
    result = {
        'vault': True,
        'format': FORMAT_V2,
        'entry_count': len(profile['entries']),
        'entries': {
            entry_id: {
                'type': entry['type'],
                'label': entry.get('label', ''),
                'has_content': bool(entry.get('value') or entry.get('attachments')),
                'source': entry['source']['kind'],
            }
            for entry_id, entry in profile['entries'].items()
        },
    }
    if form is not None:
        _values, _attachments, missing, matches = select_fields(form, profile)
        result['fields'] = [
            {
                'id': field['id'],
                'label': field['label'],
                'type': field['type'],
                'required': bool(field.get('required')),
                'match': matches.get(field['id']),
                'status': 'matched' if field['id'] in matches else 'missing',
            }
            for field in form['fields']
        ]
        result['missing'] = missing
    return result
