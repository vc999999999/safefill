"""安全敏感代码的边界/负例测试：secure_io、vault、collection、ocr_matcher、privacy。"""
import base64
import importlib.util
import io
import json
import os
import stat
import sys
import unicodedata
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag
from hypothesis import given, settings, strategies as st

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "safefill-fill" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import collection  # noqa: E402
import ocr_matcher  # noqa: E402
import secure_io  # noqa: E402
import vault  # noqa: E402

# collect 侧 privacy.py 与 fill 侧模块无重名，但其内部 `import config` 依赖共享的
# config.py（两侧逐字节相同，经 sys.path 解析）；按路径加载以避免目录级 sys.path 冲突。
_privacy_spec = importlib.util.spec_from_file_location(
    "safefill_collect_privacy",
    Path(__file__).resolve().parents[1] / "skills" / "safefill-collect" / "scripts" / "privacy.py",
)
assert _privacy_spec is not None and _privacy_spec.loader is not None
privacy = importlib.util.module_from_spec(_privacy_spec)
_privacy_spec.loader.exec_module(privacy)


def private(path: Path) -> Path:
    path.mkdir(mode=0o700)
    if os.name != "nt":
        path.chmod(0o700)
    return path


def make_entry(value="张三", **overrides):
    entry = {"type": "text", "label": "姓名", "source": {"kind": "manual"}, "value": value}
    entry.update(overrides)
    return entry


def make_profile(**entries):
    return {"format": vault.FORMAT_V2, "entries": entries}


# ---------------------------------------------------------------- secure_io


def test_checked_path_rejects_dotdot(tmp_path):
    with pytest.raises(ValueError, match="PATH_UNSAFE"):
        secure_io.checked_path(tmp_path / "sub" / ".." / "evil")


def test_checked_path_rejects_symlink(tmp_path):
    target = tmp_path / "real.txt"
    target.write_bytes(b"x")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="PATH_UNSAFE"):
        secure_io.checked_path(link)


def test_checked_path_rejects_intermediate_symlink(tmp_path):
    realdir = tmp_path / "realdir"
    realdir.mkdir()
    linkdir = tmp_path / "linkdir"
    linkdir.symlink_to(realdir, target_is_directory=True)
    with pytest.raises(ValueError, match="PATH_UNSAFE"):
        secure_io.checked_path(linkdir / "inner.txt")


def test_checked_path_rejects_outside_root(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    outside = tmp_path / "base_evil"  # 前缀相同但不是同一目录组件
    outside.mkdir()
    with pytest.raises(ValueError, match="PATH_OUTSIDE"):
        secure_io.checked_path(outside, root=base)


def test_checked_path_allows_normal_path(tmp_path):
    result = secure_io.checked_path(tmp_path / "deep" / "file.txt")
    assert result.is_absolute() and result.name == "file.txt" and ".." not in result.parts


def test_read_bytes_rejects_over_limit(tmp_path):
    path = tmp_path / "big.bin"
    path.write_bytes(b"x" * 100)
    with pytest.raises(ValueError, match="FILE_LIMIT"):
        secure_io.read_bytes(path, limit=10)
    assert secure_io.read_bytes(path, limit=100) == b"x" * 100


def test_read_bytes_rejects_symlink(tmp_path):
    target = tmp_path / "real.txt"
    target.write_bytes(b"x")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="PATH_UNSAFE"):
        secure_io.read_bytes(link)


@pytest.mark.skipif(os.name == "nt", reason="Windows 上 os.open 目录直接报 PermissionError")
def test_read_bytes_rejects_directory(tmp_path):
    # macOS 上 O_RDONLY 打开目录直接 EISDIR（IsADirectoryError）；Linux 上打开成功但
    # fstat 判定非普通文件 → FILE_LIMIT。两种路径都拒绝目录。
    with pytest.raises((ValueError, IsADirectoryError)):
        secure_io.read_bytes(tmp_path)


