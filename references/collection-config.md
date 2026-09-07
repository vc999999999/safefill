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

复核时按字段**类型**而非 id 匹配 OCR 证据：`cn_id` 对证件号、`phone_cn` 对手机号、`address` 对住址、`name` 对姓名；含 `front`/`back` 语义的附件字段视为证件正反面。自定义字段 id 不影响比对。OCR 与手填值冲突只进入人工复核，永不自动覆盖。
