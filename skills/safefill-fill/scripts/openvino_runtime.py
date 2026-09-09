"""RapidOCR 1.x 的最小 OpenVINO 设备补丁与诊断。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import config


def _resolve_model_path(model_path: str) -> str:
    """YINTIAN_MODEL_DIR 设置时重定向到该目录下的同名文件，缺失时回退原路径。"""
    if not config.MODEL_DIR:
        return model_path
    candidate = Path(config.MODEL_DIR).expanduser() / Path(str(model_path)).name
    if candidate.is_file():
        return str(candidate)
    ir_candidate = candidate.with_suffix(".xml")
    if ir_candidate.is_file():
        return str(ir_candidate)
    return model_path


def _core_type():
    try:
        from openvino import Core
    except ImportError:
        try:
            from openvino.runtime import Core
        except ImportError as exc:
            raise RuntimeError(
                "未安装 OpenVINO 后端。请执行 pip install -r requirements-ocr.txt；"
                "纯文本材料无需 OCR 依赖。"
            ) from exc
    return Core


def runtime_info() -> dict[str, Any]:
    """返回 OpenVINO 版本、可用设备和选定设备，不加载 OCR 模型。"""
    Core = _core_type()
    try:
        import openvino

        version = getattr(openvino, "__version__", "unknown")
    except ImportError:
        version = "unknown"
    core = Core()
    devices = list(core.available_devices)
    details = []
    for device in devices:
        try:
            name = str(core.get_property(device, "FULL_DEVICE_NAME"))
        except Exception:
            name = device
        details.append({"id": device, "name": name})
    return {
        "openvino_version": version,
        "requested_device": config.OCR_DEVICE,
        "available_devices": details,
    }


def install_rapidocr_device_patch() -> None:
    """让 rapidocr-openvino 1.4.4 真正使用 CPU/GPU/NPU/AUTO。"""
    from rapidocr_openvino import utils
    from rapidocr_openvino.utils import infer_engine

    if getattr(infer_engine, "_yintian_patched", False):
        return

    Core = _core_type()
    original = infer_engine.OpenVINOInferSession

    class DeviceSession(original):
        def __init__(self, cfg):
            core = Core()
            model_path = _resolve_model_path(cfg["model_path"])
            self._verify_model(model_path)
            model = core.read_model(model_path)
            self.compiled_model = core.compile_model(model, config.OCR_DEVICE)
            self.session = self.compiled_model.create_infer_request()
            try:
                used = self.compiled_model.get_property("EXECUTION_DEVICES")
                self.execution_devices = [str(item) for item in used]
            except Exception:
                self.execution_devices = [config.OCR_DEVICE]

    from rapidocr_openvino.ch_ppocr_cls import text_cls
    from rapidocr_openvino.ch_ppocr_det import text_detect
    from rapidocr_openvino.ch_ppocr_rec import text_recognize

    infer_engine.OpenVINOInferSession = DeviceSession
    utils.OpenVINOInferSession = DeviceSession
    text_detect.OpenVINOInferSession = DeviceSession
    text_cls.OpenVINOInferSession = DeviceSession
    text_recognize.OpenVINOInferSession = DeviceSession
    infer_engine._yintian_patched = True
