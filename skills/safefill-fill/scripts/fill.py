"""SafeFill · 填写端：读取 Agent 请求包，用本机保险柜取值后产出 .yintian 密文。

只在员工自己的电脑上运行：业务操作纯本地、不联网；仅显式 vlm-setup 安装模型时联网；只读显式指定的文件；
请求包使用 yintian-request/1，提交使用 yintian-submission/5。
保险柜静态加密存于系统用户数据目录，本机密钥存于独立用户密钥目录；
员工可在私有对话补录，或用本地文本提取并自行核对；收集端脚本解密导出，不向 Agent 返回资料值。
"""
from __future__ import annotations

import argparse
import base64
import copy
import json
import os
import re
import secrets
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any

OPEN_REQUEST_FORMAT = "yintian-request/1"
IMAGE_MIME_BY_SUFFIX = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
PDF_SUFFIX = ".pdf"
PDF_MIME = "application/pdf"
NOTICE_KEYS = ("title", "purpose", "deadline", "retention_until", "contact", "correction")
REQUIRED_REQUEST_KEYS = ("task_id", "format_version", "template_version", "fields", "public_key_pem", "key_id", "schema_hash", "notice_hash") + NOTICE_KEYS


class FillError(Exception):
    """填写端可预期错误：信息面向员工，命令以非零退出。"""


def _bootstrap_repo_scripts() -> None:
    scripts = Path(__file__).resolve().parent
    if not all((scripts / name).is_file() for name in ('collection.py', 'ocr_matcher.py', 'secure_io.py', 'vault.py')):
        raise RuntimeError("INSTALL_INCOMPLETE: 填写者 Skill 安装不完整，请重新复制整个 safefill-fill 文件夹")
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))


_bootstrap_repo_scripts()
import collection  # noqa: E402
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
    """读取机器请求包并做完整性校验；不修改文件。"""
    path = secure_io.checked_path(form_path)
    if not path.is_file():
        raise FillError(f"REQUEST_MISSING: 信息请求包不存在: {form_path}")
    try:
        request_bytes = secure_io.read_bytes(path, 1024 * 1024)
        form = json.loads(request_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FillError("REQUEST_INVALID: 信息请求包不是有效 JSON") from exc
    if not isinstance(form, dict) or form.get("format") != OPEN_REQUEST_FORMAT:
        raise FillError("REQUEST_INVALID: 不是受支持的 SafeFill 机器请求包")
    if form.get("kind") != "agent_request" or form.get("target_skill") != "safefill-fill":
        raise FillError("OPEN_REQUEST_INVALID: 不是发给 safefill-fill 的 Agent 请求包")
    if form.get("format_version") != collection.SUBMISSION_FORMAT_VERSION:
        raise FillError(f"OPEN_REQUEST_INVALID: 请求包要求的提交版本不受支持（本端支持 {collection.SUBMISSION_FORMAT_VERSION}）；"
                        "请升级 safefill-fill 或向发放方索取兼容请求包")
    if any(key in form for key in ("invite_id", "invite_token", "token", "name", "employee_id")):
        raise FillError("OPEN_REQUEST_INVALID: 请求包不应包含个人身份或邀请信息")
    missing = [key for key in REQUIRED_REQUEST_KEYS if not form.get(key)]
    if missing:
        raise FillError(f"REQUEST_INVALID: 信息请求包缺少必要字段: {', '.join(missing)}")
    for key in (*NOTICE_KEYS, "template_version"):
        if not isinstance(form[key], str) or len(form[key]) > collection.MAX_VALUE_CHARS:
            raise FillError(f"FORM_INVALID: {key} 必须是长度受限的文本")
    if not collection.TASK_ID_RE.fullmatch(str(form["task_id"])):
        raise FillError("REQUEST_INVALID: 信息请求包 task_id 无效")
    if not isinstance(form["public_key_pem"], str):
        raise FillError("FORM_INVALID: public_key_pem 必须是文本")
    if not re.fullmatch(r"[0-9a-f]{24}", str(form["key_id"])) or any(not re.fullmatch(r"[0-9a-f]{64}", str(form[key])) for key in ("schema_hash", "notice_hash")):
        raise FillError("FORM_INVALID: 公钥或摘要标识格式无效")
    fields = form["fields"]
    try:
        if collection.validate_field_definitions(fields) != fields:
            raise ValueError("字段定义未规范化")
    except ValueError as exc:
        raise FillError(f"FORM_INVALID: {exc}") from exc
    if collection.sha256_bytes(collection.canonical(fields)) != form["schema_hash"]:
        raise FillError("REQUEST_TAMPERED: 字段清单与 schema_hash 不匹配，文件可能被篡改，请向发放人重新索取")
    notice = {key: form[key] for key in NOTICE_KEYS}
    if collection.sha256_bytes(collection.canonical(notice)) != form["notice_hash"]:
        raise FillError("REQUEST_TAMPERED: 告知内容与 notice_hash 不匹配，文件可能被篡改，请向发放人重新索取")
    try:
        fingerprint = _public_key_fingerprint(form["public_key_pem"])
    except Exception as exc:
        raise FillError("FORM_INVALID: 公钥格式无效") from exc
    form["_key_fingerprint"] = fingerprint
    form["_path"] = str(path)
    form["_request_sha256"] = collection.sha256_bytes(request_bytes)
    return form


def inspect_info(form: dict[str, Any]) -> dict[str, Any]:
    info = {
        "task_id": form["task_id"],
        "key_fingerprint": form["_key_fingerprint"],
        "key_id_match": form["_key_fingerprint"] == form["key_id"],
        **{key: form[key] for key in NOTICE_KEYS},
        "fields": [
            {
                "id": field["id"],
                "label": field["label"],
                "type": field["type"],
                "required": bool(field.get("required")),
                "sensitive": bool(field.get("sensitive")),
                **({"notes": field["notes"]} if "notes" in field else {}),
                **({"ocr_fields": field["ocr_fields"]} if field.get("ocr_fields") else {}),
                **({"options": field["options"]} if field.get("type") == "single_choice" and field.get("options") else {}),
                **({"multiple": bool(field.get("multiple"))} if field["type"] in collection.ATTACHMENT_TYPES else {}),
            }
            for field in form["fields"]
        ],
        "past_deadline": collection.task_late(form, collection.now_iso()),
        "expired": collection.task_expired(form),
    }
    if not info["key_id_match"]:
        info["stop_reason"] = "KEY_MISMATCH: 请求包内公钥与 key_id 不一致，文件可能被替换，请勿填写并联系发放人"
    elif info["expired"]:
        info["stop_reason"] = f"TASK_EXPIRED: 已超过保存期限 {form['retention_until']}，收集方不再接收，请按 contact 索取新请求包"
    elif info["past_deadline"]:
        info["deadline_notice"] = f"已过截止时间 {form['deadline']}，仍可提交但会被标记为迟交；建议先与 {form['contact']} 确认是否继续"
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
            "LOCAL_OCR_UNAVAILABLE: rapidocr-openvino 为可选依赖，可安装 requirements-ocr.txt，或在员工私有会话中补录"
        ) from exc
    return fill_extract.extract_fields("\n".join(texts), source=path.name)


