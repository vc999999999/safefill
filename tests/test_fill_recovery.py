"""A failed identity switch must preserve the previous default and reserved revisions."""
import json
from pathlib import Path

import pytest

from test_fill_flow import run
from test_revision_privacy import preview, scenario as scenario, submit

import collection
import fill
import vault


@pytest.mark.parametrize("replaced_before_error", [False, True])
def test_failed_fresh_identity_switch_restores_default(scenario, monkeypatch, replaced_before_error):
    request, _, work = scenario
    original = submit(request, work, "original")
    fresh, confirmation = preview(request, work, "fresh", "--fresh")
    save = vault.save_vault
    calls = 0

    def fail_switch(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2 and not replaced_before_error:
            raise OSError("synthetic write failure")
        save(*args, **kwargs)
        if calls == 2:
            raise OSError("synthetic directory fsync failure after replacement")

    with monkeypatch.context() as fault:
        fault.setattr(vault, "save_vault", fail_switch)
        with pytest.raises(fill.FillError, match="VAULT_WRITE_FAILED"):
            run(["vault-fill", str(request), "--fresh", "--confirmation", str(confirmation),
                 "--out-dir", str(work / "out")])

    profile = vault.load_vault(vault.default_vault_path(), vault.load_or_create_key(vault.default_key_path()))
    state = profile["submission_identities"][original["task_id"]]
    assert state["current"] == original["invite_id"]
    assert state["identities"][fresh["invite_id"]]["last_reserved_revision"] == 1
    assert list((work / "out").glob("*.yintian")) == [Path(original["out"])]
    resumed = submit(request, work, "resumed")
    assert resumed["invite_id"] == original["invite_id"] and resumed["revision"] == 2


def test_failed_identity_rollback_preserves_signed_receipt(scenario, monkeypatch):
    request, _, work = scenario
    submit(request, work, "original")
    fresh, confirmation = preview(request, work, "fresh", "--fresh")
    save = vault.save_vault
    calls = 0

    def fail_switch_and_rollback(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("synthetic rollback failure")
        save(*args, **kwargs)
        if calls == 2:
            raise OSError("synthetic directory fsync failure after replacement")

    monkeypatch.setattr(vault, "save_vault", fail_switch_and_rollback)
    with pytest.raises(fill.FillError, match="VAULT_ROLLBACK_FAILED"):
        run(["vault-fill", str(request), "--fresh", "--confirmation", str(confirmation),
             "--out-dir", str(work / "out")])

    retained = list((work / "out").glob(f"RECEIPT-{fresh['invite_id']}-*.yintian"))
    assert len(retained) == 1 and not confirmation.exists()
    envelope = json.loads(retained[0].read_bytes())
    assert collection.validate_envelope_header(envelope, fill.load_form(request)) == fresh["invite_id"]
    profile = vault.load_vault(vault.default_vault_path(), vault.load_or_create_key(vault.default_key_path()))
    state = profile["submission_identities"][envelope["task_id"]]
    assert state["current"] == fresh["invite_id"]
    assert state["identities"][fresh["invite_id"]]["last_reserved_revision"] == envelope["revision"] == 1