def test_file_lock_second_lock_busy_and_reacquire(tmp_path):
    lock = tmp_path / "task.lock"
    with secure_io.file_lock(lock):
        with pytest.raises(RuntimeError, match="TASK_BUSY"):
            with secure_io.file_lock(lock):
                pass
    with secure_io.file_lock(lock):  # 释放后可再加锁
        pass


def test_file_lock_rejects_live_legacy_pid(tmp_path):
    lock = tmp_path / "legacy.lock"
    lock.write_text(f"pid={os.getpid()} legacy-writer\n", encoding="ascii")
    with pytest.raises(RuntimeError, match="TASK_BUSY"):
        with secure_io.file_lock(lock):
            pass


def test_file_lock_ignores_dead_legacy_pid(tmp_path):
    lock = tmp_path / "dead.lock"
    lock.write_text("pid=99999999 legacy-writer\n", encoding="ascii")
    with secure_io.file_lock(lock):  # 死进程的 pid 记录不阻塞新锁
        pass


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_atomic_write_content_and_permissions(tmp_path):
    target = tmp_path / "out.bin"
    secure_io.atomic_write(target, b"hello")
    assert target.read_bytes() == b"hello"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    secure_io.atomic_write(target, b"world")  # 默认 overwrite=True 覆盖已有目标
    assert target.read_bytes() == b"world"


def test_atomic_write_no_overwrite_refuses_existing(tmp_path):
    target = tmp_path / "keep.bin"
    secure_io.atomic_write(target, b"original")
    with pytest.raises(FileExistsError):
        secure_io.atomic_write(target, b"nope", overwrite=False)
    assert target.read_bytes() == b"original"
    fresh = tmp_path / "fresh.bin"
    secure_io.atomic_write(fresh, b"data", overwrite=False)
    assert fresh.read_bytes() == b"data"


# --------------------------------------------------------------------- vault


def test_load_vault_wrong_key(tmp_path):
    vault_dir = private(tmp_path / "vault")
    path = vault_dir / vault.VAULT_FILENAME
    vault.save_vault(path, "correct-key-" + "x" * 32, make_profile(name=make_entry()), create=True)
    with pytest.raises(ValueError, match="VAULT_UNLOCK_FAILED"):
        vault.load_vault(path, "wrong-key-" + "y" * 32)
    assert vault.load_vault(path, "correct-key-" + "x" * 32)["entries"]["name"]["value"] == "张三"


def test_load_vault_corrupted_ciphertext(tmp_path):
    vault_dir = private(tmp_path / "vault")
    path = vault_dir / vault.VAULT_FILENAME
    vault.save_vault(path, "k" * 40, make_profile(name=make_entry()), create=True)
    data = bytearray(path.read_bytes())
    data[-10] ^= 0xFF  # 篡改密文/nonce 区域一个字节
    path.write_bytes(bytes(data))
    with pytest.raises(ValueError, match="VAULT_UNLOCK_FAILED"):
        vault.load_vault(path, "k" * 40)


class _FutureDatetime(datetime):
    """vault.open_confirmation 使用模块级 datetime.now(timezone.utc)，monkeypatch 它构造过期视角。"""

    @classmethod
    def now(cls, tz=None):
        return datetime.now(tz) + timedelta(seconds=vault.CONFIRMATION_TTL_SECONDS + 60)


def test_confirmation_expired_rejected(tmp_path, monkeypatch):
    work = private(tmp_path / "work")
    key = "k" * 40
    path = work / "c.yintian-confirmation"
    vault.seal_confirmation(path, key, "vault-fill", {"request_sha256": "ab"})
    assert vault.open_confirmation(path, key, "vault-fill")["operation"] == "vault-fill"
    monkeypatch.setattr(vault, "datetime", _FutureDatetime)
    with pytest.raises(RuntimeError, match="CONFIRMATION_EXPIRED"):
        vault.open_confirmation(path, key, "vault-fill")


