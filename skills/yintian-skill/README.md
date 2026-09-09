# 隐填 · 收集者 3.5.0

群里统一发模板，员工各自加密交回，授权 HR 在本地复核并整理 Excel。对应的填写者是同级目录 yintian-fill；本文件夹本身可独立安装。

[双 Skill 协作全景](https://vc999999999.github.io/yintian/flowchart.html)：交互查看主流程、识别回退、数据权限和工程分工。页面源码为 assets/flowchart.html，由 GitHub Actions 发布，保持项目顶层布局不变。

## 安装

在本文件夹中运行，要求 Python 3.11+：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/collection.py doctor
```

Windows 使用 .venv\Scripts\python.exe。核心功能无需 RapidOCR/OpenVINO 或 API Key；如需本地 OCR，额外安装 requirements-ocr.txt。Tk 由 Python/操作系统提供。

## 工作流

1. Agent 根据用途、名单、字段和期限准备配置；HR 本人创建任务。
2. FORM.yintian-form 发群，个人 .yintian-credential 私下给对应员工；员工独立核对公钥。
3. 填写者 Skill 从本地保险柜取本次字段，本人确认后交回 .yintian 密文。
4. Agent 收件与报告状态；HR 本人复核，按字段白名单生成 Excel。

```bash
python scripts/collection.py init-config --mode group --out collection.json
python scripts/collection.py create --mode group --roster roster.csv --config collection.json --out tasks
python scripts/collection.py ingest TASK INCOMING
python scripts/collection.py status TASK
python scripts/collection.py report TASK
python scripts/collection.py review TASK
python scripts/collection.py export-clear TASK --out result.xlsx --fields phone,id_number,address --purpose "办理保险" --recipient "授权接收方"
```

create、review 和明文导出等敏感操作由本人终端执行；Agent/MCP 不取得解密或人工放行权限。新模板默认本地 OCR 优先，可使用员工授权的宿主 Agent 候选或转人工核对。最新版本未通过时不将旧版本放进当前 Excel。

[配置](references/collection-config.md) · [本人操作](references/operator.md) · [识别分工](references/recognition.md) · [文件协议](references/form-format.md) · [隐私边界](references/privacy-extraction-workflow.md)

## 维护与验证

scripts 内保留两端自动化回归，tests 内保留合成流程和浏览器测试；实际运行产物放在项目之外。两端公共模块随各自 Skill 附带，打包时校验一致，不依赖另一文件夹才能运行。

```bash
python -m pip install -r requirements-dev.txt
python -m pytest scripts -q -p no:cacheprovider
python scripts/run_tests.py
node tests/browser_contract.cjs
python scripts/package_skill.py --out /绝对路径/交付包.zip
```

两端测试和组合打包需要两个 Skill 同级存放。测试只用合成数据；[已验证范围](references/validation.md) 不包含 Intel 硬件成绩或真实证件准确率。
