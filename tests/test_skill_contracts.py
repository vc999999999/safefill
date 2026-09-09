import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COLLECT = ROOT / "skills" / "safefill-collect"
FILL = ROOT / "skills" / "safefill-fill"


def test_skill_entrypoints_and_versions_are_consistent():
    for skill, name in ((COLLECT, "safefill-collect"), (FILL, "safefill-fill")):
        text = (skill / "SKILL.md").read_text(encoding="utf-8")
        assert text.startswith("---\n") and f"name: {name}\n" in text
        assert (skill / "scripts").is_dir()
    assert json.loads((COLLECT / "meta.json").read_text(encoding="utf-8"))["version"] == "4.2.1"
    assert json.loads((FILL / "meta.json").read_text(encoding="utf-8"))["version"] == "5.0.0"
    assert "4.2.1" in (COLLECT / "README.md").read_text(encoding="utf-8")
    assert "5.0.0" in (FILL / "README.md").read_text(encoding="utf-8")


def test_prompt_uses_confirmation_flow_and_never_restores_html():
    docs = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "README.md", FILL / "README.md", FILL / "SKILL.md"))
    assert "vault-stage" in docs and "vault-preview" in docs and "--confirmation" in docs
    assert all(item not in docs for item in ("vault-init", "vault-add", "vault-edit", "--confirmed"))
    parser_help = subprocess.run(
        [sys.executable, str(FILL / "scripts" / "fill.py"), "--help"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert "vault-edit" not in parser_help and "seal" not in parser_help
    assert not list(ROOT.rglob("*.html"))


def test_shared_protocol_and_secure_io_copies_are_identical():
    for name in ("collection.py", "secure_io.py"):
        assert (COLLECT / "scripts" / name).read_bytes() == (FILL / "scripts" / name).read_bytes()


def test_no_stale_tracked_validation_claim():
    assert not (COLLECT / "evals" / "validation.json").exists()