def test_confirmation_issued_in_future_rejected(tmp_path, monkeypatch):
    work = private(tmp_path / "work")
    key = "k" * 40
    path = work / "future.yintian-confirmation"
    monkeypatch.setattr(vault, "datetime", _FutureDatetime)  # 在"未来"签发
    vault.seal_confirmation(path, key, "vault-fill", {})
    monkeypatch.undo()
    with pytest.raises(RuntimeError, match="CONFIRMATION_EXPIRED"):  # issued_at > now + 60
        vault.open_confirmation(path, key, "vault-fill")


def test_confirmation_wrong_operation_rejected(tmp_path):
    work = private(tmp_path / "work")
    key = "k" * 40
    path = work / "op.yintian-confirmation"
    vault.seal_confirmation(path, key, "vault-stage", {})
    with pytest.raises(ValueError, match="CONFIRMATION_INVALID"):
        vault.open_confirmation(path, key, "vault-fill")


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_load_key_rejects_broad_permissions(tmp_path):
    key_dir = private(tmp_path / "keys")
    key = key_dir / vault.KEY_FILENAME
    key.write_text("k" * 43, encoding="ascii")
    key.chmod(0o644)
    with pytest.raises(RuntimeError, match="VAULT_PERMISSIONS"):
        vault.load_or_create_key(key)


def test_load_key_rejects_short_secret(tmp_path):
    key_dir = private(tmp_path / "keys")
    key = key_dir / vault.KEY_FILENAME
    key.write_text("tooshort", encoding="ascii")
    if os.name != "nt":
        key.chmod(0o600)
    with pytest.raises(RuntimeError, match="VAULT_KEY_INVALID"):
        vault.load_or_create_key(key)


def test_load_key_missing(tmp_path):
    with pytest.raises(RuntimeError, match="VAULT_KEY_MISSING"):
        vault.load_or_create_key(tmp_path / "keys" / vault.KEY_FILENAME)


def test_validate_profile_rejects_over_max_entries():
    entries = {f"field_{i:03d}": make_entry() for i in range(vault.MAX_ENTRIES + 1)}
    with pytest.raises(ValueError, match="VAULT_INVALID"):
        vault.validate_profile({"format": vault.FORMAT_V2, "entries": entries})
    ok = {f"field_{i:03d}": make_entry() for i in range(vault.MAX_ENTRIES)}  # 边界：恰好 100 条放行
    assert len(vault.validate_profile({"format": vault.FORMAT_V2, "entries": ok})["entries"]) == vault.MAX_ENTRIES


def test_validate_entry_rejects_long_label():
    with pytest.raises(ValueError, match="VAULT_INVALID"):
        vault.validate_entry(make_entry(label="x" * 101))
    assert vault.validate_entry(make_entry(label="x" * 100))["label"] == "x" * 100


def test_validate_entry_rejects_bad_attachment_sha256():
    data = b"hello"
    item = {"data_b64": base64.b64encode(data).decode("ascii"), "size": len(data), "sha256": "0" * 64}
    with pytest.raises(ValueError, match="VAULT_INVALID"):
        vault.validate_entry({"type": "image_attachment", "label": "", "source": {"kind": "manual"},
                              "attachments": [item]})
    good = {**item, "sha256": collection.sha256_bytes(data)}
    result = vault.validate_entry({"type": "image_attachment", "label": "", "source": {"kind": "manual"},
                                   "attachments": [good]})
    assert result["attachments"] == [good]


def test_validate_source_rejects_bad_kind_and_sha256():
    with pytest.raises(ValueError, match="VAULT_INVALID"):
        vault.validate_entry(make_entry(source={"kind": "evil-source"}))
    with pytest.raises(ValueError, match="VAULT_INVALID"):
        vault.validate_entry(make_entry(source={"kind": "manual", "sha256": "not-hex"}))


def _form_and_profile():
    form = {"fields": [
        {"id": "name", "label": "姓名", "type": "text", "required": True, "sensitive": True},
        {"id": "photo", "label": "照片", "type": "image_attachment", "required": False, "sensitive": True},
    ]}
    profile = {"entries": {"name": make_entry()}}
    return form, profile


