"""隐填 · 从本地材料提取可验证的身份字段候选。"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

ID_RE = re.compile(r"(?<!\d)(\d{17}[\dXx])(?!\d)")
PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
BANK_CARD_RE = re.compile(r"(?<!\d)((?:\d[ -]?){16,19})(?!\d)")
ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
ID_CHECK_CODES = "10X98765432"

LABEL_PATTERNS = {
    "name": re.compile(r"(?:姓名|名字|户名)[:：\s]+([\u4e00-\u9fa5A-Za-z·.]{2,30})"),
    "gender": re.compile(r"(?:性别)[:：\s]+([男女])"),
    "address": re.compile(r"(?:住址|地址|户籍地址|联系地址)[:：\s]*(.{4,120})"),
    "bank_name": re.compile(r"(?:开户行|开户银行|发卡行|银行名称)[:：\s]*(.{2,80})"),
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


def _add_candidate(fields, field, value, source, evidence, confidence, valid=None) -> None:
    candidate = Candidate(value.strip(), source, evidence.strip(), confidence, valid)
    if candidate.value and candidate not in fields.setdefault(field, []):
        fields[field].append(candidate)


def _valid_id_date(id_number: str) -> str | None:
    try:
        return datetime.strptime(id_number[6:14], "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def validate_chinese_id(id_number: str) -> bool:
    value = id_number.upper()
    return (
        len(value) == 18
        and value[:17].isdigit()
        and _valid_id_date(value) is not None
        and ID_CHECK_CODES[sum(int(n) * w for n, w in zip(value[:17], ID_WEIGHTS)) % 11] == value[-1]
    )


def validate_luhn(number: str) -> bool:
    digits = [int(char) for char in re.sub(r"\D", "", number)]
    if len(digits) < 12:
        return False
    total = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _derive_from_id(id_number: str, source: str) -> dict[str, Candidate]:
    derived = {}
    birth_date = _valid_id_date(id_number)
    if birth_date:
        derived["birth_date"] = Candidate(birth_date, source, "derived from id_number positions 7-14", "derived", True)
    if id_number[16].isdigit():
        derived["gender"] = Candidate("男" if int(id_number[16]) % 2 else "女", source, "derived from id_number position 17", "derived", True)
    return derived


def _extract_from_text(text: str, source: str, fields, low_confidence_texts: set[str] | None = None) -> dict[str, Candidate]:
    derived = {}
    low_confidence_texts = low_confidence_texts or set()

    def confidence_for(evidence: str, normal: str) -> str:
        return "ocr_low_confidence" if any(part and part in evidence for part in low_confidence_texts) else normal
    for field, pattern in LABEL_PATTERNS.items():
        for match in pattern.finditer(text):
            evidence = _line_for_match(text, match.start())
            _add_candidate(fields, field, match.group(1), source, evidence, confidence_for(evidence, "label"))

    id_numbers = set()
    for match in ID_RE.finditer(text):
        value = match.group(1).upper()
        valid = validate_chinese_id(value)
        id_numbers.add(value)
        evidence = _line_for_match(text, match.start())
        confidence = "invalid_checksum" if not valid else confidence_for(evidence, "validated")
        _add_candidate(fields, "id_number", value, source, evidence, confidence, valid)
        if valid:
            for key, candidate in _derive_from_id(value, source).items():
                derived.setdefault(key, candidate)

    for match in PHONE_RE.finditer(text):
        evidence = _line_for_match(text, match.start())
        _add_candidate(fields, "phone", match.group(1), source, evidence, confidence_for(evidence, "pattern"), True)

    id_digit_forms = {re.sub(r"\D", "", item) for item in id_numbers}
    for match in BANK_CARD_RE.finditer(text):
        value = re.sub(r"\D", "", match.group(1))
        if value in id_digit_forms:
            continue
        valid = validate_luhn(value)
        evidence = _line_for_match(text, match.start())
        confidence = "invalid_checksum" if not valid else confidence_for(evidence, "luhn_validated")
        _add_candidate(fields, "bank_card", value, source, evidence, confidence, valid)
    return derived


def extract_identity_packet(file_paths: list[str], max_chars_per_file: int = 40000) -> dict[str, Any]:
    import source_extract

    extracted = source_extract.extract_files(file_paths, max_chars_per_file=max_chars_per_file)
    fields: dict[str, list[Candidate]] = {}
    derived: dict[str, Candidate] = {}
    errors = [item for item in extracted["files"] if item["kind"] == "error"]

    for item in extracted["files"]:
        if item["kind"] == "error":
            continue
        low_confidence_texts = {block.get("text", "") for block in item.get("ocr_blocks", []) if block.get("low_confidence")}
        for key, candidate in _extract_from_text(item["text"], item["path"], fields, low_confidence_texts).items():
            if key not in fields:
                derived.setdefault(key, candidate)

    needs_review = []
    for field, candidates in fields.items():
        if len({candidate.value for candidate in candidates}) > 1:
            needs_review.append({"field": field, "reason": "multiple_values"})
        for candidate in candidates:
            if candidate.valid is False:
                needs_review.append({"field": field, "value": candidate.value, "reason": "invalid_checksum"})
            elif candidate.confidence == "ocr_low_confidence":
                needs_review.append({"field": field, "value": candidate.value, "reason": "ocr_low_confidence"})

    valid_ids = {candidate.value for candidate in fields.get("id_number", []) if candidate.valid}
    if len(valid_ids) != 1:
        derived.pop("birth_date", None)
        derived.pop("gender", None)

    return {
        "source_count": extracted["count"],
        "processed_count": extracted["count"] - len(errors),
        "source_errors": [{"path": item["path"], "error": item["error"]} for item in errors],
        "fields": {key: [asdict(candidate) for candidate in values] for key, values in sorted(fields.items())},
        "derived": {key: asdict(value) for key, value in sorted(derived.items())},
        "needs_review": needs_review,
    }
