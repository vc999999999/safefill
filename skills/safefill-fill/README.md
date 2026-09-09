# SafeFill · 填写者 4.3.0

员工把 `REQUEST.yintian-request` 交给 Agent。Agent 直接读取 JSON 请求包并说明用途和字段，优先从本机加密保险柜匹配取值：首次使用在对话中收集基础资料完成 `vault-init`，缺项 `vault-add` 补录，也可用 `vault-scan` 通过 OpenVINO 本地识别员工指定的证件图（可选 VLM，不联网、无需 API Key），候选经本人确认后写入。员工确认取值与映射后 `vault-fill` 生成 `.yintian` 加密回执，由员工本人发送给 HR。下次收集同类信息零重复提问。

Agent 内部命令：

```bash
python scripts/fill.py inspect REQUEST.yintian-request --json
python scripts/fill.py vault-status --request REQUEST.yintian-request
python scripts/fill.py vault-init --answers TEMP.json   # 仅首次；0700 目录 + 0600 文件，用后自动删除
python scripts/fill.py vault-add --answers TEMP.json    # 缺项补录，同一纪律
python scripts/fill.py vault-fill REQUEST.yintian-request [--mapping MAP.json] --out-dir OUTPUT_DIR --confirmed
```

保险柜静态加密存放于 `data/`（目录 0700，密钥与保险柜均 0600），仅本机本账户可解，不随 skill 分发。输出回执名为 `姓名-随机短码.yintian`；姓名可见，文件内容仍对收集方端到端加密。更正时追加 `--previous 本人上一次回执.yintian`，以替换同一随机回执编号的最新版本。无保险柜时回退对话流 `submit`（0600 临时 answers JSON，用后删除）。
