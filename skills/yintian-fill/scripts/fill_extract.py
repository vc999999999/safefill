"""隐填 · 填写端：从 OCR 文本提取身份字段候选。

身份证号按 GB 11643-1999 校验码与出生日期双重校验，直接复用收集端
scripts/ocr_matcher.py 的同一实现；多候选只标记歧义，绝不静默挑选。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


def _import_ocr_matcher():
    import ocr_matcher
    return ocr_matcher


ocr_matcher = _import_ocr_matcher()
validate_chinese_id = ocr_matcher.validate_chinese_id

EXTRACT_FIELDS = ("name", "id_number", "phone")
CONFIDENCE_RANK = {"validated": 4, "label": 3, "pattern": 3, "ocr_low_confidence": 1, "invalid_checksum": 0}


def extract_fields(text: str, source: str = "text") -> dict[str, Any]:
    """从 OCR 文本提取姓名、身份证号、手机号候选。

    返回 {"fields": {字段: {"value","confidence"}}, "candidates": {字段: [全部候选]}, "ambiguous": [字段]}；
    同一字段出现多个不同取值时列入 ambiguous，由人确认，不静默取舍。
    """
    raw: dict[str, list] = {}
    ocr_matcher.extract_from_text(text, source, raw)
    fields: dict[str, Any] = {}
    candidates: dict[str, list] = {}
    ambiguous: list[str] = []
    for field in EXTRACT_FIELDS:
        items = raw.get(field, [])
        if not items:
            continue
        best_by_value: dict[str, tuple] = {}
        for candidate in items:
            rank = (candidate.valid is not False, CONFIDENCE_RANK.get(candidate.confidence, 2))
            if candidate.value not in best_by_value or rank > best_by_value[candidate.value][0]:
                best_by_value[candidate.value] = (rank, candidate)
        ordered = sorted(best_by_value.values(), key=lambda item: item[0], reverse=True)
        cand_list = [{"value": c.value, "confidence": c.confidence, "valid": c.valid} for _rank, c in ordered]
        candidates[field] = cand_list
        fields[field] = {"value": cand_list[0]["value"], "confidence": cand_list[0]["confidence"]}
        if len(cand_list) > 1:
            ambiguous.append(field)
    return {"fields": fields, "candidates": candidates, "ambiguous": ambiguous}
