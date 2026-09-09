# SafeFill · 填写者 4.2.0

员工把 `REQUEST.yintian-request` 交给 Agent。Agent 直接读取 JSON 请求包，用自然语言说明用途和字段、一次问齐缺项，然后生成 `.yintian` 加密回执。员工不打开文件、不填写网页、不运行终端，也不需要个人凭据；回执由员工本人发送给 HR。

Agent 内部命令：

```bash
python scripts/fill.py inspect REQUEST.yintian-request --json
python scripts/fill.py submit REQUEST.yintian-request --answers TEMP.json --out-dir OUTPUT_DIR
```

`TEMP.json` 只用于本次加密，必须从 `0700` 临时目录中以 `0600` 创建，完成或失败后都删除。输出名为 `姓名-随机短码.yintian`；姓名可见，文件内容仍加密。更正时追加 `--previous 本人上一次回执.yintian`，以替换同一随机回执编号的最新版本。当前 Agent 会处理员工在对话中提供的明文。
