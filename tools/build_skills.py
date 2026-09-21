"""Build two ZIP-format .skill packages from tracked source files only."""
import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def build(out: Path) -> None:
    subprocess.run([sys.executable, str(ROOT / "tests/check_skill_sync.py")], check=True)
    out.mkdir(parents=True, exist_ok=True)
    artifacts = []
    for role in ("collect", "fill"):
        name = f"safefill-{role}"
        tracked = subprocess.check_output(
            ["git", "ls-files", "-z", "--", f"skills/{name}"], cwd=ROOT).decode("utf-8").split("\0")
        files = [ROOT / item for item in tracked if item]
        if ROOT / "skills" / name / "SKILL.md" not in files:
            raise ValueError(f"Missing tracked SKILL.md: {name}")
        target = out / f"{name}.skill"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(files):
                if path.is_symlink() or not path.is_file():
                    raise ValueError(f"Not a regular source file: {path}")
                info = zipfile.ZipInfo(path.relative_to(ROOT / "skills").as_posix())
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, path.read_bytes())
        artifacts.append(target)
    installer = out / "install.py"
    shutil.copyfile(ROOT / "tools/install.py", installer)
    artifacts.append(installer)
    (out / "SHA256SUMS").write_text("".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in artifacts), encoding="ascii")
    for path in artifacts:
        print(path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    build(parser.parse_args().out.resolve())
