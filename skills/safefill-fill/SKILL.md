---
name: safefill-fill
description: 员工收到 SafeFill 机器信息请求包后，由 Agent 读取字段、通过对话补齐本人资料并生成加密 `.yintian` 回执。请求包不是网页表单；只负责本人填写，不处理 HR 汇总。
metadata:
  compatibility: "Python 3.11+；requirements-core.txt；读取 yintian-request/1，兼容旧表单协议。"
---

# SafeFill · 填写者

目标：员工把 `REQUEST.yintian-request` 交给 Agent 后，只需在对话中提供本人资料；Agent 读取字段、校验并加密，员工本人发送回执。

以本文件目录为 `SKILL_ROOT`，由 Agent 使用已安装依赖的 Python 绝对路径运行脚本。

## 默认流程

1. Agent 直接运行 `inspect --json` 读取并校验 `REQUEST.yintian-request`。不得在浏览器中打开、渲染为 HTML、生成可填写页面，也不得让员工手工查看或编辑 JSON。向员工用自然语言说明收集方、用途、截止时间及所需字段。
2. 从当前对话提取员工已提供的本人值和明确指定的附件；按请求包字段顺序一次列出所有缺项，单选字段同时给出合法选项。只问缺项，不重复询问已有内容；不猜测、不编造、不扫描磁盘找证件。
3. 在员工确认本次内容后，Agent 在 `0700` 临时目录中直接创建权限为 `0600` 的 answers JSON，运行 `submit` 生成 `姓名-随机短码.yintian`。无论成功失败都删除临时明文和空目录；不得先在公共目录生成再修改权限。
4. 把加密回执交给员工，不自动发送。若是更正，使用 `--previous 本人上一次回执.yintian` 沿用记录编号；没有旧回执时明确说明会形成新记录。

Agent 内部入口：

```bash
"$PYTHON" "$SKILL_ROOT/scripts/fill.py" inspect REQUEST.yintian-request --json
"$PYTHON" "$SKILL_ROOT/scripts/fill.py" submit REQUEST.yintian-request --answers TEMP.json --out-dir OUTPUT_DIR
# 更正时在上行追加：--previous PREVIOUS.yintian
```

临时 JSON 结构：

```json
{"consent_confirmed":true,"values":{"name":"员工填写的姓名"},"attachments":{}}
```

## 边界

- `submit` 的明文只来自员工在当前任务中明确提供的值；不要从其他对话、他人资料或未指定文件补全。
- 输出只包含请求包指定的字段和附件。缺必填、格式错误、请求包过期或公钥指纹不一致时不生成回执，并只询问可修正项。
- 默认请求包不需要 `.yintian-credential`。旧 `group` 表单仍兼容个人凭据，但不要把旧流程用于新的普通收集。
- 文件名显示员工填写的姓名和防重名短码；不要在文件名加入身份证号、手机号等其他资料。
- 更正必须使用本人上一次 `.yintian`；不要按姓名猜测旧记录，也不要使用他人的回执。没有旧回执时生成的是新记录，并明确告诉员工联系 HR 处理重复项。
- Agent 已参与处理员工提供的明文；加密保护的是交回 HR 的文件。若员工不希望当前 Agent 处理明文，应停止本流程，改用离线人工工具。
- 生成后提示员工只发送 `.yintian`，不要发送请求包的临时 answers JSON、原始保险柜或其他人的凭据；本人回执至少保留到 HR 确认收件，更正时需要它。
