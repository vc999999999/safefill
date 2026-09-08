# yintian-form/1 收集需求格式

`.yintian-form` 是纯 JSON 的收集需求描述文件，由 `collection.py create` 在建任务时生成，
供填写端（邀请 HTML 的机读副本、yintian-fill 填写器）读取并据此在本地构造
`yintian-submission/2` 加密提交信封。文件只含公钥，不含任何私钥材料。

## 顶层字段

| 字段 | 说明 |
| --- | --- |
| `format` | 固定为 `yintian-form/1` |
| `format_version` | 填写端应产出的提交信封版本，当前为 `yintian-submission/2` |
| `mode` | `directed`（定向型，默认）或 `group`（群发型），决定身份绑定语义 |
| `task_id` | 任务编号，`YT-YYYYMMDD-XXXXXX` |
| `title` / `purpose` / `deadline` / `retention_until` / `contact` / `correction` | 收集告知六要素，须原样展示给填写人 |
| `template_version` | 表单模板版本，载荷需带回供复核比对 |
| `fields` | 字段定义数组：`{id, type, label, required, options?}`，另有 `sensitive`（脱敏预览用）与附件字段的 `multiple`；类型见 collection-config.md |
| `public_key_pem` | 任务 RSA-3072 公钥（PEM），用于 OAEP-SHA256 包裹一次性 AES-256 数据密钥 |
| `key_id` | 公钥指纹（DER 的 SHA-256 前 24 位十六进制），填写前供人工核对 |
| `schema_hash` | `sha256(canonical(fields))`，信封与载荷均须带回 |
| `notice_hash` | `sha256(canonical({title,purpose,deadline,retention_until,contact,correction}))`，载荷须带回 |
| `created_at` | 任务创建时间（UTC ISO 8601） |
| `invite_id` / `invite_token` / `name` | **仅 directed 模式存在**：个人邀请编号、认证令牌、预填姓名 |

## 双模式语义

### directed 定向型（默认）

- 每人生成 `invites/INV-XXXXXXXXXX.yintian-form`，与该人的 `INV-*.html` 内嵌配置是同一份数据
  （`invite_id` / `invite_token` / `key_id` 完全一致）；HTML 页是表单的人类可读渲染版。
- 含个人认证令牌 `invite_token`：服务端只存 SHA-256 哈希，复核时恒定时间比对；
  令牌错误或不匹配的提交在复核时直接判 `invalid`，不覆盖已有有效版本。
- 信封 `invite_id = INV-...`，每人每邀请的版本上限独立计数。
- 分发：按 `invite-index.csv` 逐人私聊发送，不得串发；任何人拿到他人的 form 文件也无法伪造令牌之外的字段绑定。

### group 群发型

- 任务根目录只生成单份 `FORM.yintian-form`：无 `invite_id`、无 `invite_token`、无 `name`，
  含全部字段定义与任务公钥，可原样群发给全体填写人。
- 信封 `invite_id` 使用 `GRP-<employee_id>` 约定（`GRP-` 前缀 + 名单中的工号），
  收集端按工号定位名单记录，版本上限按工号维度计数（同 `MAX_VERSIONS_PER_INVITE`）。
- 载荷内 `invite_token` 可缺席，收集端跳过令牌比对。
- **身份仅靠 `values` 中的 `employee_id` + `name` 与名单比对**：不一致只打复核冲突标记
  （`employee_id_roster_mismatch` / `name_roster_mismatch`），进入人工复核兜底，绝不自动拒绝或覆盖。
- group 任务的字段必须包含 `id=employee_id` 的工号字段（建任务时强制校验），否则填写端无法回填身份。

## 风险对照表

| 维度 | directed | group |
| --- | --- | --- |
| 认证令牌 | 每人独立随机令牌，库中仅存 SHA-256 | 无令牌 |
| 冒名提交 | 无法通过令牌校验，复核判 invalid | 任何拿到表单的人可为任何工号提交，只能靠名单比对标记 + 人工复核兜底 |
| 分发成本 | 逐人私聊，泄露面小 | 单份群发，泄露面等同公开 |
| 适用场景 | 身份证号、证件影像等高敏感定向收集 | 低敏感、群公告式一次性分发 |
| 风险声明 | 私聊送达是工作流约定，非密码学身份认证 | 创建时必须向用户如实声明可冒名风险；高敏感场景应改用 directed |

## 填写端产出约定

填写端（HTML 页或 yintian-fill）读取本文件后，在本地组装载荷并用 Web Crypto 加密：

- 载荷带回 `format_version` / `task_id` / `invite_id` / `schema_hash` / `notice_hash` / `template_version`，
  directed 模式另带 `invite_token`；`consent_confirmed` 必须为 `true`。
- 信封 AAD 绑定 `[format_version, task_id, invite_id, schema_hash, key_id]`，算法套件固定为
  `AES-256-GCM` + `RSA-OAEP-3072-SHA256`；收集端对信封做算法白名单、尺寸、去重、过期校验，
  mode 只从服务端 `task.json` 读取，信封与载荷无法伪造。

## 已知限制

- group 任务暂不支持 `export-task` / `import-task` 交接（GRP- 标识未纳入交接包清单校验）；如需交接请使用 directed 模式任务。
