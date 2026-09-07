---
name: yintian-skill
description: 端到端加密的多人私密信息收集管理。只要用户需要由 HR、行政或其他职能部门收集手机号、身份证号、住址、证件附件等敏感身份信息，或提到隐填、私密/加密收集、身份证收集、员工信息收集、离线表单、private collection、encrypted form，就应使用本 Skill，即使用户没有点名。它生成离线个人邀请，接收 .yintian 密文，在授权人员本地用 RapidOCR + OpenVINO 复核，并生成保留 employee_id/name、但不含表单敏感值的数据最小化进度报告。Agent 不得读取任务密码、私钥、解密值、OCR 原文或附件，也不得调用或启动 create、review、reveal、purge。
compatibility: Python 3.11；依赖 requirements.txt；员工端需支持 Web Crypto 的新版 Chrome 或 Edge；OCR 需 OpenVINO 兼容设备。
---

# 隐填：端到端加密的私密信息收集管理

本 Skill 管理以下闭环：

```text
创建任务 → 批量生成离线邀请 → 员工本地填写并加密
→ 私聊交回 .yintian → 本地 OpenVINO 复核 → 数据最小化 XLSX/JSON
```

先把本 `SKILL.md` 所在目录的绝对路径记为 `SKILL_ROOT`，并把该 Skill 虚拟环境中的 Python 绝对路径记为 `PYTHON`。所有脚本都用这两个绝对路径调用，不依赖当前工作目录。涉及信任边界或分享范围时，先读 `references/privacy-extraction-workflow.md`。

## AI 可执行的安全操作

```bash
"$PYTHON" "$SKILL_ROOT/scripts/collection.py" init-config --out collection.json
"$PYTHON" "$SKILL_ROOT/scripts/collection.py" ingest TASK_DIR submissions/
"$PYTHON" "$SKILL_ROOT/scripts/collection.py" status TASK_DIR
"$PYTHON" "$SKILL_ROOT/scripts/collection.py" report TASK_DIR --formats xlsx json
"$PYTHON" "$SKILL_ROOT/scripts/benchmark.py"
```

通过 MCP 操作时必须先设置 `YINTIAN_VAULT_DIR`，将任务目录和收件目录限制在指定保险箱内。`report` 会保留名单中的 `employee_id` 和姓名；未经用户明确批准，不打开、上传或转发报告。

## 只能由用户在 Agent 未控制、未录制的终端执行

`isatty()` 只能确认终端可交互，不能证明操作者是人。Agent 不得启动、旁观或捕获下列命令。`create` 会显示一次不可恢复的任务密码；`review`/`reveal` 会读取密码或明文：

```bash
"$PYTHON" "$SKILL_ROOT/scripts/collection.py" create --roster roster.csv --config collection.json --out tasks/
"$PYTHON" "$SKILL_ROOT/scripts/collection.py" review TASK_DIR
"$PYTHON" "$SKILL_ROOT/scripts/collection.py" reveal TASK_DIR INVITE_ID
"$PYTHON" "$SKILL_ROOT/scripts/collection.py" purge TASK_DIR
```

## 经用户明确要求才能执行的敏感交接

任务交接包包含明文名单标识、邀请页和状态数据库，既非整体加密也不提供恶意篡改认证；只通过已认证且加密的渠道交接。到期清理只删除指定任务目录，外部收件、交接包、备份和磁盘残留需另行处理：

```bash
"$PYTHON" "$SKILL_ROOT/scripts/collection.py" export-task TASK_DIR --out task.yintian-task
"$PYTHON" "$SKILL_ROOT/scripts/collection.py" import-task task.yintian-task --out tasks/
```

## 安全边界

- 员工邀请 HTML 包含任务公钥和每人独立的随机认证令牌；浏览器以 AES-256-GCM 加密字段、令牌和附件，以 RSA-OAEP-3072/SHA-256 封装数据密钥。
- 员工通过私聊交回 `.yintian` 密文。Skill 不提供在线接收服务。
- AI、MCP、日志和报告不得出现表单中的手机号、身份证号、住址、OCR 原文、附件内容、任务密码或私钥；报告仍含姓名和员工编号，不是匿名数据。
- 表单敏感值只在员工浏览器及授权人员的 `review`/`reveal` 本地内存中出现；默认不生成明文总表。
- 旧版文件读取和身份字段提取模块仅供内部复用，没有命令行明文输出入口，也不得注册为 MCP 工具。
- 任务到达保存期限后停止 ingest、review、report、export 和 reveal；用户通过任务 ID 二次确认删除任务目录。

## 输入约定

名单 CSV 必须包含：

```csv
employee_id,name
E001,张三
E002,李四
```

内置模板收集姓名、手机号、身份证号、住址和身份证正反面。自定义字段只允许确定性类型：`text`、`phone_cn`、`cn_id`、`date`、`address`、`single_choice`、`image_attachment`、`pdf_attachment`。
