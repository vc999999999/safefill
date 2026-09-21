"""Local text model boundary: no network, no diagnostics containing employee data."""
import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "safefill-fill" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import collection  # noqa: E402
import vlm_extract  # noqa: E402

FIELDS = [{"id": "name", "label": "姓名", "type": "text"},
          {"id": "phone", "label": "联系电话", "type": "phone_cn"}]
SOURCE = "姓名：合成私密标记；联系电话：13800138000"


def local_model(tmp_path, monkeypatch):
    root = tmp_path / "models"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(vlm_extract, "model_root", lambda: root)
    target = vlm_extract.model_dir("local/text-model", "fixed-revision")
    target.mkdir(mode=0o700)
    model = target / "openvino_model.xml"
    model.write_text("synthetic model fixture", encoding="utf-8")
    model.chmod(0o600)
    manifest = target / vlm_extract.MANIFEST_NAME
    manifest.write_bytes(collection.canonical(vlm_extract._manifest(target, "local/text-model", "fixed-revision")))
    manifest.chmod(0o600)
    return target


def stub_pipeline(monkeypatch, output, calls=None, failure=False):
    calls = calls if calls is not None else []

    class Pipeline:
        def __init__(self, path, device):
            calls.append((path, device))
            assert os.environ["HF_HUB_OFFLINE"] == "1"
            assert os.environ["TRANSFORMERS_OFFLINE"] == "1"

        def generate(self, prompt, **kwargs):
            calls.append((prompt, kwargs))
            # The SDK may print prompts/errors using Python, native code or logging.
            print(SOURCE)
            print(SOURCE, file=sys.stderr)
            logging.critical(SOURCE)
            os.write(1, SOURCE.encode())
            os.write(2, SOURCE.encode())
            if failure:
                raise RuntimeError(SOURCE)
            return output

    monkeypatch.setitem(sys.modules, "openvino_genai", SimpleNamespace(LLMPipeline=Pipeline))
    return calls


def test_text_extraction_uses_verified_local_model_and_suppresses_output(tmp_path, monkeypatch, capfd):
    target = local_model(tmp_path, monkeypatch)
    monkeypatch.setenv("HF_HUB_OFFLINE", "old-value")
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)
    monkeypatch.setenv("YINTIAN_VLM_DEVICE", "CPU")
    calls = stub_pipeline(monkeypatch, '{"name":" 合成私密标记 ","phone":"13800138000"}')
    fields = [dict(FIELDS[0], ignored="never expose extra metadata"), FIELDS[1],
              {"id": "photo", "label": "证件照", "type": "image_attachment"}]
    assert vlm_extract.extract_text_fields(SOURCE, fields, "local/text-model", "fixed-revision") == {
        "name": "合成私密标记", "phone": "13800138000"}
    assert calls[0] == (str(target), "CPU")
    prompt, options = calls[1]
    payload = json.loads(prompt.split("\n", 1)[1])
    assert payload == {"fields": FIELDS, "document": SOURCE}
    assert options == {"max_new_tokens": 4096, "do_sample": False, "echo": False}
    assert os.environ["HF_HUB_OFFLINE"] == "old-value"
    assert "TRANSFORMERS_OFFLINE" not in os.environ
    assert capfd.readouterr() == ("", "")


@pytest.mark.parametrize("answer,code", [
    ("[]", "TEXT_EXTRACTION_INVALID"),
    ('{"unexpected":"13800138000"}', "TEXT_EXTRACTION_INVALID"),
    ('{"phone":13800138000}', "TEXT_EXTRACTION_INVALID"),
    ('{"name":"编造的姓名"}', "TEXT_EXTRACTION_INVALID"),
    ('{"phone":"13800138000","phone":"13800138000"}', "TEXT_EXTRACTION_INVALID"),
    ('{"name":' + json.dumps("x" * 201) + "}", "TEXT_EXTRACTION_INVALID"),
    ("x" * (vlm_extract.MAX_TEXT_OUTPUT_CHARS + 1), "TEXT_EXTRACTION_INVALID"),
    ('```json\n{"phone":"13800138000"}\n```', "TEXT_EXTRACTION_INVALID"),
    ('{"name":{"nested":"合成私密标记"}}', "TEXT_EXTRACTION_INVALID"),
    ("{}", "TEXT_FIELDS_EMPTY"),
    ('{"name":" "}', "TEXT_FIELDS_EMPTY"),
], ids=["array", "unknown-field", "number", "invented", "duplicate", "long-value", "long-output",
        "markdown", "nested", "empty", "blank"])
