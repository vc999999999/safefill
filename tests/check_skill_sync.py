"""校验 skills/safefill-collect 与 skills/safefill-fill 的共享模块逐字节一致。

这 6 个模块（collection.py、config.py、evidence_routing.py、ocr_matcher.py、
openvino_runtime.py、secure_io.py）是两个 skill 的同步副本：任何修改必须双侧
同步（见 README「共享模块同步」）。本脚本既可直接运行，也可作为 pytest 用例
由 CI 执行；发现漂移时打印不一致的文件名并以非零码退出。

注意：collection.py 中 review_evidence 的导入在两侧都带 try/except 降级，
因此两侧代码逐字节相同，可以整体纳入比对。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SIDES = ("safefill-collect", "safefill-fill")
SHARED_MODULES = (
    "collection.py",
    "config.py",
    "evidence_routing.py",
    "ocr_matcher.py",
    "openvino_runtime.py",
    "secure_io.py",
)


def find_out_of_sync() -> list[tuple[str, str]]:
    """返回 (文件名, 原因) 列表；空列表表示全部一致。"""
    problems = []
    for name in SHARED_MODULES:
        paths = [REPO_ROOT / "skills" / side / "scripts" / name for side in SIDES]
        missing = [str(p) for p in paths if not p.is_file()]
        if missing:
            problems.append((name, "文件缺失: " + ", ".join(missing)))
        elif paths[0].read_bytes() != paths[1].read_bytes():
            problems.append((name, f"两侧内容不一致: {paths[0]} != {paths[1]}"))
    return problems


def test_shared_modules_in_sync():
    problems = find_out_of_sync()
    assert not problems, "共享模块漂移: " + "; ".join(f"{name} ({reason})" for name, reason in problems)


def main() -> int:
    problems = find_out_of_sync()
    if problems:
        for name, reason in problems:
            print(f"OUT_OF_SYNC: {name}: {reason}")
        return 1
    print(f"OK: {len(SHARED_MODULES)} 个共享模块两侧逐字节一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