def _resolve_attachment(raw: Any) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise FillError("ATTACHMENT_INVALID: 附件路径无效（answers 的附件值应为文件路径字符串或字符串数组）")
    if ".." in PurePosixPath(raw.replace("\\", "/")).parts:
        raise FillError(f"ATTACHMENT_INVALID: 附件路径不允许包含 ..: {raw}")
    path = secure_io.checked_path(raw)
    if not path.is_file():
        raise FillError(f"ATTACHMENT_MISSING: 附件文件不存在: {raw}")
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
        raise FillError("VALUES_INVALID: values 必须是 {字段id: 值} 对象")
    if len(values) > collection.MAX_FIELDS:
        raise FillError("VALUES_INVALID: values 字段数量超过安全上限")
    labels = {field["id"]: field["label"] for field in form["fields"]}
    allowed_ids = {field["id"] for field in form["fields"] if field["type"] not in collection.ATTACHMENT_TYPES}
    unknown_ids = set(values) - allowed_ids
    problems.extend(f"未知字段: {key}（需求格式文件中不存在）" for key in sorted(unknown_ids))
    try:
        collection.validate_scalar_values({key: value for key, value in values.items() if key in allowed_ids}, allowed_ids)
    except ValueError as exc:
        raise FillError(f"VALUES_INVALID: {exc}") from exc
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
    return normalized, problems


def _build_attachments(form: dict[str, Any], specs: Any) -> tuple[dict[str, list], list[str]]:
    problems: list[str] = []
    if not isinstance(specs, dict):
        raise FillError("ATTACHMENTS_INVALID: attachments 必须是 {字段id: 文件路径} 对象")
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
            raise FillError(f"ATTACHMENTS_INVALID: 附件字段取值应为文件路径或路径数组: {field_id}")
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


def reply_filename(invite_id: str, revision: int) -> str:
    return f"RECEIPT-{invite_id}-{revision}-{collection.random_id('', 6)}.yintian"


