"""隐填 · 阶段结束后的临时产物清理；默认 dry-run，绝不删除用户数据。"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import config
import privacy

# collection.py 的 atomic_write 使用 tempfile.NamedTemporaryFile(delete=False)，
# 默认命名为 tmp 加 8 位小写字母/数字/下划线；进程中断时可能留下孤儿临时文件。
ORPHAN_TEMP_RE = re.compile(r"^tmp[a-z0-9_]{8}$")
LOCK_NAME = ".write.lock"
LOCK_PID_RE = re.compile(r"^pid=(\d+)\s+time=(\S+)")
PYCACHE_DIR = "__pycache__"
SCRIPTS_CACHE_DIR = Path(__file__).resolve().parent / PYCACHE_DIR

GUIDE_PURGE = "属于任务数据；任务到期后由用户在独立终端运行 purge 删除整个任务目录"
GUIDE_HANDOFF = "交接包由 export-task 生成；确认对方 import-task 成功后由用户手动删除"
GUIDE_MANUAL = "包含收件或报告等用户数据；请用户确认已归档后手动删除"


def keep_rule(path: Path) -> tuple[str, str] | None:
    """识别永不删除的用户数据，返回 (理由, 处理指引) 或 None。"""
    name = path.name
    if name == "task.json":
        return "任务定义", GUIDE_PURGE
    if name == "state.sqlite3":
        return "任务状态数据库", GUIDE_PURGE
    if name == "private.pem.enc":
        return "加密的任务私钥", GUIDE_PURGE
    if name == "invite-index.csv":
        return "邀请索引", GUIDE_PURGE
    if name.startswith("roster") and path.suffix == ".csv":
        return "名单文件", GUIDE_PURGE
    if re.fullmatch(r"INV-[A-Z0-9]{10}\.html", name):
        return "个人邀请页", GUIDE_PURGE
    if path.suffix == ".yintian":
        return "员工密文提交", GUIDE_PURGE
    if path.suffix == ".yintian-task":
        return "任务交接包", GUIDE_HANDOFF
    if path.suffix in {".xlsx", ".json"} and any(part == "reports" for part in path.parts):
        return "进度报告", GUIDE_MANUAL
    return None


def pid_alive(pid: int) -> bool | None:
    """判断 pid 是否存活；无法判断时返回 None。Windows 不用 os.kill(pid, 0)，避免误终止进程。"""
    if pid <= 0:
        return None
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return None
            try:
                code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return None
                return code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def judge_lock(path: Path) -> tuple[str, str]:
    """判定 .write.lock 是否可删；返回 (delete|keep, 理由)。"""
    try:
        first_line = path.read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except (OSError, IndexError):
        return "keep", "锁文件无法读取，保守保留"
    match = LOCK_PID_RE.match(first_line)
    if not match:
        return "keep", "锁文件未记录 pid，无法确认写入进程已退出，保守保留"
    alive = pid_alive(int(match.group(1)))
    if alive is False:
        return "delete", f"写入进程 pid={match.group(1)} 已退出，是死锁残留"
    if alive is True:
        return "keep", f"写入进程 pid={match.group(1)} 仍在运行，任务可能正在写入"
    return "keep", f"无法确认 pid={match.group(1)} 是否存活，保守保留"


def describe_entry(path: Path, stat_result: os.stat_result) -> dict:
    return {
        "path": str(path),
        "size": stat_result.st_size,
        "mtime": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(stat_result.st_mtime)),
    }


def scan_root(root: Path, stale_seconds: float, result: dict) -> None:
    """递归扫描一个目录，不跟随符号链接，只识别可删除的临时产物和永不删除的用户数据。"""
    try:
        entries = sorted(os.scandir(root), key=lambda entry: entry.name)
    except OSError as exc:
        result["errors"].append({"path": str(root), "error": str(exc)})
        return
    now = time.time()
    for entry in entries:
        path = Path(root) / entry.name
        try:
            if entry.is_symlink():
                result["skipped_symlinks"].append({"path": str(path), "reason": "符号链接一律不跟随、不删除"})
                continue
            if entry.is_dir(follow_symlinks=False):
                if entry.name in {"submissions", "received"}:
                    result["keep"].append({"path": str(path), "category": "收件目录", "guidance": GUIDE_MANUAL})
                    continue
                scan_root(path, stale_seconds, result)
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            stat_result = entry.stat(follow_symlinks=False)
        except OSError as exc:
            result["errors"].append({"path": str(path), "error": str(exc)})
            continue
        rule = keep_rule(path)
        if rule:
            reason, guidance = rule
            result["keep"].append({**describe_entry(path, stat_result), "category": reason, "guidance": guidance})
            continue
        if entry.name == LOCK_NAME:
            verdict, reason = judge_lock(path)
            if verdict == "delete":
                result["delete"].append({**describe_entry(path, stat_result), "category": "死锁文件", "reason": reason})
            else:
                result["keep"].append({**describe_entry(path, stat_result), "category": "写入锁", "guidance": reason})
            continue
        if ORPHAN_TEMP_RE.fullmatch(entry.name):
            age = now - stat_result.st_mtime
            if age >= stale_seconds:
                result["delete"].append({
                    **describe_entry(path, stat_result),
                    "category": "孤儿临时文件",
                    "reason": f"匹配 atomic_write 临时文件命名且超过 {age / 3600:.1f} 小时未变，是中断写入的残留",
                })
            else:
                result["keep"].append({
                    **describe_entry(path, stat_result),
                    "category": "疑似临时文件",
                    "guidance": "命名像 atomic_write 临时文件但修改时间过新，可能正在写入，保守保留",
                })
            continue
        if entry.name.endswith(".pyc") and path.parent.name == PYCACHE_DIR:
            result["delete"].append({
                **describe_entry(path, stat_result),
                "category": "解释器缓存",
                "reason": "__pycache__ 下的 .pyc 可由 Python 自动重新生成",
            })


def resolve_task_dirs(task_dirs: list[str], vault_dir: str | None) -> list[Path]:
    """校验并展开扫描目标；vault 已配置时所有路径必须在保险箱内。只读配置，不改写 config.VAULT_DIR 全局。"""
    vault = vault_dir if vault_dir is not None else config.VAULT_DIR
    if not task_dirs:
        if not vault:
            raise RuntimeError("未指定 TASK_DIR 且未配置 YINTIAN_VAULT_DIR；请显式给出要扫描的目录，绝不默认扫描当前目录")
        vault_root = Path(vault).expanduser().resolve()
        try:
            children = sorted(path for path in vault_root.iterdir() if path.is_dir() and not path.is_symlink())
        except OSError as exc:
            raise RuntimeError(f"无法读取保险箱目录: {exc}") from exc
        if not children:
            raise RuntimeError("保险箱目录下没有可扫描的任务目录")
        return children
    resolved = []
    for raw in task_dirs:
        if vault:
            privacy.assert_in_vault(raw, vault)
        path = Path(raw).expanduser()
        if not path.is_dir():
            raise RuntimeError(f"不是目录或不存在: {raw}")
        resolved.append(path)
    return resolved


def should_scan_scripts_cache(cache_dir: Path, roots: list[Path], vault: str | None) -> bool:
    """脚本安装目录的 __pycache__ 默认不扫：仅当它位于已配置保险箱内，
    或未配置保险箱时显式给出的扫描目录覆盖了它，才纳入扫描集。"""
    try:
        cache = cache_dir.resolve()
    except OSError:
        return False
    if vault:
        vault_root = Path(vault).expanduser().resolve()
        return cache == vault_root or vault_root in cache.parents
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if cache == resolved or resolved in cache.parents:
            return True
    return False


def recheck_deletable(item: dict, stale_seconds: float, now: float) -> str | None:
    """apply 删除前逐项重判，消除扫描与删除之间的 TOCTOU；返回 None 表示仍可删除，否则返回跳过理由。"""
    path = Path(item["path"])
    if path.is_symlink():
        return "删除前复查发现它已变成符号链接"
    try:
        stat_result = path.stat()
    except OSError as exc:
        return f"删除前复查无法读取状态: {exc}"
    if not path.is_file():
        return "删除前复查发现它不再是普通文件"
    category = item.get("category")
    if category == "死锁文件":
        verdict, reason = judge_lock(path)
        if verdict != "delete":
            return f"删除前复查锁文件状态变化：{reason}"
    elif category == "孤儿临时文件":
        if not ORPHAN_TEMP_RE.fullmatch(path.name):
            return "删除前复查发现文件名不再匹配孤儿临时文件特征"
        if now - stat_result.st_mtime < stale_seconds:
            return "删除前复查发现文件刚被修改，可能正在写入"
    elif category == "解释器缓存":
        if not (path.name.endswith(".pyc") and path.parent.name == PYCACHE_DIR):
            return "删除前复查发现它不再是 __pycache__ 下的 .pyc"
    else:
        return f"删除前复查遇到未知类别: {category}"
    return None


def apply_deletions(result: dict, stale_seconds: float) -> None:
    """对计划逐项复查后真正删除；复查不通过的条目跳过并记入 skipped_recheck。"""
    deleted = []
    now = time.time()
    for item in result["delete"]:
        reason = recheck_deletable(item, stale_seconds, now)
        if reason is not None:
            result["skipped_recheck"].append({"path": item["path"], "category": item.get("category", ""), "reason": reason})
            continue
        try:
            Path(item["path"]).unlink()
        except OSError as exc:
            result["errors"].append({"path": item["path"], "error": str(exc)})
            continue
        item["deleted"] = True
        deleted.append(item)
    result["deleted"] = deleted


def run_cleanup(task_dirs: list[str], vault_dir: str | None = None, stale_hours: float = 24.0, apply: bool = False) -> dict:
    """CLI 和 MCP 共用的核心逻辑；默认只生成计划，apply=True 才真正删除。"""
    vault = vault_dir if vault_dir is not None else config.VAULT_DIR
    roots = resolve_task_dirs(task_dirs, vault_dir)
    result: dict = {
        "apply": apply,
        "stale_lock_hours": stale_hours,
        "scanned": [str(path) for path in roots],
        "delete": [],
        "keep": [],
        "skipped_symlinks": [],
        "skipped_recheck": [],
        "errors": [],
    }
    for root in roots:
        scan_root(root, stale_hours * 3600, result)
    if (
        SCRIPTS_CACHE_DIR.is_dir()
        and not SCRIPTS_CACHE_DIR.is_symlink()
        and should_scan_scripts_cache(SCRIPTS_CACHE_DIR, roots, vault)
    ):
        scan_root(SCRIPTS_CACHE_DIR, stale_hours * 3600, result)
    if apply:
        apply_deletions(result, stale_hours * 3600)
    return result


def format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GB"


def print_plan(result: dict) -> None:
    action = "已删除" if result["apply"] else "将删除（dry-run，未改动任何文件）"
    print(f"== {action} ==")
    removed = result.get("deleted", result["delete"])
    if removed:
        for item in removed:
            detail = f"  {item['path']}  {format_bytes(item.get('size', 0))}  mtime={item.get('mtime', '-')}"
            print(detail + f"\n    [{item['category']}] {item.get('reason', '')}")
    else:
        print("  （无）")
    print("== 保留（永不删除的用户数据或无法确认的条目）==")
    if result["keep"]:
        for item in result["keep"]:
            detail = f"  {item['path']}"
            if "size" in item:
                detail += f"  {format_bytes(item['size'])}  mtime={item.get('mtime', '-')}"
            print(detail + f"\n    [{item['category']}] {item['guidance']}")
    else:
        print("  （无）")
    if result["skipped_symlinks"]:
        print("== 跳过的符号链接 ==")
        for item in result["skipped_symlinks"]:
            print(f"  {item['path']}\n    {item['reason']}")
    if result.get("skipped_recheck"):
        print("== 删除前复查后跳过 ==")
        for item in result["skipped_recheck"]:
            print(f"  {item['path']}\n    {item['reason']}")
    if result["errors"]:
        print("== 错误 ==")
        for item in result["errors"]:
            print(f"  {item['path']}: {item['error']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="隐填：清理孤儿临时文件、死锁和 __pycache__；默认 dry-run，绝不删除名单、邀请、密文、报告等用户数据"
    )
    parser.add_argument("task_dirs", nargs="*", metavar="TASK_DIR", help="要扫描的任务目录；为空且已配置保险箱时扫描保险箱下一层目录")
    parser.add_argument("--vault", help="保险箱目录；默认取 YINTIAN_VAULT_DIR，配置后所有 TASK_DIR 必须在保险箱内")
    parser.add_argument("--apply", action="store_true", help="实际执行删除；不加则只打印计划")
    parser.add_argument("--stale-lock-hours", type=float, default=24.0, help="孤儿临时文件判定阈值（小时），默认 24")
    parser.add_argument("--json", action="store_true", help="输出机器可读的 JSON 结果")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        result = run_cleanup(args.task_dirs, vault_dir=args.vault, stale_hours=args.stale_lock_hours, apply=args.apply)
    except Exception as exc:
        print(f"错误: {exc}", file=sys.stderr)
        raise SystemExit(1)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print_plan(result)


if __name__ == "__main__":
    main()
