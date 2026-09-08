# 收集配置与名单格式

## 名单 CSV

名单必须包含 `employee_id,name` 两列，工号在任务内唯一：

```csv
employee_id,name
E001,张三
E002,李四
```

## 收集配置

`collection.py init-config` 生成模板后必须逐项改为真实、具体的内容：

- `purpose`：收集用途；
- `deadline`：截止时间，过期提交只标记为 late，不拒收；
- `retention_until`：保存期限，到期后任务停止接收、复核、报告、导出和查看；
- `contact`：联系人；
- `correction`：更正方式。

配置和告知文案会计算哈希并绑定进每份提交；提交后修改配置不会溯及已收密文，只会触发复核标记。

## 创建模式（--mode directed|group）

`create` 支持双模式，两种模式的加密信封、复核流程和报告格式完全一致，区别只在身份绑定强度：

| 维度 | directed 定向型（默认） | group 群发型 |
| --- | --- | --- |
| 认证令牌 | 每人独立随机令牌（库中仅存 SHA-256，恒定时间比对） | 无令牌 |
| 邀请产物 | 每人一份 `INV-*.html` + `INV-*.yintian-form` JSON | 全员共用单份 `FORM.yintian-form` JSON |
| 提交标识 | `invite_id = INV-...` | `invite_id = GRP-<employee_id>`，版本上限按工号计数 |
| 身份绑定 | 令牌绑定到个人，冒名提交无法通过令牌校验 | 无令牌绑定，任何拿到表单的人可为任何工号提交；靠复核时名单比对标记 + 人工复核兜底 |
| 适用场景 | 身份证号、证件影像等高敏感定向收集 | 低敏感、群公告一次性分发、追求最小操作成本 |
| 风险声明 | 私聊送达是工作流约定，非密码学身份认证 | 必须向用户如实声明：无令牌、可冒名，依赖名单比对兜底；高敏感场景应改用 directed |

group 模式的 `FORM.yintian-form` 不含任何个人标识与令牌，可原样转发；directed 模式的邀请文件按 `invite-index.csv` 逐人私聊发送，不得串发。

## 字段类型

内置模板收集姓名、手机号、身份证号、住址和身份证正反面照片。自定义字段只允许确定性校验类型：

| 类型 | 说明 |
| --- | --- |
| `text` | 自由文本 |
| `phone_cn` | 中国大陆手机号，归一化后正则校验 |
| `cn_id` | 身份证号，按 GB 11643 校验日期和校验码 |
| `date` | `YYYY-MM-DD`，严格日期校验 |
| `address` | 住址，多行文本 |
| `single_choice` | 单选，需提供 `options` |
| `image_attachment` | JPG/PNG/WebP 图片附件 |
| `pdf_attachment` | PDF 附件，扫描件会渲染后 OCR |

附件单文件最大 5 MB、每份提交合计最大 15 MB、`.yintian` 信封最大 32 MB，PDF 最多渲染 20 页。字段 id 必须匹配 `^[a-z][a-z0-9_]{1,63}$`，且必须保留 `name` 字段。

## OCR 交叉校验的字段匹配

复核时按字段**类型**而非 id 匹配 OCR 证据：`cn_id` 对证件号、`phone_cn` 对手机号、`address` 对住址、`name` 对姓名；附件字段 id 按下划线分词后含整词 `front`/`back`（如 `id_front`、`id_card_back`、`front`）才视为证件正反面，`backdrop`、`feedback_scan` 这类仅含子串的 id 不触发证件 OCR 比对。自定义字段 id 不影响比对。OCR 与手填值冲突只进入人工复核，永不自动覆盖。
