"""隐填 · 将目标字段映射到最可靠的本地候选值。"""
from __future__ import annotations

from typing import Any

ALIASES = {
    "name": ("姓名", "名字", "name", "full name", "户名"),
    "id_number": ("身份证", "身份证号", "证件号", "id number", "national id"),
    "birth_date": ("出生日期", "生日", "birth date", "birthday"),
    "gender": ("性别", "gender", "sex"),
    "address": ("地址", "住址", "联系地址", "address"),
    "phone": ("手机", "电话", "手机号", "phone", "mobile"),
    "bank_card": ("银行卡", "卡号", "bank card", "account number"),
    "bank_name": ("开户行", "银行名称", "bank name", "bank"),
}


def _identity_field(label: str) -> str | None:
    normalized = label.strip().lower()
    for field, aliases in ALIASES.items():
        if any(alias.lower() in normalized or normalized in alias.lower() for alias in aliases):
            return field
    return None


def map_target_fields(packet: dict[str, Any], target_fields: list[str], target: str = "") -> dict[str, Any]:
    mapping, missing, needs_confirmation = [], [], []
    for label in target_fields:
        field = _identity_field(label)
        values = packet.get("fields", {}).get(field, []) if field else []
        if not values and field:
            derived = packet.get("derived", {}).get(field)
            values = [derived] if derived else []
        if not field or not values:
            missing.append({"target_field": label, "reason": "not found in local private materials"})
            continue

        usable = [item for item in values if item and item.get("valid") is not False]
        unique = {item["value"] for item in usable}
        if len(unique) != 1:
            needs_confirmation.append({"target_field": label, "identity_field": field, "candidates": values})
            continue
        selected = usable[0]
        mapping.append(
            {
                "target_field": label,
                "identity_field": field,
                "value": selected["value"],
                "source": selected["source"],
                "status": "ready",
            }
        )
    return {"target": target, "mapping": mapping, "missing": missing, "needs_confirmation": needs_confirmation}
