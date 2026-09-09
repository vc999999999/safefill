# 开放收集配置

默认使用 `open`：一份公共 `FORM.yintian-form`，不需要名单或个人凭据。姓名是模板中的必填字段，由员工通过 `safefill-fill` 填入加密载荷，收集端解密后写入 Excel。

## HR 需要说明的内容

- `purpose`：为何收集；
- `fields`：要哪些信息、是否必填；
- `deadline`：截止时间；
- `contact`：联系人及联系方式。

Agent 自动补齐：`title` 从用途概括，`name` 设为必填，`retention_until` 默认为截止后 30 天，`correction` 默认为联系任务联系人后重新提交，`template_version` 为 `1.0`。

字段 id 使用小写英文和下划线。支持：`text`、`phone_cn`、`cn_id`、`date`、`address`、`single_choice`、`image_attachment`、`pdf_attachment`。单选字段必须提供不重复的 `options`。附件不会嵌入 Excel，而是解密到同名附件目录，Excel 单元格保存相对路径。字段有确定格式时使用对应类型，不要全部降级成 `text`。

默认附件配置 `ocr_fields: []`，避免普通材料收集因 OCR 环境或误识别而阻塞。只有 HR 明确要求附件内容与某些字段自动比对时，才把这些标量字段 id 写入 `ocr_fields`；无法确定性通过的回执会被排除，不能自动放行。

开放模板只保证内容对非收集方保密，身份为员工自报。员工更正时必须带本人上一次 `.yintian`，以沿用随机回执编号；否则会形成新记录。HR 明确要求“只有预先指定员工可提交”或“提交必须绑定工号”时，才使用兼容的 `group` 模式；该模式需要 `employee_id,name` 名单和逐人凭据。
