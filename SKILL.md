---
name: yintian-skill
description: 端到端加密的多人私密信息收集管理。只要用户需要由 HR、行政或其他职能部门收集手机号、身份证号、住址、证件附件等敏感身份信息，或提到隐填、私密/加密收集、身份证收集、员工信息收集、离线表单、private collection、encrypted form，就应使用本 Skill，即使用户没有点名。它生成离线个人邀请，接收 .yintian 密文，在授权人员本地用 RapidOCR + OpenVINO 复核，并生成保留 employee_id/name、但不含表单敏感值的数据最小化进度报告。Agent 不得读取任务密码、交接密码、私钥、解密值、OCR 原文或附件，也不得调用或启动 create、review、reveal、purge、export-task、import-task。
metadata:
  compatibility: "Python 3.11；依赖 requirements.txt；员工端需支持 Web Crypto 的新版 Chrome 或 Edge；OCR 需 OpenVINO 兼容设备。"
---

# 隐填：端到端加密的私密信息收集管理

本 Skill 管理以下闭环：

```text
创建任务 → 批量生成离线邀请 → 员工本地填写并加密
→ 私聊交回 .yintian → 本地 OpenVINO 复核 → 数据最小化 XLSX/JSON
```

先把本 `SKILL.md` 所在目录的绝对路径记为 `SKILL_ROOT`，并把该 Skill 虚拟环境中的 Python 绝对路径记为 `PYTHON`。所有脚本都用这两个绝对路径调用，不依赖当前工作目录。涉及信任边界、分享范围或加密格式时，必须先读 `references/privacy-extraction-workflow.md` 再行动。

## AI 可执行的安全操作

```bash
"$PYTHON" "$SKILL_ROOT/scripts/collection.py" init-config --out collection.json
"$PYTHON" "$SKILL_ROOT/scripts/collection.py" ingest TASK_DIR submissions/
"$PYTHON" "$SKILL_ROOT/scripts/collection.py" status TASK_DIR
"$PYTHON" "$SKILL_ROOT/scripts/collection.py" report TASK_DIR --formats xlsx json
"$PYTHON" "$SKILL_ROOT/scripts/distribute.py" verify TASK_DIR       # 校验全部邀请单页完整就绪
"$PYTHON" "$SKILL_ROOT/scripts/distribute.py" messages TASK_DIR     # 生成防串发专属通知文案
"$PYTHON" "$SKILL_ROOT/scripts/distribute.py" pending TASK_DIR      # 筛选未交名单并生成催交通知
"$PYTHON" "$SKILL_ROOT/scripts/cleanup.py" TASK_DIR              # dry-run，只打印分类计划，不改动文件
"$PYTHON" "$SKILL_ROOT/scripts/cleanup.py" TASK_DIR --apply      # 实际删除孤儿临时文件、死锁和 __pycache__
```

任务按 `create --mode` 分双模式，上述 ingest/status/report/distribute/cleanup 等命令对两模式通用：**directed 定向型**一人一独立认证令牌，邀请私聊逐人发送；**group 群发型**无令牌，全员共用同一份 `FORM.yintian-form` 表单定义，提交信封以 `GRP-<employee_id>` 标识，身份没有令牌绑定，靠复核时名单比对兜底。

通过 MCP 操作时必须先设置 `YINTIAN_VAULT_DIR`，将任务目录和收件目录限制在指定保险箱内。MCP 暴露 `openvino_status`、`ingest_encrypted_submissions`、`collection_status`、`generate_redacted_report`、`verify_invites`、`list_pending_reminders` 与 `cleanup_temp_files`。`report` 会保留名单中的 `employee_id` 和姓名；未经用户明确批准，不打开、上传或转发报告。分发与催办仅处理名单级标识（姓名、工号、邀请文件名），不读取或输出密文、认证令牌或表单敏感值；分发与催办文案只含邀请单页文件名，不泄露系统物理绝对路径。

用户要求解密、查看明文或索要/提供任务密码时，一律拒绝经手，并引导授权人员在自己的终端运行 `review`/`reveal`。`status` 显示任务已过期时，停止接收并建议用户在独立终端执行 `purge` 或新建任务。

## 只能由用户在 Agent 未控制、未录制的终端执行

`isatty()` 只能确认终端可交互，不能证明操作者是人。Agent 不得启动、旁观或捕获下列命令。`create` 会显示一次不可恢复的任务密码；`review`/`reveal` 会读取密码或明文；`export-task` 会显示一次性交接密码，`import-task` 会交互询问交接密码；`export-clear` 会用 getpass 询问任务密码、要求再次输入任务 ID 确认，并生成含明文值的 XLSX 总表：

