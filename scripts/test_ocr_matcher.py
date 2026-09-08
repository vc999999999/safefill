"""ocr_matcher.py 的单元测试。支持直接运行，亦支持 pytest 收集。"""
from __future__ import annotations

import ocr_matcher

VALID_ID = "110105198503071230"
VALID_ID_X = "11010519491231002X"


def test_chinese_id_validation() -> None:
    # 正常身份证校验通过
    assert ocr_matcher.validate_chinese_id(VALID_ID) is True
    assert ocr_matcher.validate_chinese_id(VALID_ID_X) is True

    # 校验码错误
    assert ocr_matcher.validate_chinese_id(VALID_ID[:-1] + "4") is False

    # 出生日期无效（如 2 月 30 日）
    assert ocr_matcher.validate_chinese_id("110105198502301234") is False

    # 长度或字符非法
    assert ocr_matcher.validate_chinese_id("") is False
    assert ocr_matcher.validate_chinese_id("123456") is False
    assert ocr_matcher.validate_chinese_id("11010519850307123Y") is False


def test_extract_from_text() -> None:
    sample_text = (
        "姓名：张三\n"
        "性别：男\n"
        "公民身份号码：110105198503071230\n"
        "住址：北京市海淀区中关村大街1号\n"
        "联系手机：13800138000\n"
    )
    fields: dict[str, list[ocr_matcher.Candidate]] = {}
    ocr_matcher.extract_from_text(sample_text, "test_source", fields)

    assert "name" in fields
    assert fields["name"][0].value == "张三"

    assert "gender" in fields
    assert fields["gender"][0].value == "男"

    assert "id_number" in fields
    assert fields["id_number"][0].value == VALID_ID
    assert fields["id_number"][0].valid is True

    assert "phone" in fields
    assert fields["phone"][0].value == "13800138000"

    assert "address" in fields
    assert "海淀区" in fields["address"][0].value


def test_low_confidence_tagging() -> None:
    sample_text = f"姓名 李四\n公民身份号码 {VALID_ID}"
    fields: dict[str, list[ocr_matcher.Candidate]] = {}
    ocr_matcher.extract_from_text(
        sample_text,
        "test_source",
        fields,
        low_confidence_texts={"李四"},
    )
    assert fields["name"][0].confidence == "ocr_low_confidence"
    assert fields["id_number"][0].confidence == "validated"


def main() -> None:
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_") and callable(fn)]
    for name, fn in tests:
        fn()
        print(f"PASS: {name}")
    print(f"ocr_matcher 共 {len(tests)} 项检查全部通过。")


if __name__ == "__main__":
    main()
