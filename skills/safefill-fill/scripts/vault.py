"""Employee-owned encrypted profiles. No plaintext temporary files or network access."""
import base64
import copy
import getpass
import json
from pathlib import Path

import collection
import secure_io

FORMAT = 'yintian-vault/1'
MAX_BYTES = 32 * 1024 * 1024


def validate_profile(profile):
    if not isinstance(profile, dict) or profile.get('version') != 1:
        raise ValueError('VAULT_INVALID: 不支持的保险柜内容')
    for key in ('values', 'types', 'attachments'):
        if not isinstance(profile.get(key), dict) or len(profile[key]) > 100:
            raise ValueError('VAULT_INVALID: 保险柜字段结构无效')
        if any(not isinstance(k, str) or not collection.FIELD_ID_RE.fullmatch(k) for k in profile[key]):
            raise ValueError('VAULT_INVALID: 保险柜字段 id 无效')
    if any(not isinstance(value, str) or len(value) > 10000 for value in profile['values'].values()):
        raise ValueError('VAULT_INVALID: 保险柜字段值无效')
    if any(value not in collection.ALLOWED_TYPES for value in profile['types'].values()):
        raise ValueError('VAULT_INVALID: 保险柜字段类型无效')
    total = 0
    for items in profile['attachments'].values():
        if not isinstance(items, list) or len(items) > 20:
            raise ValueError('VAULT_INVALID: 保险柜附件数量无效')
        for item in items:
            data = base64.b64decode(item['data_b64'], validate=True)
            total += len(data)
            if len(data) > collection.MAX_FILE_BYTES or total > collection.MAX_TOTAL_BYTES:
                raise ValueError('VAULT_LIMIT: 保险柜附件超过大小上限')
            if len(data) != item['size'] or collection.sha256_bytes(data) != item['sha256']:
                raise ValueError('VAULT_INVALID: 保险柜附件校验失败')
    return profile


def load_vault(path, password):
    try:
        envelope = json.loads(secure_io.read_bytes(path, MAX_BYTES))
        if envelope.get('format') != FORMAT:
            raise ValueError('format')
        raw = collection.aes_gcm_open(envelope, password, FORMAT.encode())
        return validate_profile(json.loads(raw))
    except Exception:
        raise ValueError('VAULT_UNLOCK_FAILED: 密码错误或保险柜已损坏') from None


def save_vault(path, password, profile, *, create=False):
    if len(password) < 12:
        raise ValueError('VAULT_PASSWORD_SHORT: 保险柜密码至少 12 个字符')
    validate_profile(profile)
    raw = collection.canonical(profile)
    envelope = collection.aes_gcm_seal(FORMAT, raw, password, FORMAT.encode())
    blob = collection.canonical(envelope)
    if len(blob) > MAX_BYTES:
        raise ValueError('VAULT_LIMIT: 保险柜超过大小上限')
    secure_io.atomic_write(path, blob, overwrite=not create)


def select_fields(form, profile, mapping=None):
    """Exact id, explicit mapping, or a unique semantic type; never guess text identities."""
    mapping = mapping or {}
    fields = {field['id']: field for field in form['fields']}
    if not isinstance(mapping, dict) or any(key not in fields for key in mapping):
        raise ValueError('MAPPING_INVALID: 映射只能引用本次模板字段')
    values, attachments, missing, matches = {}, {}, [], {}
    for key, field in fields.items():
        source = mapping.get(key)
        if source is not None:
            if not isinstance(source, str) or profile['types'].get(source) != field['type']:
                raise ValueError('MAPPING_INVALID: 映射不存在或字段类型不同')
        elif profile['types'].get(key) == field['type']:
            source = key
        elif field['type'] in {'phone_cn', 'cn_id', 'address', 'date'}:
            candidates = [k for k, value in profile['types'].items() if value == field['type']]
            source = candidates[0] if len(candidates) == 1 else None
        container = profile['attachments'] if field['type'] in collection.ATTACHMENT_TYPES else profile['values']
        if source is not None and container.get(source):
            (attachments if field['type'] in collection.ATTACHMENT_TYPES else values)[key] = copy.deepcopy(container[source])
            matches[key] = source
        else:
            missing.append(key)
    return values, attachments, missing, matches


