"""隐填 · 本地 OCR 文本要素提取与确定性比对。

仅用于本地内存中对 RapidOCR 识别结果与员工填写值进行比对，
不输出明文、不记录日志、不上传任何外部服务。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

ID_RE = re.compile(r"(?<!\d)(\d{17}[\dXx])(?!\d)")
PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
ID_CHECK_CODES = "10X98765432"

LABEL_PATTERNS = {
    "name": re.compile(r"(?:姓名|名字|户名)[:：\s]+([\u4e00-\u9fa5A-Za-z·.]{2,30})"),
    "gender": re.compile(r"(?:性别)[:：\s]+([男女])"),
    "address": re.compile(r"(?:住址|地址|户籍地址|联系地址)[:：\s]*(.{4,120})"),
}


@dataclass
class Candidate:
    value: str
    source: str
    evidence: str
    confidence: str
    valid: bool | None = None


def _line_for_match(text: str, start: int) -> str:
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", start)
    return text[line_start : len(text) if line_end == -1 else line_end].strip()


def _valid_id_date(id_number: str) -> str | None:
    try:
        return datetime.strptime(id_number[6:14], "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def validate_chinese_id(id_number: str) -> bool:
    """按 GB 11643-1999 校验 18 位中国大陆公民身份号码的出生日期与 ISO 7064:1983.MOD 11-2 校验码。"""
    value = id_number.strip().upper()
    return (
        len(value) == 18
        and value[:17].isdigit()
        and _valid_id_date(value) is not None
        and ID_CHECK_CODES[sum(int(n) * w for n, w in zip(value[:17], ID_WEIGHTS)) % 11] == value[-1]
    )


def _add_candidate(
    fields: dict[str, list[Candidate]],
    field: str,
    value: str,
    source: str,
    evidence: str,
    confidence: str,
    valid: bool | None = None,
) -> None:
    candidate = Candidate(value.strip(), source, evidence.strip(), confidence, valid)
    if candidate.value and candidate not in fields.setdefault(field, []):
        fields[field].append(candidate)


def extract_from_text(
    text: str,
    source: str,
    fields: dict[str, list[Candidate]],
    low_confidence_texts: set[str] | None = None,
) -> None:
    """从 OCR 文本中提取姓名、身份证、手机号和地址候选，并标记置信度。"""
    low_confidence_texts = low_confidence_texts or set()

    def confidence_for(evidence: str, normal: str) -> str:
        return "ocr_low_confidence" if any(part and part in evidence for part in low_confidence_texts) else normal

    # 1. 结构化标签匹配（姓名、性别、地址）
    for field, pattern in LABEL_PATTERNS.items():
        for match in pattern.finditer(text):
            evidence = _line_for_match(text, match.start())
            _add_candidate(fields, field, match.group(1), source, evidence, confidence_for(evidence, "label"))

    # 2. 18 位身份证号码正则 + 校验码规则验证
    for match in ID_RE.finditer(text):
        value = match.group(1).upper()
        valid = validate_chinese_id(value)
        evidence = _line_for_match(text, match.start())
        confidence = "invalid_checksum" if not valid else confidence_for(evidence, "validated")
        _add_candidate(fields, "id_number", value, source, evidence, confidence, valid)

    # 3. 手机号模式提取
    for match in PHONE_RE.finditer(text):
        evidence = _line_for_match(text, match.start())
        _add_candidate(fields, "phone", match.group(1), source, evidence, confidence_for(evidence, "pattern"), True)
