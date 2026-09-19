# SafeFill · 收集者 5.0.0

HR 在对话中说明用途、字段、截止时间和联系人，Agent 生成无需名单的 `REQUEST-{任务编号}.yintian-request`（文件名带任务编号，防止多任务同名混淆）。这是给 `safefill-fill` Agent 读取的 JSON 请求包，不是让员工填写的表单。SafeFill 任何模式都不生成 HTML；员工只与 Agent 对话，HR 收到加密回执后由 Agent 汇总 Excel。

Agent 内部命令：

```bash
python scripts/collector.py create-request --config collection.json --out tasks
python scripts/collector.py list-tasks tasks            # 找回任务目录
python scripts/collector.py status TASK_DIR             # 只读进度：回执编号/版本/状态/期限
python scripts/collector.py ingest TASK_DIR INCOMING_DIR [--recursive]
python scripts/collector.py collect TASK_DIR INCOMING_DIR --out result.xlsx [--recursive]
python scripts/collector.py notice TASK_DIR INVITE_ID --out NOTICE.yintian-notice   # 退回/补正通知
```

HR 不需要准备姓名名单、网页表单或运行终端（OCR 裁定 `decide`、交接 `export-task`/`import-task`、销毁 `purge` 除外，需 HR 本人交互终端）。必须原样发送请求包，并建议把 `task_id` 尾 6 位公示给员工做带外核对。字段 id 使用 [标准字段 id](references/collection-config.md)，截止/保存时间必须带时区。`create-request` 返回保存期限提醒（到期后不再解密）与启用了附件比对的字段；`collect` 返回逐人排除原因（含完整回执编号、版本与可直接运行的 `decide_command`）、迟交人数与同名多行提醒，Agent 直接概述给 HR。Excel 第二列固定为回执编号供对账。回执文件名显示员工填写的姓名，收集端仍以解密后的姓名为准；需要强身份认证时应采用独立认证渠道。
