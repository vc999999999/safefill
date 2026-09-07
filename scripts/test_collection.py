"""端到端收集工作流回归检查（无测试框架）。"""
from __future__ import annotations

import base64
import builtins
import csv
import io
import json
import os
import stat
import struct
import sys
import tempfile
import types
import warnings
import zipfile
from pathlib import Path

import collection


class TtyCapture(io.StringIO):
    def isatty(self) -> bool:
        return True

PASSWORD = "CorrectHorseBatteryStaple-2026"
VALID_ID = "110105198503071230"


def invite_token(task_dir: Path, invite_id: str) -> str:
    html = (task_dir / "invites" / f"{invite_id}.html").read_text(encoding="utf-8")
    marker = '<script id="cfg" type="application/json">'
    return json.loads(html.split(marker, 1)[1].split("</script>", 1)[0])["invite_token"]


def make_envelope(task_dir: Path, invite_id: str, values: dict, attachments: dict | None = None, overrides: dict | None = None) -> dict:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    task = collection.load_json(task_dir / "task.json")
    payload = {
        "format_version": task["format_version"],
        "task_id": task["task_id"],
        "invite_id": invite_id,
        "invite_token": invite_token(task_dir, invite_id),
        "schema_hash": task["schema_hash"],
        "notice_hash": task["notice_hash"],
        "template_version": task["template_version"],
        "submitted_at": collection.now_iso(),
        "consent_confirmed": True,
        "values": values,
        "attachments": attachments or {"id_front": [], "id_back": []},
    }
    if overrides:
        payload.update(overrides)
    aes = AESGCM.generate_key(bit_length=256)
    iv = bytes(range(12))
    envelope = {
        "format_version": task["format_version"], "task_id": task["task_id"], "invite_id": invite_id,
        "schema_hash": task["schema_hash"], "key_id": task["key_id"],
        "algorithms": {"content": "AES-256-GCM", "key_wrap": "RSA-OAEP-3072-SHA256"},
    }
    public = serialization.load_pem_public_key((task_dir / "public.pem").read_bytes())
    wrapped = public.encrypt(aes, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    ciphertext = AESGCM(aes).encrypt(iv, json.dumps(payload, ensure_ascii=False).encode(), collection.aad_for(envelope))
    envelope.update(encrypted_key_b64=base64.b64encode(wrapped).decode(), iv_b64=base64.b64encode(iv).decode(), ciphertext_b64=base64.b64encode(ciphertext).decode())
    return envelope


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        try:
            collection.validate_config(collection.default_config())
        except ValueError:
            pass
        else:
            raise AssertionError("占位配置不应创建正式任务")
        config = collection.default_config()
        config["purpose"] = "为依法办理员工商业保险收集必要身份资料"
        config["contact"] = "人事部王老师，内线 8001"
        config["correction"] = "在截止日前联系人事部王老师撤回原提交并重新提交"
        config["deadline"] = "2098-12-31T23:59:59Z"
        config["retention_until"] = "2099-12-31T23:59:59Z"
        config["fields"].append({"id": "effective_date", "label": "生效日期", "type": "date", "required": False})
        config_path = tmp / "config.json"
        collection.dump_json(config_path, config)
        roster = tmp / "roster.csv"
        with roster.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream); writer.writerow(["employee_id", "name"]); writer.writerow(["=1+1", "张三"])
        created = collection.create_task(roster, config_path, tmp / "tasks", PASSWORD, require_terminal=False)
        task_dir = Path(created["task_dir"])
        assert len(list((task_dir / "invites").glob("*.html"))) == 1
        invite_id = next(csv.DictReader((task_dir / "invite-index.csv").open(encoding="utf-8-sig")))["invite_id"]
        values = {"name": "张三", "phone": "13800138000", "id_number": VALID_ID, "address": "北京市朝阳区", "effective_date": "2024-02-30"}
        incoming = tmp / "incoming"; incoming.mkdir()
        collection.dump_json(incoming / "one.yintian", make_envelope(task_dir, invite_id, values))
        first = collection.ingest_task(task_dir, incoming)
        assert first["accepted"] == 1
        assert collection.ingest_task(task_dir, incoming)["duplicates"] == 1
        reviewed = collection.review_task(task_dir, PASSWORD)
        assert reviewed["needs_review"] == 1  # 必填证件附件缺失
        rows = collection.report_rows(task_dir)
        assert rows[0]["status"] == "needs_review" and "id_front" in rows[0]["missing_fields"]
        assert "effective_date" in rows[0]["conflict_fields"]
        reports = collection.write_reports(task_dir, ["json", "xlsx"])
        report_text = Path(reports["json"]).read_text(encoding="utf-8")
        assert VALID_ID not in report_text and "13800138000" not in report_text and "北京市朝阳区" not in report_text
        from openpyxl import load_workbook

        sheet = load_workbook(reports["xlsx"], data_only=False).active
        assert sheet["A2"].data_type == "s" and sheet["A2"].value == "=1+1"

        unsafe_time = '=HYPERLINK("https://example.invalid")'
        collection.dump_json(incoming / "unsafe-time.yintian", make_envelope(task_dir, invite_id, values, overrides={"submitted_at": unsafe_time}))
        assert collection.ingest_task(task_dir, incoming)["accepted"] == 1
        assert collection.review_task(task_dir, PASSWORD)["needs_review"] == 1
        reports = collection.write_reports(task_dir, ["json", "xlsx"])
        assert unsafe_time not in Path(reports["json"]).read_text(encoding="utf-8")
        assert load_workbook(reports["xlsx"], data_only=False).active["E2"].value is None

        collection.dump_json(incoming / "wrong-token.yintian", make_envelope(task_dir, invite_id, values, overrides={"invite_token": "wrong"}))
        assert collection.ingest_task(task_dir, incoming)["accepted"] == 1
        assert collection.review_task(task_dir, PASSWORD)["invalid"] == 1

        fake_image = b"not-a-jpeg"
        bad_attachments = {
            "id_front": [{"name": "front.jpg", "type": "image/jpeg", "size": len(fake_image), "sha256": collection.sha256_bytes(fake_image), "data_b64": base64.b64encode(fake_image).decode()}],
            "id_back": [],
        }
        collection.dump_json(incoming / "fake-image.yintian", make_envelope(task_dir, invite_id, values, attachments=bad_attachments))
        assert collection.ingest_task(task_dir, incoming)["accepted"] == 1
        assert collection.review_task(task_dir, PASSWORD)["invalid"] == 1

        original_ocr = collection.ocr_attachment
        collection.ocr_attachment = lambda item: ("", ["ocr_no_text"])
        try:
            task_fields = collection.load_json(task_dir / "task.json")["fields"]
            assert set(collection.compare_ocr({"values": values}, [{"field_id": "id_front"}], task_fields)) == {"ocr_no_identity_fields", "ocr_no_text"}
            assert collection.compare_ocr({"values": values}, [{"field_id": "profile_photo"}], task_fields) == []
            custom_fields = [
                {"id": "name", "label": "姓名", "type": "text"},
                {"id": "mobile", "label": "手机号", "type": "phone_cn"},
                {"id": "credential", "label": "证件号", "type": "cn_id"},
                {"id": "residence", "label": "住址", "type": "address"},
                {"id": "card_front", "label": "证件正面", "type": "image_attachment"},
                {"id": "card_back", "label": "证件反面", "type": "image_attachment"},
            ]
            collection.ocr_attachment = lambda item: (f"姓名 张三\n住址 北京市海淀区\n公民身份号码 {VALID_ID}\n联系电话 13900139000\n", [])
            custom_values = {"name": "张三", "mobile": "13800138000", "credential": VALID_ID, "residence": "北京市朝阳区"}
            assert set(collection.compare_ocr({"values": custom_values}, [{"field_id": "card_front"}], custom_fields)) == {"mobile", "residence"}
        finally:
            collection.ocr_attachment = original_ocr

        tampered = make_envelope(task_dir, invite_id, values)
        raw = bytearray(base64.b64decode(tampered["ciphertext_b64"])); raw[-1] ^= 1
        tampered["ciphertext_b64"] = base64.b64encode(raw).decode()
        collection.dump_json(incoming / "two.yintian", tampered)
        assert collection.ingest_task(task_dir, incoming)["accepted"] == 1
        assert collection.review_task(task_dir, PASSWORD)["invalid"] == 1
        assert collection.report_rows(task_dir)[0]["submission_version"] == 2  # 无效新版本不覆盖旧有效版本

        (incoming / "bad.yintian").mkdir()
        (incoming / "13800138000.yintian").write_text("{", encoding="utf-8")
        with (incoming / "large.yintian").open("wb") as stream:
            stream.truncate(collection.MAX_ENVELOPE_BYTES + 1)
        original_hash = collection.sha256_file

        def failing_hash(path: Path) -> str:
            if path.name == "13800138000.yintian":
                raise OSError(f"cannot read {path}")
            return original_hash(path)

        collection.sha256_file = failing_hash
        try:
            rejected = collection.ingest_task(task_dir, incoming)
        finally:
            collection.sha256_file = original_hash
        assert rejected["rejected"] == 3 and "13800138000" not in json.dumps(rejected)

        package = tmp / "task.yintian-task"
        if os.name != "nt":
            outside_link = task_dir / "outside-link"
            outside_link.symlink_to(tmp / "roster.csv")
            try:
                collection.export_task(task_dir, package)
            except ValueError:
                pass
            else:
                raise AssertionError("任务目录内的符号链接必须阻止导出")
            outside_link.unlink()
        private_before = (task_dir / "private.pem.enc").read_bytes()
        try:
            collection.export_task(task_dir, task_dir / "private.pem.enc")
        except ValueError:
            pass
        else:
            raise AssertionError("导出不应覆盖任务自身文件")
        assert (task_dir / "private.pem.enc").read_bytes() == private_before
        old_file_limit = collection.MAX_PACKAGE_FILES
        collection.MAX_PACKAGE_FILES = 0
        try:
            try:
                collection.export_task(task_dir, tmp / "over-limit.yintian-task")
            except ValueError:
                pass
            else:
                raise AssertionError("导出必须遵守导入端文件数上限")
        finally:
            collection.MAX_PACKAGE_FILES = old_file_limit
        collection.export_task(task_dir, package)
        with zipfile.ZipFile(package) as archive:
            assert not any(name.endswith("outside-link") for name in archive.namelist())
        imported = collection.import_task(package, tmp / "imported")
        assert collection.status_task(imported["task_dir"])["total"] == 1
        assert collection.status_task(imported["task_dir"])["task_status"] == "active"

        task_id = created["task_id"]
        legacy_windows = tmp / "legacy-windows.yintian-task"
        with zipfile.ZipFile(package) as source, zipfile.ZipFile(legacy_windows, "w") as archive:
            metadata = json.loads(source.read("package.json"))
            metadata["package_version"] = collection.LEGACY_TASK_PACKAGE_VERSION
            metadata["manifest"] = {rel.replace("/", "\\"): digest for rel, digest in metadata["manifest"].items()}
            archive.writestr("package.json", json.dumps(metadata))
            for raw_rel in metadata["manifest"]:
                rel = raw_rel.replace("\\", "/")
                archive.writestr(f"{task_id}\\{raw_rel}", source.read(f"{task_id}/{rel}"))
        legacy_imported = collection.import_task(legacy_windows, tmp / "legacy-windows-import")
        assert collection.status_task(legacy_imported["task_dir"])["total"] == 1

        incomplete = tmp / "incomplete.yintian-task"
        task_bytes = (task_dir / "task.json").read_bytes()
        incomplete_metadata = {"package_version": collection.TASK_PACKAGE_VERSION, "task_id": task_id, "manifest": {"task.json": collection.sha256_bytes(task_bytes)}}
        with zipfile.ZipFile(incomplete, "w") as archive:
            archive.writestr("package.json", json.dumps(incomplete_metadata))
            archive.writestr(f"{task_id}/task.json", task_bytes)
        incomplete_parent = tmp / "incomplete-import"
        try:
            collection.import_task(incomplete, incomplete_parent)
        except ValueError:
            pass
        else:
            raise AssertionError("缺少必要文件的任务包不应导入")
        assert not (incomplete_parent / task_id).exists()

        mismatch = tmp / "mismatch.yintian-task"
        inner_task = collection.load_json(task_dir / "task.json")
        inner_task["task_id"] = "YT-20990101-ABCDEF"
        required_payloads = {
            "task.json": json.dumps(inner_task, ensure_ascii=False).encode(),
            **{name: (task_dir / name).read_bytes() for name in ("public.pem", "private.pem.enc", "roster.csv", "invite-index.csv", "state.sqlite3")},
        }
        mismatch_metadata = {"package_version": collection.TASK_PACKAGE_VERSION, "task_id": task_id, "manifest": {name: collection.sha256_bytes(data) for name, data in required_payloads.items()}}
        with zipfile.ZipFile(mismatch, "w") as archive:
            archive.writestr("package.json", json.dumps(mismatch_metadata))
            for name, data in required_payloads.items():
                archive.writestr(f"{task_id}/{name}", data)
        mismatch_parent = tmp / "mismatch-import"
        try:
            collection.import_task(mismatch, mismatch_parent)
        except ValueError:
            pass
        else:
            raise AssertionError("任务包内外 task_id 不一致时不应导入")
        assert not (mismatch_parent / task_id).exists()

        invalid_content = tmp / "invalid-content.yintian-task"
        invalid_payloads = dict(required_payloads)
        invalid_payloads["task.json"] = json.dumps({"format_version": collection.FORMAT_VERSION, "task_id": task_id}).encode()
        invalid_metadata = {"package_version": collection.TASK_PACKAGE_VERSION, "task_id": task_id, "manifest": {name: collection.sha256_bytes(data) for name, data in invalid_payloads.items()}}
        with zipfile.ZipFile(invalid_content, "w") as archive:
            archive.writestr("package.json", json.dumps(invalid_metadata))
            for name, data in invalid_payloads.items():
                archive.writestr(f"{task_id}/{name}", data)
        invalid_parent = tmp / "invalid-content-import"
        try:
            collection.import_task(invalid_content, invalid_parent)
        except ValueError:
            pass
        else:
            raise AssertionError("内容残缺的任务包不应导入")
        assert not (invalid_parent / task_id).exists()

        task_document = collection.load_json(task_dir / "task.json")
        task_document["format_version"] = collection.LEGACY_FORMAT_VERSION
        collection.dump_json(task_dir / "task.json", task_document)
        assert collection.status_task(task_dir)["total"] == 1
        try:
            collection.ingest_task(task_dir, incoming)
        except RuntimeError:
            pass
        else:
            raise AssertionError("旧版任务不应继续接收提交")
        task_document["format_version"] = collection.FORMAT_VERSION
        collection.dump_json(task_dir / "task.json", task_document)
        try:
            collection.unlock_private_key(task_dir, "wrong-password")
        except Exception:
            pass
        else:
            raise AssertionError("错误密码不应解锁")
        if os.name != "nt":
            assert stat.S_IMODE(task_dir.stat().st_mode) == 0o700
            for path in (task_dir / "roster.csv", task_dir / "invite-index.csv", task_dir / "state.sqlite3", Path(reports["xlsx"]), package):
                assert stat.S_IMODE(path.stat().st_mode) == 0o600

        bad_algo_dir = tmp / "bad-algorithms"; bad_algo_dir.mkdir()
        wrong_algo = make_envelope(task_dir, invite_id, values)
        wrong_algo["algorithms"] = {"content": "AES-256-CBC", "key_wrap": "RSA-OAEP-3072-SHA256"}
        collection.dump_json(bad_algo_dir / "wrong-algo.yintian", wrong_algo)
        missing_algo = make_envelope(task_dir, invite_id, values)
        del missing_algo["algorithms"]
        collection.dump_json(bad_algo_dir / "missing-algo.yintian", missing_algo)
        algo_result = collection.ingest_task(task_dir, bad_algo_dir)
        assert algo_result["rejected"] == 2 and algo_result["accepted"] == 0
        current_row = collection.report_rows(task_dir)[0]
        assert current_row["submission_version"] == 2 and current_row["status"] == "needs_review"  # 算法不匹配的提交不覆盖已有有效版本

        with zipfile.ZipFile(package) as source:
            base_metadata = json.loads(source.read("package.json"))
            base_members = {name: source.read(name) for name in source.namelist() if name != "package.json"}

        def write_package(path: Path, metadata: dict, extra: dict | None = None) -> None:
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("package.json", json.dumps(metadata))
                for name, data in base_members.items():
                    archive.writestr(name, data)
                for name, data in (extra or {}).items():
                    archive.writestr(name, data)

        for label, attack_rel in (("traversal", "../evil.txt"), ("absolute", "/etc/passwd")):
            attack_metadata = json.loads(json.dumps(base_metadata))
            attack_metadata["manifest"][attack_rel] = collection.sha256_bytes(b"evil")
            attack_package = tmp / f"{label}.yintian-task"
            write_package(attack_package, attack_metadata, extra={f"{task_id}/{attack_rel}": b"evil"})
            try:
                collection.import_task(attack_package, tmp / f"{label}-import")
            except ValueError:
                pass
            else:
                raise AssertionError(f"包含不安全路径的任务包不应导入: {label}")
            assert not (tmp / f"{label}-import" / task_id).exists()

        duplicate = tmp / "duplicate.yintian-task"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            write_package(duplicate, base_metadata, extra={f"{task_id}/roster.csv": base_members[f"{task_id}/roster.csv"]})
        try:
            collection.import_task(duplicate, tmp / "duplicate-import")
        except ValueError:
            pass
        else:
            raise AssertionError("包含重复条目的任务包不应导入")
        assert not (tmp / "duplicate-import" / task_id).exists()

        old_member_limit = collection.MAX_PACKAGE_MEMBER_BYTES
        collection.MAX_PACKAGE_MEMBER_BYTES = 4096
        try:
            try:
                collection.import_task(package, tmp / "oversized-import")
            except ValueError:
                pass
            else:
                raise AssertionError("超过单项上限的任务包不应导入")
            assert not (tmp / "oversized-import" / task_id).exists()
        finally:
            collection.MAX_PACKAGE_MEMBER_BYTES = old_member_limit

        raw_package = bytearray(package.read_bytes())
        pos = 0
        while True:
            pos = raw_package.find(b"PK\x01\x02", pos)
            if pos < 0:
                raise AssertionError("未找到目标中央目录条目")
            name_length, extra_length, comment_length = struct.unpack_from("<HHH", raw_package, pos + 28)
            if bytes(raw_package[pos + 46 : pos + 46 + name_length]).decode() == f"{task_id}/state.sqlite3":
                struct.pack_into("<I", raw_package, pos + 24, 1)
                break
            pos += 46 + name_length + extra_length + comment_length
        fake_size = tmp / "fake-size.yintian-task"
        fake_size.write_bytes(bytes(raw_package))
        try:
            collection.import_task(fake_size, tmp / "fake-size-import")
        except (ValueError, zipfile.BadZipFile):
            pass
        else:
            raise AssertionError("声明大小作假的任务包不应导入")
        assert not (tmp / "fake-size-import" / task_id).exists()

        with zipfile.ZipFile(package) as archive:
            try:
                collection.read_package_member(archive, f"{task_id}/state.sqlite3", 10)
            except ValueError:
                pass
            else:
                raise AssertionError("流式计数必须拒绝超过剩余配额的成员")
            assert collection.read_package_member(archive, f"{task_id}/roster.csv", collection.MAX_PACKAGE_BYTES) == (task_dir / "roster.csv").read_bytes()

        def forbidden_prompt(*args, **kwargs):
            raise AssertionError("被拒绝的操作不应出现交互提示")

        real_stdin, real_stdout = sys.stdin, sys.stdout
        real_getpass, real_input = collection.getpass.getpass, builtins.input
        capture = TtyCapture()
        sys.stdin, sys.stdout = types.SimpleNamespace(isatty=lambda: True), capture
        collection.getpass.getpass = lambda prompt="": PASSWORD
        builtins.input = lambda prompt="": ""
        real_decrypt, real_validate = collection.decrypt_envelope, collection.validate_payload
        collection.decrypt_envelope = lambda *args, **kwargs: {"values": {"name": "张三"}, "attachments": {"id_front": [{"name": "front.jpg", "type": "image/jpeg", "size": "5\x1b[31mred"}]}}
        collection.validate_payload = lambda *args, **kwargs: ([], [], [])
        try:
            assert collection.cmd_reveal(types.SimpleNamespace(task_dir=str(task_dir), invite_id=invite_id)) is None
        finally:
            sys.stdin, sys.stdout = real_stdin, real_stdout
            collection.getpass.getpass, builtins.input = real_getpass, real_input
            collection.decrypt_envelope, collection.validate_payload = real_decrypt, real_validate
        shown = capture.getvalue()
        assert "\x1b[31m" not in shown and "5 [31mred bytes" in shown

        expired_doc = collection.load_json(task_dir / "task.json")
        expired_doc["deadline"] = "2020-01-01T00:00:00Z"
        expired_doc["retention_until"] = "2020-02-01T00:00:00Z"
        expired_doc["notice_hash"] = collection.sha256_bytes(collection.canonical({key: expired_doc[key] for key in ("title", "purpose", "deadline", "retention_until", "contact", "correction")}))
        collection.dump_json(task_dir / "task.json", expired_doc)
        for action in (
            lambda: collection.ingest_task(task_dir, incoming),
            lambda: collection.review_task(task_dir, PASSWORD),
            lambda: collection.write_reports(task_dir, ["json"]),
            lambda: collection.export_task(task_dir, tmp / "expired.yintian-task"),
        ):
            try:
                action()
            except RuntimeError:
                pass
            else:
                raise AssertionError("过期任务必须拒绝接收、复核、报告与导出")
        assert collection.status_task(task_dir)["task_status"] == "expired"
        sys.stdin, sys.stdout = types.SimpleNamespace(isatty=lambda: True), TtyCapture()
        collection.getpass.getpass = forbidden_prompt
        builtins.input = forbidden_prompt
        try:
            try:
                collection.cmd_reveal(types.SimpleNamespace(task_dir=str(task_dir), invite_id=invite_id))
            except RuntimeError:
                pass
            else:
                raise AssertionError("过期任务必须拒绝查看明文")
        finally:
            sys.stdin, sys.stdout = real_stdin, real_stdout
            collection.getpass.getpass, builtins.input = real_getpass, real_input

        sys.stdin, sys.stdout = types.SimpleNamespace(isatty=lambda: True), TtyCapture()
        builtins.input = lambda prompt="": "WRONG-TASK-ID"
        try:
            try:
                collection.cmd_purge(types.SimpleNamespace(task_dir=str(task_dir), allow_early=False))
            except RuntimeError:
                pass
            else:
                raise AssertionError("任务 ID 不匹配时必须取消删除")
            assert task_dir.is_dir()
            builtins.input = lambda prompt="": task_id
            purged = collection.cmd_purge(types.SimpleNamespace(task_dir=str(task_dir), allow_early=False))
        finally:
            sys.stdin, sys.stdout = real_stdin, real_stdout
            builtins.input = real_input
        assert not task_dir.exists() and Path(purged["summary"]).is_file()

        sys.stdin, sys.stdout = types.SimpleNamespace(isatty=lambda: False), io.StringIO()
        collection.getpass.getpass = forbidden_prompt
        builtins.input = forbidden_prompt
        try:
            for action in (
                lambda: collection.cmd_create(types.SimpleNamespace(roster=str(roster), config=str(config_path), out=str(tmp / "blocked"))),
                lambda: collection.cmd_review(types.SimpleNamespace(task_dir=str(task_dir))),
                lambda: collection.cmd_reveal(types.SimpleNamespace(task_dir=str(task_dir), invite_id=invite_id)),
                lambda: collection.cmd_purge(types.SimpleNamespace(task_dir=str(task_dir), allow_early=True)),
            ):
                try:
                    action()
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("非 TTY 环境必须拒绝敏感操作")
        finally:
            sys.stdin, sys.stdout = real_stdin, real_stdout
            collection.getpass.getpass, builtins.input = real_getpass, real_input
    print("PASS: encrypted collection workflow")


if __name__ == "__main__":
    main()
