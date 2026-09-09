"""cleanup.py 的 pytest 回归检查；也可直接 `python scripts/test_cleanup.py` 运行。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import cleanup
import config
import privacy

OLD = time.time() - 48 * 3600
SCRIPTS_DIR = Path(__file__).resolve().parent


def set_old_mtime(path: Path) -> None:
    os.utime(path, (OLD, OLD))


def under(tmp_path: Path, items: list[dict]) -> list[dict]:
    prefix = str(tmp_path)
    return [item for item in items if item["path"].startswith(prefix)]


def make_task_dir(tmp_path: Path) -> Path:
    task = tmp_path / "YT-20260101-ABC123"
    (task / "submissions").mkdir(parents=True)
    (task / "reports").mkdir()
    return task


def test_orphan_temp_detected(tmp_path):
    task = make_task_dir(tmp_path)
    orphan = task / "tmpab12_cd3"
    orphan.write_bytes(b"partial")
    set_old_mtime(orphan)
    result = cleanup.run_cleanup([str(task)])
    deleted = under(tmp_path, result["delete"])
    assert [Path(item["path"]).name for item in deleted] == ["tmpab12_cd3"]
    assert deleted[0]["category"] == "孤儿临时文件" and deleted[0]["size"] == 7


def test_fresh_temp_kept_by_threshold(tmp_path):
    task = make_task_dir(tmp_path)
    fresh = task / "tmpzz99_yx8"
    fresh.write_bytes(b"writing")
    result = cleanup.run_cleanup([str(task)], stale_hours=24)
    assert not under(tmp_path, result["delete"])
    kept = under(tmp_path, result["keep"])
    assert any(item["category"] == "疑似临时文件" for item in kept)


def test_live_lock_kept(tmp_path):
    task = make_task_dir(tmp_path)
    lock = task / ".write.lock"
    lock.write_text(f"pid={os.getpid()} time=2026-09-08T00:00:00Z\n", encoding="utf-8")
    result = cleanup.run_cleanup([str(task)])
    assert not under(tmp_path, result["delete"])
    kept = under(tmp_path, result["keep"])
    assert any(item["category"] == "写入锁" and "仍在运行" in item["guidance"] for item in kept)


def test_dead_lock_deleted(tmp_path):
    task = make_task_dir(tmp_path)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    lock = task / ".write.lock"
    lock.write_text(f"pid={dead.pid} time=2026-09-08T00:00:00Z\n", encoding="utf-8")
    result = cleanup.run_cleanup([str(task)], apply=True)
    deleted = under(tmp_path, result["deleted"])
    assert any(Path(item["path"]).name == ".write.lock" for item in deleted)
    assert not lock.exists()


def test_unparseable_lock_kept(tmp_path):
    task = make_task_dir(tmp_path)
    (task / ".write.lock").write_text("garbage\n", encoding="utf-8")
    result = cleanup.run_cleanup([str(task)])
    assert not under(tmp_path, result["delete"])
    assert any(item["category"] == "写入锁" for item in under(tmp_path, result["keep"]))


def test_never_delete_set(tmp_path):
    task = make_task_dir(tmp_path)
    protected = [
        task / "submissions" / "INV-AAAAAAAAAA" / "v0001_abcdef012345.yintian",
        task / "reports" / "progress.xlsx",
        task / "reports" / "progress.json",
        task / "roster.csv",
        task / "invite-index.csv",
        task / "INV-AAAAAAAAAA.yintian-form",
        task / "task.json",
        task / "state.sqlite3",
        task / "private.pem.enc",
        tmp_path / "task.yintian-task",
        tmp_path / "received" / "x.yintian",
    ]
    protected[0].parent.mkdir(parents=True)
    protected[10].parent.mkdir(exist_ok=True)
    for path in protected:
        path.write_bytes(b"data")
    result = cleanup.run_cleanup([str(tmp_path)], apply=True)
    assert not under(tmp_path, result.get("deleted", []))
    for path in protected:
        assert path.exists(), f"用户数据被误删: {path}"
    kept = under(tmp_path, result["keep"])
    kept_names = {Path(item["path"]).name for item in kept}
    for name in ("progress.xlsx", "roster.csv", "invite-index.csv", "INV-AAAAAAAAAA.yintian-form", "task.json",
                 "state.sqlite3", "private.pem.enc", "task.yintian-task", "received", "submissions"):
        assert name in kept_names, f"保留清单缺少: {name}"


def test_pycache_cleaned(tmp_path):
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    pyc = cache / "module.cpython-311.pyc"
    pyc.write_bytes(b"cache")
    result = cleanup.run_cleanup([str(tmp_path)], apply=True)
    assert not pyc.exists()
    assert any(Path(item["path"]) == pyc for item in result["deleted"])


def test_symlink_never_deleted(tmp_path):
    task = make_task_dir(tmp_path)
    target = task / "task.json"
    target.write_bytes(b"{}")
    link = task / "tmplink_001"
    link.symlink_to(target)
    set_old_mtime(target)
    result = cleanup.run_cleanup([str(task)], apply=True)
    assert link.exists() and target.exists()
    assert any(Path(item["path"]) == link for item in result["skipped_symlinks"])
    assert not under(tmp_path, result.get("deleted", []))


def test_outside_vault_rejected(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        cleanup.run_cleanup([str(outside)], vault_dir=str(vault))
    except privacy.PrivacyViolation:
        pass
    else:
        raise AssertionError("保险箱外路径未被拒绝")
    finally:
        config.VAULT_DIR = ""


def test_explicit_paths_required_without_vault():
    try:
        cleanup.run_cleanup([], vault_dir="")
    except RuntimeError:
        pass
    else:
        raise AssertionError("未配置保险箱且未给路径时不应默认扫描")


def test_dry_run_changes_nothing(tmp_path):
    task = make_task_dir(tmp_path)
    orphan = task / "tmpab12_cd3"
    orphan.write_bytes(b"partial")
    set_old_mtime(orphan)
    result = cleanup.run_cleanup([str(task)], apply=False)
    assert under(tmp_path, result["delete"])
    assert orphan.exists(), "dry-run 不应删除任何文件"


def test_json_cli_structure(tmp_path):
    task = make_task_dir(tmp_path)
    orphan = task / "tmpab12_cd3"
    orphan.write_bytes(b"partial")
    set_old_mtime(orphan)
    output = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "cleanup.py"), str(task), "--json"],
        capture_output=True, text=True, check=True,
    )
    result = json.loads(output.stdout)
    assert result["apply"] is False
    assert set(result) >= {"apply", "scanned", "delete", "keep", "skipped_symlinks", "errors"}
    assert any(Path(item["path"]).name == "tmpab12_cd3" for item in result["delete"])
    entry = result["delete"][0]
    assert set(entry) >= {"path", "size", "mtime", "reason", "category"}


def test_scripts_pycache_not_scanned_by_default(tmp_path):
    """默认（无保险箱、显式路径未覆盖安装目录）不扫脚本安装目录的 __pycache__。"""
    task = make_task_dir(tmp_path)
    orphan = task / "tmpab12_cd3"
    orphan.write_bytes(b"partial")
    set_old_mtime(orphan)
    result = cleanup.run_cleanup([str(task)], vault_dir="")
    assert all(not str(path).startswith(str(SCRIPTS_DIR)) for path in result["scanned"])
    for key in ("delete", "keep"):
        assert all(not item["path"].startswith(str(SCRIPTS_DIR)) for item in result[key]), f"{key} 泄露安装目录路径"


def test_should_scan_scripts_cache_rules(tmp_path):
    cache = tmp_path / "pkg" / "__pycache__"
    cache.mkdir(parents=True)
    other = tmp_path / "task"
    other.mkdir()
    # 无保险箱且显式路径未覆盖 → 不扫
    assert cleanup.should_scan_scripts_cache(cache, [other], None) is False
    # 无保险箱但显式路径覆盖 → 扫
    assert cleanup.should_scan_scripts_cache(cache, [tmp_path / "pkg"], None) is True
    # 位于已配置保险箱内 → 扫
    assert cleanup.should_scan_scripts_cache(cache, [other], str(tmp_path)) is True
    # 保险箱不包含它 → 不扫
    vault = tmp_path / "vault"
    vault.mkdir()
    assert cleanup.should_scan_scripts_cache(cache, [other], str(vault)) is False


def test_scripts_cache_scanned_only_when_covered(tmp_path):
    """伪造安装目录缓存：vault 覆盖时纳入扫描，未覆盖时不纳入。"""
    vault = tmp_path / "vault"
    (vault / "task").mkdir(parents=True)
    fake_cache = vault / "skill" / "scripts" / "__pycache__"
    fake_cache.mkdir(parents=True)
    pyc = fake_cache / "m.cpython-39.pyc"
    pyc.write_bytes(b"cache")
    original = cleanup.SCRIPTS_CACHE_DIR
    cleanup.SCRIPTS_CACHE_DIR = fake_cache
    try:
        covered = cleanup.run_cleanup([str(vault / "task")], vault_dir=str(vault))
        assert any(Path(item["path"]) == pyc for item in covered["delete"])

        outside_task = tmp_path / "elsewhere"
        outside_task.mkdir()
        not_covered = cleanup.run_cleanup([str(outside_task)], vault_dir="")
        assert all(Path(item["path"]) != pyc for item in not_covered["delete"])

        explicit = cleanup.run_cleanup([str(fake_cache.parent)], vault_dir="")
        assert any(Path(item["path"]) == pyc for item in explicit["delete"])
    finally:
        cleanup.SCRIPTS_CACHE_DIR = original


def test_apply_recheck_skips_symlink_swap(tmp_path):
    """扫描后、删除前条目被换成符号链接：复查跳过且不删除。"""
    task = make_task_dir(tmp_path)
    orphan = task / "tmpab12_cd3"
    orphan.write_bytes(b"partial")
    set_old_mtime(orphan)
    result = cleanup.run_cleanup([str(task)])
    assert under(tmp_path, result["delete"])
    orphan.unlink()
    orphan.symlink_to(task / "task.json")
    cleanup.apply_deletions(result, 24 * 3600)
    assert not result["deleted"]
    assert any(Path(item["path"]) == orphan for item in result["skipped_recheck"])
    assert orphan.is_symlink(), "符号链接不应被删除"


def test_apply_recheck_skips_freshened_orphan(tmp_path):
    """扫描后孤儿临时文件被重新写入（mtime 变新）：复查跳过。"""
    task = make_task_dir(tmp_path)
    orphan = task / "tmpab12_cd3"
    orphan.write_bytes(b"partial")
    set_old_mtime(orphan)
    result = cleanup.run_cleanup([str(task)])
    assert under(tmp_path, result["delete"])
    orphan.write_bytes(b"active write")
    cleanup.apply_deletions(result, 24 * 3600)
    assert not result["deleted"]
    assert orphan.exists(), "正在写入的文件不应被删除"
    assert any("刚被修改" in item["reason"] for item in result["skipped_recheck"])


def test_apply_recheck_skips_revived_lock(tmp_path):
    """扫描后死锁被活进程重建：复查跳过。"""
    task = make_task_dir(tmp_path)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    lock = task / ".write.lock"
    lock.write_text(f"pid={dead.pid} time=2026-09-08T00:00:00Z\n", encoding="utf-8")
    result = cleanup.run_cleanup([str(task)])
    assert any(Path(item["path"]) == lock for item in under(tmp_path, result["delete"]))
    lock.write_text(f"pid={os.getpid()} time=2026-09-08T00:00:00Z\n", encoding="utf-8")
    cleanup.apply_deletions(result, 24 * 3600)
    assert not result["deleted"]
    assert lock.exists(), "活进程的锁不应被删除"
    assert any(Path(item["path"]) == lock for item in result["skipped_recheck"])


def test_resolve_task_dirs_does_not_mutate_global_vault(tmp_path):
    """库式调用不得改写 config.VAULT_DIR 模块级全局。"""
    vault = tmp_path / "vault"
    task = vault / "task"
    task.mkdir(parents=True)
    original = config.VAULT_DIR
    try:
        config.VAULT_DIR = ""
        cleanup.run_cleanup([str(task)], vault_dir=str(vault))
        assert config.VAULT_DIR == "", "run_cleanup 改写了 config.VAULT_DIR"
    finally:
        config.VAULT_DIR = original


def _import_mcp_server_with_stub():
    """本机装不上 mcp 库，用 stub 的 FastMCP 导入 mcp_server 以验证其逻辑。"""
    import importlib
    import types

    names = ("mcp", "mcp.server", "mcp.server.fastmcp", "mcp_server")
    saved = {name: sys.modules.get(name) for name in names}
    sys.modules.pop("mcp_server", None)

    fake_fastmcp = types.ModuleType("mcp.server.fastmcp")

    class FakeFastMCP:
        def __init__(self, name):
            self.name = name
            self.tools = {}

        def tool(self):
            def decorator(fn):
                self.tools[fn.__name__] = fn
                return fn
            return decorator

        def run(self):
            raise RuntimeError("stub 不支持运行")

    fake_fastmcp.FastMCP = FakeFastMCP
    fake_server = types.ModuleType("mcp.server")
    fake_server.fastmcp = fake_fastmcp
    fake_mcp = types.ModuleType("mcp")
    fake_mcp.server = fake_server
    sys.modules["mcp"] = fake_mcp
    sys.modules["mcp.server"] = fake_server
    sys.modules["mcp.server.fastmcp"] = fake_fastmcp
    return importlib.import_module("mcp_server"), saved


def _restore_modules(saved: dict) -> None:
    for name, module in saved.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


def test_mcp_cleanup_result_paths_relativized(tmp_path):
    """MCP 边界的 cleanup_temp_files 返回路径一律相对化，不泄露绝对路径。"""
    module, saved = _import_mcp_server_with_stub()
    original_vault = config.VAULT_DIR
    try:
        vault = tmp_path / "vault"
        task = vault / "task"
        task.mkdir(parents=True)
        orphan = task / "tmpab12_cd3"
        orphan.write_bytes(b"partial")
        set_old_mtime(orphan)
        config.VAULT_DIR = str(vault)
        result = module.cleanup_temp_files(task_dir=str(task), apply=False)
        assert result["delete"], "dry-run 应计划删除孤儿文件"
        assert all(not Path(path).is_absolute() for path in result["scanned"])
        for key in ("delete", "keep", "skipped_symlinks", "skipped_recheck", "errors"):
            for item in result[key]:
                assert not Path(item["path"]).is_absolute(), f"{key} 泄露绝对路径: {item['path']}"
        assert [Path(item["path"]).name for item in result["delete"]] == ["tmpab12_cd3"]
        assert result["delete"][0]["path"].startswith("task")
    finally:
        config.VAULT_DIR = original_vault
        _restore_modules(saved)


def main() -> None:
    tests = [(name, func) for name, func in sorted(globals().items()) if name.startswith("test_")]
    for name, func in tests:
        if "tmp_path" in func.__code__.co_varnames[: func.__code__.co_argcount]:
            with tempfile.TemporaryDirectory() as tmp:
                func(Path(tmp))
        else:
            func()
        print(f"PASS: {name}")
    print(f"共 {len(tests)} 项检查全部通过")


if __name__ == "__main__":
    main()
