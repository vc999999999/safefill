# SafeFill · 收集者 4.2.1

HR 在对话中说明用途、字段、截止时间和联系人，Agent 生成无需名单的 `REQUEST.yintian-request`。这是给 `safefill-fill` Agent 读取的 JSON 请求包，不是让员工填写的表单。SafeFill 任何模式都不生成 HTML；员工只与 Agent 对话，HR 收到加密回执后由 Agent 汇总 Excel。

Agent 内部命令：

```bash
python scripts/collection.py create-request --config collection.json --out tasks
python scripts/collection.py collect TASK_DIR INCOMING_DIR --out result.xlsx
```

HR 不需要准备姓名名单、个人凭据、任务密码、网页表单或运行终端。必须原样发送请求包。回执文件名显示员工填写的姓名，收集端仍以解密后的姓名为准；开放请求使用自报身份，需要提交者认证时才使用兼容的 `group` 模式。
