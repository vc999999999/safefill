"""Install trusted SafeFill .skill packages and core dependencies into a chosen Skills directory."""
import argparse
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import stat
import subprocess
import sys
import venv
import zipfile


def install(package: Path, destination: Path, role: str, *, dependencies: bool = True) -> Path:
    name = f"safefill-{role}"
    target = destination / name
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"Already exists; no files changed: {target}")
    with zipfile.ZipFile(package) as archive:
        members = archive.infolist()
        if not members or len(members) > 100 or sum(item.file_size for item in members) > 10 * 1024 * 1024:
            raise ValueError("Invalid package size")
        seen = set()
        for item in members:
            path = PurePosixPath(item.filename)
            if (item.orig_filename != item.filename or len(path.parts) < 2
                    or path.parts[0] != name or item.filename != path.as_posix()
                    or any(not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", part)
                           or part.endswith(".") or PureWindowsPath(part).is_reserved() for part in path.parts)
                    or stat.S_ISLNK(item.external_attr >> 16)
                    or item.filename.lower() in seen):
                raise ValueError("Invalid package path or duplicate member")
            seen.add(item.filename.lower())
        entrypoint = "collector" if role == "collect" else "fill"
        required = {f"{name}/SKILL.md", f"{name}/requirements.txt", f"{name}/scripts/{entrypoint}.py"}
        if not required.issubset({item.filename for item in members}):
            raise ValueError("Incomplete SafeFill package")
        # mkdir without exist_ok owns exactly this new directory; existing installs are never removed.
        target.mkdir(parents=True)
        try:
            for item in members:
                relative = PurePosixPath(item.filename).parts[1:]
                output = target.joinpath(*relative)
                output.parent.mkdir(parents=True, exist_ok=True)
                with output.open("xb") as stream:
                    stream.write(archive.read(item))
            if dependencies:
                environment = target / ".venv"
                venv.EnvBuilder(with_pip=True).create(environment)
                python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
                subprocess.run([str(python), "-m", "pip", "install", "-r", str(target / "requirements.txt")], check=True)
                result = subprocess.run([str(python), str(target / "scripts" / f"{entrypoint}.py"), "doctor"],
                                        check=True, capture_output=True, text=True, encoding="utf-8")
                report = json.loads(result.stdout)
                ready = report.get("core_ready") if role == "collect" else report.get("core", {}).get("ok")
                if not ready:
                    raise RuntimeError("Core dependency check failed")
        except BaseException:
            shutil.rmtree(target)
            raise
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("fill", "collect", "both"), default="both")
    parser.add_argument("--dest", type=Path, required=True, help="The host Agent's Skills directory")
    parser.add_argument("--packages", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--no-deps", action="store_true", help="Only unpack; use an existing Python environment")
    args = parser.parse_args()
    if not (3, 11) <= sys.version_info[:2] <= (3, 13):
        parser.error("Use Python 3.11–3.13")
    roles = ("fill", "collect") if args.role == "both" else (args.role,)
    destination = args.dest.expanduser().resolve()
    for role in roles:
        package = args.packages / f"safefill-{role}.skill"
        target = destination / f"safefill-{role}"
        if not package.is_file() or target.exists() or target.is_symlink():
            parser.error(f"Package missing or destination already exists: {package} / {target}")
    for role in roles:
        target = install(args.packages / f"safefill-{role}.skill", destination, role, dependencies=not args.no_deps)
        print(f"Installed: {target}")
    print("Reload your host's Skills. OCR/VLM models are optional and were not downloaded.")


if __name__ == "__main__":
    main()