def _complete(form, values, attachments, *, edit=False):
    import fill
    for field in form['fields']:
        key = field['id']
        label = collection.terminal_text(field['label'])
        if field['type'] in collection.ATTACHMENT_TYPES:
            if attachments.get(key) and not edit:
                continue
            raw = getpass.getpass(f"{label}：输入文件路径（多个以 | 分隔；回车保留/跳过；- 清除）: ")
            if raw == '-':
                attachments.pop(key, None)
            elif raw:
                selected = {**form, 'fields': [field]}
                built, errors = fill._build_attachments(selected, {key: [p.strip() for p in raw.split('|')]})
                if errors:
                    raise ValueError('ATTACHMENT_INVALID: ' + '; '.join(errors))
                attachments.update(built)
        elif edit or not values.get(key):
            raw = getpass.getpass(f"{label}（输入隐藏；回车保留/跳过；- 清除）: ")
            if raw == '-':
                values.pop(key, None)
            elif raw:
                values[key] = raw


def edit_interactive(form_path, vault_path, *, create):
    import fill
    collection.require_tty('vault edit')
    path = secure_io.checked_path(vault_path)
    form = fill.load_form(form_path)
    with secure_io.file_lock(str(path) + '.lock'):
        if create and path.exists():
            raise FileExistsError('VAULT_EXISTS: 保险柜已存在，请用 vault-edit')
        password = getpass.getpass('保险柜密码（至少 12 个字符）: ')
        if create:
            if password != getpass.getpass('再次输入保险柜密码: '):
                raise ValueError('VAULT_PASSWORD_MISMATCH: 两次密码不同')
            profile = {'version': 1, 'types': {}, 'values': {}, 'attachments': {}}
        else:
            profile = load_vault(path, password)
        try:
            # Edit the profile's exact keys. Reusing a value must not overwrite a different alias.
            values = {f['id']: profile['values'][f['id']] for f in form['fields'] if f['id'] in profile['values']}
            attachments = {f['id']: profile['attachments'][f['id']] for f in form['fields'] if f['id'] in profile['attachments']}
            _complete(form, values, attachments, edit=True)
            optional_form = {**form, 'fields': [{**field, 'required': False} for field in form['fields']]}
            values, problems = fill._check_values(optional_form, values)
            if problems:
                raise ValueError('VALUES_INVALID: ' + '; '.join(problems))
            for field in form['fields']:
                key = field['id']
                profile['types'][key] = field['type']
                profile['values'].pop(key, None)
                profile['attachments'].pop(key, None)
            profile['values'].update(values)
            profile['attachments'].update(attachments)
            if input('将以上录入内容保存到本机加密保险柜？输入 SAVE: ').strip() != 'SAVE':
                raise ValueError('CANCELLED: 已取消保存')
            save_vault(path, password, profile, create=create)
            return {'saved': True, 'field_count': len(profile['types'])}
        finally:
            profile.clear()


def fill_interactive(form_path, vault_path, out_path, *, credential_path=None, mapping_path=None, agent_ocr_path=None):
    import fill
    collection.require_tty('fill')
    form = fill.bind_credential(fill.load_form(form_path), credential_path)
    if collection.task_expired(form):
        raise ValueError('TASK_EXPIRED: 模板已过期')
    mapping = json.loads(secure_io.read_bytes(mapping_path, 64 * 1024)) if mapping_path else {}
    profile = load_vault(vault_path, getpass.getpass('保险柜密码: '))
    try:
        values, attachments, _, _ = select_fields(form, profile, mapping)
        if not values.get('name') and form.get('name'):
            values['name'] = form['name']
        if form.get('_employee_id') and not values.get('employee_id'):
            values['employee_id'] = form['_employee_id']
        _complete(form, values, attachments)
        normalized, problems = fill._check_values(form, values)
        if problems:
            raise ValueError('VALUES_INVALID: ' + '; '.join(problems))
        agent_ocr = fill.prepare_agent_result(form, attachments, agent_ocr_path)
        if agent_ocr is not None:
            fill.confirm_submission(form, normalized, attachments, agent_ocr)
        else:
            fill.confirm_submission(form, normalized, attachments)
        return fill.seal_data(form, normalized, attachments, out_path, confirmed=True,
                              agent_ocr=agent_ocr, agent_confirmed=agent_ocr is not None)
    finally:
        profile.clear()
