"""隐填 · OpenVINO 设备自检与 OCR 延迟基准。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from statistics import mean
from typing import Any

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def _model_info() -> dict[str, Any]:
    """返回实际使用的模型路径与总大小，不加载模型。"""
    info: dict[str, Any] = {"files": [], "total_mb": 0.0}
    try:
        import rapidocr_openvino
    except ImportError:
        info["error"] = "未安装 rapidocr-openvino"
        return info

    import config
    from openvino_runtime import _resolve_model_path

    if config.MODEL_DIR:
        info["custom_dir"] = config.MODEL_DIR
    models_dir = Path(rapidocr_openvino.__file__).resolve().parent / "models"
    total = 0
    for xml in sorted([*models_dir.glob("*.xml"), *models_dir.glob("*.onnx")]):
        path = Path(_resolve_model_path(str(xml)))
        size = path.stat().st_size
        weights = path.with_suffix(".bin")
        if weights.is_file():
            size += weights.stat().st_size
        total += size
        info["files"].append({"path": str(path), "size_mb": round(size / 1048576, 2)})
    info["total_mb"] = round(total / 1048576, 2)
    return info


def _images(target: str) -> list[str]:
    path = Path(target).expanduser()
    if not path.is_dir():
        return [target]
    images = sorted(p for p in path.rglob("*") if p.suffix.lower() in IMAGE_SUFFIXES)
    if not images:
        raise ValueError(f"目录中没有可用图片: {target}")
    return [str(p) for p in images]


def _timed_scan(image: str, runs: int) -> dict[str, Any]:
    import ocr_engine

    ocr_engine.scan(image)  # 预热一次不计时，含模型编译
    samples = [ocr_engine.scan(image) for _ in range(max(1, runs))]
    latencies = [item["elapsed_ms"] for item in samples]
    return {
        "image": image,
        "runs": len(samples),
        "execution_devices": samples[-1]["execution_devices"],
        "latency_ms": latencies,
        "average_ms": round(mean(latencies), 1),
        "texts": [block["text"] for block in samples[-1]["blocks"]],
    }


def _switch_model_dir(model_dir: str | None) -> str | None:
    """切换 YINTIAN_MODEL_DIR 并重置引擎，返回原值。"""
    import config
    import ocr_engine

    original = config.MODEL_DIR
    config.MODEL_DIR = model_dir
    ocr_engine._engine = None
    return original


def _agreement(ref: list[str], other: list[str]) -> float:
    if not ref and not other:
        return 1.0
    ref_set, other_set = set(ref), set(other)
    if not ref_set and not other_set:
        return 1.0
    if not ref_set or not other_set:
        return 0.0
    return round(len(ref_set & other_set) / max(len(ref_set), len(other_set)), 4)


def benchmark(image: str | None = None, runs: int = 3) -> dict:
    from openvino_runtime import runtime_info

    report = runtime_info()
    report["models"] = _model_info()
    if not image:
        return report

    import config

    images = _images(image)
    samples = [_timed_scan(item, runs) for item in images]
    report["ocr"] = {
        "model_dir": config.MODEL_DIR or "default",
        "runs": samples[0]["runs"],
        "execution_devices": samples[0]["execution_devices"],
        "images": [
            {key: value for key, value in item.items() if key != "texts"} | {"text_blocks": len(item["texts"])}
            for item in samples
        ],
        "average_ms": round(mean(item["average_ms"] for item in samples), 1),
    }

    if config.MODEL_DIR:
        if not list(Path(config.MODEL_DIR).expanduser().glob("*.xml")):
            raise ValueError(f"对比模型目录中没有 .xml 模型: {config.MODEL_DIR}")
        original = _switch_model_dir(None)
        try:
            baseline = [_timed_scan(item, runs) for item in images]
        finally:
            _switch_model_dir(original)
        report["compare"] = {
            "baseline_model_dir": "default",
            "images": [
                {
                    "image": custom["image"],
                    "text_agreement": _agreement(base["texts"], custom["texts"]),
                    "baseline_average_ms": base["average_ms"],
                    "custom_average_ms": custom["average_ms"],
                }
                for base, custom in zip(baseline, samples)
            ],
        }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="检查 OpenVINO 设备并可选运行 OCR 基准")
    parser.add_argument("image", nargs="?", help="可选：本地图片路径或图片目录")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--device", help="覆盖 YINTIAN_OCR_DEVICE，如 CPU/GPU/NPU/AUTO")
    parser.add_argument("--model-dir", help="覆盖 YINTIAN_MODEL_DIR，如 models/int8；与默认模型对比输出一致率")
    args = parser.parse_args()
    # 必须在 import config/ocr_engine 之前设置，配置只在导入时读取一次
    if args.device:
        os.environ["YINTIAN_OCR_DEVICE"] = args.device
    if args.model_dir:
        os.environ["YINTIAN_MODEL_DIR"] = args.model_dir
    try:
        report = benchmark(args.image, args.runs)
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
