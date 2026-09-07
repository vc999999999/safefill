"""隐填 · 用 NNCF 对 RapidOCR 的 det/cls/rec IR 模型做 INT8 训练后量化。"""
from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

# det/cls/rec 各自的校准输入尺寸 (高, 宽)
INPUT_SHAPES = {"det": (640, 640), "cls": (48, 192), "rec": (48, 320)}
CALIB_COUNT = 40
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
# 常见中文字体，都找不到时退回 PIL 默认字体的纯 ASCII 文本行
CJK_FONTS = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
]


def _import_deps():
    try:
        import nncf
        import openvino as ov
    except ImportError:
        sys.exit('缺少量化依赖，请执行 pip install "nncf>=2.9" 后重试')
    return nncf, ov


def _find_models() -> dict[str, Path]:
    try:
        import rapidocr_openvino
    except ImportError:
        sys.exit("未安装 rapidocr-openvino，请执行 pip install -r requirements.txt")
    package = Path(rapidocr_openvino.__file__).resolve().parent
    found: dict[str, Path] = {}
    for xml in sorted(package.rglob("*.xml")):
        name = xml.name.lower()
        for kind in INPUT_SHAPES:
            if kind in name and kind not in found:
                found[kind] = xml
    missing = [kind for kind in INPUT_SHAPES if kind not in found]
    if missing:
        sys.exit(f"在 {package} 内未找到 {', '.join(missing)} 模型的 .xml 文件")
    return found


def _pick_font(size: int):
    from PIL import ImageFont

    for path in CJK_FONTS:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size), True
            except OSError:
                continue
    return ImageFont.load_default(), False


def _synth_image(seed: int):
    from PIL import Image, ImageDraw

    rng = random.Random(seed)
    font, cjk = _pick_font(28)
    charset = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    if cjk:
        charset += "姓名性别民族出生住址公民身份号码签发机关有效期限张三李四王五省市区县路号"
    image = Image.new("RGB", (640, 200), "white")
    draw = ImageDraw.Draw(image)
    y = 12
    while y + 36 < 200:
        text = "".join(rng.choice(charset) for _ in range(rng.randint(8, 24)))
        draw.text((12, y), text, fill="black", font=font)
        y += 44
    return image


def _load_calib_images(calib_dir: str | None, count: int) -> list:
    try:
        from PIL import Image
    except ImportError:
        sys.exit("缺少 Pillow，请执行 pip install -r requirements.txt")
    if not calib_dir:
        return [_synth_image(seed) for seed in range(count)]
    paths = sorted(
        p
        for p in Path(calib_dir).expanduser().rglob("*")
        if p.suffix.lower() in IMAGE_SUFFIXES
    )
    if not paths:
        sys.exit(f"校准目录中没有可用图片: {calib_dir}")
    images = []
    for path in paths[:count]:
        with Image.open(path) as img:
            images.append(img.convert("RGB"))
    return images


def _preprocess(image, size: tuple[int, int]):
    import numpy as np

    height, width = size
    array = np.asarray(image.resize((width, height)), dtype=np.float32) / 255.0
    array = (array - 0.5) / 0.5
    return array.transpose(2, 0, 1)[None]


def _ir_size(xml: Path) -> int:
    total = xml.stat().st_size
    weights = xml.with_suffix(".bin")
    if weights.is_file():
        total += weights.stat().st_size
    return total


def quantize(calib_dir: str | None, out: str, count: int) -> list[dict]:
    nncf, ov = _import_deps()
    models = _find_models()
    images = _load_calib_images(calib_dir, count)
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    core = ov.Core()

    results = []
    for kind, size in INPUT_SHAPES.items():
        xml = models[kind]
        model = core.read_model(str(xml))
        if len(model.inputs) != 1:
            sys.exit(f"{xml.name} 不是单输入模型，暂不支持自动量化")
        input_name = model.inputs[0].get_any_name()
        dataset = nncf.Dataset(
            images,
            lambda image: {input_name: _preprocess(image, size)},
        )
        quantized = nncf.quantize(
            model,
            dataset,
            preset=nncf.QuantizationPreset.MIXED,
            subset_size=len(images),
        )
        out_xml = out_dir / xml.name
        ov.save_model(quantized, str(out_xml))
        results.append(
            {
                "kind": kind,
                "out": str(out_xml),
                "fp32_mb": _ir_size(xml) / 1048576,
                "int8_mb": _ir_size(out_xml) / 1048576,
            }
        )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="用 NNCF 对 RapidOCR 的 det/cls/rec 模型做 INT8 训练后量化")
    parser.add_argument("--calib-dir", help="校准图片目录；缺省时生成合成文本行图片")
    parser.add_argument("--out", default="models/int8", help="INT8 IR 输出目录")
    parser.add_argument("--num-calib", type=int, default=CALIB_COUNT, help="校准图片数量")
    args = parser.parse_args()
    results = quantize(args.calib_dir, args.out, max(1, args.num_calib))
    print(f"INT8 模型已保存到 {args.out}")
    for item in results:
        ratio = item["fp32_mb"] / item["int8_mb"] if item["int8_mb"] else 0.0
        print(f"{item['kind']}: FP32 {item['fp32_mb']:.2f} MB -> INT8 {item['int8_mb']:.2f} MB ({ratio:.1f}x)")


if __name__ == "__main__":
    main()