def seal_data(form, values, attachments, out_path, *, signing_key, revision):
    """Consume the exact in-memory data approved by the caller via a confirmation file."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if collection.task_expired(form):
        raise FillError("TASK_EXPIRED: 已超过保存期限，请向 HR 索取新请求包")
    if form["_key_fingerprint"] != form["key_id"]:
        raise FillError("KEY_MISMATCH: 公钥指纹不一致")
    normalized, problems = _check_values(form, values)
    if problems:
        raise FillError("VALUES_INVALID: " + "; ".join(problems))
    public_b64 = base64.b64encode(signing_key.public_key().public_bytes_raw()).decode("ascii")
    invite_id = collection.derive_invite_id(form["task_id"], public_b64)

    payload = {
        "format_version": form["format_version"],
        "task_id": form["task_id"],
        "invite_id": invite_id,
        "revision": revision,
        "schema_hash": form["schema_hash"],
        "notice_hash": form["notice_hash"],
        "template_version": str(form.get("template_version", "1.0")),
        "submitted_at": collection.now_iso(),
        "consent_confirmed": True,
        "values": normalized,
        "attachments": attachments,
    }
    missing, conflicts, _ = collection.validate_payload(form, payload)
    if missing or conflicts:
        raise FillError("PAYLOAD_INVALID: " + ",".join(missing + conflicts))
    aes_key = AESGCM.generate_key(bit_length=256)
    iv = secrets.token_bytes(12)
    header = {key: payload[key] for key in ("format_version", "task_id", "invite_id", "schema_hash", "revision")}
    header.update(key_id=form["key_id"], sender_public_key_b64=public_b64)
    aad = collection.aad_for(header)
    plaintext = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ciphertext = AESGCM(aes_key).encrypt(iv, plaintext, aad)
    public_key = serialization.load_pem_public_key(str(form["public_key_pem"]).encode("utf-8"))
    wrapped = public_key.encrypt(aes_key, padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    envelope = {
        **header,
        "algorithms": {"content": "AES-256-GCM", "key_wrap": "RSA-OAEP-3072-SHA256"},
        "encrypted_key_b64": base64.b64encode(wrapped).decode("ascii"),
        "iv_b64": base64.b64encode(iv).decode("ascii"),
        "ciphertext_b64": base64.b64encode(ciphertext).decode("ascii"),
    }
    envelope = collection.sign_envelope(envelope, signing_key)
    blob = collection.canonical(envelope)
    if len(blob) > collection.MAX_ENVELOPE_BYTES:
        raise FillError("FILE_LIMIT: 加密信封超过 32MB 上限，未生成文件")
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
        "revision": revision,
        "fields": len(normalized),
        "attachments": sum(len(items) for items in attachments.values()),
        "bytes": len(blob),
    }


def _load_notice(path: Path) -> dict[str, Any] | None:
    """识别 yintian-notice/1 退回/补正通知（不可信数据，只做提示解析）；非通知文件返回 None。"""
    try:
        data = json.loads(secure_io.read_bytes(path, 1024 * 1024))
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("format") != collection.NOTICE_FORMAT_VERSION:
        return None
    allowed = {"format", "task_id", "invite_id", "status", "late", "reasons", "next_action", "contact", "generated_at",
               "revision", "receipt_sha256"}
    if set(data) - allowed:
        raise FillError("NOTICE_INVALID: 通知包含协议外字段")
    if any(not isinstance(data.get(key), str) or not data[key] for key in ("task_id", "invite_id", "status", "next_action")):
        raise FillError("NOTICE_INVALID: 通知缺少必要字段")
    if not collection.TASK_ID_RE.fullmatch(data["task_id"]) or not collection.OPEN_INVITE_ID_RE.fullmatch(data["invite_id"]):
        raise FillError("NOTICE_INVALID: 通知中的任务或回执编号格式无效")
    reasons = data["reasons"]
    if not isinstance(reasons, list) or len(reasons) > 64 or any(not isinstance(item, str) or len(item) > 256 for item in reasons):
        raise FillError("NOTICE_INVALID: 通知原因列表无效")
    for key in ("status", "next_action", "contact", "generated_at"):
        if key in data and (not isinstance(data[key], str) or len(data[key]) > collection.MAX_VALUE_CHARS):
            raise FillError("NOTICE_INVALID: 通知字段无效")
    if "revision" in data and (type(data["revision"]) is not int or not 1 <= data["revision"] <= collection.MAX_REVISION):
        raise FillError("NOTICE_INVALID: 通知更正序号无效")
    if "receipt_sha256" in data and (not isinstance(data["receipt_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", data["receipt_sha256"])):
        raise FillError("NOTICE_INVALID: 通知回执摘要无效")
    return {"kind": "notice", "task_id": data["task_id"], "invite_id": data["invite_id"],
            "status": data["status"], "late": bool(data.get("late")), "reasons": reasons,
            "next_action": data["next_action"], "contact": data.get("contact", "")}


def cmd_inspect(args) -> dict[str, Any]:
    path = secure_io.checked_path(args.form)
    notice = _load_notice(path)
    if notice is not None:
        return notice
    return inspect_info(load_form(path))


def _previous_envelope(form, previous):
    try:
        envelope = json.loads(secure_io.read_bytes(previous, collection.MAX_ENVELOPE_BYTES))
        collection.validate_envelope_header(envelope, form)
        return envelope
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise FillError("PREVIOUS_INVALID: 旧回执不属于本请求包或已损坏") from exc


def _resolve_previous(args, vault_path: Path, task_id: str, invite_id: str) -> tuple[Any, str]:
    """--previous 解析顺序：显式参数 > 提交登记中该任务最新回执 > 无；--fresh 强制不用旧回执。"""
    if getattr(args, "previous", None):
        return secure_io.checked_path(args.previous), "explicit"
    if getattr(args, "fresh", False):
        return None, "none"
    record = vault.find_previous(vault_path.parent, task_id, invite_id)
    if record is not None:
        return secure_io.checked_path(record["path"]), "registry"
    return None, "none"


def _submission_identity(args, profile, form, vault_path, *, confirmed_id=None):
    """Select only an identity whose private key is in this vault; the registry is a hint."""
    task_id = form["task_id"]
    state = profile.get("submission_identities", {}).get(task_id)
    explicit = getattr(args, "previous", None)
    previous = _previous_envelope(form, explicit) if explicit else None
    fresh = bool(getattr(args, "fresh", False))
    if explicit and fresh:
        raise FillError("PREVIOUS_INVALID: --previous 与 --fresh 不能同时使用")
    if previous is not None:
        invite_id = previous["invite_id"]
        if state is None or invite_id not in state["identities"]:
            raise FillError("PREVIOUS_INVALID: 本机保险柜不持有该回执的签名私钥")
    elif confirmed_id is not None:
        invite_id = confirmed_id
        if state is None or invite_id not in state["identities"] or (not fresh and state["current"] != invite_id):
            raise FillError("CONFIRMATION_STALE: 提交身份已变化，请重新确认")
    elif fresh or state is None:
        invite_id = vault.create_identity(profile, task_id)
    else:
        invite_id = state["current"]
    if confirmed_id is not None and invite_id != confirmed_id:
        raise FillError("CONFIRMATION_STALE: 提交身份已变化，请重新确认")
    state = profile["submission_identities"][task_id]
    identity = state["identities"][invite_id]
    previous_path, previous_source = _resolve_previous(args, vault_path, task_id, invite_id)
    if previous_path is not None and previous is None:
        try:
            previous = _previous_envelope(form, previous_path)
            if previous["invite_id"] != invite_id:
                previous = None
        except FillError:
            previous = None
        if previous is None:
            previous_path, previous_source = None, "none"
    revision = max(identity["last_reserved_revision"], previous["revision"] if previous else 0) + 1
    if revision > collection.MAX_REVISION:
        raise FillError("REVISION_LIMIT: 本任务提交序号已达上限")
    return {"invite_id": invite_id, "revision": revision, "fresh": fresh,
            "explicit_previous": str(secure_io.checked_path(explicit)) if explicit else None,
            "previous": str(previous_path) if previous_path is not None else None,
            "previous_source": previous_source,
            "previous_sha256": collection.sha256_bytes(collection.canonical(previous)) if previous else None}


def _clear_stale_confirmation(path: Path, key: str) -> None:
    """同名确认文件已过期或损坏时清除重写；仍有效的文件保留并照常报 CONFIRMATION_EXISTS。"""
    if not path.is_file():
        return
    valid = False
    for operation in ("vault-change", "submission"):
        try:
            vault.open_confirmation(path, key, operation)
            valid = True
            break
        except Exception:
            pass
    if valid:
        return
    try:
        path.unlink()
    except OSError:
        pass


def _storage_paths(args) -> tuple[Path, Path]:
    vault_override = getattr(args, "vault", None)
    key_override = getattr(args, "key_file", None)
    if bool(vault_override) != bool(key_override):
        raise FillError("VAULT_STORAGE_INVALID: 自定义 --vault 必须同时提供 --key-file")
    if vault_override:
        vault_path, key_path = secure_io.checked_path(vault_override), secure_io.checked_path(key_override)
        if vault_path == key_path:
            raise FillError("VAULT_STORAGE_INVALID: 保险柜与密钥路径必须不同")
        return vault_path, key_path
    try:
        return vault.default_vault_path(), vault.default_key_path()
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


def _read_stdin_answers() -> dict[str, Any]:
    """从标准输入读取临时 JSON；明文不落盘。"""
    raw = sys.stdin.buffer.read(collection.MAX_ENVELOPE_BYTES + 1)
    if len(raw) > collection.MAX_ENVELOPE_BYTES:
        raise FillError("VAULT_ANSWERS_INVALID: 标准输入超过大小上限")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FillError("VAULT_ANSWERS_INVALID: 标准输入不是有效 JSON") from exc
    if not isinstance(data, dict):
        raise FillError("VAULT_ANSWERS_INVALID: 临时 JSON 必须是对象")
    return data


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
    vault_path, key_path = _storage_paths(args)
    try:
        key = vault.load_or_create_key(key_path, create=create_key)
    except (RuntimeError, ValueError) as exc:
        raise FillError(str(exc)) from exc
    return vault_path, key_path, key


def cmd_vault_status(args) -> dict[str, Any]:
    form = load_form(args.request) if getattr(args, "request", None) else None
    vault_path, key_path = _storage_paths(args)
    if not vault_path.is_file():
        result: dict[str, Any] = {"vault": False, "vault_path": str(vault_path), "key_path": str(key_path),
                                  "wiki_path": str(vault_path.parent / "wiki.md")}
        if form is not None:
            result["fields"] = [
                {"id": field["id"], "label": field["label"], "type": field["type"],
                 "required": bool(field.get("required")), "match": None, "status": "missing"}
                for field in form["fields"]
            ]
        return result
    try:
        vault.check_vault_file(vault_path)
    except ValueError as exc:
        raise FillError(str(exc)) from exc
    vault_path, key_path, key = _unlock(args)
    try:
        profile = vault.load_vault(vault_path, key)
    except ValueError as exc:
        raise FillError(str(exc)) from exc
    result = vault.status_view(profile, form)
    result.update(vault_path=str(vault_path), key_path=str(key_path), wiki_path=str(vault_path.parent / "wiki.md"))
    if form is not None and result.get("missing"):
        required = {field["id"] for field in form["fields"] if field.get("required")}
        result["same_type_entries"] = _same_type_entries(form, profile, [item for item in result["missing"] if item in required])
    return result


def _entry_preview(entry: dict[str, Any] | None) -> Any:
    if entry is None:
        return None
    if entry["type"] in collection.ATTACHMENT_TYPES:
        return [{"name": item["name"], "size": item["size"], "sha256": item["sha256"]}
                for item in entry.get("attachments", [])]
    return entry.get("value", "")


def _private_entry(entry) -> bool:
    return bool(entry and entry["source"]["kind"] == "openvino-text")


def _seal_with_local_review(path, key, operation, payload, content=None):
    """Keep plaintext review out of tool output; bind the file to the encrypted approval."""
    review = None
    if content is not None:
        review = path.parent / f"REVIEW-{secrets.token_hex(12)}.txt"
        raw = ("本机核对文件：含明文资料，请自行查看，不要发给 Agent。\n"
               "如需更正，请修改原资料后重新暂存/预览；不要修改本核对文件。\n\n"
               + json.dumps(content, ensure_ascii=False, indent=2)).encode("utf-8")
        secure_io.atomic_write(review, raw, overwrite=False)
        payload = {**payload, "local_review": {"path": str(review), "sha256": collection.sha256_bytes(raw)}}
    try:
        result = vault.seal_confirmation(path, key, operation, payload)
    except Exception:
        if review is not None:
            review.unlink(missing_ok=True)
        raise
    if review is not None:
        result.update(local_only=True, review=str(review))
    return result


def _verify_private_files(approved):
    for name in ("local_review", "text_source"):
        binding = approved.get(name)
        if binding is None:
            continue
        try:
            if vault.file_digest(secure_io.checked_path(binding["path"])) != binding["sha256"]:
                raise ValueError
        except Exception:
            raise FillError("CONFIRMATION_STALE: 本机核对文件或原文本已变化，请重新暂存/预览") from None


def _cleanup_local_review(approved):
    binding = approved.get("local_review")
    if binding is not None:
        try:
            path = secure_io.checked_path(binding["path"])
            if vault.file_digest(path) != binding["sha256"]:
                raise ValueError
            path.unlink()
        except Exception:
            return {"review_cleanup_required": binding["path"]}
    return {}


def _text_entries(args, path):
    if not getattr(args, "request", None) or not getattr(args, "model", None):
        raise FillError("TEXT_INPUT_INVALID: 文本提取须提供 --request 和 --model")
    form = load_form(args.request)
    if collection.task_expired(form):
        raise FillError("TASK_EXPIRED: 请求包已过期")
    if form["_key_fingerprint"] != form["key_id"]:
        raise FillError("KEY_MISMATCH: 公钥指纹不一致")
    import vlm_extract

    try:
        if path.suffix.lower() != ".txt":
            raise ValueError
        raw = secure_io.read_bytes(path, vlm_extract.MAX_TEXT_BYTES)
        text = raw.decode("utf-8-sig")
        if not text.strip() or "\0" in text:
            raise ValueError
    except (ValueError, OSError):
        raise FillError("TEXT_INPUT_INVALID: 请提供可读取的 UTF-8 文本文件，最大 16 KiB") from None
    try:
        values = vlm_extract.extract_text_fields(text, form["fields"], args.model, getattr(args, "revision", None))
    except vlm_extract.VlmUnavailable as exc:
        raise FillError(str(exc)) from None
    fields = [field for field in form["fields"] if field["id"] in values]
    try:
        values, problems = _check_values({"fields": fields}, values)
    except FillError:
        raise FillError("TEXT_VALUES_INVALID: 提取值未通过字段校验，请在原文本中修正后重试") from None
    if problems:
        raise FillError("TEXT_VALUES_INVALID: 提取值未通过字段校验，请在原文本中修正后重试")
    source = {"kind": "openvino-text", "sha256": collection.sha256_bytes(raw)}
    entries = _build_entries({field["id"]: {"type": field["type"], "label": field["label"],
                              "value": values[field["id"]], "source": source} for field in fields})
    return entries, {"path": str(path), "sha256": source["sha256"]}


def cmd_vault_stage(args) -> dict[str, Any]:
    text_file = getattr(args, "text_file", None)
    text_path = secure_io.checked_path(text_file) if text_file else None
    answers_path = None if text_path is not None or args.answers == "-" else secure_io.checked_path(args.answers)
    vault_path, key_path = _storage_paths(args)
    confirmation_path = secure_io.checked_path(args.confirmation_out)
    request_path = secure_io.checked_path(args.request) if getattr(args, "request", None) else None
    if text_path is not None and (text_path in {vault_path, key_path, confirmation_path, request_path}
                                  or request_path in {vault_path, key_path, confirmation_path}):
        raise FillError("VAULT_PATH_COLLISION: 原文本、请求包、确认文件、保险柜和密钥路径必须分开")
    if answers_path is not None and answers_path in {vault_path, key_path, confirmation_path}:
        raise FillError("VAULT_PATH_COLLISION: 临时文件、确认文件、保险柜和密钥路径必须分开")
    if confirmation_path in {vault_path, key_path}:
        if answers_path is not None and answers_path.is_file():
            answers_path.unlink()
        raise FillError("VAULT_PATH_COLLISION: 临时文件、确认文件、保险柜和密钥路径必须分开")
    text_source = None
    if text_path is not None:
        entries, text_source = _text_entries(args, text_path)
    else:
        if any(getattr(args, name, None) for name in ("request", "model", "revision")):
            raise FillError("TEXT_INPUT_INVALID: --request/--model/--revision 仅用于 --text-file")
        answers = _read_stdin_answers() if answers_path is None else _read_temp_answers(str(answers_path))
        entries = _build_entries(_entry_specs(answers))
    try:
        vault.ensure_private_dir(vault_path.parent)
        vault.ensure_private_dir(key_path.parent)
        vault.ensure_private_dir(confirmation_path.parent)
    except (RuntimeError, ValueError) as exc:
        raise FillError(str(exc)) from exc
    vault_path, key_path, key = _unlock(args, create_key=True)
    with secure_io.file_lock(str(vault_path) + ".lock"):
        try:
            if vault_path.is_file():
                vault.check_vault_file(vault_path)
            profile: dict[str, Any] = (vault.load_vault(vault_path, key) if vault_path.is_file()
                                       else {"format": vault.FORMAT, "entries": {}})
        except (RuntimeError, ValueError) as exc:
            raise FillError(str(exc)) from exc
        candidate = {"format": vault.FORMAT, "entries": {**profile["entries"], **entries}}
        if len(candidate["entries"]) > vault.MAX_ENTRIES:
            raise FillError("VAULT_LIMIT: 保险柜条目超过数量上限")
        prior_digests = {
            entry_id: (collection.sha256_bytes(collection.canonical(profile["entries"][entry_id]))
                       if entry_id in profile["entries"] else None)
            for entry_id in entries
        }
        changes = [
            {"id": entry_id, "type": entry["type"], "action": "update" if entry_id in profile["entries"] else "add",
             "old_value": _entry_preview(profile["entries"].get(entry_id)), "new_value": _entry_preview(entry),
             "source": entry["source"]["kind"]}
            for entry_id, entry in entries.items()
        ]
        local_only = any(_private_entry(entry) or _private_entry(profile["entries"].get(entry_id))
                         for entry_id, entry in entries.items())
        try:
            vault.validate_profile(candidate)
            _clear_stale_confirmation(confirmation_path, key)
            confirmation = _seal_with_local_review(
                confirmation_path, key, "vault-change",
                {"vault_path": str(vault_path), "key_path": str(key_path),
                 "prior_digests": prior_digests,
                 "entries": entries, **({"text_source": text_source} if text_source else {})},
                {"changes": changes} if local_only else None)
        except FileExistsError as exc:
            raise FillError(f"CONFIRMATION_EXISTS: 确认文件已存在: {confirmation_path}；"
                            "若上次操作已放弃，请删除该文件后重试，或更换 --confirmation-out 路径") from exc
        except (RuntimeError, ValueError) as exc:
            raise FillError(str(exc)) from exc
    if local_only:
        changes = [{key: value for key, value in change.items() if key not in {"old_value", "new_value"}}
                   for change in changes]
    return {"vault_path": str(vault_path), "changes": changes, **confirmation}


def cmd_vault_apply(args) -> dict[str, Any]:
    confirmation_path = secure_io.checked_path(args.confirmation)
    vault_path, key_path, key = _unlock(args)
    if confirmation_path in {vault_path, key_path}:
        raise FillError("VAULT_PATH_COLLISION: 确认文件、保险柜和密钥路径必须分开")
    verified = False
    try:
        staged = vault.open_confirmation(confirmation_path, key, "vault-change")
        verified = True
        if staged.get("vault_path") != str(vault_path) or staged.get("key_path") != str(key_path):
            raise FillError("CONFIRMATION_STALE: 确认文件不属于当前保险柜或密钥")
        _verify_private_files(staged)
        entries = {entry_id: vault.validate_entry(entry) for entry_id, entry in staged.get("entries", {}).items()}
        with secure_io.file_lock(str(vault_path) + ".lock"):
            old_blob = secure_io.read_bytes(vault_path, vault.MAX_BYTES) if vault_path.is_file() else None
            profile = (vault.load_vault(vault_path, key) if vault_path.is_file()
                       else {"format": vault.FORMAT, "entries": {}})
            prior_digests = staged.get("prior_digests")
            if isinstance(prior_digests, dict):
                stale = []
                for entry_id in entries:
                    existing = profile["entries"].get(entry_id)
                    current = (collection.sha256_bytes(collection.canonical(existing))
                               if existing is not None else None)
                    if current != prior_digests.get(entry_id):
                        stale.append(entry_id)
                if stale:
                    raise FillError("CONFIRMATION_STALE: 保险柜条目已变化，请重新预览: " + ", ".join(sorted(stale)))
            elif (vault.file_digest(vault_path) if vault_path.is_file() else None) != staged.get("base_vault_sha256"):
                raise FillError("CONFIRMATION_STALE: 保险柜已变化，请重新预览")
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
            "entry_count": len(profile["entries"]), **_cleanup_local_review(staged)}


def cmd_vault_scan(args) -> dict[str, Any]:
    scans = []
    for image in args.images:
        path = secure_io.checked_path(image)
        if not path.is_file():
            raise FillError(f"IMAGE_MISSING: 图片不存在: {image}")
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
    return {"scans": scans}


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
    """Return missing fields to preview and fill; neither may submit while required fields are missing."""
    try:
        values, attachments, missing, matches = vault.select_fields(form, profile, mapping)
    except ValueError as exc:
        raise FillError(str(exc)) from exc
    required_missing = [field["id"] for field in form["fields"] if field.get("required") and field["id"] in missing]
    attachment_problems = _check_vault_attachments(form, attachments)
    if attachment_problems and not required_missing:
        raise FillError("ATTACHMENTS_INVALID: " + "; ".join(attachment_problems))
    return values, attachments, missing, matches, required_missing


def _field_preview(form, profile, values, attachments, matches):
    preview = []
    for field in form["fields"]:
        field_id = field["id"]
        if field_id in attachments:
            content = [{"name": item["name"], "size": item["size"], "sha256": item["sha256"]}
                       for item in attachments[field_id]]
        else:
            content = values.get(field_id)
        preview.append({"id": field_id, "label": field["label"], "type": field["type"], "required": bool(field.get("required")),
                        "value": content, "source_entry": matches.get(field_id),
                        "source": profile["entries"][matches[field_id]]["source"]["kind"] if field_id in matches else None})
    return preview


def _submission_snapshot(args, profile, form, vault_path, key_path, mapping, selected, *, confirmed_id=None):
    values, attachments, _missing, matches, _required = selected
    identity = _submission_identity(args, profile, form, vault_path, confirmed_id=confirmed_id)
    selection = {"values": values, "attachments": attachments, "matches": matches,
                 "sources": {entry_id: profile["entries"][entry_id]["source"] for entry_id in matches.values()}}
    snapshot = {"request_sha256": form["_request_sha256"], "vault_path": str(vault_path), "key_path": str(key_path),
                "mapping": mapping, "selection_sha256": collection.sha256_bytes(collection.canonical(selection)), **identity}
    return snapshot


def cmd_vault_preview(args) -> dict[str, Any]:
    form = load_form(args.form)
    if collection.task_expired(form):
        raise FillError("TASK_EXPIRED: 请求包已过期，请向 HR 索取新请求包")
    if form["_key_fingerprint"] != form["key_id"]:
        raise FillError("KEY_MISMATCH: 公钥指纹不一致")
    vault_path, key_path, key = _unlock(args)
    if not vault_path.is_file():
        raise FillError("VAULT_MISSING: 保险柜不存在，请先经本人确认后 vault-stage/vault-apply")
    mapping = _mapping(getattr(args, "mapping", None))
    confirmation_path = secure_io.checked_path(args.confirmation_out)
    inputs = {vault_path, key_path, secure_io.checked_path(args.form)}
    if getattr(args, "mapping", None):
        inputs.add(secure_io.checked_path(args.mapping))
    if getattr(args, "previous", None):
        inputs.add(secure_io.checked_path(args.previous))
    if confirmation_path in inputs:
        raise FillError("VAULT_PATH_COLLISION: 确认文件、保险柜和密钥路径必须分开")
    vault.ensure_private_dir(confirmation_path.parent)
    with secure_io.file_lock(str(vault_path) + ".lock"):
        profile = vault.load_vault(vault_path, key)
        selected = _prepare_vault_selection(form, profile, mapping)
        values, attachments, missing, matches, required = selected
        fields = _field_preview(form, profile, values, attachments, matches)
        local_only = any(_private_entry(profile["entries"][entry_id]) for entry_id in matches.values())
        visible_fields = [{key: value for key, value in field.items() if key != "value"}
                          for field in fields] if local_only else fields
        if required:
            labels = {field["id"]: field["label"] for field in form["fields"]}
            return {"ready": False, "fields": visible_fields, "mapping": mapping,
                    **({"local_only": True} if local_only else {}),
                    "required_missing": [{"id": item, "label": labels[item]} for item in required],
                    "optional_missing": [item for item in missing if item not in required],
                    "same_type_entries": _same_type_entries(form, profile, required)}
        _clear_stale_confirmation(confirmation_path, key)
        if confirmation_path.exists():
            raise FillError(f"CONFIRMATION_EXISTS: 确认文件已存在: {confirmation_path}；删除该文件或更换 --confirmation-out 路径")
        identities_before = copy.deepcopy(profile.get("submission_identities"))
        snapshot = _submission_snapshot(args, profile, form, vault_path, key_path, mapping, selected)
        if profile.get("submission_identities") != identities_before:
            vault.save_vault(vault_path, key, profile)
        try:
            confirmation = _seal_with_local_review(
                confirmation_path, key, "submission", snapshot,
                {"task_id": form["task_id"], "fields": fields, "mapping": mapping,
                 "invite_id": snapshot["invite_id"], "revision": snapshot["revision"]} if local_only else None)
        except FileExistsError as exc:
            raise FillError("CONFIRMATION_EXISTS: 确认文件已存在，请更换路径") from exc
    result = {"ready": True, "fields": visible_fields, "mapping": mapping,
              "optional_missing": missing, "invite_id": snapshot["invite_id"], "revision": snapshot["revision"], **confirmation}
    if snapshot["previous"]:
        result.update(previous=snapshot["previous"], previous_source=snapshot["previous_source"])
    return result


def _same_type_entries(form, profile, field_ids):
    """列出与缺失字段类型相同、但 id 不同的保险柜条目，供 Agent 提出显式 mapping；脚本本身不做语义推断。"""
    fields = {field["id"]: field for field in form["fields"]}
    result = {}
    for field_id in field_ids:
        field_type = fields[field_id]["type"]
        candidates = [{"entry": entry_id, "label": entry.get("label", "")}
                      for entry_id, entry in profile["entries"].items()
                      if entry_id != field_id and entry["type"] == field_type and (entry.get("value") or entry.get("attachments"))]
        if candidates:
            result[field_id] = candidates
    return result


def cmd_vault_fill(args) -> dict[str, Any]:
    vault_path, key_path, key = _unlock(args)
    if not vault_path.is_file():
        raise FillError("VAULT_MISSING: 保险柜不存在，请先经本人确认后 vault-stage/vault-apply")
    confirmation_path = secure_io.checked_path(args.confirmation)
    if confirmation_path in {vault_path, key_path}:
        raise FillError("VAULT_PATH_COLLISION: 确认文件、保险柜和密钥路径必须分开")
    with secure_io.file_lock(str(vault_path) + ".lock"):
        try:
            approved = vault.open_confirmation(confirmation_path, key, "submission")
        except (RuntimeError, ValueError) as exc:
            raise FillError(str(exc)) from exc
        try:
            mapping = approved.get("mapping")
            if not isinstance(mapping, dict) or not isinstance(approved.get("invite_id"), str):
                raise FillError("CONFIRMATION_INVALID: 确认文件中的映射或身份无效")
            _verify_private_files(approved)
            form = load_form(args.form)
            if collection.task_expired(form):
                raise FillError("TASK_EXPIRED: 请求包已过期")
            profile = vault.load_vault(vault_path, key)
            selected = _prepare_vault_selection(form, profile, mapping)
            values, attachments, _missing, matches, required = selected
            expected = _submission_snapshot(args, profile, form, vault_path, key_path, mapping, selected,
                                            confirmed_id=approved["invite_id"])
            if required or any(approved.get(name) != value for name, value in expected.items()):
                raise FillError("CONFIRMATION_STALE: 请求、取值、来源或提交身份变化，请重新确认")
        except Exception as exc:
            confirmation_path.unlink(missing_ok=True)
            if isinstance(exc, FillError) and str(exc).startswith(("TASK_EXPIRED", "CONFIRMATION_")):
                raise
            raise FillError("CONFIRMATION_STALE: 请求、保险柜或旧回执变化，请重新确认") from exc
        state = profile["submission_identities"][form["task_id"]]
        identity = state["identities"][approved["invite_id"]]
        identity["last_reserved_revision"] = approved["revision"]
        # A persisted reservation is never rolled back, including failed output or confirmation consumption.
        vault.save_vault(vault_path, key, profile)
        out = args.out or secure_io.checked_path(args.out_dir) / reply_filename(approved["invite_id"], approved["revision"])
        result = seal_data(form, values, attachments, out, signing_key=vault.identity_key(identity), revision=approved["revision"])
        try:
            confirmation_path.unlink()
        except OSError as exc:
            try:
                Path(result["out"]).unlink()
            except OSError as rollback_exc:
                raise FillError("REPLY_ROLLBACK_FAILED: 确认无法消费且回执无法撤回，请停止重试") from rollback_exc
            raise FillError("CONFIRMATION_CONSUME_FAILED: 确认无法消费，回执已撤回") from exc
        # A cancelled fresh preview does not change the default identity; only a completed receipt does.
        if state["current"] != approved["invite_id"]:
            previous_current = state["current"]
            state["current"] = approved["invite_id"]
            try:
                vault.save_vault(vault_path, key, profile)
            except Exception as exc:
                # atomic_write can fail after replacement; restore only the pointer, never the reserved counter.
                state["current"] = previous_current
                try:
                    vault.save_vault(vault_path, key, profile)
                except Exception as rollback_exc:
                    raise FillError("VAULT_ROLLBACK_FAILED: 当前身份恢复失败，已保留回执和保险柜现场，请停止重试") from rollback_exc
                try:
                    Path(result["out"]).unlink()
                except OSError as rollback_exc:
                    raise FillError("REPLY_ROLLBACK_FAILED: 当前身份已恢复但回执无法撤回，请停止重试并保留现场") from rollback_exc
                raise FillError("VAULT_WRITE_FAILED: 当前身份已恢复，回执已撤回，请重新确认") from exc
        result["matched"] = matches
        if approved["previous"]:
            result.update(previous=approved["previous"], previous_source=approved["previous_source"])
        try:
            vault.record_submission(vault_path.parent, {
                "task_id": form["task_id"], "invite_id": result["invite_id"],
                "path": result["out"], "sealed_at": collection.now_iso()})
        except Exception:
            result["registry_warning"] = "提交登记写入失败；签名身份和提交序号已保存"
        if approved["fresh"]:
            earlier = _same_task_receipts(Path(result["out"]), form["task_id"])
            if earlier:
                result["warning"] = f"本次明确新建记录；输出目录另有 {len(earlier)} 份本任务回执，请核对是否需要保留独立记录"
    result.update(_cleanup_local_review(approved))
    return result


def cmd_receipt_inspect(args) -> dict[str, Any]:
    """读取回执信封明文头，帮助员工辨认文件属于哪个任务、对应哪次提交。"""
    path = secure_io.checked_path(args.receipt)
    if not path.is_file():
        raise FillError(f"RECEIPT_MISSING: 回执文件不存在: {args.receipt}")
    try:
        envelope = json.loads(secure_io.read_bytes(path, collection.MAX_ENVELOPE_BYTES))
    except Exception as exc:
        raise FillError("RECEIPT_INVALID: 不是有效的回执文件") from exc
    if not isinstance(envelope, dict):
        raise FillError("RECEIPT_INVALID: 不是有效的回执文件")
    result: dict[str, Any] = {}
    for key in ("task_id", "invite_id", "key_id", "schema_hash", "format_version"):
        value = envelope.get(key)
        if not isinstance(value, str) or not value:
            raise FillError(f"RECEIPT_INVALID: 回执信封头缺少字段 {key}")
        result[key] = value
    if not collection.TASK_ID_RE.fullmatch(result["task_id"]):
        raise FillError("RECEIPT_INVALID: 回执信封头 task_id 无效")
    try:
        collection.validate_envelope_header(envelope, envelope)
    except (ValueError, TypeError, KeyError) as exc:
        raise FillError("RECEIPT_INVALID: 回执格式、归属或签名无效") from exc
    result["revision"] = envelope["revision"]
    try:
        vault_path, _key_path = _storage_paths(args)
        match = next((record for record in vault.load_registry(vault_path.parent)
                      if record["invite_id"] == result["invite_id"]), None)
        if match is not None:
            result["registry"] = match
    except Exception:
        pass
    return result


def _same_task_receipts(out_path: Path, task_id: str) -> list[str]:
    """只统计信封头 task_id 相同的旧回执；其他任务的回执不触发提醒。"""
    def header_task_id(path: Path) -> Any:
        try:
            envelope = json.loads(secure_io.read_bytes(path, collection.MAX_ENVELOPE_BYTES))
        except Exception:
            return None
        return envelope.get("task_id") if isinstance(envelope, dict) else None

    return [path.name for path in sorted(out_path.parent.glob("*.yintian"))
            if path != out_path and header_task_id(path) == task_id]


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
    parser = argparse.ArgumentParser(description="SafeFill · 填写端：读取机器请求包并用本机保险柜产出 .yintian 密文")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("doctor", help="只读检查 Python、核心/OCR/VLM 依赖与保险柜存储状态（不联网、不写盘）")
    _add_storage_args(p)
    p.set_defaults(func=cmd_doctor)
    p = sub.add_parser("inspect", help="查看请求包的告知内容、字段清单与公钥指纹；也识别 .yintian-notice 退回/补正通知（不修改文件）")
    p.add_argument("form", metavar="REQUEST-*.yintian-request")
    p.set_defaults(func=cmd_inspect)
    p = sub.add_parser("vault-status", help="查看本机保险柜摘要及与请求包的字段匹配预览（不输出条目值）")
    p.add_argument("--request", metavar="REQUEST-*.yintian-request", help="可选：按该请求包字段预览 match/missing")
    _add_storage_args(p)
    p.set_defaults(func=cmd_vault_status)
    p = sub.add_parser("vault-stage", help="暂存条目供本人确认；支持对话 JSON 或 OpenVINO 本机文本提取")
    entry_input = p.add_mutually_exclusive_group(required=True)
    entry_input.add_argument("--answers",
                   help='推荐 "-"：从标准输入读取 JSON，明文不落盘；或 0700 目录内的 0600 临时 JSON：{"entries": {"phone": {"type": "phone_cn", "value": "..."}}}')
    entry_input.add_argument("--text-file", help="本人指定的 UTF-8 .txt（最大 16 KiB）；仅脚本读取，原文件保留")
    p.add_argument("--request", help="文本提取所用请求包；只提取其中的标量字段")
    p.add_argument("--model", help="已安装的 OpenVINO GenAI LLMPipeline 兼容文本模型")
    p.add_argument("--revision", help="可选模型 revision，与 vlm-setup 相同")
    p.add_argument("--confirmation-out", required=True, help="写入 0600 加密确认文件；路径必须位于 0700 目录")
    _add_storage_args(p)
    p.set_defaults(func=cmd_vault_stage)
    p = sub.add_parser("vault-apply", help="应用员工已确认的保险柜变更")
    p.add_argument("--confirmation", required=True, help="vault-stage 生成的确认文件")
    _add_storage_args(p)
    p.set_defaults(func=cmd_vault_apply)
    p = sub.add_parser("vault-scan", help="OpenVINO 本地识别本人明确指定的证件图，输出候选供确认后写入保险柜")
    p.add_argument("images", nargs="+", metavar="IMAGE")
    p.add_argument("--vlm", action="store_true", help="使用独立 VLM 环境；不可用时由 Agent 改用核心环境重试普通 OCR")
    p.add_argument("--model", help="使用 --vlm 时指定兼容的 Hugging Face 模型")
    p.add_argument("--revision", help="可选模型 revision；不指定时使用模型仓库默认版本")
    p.set_defaults(func=cmd_vault_scan)
    p = sub.add_parser("vault-preview", help="预览并生成确认凭据；本地文本条目的完整值只写入本人核对文件")
    p.add_argument("form", metavar="REQUEST-*.yintian-request")
    p.add_argument("--mapping", help="请求字段 id → 保险柜条目 id 的显式映射 JSON")
    p.add_argument("--confirmation-out", required=True, help="写入 0600 加密确认文件；路径必须位于 0700 目录")
    identity = p.add_mutually_exclusive_group()
    identity.add_argument("--previous", help="更正时指定本人上一次签名回执，必须持有对应本机私钥")
    identity.add_argument("--fresh", action="store_true", help="明确新建独立签名身份和记录；不作为错误兜底")
    _add_storage_args(p)
    p.set_defaults(func=cmd_vault_preview)
    p = sub.add_parser("vault-fill", help="用保险柜匹配请求包字段，本人确认后生成带签名和递增序号的回执")
    p.add_argument("form", metavar="REQUEST-*.yintian-request")
    p.add_argument("--confirmation", required=True, help="vault-preview 生成的确认文件")
    output = p.add_mutually_exclusive_group(required=True)
    output.add_argument("--out", help="显式指定输出 .yintian 路径")
    output.add_argument("--out-dir", help="在目录中生成 RECEIPT-回执编号-序号-短码.yintian")
    identity = p.add_mutually_exclusive_group()
    identity.add_argument("--previous", help="指定本机持有私钥的旧签名回执，以更正同一条记录")
    identity.add_argument("--fresh", action="store_true", help="明确新建独立签名身份和记录；不作为错误兜底")
    _add_storage_args(p)
    p.set_defaults(func=cmd_vault_fill)
    p = sub.add_parser("receipt-inspect", help="读取回执信封明文头：辨认文件属于哪个任务、对应哪次提交")
    p.add_argument("receipt", metavar="RECEIPT.yintian")
    _add_storage_args(p)
    p.set_defaults(func=cmd_receipt_inspect)
    p = sub.add_parser("vlm-setup", help="下载用户指定的兼容模型（安装时联网，推理离线）")
    p.add_argument("--model", required=True, help="Hugging Face 或 ModelScope 模型 ID")
    p.add_argument("--revision", help="可选模型 revision；不指定时使用模型仓库默认版本")
    p.add_argument("--source", choices=["huggingface", "modelscope"], default="huggingface",
                   help="模型下载来源；modelscope 为可选备选，需先 pip install modelscope")
    p.set_defaults(func=cmd_vlm_setup)
    return parser


def main(argv: list[str] | None = None) -> int:
    collection.configure_cli_stdio()
    args = build_parser().parse_args(argv)
    try:
        result = args.func(args)
        if result is not None:
            print(json.dumps({"ok": True, **result} if isinstance(result, dict) else result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except Exception as exc:
        print(json.dumps(collection.error_report(exc), ensure_ascii=False, separators=(",", ":")), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