def test_select_fields_mapping_invalid_branches():
    form, profile = _form_and_profile()
    with pytest.raises(ValueError, match="MAPPING_INVALID"):
        vault.select_fields(form, profile, {"ghost": "name"})  # 映射键不在本次请求字段中
    with pytest.raises(ValueError, match="MAPPING_INVALID"):
        vault.select_fields(form, profile, {"name": 42})  # 映射取值必须是字符串
    with pytest.raises(ValueError, match="MAPPING_INVALID"):
        vault.select_fields(form, profile, {"name": "missing_entry"})  # 映射的保险柜条目不存在
    with pytest.raises(ValueError, match="MAPPING_INVALID"):
        vault.select_fields(form, profile, {"photo": "name"})  # 映射条目类型与字段类型不同
    values, attachments, missing, matches = vault.select_fields(form, profile, {"name": "name"})
    assert values == {"name": "张三"} and missing == ["photo"] and matches == {"name": "name"}


def _write_v1_source(source_dir: Path, password="password") -> Path:
    old = {"version": 1, "types": {"name": "text"}, "values": {"name": "张三"}, "attachments": {}}
    source = source_dir / "vault.yintian-vault"
    source.write_bytes(collection.canonical(collection.aes_gcm_seal(
        vault.FORMAT_V1, collection.canonical(old), password, vault.FORMAT_V1.encode())))
    if os.name != "nt":
        source.chmod(0o600)
    return source


def test_migrate_v1_conflict_existing_target(tmp_path):
    source = _write_v1_source(private(tmp_path / "old"))
    target_dir, key_dir = private(tmp_path / "new"), private(tmp_path / "keys")
    target = target_dir / vault.VAULT_FILENAME
    target.write_bytes(b"existing")
    with pytest.raises(RuntimeError, match="VAULT_MIGRATION_CONFLICT"):
        vault.migrate_v1(source, "password", target, key_dir / vault.KEY_FILENAME)


def test_migrate_v1_conflict_existing_key_only(tmp_path):
    source = _write_v1_source(private(tmp_path / "old"))
    target_dir, key_dir = private(tmp_path / "new"), private(tmp_path / "keys")
    (key_dir / vault.KEY_FILENAME).write_bytes(b"existing-key")
    with pytest.raises(RuntimeError, match="VAULT_MIGRATION_CONFLICT"):
        vault.migrate_v1(source, "password", target_dir / vault.VAULT_FILENAME, key_dir / vault.KEY_FILENAME)


def test_migrate_v1_rejects_v2_source(tmp_path):
    source_dir = private(tmp_path / "src")
    source = source_dir / vault.VAULT_FILENAME
    vault.save_vault(source, "k" * 40, make_profile(), create=True)
    with pytest.raises(ValueError, match="VAULT_MIGRATION_NOT_REQUIRED"):
        vault.migrate_v1(source, "password", tmp_path / "t" / vault.VAULT_FILENAME, tmp_path / "k" / vault.KEY_FILENAME)


# --------------------------------------------------------------- collection


def _kdf(**overrides):
    params = {"name": "scrypt", "salt": base64.b64encode(b"s" * 16).decode("ascii"),
              "n": collection.SCRYPT_N, "r": collection.SCRYPT_R, "p": collection.SCRYPT_P}
    params.update(overrides)
    return params


@pytest.mark.parametrize("params", [
    "scrypt",                                              # 非 dict
    _kdf(name="pbkdf2"),                                   # 不支持的 KDF
    {"name": "scrypt"},                                    # 缺少 n/r/p
    _kdf(salt="!!!"),                                      # salt 非法 base64
    _kdf(salt=base64.b64encode(b"s" * 8).decode("ascii")),  # salt 长度非 16
    _kdf(n=2**9),                                          # N 低于下限
    _kdf(n=2**17),                                         # N 高于上限
    _kdf(n=2**10 + 2**11),                                 # N 非 2 的幂
    _kdf(r=0), _kdf(r=9),                                  # r 越界
    _kdf(p=0), _kdf(p=3),                                  # p 越界
], ids=["not-dict", "wrong-name", "missing-keys", "bad-b64", "short-salt",
        "n-low", "n-high", "n-not-pow2", "r-zero", "r-high", "p-zero", "p-high"])
