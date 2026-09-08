"""隐填 · 只暴露密文和数据最小化收集管理能力的 MCP 服务。"""
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

import cleanup
import collection
import config
import distribute
import privacy
from pathlib import Path
from openvino_runtime import runtime_info

mcp = FastMCP("yintian-private-collection")


def scoped_path(path: str) -> str:
    if not config.VAULT_DIR:
        raise RuntimeError("MCP 启动前必须设置 YINTIAN_VAULT_DIR")
    return privacy.assert_in_vault(path)


def _relativize(path_str: str, vault: Path) -> str:
    """MCP 边界脱敏：保险箱内路径转为相对 vault 的相对路径，保险箱外只保留文件名。"""
    path = Path(path_str)
    if not path.is_absolute():
        return path_str
    resolved = path.resolve()
    if resolved == vault or vault in resolved.parents:
        return str(resolved.relative_to(vault))
    return path.name


def _relativize_cleanup_result(result: dict[str, Any]) -> dict[str, Any]:
    vault = Path(config.VAULT_DIR).expanduser().resolve()
    for key in ("delete", "keep", "skipped_symlinks", "skipped_recheck", "errors", "deleted"):
        for item in result.get(key, []):
            if "path" in item:
                item["path"] = _relativize(item["path"], vault)
    result["scanned"] = [_relativize(path, vault) for path in result.get("scanned", [])]
    return result


@mcp.tool()
def openvino_status() -> dict[str, Any]:
    """检查本机 OpenVINO 版本和可用推理设备，不读取私密材料。"""
    try:
        return {**runtime_info(), 'available': True, 'required': False}
    except (ImportError, RuntimeError):
        return {'available': False, 'required': False, 'code': 'LOCAL_OCR_UNAVAILABLE',
                'next_step': 'auto 模板可使用本人授权的宿主 Agent 候选或本地人工复核；无需 API Key'}


@mcp.tool()
def ingest_encrypted_submissions(task_dir: str, submissions_dir: str) -> dict[str, Any]:
    """接收 .yintian 密文文件；只返回数量、重复和匿名错误引用。"""
    return collection.ingest_task(scoped_path(task_dir), scoped_path(submissions_dir))


@mcp.tool()
def collection_status(task_dir: str) -> dict[str, Any]:
    """返回不含人员明细的任务状态统计。"""
    return collection.status_task(scoped_path(task_dir))


@mcp.tool()
def generate_redacted_report(task_dir: str, formats: list[str] | None = None) -> dict[str, str]:
    """生成保留姓名/员工号、但不含提交敏感值的最小化进度报告。"""
    written = collection.write_reports(scoped_path(task_dir), formats or ["xlsx", "json"])
    vault = Path(config.VAULT_DIR).expanduser().resolve()
    return {fmt: _relativize(path, vault) for fmt, path in written.items()}


@mcp.tool()
def verify_invites(task_dir: str) -> dict[str, Any]:
    """校验任务目录下所有员工离线邀请单页文件是否完整且非空。"""
    return distribute.verify_invites(Path(scoped_path(task_dir)))


@mcp.tool()
def list_pending_reminders(task_dir: str) -> list[dict[str, Any]]:
    """查询尚未交回密文的员工名单并生成催交提醒文案（仅含姓名与工号，不含敏感数据）。"""
    return distribute.pending_reminders(Path(scoped_path(task_dir)))


@mcp.tool()
def cleanup_temp_files(task_dir: str | None = None, apply: bool = False) -> dict[str, Any]:
    """清理孤儿临时文件、死锁和 __pycache__；默认只返回 dry-run 计划，绝不删除名单、邀请、密文、报告等用户数据。"""
    scoped_path(config.VAULT_DIR)
    targets = [scoped_path(task_dir)] if task_dir else []
    result = cleanup.run_cleanup(targets, vault_dir=config.VAULT_DIR, apply=apply)
    return _relativize_cleanup_result(result)


if __name__ == "__main__":
    mcp.run()
