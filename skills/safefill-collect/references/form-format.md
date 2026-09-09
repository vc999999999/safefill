# 文件协议

| 文件 | 默认用途 |
|---|---|
| `FORM.yintian-form` | HR 发给所有员工的开放模板；不含姓名名单或个人凭据 |
| `姓名-短码.yintian` | 员工回执；姓名只在外部文件名可见，正文加密 |
| `结果.xlsx` | 通过校验的最新记录 |
| `结果-attachments/` | 解密附件；Excel 使用相对路径引用 |
| `*.yintian-task` | 加密任务交接包；仅授权 HR 持有 |

## 默认开放协议

开放模板是 `yintian-form/3`，提交是 `yintian-submission/4`。模板包含任务告知、字段、公钥、`key_id`、`schema_hash` 和 `notice_hash`；`name` 必须是必填文本字段。单选项、字段数量、文本长度、附件数量与字节数都有硬上限。

每次首次提交生成随机 `OPEN-…` 回执编号。信封头只含任务、模板、公钥和随机编号，不含姓名；加密载荷包含员工确认后的 `values` 与 `attachments`。AAD 是规范 JSON 数组 `[format_version,task_id,invite_id,schema_hash,key_id]`。正文使用 AES-256-GCM，数据密钥使用 RSA-OAEP-3072/SHA-256 包裹。

更正时填写端读取员工本人上一次回执的公开信封头并沿用回执编号；收集端保留版本历史，只导出该编号的最新通过版本。没有旧回执就无法安全判定哪条同名记录应被替换。

开放任务的私钥文件始终加密。随机本地密钥以 `0600` 保存到任务目录同级的 `.safefill-keys/`，防止只复制任务目录时带走解密能力；同一操作系统账号仍是共同信任边界。加密任务交接包会把密钥材料再次包在独立交接密码下，明文旧包不得携带开放任务密钥。

## 兼容协议

定向模式保留 `yintian-form/1` / `yintian-submission/2`；群组强认证模式保留 `yintian-form/2` / `yintian-submission/3` 与 `yintian-credential/1`。只有 HR 明确要求预先名单和提交者绑定时才使用群组模式。旧无认证群发模板拒绝继续写入，不能通过迁移伪造历史认证。
