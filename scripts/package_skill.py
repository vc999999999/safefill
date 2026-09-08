"""Build a deterministic allowlisted dual-Skill archive; never walk task data."""
import argparse
import io
from pathlib import Path
import zipfile

import secure_io

ROOT = Path(__file__).resolve().parents[1]
TOP = 'yintian-skill/'


def build_package(out):
    files = {}
    for name in ('SKILL.md', 'README.md', 'meta.json', 'info.json', 'requirements.txt',
                 'requirements-core.txt', 'requirements-ocr.txt', 'requirements-dev.txt',
                 'requirements-quantization.txt', 'requirements-tested-py311.txt'):
        path = ROOT / name
        if path.is_file():
            files[name] = path
    for directory, patterns in {'scripts': ['*.py'], 'references': ['*.md'],
                                'assets': ['invite_template.html', 'mcp.example.json'],
                                'agents': ['*.yaml'], 'evals': ['evals.json']}.items():
        for pattern in patterns:
            for path in (ROOT / directory).glob(pattern):
                if not path.name.startswith(('test_', 'run_tests')):
                    files[path.relative_to(ROOT).as_posix()] = path
    for name in ('SKILL.md', 'README.md', 'meta.json', 'info.json', 'requirements.txt'):
        files['yintian-fill/' + name] = ROOT / 'yintian-fill' / name
    for directory, pattern in (('scripts', '*.py'), ('agents', '*.yaml'), ('evals', 'evals.json'), ('references', '*.md')):
        for path in (ROOT / 'yintian-fill' / directory).glob(pattern):
            if not path.name.startswith('test_'):
                files[path.relative_to(ROOT).as_posix()] = path
    # An independently installed filler uses identical, generated shared modules.
    for name in ('collection.py', 'ocr_matcher.py', 'secure_io.py', 'evidence_routing.py'):
        files['yintian-fill/scripts/' + name] = ROOT / 'scripts' / name
    files['yintian-fill/requirements-core.txt'] = ROOT / 'requirements-core.txt'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, path in sorted(files.items()):
            info = zipfile.ZipInfo(TOP + name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100600 << 16
            archive.writestr(info, secure_io.read_bytes(path))
    secure_io.atomic_write(out, buffer.getvalue(), overwrite=False)
    return {'files': len(files), 'out': str(out)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Build the industrial dual-Skill bundle')
    parser.add_argument('--out', required=True)
    print(build_package(parser.parse_args().out))
