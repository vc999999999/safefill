# 文件协议

| 文件 | 用途与分发 |
|---|---|
| `FORM.yintian-form` | 字段、告知、任务公钥，可发群 |
| `GRP-工号.yintian-credential` | 任务/公钥/模板绑定、工号姓名、个人令牌，仅本人私下接收 |
| `INV-*.yintian-form / .html` | 内含个人令牌的定向模板，私下发放 |
| `*.yintian-vault` | 加密个人字段及附件，留在本机 |
| `*.yintian` | 加密提交，按指定渠道交回 |
| `*.yintian-task` | 加密任务交接，仅授权 HR |

## 兼容

定向保留 `yintian-form/1` 和 `yintian-submission/2`。新群发模板 `yintian-form/2`、提交 `yintian-submission/3`，要求 `submission_auth=hmac-sha256-token/1`；旧程序不能无声跳过认证。

旧无认证群发模板拒绝填写，任务仅查询；请新建任务，不能通过迁移伪造过去认证。数据库独立版本 `PRAGMA user_version=1`；旧定向写入提示显式迁移，查询不迁移。交接外层仍为加密 `yintian-task/3`，包含模板、凭据、密文和数据库一致性快照。

## 模板与凭据

模板有 `task_id/title/purpose/deadline/retention_until/contact/correction/fields/public_key_pem/key_id/schema_hash/notice_hash`。`key_id` 是公钥 DER 的 SHA-256 前 24 个十六进制字符，须经独立渠道核对。

`schema_hash` 覆盖规范 JSON 字段清单（含 `ocr_fields`），`notice_hash` 覆盖告知。个人凭据格式 `yintian-credential/1`，绑定任务、公钥、字段摘要、工号姓名和个人令牌。

## 提交

AAD 是规范 JSON 数组 `[format_version,task_id,invite_id,schema_hash,key_id]`。加密载荷包含告知、时间、确认标记、个人令牌、`values` 与 `attachments`。附件只允许请求字段，含文件名、类型、大小、SHA-256、base64 字节。

群发信封额外带：

```text
auth_tag = HMAC-SHA256(SHA256(invite_token), canonical_json(envelope_without_auth_tag))
```

标签覆盖全部密文和头。收件端用数据库令牌摘要验证后才落盘和计数；无凭据、篡改或跨邀请提交不消耗版本配额。相同密文按哈希去重。确认标记必须由本地程序在本人确认后写入。

## 保险柜

`yintian-vault/1` 使用受限参数 scrypt、随机盐/nonce、AES-256-GCM，加密字段类型、值和附件字节，不存明文路径引用。
按显式映射、同名同类型、唯一语义类型（手机号/证件号/住址/日期）匹配。姓名等文本不会仅凭类型猜测。多候选和缺项由本人处理，仅选择本模板字段。

手工 `seal --values ...` 保留为本人终端兼容入口，也要确认指纹和本次内容。保险柜主流程不需要明文中间 JSON。
