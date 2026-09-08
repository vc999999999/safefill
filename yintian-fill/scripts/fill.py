"""隐填 · 填写端：员工本机填写需求格式文件并产出 .yintian 密文。

只在员工自己的电脑上运行：纯本地、不联网；只读显式指定的文件；
产出的信封与浏览器邀请页字节级同构（yintian-submission/2）。
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
    try:
        import collection  # noqa: F401
        import ocr_matcher  # noqa: F401
        return
    except ImportError:
        pass
    repo_scripts = Path(__file__).resolve().parents[2] / "scripts"
    if (repo_scripts / "collection.py").is_file() and (repo_scripts / "ocr_matcher.py").is_file():
        if str(repo_scripts) not in sys.path:
            sys.path.insert(0, str(repo_scripts))
        return
    raise RuntimeError("找不到收集端 scripts/collection.py；请在完整隐填仓库内使用本 Skill（yintian-fill/ 应与收集端 scripts/ 同级）")


_bootstrap_repo_scripts()
import collection  # noqa: E402
import fill_extract  # noqa: E402


def _public_key_fingerprint(public_key_pem: str) -> str:
    import hashlib

    from cryptography.hazmat.primitives import serialization

    public_key = serialization.load_pem_public_key(public_key_pem.encode("utf-8"))
    public_der = public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(public_der).hexdigest()[:24]


def load_form(form_path: str | Path) -> dict[str, Any]:
    """读取并校验 yintian-form/1 需求格式文件；只读取这一个文件，不做任何修改。"""
    path = Path(form_path).expanduser().resolve()
    if not path.is_file():
        raise FillError(f"需求格式文件不存在: {form_path}")
    try:
        form = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FillError("需求格式文件不是有效 JSON") from exc
    if not isinstance(form, dict) or form.get("format") != FORM_FORMAT:
        raise FillError(f"不是 {FORM_FORMAT} 需求格式文件")
    mode = form.get("mode")
    if mode not in ("group", "directed"):
        raise FillError("需求格式文件 mode 无效（应为 group 或 directed）")
    missing = [key for key in REQUIRED_FORM_KEYS if not form.get(key)]
    if missing:
        raise FillError(f"需求格式文件缺少必要字段: {', '.join(missing)}")
    if not collection.TASK_ID_RE.fullmatch(str(form["task_id"])):
        raise FillError("需求格式文件 task_id 无效")
    fields = form["fields"]
    if not isinstance(fields, list) or not fields:
        raise FillError("需求格式文件 fields 无效")
    seen = set()
    for field in fields:
        if not isinstance(field, dict) or not collection.FIELD_ID_RE.fullmatch(str(field.get("id", ""))) or field["id"] in seen:
            raise FillError("需求格式文件字段 id 无效或重复")
        if field.get("type") not in collection.ALLOWED_TYPES or not field.get("label"):
            raise FillError(f"需求格式文件字段类型不受支持: {field.get('id')}")
        seen.add(field["id"])
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
    if form.get("format_version") and form["format_version"] != collection.FORMAT_VERSION:
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


def inspect_info(form: dict[str, Any]) -> dict[str, Any]:
    info = {
        "format": FORM_FORMAT,
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
                "sensitive": bool(field.get("sensitive")),
                **({"options": field["options"]} if field.get("type") == "single_choice" and field.get("options") else {}),
                **({"multiple": bool(field.get("multiple"))} if field["type"] in collection.ATTACHMENT_TYPES else {}),
            }
            for field in form["fields"]
        ],
        "warning": VERIFY_HINT,
    }
    if form["mode"] == "directed":
        info["invite_id"] = form["invite_id"]
        if form.get("name"):
            info["name"] = form["name"]
    return info


def print_inspect(info: dict[str, Any]) -> None:
    mode_label = "directed 定向邀请（绑定邀请编号与认证令牌）" if info["mode"] == "directed" else "group 群组模式（无邀请令牌，提交标识为 GRP-<工号>）"
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
    path = Path(image).expanduser().resolve()
    if not path.is_file():
        raise FillError(f"图片不存在: {image}")
    try:
        texts = _ocr_texts(path)
    except ImportError as exc:
        raise FillError(
            "未安装可选 OCR 依赖 rapidocr-openvino；如需扫描证件请执行 "
            "pip install rapidocr-openvino==1.4.4 openvino==2024.0.0（见 requirements.txt 可选段），或改为手动填写"
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
    path = Path(raw).expanduser()
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
        items = []
        for raw_path in raws:
            path = _resolve_attachment(raw_path)
            data = path.read_bytes()
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


def seal_form(form_path: str | Path, values_path: str | Path, out_path: str | Path) -> dict[str, Any]:
    """校验并加密产出与浏览器邀请页同构的 .yintian 信封；校验不通过则不产文件。"""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    form = load_form(form_path)
    if form["_key_fingerprint"] != form["key_id"]:
        raise FillError("公钥指纹与 key_id 不一致，文件可能被篡改，请向发放人重新索取")
    values_file = Path(values_path).expanduser().resolve()
    if not values_file.is_file():
        raise FillError(f"values.json 不存在: {values_path}")
    _warn_values_permissions(values_file)
    try:
        data = json.loads(values_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FillError("values.json 不是有效 JSON") from exc
    if not isinstance(data, dict):
        raise FillError("values.json 必须是 {\"values\": {...}, \"attachments\": {...}} 对象")

    normalized, problems = _check_values(form, data.get("values", {}))
    attachments, attachment_problems = _build_attachments(form, data.get("attachments", {}))
    problems += attachment_problems

    if form["mode"] == "directed":
        invite_id = form["invite_id"]
    else:
        employee_id = data.get("employee_id") or normalized.get("employee_id") or str(data.get("values", {}).get("employee_id", ""))
        try:
            collection.parse_group_employee_id(GROUP_INVITE_PREFIX + str(employee_id))
        except ValueError:
            problems.append("group 模式需要有效的 employee_id（工号字段或 values.json 顶层提供）")
            employee_id = ""
        invite_id = GROUP_INVITE_PREFIX + employee_id
    if problems:
        raise FillError("填写校验未通过，未生成任何文件：\n- " + "\n- ".join(problems))

    payload = {
        "format_version": collection.FORMAT_VERSION,
        "task_id": form["task_id"],
        "invite_id": invite_id,
        **({"invite_token": form["_token"]} if form["mode"] == "directed" else {}),
        "schema_hash": form["schema_hash"],
        "notice_hash": form["notice_hash"],
        "template_version": str(form.get("template_version", "1.0")),
        "submitted_at": collection.now_iso(),
        "consent_confirmed": True,
        "values": normalized,
        "attachments": attachments,
    }
    envelope: dict[str, Any] = {
        "format_version": collection.FORMAT_VERSION,
        "task_id": form["task_id"],
        "invite_id": invite_id,
        "schema_hash": form["schema_hash"],
        "key_id": form["key_id"],
        "algorithms": {"content": "AES-256-GCM", "key_wrap": "RSA-OAEP-3072-SHA256"},
    }
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
    blob = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(blob) > collection.MAX_ENVELOPE_BYTES:
        raise FillError("加密信封超过 32MB 上限，未生成文件")
    out = Path(out_path).expanduser()
    if out.suffix != ".yintian":
        out = out.with_suffix(out.suffix + ".yintian") if out.suffix else out.with_suffix(".yintian")
    collection.atomic_write(out, blob)
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


def cmd_seal(args) -> dict[str, Any] | None:
    summary = seal_form(args.form, args.values, args.out)
    if args.json:
        return summary
    print(f"加密提交文件已生成：{summary['out']}")
    print(f"任务编号：{summary['task_id']}　提交标识：{summary['invite_id']}（{summary['mode']}）")
    print("请通过发放人指定的私聊方式交回该 .yintian 文件；不要交回 values.json。")
    print("values.json 含明文，确认交回后请删除。")
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="隐填 · 填写端：本机填写需求格式文件并产出 .yintian 密文")
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
    p.add_argument("--values", required=True, help="填写值 JSON：{\"values\": {...}, \"attachments\": {...}}")
    p.add_argument("--out", required=True, help="输出 .yintian 路径")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_seal)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = args.func(args)
        if result is not None:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
