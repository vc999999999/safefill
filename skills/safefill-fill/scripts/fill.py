"""SafeFill · 填写端：员工本机填写需求格式文件并产出 .yintian 密文。

只在员工自己的电脑上运行：纯本地、不联网；只读显式指定的文件；
定向提交兼容 yintian-submission/2；群发使用带个人认证的 /3。
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
GROUP_INVITE_PREFIX = "GRP-"
IMAGE_MIME_BY_SUFFIX = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
PDF_SUFFIX = ".pdf"
PDF_MIME = "application/pdf"
NOTICE_KEYS = ("title", "purpose", "deadline", "retention_until", "contact", "correction")
REQUIRED_FORM_KEYS = ("task_id", "fields", "public_key_pem", "key_id", "schema_hash", "notice_hash") + NOTICE_KEYS
VERIFY_HINT = "填写前请与发放人核对任务编号与公钥指纹（key_id）；不一致请勿填写。"


class FillError(Exception):
    """填写端可预期错误：信息面向员工，命令以非零退出。"""


def _bootstrap_repo_scripts() -> None:
    scripts = Path(__file__).resolve().parent
    if not all((scripts / name).is_file() for name in ('collection.py', 'ocr_matcher.py', 'secure_io.py', 'evidence_routing.py')):
        raise RuntimeError("填写者 Skill 安装不完整，请重新复制整个 safefill-fill 文件夹")
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))


_bootstrap_repo_scripts()
import collection  # noqa: E402
import fill_extract  # noqa: E402
import secure_io  # noqa: E402
import evidence_routing  # noqa: E402


def _public_key_fingerprint(public_key_pem: str) -> str:
    import hashlib

    from cryptography.hazmat.primitives import serialization

    public_key = serialization.load_pem_public_key(public_key_pem.encode("utf-8"))
    public_der = public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(public_der).hexdigest()[:24]


def load_form(form_path: str | Path) -> dict[str, Any]:
    """读取并校验定向 /1 或群发 /2 模板；不修改文件。"""
    path = secure_io.checked_path(form_path)
    if not path.is_file():
        raise FillError(f"需求格式文件不存在: {form_path}")
    try:
        form = json.loads(secure_io.read_bytes(path, 1024 * 1024))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FillError("需求格式文件不是有效 JSON") from exc
    if not isinstance(form, dict) or form.get("format") not in {FORM_FORMAT, "yintian-form/2"}:
        raise FillError(f"不是 {FORM_FORMAT} 需求格式文件")
    mode = form.get("mode")
    if mode not in ("group", "directed"):
        raise FillError("需求格式文件 mode 无效（应为 group 或 directed）")
    if mode == "group" and (form.get("format") != "yintian-form/2" or form.get("submission_auth") != collection.AUTH_VERSION or form.get("format_version") != collection.GROUP_FORMAT_VERSION):
        raise FillError("GROUP_AUTH_REQUIRED: 旧群发模板没有个人认证，请向 HR 索取新版模板及个人凭据")
    missing = [key for key in REQUIRED_FORM_KEYS if not form.get(key)]
    if missing:
        raise FillError(f"需求格式文件缺少必要字段: {', '.join(missing)}")
    if not collection.TASK_ID_RE.fullmatch(str(form["task_id"])):
        raise FillError("需求格式文件 task_id 无效")
    fields = form["fields"]
    if not isinstance(fields, list) or not fields or len(fields) > 100:
        raise FillError("需求格式文件 fields 无效")
    seen = set()
    for field in fields:
        if not isinstance(field, dict) or not collection.FIELD_ID_RE.fullmatch(str(field.get("id", ""))) or field["id"] in seen:
            raise FillError("需求格式文件字段 id 无效或重复")
        if field.get("type") not in collection.ALLOWED_TYPES or not isinstance(field.get("label"), str) or not field['label'] or len(field['label']) > 200:
            raise FillError(f"需求格式文件字段类型不受支持: {field.get('id')}")
        if field['type'] == 'single_choice' and (not isinstance(field.get('options'), list) or not field['options'] or len(field['options']) > 100 or any(not isinstance(v, str) or len(v) > 200 for v in field['options'])):
            raise FillError('FORM_INVALID: 单选字段选项无效')
        seen.add(field["id"])
    evidence_routing.validate_fields(fields)
    if collection.sha256_bytes(collection.canonical(fields)) != form["schema_hash"]:
        raise FillError("字段清单与 schema_hash 不匹配，文件可能被篡改，请向发放人重新索取")
    notice = {key: form[key] for key in NOTICE_KEYS}
    if collection.sha256_bytes(collection.canonical(notice)) != form["notice_hash"]:
        raise FillError("告知内容与 notice_hash 不匹配，文件可能被篡改，请向发放人重新索取")
    if "notice" in form and isinstance(form["notice"], dict):
        if collection.sha256_bytes(collection.canonical(form["notice"])) != form["notice_hash"]:
            raise FillError("notice 摘要与 notice_hash 不匹配，文件可能被篡改，请向发放人重新索取")
    fingerprint = _public_key_fingerprint(str(form["public_key_pem"]))
    form["_key_fingerprint"] = fingerprint
    form["_path"] = str(path)
    form.setdefault('template_version', '1.0')
    if form.get("format_version") and form["format_version"] != (collection.GROUP_FORMAT_VERSION if mode == "group" else collection.FORMAT_VERSION):
        raise FillError(f"需求格式文件要求的提交版本不受支持: {form['format_version']}")
    if mode == "directed":
        if not collection.INVITE_ID_RE.fullmatch(str(form.get("invite_id", ""))):
            raise FillError("directed 模式需求格式文件缺少有效 invite_id")
        token = form.get("invite_token") or form.get("token")
        if not isinstance(token, str) or not token:
            raise FillError("directed 模式需求格式文件缺少 invite_token")
        form["_token"] = token
    else:
        if form.get("invite_id") or form.get("invite_token") or form.get("token"):
            raise FillError("group 模式需求格式文件不应包含 invite_id 或 invite_token")
    return form


def bind_credential(form, credential_path=None):
    if form['mode'] == 'directed':
        if credential_path:
            raise FillError('CREDENTIAL_UNEXPECTED: 定向邀请不需要额外凭据')
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
    mode_label = "directed 定向邀请" if info["mode"] == "directed" else "group 群组模式（需要个人凭据）"
    print(f"需求格式文件：{info['format']} · {mode_label}")
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
    from rapidocr_openvino import RapidOCR

    engine = RapidOCR()
    result, _stages = engine(str(image_path))
    return [text for _box, text, _score, *_ in (result or [])]


def scan_idcard(image: str | Path) -> dict[str, Any]:
    """对显式指定的这一张证件图做本地 OCR 并提取候选；输出一律遮罩。"""
    path = secure_io.checked_path(image)
    if not path.is_file():
        raise FillError(f"图片不存在: {image}")
    try:
        texts = _ocr_texts(path)
    except ImportError as exc:
        raise FillError(
            "LOCAL_OCR_UNAVAILABLE: rapidocr-openvino 为可选依赖，可执行 pip install -r requirements-ocr.txt；也可使用本人授权的宿主 Agent 识别或手工填写，无需 API Key"
        ) from exc
    extracted = fill_extract.extract_fields("\n".join(texts), source=path.name)
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
    labels = {field["id"]: field["label"] for field in form["fields"]}
    for key in values:
        if key not in labels and not (form["mode"] == "group" and key == "employee_id"):
            problems.append(f"未知字段: {key}（需求格式文件中不存在）")
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


def seal_data(form, values, attachments, out_path, *, confirmed=False, agent_ocr=None, agent_confirmed=False):
    """No files/passwords/terminal I/O: consume the exact in-memory data approved by the caller."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if confirmed is not True:
        raise FillError("CONSENT_REQUIRED: 必须先由本人确认本次字段及附件")
    if collection.task_expired(form):
        raise FillError("TASK_EXPIRED: 已超过保存期限，请向 HR 索取新模板")
    if form["_key_fingerprint"] != form["key_id"]:
        raise FillError("KEY_MISMATCH: 公钥指纹不一致")
    if not form.get("_token"):
        raise FillError("CREDENTIAL_REQUIRED: 缺少个人认证凭据")
    normalized, problems = _check_values(form, values)
    if problems:
        raise FillError("VALUES_INVALID: " + "; ".join(problems))
    invite_id = form["invite_id"]

    payload = {
        "format_version": form.get("format_version", collection.FORMAT_VERSION),
        "task_id": form["task_id"],
        "invite_id": invite_id,
        "invite_token": form["_token"],
        "schema_hash": form["schema_hash"],
        "notice_hash": form["notice_hash"],
        "template_version": str(form.get("template_version", "1.0")),
        "submitted_at": collection.now_iso(),
        "consent_confirmed": True,
        "values": normalized,
        "attachments": attachments,
    }
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
    invite = {"name": form.get("name", normalized.get("name", "")), "employee_id": form.get("_employee_id", ""),
              "token_hash": collection.sha256_bytes(form["_token"].encode())}
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


