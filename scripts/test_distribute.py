"""distribute.py 的单元与回归测试。支持直接运行，亦支持 pytest 收集。"""
from __future__ import annotations

import csv
import inspect
import os
import tempfile
from pathlib import Path

import collection
import distribute

try:
    import pytest

    @pytest.fixture
    def sample_task(tmp_path: Path) -> Path:
        return create_sample_task(tmp_path)

except ImportError:
    pytest = None  # type: ignore


def create_sample_task(tmp_path: Path) -> Path:
    roster_path = tmp_path / "roster.csv"
    with roster_path.open("w", encoding="utf-8", newline="") as s:
        w = csv.writer(s)
        w.writerow(["employee_id", "name"])
        w.writerow(["E001", "张三"])
        w.writerow(["E002", "李四"])

    cfg_path = tmp_path / "config.json"
    cfg = collection.default_config()
    cfg["purpose"] = "员工档案登记"
    cfg["contact"] = "人事小王"
    cfg["correction"] = "联系人事小王"
    cfg["deadline"] = "2099-12-31T23:59:59Z"
    cfg["retention_until"] = "2100-01-31T23:59:59Z"
    collection.dump_json(cfg_path, cfg)

    created = collection.create_task(
        roster_path, cfg_path, tmp_path / "tasks", "TestPassword-1234", require_terminal=False
    )
    return Path(created["task_dir"])


def test_verify_invites_all_valid(sample_task: Path) -> None:
    res = distribute.verify_invites(sample_task)
    assert res["total"] == 2
    assert res["valid"] == 2
    assert res["all_valid"] is True
    assert len(res["missing"]) == 0


def test_verify_invites_missing_detected(sample_task: Path) -> None:
    # 模拟人为误删或遗漏某个邀请文件
    first_html = next((sample_task / "invites").glob("*.html"))
    first_html.unlink()

    res = distribute.verify_invites(sample_task)
    assert res["total"] == 2
    assert res["valid"] == 1
    assert res["all_valid"] is False
    assert len(res["missing"]) == 1


def test_generate_messages(sample_task: Path) -> None:
    msgs = distribute.generate_messages(sample_task)
    assert len(msgs) == 2
    assert any("张三" in m["message"] and "E001" in m["message"] for m in msgs)
    assert any("李四" in m["message"] and "E002" in m["message"] for m in msgs)
    # 文案中只含邀请文件名，不得泄露系统物理绝对路径
    for m in msgs:
        assert str(sample_task) not in m["message"]


def test_pending_reminders(sample_task: Path) -> None:
    reminders = distribute.pending_reminders(sample_task)
    assert len(reminders) == 2
    assert all(r["status"] == "pending" for r in reminders)
    assert any("张三" in r["reminder_message"] for r in reminders)


def test_verify_invites_rejects_traversal(sample_task: Path) -> None:
    """invite_file 含 ..、绝对路径、反斜杠或 invites/ 之外子目录的行记入 invalid，绝不拼出任务目录外路径。"""
    index_path = sample_task / "invite-index.csv"
    with index_path.open("a", encoding="utf-8", newline="") as s:
        writer = csv.writer(s)
        writer.writerow(["E901", "王五", "INVA00000001", "../task.json"])
        writer.writerow(["E902", "赵六", "INVA00000002", "/etc/passwd"])
        writer.writerow(["E903", "孙七", "INVA00000003", "invites\\..\\task.json"])
        writer.writerow(["E904", "周八", "INVA00000004", "other/INV-BBBBBBBBBB.html"])
        writer.writerow(["E905", "吴九", "INVA00000005", "invites/../task.json"])
    res = distribute.verify_invites(sample_task)
    assert res["total"] == 7
    assert res["valid"] == 2
    assert len(res["invalid"]) == 5
    assert {row["employee_id"] for row in res["invalid"]} == {"E901", "E902", "E903", "E904", "E905"}
    assert res["all_valid"] is False


def test_safe_invite_relpath_accepts_legit_forms() -> None:
    assert distribute.safe_invite_relpath("INV-AAAAAAAAAA.html") == Path("INV-AAAAAAAAAA.html")
    assert distribute.safe_invite_relpath("invites/INV-AAAAAAAAAA.html") == Path("invites/INV-AAAAAAAAAA.html")
    for bad in ("", "../x", "a/../b", "/abs/x", "C:\\x", "invites\\x", "sub/x"):
        assert distribute.safe_invite_relpath(bad) is None, bad


def test_generate_messages_custom_template(sample_task: Path) -> None:
    msgs = distribute.generate_messages(sample_task, template="{name}|{invite_name}|{invite_id}")
    assert len(msgs) == 2
    assert any(m["message"].startswith("张三|INV-") for m in msgs)
    assert all("|" in m["message"] for m in msgs)


def test_messages_template_cli(sample_task: Path) -> None:
    out = sample_task / "custom.csv"
    rc = distribute.main(["messages", str(sample_task), "--template", "{name}@{invite_name}", "--out-csv", str(out)])
    assert rc == 0
    with out.open("r", encoding="utf-8-sig", newline="") as s:
        rows = list(csv.DictReader(s))
    assert any(row["message"].startswith("张三@INV-") for row in rows)


def test_out_csv_only_filenames_and_permissions(sample_task: Path) -> None:
    out = sample_task / "dist.csv"
    rc = distribute.main(["messages", str(sample_task), "--out-csv", str(out)])
    assert rc == 0
    text = out.read_text(encoding="utf-8-sig")
    assert str(sample_task) not in text, "落盘 CSV 不得包含绝对路径"
    with out.open("r", encoding="utf-8-sig", newline="") as s:
        rows = list(csv.DictReader(s))
    assert "invite_path" not in rows[0], "落盘 CSV 不应再有 invite_path 绝对路径列"
    assert all("/" not in row["invite_file"] and row["invite_file"].endswith(".html") for row in rows)
    if os.name != "nt":
        assert (out.stat().st_mode & 0o777) == 0o600, "导出 CSV 权限应为 0600"


def test_out_csv_refuses_overwrite_unless_force(sample_task: Path) -> None:
    out = sample_task / "dist.csv"
    assert distribute.main(["messages", str(sample_task), "--out-csv", str(out)]) == 0
    first = out.read_bytes()
    assert distribute.main(["messages", str(sample_task), "--out-csv", str(out)]) == 2, "已存在时应拒绝覆盖"
    assert out.read_bytes() == first, "拒绝覆盖时原文件不应被改动"
    assert distribute.main(["messages", str(sample_task), "--out-csv", str(out), "--force"]) == 0, "--force 应允许覆盖"


def test_out_csv_refuses_symlink(sample_task: Path) -> None:
    if os.name == "nt":
        return
    target = sample_task / "task.json"
    link = sample_task / "link.csv"
    link.symlink_to(target)
    before = target.read_bytes()
    assert distribute.main(["messages", str(sample_task), "--out-csv", str(link)]) == 2
    assert distribute.main(["messages", str(sample_task), "--out-csv", str(link), "--force"]) == 2, "--force 也不得写符号链接"
    assert target.read_bytes() == before, "符号链接目标不应被写入"


def main() -> None:
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_") and callable(fn)]
    for name, fn in tests:
        if inspect.signature(fn).parameters:
            with tempfile.TemporaryDirectory() as tmp:
                fn(create_sample_task(Path(tmp)))
        else:
            fn()
        print(f"PASS: {name}")
    print(f"distribute 共 {len(tests)} 项检查全部通过。")


if __name__ == "__main__":
    main()
