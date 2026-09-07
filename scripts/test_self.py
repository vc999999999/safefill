"""无测试框架的最小回归检查。"""
from __future__ import annotations

import tempfile
from pathlib import Path

import identity_extract
import config
import privacy
import target_map


def main() -> None:
    valid_id = "110105198503071230"
    assert identity_extract.validate_chinese_id(valid_id)
    assert not identity_extract.validate_chinese_id(valid_id[:-1] + "4")
    assert identity_extract.validate_luhn("4532015112830366")
    assert not identity_extract.validate_luhn("6222020202020202020")

    with tempfile.TemporaryDirectory() as tmp:
        vault = Path(tmp) / "vault"
        vault.mkdir()
        config.VAULT_DIR = str(vault)
        assert privacy.assert_in_vault(str(vault / "received"))
        try:
            privacy.assert_in_vault(str(Path(tmp) / "outside"))
        except privacy.PrivacyViolation:
            pass
        else:
            raise AssertionError("vault boundary accepted an outside path")

        source = vault / "identity.txt"
        source.write_text(f"姓名 张三\n公民身份号码 {valid_id}\n联系电话 13800138000\n", encoding="utf-8")
        packet = identity_extract.extract_identity_packet([str(source)])
        assert packet["processed_count"] == 1
        assert packet["derived"]["birth_date"]["value"] == "1985-03-07"
        mapped = target_map.map_target_fields(packet, ["姓名", "出生日期", "紧急联系人"])
        assert [item["target_field"] for item in mapped["mapping"]] == ["姓名", "出生日期"]
        assert mapped["missing"][0]["target_field"] == "紧急联系人"

        low_fields = {}
        identity_extract._extract_from_text(
            f"姓名 张三\n公民身份号码 {valid_id}",
            str(source),
            low_fields,
            {"姓名 张三"},
        )
        assert low_fields["name"][0].confidence == "ocr_low_confidence"

        source.write_text(
            f"公民身份号码 {valid_id}\n公民身份号码 11010519491231002X\n",
            encoding="utf-8",
        )
        ambiguous = identity_extract.extract_identity_packet([str(source)])
        assert "birth_date" not in ambiguous["derived"] and "gender" not in ambiguous["derived"]
    print("PASS: validation, extraction and target mapping")


if __name__ == "__main__":
    main()