def test_validate_kdf_params_rejects_out_of_range(params):
    with pytest.raises(ValueError, match="KDF 参数"):
        collection.validate_kdf_params(params)


def test_validate_kdf_params_accepts_boundary():
    salt, n, r, p = collection.validate_kdf_params(_kdf())
    assert salt == b"s" * 16 and (n, r, p) == (collection.SCRYPT_N, collection.SCRYPT_R, collection.SCRYPT_P)
    assert collection.validate_kdf_params(_kdf(n=2**10, r=1, p=2))[1:] == (2**10, 1, 2)


def test_verify_submission_auth():
    token = "t" * 43
    invite = {"token_hash": collection.sha256_bytes(token.encode())}
    task = {"submission_auth": collection.AUTH_VERSION}
    envelope = {"format_version": "v", "task_id": "t", "invite_id": "i"}
    tag = collection.submission_auth_tag(envelope, invite["token_hash"])
    collection.verify_submission_auth(task, {**envelope, "auth_tag": tag}, invite)  # 正确 HMAC 通过
    with pytest.raises(ValueError, match="SUBMISSION_AUTH_FAILED"):
        collection.verify_submission_auth(task, {**envelope, "auth_tag": "0" * 64}, invite)
    with pytest.raises(ValueError, match="SUBMISSION_AUTH_FAILED"):
        collection.verify_submission_auth(task, envelope, invite)  # 缺少 auth_tag
    collection.verify_submission_auth({}, envelope, invite)  # 未启用认证的任务不校验


@pytest.mark.parametrize("rule,value,expected", [
    ("last4", "13812345678", "*******5678"),
    ("last4", "1234", "****"),       # 长度恰好 4 的边界
    ("last4", "abc", "***"),         # 短输入整体打码
    ("last4", "", ""),
    ("mid4", "13812345678", "138****5678"),
    ("mid4", "1234567", "*******"),  # 长度恰好 7 的边界
    ("mid4", "123456", "******"),
])
def test_mask_value(rule, value, expected):
    assert collection.mask_value(rule, value) == expected


def test_mask_value_rejects_unknown_rule():
    with pytest.raises(ValueError, match="不支持的脱敏规则"):
        collection.mask_value("all", "13812345678")


@pytest.mark.parametrize("value", ["../etc/passwd", "..\\..\\win", "a/b\\c:d*e?f\"g<h>i|j"])
def test_safe_filename_component_strips_dangerous_chars(value):
    result = collection.safe_filename_component(value)
    assert result and "/" not in result and "\\" not in result
    assert not result.startswith(".") and ".." not in result


def test_safe_filename_component_control_chars_and_fallback():
    assert collection.safe_filename_component("\x00\x1fname\x7f") == "name"
    assert collection.safe_filename_component("///") == "item"  # 全部消毒后回退 fallback
    assert collection.safe_filename_component("") == "item"


def test_safe_filename_component_length_limits():
    assert len(collection.safe_filename_component("a" * 500)) <= 60
    long_cjk = collection.safe_filename_component("张" * 100)
    assert len(long_cjk.encode("utf-8")) <= 120


def test_terminal_text_strips_control_chars():
    assert collection.terminal_text("a\tb\x00c​d") == "a b c d"
    assert all(unicodedata.category(c) not in {"Cc", "Cf"} for c in collection.terminal_text("\x00\x1f\x7f‍"))
    assert collection.terminal_text(123) == "123"


def _zip_bytes(members: dict[str, bytes]) -> io.BytesIO:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    buffer.seek(0)
    return buffer


def test_read_package_member_enforces_limits(monkeypatch):
    with zipfile.ZipFile(_zip_bytes({"big.bin": b"x" * 2048})) as archive:
        with pytest.raises(ValueError, match="超过安全上限"):
            collection.read_package_member(archive, "big.bin", 1024)  # 超过剩余额度
        assert collection.read_package_member(archive, "big.bin", 2048) == b"x" * 2048
        monkeypatch.setattr(collection, "MAX_PACKAGE_MEMBER_BYTES", 100)
        with pytest.raises(ValueError, match="超过安全上限"):
            collection.read_package_member(archive, "big.bin", 10**9)  # 超过单项上限