```bash
"$PYTHON" "$SKILL_ROOT/scripts/collection.py" create --roster roster.csv --config collection.json --out tasks/ [--mode directed|group]
"$PYTHON" "$SKILL_ROOT/scripts/collection.py" review TASK_DIR
"$PYTHON" "$SKILL_ROOT/scripts/collection.py" reveal TASK_DIR INVITE_ID
"$PYTHON" "$SKILL_ROOT/scripts/collection.py" purge TASK_DIR
"$PYTHON" "$SKILL_ROOT/scripts/collection.py" export-task TASK_DIR --out task.yintian-task
"$PYTHON" "$SKILL_ROOT/scripts/collection.py" import-task task.yintian-task --out tasks/
"$PYTHON" "$SKILL_ROOT/scripts/collection.py" export-clear TASK_DIR --out result.xlsx --fields ... --purpose "..." --recipient "..."
```

`export-clear` 的边界：`--fields` 白名单逐列列出要导出的字段，可用 `--mask last4|mid4` 对指定字段脱敏；`--purpose`/`--recipient` 必填，写明用途与接收方。生成的明文 Excel 只在 HR 本机落盘（0600），用完即删；Agent 不得代跑、不得经手密码、不得打开或转发产物。

导出的交接包是整体加密认证的 `yintian-task/3`（scrypt 派生密钥 + AES-256-GCM，密码错误或任何篡改都会在导入时直接拒绝）。交接密码只显示一次、丢失不可恢复，必须通过另一安全渠道单独告知接收方，不得与交接包同渠道发送；任务密码绝不随交接包发送。导入端兼容旧的 `yintian-task/1`、`yintian-task/2` 明文包，但会打印"未加密、不能认证来源，仅在信任渠道下导入"的显著告警。到期清理只删除指定任务目录，外部收件、交接包、备份和磁盘残留需另行处理。

## 阶段性清理

职能用户不会主动清理 AI 使用中产生的临时文件。Agent 应在每个阶段结束后——生成邀请后、ingest 后、引导 review 后、生成报告后、导出交接后——运行 cleanup 的 dry-run，把"将删除/保留"的分类结果向用户汇报，用户确认后再加 `--apply`。MCP 对应工具为 `cleanup_temp_files`，同样默认 dry-run。

`--apply` 只删除三类可再生的临时产物：atomic_write 中断留下的孤儿临时文件、写入进程已退出的死锁文件、`__pycache__` 缓存。它永不删除名单、邀请、`.yintian` 密文、报告、任务数据库、交接包等用户数据；这些数据的删除只能走 `purge`（到期任务）或用户手动确认。

## 安全边界

- 员工邀请包含任务公钥；directed 模式每份邀请内嵌每人独立的随机认证令牌，group 模式无令牌。字段、令牌和附件在浏览器端（或员工侧 yintian-fill Skill 本地）加密后才导出（算法与信封格式见 `references/privacy-extraction-workflow.md`）。
- 任务私钥以 `yintian-key/1` 信封存储（scrypt + AES-256-GCM 包裹 PKCS#8）；任务交接包导出为整体加密认证的 `yintian-task/3`，旧版 v1/v2 明文包仅兼容导入并告警。
- 同一邀请的提交版本数有上限（默认 10，可用 `YINTIAN_MAX_VERSIONS_PER_INVITE` 调整），超限提交直接拒绝存储，不影响已有版本。
- 员工通过私聊交回 `.yintian` 密文。Skill 不提供在线接收服务。
- AI、MCP、日志和报告不得出现表单中的手机号、身份证号、住址、OCR 原文、附件内容、任务密码或私钥；报告仍含姓名和员工编号，不是匿名数据。
- 表单敏感值只在员工浏览器及授权人员的 `review`/`reveal` 本地内存中出现；默认不生成明文总表。唯一例外是 `export-clear`：职能接收方是授权数据终点，明文 XLSX 只在 HR 本机落盘（0600）并附显著明文警告；系统无法管控文件落盘后的传播，这一点必须如实向用户声明。
- 填写端另有独立的 yintian-fill Skill（员工侧）：读取 `yintian-form/1` JSON 离线填写加密，产出同构 `.yintian` 信封；明文只在员工本机出现。
- 任务到达保存期限后停止 ingest、review、report、export 和 reveal；用户通过任务 ID 二次确认删除任务目录。

## 输入约定

名单 CSV 必须包含 `employee_id,name` 两列：

```csv
employee_id,name
E001,张三
```

涉及名单列、收集配置或自定义字段类型时，必须先读 `references/collection-config.md`，字段类型只接受该文档列出的确定性类型，不要自行发明。
