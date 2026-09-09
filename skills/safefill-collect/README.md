# SafeFill · 收集者 4.1.0

HR 在对话中说明用途、字段、截止时间和联系人，Agent 生成一份无需名单的 `FORM.yintian-form`。员工用 `safefill-fill` 填写并生成加密回执；HR 收到回执后，Agent 一次完成收件、校验、解密和 Excel 汇总。

Agent 内部命令：

```bash
python scripts/collection.py create-open --config collection.json --out tasks
python scripts/collection.py collect TASK_DIR INCOMING_DIR --out result.xlsx
```

HR 不需要准备姓名名单、个人凭据、任务密码或运行终端。回执文件名显示员工填写的姓名，收集端仍以解密后的姓名为准；附件解密到 Excel 同名目录并以相对路径引用。开放任务私钥保持加密，本地密钥不放进任务目录。开放模板使用自报身份；需要提交者认证时仍可使用兼容的 `group` 模式。
