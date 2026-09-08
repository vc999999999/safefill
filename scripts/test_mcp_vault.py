"""privacy 保险箱边界与 MCP 工具面测试。支持直接运行，亦支持 pytest 收集。"""
from __future__ import annotations

import asyncio
import inspect
import os
import tempfile
from pathlib import Path

import config
import privacy

try:
    import pytest

    @pytest.fixture
    def vault(tmp_path: Path, monkeypatch) -> Path:
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir(exist_ok=True)
        monkeypatch.setattr(config, "VAULT_DIR", str(vault_dir))
        return vault_dir

except ImportError:
    pytest = None  # type: ignore


class _McpUnavailable(RuntimeError):
    """直跑入口下标记 mcp 依赖缺失（pytest 下走 pytest.skip）。"""


_SKIP_EXCEPTIONS: tuple = (_McpUnavailable,)
if pytest is not None:
    _SKIP_EXCEPTIONS = _SKIP_EXCEPTIONS + (pytest.skip.Exception,)


def _require_mcp_server():
    """导入 mcp_server；缺失时 pytest 下按用例级 skip，直跑入口抛 _McpUnavailable。"""
    try:
        import mcp_server
    except ImportError:
        if pytest is not None:
            pytest.skip("未安装 mcp 包，跳过 MCP 相关用例")
        raise _McpUnavailable("未安装 mcp 包")
    return mcp_server


def test_assert_in_vault_accepts_paths_inside(vault: Path) -> None:
    assert privacy.assert_in_vault(str(vault / "received")) == str(vault / "received")
    assert privacy.assert_in_vault(str(vault / "nested" / ".." / "file.txt")) == str(vault / "nested" / ".." / "file.txt")
    assert privacy.assert_in_vault(str(vault)) == str(vault)


def test_assert_in_vault_rejects_dotdot_escape(vault: Path, tmp_path: Path) -> None:
    try:
        privacy.assert_in_vault(str(vault / ".." / "outside"))
        raise AssertionError("未拒绝 .. 逃逸")
    except privacy.PrivacyViolation:
        pass

    try:
        privacy.assert_in_vault(str(tmp_path / "outside"))
        raise AssertionError("未拒绝外部路径")
    except privacy.PrivacyViolation:
        pass


def test_assert_in_vault_rejects_symlink_escape(vault: Path, tmp_path: Path) -> None:
    if os.name == "nt":
        return
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    link = vault / "link"
    if not link.exists():
        link.symlink_to(outside)
    try:
        privacy.assert_in_vault(str(link / "secret.txt"))
        raise AssertionError("未拒绝符号链接逃逸")
    except privacy.PrivacyViolation:
        pass


def test_assert_in_vault_without_vault_configured(tmp_path: Path) -> None:
    orig = config.VAULT_DIR
    try:
        config.VAULT_DIR = ""
        anywhere = str(tmp_path / "anywhere")
        assert privacy.assert_in_vault(anywhere) == anywhere
    finally:
        config.VAULT_DIR = orig


def test_scoped_path_requires_vault_and_stays_inside(vault: Path) -> None:
    mcp_server = _require_mcp_server()

    orig = config.VAULT_DIR
    try:
        config.VAULT_DIR = ""
        try:
            mcp_server.scoped_path(str(vault))
            raise AssertionError("未设置 VAULT_DIR 时未报错")
        except RuntimeError:
            pass

        config.VAULT_DIR = str(vault)
        inside = str(vault / "task")
        assert mcp_server.scoped_path(inside) == inside
        try:
            mcp_server.scoped_path(str(vault / ".." / "outside"))
            raise AssertionError("越界路径未报错")
        except privacy.PrivacyViolation:
            pass
    finally:
        config.VAULT_DIR = orig


def _tool_names(server) -> set[str]:
    manager = getattr(server, "_tool_manager", None)
    if manager is not None:
        return {tool.name for tool in manager.list_tools()}
    tools = asyncio.run(server.get_tools())
    if isinstance(tools, dict):
        return set(tools)
    return {tool.name for tool in tools}


def test_mcp_tool_inventory_is_minimal() -> None:
    mcp_server = _require_mcp_server()

    names = _tool_names(mcp_server.mcp)
    required = {
        "openvino_status",
        "ingest_encrypted_submissions",
        "collection_status",
        "generate_redacted_report",
        "verify_invites",
        "list_pending_reminders",
        "cleanup_temp_files",
    }
    assert required <= names, f"MCP 缺少必要工具: {required - names}"
    forbidden = {name for name in names if any(word in name for word in ("create", "review", "reveal", "purge"))}
    assert not forbidden, f"MCP 不应暴露敏感操作工具: {sorted(forbidden)}"


def main() -> None:
    tests = [
        (name, fn)
        for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]
    skipped = []
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp_path = Path(tmp_str)
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()

        orig = config.VAULT_DIR
        try:
            config.VAULT_DIR = str(vault_dir)
            for name, fn in tests:
                kwargs = {}
                params = inspect.signature(fn).parameters
                if "vault" in params:
                    kwargs["vault"] = vault_dir
                if "tmp_path" in params:
                    kwargs["tmp_path"] = tmp_path
                try:
                    fn(**kwargs)
                except _SKIP_EXCEPTIONS as exc:
                    skipped.append(name)
                    print(f"SKIP: {name} ({exc})")
                else:
                    print(f"PASS: {name}")
        finally:
            config.VAULT_DIR = orig

    print(f"test_mcp_vault: {len(tests) - len(skipped)} 项通过，{len(skipped)} 项跳过。")


if __name__ == "__main__":
    main()
