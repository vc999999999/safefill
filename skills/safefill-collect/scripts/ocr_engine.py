"""SafeFill · RapidOCR + OpenVINO 本地 OCR。"""
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


def _preprocess_image_bytes(image_bytes: bytes) -> bytes:
    """自动纠正移动端拍照的 EXIF 旋转角度（例如 iPhone 横向/纵向拍摄元数据），提高 OCR 准确率。"""
    try:
        import io
        from PIL import Image, ImageOps

        with Image.open(io.BytesIO(image_bytes)) as img:
            transposed = ImageOps.exif_transpose(img)
            if transposed is not img:
                out = io.BytesIO()
                fmt = img.format or "JPEG"
                if fmt.upper() in ("JPEG", "JPG"):
                    transposed.save(out, format="JPEG", quality=95)
                elif fmt.upper() == "PNG":
                    transposed.save(out, format="PNG")
                elif fmt.upper() == "WEBP":
                    transposed.save(out, format="WEBP", quality=95)
                else:
                    transposed.save(out, format=fmt)
                return out.getvalue()
    except Exception:
        pass
    return image_bytes


def scan_bytes(image_bytes: bytes) -> dict[str, Any]:
    """对内存中的解密图片执行 OCR，不创建明文临时文件。自动纠偏移动端 EXIF 拍摄角度。"""
    if not image_bytes:
        raise ValueError("图片数据为空")
    return _scan_content(_preprocess_image_bytes(image_bytes))