def test_open_task_package_rejects_bad_envelope_and_password(tmp_path):
    bad = tmp_path / "bad.yintian-package"
    bad.write_bytes(b"{not-json")
    with pytest.raises(ValueError, match="交接包信封无效"):
        collection.open_task_package(bad)
    wrong_format = tmp_path / "fmt.yintian-package"
    wrong_format.write_bytes(json.dumps({"format": "yintian-task/9"}).encode())
    with pytest.raises(ValueError, match="交接包格式不受支持"):
        collection.open_task_package(wrong_format)

    task_id = "YT-20260101-ABCDEF"
    envelope = collection.aes_gcm_seal(collection.ENCRYPTED_TASK_PACKAGE_VERSION,
                                       _zip_bytes({"package.json": b"{}"}).getvalue(),
                                       "right-password", collection.task_package_aad(task_id))
    envelope["task_id"] = task_id
    package = tmp_path / "enc.yintian-package"
    package.write_bytes(collection.canonical(envelope))
    with pytest.raises(ValueError, match="交接密码错误或交接包已被篡改"):
        collection.open_task_package(package, "wrong-password")
    archive, encrypted = collection.open_task_package(package, "right-password")
    assert encrypted and archive.namelist() == ["package.json"]
    archive.close()


def test_open_task_package_plaintext_zip(tmp_path, capsys):
    package = tmp_path / "plain.zip"
    package.write_bytes(_zip_bytes({"package.json": b"{}"}).getvalue())
    archive, encrypted = collection.open_task_package(package)
    assert not encrypted and archive.namelist() == ["package.json"]
    archive.close()
    assert "明文交接包" in capsys.readouterr().err


def test_import_task_rejects_manifest_over_max_files(tmp_path):
    metadata = {"task_id": "YT-20260101-ABCDEF", "package_version": collection.TASK_PACKAGE_VERSION,
                "manifest": {f"e{i}": "x" for i in range(collection.MAX_PACKAGE_FILES + 1)}}
    package = tmp_path / "huge.zip"
    package.write_bytes(_zip_bytes({"package.json": json.dumps(metadata).encode()}).getvalue())
    with pytest.raises(ValueError, match="清单无效或文件过多"):
        collection.import_task(package, tmp_path / "out")


def test_aes_gcm_roundtrip_and_tamper_failures():
    envelope = collection.aes_gcm_seal("test-format", b"secret", "password", b"aad")
    assert collection.aes_gcm_open(envelope, "password", b"aad") == b"secret"
    with pytest.raises(InvalidTag):
        collection.aes_gcm_open(envelope, "password", b"wrong-aad")
    with pytest.raises(InvalidTag):
        collection.aes_gcm_open(envelope, "wrong-password", b"aad")
    tampered = dict(envelope)
    raw = bytearray(base64.b64decode(envelope["ciphertext"]))
    raw[0] ^= 0xFF
    tampered["ciphertext"] = base64.b64encode(bytes(raw)).decode("ascii")
    with pytest.raises(InvalidTag):
        collection.aes_gcm_open(tampered, "password", "aad".encode())


def test_aes_gcm_rejects_malformed_envelope():
    with pytest.raises(ValueError, match="加密信封格式不受支持"):
        collection.aes_gcm_open({"cipher": "AES-128-GCM"}, "pw", b"aad")
    envelope = collection.aes_gcm_seal("test-format", b"x", "pw")
    bad_nonce = dict(envelope, nonce=base64.b64encode(b"n" * 8).decode("ascii"))
    with pytest.raises(ValueError, match="nonce 无效"):
        collection.aes_gcm_open(bad_nonce, "pw", "test-format".encode())
    missing = dict(envelope)
    del missing["ciphertext"]
    with pytest.raises(ValueError, match="缺少 nonce 或密文"):
        collection.aes_gcm_open(missing, "pw", "test-format".encode())


