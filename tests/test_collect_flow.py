import json
import os
import re
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "safefill-fill" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import collection  # noqa: E402


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
    collection.dump_json(config_path, base_config())
    args = collection.build_parser().parse_args(
        ["create-request", "--config", str(config_path), "--out", str(tmp_path / "tasks")])
    return Path(args.func(args)["task_dir"])


def make_directed_task(tmp_path: Path) -> Path:
    config_path = tmp_path / "collection.json"
    collection.dump_json(config_path, base_config())
    roster = tmp_path / "roster.csv"
    roster.write_text("employee_id,name\nE001,张三\n", encoding="utf-8")
    created = collection.create_task(roster, config_path, tmp_path / "tasks", "pw-123", require_terminal=False, mode="directed")
    return Path(created["task_dir"])


def run_collect(argv):
    args = collection.build_parser().parse_args(argv)
    return args.func(args)


def test_ingest_reports_skipped_directories_and_hint(tmp_path):
    task_dir = make_open_task(tmp_path)
    incoming = private(tmp_path / "incoming")
    nested = incoming / "张三"
    nested.mkdir()
    (nested / "回执.yintian").write_bytes(b"{}")
    (incoming / "李四").mkdir()

    summary = collection.ingest_task(task_dir, incoming)

    assert summary["accepted"] == 0 and summary["duplicates"] == 0 and summary["rejected"] == 0
    assert summary["errors"] == []
    assert summary["skipped_directories"] == 2
    assert summary["hint"] == "收件目录顶层无 .yintian 文件，不递归子目录；发现 2 个子目录，请将回执文件移到顶层后重试"
    serialized = json.dumps(summary, ensure_ascii=False)
    assert "张三" not in serialized and "李四" not in serialized


def test_ingest_no_hint_when_top_level_receipts_processed(tmp_path):
    task_dir = make_open_task(tmp_path)
    incoming = private(tmp_path / "incoming")
    (incoming / "broken.yintian").write_bytes(b"not-json")
    (incoming / "张三").mkdir()

    summary = collection.ingest_task(task_dir, incoming)

    assert summary["rejected"] == 1
    assert summary["skipped_directories"] == 1
    assert "hint" not in summary


def test_ingest_no_hint_without_skipped_directories(tmp_path):
    task_dir = make_open_task(tmp_path)
    incoming = private(tmp_path / "incoming")

    summary = collection.ingest_task(task_dir, incoming)

    assert summary["skipped_directories"] == 0
    assert "hint" not in summary


def test_collect_fails_fast_with_suggested_name_when_xlsx_exists(tmp_path, monkeypatch):
    task_dir = make_open_task(tmp_path)
    incoming = private(tmp_path / "incoming")
    out = tmp_path / "result.xlsx"
    out.write_bytes(b"old")
    ingest_calls = []
    monkeypatch.setattr(collection, "ingest_task", lambda *a, **k: ingest_calls.append(a))

    with pytest.raises(FileExistsError, match="OUTPUT_EXISTS: 导出文件已存在") as excinfo:
        collection.collect_open(task_dir, incoming, out)

    assert ingest_calls == []
    suggested = re.search(r"result-\d{8}-\d{4}\.xlsx", str(excinfo.value))
    assert suggested is not None
    assert not (tmp_path / suggested.group(0)).exists()


def test_task_password_returns_none_for_open_task_without_getpass(tmp_path, monkeypatch):
    task_dir = make_open_task(tmp_path)

    def fail(prompt):
        raise AssertionError("open 任务不应询问任务密码")

    monkeypatch.setattr(collection.getpass, "getpass", fail)
    assert collection.task_password(task_dir) is None


def test_task_password_prompts_for_non_open_task(tmp_path, monkeypatch):
    task_dir = make_directed_task(tmp_path)
    prompts = []
    monkeypatch.setattr(collection.getpass, "getpass", lambda prompt: prompts.append(prompt) or "pw-123")

    assert collection.task_password(task_dir) == "pw-123"
    assert prompts == ["任务密码: "]


def test_collect_retries_needs_review_by_default(tmp_path, monkeypatch):
    task_dir = make_open_task(tmp_path)
    incoming = private(tmp_path / "incoming")
    calls = []

    def fake_review(root, password, retry_needs_review=False, invite_id=None):
        calls.append(retry_needs_review)
        return {"verified": 0, "needs_review": 0, "invalid": 0}

    monkeypatch.setattr(collection, "review_task", fake_review)
    result = collection.collect_open(task_dir, incoming, tmp_path / "out.xlsx")

    assert calls == [True]
    assert result["review"] == {"verified": 0, "needs_review": 0, "invalid": 0}


def test_collect_no_retry_needs_review_flag(tmp_path, monkeypatch):
    task_dir = make_open_task(tmp_path)
    incoming = private(tmp_path / "incoming")
    calls = []

    def fake_review(root, password, retry_needs_review=False, invite_id=None):
        calls.append(retry_needs_review)
        return {"verified": 0, "needs_review": 0, "invalid": 0}

    monkeypatch.setattr(collection, "review_task", fake_review)
    run_collect(["collect", str(task_dir), str(incoming), "--out", str(tmp_path / "a.xlsx")])
    run_collect(["collect", str(task_dir), str(incoming), "--out", str(tmp_path / "b.xlsx"), "--no-retry-needs-review"])

    assert calls == [True, False]
