"""Host-Agent evidence is untrusted data. This module never calls a model or network."""
from __future__ import annotations

import json

import secure_io

FORMAT = 'yintian-agent-ocr/1'
BINDING_KEYS = ('task_id', 'schema_hash', 'notice_hash')
BACKENDS = {'auto', 'local', 'agent', 'manual'}
MAX_RESULT_BYTES = 128 * 1024


def bindings(field, fields):
    if 'ocr_fields' in field:
        return field['ocr_fields']
    if 'front' in field['id'].split('_'):
        return [f['id'] for f in fields if f['id'] == 'name' or f['type'] in {'cn_id', 'address'}]
    return []


def validate_fields(fields):
    scalars = {f['id'] for f in fields if f['type'] not in {'image_attachment', 'pdf_attachment'}}
    for field in fields:
        if 'ocr_backend' in field and (field['type'] not in {'image_attachment', 'pdf_attachment'} or field['ocr_backend'] not in BACKENDS):
            raise ValueError('OCR_BACKEND_INVALID: 附件识别方式限 auto/local/agent/manual')
        keys = bindings(field, fields)
        if not isinstance(keys, list) or any(not isinstance(k, str) or k not in scalars for k in keys) or len(set(keys)) != len(keys):
            raise ValueError('OCR_BINDING_INVALID: 识别字段必须来自本次模板')


def load_result(path):
    try:
        return json.loads(secure_io.read_bytes(path, MAX_RESULT_BYTES))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError('AGENT_OCR_INVALID: 候选文件不是有效 JSON') from None


def validate_result(task, attachments, result):
    import collection
    error = 'AGENT_OCR_INVALID: 候选格式、任务、模板或附件不匹配'
    if not isinstance(result, dict) or set(result) != {'format', 'items', *BINDING_KEYS} or result.get('format') != FORMAT:
        raise ValueError(error)
    if len(collection.canonical(result)) > MAX_RESULT_BYTES or any(result[k] != task.get(k) for k in BINDING_KEYS):
        raise ValueError(error)
    if not isinstance(result['items'], list) or not 1 <= len(result['items']) <= 60:
        raise ValueError(error)
    definitions = {f['id']: f for f in task['fields']}
    actual = {(item['field_id'], collection.sha256_bytes(item['data'])) for item in attachments}
    seen = set()
    for record in result['items']:
        if not isinstance(record, dict) or set(record) != {'field_id', 'sha256', 'candidates'}:
            raise ValueError(error)
        if not isinstance(record['field_id'], str) or not isinstance(record['sha256'], str):
            raise ValueError(error)
        key = (record['field_id'], record['sha256'])
        field = definitions.get(record['field_id'], {})
        if key not in actual or key in seen or field.get('ocr_backend', 'local') not in {'auto', 'agent'}:
            raise ValueError(error)
        allowed = bindings(field, task['fields'])
        candidates = record['candidates']
        if not allowed or not isinstance(candidates, dict) or set(candidates) - set(allowed):
            raise ValueError(error)
        for values in candidates.values():
            if not isinstance(values, list) or len(values) > 20 or any(not isinstance(value, str) or len(value) > 512 for value in values):
                raise ValueError(error)
        seen.add(key)
    return result


def agent_record(payload, item):
    import collection
    if payload.get('agent_ocr_confirmed') is not True:
        return None
    for record in payload.get('agent_ocr', {}).get('items', []):
        if record['field_id'] == item['field_id'] and record['sha256'] == collection.sha256_bytes(item['data']):
            return record['candidates']
    return None


def select(payload, item, field, local_ocr):
    """Return text, warnings, candidate map, source, fixed reason; never secret metadata."""
    mode = field.get('ocr_backend', 'local')  # Old templates retain their existing meaning.
    if mode in {'auto', 'local'}:
        text, warnings = local_ocr(item)
        unavailable = any('unavailable' in w or 'error' in w for w in warnings)
        if not unavailable or mode == 'local':
            return text, warnings, None, 'local', 'local_issue' if warnings else 'local_completed'
    if mode in {'auto', 'agent'}:
        candidates = agent_record(payload, item)
        if candidates is not None:
            return '', [], candidates, 'agent', 'host_agent_candidates'
    return '', [], None, 'manual', 'manual_selected' if mode == 'manual' else 'recognition_unavailable'


def candidate_valid(field, value):
    import re
    import collection
    from ocr_matcher import validate_chinese_id
    value = collection.normalize_value(field['type'], value)
    if not value:
        return False
    if field['type'] == 'cn_id':
        return validate_chinese_id(value)
    if field['type'] == 'phone_cn':
        return bool(re.fullmatch(r'1[3-9]\d{9}', value))
    if field['type'] == 'date':
        return collection.valid_date(value)
    if field['type'] == 'single_choice':
        return value in field.get('options', [])
    return True