# -------------------------------------------------------------- ocr_matcher


def test_validate_chinese_id_accepts_valid():
    assert ocr_matcher.validate_chinese_id("11010519491231002X") is True
    assert ocr_matcher.validate_chinese_id("11010519491231002x") is True  # 小写 x 归一化
    assert ocr_matcher.validate_chinese_id(" 11010519491231002X ") is True  # 空白剥离


def test_validate_chinese_id_rejects_bad_checksum():
    assert ocr_matcher.validate_chinese_id("110105194912310020") is False


def test_validate_chinese_id_rejects_bad_length():
    assert ocr_matcher.validate_chinese_id("11010519491231002") is False
    assert ocr_matcher.validate_chinese_id("11010519491231002X0") is False
    assert ocr_matcher.validate_chinese_id("") is False


def test_validate_chinese_id_rejects_bad_chars_and_date():
    assert ocr_matcher.validate_chinese_id("1101051949123100AX") is False
    assert ocr_matcher.validate_chinese_id("110105194913310020") is False  # 13 月非法


def test_validate_chinese_id_unicode_digit_raises_valueerror():
    # ² 的 isdigit() 为 True 但 int("²") 抛 ValueError：现有实现不捕获，记录该行为。
    with pytest.raises(ValueError):
        ocr_matcher.validate_chinese_id("110105194912310²2X")


# ------------------------------------------------------------------ privacy


def test_assert_in_vault_allows_inside_path(tmp_path):
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    inside = vault_dir / "sub" / "file.txt"
    assert privacy.assert_in_vault(str(inside), str(vault_dir)) == str(inside)
    assert privacy.assert_in_vault(str(vault_dir), str(vault_dir)) == str(vault_dir)


def test_assert_in_vault_rejects_outside_path(tmp_path):
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    with pytest.raises(privacy.PrivacyViolation):
        privacy.assert_in_vault(str(tmp_path / "outside.txt"), str(vault_dir))
    with pytest.raises(privacy.PrivacyViolation):
        privacy.assert_in_vault(str(tmp_path / "vault_evil" / "x"), str(vault_dir))  # 同前缀兄弟目录


def test_assert_in_vault_no_vault_dir_passes_through():
    assert privacy.assert_in_vault("/anywhere/file", "") == "/anywhere/file"


# ---------------------------------------------------------- hypothesis 属性

_SAFE_TEXT = st.text(alphabet=st.characters(blacklist_categories=("Cs",)))  # type: ignore[arg-type]  # hypothesis 存根要求 Literal 类别集合，运行时接受任意可迭代


@given(_SAFE_TEXT)
@settings(max_examples=50, derandomize=True)
def test_prop_validate_chinese_id_only_bool_or_valueerror(value):
    try:
        result = ocr_matcher.validate_chinese_id(value)
    except ValueError:
        return  # 预期异常类型：Unicode 数字（如 ²）触发 int() 的 ValueError
    assert isinstance(result, bool)


@given(_SAFE_TEXT)
@settings(max_examples=50, derandomize=True)
def test_prop_safe_filename_component_output_safe(value):
    result = collection.safe_filename_component(value)
    assert result and "/" not in result and "\\" not in result
    assert all(unicodedata.category(c) not in {"Cc", "Cf"} for c in result)
    assert len(result) <= 60 and len(result.encode("utf-8")) <= 120


_JSON_SCALAR = st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False, allow_infinity=False) | _SAFE_TEXT
_JSON_VALUE = st.recursive(
    _JSON_SCALAR,
    lambda children: st.lists(children) | st.dictionaries(_SAFE_TEXT, children),
    max_leaves=20,
)


@given(_JSON_VALUE)
@settings(max_examples=50, derandomize=True)
def test_prop_canonical_json_roundtrip_idempotent(value):
    assert collection.canonical(json.loads(collection.canonical(value))) == collection.canonical(value)
