"""SafeFill · 中大型团队离线邀请分发与催办辅助 CLI。

纯本地运行，不发起任何网络请求，辅助职能人员高效、精准分发个人邀请文件并跟踪催办。
"""
from __future__ import annotations

import argparse
import csv
import json
import io
import os
import sys
from pathlib import Path
from typing import Any

import collection
import secure_io

REQUIRED_INDEX_COLUMNS = ("employee_id", "name", "invite_id", "invite_file")


def _one_line(value: Any) -> str:
    """姓名、工号等字段来自名单 CSV，拼入消息前去掉换行，防止破坏消息结构或伪装成系统指令。"""
    return " ".join(str(value).splitlines()).strip()


def load_invite_index(task_dir: Path) -> list[dict[str, str]]:
    index_path = secure_io.checked_path(task_dir / "invite-index.csv", task_dir)
    if not index_path.is_file():
        raise FileNotFoundError(f"未找到邀请索引文件: {index_path}")
    with io.StringIO(secure_io.read_bytes(index_path, 8 * 1024 * 1024).decode('utf-8-sig')) as stream:
        reader = csv.DictReader(stream)
        missing_cols = [col for col in REQUIRED_INDEX_COLUMNS if col not in (reader.fieldnames or [])]
        if missing_cols:
            raise ValueError(f"邀请索引文件缺少必需列: {', '.join(missing_cols)}")
        return list(reader)


def safe_invite_relpath(invite_file: str) -> Path | None:
    """只接受公共或逐人机器协议文件，并拒绝路径穿越。"""
    if not invite_file or "\\" in invite_file:
        return None
    rel = Path(invite_file)
    if rel.is_absolute() or ".." in rel.parts:
        return None
    if len(rel.parts) > 1 and rel.parts[0] != "invites":
        return None
    name = rel.name
    if len(rel.parts) == 1 and name == "FORM.yintian-form":
        return rel
    return rel if name.endswith(".yintian-form") and collection.INVITE_ID_RE.fullmatch(name[:-13]) else None


def verify_invites(task_dir: Path) -> dict[str, Any]:
    """校验所有邀请文件是否存在且非空；invite_file 路径非法的行记入 invalid。"""
    rows = load_invite_index(task_dir)
    missing, invalid, valid = [], [], 0
    for row in rows:
        rel = safe_invite_relpath(row["invite_file"])
        if rel is None:
            invalid.append(row)
            continue
        try:
            target = secure_io.checked_path(task_dir / rel, task_dir)
            if row['invite_id'].startswith(collection.GROUP_INVITE_PREFIX):
                credential = secure_io.checked_path(task_dir / 'credentials' / (row['invite_id'] + '.yintian-credential'), task_dir)
                if not credential.is_file() or credential.stat().st_size == 0:
                    missing.append(row)
                    continue
        except ValueError:
            invalid.append(row)
            continue
        if target.is_file() and target.stat().st_size > 0:
            valid += 1
        else:
            missing.append(row)
    return {
        "total": len(rows),
        "valid": valid,
        "missing": missing,
        "invalid": invalid,
        "all_valid": not missing and not invalid,
    }


def generate_messages(task_dir: Path, template: str | None = None) -> list[dict[str, str]]:
    """生成面向每位员工的专属私聊文案，避免职能人员手动复制错发。"""
    rows = load_invite_index(task_dir)
    _, task = collection.load_task(task_dir)
    title = _one_line(task.get("title", "信息收集"))
    deadline = _one_line(task.get("deadline", "按期"))

    default_tpl = (
        "【{title}】您好，{name}（工号：{employee_id}）：\n"
        "请把专属机器请求文件 {invite_name} 交给 safefill-fill Agent，由 Agent 在对话中问齐并加密；"
        "不要打开或手工编辑请求文件。生成的 .yintian 密文请直接私聊交回。截止时间：{deadline}。"
    )
    if collection.task_mode(task) == 'group':
        default_tpl = (
            "【{title}】{name}（{employee_id}），请用 SafeFill 填写者 Skill 读取群内 FORM.yintian-form，"
            "在本人终端解锁保险柜填写；本人的 {invite_id}.yintian-credential 将私下发放，不能发到群里。"
            "仅交回 .yintian 密文，截止时间：{deadline}。"
        )
    tpl = template or default_tpl

    results = []
    for r in rows:
        rel = safe_invite_relpath(r['invite_file'])
        if rel is None:
            raise ValueError('INDEX_INVALID: 分发索引含非法邀请或文件路径')
        if collection.task_mode(task) == 'group':
            collection.parse_group_employee_id(r['invite_id'])
        elif not collection.INVITE_ID_RE.fullmatch(r['invite_id']):
            raise ValueError('INDEX_INVALID: 非法邀请标识')
        secure_io.checked_path(task_dir / rel, task_dir)
        msg = tpl.format(
            title=title,
            name=_one_line(r["name"]),
            employee_id=_one_line(r["employee_id"]),
            invite_id=r["invite_id"],
            invite_file=r["invite_file"],
            invite_name=Path(r["invite_file"]).name,
            deadline=deadline,
        )
        results.append({
            "employee_id": r["employee_id"],
            "name": r["name"],
            "invite_id": r["invite_id"],
            "invite_path": str(task_dir / r["invite_file"]),
            "message": msg,
            **({"credential_path": str(task_dir / 'credentials' / (r['invite_id'] + '.yintian-credential')),
                "credential_delivery": "private", "template_delivery": "group"} if collection.task_mode(task) == 'group' else {}),
        })
    return results


