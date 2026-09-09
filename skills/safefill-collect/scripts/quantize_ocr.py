"""SafeFill · 用 NNCF 对 RapidOCR 的 det/cls/rec 模型做 INT8 训练后量化。"""
from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

# det/cls/rec 各自的校准输入尺寸 (高, 宽)
INPUT_SHAPES = {"det": (640, 640), "cls": (48, 192), "rec": (48, 320)}
# 实测 det(DBNet)/rec(SVTR) 对激活 PTQ 极度敏感（检测丢行、CTC 解码全空），
# 只能用 INT8 权重压缩；cls 用含校准数据的完整 PTQ
WEIGHTS_ONLY_KINDS = {"det", "rec"}
# RapidOCR det 预处理约定：长边不超过 960、边长 32 对齐、ImageNet 归一化
DET_LIMIT = 960
DET_MEAN = (0.485, 0.456, 0.406)
DET_STD = (0.229, 0.224, 0.225)
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
        sys.exit('缺少量化依赖，请执行 pip install -r requirements-quantization.txt 后重试')
    return nncf, ov


def _find_models() -> dict[str, Path]:
    try:
        import rapidocr_openvino
    except ImportError:
        sys.exit("未安装 rapidocr-openvino，请执行 pip install -r requirements.txt")
    package = Path(rapidocr_openvino.__file__).resolve().parent
    found: dict[str, Path] = {}
    # 1.4.4 随包发布的是 .onnx（OpenVINO 直接读取），更老的包才是 IR .xml
    candidates = sorted(package.rglob("*.xml")) or sorted(package.rglob("*.onnx"))
    for xml in candidates:
        name = xml.name.lower()
        for kind in INPUT_SHAPES:
            if kind in name and kind not in found:
                found[kind] = xml
    missing = [kind for kind in INPUT_SHAPES if kind not in found]
    if missing:
        sys.exit(f"在 {package} 内未找到 {', '.join(missing)} 模型的 .xml/.onnx 文件")
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


def _paste(canvas, resized) -> None:
    canvas[: resized.shape[0], : resized.shape[1]] = resized


def _preprocess(image, kind: str, size: tuple[int, int]):
    """与 RapidOCR 推理预处理一致：等比缩放 + 零填充，而不是直接 squash。"""
    import numpy as np

    height, width = size
    src_w, src_h = image.size
    array = np.zeros((height, width, 3), dtype=np.float32)
    if kind == "det":
        ratio = min(DET_LIMIT / max(src_h, src_w), 1.0)
        new_h = min(max(32, int(np.ceil(src_h * ratio / 32)) * 32), height)
        new_w = min(max(32, int(np.ceil(src_w * ratio / 32)) * 32), width)
        _paste(array, np.asarray(image.resize((new_w, new_h)), dtype=np.float32) / 255.0)
        array = (array - np.array(DET_MEAN, dtype=np.float32)) / np.array(DET_STD, dtype=np.float32)
    else:
        # cls/rec：等比缩放到高 48，宽不足时右侧补零，归一化到 [-1, 1]
        new_w = min(max(1, int(np.ceil(src_w * height / src_h))), width)
        _paste(array, np.asarray(image.resize((new_w, height)), dtype=np.float32) / 255.0)
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
        if kind in WEIGHTS_ONLY_KINDS:
            quantized = nncf.compress_weights(model, mode=nncf.CompressWeightsMode.INT8_SYM)
        else:
            input_name = model.inputs[0].get_any_name()
            dataset = nncf.Dataset(
                images,
                lambda image: {input_name: _preprocess(image, kind, size)},
            )
            quantized = nncf.quantize(
                model,
                dataset,
                preset=nncf.QuantizationPreset.MIXED,
                subset_size=len(images),
            )
        out_xml = out_dir / f"{xml.stem}.xml"
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
