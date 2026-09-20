"""A known signed correction must block stale exports even when ciphertext storage fails."""
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from test_collect_flow import collector, make_open_task, private
import fill


@pytest.mark.parametrize("failure", ["version_limit", "byte_limit", "write_failure"])
def test_blocked_correction_survives_empty_inbox_handoff_and_retry(tmp_path, monkeypatch, failure):
    root = make_open_task(tmp_path)
    request = next(root.glob("REQUEST-*.yintian-request"))
    form = fill.load_form(request)
    signing_key = Ed25519PrivateKey.generate()
    inbox = private(tmp_path / "inbox")
    fill.seal_data(form, {"name": "old-sentinel"}, {}, inbox / "old.yintian", signing_key=signing_key, revision=1)
    assert collector.ingest_task(root, inbox)["accepted"] == 1
    collector.review_task(root)
    newer = Path(fill.seal_data(form, {"name": "new-sentinel"}, {}, inbox / "new.yintian",
                               signing_key=signing_key, revision=2)["out"])

    with monkeypatch.context() as blocked:
        if failure == "version_limit":
            blocked.setattr(collector, "MAX_VERSIONS_PER_INVITE", 1)
        elif failure == "byte_limit":
            blocked.setattr(collector, "MAX_TASK_SUBMISSION_BYTES", 1)
        else:
            original_write = collector.atomic_write

            def fail_receipt(path, *args, **kwargs):
                if Path(path).suffix == ".yintian":
                    raise OSError("private-error-sentinel")
                return original_write(path, *args, **kwargs)

            blocked.setattr(collector, "atomic_write", fail_receipt)
        with pytest.raises(RuntimeError, match="TASK_STORAGE_LIMIT|INGEST_IO"):
            collector.collect_task(root, inbox, tmp_path / "failed.xlsx")
    assert not (tmp_path / "failed.xlsx").exists()
    with collector.connect_db(root) as db:
        pending = dict(db.execute("SELECT * FROM submissions WHERE revision=2").fetchone())
        assert pending["path"] == "" and pending["status"] == "storage_blocked"
        assert db.execute("SELECT COUNT(*) FROM submissions WHERE path<>''").fetchone()[0] == 1

    held = newer.replace(tmp_path / "held.yintian")
    empty_inbox = private(tmp_path / "empty")
    report = collector.collect_task(root, empty_inbox, tmp_path / "still-blocked.xlsx")
    assert report["rows"] == 0 and report["excluded"] == 1
    assert report["exclusions"][0]["revision"] == 2
    assert report["exclusions"][0]["status"] == "storage_blocked"
    assert collector.review_task(root, retry_needs_review=True) == {"verified": 0, "needs_review": 0, "invalid": 0}
    private_key = collector.unlock_private_key(root, form)
    assert collector.collect_rows(root, form, private_key, None, "attachments")[0] == []

    package = tmp_path / "handoff.yintian-task"
    collector.export_task(root, package, "test-handoff-password")
    imported = Path(collector.import_task(package, tmp_path / "imported", "test-handoff-password")["task_dir"])
    assert collector.progress_rows(imported)[0]["status"] == "storage_blocked"
    assert collector.collect_task(imported, empty_inbox, tmp_path / "imported-blocked.xlsx")["rows"] == 0

    held.replace(inbox / "new.yintian")
    retried = collector.ingest_task(root, inbox)
    assert retried["accepted"] == 1 and retried["duplicates"] == 1
    with collector.connect_db(root) as db:
        repaired = db.execute("SELECT * FROM submissions WHERE revision=2").fetchone()
        assert repaired["id"] == pending["id"] and repaired["version"] == pending["version"]
        assert repaired["path"] and repaired["status"] == "submitted"
        assert db.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 2
    collector.review_task(root)
    rows, _ = collector.collect_rows(root, form, private_key, None, "attachments")
    assert len(rows) == 1 and rows[0]["name"] == "new-sentinel"


def test_database_write_failure_stops_export_and_preserves_ciphertext_for_retry(tmp_path):
    root = make_open_task(tmp_path)
    form = fill.load_form(next(root.glob("REQUEST-*.yintian-request")))
    signing_key = Ed25519PrivateKey.generate()
    inbox = private(tmp_path / "inbox")
    fill.seal_data(form, {"name": "old-sentinel"}, {}, inbox / "old.yintian", signing_key=signing_key, revision=1)
    collector.ingest_task(root, inbox)
    collector.review_task(root)
    fill.seal_data(form, {"name": "new-sentinel"}, {}, inbox / "new.yintian", signing_key=signing_key, revision=2)
    with collector.connect_db(root) as db:
        db.execute("""CREATE TRIGGER fail_new BEFORE INSERT ON submissions WHEN NEW.revision=2
                      BEGIN SELECT RAISE(ABORT, 'synthetic storage failure'); END""")
    with pytest.raises(RuntimeError, match="DATABASE_WRITE_FAILED"):
        collector.collect_task(root, inbox, tmp_path / "failed.xlsx")
    assert not (tmp_path / "failed.xlsx").exists()
    assert len(list((root / "submissions").glob("*/*.yintian"))) == 2
    assert collector.progress_rows(root)[0]["revision"] == 1  # A failed DB write cannot claim persisted ordering.
    with collector.connect_db(root) as db:
        db.execute("DROP TRIGGER fail_new")
    assert collector.ingest_task(root, inbox)["accepted"] == 1
    collector.review_task(root)
    assert collector.progress_rows(root)[0]["revision"] == 2
