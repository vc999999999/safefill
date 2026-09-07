"""隐填 · RapidOCR + OpenVINO 本地 OCR。"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import privacy
from config import MAX_SIDE, MIN_SCORE, OCR_DEVICE

_engine = None


def _build_engine():
    try:
        from openvino_runtime import install_rapidocr_device_patch
        from rapidocr_openvino import RapidOCR
    except ImportError as exc:
        raise RuntimeError(
            "未安装 OCR 后端。请执行 pip install -r requirements.txt；"
            "纯文本材料无需 OCR 依赖。"
        ) from exc
    install_rapidocr_device_patch()
    return RapidOCR(text_score=0.0, max_side_len=MAX_SIDE)


def _get_engine():
    global _engine
    if _engine is None:
        _engine = _build_engine()
    return _engine


def _execution_devices(engine) -> list[str]:
    devices: list[str] = []
    for owner in (getattr(engine, "text_det", None), getattr(engine, "text_cls", None), getattr(engine, "text_rec", None)):
        session = getattr(owner, "infer", None) or getattr(owner, "session", None)
        for device in getattr(session, "execution_devices", []):
            if device not in devices:
                devices.append(device)
    return devices or [OCR_DEVICE]


def _scan_content(content: Any) -> dict[str, Any]:
    engine = _get_engine()
    started = time.perf_counter()
    result, stages = engine(content)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)

    blocks = []
    for box, text, score, *_ in result or []:
        score = float(score)
        blocks.append(
            {
                "text": text,
                "score": round(score, 3),
                "low_confidence": score < MIN_SCORE,
                "box": [[round(float(x), 1), round(float(y), 1)] for x, y in box],
            }
        )

    return {
        "backend": "RapidOCR + OpenVINO",
        "requested_device": OCR_DEVICE,
        "execution_devices": _execution_devices(engine),
        "elapsed_ms": elapsed_ms,
        "stage_elapsed_ms": [round(float(item) * 1000, 1) for item in stages or []],
        "blocks": blocks,
        "full_text": "\n".join(block["text"] for block in blocks),
    }


def scan(image_path: str) -> dict[str, Any]:
    path = Path(image_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"图片不存在: {image_path}")
    privacy.assert_in_vault(str(path))
    return _scan_content(str(path))


def scan_bytes(image_bytes: bytes) -> dict[str, Any]:
    """对内存中的解密图片执行 OCR，不创建明文临时文件。"""
    if not image_bytes:
        raise ValueError("图片数据为空")
    return _scan_content(image_bytes)
