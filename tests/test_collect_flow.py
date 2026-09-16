import argparse
import json
import os
import re
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "safefill-fill" / "scripts"
COLLECT_SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "safefill-collect" / "scripts"
sys.path.insert(0, str(COLLECT_SCRIPTS))
sys.path.insert(0, str(SCRIPTS))

import collector  # noqa: E402


def private(path: Path) -> Path:
    path.mkdir(mode=0o700)
    if os.name != "nt":
        path.chmod(0o700)
    return path


def base_config() -> dict:
    return {
        "title": "员工资料",
        "purpose": "入职登记",
        "deadline": "2098-12-31T23:59:59Z",
        "retention_until": "2099-01-31T23:59:59Z",
        "contact": "hr@example.com",
        "correction": "使用本人旧回执更正",
        "template_version": "1.0",
        "fields": [
            {"id": "name", "label": "姓名", "type": "text", "required": True, "sensitive": True},
        ],
    }


def make_open_task(tmp_path: Path) -> Path:
    config_path = tmp_path / "collection.json"
    collector.dump_json(config_path, base_config())
    args = collector.build_parser().parse_args(
        ["create-request", "--config", str(config_path), "--out", str(tmp_path / "tasks")])
    return Path(args.func(args)["task_dir"])


def run_collect(argv):
    args = collector.build_parser().parse_args(argv)
    return args.func(args)


def test_ingest_reports_skipped_directories_and_hint(tmp_path):
    task_dir = make_open_task(tmp_path)
    incoming = private(tmp_path / "incoming")
    nested = incoming / "张三"
    nested.mkdir()
    (nested / "回执.yintian").write_bytes(b"{}")
    (incoming / "李四").mkdir()

    summary = collector.ingest_task(task_dir, incoming)

    assert summary["accepted"] == 0 and summary["duplicates"] == 0 and summary["rejected"] == 0
    assert summary["errors"] == []
    assert summary["skipped_directories"] == 2
    assert summary["hint"] == "收件目录顶层无 .yintian 文件，不递归子目录；发现 2 个子目录，请将回执文件移到顶层后重试"
    serialized = json.dumps(summary, ensure_ascii=False)
    assert "张三" not in serialized and "李四" not in serialized


def test_ingest_hint_mentions_subdirectories_even_when_top_level_processed(tmp_path):
    task_dir = make_open_task(tmp_path)
    incoming = private(tmp_path / "incoming")
    (incoming / "broken.yintian").write_bytes(b"not-json")
    (incoming / "张三").mkdir()

    summary = collector.ingest_task(task_dir, incoming)

    assert summary["rejected"] == 1
    assert summary["skipped_directories"] == 1
    assert summary["hint"] == "收件目录不递归子目录；发现 1 个子目录，若其中还有回执请将回执文件移到顶层后重试"
    assert "张三" not in json.dumps(summary, ensure_ascii=False)


def test_ingest_no_hint_without_skipped_directories(tmp_path):
    task_dir = make_open_task(tmp_path)
    incoming = private(tmp_path / "incoming")

    summary = collector.ingest_task(task_dir, incoming)

    assert summary["skipped_directories"] == 0
    assert "hint" not in summary


def test_collect_fails_fast_with_suggested_name_when_xlsx_exists(tmp_path, monkeypatch):
    task_dir = make_open_task(tmp_path)
    incoming = private(tmp_path / "incoming")
    out = tmp_path / "result.xlsx"
    out.write_bytes(b"old")
    ingest_calls = []
    monkeypatch.setattr(collector, "ingest_task", lambda *a, **k: ingest_calls.append(a))

    with pytest.raises(FileExistsError, match="OUTPUT_EXISTS: 导出文件已存在") as excinfo:
        collector.collect_task(task_dir, incoming, out)

    assert ingest_calls == []
    suggested = re.search(r"result-\d{8}-\d{4}\.xlsx", str(excinfo.value))
    assert suggested is not None
    assert not (tmp_path / suggested.group(0)).exists()