def export_messages_csv(msgs: list[dict[str, str]], out_csv: str, force: bool = False) -> Path:
    """导出分发文案 CSV；落盘只含机器请求文件名，拒绝符号链接与意外覆盖，写后收紧权限为 0600。"""
    out_path = Path(out_csv).expanduser()
    try:
        secure_io.checked_path(out_path)
    except ValueError:
        raise RuntimeError('PATH_UNSAFE: 输出路径包含符号链接或重解析点') from None
    rows = [
        {
            "employee_id": item["employee_id"],
            "name": item["name"],
            "invite_id": item["invite_id"],
            "invite_file": Path(item["invite_path"]).name,
            "message": item["message"],
        }
        for item in msgs
    ]
    try:
        buffer = io.StringIO(newline='')
        writer = csv.DictWriter(buffer, fieldnames=["employee_id", "name", "invite_id", "invite_file", "message"])
        writer.writeheader()
        writer.writerows([{key: collection.csv_text(value) for key, value in row.items()} for row in rows])
        secure_io.atomic_write(out_path, buffer.getvalue().encode('utf-8-sig'), overwrite=force)
    except FileExistsError:
        raise RuntimeError(f"输出文件已存在，拒绝覆盖（确认要覆盖请加 --force）: {out_path}") from None
    return out_path


def pending_reminders(task_dir: Path) -> list[dict[str, Any]]:
    """比对当前提交状态数据库，筛选出尚未提交的员工名单与催交文案。"""
    rows = collection.report_rows(task_dir)
    # 数据库内部初始状态为 invited；此处统一映射为面向人工的 "pending"（尚未交回）
    pending = [r for r in rows if r["status"] == "invited"]
    _, task = collection.load_task(task_dir)
    deadline = _one_line(task.get("deadline", "按期"))

    reminders = []
    for p in pending:
        name = _one_line(p["name"])
        reminders.append({
            "employee_id": p["employee_id"],
            "name": p["name"],
            "status": "pending",
            "reminder_message": f"【温馨提醒】{name} 同事，您的私密信息收集尚未交回，截止时间为 {deadline}，请尽快完成填报并私聊交回 .yintian 密文文件，感谢配合！",
        })
    return reminders


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SafeFill · 离线邀请分发与催办辅助")
    sub = parser.add_subparsers(dest="cmd", required=True)

    v_cmd = sub.add_parser("verify", help="校验邀请文件完整性")
    v_cmd.add_argument("task_dir", help="任务目录路径")

    m_cmd = sub.add_parser("messages", help="生成防串发个性化私聊发送文案")
    m_cmd.add_argument("task_dir", help="任务目录路径")
    m_cmd.add_argument("--json", action="store_true", help="以 JSON 格式输出")
    m_cmd.add_argument("--out-csv", help="导出为分发文案 CSV 文件（只含机器请求文件名，已存在时拒绝覆盖）")
    m_cmd.add_argument("--force", action="store_true", help="允许覆盖已存在的 --out-csv 文件")
    m_cmd.add_argument("--template", help="自定义文案模板，可用占位符 {title} {name} {employee_id} {invite_id} {invite_file} {invite_name} {deadline}")

    r_cmd = sub.add_parser("pending", help="查询未交员工清单并生成催交通知")
    r_cmd.add_argument("task_dir", help="任务目录路径")
    r_cmd.add_argument("--json", action="store_true", help="以 JSON 格式输出")

    args = parser.parse_args(argv)
    task_dir = Path(args.task_dir).expanduser().resolve()

    if not task_dir.is_dir():
        print(f"错误: 任务目录不存在: {task_dir}", file=sys.stderr)
        return 2

    try:
        if args.cmd == "verify":
            res = verify_invites(task_dir)
            print(f"任务共包含 {res['total']} 份邀请：有效 {res['valid']} 份，缺失 {len(res['missing'])} 份，路径非法 {len(res['invalid'])} 份。")
            if not res["all_valid"]:
                for m in res["missing"]:
                    print(f"  ❌ 缺失: {m['employee_id']} - {m['name']} ({m['invite_file']})")
                for m in res["invalid"]:
                    print(f"  ⚠️ 路径非法: {m['employee_id']} - {m['name']} ({m['invite_file']})")
                return 1
            print("  ✅ 全部邀请文件均完整就绪。")
            return 0

        if args.cmd == "messages":
            msgs = generate_messages(task_dir, template=args.template)
            if args.out_csv:
                out_path = export_messages_csv(msgs, args.out_csv, force=args.force)
                print(f"已导出 {len(msgs)} 条个性化分发文案至: {out_path}")
                return 0
            if args.json:
                print(json.dumps(msgs, ensure_ascii=False, indent=2))
            else:
                for item in msgs:
                    invite_filename = Path(item["invite_path"]).name
                    print(f"--- [工号 {item['employee_id']} - {item['name']}] ---")
                    print(f"机器请求文件: {invite_filename}")
                    print(item["message"])
                    print()
            return 0

        if args.cmd == "pending":
            p = pending_reminders(task_dir)
            if args.json:
                print(json.dumps(p, ensure_ascii=False, indent=2))
            else:
                print(f"当前待提交人数: {len(p)} 人")
                for item in p:
                    print(f"- {item['employee_id']} {item['name']}: {item['reminder_message']}")
            return 0
    except Exception as exc:  # 含缺文件、坏 CSV、损坏的状态数据库等，CLI 一律友好报错而非 traceback
        print(f"错误: {exc}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
