"""可选增强：OpenVINO 驱动的本地 VLM 从本人指定的证件图提取结构化字段候选。

依赖 requirements-vlm.txt（与 requirements-ocr.txt 分开安装：rapidocr-openvino
钉死 openvino==2024.0.0，VLM 需要更新版本）。模型经 `fill.py vlm-setup` 下载到
data/models/，不随 skill 分发。依赖或模型缺失一律抛 VlmUnavailable，由调用方
回退到 openvino-ocr 路径；候选只作参考，经本人确认后才可写入保险柜。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

MODEL_ROOT = Path(__file__).resolve().parent.parent / "data" / "models"
DEFAULT_MODEL_ID = "OpenVINO/MiniCPM-V-2_6-int4-ov"
PROMPT = (
    "提取这张证件图片上的姓名、身份证号、手机号、住址。"
    '只输出一个 JSON 对象：{"name":"","id_number":"","phone":"","address":""}，'
    "没有的字段留空字符串，不要输出其他文字。"
)
FIELD_TYPES = {"name": "text", "id_number": "cn_id", "phone": "phone_cn", "address": "address"}


class VlmUnavailable(RuntimeError):
    """VLM 依赖、模型或推理不可用；调用方应回退到 openvino-ocr。"""


def _model_dir(model_id: str | None = None) -> Path:
    return MODEL_ROOT / (model_id or DEFAULT_MODEL_ID).replace("/", "--")


def setup(model_id: str | None = None) -> dict:
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise VlmUnavailable("缺少 huggingface_hub，请先 pip install -r requirements-vlm.txt") from exc
    repo = model_id or DEFAULT_MODEL_ID
    target = _model_dir(repo)
    target.mkdir(parents=True, exist_ok=True)
    snapshot_download(repo_id=repo, local_dir=str(target))
    return {"model": repo, "path": str(target)}


def _run_model(image_path: Path) -> str:
    try:
        from optimum.intel.openvino import OVModelForVisualCausalLM
        from transformers import AutoProcessor
    except ImportError as exc:
        raise VlmUnavailable("缺少 VLM 依赖，请 pip install -r requirements-vlm.txt（与 rapidocr 环境分开）") from exc
    target = _model_dir()
    if not target.is_dir():
        raise VlmUnavailable(f"本地模型未安装，请先运行 vlm-setup（期望目录 {target}）")
    try:
        from PIL import Image

        image = Image.open(str(image_path)).convert("RGB")
        processor = AutoProcessor.from_pretrained(str(target), trust_remote_code=True)
        model = OVModelForVisualCausalLM.from_pretrained(str(target), trust_remote_code=True)
        tokenizer = getattr(processor, "tokenizer", processor)
        answer = model.chat(msgs=[{"role": "user", "content": [image, PROMPT]}], tokenizer=tokenizer)
        if isinstance(answer, tuple):
            answer = answer[0]
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError("模型无文本输出")
        return answer
    except VlmUnavailable:
        raise
    except Exception as exc:
        raise VlmUnavailable(f"VLM 推理失败: {exc}") from exc


def _valid_flag(field: str, value: str):
    if field == "id_number":
        from fill_extract import validate_chinese_id

        return bool(validate_chinese_id(value))
    if field == "phone":
        return bool(re.fullmatch(r"1[3-9]\d{9}", value))
    return None


def extract_fields(image_path) -> dict:
    """返回与 fill_extract.extract_fields 相同的结构，confidence 标记为 vlm。"""
    text = _run_model(Path(image_path))
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise VlmUnavailable("VLM 输出不含 JSON 对象")
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise VlmUnavailable("VLM 输出 JSON 无法解析") from exc
    if not isinstance(parsed, dict):
        raise VlmUnavailable("VLM 输出 JSON 结构无效")
    fields: dict = {}
    candidates: dict = {}
    for field in FIELD_TYPES:
        value = parsed.get(field)
        if not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()
        candidate = {"value": value, "confidence": "vlm", "valid": _valid_flag(field, value)}
        fields[field] = {"value": value, "confidence": "vlm"}
        candidates[field] = [candidate]
    return {"fields": fields, "candidates": candidates, "ambiguous": []}
