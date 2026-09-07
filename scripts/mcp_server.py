"""隐填 · 只暴露密文和数据最小化收集管理能力的 MCP 服务。"""
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

import collection
import config
import privacy
from openvino_runtime import runtime_info

mcp = FastMCP("yintian-private-collection")


def scoped_path(path: str) -> str:
    if not config.VAULT_DIR:
        raise RuntimeError("MCP 启动前必须设置 YINTIAN_VAULT_DIR")
    return privacy.assert_in_vault(path)


@mcp.tool()
def openvino_status() -> dict[str, Any]:
    """检查本机 OpenVINO 版本和可用推理设备，不读取私密材料。"""
    return runtime_info()


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
    return collection.write_reports(scoped_path(task_dir), formats or ["xlsx", "json"])


if __name__ == "__main__":
    mcp.run()
