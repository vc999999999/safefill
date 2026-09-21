# SafeFill · 收集者 6.0.0

HR 说明用途、字段、截止时间和联系人，Agent 生成请求包；员工回执经签名验证与修订选择后，脚本在本机导出 Excel/附件。命令输出只含回执编号、计数、状态与路径，不含员工姓名、字段值或识别内容。

Agent 指令见 [SKILL.md](SKILL.md)，格式与边界见 [PROTOCOL.md](references/PROTOCOL.md)，字段类型、标准 ID 与配置示例见 [collection-config.md](references/collection-config.md)。安装整个 Skill 目录即可，不依赖填写端目录或仓库根文件。

核心环境 Python 3.11–3.13，在 Skill 目录内：

```bash
python -m pip install -r requirements.txt
python scripts/collector.py doctor
python scripts/collector.py create-request --config collection.json --out tasks
python scripts/collector.py collect TASK_DIR INCOMING_DIR --out result.xlsx
```

- 任务总目录可放 `wiki.md`，供 Agent 读取业务背景与字段说明。
- OCR 比对默认关闭；启用后异常需 HR 本人在本地图形环境 `decide` 裁定，OCR 依赖需 Python 3.11 与 `requirements-ocr.txt`。
- `decide`、`export-task`、`import-task`、`purge` 需 HR 本人在交互终端运行。