def test_collect_retries_needs_review_by_default(tmp_path, monkeypatch):
    task_dir = make_open_task(tmp_path)
    incoming = private(tmp_path / "incoming")
    calls = []

    def fake_review(root, retry_needs_review=False, invite_id=None):
        calls.append(retry_needs_review)
        return {"verified": 0, "needs_review": 0, "invalid": 0}

    monkeypatch.setattr(collector, "review_task", fake_review)
    result = collector.collect_task(task_dir, incoming, tmp_path / "out.xlsx")

    assert calls == [True]
    assert result["review"] == {"verified": 0, "needs_review": 0, "invalid": 0}


def test_collect_no_retry_needs_review_flag(tmp_path, monkeypatch):
    task_dir = make_open_task(tmp_path)
    incoming = private(tmp_path / "incoming")
    calls = []

    def fake_review(root, retry_needs_review=False, invite_id=None):
        calls.append(retry_needs_review)
        return {"verified": 0, "needs_review": 0, "invalid": 0}

    monkeypatch.setattr(collector, "review_task", fake_review)
    run_collect(["collect", str(task_dir), str(incoming), "--out", str(tmp_path / "a.xlsx")])
    run_collect(["collect", str(task_dir), str(incoming), "--out", str(tmp_path / "b.xlsx"), "--no-retry-needs-review"])

    assert calls == [True, False]


def test_cli_only_exposes_open_workflow_commands():
    sub = next(action for action in collector.build_parser()._actions
               if isinstance(action, argparse._SubParsersAction))
    assert set(sub.choices) == {"create-request", "collect", "ingest", "review", "decide",
                                "doctor", "export-task", "import-task", "purge"}


def test_export_import_roundtrip_unlocks_task(tmp_path):
    task_dir = make_open_task(tmp_path)
    package = tmp_path / "handoff.yintian-package"
    exported = collector.export_task(task_dir, package, handoff_password="pw-123")
    assert package.is_file() and exported["files"] >= 6

    with pytest.raises(ValueError, match="交接密码错误"):
        collector.import_task(package, tmp_path / "imported", handoff_password="wrong")

    imported = collector.import_task(package, tmp_path / "imported", handoff_password="pw-123")
    new_dir = Path(imported["task_dir"])
    _, task = collector.load_task(new_dir)
    assert task["task_id"] == exported["task_id"]
    collector.unlock_private_key(new_dir, task)  # 交接密钥已随行，私钥可解锁
    assert (new_dir.parent / ".safefill-keys" / f"{task['task_id']}.key").is_file()


def test_purge_requires_typed_task_id_and_removes_task(tmp_path, monkeypatch):
    task_dir = make_open_task(tmp_path)
    _, task = collector.load_task(task_dir)
    monkeypatch.setattr(collector, "require_tty", lambda action: None)

    monkeypatch.setattr("builtins.input", lambda prompt="": "wrong-id")
    with pytest.raises(RuntimeError, match="已取消"):
        collector.cmd_purge(argparse.Namespace(task_dir=str(task_dir), allow_early=True))
    assert task_dir.exists()

    monkeypatch.setattr("builtins.input", lambda prompt="": task["task_id"])
    result = collector.cmd_purge(argparse.Namespace(task_dir=str(task_dir), allow_early=True))
    assert not task_dir.exists()
    assert not (task_dir.parent / ".safefill-keys" / f"{task['task_id']}.key").exists()
    summary = json.loads(Path(result["summary"]).read_text(encoding="utf-8"))
    assert summary["task_id"] == task["task_id"] and summary["total"] == 0


def test_human_commands_require_tty():
    for argv in (["decide", "t", "OPEN-X", "--version", "1", "--action", "return", "--operator", "hr"],
                 ["export-task", "t", "--out", "p"], ["import-task", "p", "--out", "o"], ["purge", "t"]):
        args = collector.build_parser().parse_args(argv)
        with pytest.raises(RuntimeError, match="交互终端"):
            args.func(args)
