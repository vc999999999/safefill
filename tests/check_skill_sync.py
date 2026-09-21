"""校验两个独立 Skill 的共享模块、协议副本与本地文档引用。

这 5 个模块（collection.py、config.py、ocr_matcher.py、openvino_runtime.py、
secure_io.py）是两个 skill 的同步副本：任何修改必须双侧同步（见
README「共享模块同步」）。本脚本既可直接运行，也可作为 pytest 用例由 CI 执行；
协议维护源为根目录 PROTOCOL.md，两端必须携带相同副本。
发现漂移、缺失或越出 Skill 安装目录的文档引用时以非零码退出。
"""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
SIDES = ("safefill-collect", "safefill-fill")
SHARED_MODULES = (
    "collection.py",
    "config.py",
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
    protocol = REPO_ROOT / "PROTOCOL.md"
    for side in SIDES:
        root = REPO_ROOT / "skills" / side
        copy = root / "references" / "PROTOCOL.md"
        if not protocol.is_file() or not copy.is_file():
            problems.append((f"{side}/PROTOCOL.md", "维护源或独立安装副本缺失"))
        elif protocol.read_bytes() != copy.read_bytes():
            problems.append((f"{side}/PROTOCOL.md", "协议副本与根目录维护源不一致"))
        documents = [root / "SKILL.md", root / "README.md", *(root / "references").rglob("*.md")]
        for document in documents:
            if not document.is_file():
                problems.append((str(document), "文档缺失"))
                continue
            for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", document.read_text(encoding="utf-8")):
                target = target.strip("<>").split("#", 1)[0]
                if not target or urlsplit(target).scheme:
                    continue
                destination = (document.parent / target).resolve()
                if not destination.is_relative_to(root.resolve()) or not destination.is_file():
                    problems.append((str(document), f"引用不在独立 Skill 中或文件缺失: {target}"))
    return problems


def test_shared_modules_in_sync():
    problems = find_out_of_sync()
    assert not problems, "Skill 交付检查失败: " + "; ".join(f"{name} ({reason})" for name, reason in problems)


def main() -> int:
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    problems = find_out_of_sync()
    if problems:
        for name, reason in problems:
            print(f"OUT_OF_SYNC: {name}: {reason}")
        return 1
    print(f"OK: {len(SHARED_MODULES)} 个共享模块、协议副本与独立 Skill 文档引用均有效")
    return 0


if __name__ == "__main__":
    sys.exit(main())
