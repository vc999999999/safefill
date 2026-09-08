"""隐填 · 环境变量配置。"""
from __future__ import annotations

import os

OCR_DEVICE = os.getenv("YINTIAN_OCR_DEVICE", "AUTO").upper()
MAX_SIDE = int(os.getenv("YINTIAN_MAX_SIDE", "1600"))
MIN_SCORE = float(os.getenv("YINTIAN_MIN_SCORE", "0.5"))
VAULT_DIR = os.getenv("YINTIAN_VAULT_DIR", "")
MODEL_DIR = os.getenv("YINTIAN_MODEL_DIR", "")

if OCR_DEVICE not in {"CPU", "GPU", "NPU", "AUTO"}:
    raise ValueError("YINTIAN_OCR_DEVICE 必须是 CPU、GPU、NPU 或 AUTO")
if MAX_SIDE < 320:
    raise ValueError("YINTIAN_MAX_SIDE 不能小于 320")
if not 0 <= MIN_SCORE <= 1:
    raise ValueError("YINTIAN_MIN_SCORE 必须在 0 到 1 之间")
