"""SafeFill · 收集端 CLI：任务创建、收件、解密校验、Excel 导出与任务交接。"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import string
import sys
import tempfile
import warnings
import zipfile
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import secure_io
from collection import (
    ATTACHMENT_TYPES,
    MAX_ENVELOPE_BYTES,
    MAX_IMAGE_PIXELS,
    MAX_PACKAGE_BYTES,
    MAX_PACKAGE_ENVELOPE_BYTES,
    MAX_PACKAGE_FILES,
    MAX_PACKAGE_MEMBER_BYTES,
    MAX_PDF_PAGES,
    MAX_VALUE_CHARS,
    NOTICE_KEYS,
    OPEN_INVITE_ID_RE,
    PDF_RENDER_SCALE,
    NOTICE_FORMAT_VERSION,
    REQUEST_FORMAT_VERSION,
    SUBMISSION_FORMAT_VERSION,
    TASK_ID_RE,
    aad_for,
    aes_gcm_open,
    aes_gcm_seal,
    atomic_write,
    canonical,
    decrypt_private_key,
    dump_json,
    error_report,
    expired_message,
    generate_keys,
    load_json,
    normalize_submitted_at,
    normalize_value,
    now_iso,
    parse_time,
    positive_int_env,
    random_id,
    sha256_bytes,
    sha256_file,
    suppress_private_output,
    task_expired,
    task_late,
    valid_private_key_blob,
    validate_envelope_header,
    validate_field_definitions,
    validate_payload,
)

DB_VERSION = 2
TASK_PACKAGE_VERSION = "yintian-task/2"
ENCRYPTED_TASK_PACKAGE_VERSION = "yintian-task/3"
PASSED_STATES = ("verified", "verified_manual")
MAX_VERSIONS_PER_INVITE = positive_int_env("YINTIAN_MAX_VERSIONS_PER_INVITE", 10)
MAX_OPEN_INVITES = positive_int_env("YINTIAN_MAX_OPEN_INVITES", 10_000)
MAX_TASK_SUBMISSION_BYTES = positive_int_env("YINTIAN_MAX_TASK_SUBMISSION_BYTES", 1024 * 1024 * 1024)


def generate_password(length: int = 28) -> str:
    alphabet = string.ascii_letters + string.digits + "-_.~"
    while True:
        value = "".join(secrets.choice(alphabet) for _ in range(length))
        if any(c.islower() for c in value) and any(c.isupper() for c in value) and any(c.isdigit() for c in value):
            return value


def open_control_terminal():
    """打开控制终端用于显示一次性密码（POSIX 用 /dev/tty，Windows 用 CON）；打不开时返回 None。"""
    try:
        return open("CON" if os.name == "nt" else "/dev/tty", "w", encoding="utf-8", errors="replace")
    except OSError:
        return None


def print_once_secret(heading: str, secret: str, footnote: str, *, stream=None) -> None:
    """一次性密码只写控制终端，绝不回退到会被 Agent 采集的标准输出流。"""
    owned = stream is None
    stream = open_control_terminal() if owned else stream
    if stream is None:
        raise RuntimeError("SECRET_TTY_REQUIRED: 无法打开安全控制终端")
    try:
        print(f"\n{heading}", file=stream)
        print(secret, file=stream)
        print(footnote + "\n", file=stream)
        stream.flush()
    finally:
        if owned:
            stream.close()


def require_tty(action: str) -> None:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise RuntimeError(f"{action} 必须在交互终端中运行；TTY 检查不能证明该终端未被 Agent 控制。")


def load_task(task_dir: str | Path) -> tuple[Path, dict[str, Any]]:
    root = secure_io.checked_path(task_dir)
    task_path = root / "task.json"
    if not task_path.is_file():
        raise FileNotFoundError(f"不是有效任务目录: {root}")
    task = load_json(task_path)
    if not isinstance(task, dict) or not isinstance(task.get("task_id"), str) or not TASK_ID_RE.fullmatch(task["task_id"]):
        raise ValueError("TASK_INVALID: task.json 中的 task_id 无效")
    if task.get("format_version") != SUBMISSION_FORMAT_VERSION:
        raise ValueError("LEGACY_TASK_UNSUPPORTED: 旧任务须使用原版本完成，新版不迁移旧任务")
    required = ("key_id", "template_version", "schema_hash", "notice_hash", "fields", *NOTICE_KEYS)
    if any(key not in task for key in required) or not isinstance(task["fields"], list):
        raise ValueError("TASK_INVALID: task.json 缺少必要配置")
    for key in (*NOTICE_KEYS, "template_version"):
        if not isinstance(task[key], str) or not task[key] or len(task[key]) > MAX_VALUE_CHARS:
            raise ValueError(f"TASK_INVALID: task.json 文本字段无效: {key}")
    if not isinstance(task["key_id"], str) or not re.fullmatch(r"[0-9a-f]{24}", task["key_id"]) or any(not isinstance(task[key], str) or not re.fullmatch(r"[0-9a-f]{64}", task[key]) for key in ("schema_hash", "notice_hash")):
        raise ValueError("TASK_INVALID: task.json 公钥或摘要标识无效")
    try:
        deadline, retention = parse_time(task["deadline"]), parse_time(task["retention_until"])
    except ValueError:
        raise ValueError("TASK_INVALID: task.json 日期时间无效") from None
    if retention <= deadline:
        raise ValueError("RETENTION_INVALID: task.json 保存期限无效")
    if validate_field_definitions(task["fields"]) != task["fields"]:
        raise ValueError("SCHEMA_INVALID: task.json 字段模板未规范化")
    if sha256_bytes(canonical(task["fields"])) != task["schema_hash"]:
        raise ValueError("TASK_INVALID: task.json 字段模板哈希不匹配")
    if sha256_bytes(canonical({key: task[key] for key in NOTICE_KEYS})) != task["notice_hash"]:
        raise ValueError("TASK_INVALID: task.json 告知内容哈希不匹配")
    return root, task


@contextmanager
def task_lock(root: Path):
    with secure_io.file_lock(root / ".write.lock"):
        with closing(connect_db(root)) as db:
            if db.execute("PRAGMA user_version").fetchone()[0] != DB_VERSION:
                raise RuntimeError("SCHEMA_UNSUPPORTED: 任务数据库版本不受支持，请新建请求包")
        yield


def connect_db(root: Path) -> sqlite3.Connection:
    path = secure_io.checked_path(root / "state.sqlite3", root)
    for suffix in ("-wal", "-shm", "-journal"):
        secure_io.checked_path(str(path) + suffix, root)
    if not path.is_file():
        raise FileNotFoundError("DATABASE_MISSING: 任务数据库不存在")
    db = sqlite3.connect(path, timeout=5.0)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def init_db(root: Path) -> None:
    db_path = root / "state.sqlite3"
    secure_io.atomic_write(db_path, b"", overwrite=False)
    with closing(connect_db(root)) as db, db:
        db.executescript(
            """
            CREATE TABLE invites (
                invite_id TEXT PRIMARY KEY,
                employee_id TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                token_hash TEXT NOT NULL,
                -- Legacy cache columns retained for DB2 compatibility; current state is derived from revision.
                status TEXT NOT NULL DEFAULT 'invited',
                current_submission_id INTEGER,
                created_at TEXT NOT NULL
            );
            CREATE TABLE submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invite_id TEXT NOT NULL REFERENCES invites(invite_id),
                version INTEGER NOT NULL,
                revision INTEGER NOT NULL CHECK(revision BETWEEN 1 AND 2147483647),
                sha256 TEXT NOT NULL UNIQUE,
                path TEXT NOT NULL,
                received_at TEXT NOT NULL,
                submitted_at TEXT,
                reviewed_at TEXT,
                late INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                missing_fields TEXT NOT NULL DEFAULT '[]',
                conflict_fields TEXT NOT NULL DEFAULT '[]',
                attachment_count INTEGER NOT NULL DEFAULT 0,
                consent_confirmed INTEGER NOT NULL DEFAULT 0,
                UNIQUE(invite_id, version)
            );
            CREATE TABLE audit (
                id INTEGER PRIMARY KEY,
                action TEXT NOT NULL,
                submission_id INTEGER,
                at TEXT NOT NULL,
                result TEXT NOT NULL,
                reason TEXT NOT NULL,
                operator TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX submissions_revision ON submissions(invite_id, revision);
            PRAGMA user_version=2;
            """
        )
    os.chmod(root / "state.sqlite3", 0o600)


def audit(db, action, submission_id=None, result="ok", reason="", operator=""):
    db.execute("INSERT INTO audit(action,submission_id,at,result,reason,operator) VALUES(?,?,?,?,?,?)",
               (action, submission_id, now_iso(), result, reason, operator))


def current_submissions(db, invite_id=None) -> list[dict[str, Any]]:
    """先选最大已验签 revision，再看业务状态；同序号分叉永不择一放行。"""
    query = """SELECT s.* FROM submissions s
               WHERE s.revision=(SELECT MAX(n.revision) FROM submissions n WHERE n.invite_id=s.invite_id)"""
    params: tuple[Any, ...] = ()
    if invite_id is not None:
        query += " AND s.invite_id=?"
        params = (invite_id,)
    query += " ORDER BY s.invite_id,s.version"
    heads: dict[str, dict[str, Any]] = {}
    for row in db.execute(query, params):
        if row["invite_id"] not in heads:
            heads[row["invite_id"]] = {**dict(row), "conflicting_versions": []}
        else:
            head = heads[row["invite_id"]]
            if not head["conflicting_versions"]:
                head["conflicting_versions"].append(head["version"])
            head["conflicting_versions"].append(row["version"])
            head["status"] = "revision_conflict"
            head["missing_fields"] = "[]"
            head["conflict_fields"] = '["revision_conflict"]'
    return list(heads.values())


def cmd_doctor(args):
    import importlib.util
    import importlib.metadata
    modules = {'cryptography': 'cryptography', 'PIL': 'pillow', 'openpyxl': 'openpyxl',
               'pypdfium2': 'pypdfium2', 'rapidocr_openvino': 'rapidocr-openvino', 'tkinter': None}
    checks = {}
    for module, package in modules.items():
        available = importlib.util.find_spec(module) is not None
        if module == 'tkinter' and available:
            try:
                __import__('tkinter')
            except ImportError:
                available = False
        try:
            version = importlib.metadata.version(package) if available and package else None
        except importlib.metadata.PackageNotFoundError:
            version = None
        checks[module] = {'available': available, 'version': version}
    python_supported = sys.version_info >= (3, 11)
    return {'python': sys.version.split()[0], 'python_supported': python_supported, 'dependencies': checks,
            'core_ready': python_supported and all(checks[k]['available'] for k in ('cryptography', 'PIL', 'openpyxl', 'pypdfium2')),
            'local_ocr_ready': checks['rapidocr_openvino']['available'],
            'manual_review_ready': checks['tkinter']['available']}


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    from collection import explicit_timezone

    if not isinstance(config, dict):
        raise ValueError("CONFIG_INVALID: 配置必须是 JSON 对象")
    for key in (*NOTICE_KEYS, "template_version", "fields"):
        if not config.get(key):
            raise ValueError(f"CONFIG_MISSING_FIELDS: 配置缺少必填项: {key}")
    for key in (*NOTICE_KEYS, "template_version"):
        if not isinstance(config[key], str) or len(config[key]) > MAX_VALUE_CHARS:
            raise ValueError(f"CONFIG_INVALID: 配置字段必须是长度不超过 {MAX_VALUE_CHARS} 的文本: {key}")
    for key in ("deadline", "retention_until"):
        if not explicit_timezone(config[key]):
            raise ValueError(f"TIME_ZONE_REQUIRED: {key} 必须带时间和时区偏移（例如 2026-10-01T18:00:00+08:00），"
                             "不接受纯日期或无时区时间，以免员工与 HR 对截止时刻理解不一致")
    deadline, retention = parse_time(config["deadline"]), parse_time(config["retention_until"])
    if deadline <= datetime.now(timezone.utc):
        raise ValueError("DEADLINE_INVALID: deadline 必须晚于当前时间")
    if retention <= deadline:
        raise ValueError("RETENTION_INVALID: retention_until 必须晚于 deadline")
    result = dict(config)
    result["fields"] = validate_field_definitions(config["fields"])
    return result


def local_task_secret_path(root: Path, task_id: str) -> Path:
    return secure_io.checked_path(root.parent / ".safefill-keys" / f"{task_id}.key", root.parent)


def save_local_task_secret(root: Path, task_id: str, secret: str) -> Path:
    path = local_task_secret_path(root, task_id)
    secure_io.atomic_write(path, secret.encode("ascii"), overwrite=False)
    return path


def load_local_task_secret(root: Path, task_id: str) -> str:
    path = local_task_secret_path(root, task_id)
    if not path.is_file():
        raise RuntimeError("LOCAL_KEY_UNAVAILABLE: 任务的本地密钥缺失；若是跨设备/跨目录迁移，请在源机器运行 export-task 并在本机 import-task")
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise RuntimeError(f"LOCAL_KEY_UNAVAILABLE: 任务的本地密钥权限不安全，请将 {path} 权限修为 600")
    try:
        secret = secure_io.read_bytes(path, 256).decode("ascii")
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError("LOCAL_KEY_UNAVAILABLE: 任务的本地密钥无法读取") from exc
    if not re.fullmatch(r"[A-Za-z0-9_.~-]{28,128}", secret):
        raise RuntimeError("LOCAL_KEY_INVALID: 任务的本地密钥格式无效")
    return secret


def create_request(config_path: Path, out_parent: Path) -> dict[str, Any]:
    config = validate_config(load_json(config_path))
    task_id = "YT-" + datetime.now().strftime("%Y%m%d") + "-" + random_id("", 6)
    root = secure_io.checked_path(out_parent) / task_id
    if root.exists():
        raise FileExistsError(f"任务目录已存在: {root}")
    root.mkdir(parents=True, mode=0o700)
    saved_secret_path = None
    try:
        (root / "submissions").mkdir(mode=0o700)
        local_secret = secrets.token_urlsafe(32)
        public_pem, private_blob, key_id = generate_keys(local_secret)
        task = {
            "format_version": SUBMISSION_FORMAT_VERSION,
            "task_id": task_id,
            "key_id": key_id,
            **{key: config[key] for key in NOTICE_KEYS},
            "template_version": config["template_version"],
            "schema_hash": sha256_bytes(canonical(config["fields"])),
            "notice_hash": sha256_bytes(canonical({key: config[key] for key in NOTICE_KEYS})),
            "fields": config["fields"],
            "created_at": now_iso(),
        }
        dump_json(root / "task.json", task)
        atomic_write(root / "public.pem", public_pem)
        atomic_write(root / "private.pem.enc", private_blob)
        request_path = root / f"REQUEST-{task_id}.yintian-request"
        dump_json(request_path, {"format": REQUEST_FORMAT_VERSION, "kind": "agent_request", "target_skill": "safefill-fill",
                                 "expects": {"reply_format": SUBMISSION_FORMAT_VERSION, "reply_suffix": ".yintian",
                                             "notice_format": NOTICE_FORMAT_VERSION,
                                             "delivery": "员工本人将 .yintian 回执文件发回发放方；勿代发"},
                                 **task, "public_key_pem": public_pem.decode("ascii")})
        init_db(root)
        saved_secret_path = save_local_task_secret(root, task_id, local_secret)
        ocr_bound = [field["id"] for field in config["fields"] if field.get("ocr_fields")]
        return {"task_id": task_id, "task_dir": str(root), "request": str(request_path), "key_fingerprint": key_id,
                "deadline": config["deadline"], "retention_until": config["retention_until"], "ocr_bound_fields": ocr_bound,
                "reminder": (f"收件、汇总与查看必须在保存期限 {config['retention_until']} 之前完成，到期后按告知承诺不再解密任何回执"
                             + ("；已启用附件自动比对的字段：" + ", ".join(ocr_bound) + "，比对不通过的回执需要 HR 本人在终端运行 decide 逐项裁定" if ocr_bound else ""))}
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        if saved_secret_path is not None:
            saved_secret_path.unlink(missing_ok=True)
        raise


def cmd_create_request(args) -> dict[str, Any]:
    return create_request(Path(args.config), Path(args.out))


def ingest_task(task_dir: str | Path, submissions_dir: str | Path, recursive: bool = False) -> dict[str, Any]:
    root, task = load_task(task_dir)
    if task_expired(task):
        raise RuntimeError(expired_message(task, "接收新提交"))
    source = secure_io.checked_path(submissions_dir)
    if not source.is_dir():
        raise NotADirectoryError(source)
    paths = []
    skipped_directories = 0
    if recursive:
        candidates = source.rglob("*")
    else:
        candidates = source.iterdir()
    for path in candidates:
        if path.suffix == ".yintian":
            paths.append(path)
            if len(paths) > MAX_PACKAGE_FILES:
                raise ValueError(f"INBOX_LIMIT: 单次收件最多处理 {MAX_PACKAGE_FILES} 个回执")
        elif not recursive and path.is_dir():
            skipped_directories += 1
    summary = {"accepted": 0, "duplicates": 0, "rejected": 0, "errors": [], "skipped_directories": skipped_directories}
    current_storage_blocked = None
    with task_lock(root), closing(connect_db(root)) as db, db:
        stored_bytes = 0
        for stored in db.execute("SELECT path FROM submissions WHERE path<>''"):
            stored_path = secure_io.checked_path(root / stored["path"], root)
            stored_bytes += stored_path.stat().st_size
        for index, path in enumerate(sorted(paths), start=1):
            digest = None
            created_path = None
            authenticated_current = False
            db.execute("SAVEPOINT receive_one")
            try:
                if not path.is_file() or path.is_symlink():
                    raise ValueError("提交项不是普通文件")
                if path.stat().st_size > MAX_ENVELOPE_BYTES:
                    raise ValueError("提交包超过 32MB 上限")
                raw = secure_io.read_bytes(path, MAX_ENVELOPE_BYTES)
                digest = sha256_bytes(raw)
                envelope = json.loads(raw)
                invite_id = validate_envelope_header(envelope, task)
                raw = canonical(envelope)
                digest = sha256_bytes(raw)
                existing = db.execute("SELECT * FROM submissions WHERE sha256=?", (digest,)).fetchone()
                if existing is not None and (existing["path"] or existing["status"] != "storage_blocked"):
                    summary["duplicates"] += 1
                    db.execute("RELEASE receive_one")
                    continue
                current = current_submissions(db, invite_id)
                authenticated_current = not current or envelope["revision"] >= current[0]["revision"]
                if not db.execute("SELECT 1 FROM invites WHERE invite_id=?", (invite_id,)).fetchone():
                    if db.execute("SELECT COUNT(*) FROM invites").fetchone()[0] >= MAX_OPEN_INVITES:
                        raise ValueError(f"OPEN_INVITE_LIMIT: 任务最多接收 {MAX_OPEN_INVITES} 个不同回执编号")
                    db.execute(
                        "INSERT INTO invites(invite_id,employee_id,name,token_hash,status,created_at) VALUES(?,?,?,'','invited',?)",
                        (invite_id, invite_id, "", now_iso()),
                    )
                existing_versions = db.execute("SELECT COUNT(*) FROM submissions WHERE invite_id=? AND path<>''", (invite_id,)).fetchone()[0]
                if existing_versions >= MAX_VERSIONS_PER_INVITE:
                    raise ValueError("TASK_STORAGE_LIMIT: 该回执编号的历史数量达到安全上限")
                if stored_bytes + len(raw) > MAX_TASK_SUBMISSION_BYTES:
                    raise ValueError("TASK_STORAGE_LIMIT: 任务密文总量达到安全上限")
                version = existing["version"] if existing is not None else db.execute("SELECT COALESCE(MAX(version),0)+1 FROM submissions WHERE invite_id=?", (invite_id,)).fetchone()[0]
                target_dir = secure_io.checked_path(root / "submissions" / invite_id, root)
                target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                target = target_dir / f"v{version:04d}_{digest[:12]}.yintian"
                if target.exists() and sha256_file(target) != digest:
                    raise ValueError("RECOVERY_CONFLICT: 收件路径已存在不同内容")
                if not target.exists():
                    created_path = target
                atomic_write(target, raw)
                received_at = now_iso()
                if existing is None:
                    db.execute(
                        "INSERT INTO submissions(invite_id,version,revision,sha256,path,received_at,late,status) VALUES(?,?,?,?,?,?,?,?)",
                        (invite_id, version, envelope["revision"], digest, target.relative_to(root).as_posix(), received_at, int(task_late(task, received_at)), "submitted"),
                    )
                    submission_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
                else:
                    submission_id = existing["id"]
                    db.execute("UPDATE submissions SET path=?,status='submitted',conflict_fields='[]' WHERE id=?",
                               (target.relative_to(root).as_posix(), submission_id))
                audit(db, "ingest", submission_id)
                db.execute("RELEASE receive_one")
                summary["accepted"] += 1
                stored_bytes += len(raw)
            except Exception as exc:
                db.execute("ROLLBACK TO receive_one")
                db.execute("RELEASE receive_one")
                if isinstance(exc, sqlite3.Error):
                    # Keep any written ciphertext for recovery; a database failure is not a rejected receipt.
                    raise RuntimeError("DATABASE_WRITE_FAILED: 数据库写入失败，保留原始回执并修复后重试") from None
                if created_path is not None:
                    created_path.unlink(missing_ok=True)
                if authenticated_current and (isinstance(exc, OSError) or str(exc).startswith("TASK_STORAGE_LIMIT:")):
                    # Keep authenticated ordering metadata even if ciphertext storage fails; never revive older values.
                    current_storage_blocked = "INGEST_IO" if isinstance(exc, OSError) else "TASK_STORAGE_LIMIT"
                    received_at = now_iso()
                    db.execute("INSERT OR IGNORE INTO invites(invite_id,employee_id,name,token_hash,status,created_at) VALUES(?,?,'','','invited',?)",
                               (invite_id, invite_id, received_at))
                    version = db.execute("SELECT COALESCE(MAX(version),0)+1 FROM submissions WHERE invite_id=?", (invite_id,)).fetchone()[0]
                    db.execute("INSERT OR IGNORE INTO submissions(invite_id,version,revision,sha256,path,received_at,late,status,conflict_fields) VALUES(?,?,?,?,'',?,?,'storage_blocked',?)",
                               (invite_id, version, envelope["revision"], digest, received_at, int(task_late(task, received_at)), json.dumps([current_storage_blocked])))
                    blocked_id = db.execute("SELECT id FROM submissions WHERE sha256=?", (digest,)).fetchone()[0]
                    audit(db, "ingest_blocked", blocked_id, "storage_blocked", current_storage_blocked)
                    break
                summary["rejected"] += 1
                code = error_report(exc)["error"]
                summary["errors"].append({"file_ref": digest[:12] if digest else f"entry-{index}",
                                          "error": type(exc).__name__,
                                          "code": "INGEST_IO" if isinstance(exc, OSError) else code if code in {"SIGNATURE_INVALID", "REVISION_INVALID", "TASK_STORAGE_LIMIT"} else "SUBMISSION_REJECTED",
                                          "retryable": isinstance(exc, OSError), "next_action": "retry" if isinstance(exc, OSError) else "check_invitation"})
    if current_storage_blocked:
        raise RuntimeError(f"{current_storage_blocked}: 无法保存已验证的新回执，已记录阻断状态，停止汇总以免导出旧值")
    if skipped_directories:
        processed = summary["accepted"] or summary["duplicates"] or summary["rejected"]
        summary["hint"] = (f"收件目录{'顶层无 .yintian 文件，' if not processed else ''}默认不递归子目录；发现 {skipped_directories} 个子目录，"
                           f"{'请' if not processed else '若其中还有回执请'}将回执文件移到顶层后重试，或加 --recursive 递归收件")
    return summary


def cmd_ingest(args) -> dict[str, Any]:
    return ingest_task(args.task_dir, args.submissions_dir, recursive=getattr(args, "recursive", False))


def unlock_private_key(root: Path, task: dict[str, Any]):
    from cryptography.hazmat.primitives import serialization

    data = secure_io.read_bytes(root / "private.pem.enc", 128 * 1024)
    if not valid_private_key_blob(data):
        raise ValueError("私钥文件不是 yintian-key/1 信封，拒绝解锁")
    private_der = decrypt_private_key(json.loads(data), load_local_task_secret(root, task["task_id"]))
    return serialization.load_der_private_key(private_der, password=None)


def decrypt_envelope(root: Path, task: dict[str, Any], path: Path, private_key, expected_invite_id: str) -> dict[str, Any]:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    path = secure_io.checked_path(path, root)
    submissions_root = secure_io.checked_path(root / "submissions", root)
    if submissions_root not in path.parents or not path.is_file():
        raise ValueError("提交路径不在任务 submissions 目录内")
    envelope = load_json(path)
    invite_id = validate_envelope_header(envelope, task)
    if invite_id != expected_invite_id:
        raise ValueError("提交包与数据库记录不匹配")
    encrypted_key = base64.b64decode(envelope["encrypted_key_b64"], validate=True)
    aes_key = private_key.decrypt(encrypted_key, padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    plaintext = AESGCM(aes_key).decrypt(base64.b64decode(envelope["iv_b64"], validate=True), base64.b64decode(envelope["ciphertext_b64"], validate=True), aad_for(envelope))
    payload = json.loads(plaintext)
    if not isinstance(payload, dict):
        raise ValueError("解密载荷必须是对象")
    payload_fields = {"format_version", "task_id", "invite_id", "schema_hash", "revision", "notice_hash", "template_version", "submitted_at", "consent_confirmed", "values", "attachments"}
    if set(payload) - payload_fields:
        raise ValueError("解密载荷包含协议外字段")
    if type(payload.get("revision")) is not int:
        raise ValueError("REVISION_INVALID: 解密载荷更正序号无效")
    for key in ("format_version", "task_id", "invite_id", "schema_hash", "revision"):
        if payload.get(key) != envelope[key]:
            raise ValueError(f"解密载荷 {key} 与外层不一致")
    return payload


def ocr_attachment(item: dict[str, Any]) -> tuple[str, list[str]]:
    try:
        import ocr_engine
    except Exception as exc:
        return "", [f"ocr_unavailable:{type(exc).__name__}"]
    try:
        if item["type"].startswith("image/"):
            result = ocr_engine.scan_bytes(item["data"])
            flags = ["ocr_low_confidence"] if any(block.get("low_confidence") for block in result["blocks"]) else []
            text = result["full_text"]
            if not text.strip():
                flags.append("ocr_no_text")
            return text, flags
        if item["type"] == "application/pdf":
            try:
                import pypdfium2 as pdfium
            except ImportError:
                return "", ["pdf_ocr_unavailable"]
            document = pdfium.PdfDocument(item["data"])
            flags = []
            if len(document) > MAX_PDF_PAGES:
                flags.append("pdf_page_limit")
            texts = []
            for page_index in range(min(len(document), MAX_PDF_PAGES)):
                page = document[page_index]
                width, height = page.get_size()
                rendered_w, rendered_h = width * PDF_RENDER_SCALE, height * PDF_RENDER_SCALE
                if rendered_w * rendered_h > MAX_IMAGE_PIXELS:
                    flags.append("pdf_pixel_limit")
                    continue
                bitmap = page.render(scale=PDF_RENDER_SCALE)
                image = bitmap.to_pil()
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                result = ocr_engine.scan_bytes(buffer.getvalue())
                texts.append(result["full_text"])
                if any(block.get("low_confidence") for block in result["blocks"]):
                    flags.append("ocr_low_confidence")
            text = "\n".join(texts)
            if not text.strip():
                flags.append("ocr_no_text")
            return text, sorted(set(flags))
    except Exception as exc:
        return "", [f"ocr_error:{type(exc).__name__}"]
    return "", []


def compare_ocr(payload, attachments, field_defs):
    """只比对显式 ocr_fields 绑定的附件；识别不可用记 runtime:ocr，识别到多个不同值记 ambiguous，绝不按任一匹配放行。"""
    from ocr_matcher import extract_from_text

    definitions = {f["id"]: f for f in field_defs}
    flags = []
    for item in attachments:
        definition = definitions.get(item["field_id"])
        binding = definition.get("ocr_fields", []) if definition else []
        if not binding:
            continue
        text, warnings = ocr_attachment(item)
        for warning in warnings:
            kind = "runtime:ocr" if any(k in warning for k in ("unavailable", "error")) else "ocr:quality"
            flags.append(f"{kind}:{item['field_id']}")
        candidates = {}
        extract_from_text(text, "attachment", candidates)
        for field_id in binding:
            field = definitions[field_id]
            value = normalize_value(field["type"], payload.get("values", {}).get(field_id, ""))
            if not value:
                continue
            extracted = "name" if field_id == "name" else {"cn_id": "id_number", "address": "address", "phone_cn": "phone"}.get(field["type"])
            if extracted is None:
                flags.append(f"ocr:unsupported:{field_id}")
                continue
            found = candidates.get(extracted, [])
            valid = {normalize_value(field["type"], candidate.value) for candidate in found if candidate.valid is not False}
            if any(candidate.valid is False for candidate in found):
                flags.append(f"ocr:invalid:{field_id}")
            if not valid:
                flags.append(f"ocr:missing:{field_id}")
            elif len(valid) > 1:
                flags.append(f"ocr:ambiguous:{field_id}")
            elif value not in valid:
                flags.append(f"ocr:conflict:{field_id}")
    return sorted(set(flags))


def stored_payload(root, task, row, private_key):
    path = secure_io.checked_path(root / row["path"], root)
    if sha256_file(path) != row["sha256"]:
        raise ValueError("CIPHERTEXT_CHANGED: 已接收的文件发生变化")
    payload = decrypt_envelope(root, task, path, private_key, row["invite_id"])
    if payload["revision"] != row["revision"]:
        raise ValueError("CIPHERTEXT_CHANGED: 回执更正序号与数据库不一致")
    return payload


def review_task(task_dir, retry_needs_review=False, invite_id=None):
    from cryptography.exceptions import InvalidTag
    root, task = load_task(task_dir)
    if task_expired(task):
        raise RuntimeError(expired_message(task, "解密复核"))
    try:
        private_key = unlock_private_key(root, task)
    except Exception as exc:
        raise RuntimeError("KEY_UNLOCK_FAILED: 本地密钥错误或私钥损坏") from exc
    summary = {"verified": 0, "needs_review": 0, "invalid": 0}
    with task_lock(root), closing(connect_db(root)) as db, db:
        states = "('submitted','needs_review')" if retry_needs_review else "('submitted')"
        query = f"SELECT s.* FROM submissions s WHERE s.status IN {states}"
        params = []
        if invite_id:
            query += " AND s.invite_id=?"
            params.append(invite_id)
        # Historical pending receipts may be checked, but only the signed current revision is actionable.
        query += " AND (s.status='submitted' OR s.revision=(SELECT MAX(n.revision) FROM submissions n WHERE n.invite_id=s.invite_id)) ORDER BY s.id"
        for row in db.execute(query, params).fetchall():
            if task_expired(load_task(root)[1]):
                raise RuntimeError("TASK_EXPIRED: 任务已超过保存期限")
            missing, conflicts, attachments, payload = [], [], [], {}
            try:
                payload = stored_payload(root, task, row, private_key)
                missing, conflicts, attachments = validate_payload(task, payload)
                if not missing and not conflicts:
                    with suppress_private_output():
                        conflicts = compare_ocr(payload, attachments, task["fields"])
                state = "needs_review" if missing or conflicts else "verified"
            except (OSError, ImportError):
                state, conflicts = "needs_review", ["runtime:dependency_or_io"]
            except (ValueError, InvalidTag, TypeError, KeyError):
                state, conflicts = "invalid", ["ciphertext_or_payload_invalid"]
            except Exception:
                state, conflicts = "needs_review", ["runtime:review"]
            db.execute(
                "UPDATE submissions SET submitted_at=?,reviewed_at=?,status=?,missing_fields=?,conflict_fields=?,attachment_count=?,consent_confirmed=? WHERE id=?",
                (normalize_submitted_at(payload.get("submitted_at")), now_iso(), state, json.dumps(missing), json.dumps(conflicts), len(attachments), int(payload.get("consent_confirmed") is True), row["id"]))
            audit(db, "review", row["id"], state, ",".join(missing + conflicts))
            summary[state] += 1
    return summary


def cmd_review(args):
    return review_task(args.task_dir, getattr(args, "retry_needs_review", False), getattr(args, "invite", None))


def cmd_decide(args):
    require_tty("decide")
    root, task = load_task(args.task_dir)
    if task_expired(task):
        raise RuntimeError(expired_message(task, "人工裁定"))
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,64}", args.operator):
        raise ValueError("OPERATOR_INVALID: 操作者标识限字母、数字、_.@-")
    private_key = unlock_private_key(root, task)
    with task_lock(root), closing(connect_db(root)) as db:
        heads = current_submissions(db, args.invite_id)
        row = heads[0] if heads else None
        if row is not None and row["status"] == "revision_conflict":
            raise RuntimeError("REVISION_CONFLICT: 同一序号存在不同回执，须由本人生成更高序号")
        if row is not None and row["status"] == "storage_blocked":
            raise RuntimeError("MANUAL_NOT_ALLOWED: 必须先恢复原始回执存储，不能人工绕过")
        if row is None or row['version'] != args.version or row['status'] in {'verified_manual', 'returned'}:
            raise RuntimeError("STATE_CHANGED: 版本不符或已人工结案")
        snapshot = export_snapshot(root)
        issues = json.loads(row['conflict_fields'])
        if args.action == 'confirm':
            payload = stored_payload(root, task, row, private_key)
            missing, hard_conflicts, attachments = validate_payload(task, payload)
            if row['status'] != 'needs_review' or missing or hard_conflicts or not issues or any(not item.startswith('ocr:') for item in issues):
                raise RuntimeError("MANUAL_NOT_ALLOWED: 人工只能裁定 OCR 问题；输入和运行错误不能放行")
    if args.action == 'confirm':
        try:
            from review_evidence import confirm_evidence
        except ImportError as exc:
            raise RuntimeError("MANUAL_REVIEW_UNAVAILABLE: 人工确认模块不可用，无法执行逐项确认") from exc
        if not confirm_evidence(payload['values'], attachments, issues):
            raise RuntimeError("CANCELLED: 未完成逐项确认")
        state, reason = 'verified_manual', 'evidence_checked'
    else:
        if input(f"退回版本 {args.version}，输入任务 ID {task['task_id']}: ").strip() != task['task_id']:
            raise RuntimeError('CANCELLED: 已取消退回')
        state, reason = 'returned', 'correction_requested'
    with task_lock(root), closing(connect_db(root)) as db, db:
        if task_expired(load_task(root)[1]) or snapshot != export_snapshot(root):
            raise RuntimeError('STATE_CHANGED: 任务已变化或到期，请刷新后重试')
        if args.action == 'confirm':
            stored_payload(root, task, row, private_key)
        db.execute('UPDATE submissions SET status=?,reviewed_at=? WHERE id=?', (state, now_iso(), row['id']))
        for code in issues if args.action == 'confirm' else [reason]:
            audit(db, 'manual_' + args.action, row['id'], state, code, args.operator)
    return {'version': args.version, 'revision': row['revision'], 'status': state}


def progress_rows(root: Path) -> list[dict[str, Any]]:
    """每个回执编号一行：当前签名序号的状态与校验问题，不含姓名或字段值。"""
    with closing(connect_db(root)) as db:
        heads = {row["invite_id"]: row for row in current_submissions(db)}
        rows = [heads.get(row["invite_id"], {"invite_id": row["invite_id"], "status": "invited", "version": None,
                "revision": None, "late": False, "missing_fields": "[]", "conflict_fields": "[]", "received_at": None,
                "conflicting_versions": []}) for row in db.execute("SELECT invite_id FROM invites ORDER BY invite_id")]
    return [{"invite_id": row["invite_id"], "status": row["status"], "version": row["version"], "revision": row["revision"],
             "late": bool(row["late"]),
             "missing_fields": json.loads(row["missing_fields"] or "[]"), "conflict_fields": json.loads(row["conflict_fields"] or "[]"),
             "received_at": row["received_at"], "conflicting_versions": row["conflicting_versions"]} for row in rows]


def status_counts(root: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in progress_rows(root):
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return counts


def collect_rows(root: Path, task: dict[str, Any], private_key, attachment_stage: Path | None, attachment_dir_name: str) -> tuple[list[dict[str, str]], int]:
    """只解密每人最新且已通过的提交，重新做硬性校验；附件写入暂存目录并在单元格保存相对路径。"""
    field_defs = {field["id"]: field for field in task["fields"]}
    with closing(connect_db(root)) as db:
        db_rows = [row for row in current_submissions(db) if row["status"] in PASSED_STATES]
    output, attachment_count = [], 0
    for row in db_rows:
        payload = stored_payload(root, task, row, private_key)
        missing, conflicts, attachments = validate_payload(task, payload)
        if missing or conflicts:
            raise ValueError("EXPORT_VALIDATION_FAILED: 已通过记录的业务校验失败，停止导出")
        values = payload["values"]
        record = {
            field_id: normalize_value(field_defs[field_id]["type"], values.get(field_id, ""))
            for field_id in field_defs
            if field_defs[field_id]["type"] not in ATTACHMENT_TYPES
        }
        record["__receipt_id__"] = row["invite_id"]
        by_field: dict[str, list[dict[str, Any]]] = {}
        for item in attachments:
            by_field.setdefault(item["field_id"], []).append(item)
        person_dir = row["invite_id"]
        for field_id, field in field_defs.items():
            if field["type"] not in ATTACHMENT_TYPES:
                continue
            paths = []
            for index, item in enumerate(by_field.get(field_id, []), 1):
                suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "application/pdf": ".pdf"}[item["type"]]
                filename = f"{field_id}-{index}{suffix}"
                if attachment_stage is None:
                    raise RuntimeError("ATTACHMENT_EXPORT_UNAVAILABLE: 附件暂存目录未创建")
                secure_io.atomic_write(attachment_stage / person_dir / filename, item["data"], overwrite=False)
                paths.append(f"{attachment_dir_name}/{person_dir}/{filename}")
                attachment_count += 1
            record[field_id] = "; ".join(paths)
        output.append(record)
    output.sort(key=lambda row: row.get("name", ""))
    return output, attachment_count


def suggested_output_name(out_path: Path) -> str:
    return f"{out_path.stem}-{datetime.now().strftime('%Y%m%d-%H%M%S')}{out_path.suffix}"


def collect_task(task_dir: str | Path, submissions_dir: str | Path, out: str | Path, retry_needs_review: bool = True,
                 recursive: bool = False) -> dict[str, Any]:
    root, task = load_task(task_dir)
    out_path = secure_io.checked_path(out).with_suffix(".xlsx")
    if out_path == root or root in out_path.parents:
        raise ValueError("导出路径不能位于任务目录内")
    attachment_fields = [field for field in task["fields"] if field["type"] in ATTACHMENT_TYPES]
    attachment_target = out_path.with_name(out_path.stem + "-attachments")
    if out_path.exists():
        raise FileExistsError(f"OUTPUT_EXISTS: 导出文件已存在，请选择新文件名（例如 {suggested_output_name(out_path)}）")
    if attachment_fields and attachment_target.exists():
        raise FileExistsError("OUTPUT_EXISTS: 附件导出目录已存在，请选择新文件名")
    print("收件中…", file=sys.stderr)
    ingested = ingest_task(root, submissions_dir, recursive=recursive)
    print(f"收件完成：接受 {ingested['accepted']}，重复 {ingested['duplicates']}，拒绝 {ingested['rejected']}", file=sys.stderr)
    print("解密复核中…", file=sys.stderr)
    reviewed = review_task(root, retry_needs_review=retry_needs_review)
    print(f"复核完成：通过 {reviewed['verified']}，待复核 {reviewed['needs_review']}，无效 {reviewed['invalid']}", file=sys.stderr)
    attachment_stage = None
    written: dict[str, str] = {}
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if attachment_fields:
            attachment_stage = Path(tempfile.mkdtemp(prefix=".yintian-attachments-", dir=out_path.parent))
        with task_lock(root):
            private_key = unlock_private_key(root, task)
            rows, attachment_count = collect_rows(root, task, private_key, attachment_stage, attachment_target.name)
            progress = progress_rows(root)
            exclusions = exclusion_details(root, progress)
            late_count = sum(1 for row in progress if row["late"] and row["status"] in PASSED_STATES)
            name_groups: dict[str, list[str]] = {}
            for record in rows:
                name_groups.setdefault(record.get("name", ""), []).append(record["__receipt_id__"])
            duplicate_groups = [sorted(group) for group in name_groups.values() if len(group) > 1]
            write_excel(task, rows, out_path)
            written["xlsx"] = str(out_path)
            if attachment_stage is not None:
                if attachment_count:
                    attachment_stage.rename(attachment_target)
                    written["attachments_dir"] = str(attachment_target)
                else:
                    shutil.rmtree(attachment_stage)
                attachment_stage = None
            with closing(connect_db(root)) as db, db:
                audit(db, "collect", reason="agent_requested")
    except BaseException:
        if attachment_stage is not None:
            shutil.rmtree(attachment_stage, ignore_errors=True)
        if written.get("xlsx"):
            Path(written["xlsx"]).unlink(missing_ok=True)
        if written.get("attachments_dir"):
            shutil.rmtree(written["attachments_dir"], ignore_errors=True)
        raise
    print("导出完成。", file=sys.stderr)
    days_left = retention_days_left(task)
    result = {"task_id": task["task_id"], "rows": len(rows), "excluded": len(progress) - len(rows), "exclusions": exclusions,
              "late": late_count, "attachments": attachment_count, "ingest": ingested, "review": reviewed,
              "retention_until": task["retention_until"], "retention_days_left": days_left, **written}
    warning = retention_warning(task, days_left)
    if warning:
        result["retention_warning"] = warning
    if duplicate_groups:
        result["duplicate_name_groups"] = duplicate_groups
        result["warning"] = "Excel 中存在同名多行；请 HR 在本地核对列出的回执编号，脚本不会自动合并。"
    return result


def _exclusion_action(row: dict[str, Any], reasons: list[str]) -> str:
    """未通过记录的下一步建议；status/notice 与 collect 的 exclusions 共用。"""
    if row["status"] == "revision_conflict":
        return "同一更正序号存在不同回执；请持有人使用本人原保险柜，以同一回执编号生成更高序号的补正回执"
    if row["status"] == "storage_blocked":
        return "已验证回执未能保存；请检查容量或写入权限，再将同一原始回执放回收件目录重试，旧版本不会导出"
    if row["status"] == "invalid":
        return "回执损坏、被改动或不属于本任务，请员工用同一请求包重新生成并发送"
    if any(reason.startswith("runtime:") for reason in reasons):
        return "收件机器缺少依赖或读取失败；先运行 doctor 定位缺失组件，按需安装后重跑 collect 即会重审"
    if any(reason.startswith("ocr:") for reason in reasons):
        return "附件与填写值自动比对未通过；需 HR 本人在终端运行 decide 逐项核对原件，或让员工在本人原保险柜更正，以同一回执编号生成更高序号重交"
    if row["missing_fields"]:
        return "必填项缺失；请员工在本人原保险柜补齐，以同一回执编号生成更高序号重交"
    if row["status"] in {"submitted", "needs_review"}:
        return "校验未通过；请员工在本人原保险柜按提示更正，以同一回执编号生成更高序号重交"
    if row["status"] == "returned":
        return "已退回等待员工重交"
    return "尚未收到有效回执"


def exclusion_details(root: Path, progress: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把未进入 Excel 的记录整理成逐人原因、下一步与可直接运行的 decide 命令，供 Agent 直接向 HR 概述。"""
    details = []
    for row in progress:
        if row["status"] in PASSED_STATES:
            continue
        reasons = list(row["missing_fields"]) + list(row["conflict_fields"])
        detail = {"invite_id": row["invite_id"],
                  "version": row["version"], "revision": row["revision"], "status": row["status"],
                  "late": row["late"], "reasons": reasons, "next_action": _exclusion_action(row, reasons)}
        if row.get("conflicting_versions"):
            detail["conflicting_versions"] = row["conflicting_versions"]
        if row["version"] is not None and any(reason.startswith("ocr:") for reason in reasons):
            detail["decide_command"] = (f'decide "{root}" {row["invite_id"]} --version {row["version"]} '
                                        "--action confirm --operator <HR标识>（需 HR 本人交互终端）")
        details.append(detail)
    return details


def cmd_collect(args) -> dict[str, Any]:
    return collect_task(args.task_dir, args.submissions_dir, args.out,
                        retry_needs_review=not getattr(args, "no_retry_needs_review", False),
                        recursive=getattr(args, "recursive", False))


def retention_days_left(task: dict[str, Any]) -> int:
    return (parse_time(task["retention_until"]) - datetime.now(timezone.utc)).days


def retention_warning(task: dict[str, Any], days_left: int) -> str | None:
    """保存期限临期/到期提示；status 与 collect 共用。"""
    if task_expired(task):
        return "已超过保存期限，按告知承诺不再解密任何回执；只能新建请求包重收"
    if days_left <= 7:
        return f"距保存期限 {task['retention_until']} 仅剩 {days_left} 天，请尽快完成收件、汇总与人工裁定"
    return None


def cmd_status(args) -> dict[str, Any]:
    """只读进度：逐人回执编号、版本、状态与保存期限剩余天数；不解密、不写盘。"""
    root, task = load_task(args.task_dir)
    days_left = retention_days_left(task)
    result = {"task_id": task["task_id"], "deadline": task["deadline"],
              "retention_until": task["retention_until"], "retention_days_left": days_left,
              "expired": task_expired(task), "counts": status_counts(root), "rows": progress_rows(root)}
    warning = retention_warning(task, days_left)
    if warning:
        result["retention_warning"] = warning
    return result


def cmd_list_tasks(args) -> dict[str, Any]:
    """列出目录下全部 YT- 任务：找回 task_dir 的入口，逐任务状态计数。"""
    parent = secure_io.checked_path(args.tasks_dir)
    if not parent.is_dir():
        raise NotADirectoryError(parent)
    tasks = []
    for child in sorted(parent.iterdir()):
        if not child.is_dir() or not child.name.startswith("YT-"):
            continue
        try:
            root, task = load_task(child)
            entry = {"task_id": task["task_id"], "task_dir": str(root),
                     "deadline": task["deadline"], "retention_until": task["retention_until"],
                     "expired": task_expired(task)}
            try:
                entry["status_counts"] = status_counts(root)
            except Exception:
                entry["status_counts"] = {}
        except Exception as exc:
            report = error_report(exc)
            entry = {"task_dir": str(child), "error": report["error"], "message": report["message"]}
        tasks.append(entry)
    return {"tasks_dir": str(parent), "tasks": tasks}


def cmd_audit_log(args) -> dict[str, Any]:
    """只读审计日志：ingest/review/collect/decide/purge 的逐条记录，submission 关联回执编号。"""
    root, task = load_task(args.task_dir)
    with closing(connect_db(root)) as db:
        rows = db.execute("""
            SELECT a.action,a.at,a.result,a.reason,a.operator,
                   s.invite_id,s.version,s.revision
            FROM audit a LEFT JOIN submissions s ON s.id=a.submission_id
            ORDER BY a.id
        """).fetchall()
    entries = [{"action": row["action"], "at": row["at"], "result": row["result"],
                "reason": row["reason"], "operator_recorded": bool(row["operator"]),
                "invite_id": row["invite_id"], "version": row["version"], "revision": row["revision"]} for row in rows]
    return {"task_id": task["task_id"], "entries": entries}


def cmd_notice(args) -> dict[str, Any]:
    """为指定回执编号生成退回/补正通知文件，由 HR 转发给员工；通知是不可信提示数据，不含明文值。"""
    root, task = load_task(args.task_dir)
    invite_id = args.invite_id
    if not OPEN_INVITE_ID_RE.fullmatch(invite_id):
        raise ValueError("回执编号格式无效")
    row = next((item for item in progress_rows(root) if item["invite_id"] == invite_id), None)
    if row is None:
        raise ValueError(f"任务中不存在回执编号: {invite_id}")
    reasons = list(row["missing_fields"]) + list(row["conflict_fields"])
    if row["status"] in PASSED_STATES:
        next_action = "回执已通过校验；如需退回请先在终端运行 decide --action return，再重新生成通知"
    else:
        next_action = _exclusion_action(row, reasons)
    notice = {"format": NOTICE_FORMAT_VERSION, "task_id": task["task_id"], "invite_id": invite_id,
              "revision": row["revision"],
              "status": row["status"], "late": row["late"], "reasons": reasons,
              "next_action": next_action, "contact": task["contact"],
              "generated_at": now_iso()}
    out = secure_io.checked_path(args.out)
    secure_io.atomic_write(out, canonical(notice), overwrite=False)
    return {"out": str(out), "task_id": task["task_id"], "invite_id": invite_id, "revision": row["revision"], "status": row["status"]}


def write_excel(task: dict[str, Any], rows: list[dict[str, str]], out_path: Path) -> None:
    """明文行落盘为 0600 XLSX：表头用中文标签，所有单元格按字符串写入以防公式注入。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    columns = ["name", "__receipt_id__"] + [field["id"] for field in task["fields"] if field["id"] != "name"]
    labels = {field["id"]: field["label"] for field in task["fields"]}
    labels["__receipt_id__"] = "回执编号"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "收集结果"
    sheet.append([labels[key] for key in columns])
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.data_type = "s"
    for row_index, record in enumerate(rows, start=2):
        for column_index, key in enumerate(columns, start=1):
            cell = sheet.cell(row_index, column_index, str(record.get(key, "")))
            cell.data_type = "s"
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    buffer = io.BytesIO()
    workbook.save(buffer)
    secure_io.atomic_write(out_path, buffer.getvalue(), overwrite=False)


def export_snapshot(root):
    with closing(connect_db(root)) as db:
        rows = [tuple(row) for row in db.execute("SELECT id,invite_id,version,revision,sha256,status FROM submissions ORDER BY id")]
    return sha256_bytes(canonical([load_task(root)[1], rows]))


PACKAGE_REQUIRED_FILES = {"task.json", "public.pem", "private.pem.enc", "state.sqlite3", "REQUEST.yintian-request"}


def build_task_package(task_dir) -> tuple[bytes, dict[str, Any]]:
    root, task = load_task(task_dir)
    if task_expired(task):
        raise RuntimeError(expired_message(task, "导出交接包"))
    with task_lock(root):
        for path in root.rglob("*"):
            secure_io.checked_path(path, root)
        request_files = sorted(root.glob("REQUEST*.yintian-request"))
        candidates = [root / name for name in sorted(PACKAGE_REQUIRED_FILES) if name != "REQUEST.yintian-request"]
        if any(not path.is_file() for path in candidates) or len(request_files) != 1:
            raise ValueError("任务目录缺少必要文件")
        candidates += request_files
        candidates += list((root / "submissions").glob("*/*.yintian"))
        if len(candidates) + 1 > MAX_PACKAGE_FILES:
            raise ValueError("任务内容超过交接包安全上限")
        data, total = {}, 0
        for path in sorted(candidates):
            if path.name == "state.sqlite3":
                with closing(connect_db(root)) as source, closing(sqlite3.connect(":memory:")) as snapshot:
                    source.backup(snapshot)
                    raw = snapshot.serialize()
            else:
                raw = secure_io.read_bytes(path, MAX_PACKAGE_MEMBER_BYTES)
            total += len(raw)
            if len(raw) > MAX_PACKAGE_MEMBER_BYTES or total > MAX_PACKAGE_BYTES:
                raise ValueError("任务内容超过交接包安全上限")
            member = "REQUEST.yintian-request" if path in request_files else path.relative_to(root).as_posix()
            data[member] = raw
        raw = load_local_task_secret(root, task["task_id"]).encode("ascii")
        total += len(raw)
        if total > MAX_PACKAGE_BYTES:
            raise ValueError("任务内容超过交接包安全上限")
        data["local-open-key"] = raw
        metadata = {"package_version": TASK_PACKAGE_VERSION, "task_id": task["task_id"], "created_at": now_iso(),
                    "manifest": {name: sha256_bytes(raw) for name, raw in data.items()}}
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("package.json", canonical(metadata))
            for name, raw in data.items():
                archive.writestr(task["task_id"] + "/" + name, raw)
        return buffer.getvalue(), {"task_id": task["task_id"], "files": len(data)}


def task_package_aad(task_id: str) -> bytes:
    return f"{ENCRYPTED_TASK_PACKAGE_VERSION}:{task_id}".encode("utf-8")


def export_task(task_dir: str | Path, out: str | Path, handoff_password: str | None = None) -> dict[str, Any]:
    root, task = load_task(task_dir)
    zip_bytes, info = build_task_package(root)
    password = handoff_password if handoff_password is not None else generate_password()
    envelope = aes_gcm_seal(ENCRYPTED_TASK_PACKAGE_VERSION, zip_bytes, password, aad=task_package_aad(task["task_id"]))
    envelope["task_id"] = task["task_id"]
    out_path = secure_io.checked_path(out)
    if out_path == root or root in out_path.parents:
        raise ValueError("导出路径不能位于任务目录内")
    secure_io.atomic_write(out_path, canonical(envelope), overwrite=False)
    return {**info, "out": str(out_path), "handoff_password": password}


def cmd_export(args) -> dict[str, Any]:
    require_tty("export-task")
    stream = open_control_terminal()
    if stream is None:
        raise RuntimeError("SECRET_TTY_REQUIRED: 无法打开安全控制终端")
    with stream:
        result = export_task(args.task_dir, args.out)
        password = result.pop("handoff_password")
        try:
            print_once_secret("交接密码（仅显示一次，丢失不可恢复）：", password,
                              "请通过另一独立安全渠道告知接收方；不要与交接包同渠道发送，也不要写入聊天。", stream=stream)
        except OSError:
            Path(result["out"]).unlink(missing_ok=True)
            raise RuntimeError("SECRET_TTY_REQUIRED: 密码显示失败，已撤回交接包") from None
    return result


def read_package_member(archive: zipfile.ZipFile, name: str, remaining: int) -> bytes:
    """流式读取任务包成员并按实际字节计数，超过单项或累计上限立即失败。"""
    data = bytearray()
    with archive.open(name) as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                return bytes(data)
            data += chunk
            if len(data) > MAX_PACKAGE_MEMBER_BYTES or len(data) > remaining:
                raise ValueError("任务包解压后超过安全上限")


def open_task_package(package_path: Path, handoff_password: str | None = None) -> zipfile.ZipFile:
    """解密 yintian-task/3 交接信封并返回其中的 ZIP；先按 stat 尺寸拒绝超限文件，再解密认证。"""
    if package_path.stat().st_size > MAX_PACKAGE_ENVELOPE_BYTES:
        raise ValueError("交接包信封超过 200MB 安全上限，拒绝读取")
    try:
        envelope = json.loads(package_path.read_bytes())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("交接包信封无效") from exc
    if not isinstance(envelope, dict) or envelope.get("format") != ENCRYPTED_TASK_PACKAGE_VERSION:
        raise ValueError("交接包格式不受支持")
    task_id = envelope.get("task_id")
    if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
        raise ValueError("交接包 task_id 无效")
    password = handoff_password
    if password is None:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                password = getpass.getpass("交接密码: ")
        except getpass.GetPassWarning:
            raise RuntimeError("SECRET_TTY_REQUIRED: 无法安全读取交接密码") from None
    try:
        zip_bytes = aes_gcm_open(envelope, password, task_package_aad(task_id))
    except Exception as exc:
        raise ValueError("交接密码错误或交接包已被篡改，拒绝导入") from exc
    return zipfile.ZipFile(io.BytesIO(zip_bytes))


def import_task(package: str | Path, out_parent: str | Path, handoff_password: str | None = None) -> dict[str, Any]:
    package_path, parent = secure_io.checked_path(package), secure_io.checked_path(out_parent)
    if package_path.stat().st_size > MAX_PACKAGE_BYTES:
        raise ValueError("任务包超过安全上限")
    with open_task_package(package_path, handoff_password) as archive:
        if "package.json" not in archive.namelist():
            raise ValueError("任务包缺少清单")
        package_info = archive.getinfo("package.json")
        if package_info.file_size > 1024 * 1024:
            raise ValueError("任务包清单过大")
        metadata = json.loads(read_package_member(archive, "package.json", 1024 * 1024))
        task_id = metadata.get("task_id")
        if metadata.get("package_version") != TASK_PACKAGE_VERSION or not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
            raise ValueError("任务包格式无效")
        raw_manifest = metadata.get("manifest")
        if not isinstance(raw_manifest, dict) or len(raw_manifest) > MAX_PACKAGE_FILES:
            raise ValueError("任务包清单无效或文件过多")
        manifest: dict[str, str] = {}
        for rel, expected_hash in raw_manifest.items():
            if not isinstance(rel, str) or not rel or not isinstance(expected_hash, str) or ":" in rel or "\\" in rel:
                raise ValueError("任务包包含不安全路径")
            rel_path = PurePosixPath(rel)
            if rel_path.is_absolute() or ".." in rel_path.parts:
                raise ValueError("任务包包含不安全路径")
            parts = rel_path.parts
            allowed = (
                rel in PACKAGE_REQUIRED_FILES or rel == "local-open-key"
                or (len(parts) == 3 and parts[0] == "submissions" and OPEN_INVITE_ID_RE.fullmatch(parts[1]) and re.fullmatch(r"v\d{4,}_[0-9a-f]{12}\.yintian", parts[2]))
            )
            if not allowed or rel in manifest:
                raise ValueError("任务包包含不允许或重复的文件")
            manifest[rel] = expected_hash
        if not (PACKAGE_REQUIRED_FILES | {"local-open-key"}).issubset(manifest):
            raise ValueError("任务包缺少必要文件")
        target = parent / task_id
        if target.exists():
            raise FileExistsError(f"目标任务已存在: {target}")
        expected_prefix = task_id + "/"
        names = set()
        for info in archive.infolist():
            if "\\" in info.filename or info.filename in names:
                raise ValueError("任务包包含不安全或重复的条目")
            names.add(info.filename)
        expected_names = {expected_prefix + rel for rel in manifest}
        if names != expected_names | {"package.json"}:
            raise ValueError("任务包文件不完整或包含额外条目")
        infos = [archive.getinfo(name) for name in expected_names]
        if any(info.file_size > MAX_PACKAGE_MEMBER_BYTES for info in infos) or sum(info.file_size for info in infos) > MAX_PACKAGE_BYTES:
            raise ValueError("任务包解压后超过安全上限")
        target.mkdir(parents=True, mode=0o700)
        imported_secret_path = None
        try:
            total_size = 0
            open_secret = b""
            for rel, expected_hash in manifest.items():
                destination = (target.joinpath(*PurePosixPath(rel).parts)).resolve()
                if target != destination and target not in destination.parents:
                    raise ValueError("任务包包含越界路径")
                data = read_package_member(archive, expected_prefix + rel, MAX_PACKAGE_BYTES - total_size)
                total_size += len(data)
                if sha256_bytes(data) != expected_hash:
                    raise ValueError(f"任务包哈希不匹配: {rel}")
                if rel == "local-open-key":
                    open_secret = data
                else:
                    atomic_write(destination, data)
            _, imported_task = load_task(target)
            if imported_task["task_id"] != task_id:
                raise ValueError("任务包内外 task_id 不一致")
            canonical_request = target / "REQUEST.yintian-request"
            if canonical_request.is_file():
                canonical_request.rename(target / f"REQUEST-{task_id}.yintian-request")
            try:
                secret = open_secret.decode("ascii")
            except UnicodeDecodeError as exc:
                raise ValueError("OPEN_KEY_PACKAGE_INVALID: 本地密钥格式无效") from exc
            if not re.fullmatch(r"[A-Za-z0-9_.~-]{28,128}", secret):
                raise ValueError("OPEN_KEY_PACKAGE_INVALID: 本地密钥格式无效")
            imported_secret_path = save_local_task_secret(target, task_id, secret)
            from cryptography.hazmat.primitives import serialization

            public_key = serialization.load_pem_public_key((target / "public.pem").read_bytes())
            public_der = public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
            if hashlib.sha256(public_der).hexdigest()[:24] != imported_task["key_id"]:
                raise ValueError("任务包密钥材料无效")
            private_der = unlock_private_key(target, imported_task).public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
            if not secrets.compare_digest(private_der, public_der):
                raise ValueError("任务包公私钥不匹配")
            with task_lock(target):
                pass
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            if imported_secret_path is not None:
                imported_secret_path.unlink(missing_ok=True)
            raise
    if os.name != "nt":
        for directory in [target, *(path for path in target.rglob("*") if path.is_dir())]:
            os.chmod(directory, 0o700)
    return {"task_id": task_id, "task_dir": str(target)}


def cmd_import(args) -> dict[str, Any]:
    require_tty("import-task")
    return import_task(args.package, args.out)


def cmd_purge(args) -> dict[str, Any]:
    require_tty("purge")
    root, task = load_task(args.task_dir)
    if not task_expired(task) and not args.allow_early:
        raise RuntimeError("任务尚未到保存期限；如确需提前删除，请增加 --allow-early")
    typed = input(f"输入任务 ID {task['task_id']} 以确认删除任务目录: ").strip()
    if typed != task["task_id"]:
        raise RuntimeError("任务 ID 不匹配，已取消")
    parent = root.parent
    local_secret = local_task_secret_path(root, task["task_id"])
    with task_lock(root):
        counts = status_counts(root)
        summary = {"task_id": task["task_id"], "purged_at": now_iso(), "aggregate_counts": counts, "total": sum(counts.values())}
        if os.name == 'nt':
            # Windows cannot delete an open CRT lock file. Remove task data under
            # the lock; after release only the inert lock/empty directory remain.
            for path in root.rglob('*'):
                secure_io.checked_path(path, root)
            for path in root.iterdir():
                if path.name != '.write.lock':
                    shutil.rmtree(path) if path.is_dir() else path.unlink()
        else:
            shutil.rmtree(root)
    if os.name == 'nt':
        if {path.name for path in root.iterdir()} - {'.write.lock'}:
            raise RuntimeError('STATE_CHANGED: 删除结束时目录出现新文件，停止清理')
        (root / '.write.lock').unlink(missing_ok=True)
        root.rmdir()
    summary_path = parent / f"{task['task_id']}.purged.json"
    dump_json(summary_path, summary)
    local_secret.unlink(missing_ok=True)
    return {"summary": str(summary_path)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SafeFill：端到端加密的私密信息收集管理")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("create-request", help="生成供 safefill-fill Agent 读取的机器请求包")
    p.add_argument("--config", required=True); p.add_argument("--out", required=True); p.set_defaults(func=cmd_create_request)
    p = sub.add_parser("collect", help="一键收件、校验、解密并导出 Excel 与附件目录")
    p.add_argument("task_dir"); p.add_argument("submissions_dir"); p.add_argument("--out", required=True)
    p.add_argument("--no-retry-needs-review", action="store_true", help="默认重审历史 needs_review 回执；此旗标关闭重审")
    p.add_argument("--recursive", action="store_true", help="递归收件目录下的子目录"); p.set_defaults(func=cmd_collect)
    p = sub.add_parser("ingest", help="只接收 .yintian 密文提交，不解密")
    p.add_argument("task_dir"); p.add_argument("submissions_dir")
    p.add_argument("--recursive", action="store_true", help="递归收件目录下的子目录"); p.set_defaults(func=cmd_ingest)
    p = sub.add_parser("status", help="只读查看进度：逐人回执编号、版本、状态与保存期限剩余天数（不解密）")
    p.add_argument("task_dir"); p.set_defaults(func=cmd_status)
    p = sub.add_parser("list-tasks", help="列出目录下全部 YT- 任务及状态计数，用于找回 task_dir")
    p.add_argument("tasks_dir"); p.set_defaults(func=cmd_list_tasks)
    p = sub.add_parser("notice", help="为指定回执编号生成 yintian-notice/1 退回/补正通知文件，转发给员工")
    p.add_argument("task_dir"); p.add_argument("invite_id"); p.add_argument("--out", required=True); p.set_defaults(func=cmd_notice)
    p = sub.add_parser("audit-log", help="只读审计日志：收件/复核/汇总/裁定/销毁的逐条记录（不解密内容）")
    p.add_argument("task_dir"); p.set_defaults(func=cmd_audit_log)
    p = sub.add_parser("review", help="只解密校验并做 OCR 复核，不导出")
    p.add_argument("task_dir"); p.add_argument("--retry-needs-review", action="store_true"); p.add_argument("--invite"); p.set_defaults(func=cmd_review)
    p = sub.add_parser("decide", help="HR 本人终端逐项核对 OCR 证据（Tk 窗口）或退回重填")
    p.add_argument("task_dir"); p.add_argument("invite_id"); p.add_argument("--version", required=True, type=int)
    p.add_argument("--action", choices=["confirm", "return"], required=True); p.add_argument("--operator", required=True); p.set_defaults(func=cmd_decide)
    p = sub.add_parser("doctor", help="检查 Python、核心依赖、OCR 与 Tk，不读取私密材料")
    p.set_defaults(func=cmd_doctor)
    p = sub.add_parser("export-task", help="导出加密的任务交接包（一次性交接密码仅显示一次）")
    p.add_argument("task_dir"); p.add_argument("--out", required=True); p.set_defaults(func=cmd_export)
    p = sub.add_parser("import-task", help="导入加密任务交接包（交互输入交接密码）")
    p.add_argument("package"); p.add_argument("--out", required=True); p.set_defaults(func=cmd_import)
    p = sub.add_parser("purge", help="删除任务目录；不保证擦除备份或磁盘残留")
    p.add_argument("task_dir"); p.add_argument("--allow-early", action="store_true"); p.set_defaults(func=cmd_purge)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = args.func(args)
        if result is not None:
            print(json.dumps({"ok": True, **result} if isinstance(result, dict) else result, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(json.dumps(error_report(exc), ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
