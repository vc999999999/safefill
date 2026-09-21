"""Delivery packages install independently and reject unsafe archive paths."""
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import install  # noqa: E402


def test_built_packages_install_without_repository_and_refuse_overwrite(tmp_path):
    out = tmp_path / "packages"
    subprocess.run([sys.executable, str(ROOT / "tools/build_skills.py"), "--out", str(out)], check=True,
                   capture_output=True, text=True, encoding="utf-8",
                   env={**os.environ, "PYTHONIOENCODING": "cp1252"})
    for role in ("fill", "collect"):
        package = out / f"safefill-{role}.skill"
        with zipfile.ZipFile(package) as archive:
            assert all("__pycache__" not in item and "/.venv/" not in item for item in archive.namelist())
        target = install.install(package, tmp_path / "installed", role, dependencies=False)
        entry = "fill" if role == "fill" else "collector"
        result = subprocess.run([sys.executable, "-S", str(target / "scripts" / f"{entry}.py"), "--help"],
                                capture_output=True, text=True, encoding="utf-8", check=True, cwd=tmp_path,
                                env={**os.environ, "PYTHONIOENCODING": "cp1252"})
        assert "SafeFill" in result.stdout
        original = (target / "SKILL.md").read_bytes()
        with pytest.raises(FileExistsError):
            install.install(package, tmp_path / "installed", role, dependencies=False)
        assert (target / "SKILL.md").read_bytes() == original


@pytest.mark.parametrize("member", ["../escape.py", "safefill-fill/../escape.py", "safefill-fill/C:/bad.py",
                                     "safefill-fill/scripts/CON", "safefill-fill/scripts\\bad.py"])
def test_install_rejects_unsafe_members_before_writing(tmp_path, member):
    package = tmp_path / "bad.skill"
    with zipfile.ZipFile(package, "w") as archive:
        info = zipfile.ZipInfo()
        # Preserve malformed names; ZipInfo(member) normalizes backslashes on Windows.
        info.filename = member
        archive.writestr(info, "bad")
    with pytest.raises(ValueError, match="package path"):
        install.install(package, tmp_path / "destination", "fill", dependencies=False)
    assert not (tmp_path / "destination").exists()


def test_failed_dependency_install_rolls_back_only_new_directory(tmp_path, monkeypatch):
    package = tmp_path / "fill.skill"
    with zipfile.ZipFile(package, "w") as archive:
        for name in ("SKILL.md", "requirements.txt", "scripts/fill.py"):
            archive.writestr(f"safefill-fill/{name}", "fixture")
    destination = tmp_path / "destination"
    destination.mkdir()
    kept = destination / "other-skill.txt"
    kept.write_text("keep")
    monkeypatch.setattr(install.venv.EnvBuilder, "create", lambda *args: None)

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "pip")

    monkeypatch.setattr(install.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        install.install(package, destination, "fill")
    assert not (destination / "safefill-fill").exists()
    assert kept.read_text() == "keep"
