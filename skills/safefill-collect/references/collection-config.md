# 收集配置

生成一份 `REQUEST-{任务编号}.yintian-request` 机器请求包（文件名含任务编号，多任务不同名），不需要名单或个人凭据。请求包只描述告知、字段、校验规则和收集方公钥，不含网页界面或员工值；员工通过私有对话或本地文本补录，确认后由填写端脚本写入加密载荷。

## HR 需要说明的内容

- `purpose`：为何收集；
- `fields`：要哪些信息、是否必填；
- `deadline`：截止时间；
- `contact`：联系人及联系方式。

Agent 自动补齐：`title` 从用途概括，`name` 设为必填，`retention_until` 默认为截止后 30 天，`correction` 默认为联系任务联系人后重新提交，`template_version` 为 `1.0`。完成后必须调用 `create-request`；不得把配置直接交给 HR，也不得生成 HTML 表单。

## 时间必须带时区

`deadline` 与 `retention_until` 必须是带时区偏移的 ISO 时间，例如 `2026-10-01T18:00:00+08:00`。纯日期（`2026-10-01`）或无时区时间会被 `create-request` 以 `TIME_ZONE_REQUIRED` 拒绝：不同机器会把它理解成不同时刻，员工 Agent 转述的截止时间会与 HR 本意相差数小时。HR 只说"10 月 1 日"时，Agent 按 HR 所在时区补成当天 18:00 或 23:59 并在转述时说明。

`create-request` 返回的 `reminder` 会写明保存期限：**收件、汇总、解密查看必须在 `retention_until` 之前完成**，到期后脚本按告知承诺拒绝解密任何回执，只能新建请求包重收。Agent 生成请求包后准确转述期限与影响；HR 预计汇总较晚时，把 `retention_until` 设得更远。此前已导出的本地 Excel 不会自动失效。

## 标准字段 id

字段 id 使用小写英文和下划线。**同一含义务必使用下表的标准 id**：员工端保险柜按 id 精确匹配复用，`vault-scan` 识别结果也用这些 id 输出；自创 id（`mobile`、`phone_number`、`idcard`）会让每位员工每次都要额外确认一次映射。

| 含义 | id | type |
|---|---|---|
| 姓名 | `name` | `text`（自动加入，必填） |
| 工号 | `employee_id` | `text` |
| 本人手机号 | `phone` | `phone_cn` |
| 身份证号 | `id_number` | `cn_id` |
| 性别 | `gender` | `single_choice`（`["男","女"]`） |
| 出生日期 | `birth_date` | `date` |
| 户籍/住址 | `address` | `address` |
| 现居地址 | `current_address` | `address` |
| 电子邮箱 | `email` | `text` |
| 入职日期 | `hire_date` | `date` |
| 紧急联系人姓名 | `emergency_contact_name` | `text` |
| 紧急联系人电话 | `emergency_contact_phone` | `phone_cn` |
| 银行卡号 | `bank_card` | `text` |
| 开户行 | `bank_name` | `text` |
| 身份证正面 | `id_front` | `image_attachment` |
| 身份证反面 | `id_back` | `image_attachment` |
| 证件照 | `photo` | `image_attachment` |
| 学历证明 | `education_certificate` | `pdf_attachment` 或 `image_attachment` |

表外字段按同样风格命名（如 `graduation_date`、`degree`），并尽量复用上表的类型选择：类型不同（`text` 与 `address`）的条目即使语义相同也无法映射。

## 字段类型

支持：`text`、`phone_cn`、`cn_id`、`date`、`address`、`single_choice`、`image_attachment`、`pdf_attachment`。单选字段必须提供不重复的 `options`。附件不会嵌入 Excel，而是解密到同名附件目录，Excel 单元格保存相对路径。字段有确定格式时使用对应类型，不要全部降级成 `text`。

## 附件自动比对（ocr_fields）是重交互操作

附件默认不做自动比对（`ocr_fields` 不写或写 `[]`）。字段名本身（如 `id_front`）不会触发比对，只有显式 `ocr_fields` 才会。

只有 HR 明确要求"附件内容必须与某些字段自动核对"时，才把这些标量字段 id 写入 `ocr_fields`，并且**先告知 HR 代价**：比对不通过或收件机器缺少 OCR 依赖的回执不会进入 Excel，而 `decide` 裁定必须由 HR 本人在交互终端逐项看图完成，Agent 不能代跑。`create-request` 返回的 `ocr_bound_fields` 与 `reminder` 会列出启用了比对的字段。

## 身份与更正

v5 回执编号由任务与提交者签名公钥派生；签名证明同一密钥持有者的更正连续性，不认证现实员工身份。填写端默认沿用本机保险柜中的任务身份、递增修订，旧回执或提交登记丢失不影响更正。`--previous` 仅用于显式选择本机持有私钥的旧身份，明确新建记录才使用与它互斥的 `--fresh`，不能用作错误兜底。

HR 用 `notice` 生成补正通知，员工以同一编号提交更高修订。`collect` 的 `duplicate_name_groups` 只返回同名记录的编号组，由 HR 在本地结果中核对。需要限制指定员工提交时，应使用独立认证渠道；本协议不提供名单或账号机制。修订选择与冲突处理见 [PROTOCOL.md](PROTOCOL.md)。
