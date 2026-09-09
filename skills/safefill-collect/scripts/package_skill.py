"""Deterministic allowlisted delivery of the two sibling Skills."""
import argparse
import io
from pathlib import Path
import zipfile
import secure_io

ROOT = Path(__file__).resolve().parents[2]
SHARED = ('collection.py', 'ocr_matcher.py', 'secure_io.py', 'evidence_routing.py', 'config.py', 'openvino_runtime.py')


def build_package(out):
    files = {}
    for name in SHARED:
        if secure_io.read_bytes(ROOT / 'safefill-collect/scripts' / name) != secure_io.read_bytes(ROOT / 'safefill-fill/scripts' / name):
            raise ValueError('SHARED_MODULE_DRIFT: 请同步两个 Skill 的公共模块')
    for skill in ('safefill-collect', 'safefill-fill'):
        base = ROOT / skill
        for name in ('SKILL.md', 'README.md', 'meta.json', 'info.json', 'requirements.txt',
                     'requirements-core.txt', 'requirements-ocr.txt', 'requirements-quantization.txt'):
            if (base / name).is_file():
                files[skill + '/' + name] = base / name
        for directory, patterns in {'scripts': ['*.py'], 'references': ['*.md'],
                                    'assets': ['invite_template.html', 'mcp.example.json'],
                                    'agents': ['*.yaml']}.items():
            for pattern in patterns:
                for path in (base / directory).glob(pattern):
                    if path.name.startswith(('test_', 'run_tests', 'conftest', 'package_skill')):
                        continue
                    files[path.relative_to(ROOT).as_posix()] = path
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, path in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100600 << 16
            archive.writestr(info, secure_io.read_bytes(path))
    secure_io.atomic_write(out, buffer.getvalue(), overwrite=False)
    return {'files': len(files), 'out': str(out)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='打包两个独立 Skill')
    parser.add_argument('--out', required=True)
    print(build_package(parser.parse_args().out))