def test_text_model_rejects_invalid_or_ungrounded_output(tmp_path, monkeypatch, answer, code, capfd):
    local_model(tmp_path, monkeypatch)
    stub_pipeline(monkeypatch, answer)
    with pytest.raises(vlm_extract.VlmUnavailable, match=code) as exc:
        vlm_extract.extract_text_fields(SOURCE, FIELDS, "local/text-model", "fixed-revision")
    assert SOURCE not in str(exc.value)
    assert "13800138000" not in str(exc.value)
    assert capfd.readouterr() == ("", "")


def test_text_inference_failure_is_safe_and_integrity_prevents_loading(tmp_path, monkeypatch, capfd):
    target = local_model(tmp_path, monkeypatch)
    calls = stub_pipeline(monkeypatch, "", failure=True)
    with pytest.raises(vlm_extract.VlmUnavailable, match="VLM_UNAVAILABLE") as exc:
        vlm_extract.extract_text_fields(SOURCE, FIELDS, "local/text-model", "fixed-revision")
    assert SOURCE not in str(exc.value)
    assert capfd.readouterr() == ("", "")
    assert len(calls) == 2
    (target / "openvino_model.xml").write_text("changed model", encoding="utf-8")
    with pytest.raises(vlm_extract.VlmUnavailable, match="VLM_UNAVAILABLE"):
        vlm_extract.extract_text_fields(SOURCE, FIELDS, "local/text-model", "fixed-revision")
    assert len(calls) == 2


@pytest.mark.parametrize("text,fields", [
    (" ", FIELDS),
    ("私" * (vlm_extract.MAX_TEXT_BYTES // 3 + 1), FIELDS),
    (SOURCE, [FIELDS[0], FIELDS[0]]),
    (SOURCE, [{"id": "name", "label": "姓名", "type": "not-a-type"}]),
], ids=["blank", "oversized", "duplicate-field", "invalid-type"])
def test_text_input_bounds_reject_before_model_loading(monkeypatch, text, fields):
    calls = stub_pipeline(monkeypatch, "{}")
    with pytest.raises(vlm_extract.VlmUnavailable, match="TEXT_EXTRACTION_INVALID"):
        vlm_extract.extract_text_fields(text, fields, "uninstalled-model")
    assert not calls


def test_text_extraction_missing_model_does_not_download(tmp_path, monkeypatch):
    root = tmp_path / "uninstalled"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(vlm_extract, "model_root", lambda: root)
    calls = stub_pipeline(monkeypatch, "{}")
    monkeypatch.setattr(vlm_extract, "setup", lambda *a, **kw: pytest.fail("inference must not download"))
    with pytest.raises(vlm_extract.VlmUnavailable, match="VLM_UNAVAILABLE"):
        vlm_extract.extract_text_fields(SOURCE, FIELDS, "uninstalled-model")
    assert not calls and list(root.iterdir()) == []


def test_text_extraction_missing_runtime_is_actionable(tmp_path, monkeypatch, capfd):
    local_model(tmp_path, monkeypatch)
    monkeypatch.setitem(sys.modules, "openvino_genai", None)
    with pytest.raises(vlm_extract.VlmUnavailable, match="requirements-vlm.txt") as exc:
        vlm_extract.extract_text_fields(SOURCE, FIELDS, "local/text-model", "fixed-revision")
    assert str(exc.value).startswith("VLM_UNAVAILABLE:") and SOURCE not in str(exc.value)
    assert capfd.readouterr() == ("", "")
