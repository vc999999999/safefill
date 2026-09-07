"""隐填 · OpenVINO 设备自检与 OCR 延迟基准。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from statistics import mean
from typing import Any


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
    for xml in sorted(models_dir.glob("*.xml")):
        path = Path(_resolve_model_path(str(xml)))
        size = path.stat().st_size
        weights = path.with_suffix(".bin")
        if weights.is_file():
            size += weights.stat().st_size
        total += size
        info["files"].append({"path": str(path), "size_mb": round(size / 1048576, 2)})
    info["total_mb"] = round(total / 1048576, 2)
    return info


def benchmark(image: str | None = None, runs: int = 3) -> dict:
    from openvino_runtime import runtime_info

    report = runtime_info()
    report["models"] = _model_info()
    if not image:
        return report

    import ocr_engine

    samples = [ocr_engine.scan(image) for _ in range(max(1, runs))]
    latencies = [item["elapsed_ms"] for item in samples]
    report["ocr"] = {
        "image": image,
        "runs": len(samples),
        "execution_devices": samples[-1]["execution_devices"],
        "latency_ms": latencies,
        "average_ms": round(mean(latencies), 1),
        "text_blocks": len(samples[-1]["blocks"]),
    }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="检查 OpenVINO 设备并可选运行 OCR 基准")
    parser.add_argument("image", nargs="?", help="可选：本地图片路径")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--device", help="覆盖 YINTIAN_OCR_DEVICE，如 CPU/GPU/NPU/AUTO")
    parser.add_argument("--model-dir", help="覆盖 YINTIAN_MODEL_DIR，如 models/int8")
    args = parser.parse_args()
    # 必须在 import config/ocr_engine 之前设置，配置只在导入时读取一次
    if args.device:
        os.environ["YINTIAN_OCR_DEVICE"] = args.device
    if args.model_dir:
        os.environ["YINTIAN_MODEL_DIR"] = args.model_dir
    try:
        report = benchmark(args.image, args.runs)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
