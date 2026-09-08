> 3.5.0 可选识别的模式、宿主授权与 Python 边界以 [recognition.md](recognition.md) 为准；此前的“Agent 不接触原始附件”限制，仅对该文档明确授权的填写者原件识别入口作例外。收集者解密权限不变。

# 本人终端操作

敏感命令只在本人控制且未被 Agent 录制的终端运行。TTY 只能排除普通重定向，不能识别人类；同一操作系统账号下的其他程序不是强隔离边界。

## HR

1. 运行 `python scripts/collection.py doctor`。用 `init-config --mode group --out collection.json` 生成配置，填写用途、联系人、更正方式、期限，准备 `employee_id,name` 名单。
2. 创建：`python scripts/collection.py create --roster roster.csv --config collection.json --out tasks --mode group`。
3. `FORM.yintian-form` 发群；`credentials/GRP-工号.yintian-credential` 私下发给对应本人，不能公开或转借。另一可信渠道核对任务编号和公钥指纹。`distribute.py messages TASK --json` 提供分发清单，不自动发送。
4. 密文保存到收件目录，`ingest TASK INCOMING` 后复核：
   ```bash
   python scripts/collection.py review TASK
   python scripts/collection.py review TASK --retry-needs-review --invite GRP-E001
   ```
5. 人工逐页查看图片/PDF、缩放旋转并逐项确认，或退回重填：
   ```bash
   python scripts/collection.py decide TASK GRP-E001 --version 2 --action confirm --operator hr-01
   python scripts/collection.py decide TASK GRP-E001 --version 2 --action return --operator hr-01
   ```
   人工仅裁定 OCR 问题；缺必填、格式错误、身份不符、未确认告知、认证或运行故障不能放行。窗口空闲五分钟关闭；提交时重新核对版本和期限。缺 Tk 时先修复环境或退回重填。
6. 汇总：
   ```bash
   python scripts/collection.py export-clear TASK --out result.xlsx --fields phone,id_number,address --mask id_number=last4 --purpose "办理保险" --recipient "保险对接人"
   ```
   需密码及任务编号确认。姓名、工号默认附带，显示未纳入人数。每人只导出最新已通过版本；旧通过记录保留，但新版本待处理时不静默导出旧数据。已有文件不覆盖，更换文件名。明文 Excel 只给授权接收方，后续传播由授权人管理。

`report` 是含姓名、工号和状态的进度表；`status` 返回计数。

| 代码/状态 | 处理 |
|---|---|
| TASK_BUSY | 等另一进程结束；不要删除操作系统锁文件 |
| MIGRATION_REQUIRED | `migrate TASK`，生成受限权限的数据库备份后升级 |
| GROUP_AUTH_REQUIRED | 旧无认证群发任务只读；新建并发个人凭据 |
| needs_review + retryable | 恢复依赖后按邀请重试 |
| needs_review，输入有误 | 退回重填，不能用人工确认越过校验 |
| STATE_CHANGED | 刷新新提交、判定或期限后重新操作 |
| OUTPUT_EXISTS / PATH_UNSAFE | 更换合法路径，不关闭保护 |

交接使用 `export-task/import-task` 的加密 v3 包，包含模板、个人凭据和 SQLite 一致性快照。交接密码经另一安全渠道告知；任务密码单独管理。旧明文包导入须核对来源，导入不自动升级数据库。

## 员工

见 [填写端](../yintian-fill/README.md)。保险柜保存可复用字段及附件字节，日常填写不创建明文临时文件；不自动接管外部密码管理器或扫描磁盘。

旧数据库迁移会把旧自动通过记录转为待重新复核，以免沿用旧版宽松 OCR 判断；用 review --retry-needs-review 重新核验。邀请和密文本身不改写。
