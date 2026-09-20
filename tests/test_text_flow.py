"""Optional local text intake keeps values off the Agent-facing workflow."""
import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from openpyxl import load_workbook

from test_fill_flow import make_request, private, private_json, run

import collector
import collection
import fill
import vault
import vlm_extract

VALUES = {"name": "SYNTHETIC_TEXT_PERSON", "phone": "13800138000", "id_number": "11010519491231002X"}
FIELDS = [
    {"id": "name", "label": "姓名", "type": "text", "required": True},
    {"id": "phone", "label": "电话", "type": "phone_cn", "required": True},
    {"id": "id_number", "label": "证件号", "type": "cn_id", "required": True},
]


def assert_private(value):
    rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    assert all(marker not in rendered for marker in VALUES.values())


def cli(argv, capfd, *, error=None):
    status = fill.main(argv)
    captured = capfd.readouterr()
    assert_private(captured.out + captured.err)
    if error is not None:
        assert status == 1 and not captured.out
        result = json.loads(captured.err)
        assert result["error"] == error
    else:
        assert status == 0 and not captured.err
        result = json.loads(captured.out)
        assert result["ok"] is True
    return result


@pytest.fixture
def text_case(tmp_path, monkeypatch):
    monkeypatch.setenv("YINTIAN_VAULT_DIR", str(private(tmp_path / "vault")))
    monkeypatch.setenv("YINTIAN_VAULT_KEY_DIR", str(private(tmp_path / "keys")))
    request, task = make_request(tmp_path, FIELDS)
    work = private(tmp_path / "work")
    source = tmp_path / f"{VALUES['name']}.txt"
    source.write_text("\n".join(f"{key}: {value}" for key, value in VALUES.items()), encoding="utf-8")

    class Pipeline:
        def __init__(self, *args):
            pass

        def generate(self, prompt, **kwargs):
            assert all(value in prompt for value in VALUES.values())
            print(VALUES["name"])
            print(VALUES["phone"], file=sys.stderr)
            os.write(1, VALUES["id_number"].encode())
            os.write(2, VALUES["name"].encode())
            logging.warning(VALUES["phone"])
            return json.dumps(VALUES)

    monkeypatch.setattr(vlm_extract, "verify_model", lambda *args: {})
    monkeypatch.setitem(sys.modules, "openvino_genai", SimpleNamespace(LLMPipeline=Pipeline))
    stage = ["vault-stage", "--text-file", str(source), "--request", str(request), "--model", "local-model",
             "--confirmation-out", str(work / "change.confirm")]
    return SimpleNamespace(request=request, task=task, work=work, source=source, stage=stage)


def test_text_intake_export_and_mapped_reuse(text_case, tmp_path, capfd):
    case = text_case
    original = case.source.read_bytes()
    staged = cli(case.stage, capfd)
    assert staged["local_only"] and [row["id"] for row in staged["changes"]] == list(VALUES)
    assert all("new_value" not in row and "old_value" not in row for row in staged["changes"])
    review = Path(staged["review"])
    assert all(value in review.read_text(encoding="utf-8") for value in VALUES.values())
    if os.name != "nt":
        assert review.stat().st_mode & 0o777 == 0o600
    cli(["vault-apply", "--confirmation", staged["confirmation"]], capfd)
    assert not review.exists() and case.source.read_bytes() == original

    confirmation = case.work / "submit.confirm"
    preview = cli(["vault-preview", str(case.request), "--confirmation-out", str(confirmation)], capfd)
    assert preview["ready"] and preview["local_only"]
    assert all("value" not in field for field in preview["fields"])
    review = Path(preview["review"])
    assert all(value in review.read_text(encoding="utf-8") for value in VALUES.values())
    receipt = cli(["vault-fill", str(case.request), "--confirmation", str(confirmation),
                   "--out-dir", str(case.work / "out")], capfd)
    assert not review.exists() and receipt["revision"] == 1
    collected = collector.collect_task(case.task, case.work / "out", tmp_path / "result.xlsx")
    assert_private(collected)
    collected_output = capfd.readouterr()
    assert_private(collected_output.out + collected_output.err)
    assert collected["rows"] == 1
    book = load_workbook(collected["xlsx"], read_only=True)
    try:
        cells = {str(value) for row in book.active.values for value in row}
        assert set(VALUES.values()) <= cells
    finally:
        book.close()
    assert_private([path.name for path in case.work.rglob("*")])

    # The source classification survives both a later task and an explicit semantic mapping.
    later_request, _ = make_request(private(tmp_path / "later"), [FIELDS[0],
        {"id": "contact_phone", "label": "联系电话", "type": "phone_cn", "required": True}])
    mapping = private_json(case.work / "mapping.json", {"contact_phone": "phone"})
    later = cli(["vault-preview", str(later_request), "--mapping", str(mapping),
                 "--confirmation-out", str(case.work / "later.confirm")], capfd)
    assert later["local_only"] and later["ready"]
    assert later["fields"][1]["source_entry"] == "phone"
    assert VALUES["phone"] in Path(later["review"]).read_text(encoding="utf-8")


