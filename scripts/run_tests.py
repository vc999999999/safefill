"""隐填 · 自动化测试全量套件入口。

自动发现并执行 scripts/ 下全部 test_*.py（与 pytest 的收集范围一致，
无需手工维护清单），支持直接运行：
    python scripts/run_tests.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent

TEST_TIMEOUT_SECONDS = 300


def discover_test_scripts() -> list[Path]:
    return sorted(SCRIPTS_DIR.glob("test_*.py"))


def run_all() -> int:
    python = sys.executable
    scripts = discover_test_scripts()
    print("=" * 60)
    print("开始执行隐填全量工程自检与测试套件")
    print(f"Python 解释器: {python}")
    print(f"工作目录: {SCRIPTS_DIR}")
    print(f"发现 {len(scripts)} 个测试模块: {', '.join(p.name for p in scripts)}")
    print("=" * 60)

    failed = []
    skipped_total = 0
    for script_path in scripts:
        print(f"\n>>> 正在运行: {script_path.name} ...")
        try:
            proc = subprocess.run(
                [python, str(script_path)],
                cwd=str(SCRIPTS_DIR.parent),
                capture_output=True,
                text=True,
                timeout=TEST_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            print(f"[FAIL] {script_path.name} 超过 {TEST_TIMEOUT_SECONDS} 秒未结束，按失败处理")
            failed.append(script_path.name)
            continue

        if proc.stdout:
            sys.stdout.write(proc.stdout)
        if proc.stderr:
            sys.stderr.write(proc.stderr)
        skipped_total += sum(
            1 for line in proc.stdout.splitlines() if line.startswith("SKIP:")
        )
        if proc.returncode != 0:
            print(f"[FAIL] {script_path.name} 返回非零退出码: {proc.returncode}")
            failed.append(script_path.name)
        else:
            print(f"[OK] {script_path.name} 通过")

    print("\n" + "=" * 60)
    if failed:
        print(
            f"测试失败！通过 {len(scripts) - len(failed)}/{len(scripts)} 个模块"
            f"（跳过 {skipped_total} 项用例）；失败项: {', '.join(failed)}"
        )
        return 1

    print(f"全部 {len(scripts)} 个测试模块执行完毕，无失败（跳过 {skipped_total} 项用例）。")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(run_all())
