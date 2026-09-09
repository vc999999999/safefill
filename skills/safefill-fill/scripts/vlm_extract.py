"""Pinned, offline OpenVINO VLM extraction for employee-selected images."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path
from unittest import mock

import collection
import secure_io

MANIFEST_NAME = "safefill-model-manifest.json"
PROMPT = (
    "提取这张证件图片上的姓名、身份证号、手机号、住址。"
    '只输出一个 JSON 对象：{"name":"","id_number":"","phone":"","address":""}，'
    "没有的字段留空字符串，不要输出其他文字。"
)
FIELD_TYPES = {"name": "text", "id_number": "cn_id", "phone": "phone_cn", "address": "address"}


class VlmUnavailable(RuntimeError):
    """The pinned model, dependencies or offline inference is unavailable."""


def _home() -> Path:
    return secure_io.checked_path(Path.home())


def model_root() -> Path:
    override = os.environ.get("YINTIAN_VLM_MODEL_DIR", "").strip()
    if override:
        return secure_io.checked_path(override)
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA", "").strip()
        if not root:
            raise VlmUnavailable("Windows 缺少 LOCALAPPDATA，无法确定模型缓存目录")
        return secure_io.checked_path(root) / "SafeFill" / "Cache" / "models"
    if sys.platform == "darwin":
        return _home() / "Library" / "Caches" / "SafeFill" / "models"
    root = os.environ.get("XDG_CACHE_HOME", "").strip()
    return (secure_io.checked_path(root) if root else _home() / ".cache") / "safefill" / "models"


def model_dir(model_id: str, revision: str | None = None) -> Path:
    if not isinstance(model_id, str) or not model_id.strip():
        raise VlmUnavailable("必须指定要使用的模型")
    identity = f"{model_id.strip()}\0{revision.strip() if revision else ''}".encode()
    return model_root() / hashlib.sha256(identity).hexdigest()[:20]


def _check_write_permissions(path: Path) -> None:
    if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o022:
        raise VlmUnavailable(f"模型路径可被其他账户写入，拒绝使用: {path}")


def _files(target: Path) -> list[Path]:
    result = []
    for path in target.rglob("*"):
        relative = path.relative_to(target)
        if path.name == MANIFEST_NAME:
            continue
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise VlmUnavailable(f"模型目录含符号链接，拒绝加载: {relative}")
        if stat.S_ISREG(info.st_mode):
            _check_write_permissions(path)
            result.append(path)
    return sorted(result)


def _sha256(path: Path) -> str:
    checked = secure_io.checked_path(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(checked, flags)
    digest = hashlib.sha256()
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise VlmUnavailable(f"模型文件不是普通文件: {path.name}")
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest(target: Path, model_id: str, revision: str | None) -> dict:
    return {
        "format": "safefill-model-manifest/1",
        "model_id": model_id,
        "revision": revision,
        "files": [{"path": path.relative_to(target).as_posix(), "size": path.stat().st_size,
                   "sha256": _sha256(path)} for path in _files(target)],
    }


def verify_model(model_id: str, revision: str | None = None, target: Path | None = None) -> dict:
    model_id = model_id.strip()
    revision = (revision or "").strip() or None
    target = secure_io.checked_path(target or model_dir(model_id, revision))
    manifest_path = target / MANIFEST_NAME
    if not target.is_dir() or not manifest_path.is_file():
        raise VlmUnavailable(f"所选模型未安装，请先运行 vlm-setup（期望目录 {target}）")
    _check_write_permissions(target.parent)
    _check_write_permissions(target)
    _check_write_permissions(manifest_path)
    try:
        expected = json.loads(secure_io.read_bytes(manifest_path, 4 * 1024 * 1024))
        if (expected.get("format") != "safefill-model-manifest/1"
                or expected.get("model_id") != model_id or expected.get("revision") != revision):
            raise ValueError
        actual = _manifest(target, model_id, revision)
        if actual != expected:
            raise ValueError
    except VlmUnavailable:
        raise
    except Exception:
        raise VlmUnavailable("模型完整性校验失败；请重新安装所选模型") from None
    return expected


def _snapshot_downloader(source: str):
    if source == "huggingface":
        try:
            from huggingface_hub import snapshot_download
        except ImportError as exc:
            raise VlmUnavailable("缺少 huggingface_hub，请在独立 VLM 环境安装 requirements-vlm.txt") from exc
        return snapshot_download, "repo_id"
    if source == "modelscope":
        try:
            from modelscope import snapshot_download
        except ImportError as exc:
            raise VlmUnavailable("缺少 modelscope，请先 pip install modelscope 后重试") from exc
        return snapshot_download, "model_id"
    raise VlmUnavailable(f"未知下载来源: {source}")


def setup(model_id: str, revision: str | None = None, source: str = "huggingface") -> dict:
    snapshot_download, id_key = _snapshot_downloader(source)
    model_id = model_id.strip()
    revision = (revision or "").strip() or None
    root, target = model_root(), model_dir(model_id, revision)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _check_write_permissions(root)
    with secure_io.file_lock(str(target) + ".setup.lock"):
        if target.exists():
            manifest = verify_model(model_id, revision, target)
            return {"model": model_id, "revision": revision, "path": str(target), "files": len(manifest["files"])}
        partial = target.with_name(target.name + ".partial")
        partial.mkdir(mode=0o700, exist_ok=True)
        try:
            options: dict[str, str] = {id_key: model_id, "local_dir": str(partial)}
            if revision:
                options["revision"] = revision
            snapshot_download(**options)
            shutil.rmtree(partial / ".cache", ignore_errors=True)
            manifest = _manifest(partial, model_id, revision)
            (partial / MANIFEST_NAME).unlink(missing_ok=True)
            secure_io.atomic_write(partial / MANIFEST_NAME, collection.canonical(manifest), overwrite=False)
            verify_model(model_id, revision, partial)
            partial.rename(target)
        except Exception as exc:
            raise VlmUnavailable(f"模型下载或校验失败: {exc}；已保留部分下载，重新运行 vlm-setup 将断点续传") from exc
    return {"model": model_id, "revision": revision, "path": str(target), "files": len(manifest["files"])}


def _run_model(image_path: Path, model_id: str, revision: str | None = None) -> str:
    try:
        import numpy as np
        import openvino as ov
        import openvino_genai as ov_genai
        from PIL import Image
    except ImportError as exc:
        raise VlmUnavailable("缺少固定 VLM 依赖，请在独立环境安装 requirements-vlm.txt") from exc
    target = model_dir(model_id, revision)
    verify_model(model_id, revision, target)
    device = os.environ.get("YINTIAN_VLM_DEVICE", "CPU").strip().upper()
    if device not in {"CPU", "GPU", "NPU", "AUTO"}:
        raise VlmUnavailable("YINTIAN_VLM_DEVICE 仅支持 CPU/GPU/NPU/AUTO")
    try:
        image = Image.open(str(image_path)).convert("RGB")
        array = np.asarray(image, dtype=np.uint8)[None, ...]
        with mock.patch.dict(os.environ, {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}):
            pipe = ov_genai.VLMPipeline(str(target), device)
            answer = pipe.generate(PROMPT, image=ov.Tensor(array), max_new_tokens=256)
        text = str(answer)
        if not text.strip():
            raise ValueError("模型无文本输出")
        return text
    except Exception as exc:
        raise VlmUnavailable(f"VLM 离线推理失败: {exc}") from exc


def _valid_flag(field: str, value: str):
    if field == "id_number":
        from fill_extract import validate_chinese_id

        return bool(validate_chinese_id(value))
    if field == "phone":
        return bool(re.fullmatch(r"1[3-9]\d{9}", value))
    return None


def extract_fields(image_path, model_id: str, revision: str | None = None) -> dict:
    text = _run_model(Path(image_path), model_id, revision)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise VlmUnavailable("VLM 输出不含 JSON 对象")
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise VlmUnavailable("VLM 输出不是有效 JSON") from exc
    if not isinstance(parsed, dict):
        raise VlmUnavailable("VLM 输出结构无效")
    candidates, fields = [], {}
    for name, entry_type in FIELD_TYPES.items():
        value = parsed.get(name, "")
        if not isinstance(value, str):
            raise VlmUnavailable(f"VLM 字段 {name} 不是文本")
        value = value.strip()
        if value:
            fields[name] = value
            candidate: dict = {"field": name, "type": entry_type, "value": value,
                               "confidence": "vlm", "valid": _valid_flag(name, value)}
            if candidate["valid"] is None:
                candidate["needs_review"] = True
            candidates.append(candidate)
    return {"fields": fields, "candidates": candidates, "ambiguous": []}