def test_missing_fields_and_manual_replacement_hide_imported_values(text_case, monkeypatch, capfd):
    case = text_case
    monkeypatch.setattr(vlm_extract, "extract_text_fields", lambda *args: {"name": VALUES["name"]})
    staged = cli(case.stage, capfd)
    cli(["vault-apply", "--confirmation", staged["confirmation"]], capfd)
    confirmation = case.work / "missing.confirm"
    result = cli(["vault-preview", str(case.request), "--confirmation-out", str(confirmation)], capfd)
    assert not result["ready"] and result["local_only"]
    assert {field["id"] for field in result["required_missing"]} == {"phone", "id_number"}
    assert "review" not in result and "confirmation" not in result and not confirmation.exists()
    assert all("value" not in field for field in result["fields"])

    answers = private_json(case.work / "manual.json", {"entries": {"name": {"type": "text", "value": "Replacement"}}})
    change = cli(["vault-stage", "--answers", str(answers),
                  "--confirmation-out", str(case.work / "manual.confirm")], capfd)
    assert change["local_only"] and "old_value" not in change["changes"][0]
    review = Path(change["review"]).read_text(encoding="utf-8")
    assert VALUES["name"] in review and "Replacement" in review


@pytest.mark.parametrize("changed", ["text_source", "stage_review", "submission_review"])
def test_changed_private_inputs_cannot_apply_or_reserve_revision(text_case, capfd, changed):
    case = text_case
    staged = cli(case.stage, capfd)
    operation = ["vault-apply", "--confirmation", staged["confirmation"]]
    altered = case.source if changed == "text_source" else Path(staged["review"])
    if changed == "submission_review":
        cli(operation, capfd)
        preview = cli(["vault-preview", str(case.request),
                       "--confirmation-out", str(case.work / "submit.confirm")], capfd)
        altered = Path(preview["review"])
        operation = ["vault-fill", str(case.request), "--confirmation", preview["confirmation"],
                     "--out-dir", str(case.work / "out")]
    vault_path = vault.default_vault_path()
    before = vault_path.read_bytes() if vault_path.exists() else None
    altered.write_text("Changed after review", encoding="utf-8")
    cli(operation, capfd, error="CONFIRMATION_STALE")
    assert (vault_path.read_bytes() if vault_path.exists() else None) == before
    assert not list(case.work.rglob("*.yintian"))
    assert case.source.exists()


@pytest.mark.parametrize("failure", ["model", "values"])
def test_text_failure_keeps_source_and_sanitizes_output(text_case, monkeypatch, capfd, failure):
    case = text_case
    original = case.source.read_bytes()

    def fail(*args):
        if failure == "model":
            raise vlm_extract.VlmUnavailable("VLM_UNAVAILABLE: " + VALUES["name"])
        return {"phone": "invalid-" + VALUES["phone"]}

    monkeypatch.setattr(vlm_extract, "extract_text_fields", fail)
    cli(case.stage, capfd, error="VLM_UNAVAILABLE" if failure == "model" else "TEXT_VALUES_INVALID")
    assert case.source.read_bytes() == original
    assert not list(case.work.iterdir()) and not vault.default_vault_path().exists()


@pytest.mark.parametrize("collision", ["source_confirmation", "request_confirmation", "source_request"])
def test_text_path_collisions_preserve_inputs(text_case, capfd, collision):
    case = text_case
    args = list(case.stage)
    if collision == "source_confirmation":
        args[-1] = str(case.source)
    elif collision == "request_confirmation":
        args[-1] = str(case.request)
    else:
        args[args.index("--request") + 1] = str(case.source)
    inputs = {path: path.read_bytes() for path in (case.source, case.request)}
    cli(args, capfd, error="VAULT_PATH_COLLISION")
    assert all(path.read_bytes() == data for path, data in inputs.items())
    assert not list(case.work.iterdir())


def test_failed_confirmation_write_removes_new_review(text_case, monkeypatch, capfd):
    case = text_case
    original = case.source.read_bytes()

    def fail(*args, **kwargs):
        assert len(list(case.work.glob("REVIEW-*.txt"))) == 1
        raise RuntimeError(VALUES["name"])

    monkeypatch.setattr(vault, "seal_confirmation", fail)
    cli(case.stage, capfd, error="FillError")
    assert not list(case.work.iterdir()) and case.source.read_bytes() == original
    assert not vault.default_vault_path().exists()


def test_text_cli_input_is_exclusive(text_case):
    with pytest.raises(SystemExit):
        fill.build_parser().parse_args([*text_case.stage, "--answers", "-"])
    assert run(["vault-status"])["vault"] is False


def test_text_import_accepts_full_request_label_limit(text_case, tmp_path, capfd):
    label = "长" * collection.MAX_LABEL_CHARS
    fields = [{**field, "label": label} if field["id"] == "phone" else field for field in FIELDS]
    request, _ = make_request(private(tmp_path / "long-label"), fields)
    arguments = list(text_case.stage)
    arguments[arguments.index("--request") + 1] = str(request)
    staged = cli(arguments, capfd)
    cli(["vault-apply", "--confirmation", staged["confirmation"]], capfd)
    key = vault.load_or_create_key(vault.default_key_path())
    assert vault.load_vault(vault.default_vault_path(), key)["entries"]["phone"]["label"] == label