def cmd_vault(args):
    collection.require_tty(args.command)
    import vault
    if args.command in {"vault-init", "vault-edit"}:
        return vault.edit_interactive(args.form, args.vault, create=args.command == "vault-init")
    return vault.fill_interactive(args.form, args.vault, args.out,
                                  credential_path=args.credential, mapping_path=args.mapping, agent_ocr_path=getattr(args, 'agent_ocr', None))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SafeFill · 填写端：本机填写需求格式文件并产出 .yintian 密文")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("inspect", help="查看需求格式文件的告知内容、字段清单与公钥指纹（不修改文件）")
    p.add_argument("form", metavar="FORM.yintian-form")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_inspect)
    p = sub.add_parser("scan-idcard", help="本地 OCR 扫描一张证件图，遮罩输出候选（可选依赖）")
    p.add_argument("image", metavar="IMAGE")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_scan_idcard)
    p = sub.add_parser("seal", help="校验 values.json 并加密产出 .yintian 提交文件")
    p.add_argument("form", metavar="FORM.yintian-form")
    p.add_argument("--credential", help="HR 私下发放的个人凭据（群发必需）")
    p.add_argument("--agent-ocr", help="本人已授权宿主 Agent 生成的附件候选 JSON；不调用 API")
    p.add_argument("--values", required=True, help="填写值 JSON：{\"values\": {...}, \"attachments\": {...}}")
    p.add_argument("--out", required=True, help="输出 .yintian 路径")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_seal)
    for command in ('vault-init', 'vault-edit'):
        p = sub.add_parser(command, help='本人终端创建或更新加密个人保险柜')
        p.add_argument('form')
        p.add_argument('--vault', required=True)
        p.set_defaults(func=cmd_vault)
    p = sub.add_parser('fill', help='本人解锁保险柜，匹配模板并确认后加密')
    p.add_argument('form')
    p.add_argument('--vault', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--credential')
    p.add_argument('--agent-ocr', help='本人已授权的宿主 Agent 附件候选 JSON')
    p.add_argument('--mapping', help='仅含模板字段 id 与保险柜字段 id 的映射 JSON')
    p.set_defaults(func=cmd_vault)
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
