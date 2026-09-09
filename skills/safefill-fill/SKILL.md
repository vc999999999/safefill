---
name: safefill-fill
description: 员工收到 SafeFill 机器信息请求包后，由 Agent 读取字段、从本机加密保险柜匹配取值（首次初始化、缺项补录、可选 OpenVINO 本地识别证件）并生成加密 .yintian 回执。请求包不是网页表单；只负责本人填写，不处理 HR 汇总。
metadata:
  compatibility: "Python 3.11+；requirements-core.txt；读取 yintian-request/1，兼容旧表单协议。可选：requirements-ocr.txt（本地 OCR）、requirements-vlm.txt（本地 VLM，独立 venv）。"
---

# SafeFill · 填写者

目标：员工把 `REQUEST.yintian-request` 交给 Agent 后，只需在对话中确认取值；资料沉淀在本机加密保险柜，下次收集零重复提问。回执由员工本人发送。

以本文件目录为 `SKILL_ROOT`，由 Agent 使用已安装依赖的 Python 绝对路径运行脚本。

## 默认流程（保险柜优先）

1. Agent 运行 `inspect --json` 读取并校验请求包。不得在浏览器中打开、渲染为 HTML、生成可填写页面，也不得让员工手工查看或编辑 JSON。向员工用自然语言说明收集方、用途、截止时间及所需字段。
2. 运行 `vault-status --request REQUEST`：有保险柜时得到每个请求字段的 `matched/missing` 预览（不输出条目值）；没有时 `vault: false`。
3. **首次使用（无保险柜）**：Agent 在对话中向员工收集通用基础条目（姓名、手机号、身份证号、住址等；也可由员工明确指定证件图后用 `vault-scan` 本地识别出候选，员工确认后再写入）。把员工确认的值写成 `0700` 目录中的 `0600` 临时 JSON，运行 `vault-init`；无论成败临时文件都会被删除。可选地用 `vault-scan --vlm` 走本地 VLM（需先 `vlm-setup`）。
4. **缺项**：`vault-status` 标记 missing 的字段（含必填），只问这些缺项；员工确认后用 `vault-add` 以同样的临时 JSON 纪律写入，下次收集不再问。
5. **匹配与确认**：Agent 依据 `vault-status` 的 match 结果生成映射（请求字段 id → 保险柜条目 id，仅 auto-match 未覆盖时需要），把每个字段的取值来源和值在对话中列给员工确认。确认后运行 `vault-fill --confirmed` 生成 `姓名-随机短码.yintian`。临时映射 JSON 用后删除。
6. 把加密回执交给员工，不自动发送。若是更正，使用 `--previous 本人上一次回执.yintian` 沿用记录编号；没有旧回执时明确说明会形成新记录。

Agent 内部入口：

```bash
"$PYTHON" "$SKILL_ROOT/scripts/fill.py" inspect REQUEST.yintian-request --json
"$PYTHON" "$SKILL_ROOT/scripts/fill.py" vault-status --request REQUEST.yintian-request
"$PYTHON" "$SKILL_ROOT/scripts/fill.py" vault-scan 证件图... [--vlm]
"$PYTHON" "$SKILL_ROOT/scripts/fill.py" vault-init --answers TEMP.json   # 仅首次
"$PYTHON" "$SKILL_ROOT/scripts/fill.py" vault-add --answers TEMP.json    # 缺项补录
"$PYTHON" "$SKILL_ROOT/scripts/fill.py" vault-fill REQUEST.yintian-request [--mapping MAP.json] --out-dir OUTPUT_DIR --confirmed
# 更正时追加：--previous PREVIOUS.yintian；无保险柜时回退：submit（见下）
```

临时 JSON 结构（vault-init / vault-add 通用）：

```json
{"entries": {"phone": {"type": "phone_cn", "value": "13800138000"}, "cn_id": {"type": "cn_id", "value": "..."}, "id_card": {"type": "image_attachment", "paths": ["/指定/证件.png"], "source": {"kind": "openvino-ocr", "sha256": "..."}}}}
```

## 边界

- 保险柜（`SKILL_ROOT/data/`，目录 `0700`）静态加密，`vault.key` 与 `vault.yintian-vault` 均为 `0600`，仅本机本账户可解；权限不符时脚本拒绝运行。保险柜与密钥永远不发给任何人、不复制到对话或公共目录。
- 保险柜条目值只来自员工在本次对话中明确提供或确认的内容；`vault-scan` 的本地识别结果只是候选，必须经员工本人确认后才 `vault-add`，source 记录 `manual/openvino-ocr/openvino-vlm` 与原件 sha256。
- 只处理员工明确指定的证件文件，不扫描磁盘寻找证件。OCR/VLM 均为本地 OpenVINO 推理，不联网、不需要 API Key；识别前向员工说明图片由本机模型处理。
- `vault-fill` 的取值与映射必须先在对话中向员工逐项展示并获确认，才加 `--confirmed`。缺必填、格式错误、请求包过期或公钥指纹不一致时不生成回执，只补可修正项。
- 文件名显示员工填写的姓名和防重名短码；不要在文件名加入身份证号、手机号等其他资料。
- 更正必须使用本人上一次 `.yintian`；不要按姓名猜测旧记录，也不要使用他人的回执。没有旧回执时生成的是新记录，并明确告诉员工联系 HR 处理重复项。
- Agent 已参与处理员工提供的明文；保险柜保护静态数据，回执端到端加密保护交回 HR 的文件。若员工不希望当前 Agent 处理明文，应停止本流程，改用 `vault-edit` 等终端人工通道。
- 无保险柜或员工拒绝建柜时回退对话流：`submit` 的 0600 临时 answers JSON 结构为 `{"consent_confirmed":true,"values":{...},"attachments":{}}`，纪律相同。
- 生成后提示员工只发送 `.yintian`，不要发送临时 JSON、保险柜、密钥或他人凭据；本人回执至少保留到 HR 确认收件，更正时需要它。
- 默认请求包不需要 `.yintian-credential`。旧 `group` 表单仍兼容个人凭据，但不要把旧流程用于新的普通收集。
