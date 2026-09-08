> 3.5.0：默认安装不再要求 RapidOCR/OpenVINO。新模板支持本地优先 → 本人授权的宿主 Agent 候选 → 本地人工复核；无需独立 API Key。详见 [可选识别与权限边界](references/recognition.md)。历史 3.4.0 评测和文章按原版本保留。

# 隐填 3.5.0

**群内统一发模板，员工从自己的加密保险柜填写，授权 HR 汇总 Excel，同事看不到彼此的填写内容。**

| Skill | 工作 |
|---|---|
| `yintian-skill` 收集者 | 准备模板、组织分发、收件、进度与本地复核/Excel 交接 |
| `yintian-fill` 填写者 | 解释模板、引导解锁保险柜、匹配本次字段、本人确认后加密 |

公共模板发群，个人凭据私下发放，只交回密文。凭据认证持有者，不证明证件真实性或阻止本人转借。

## 安装

Python 3.11+，建议独立虚拟环境：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-core.txt
.venv/bin/python scripts/collection.py doctor
```

Windows 对应解释器为 `.venv\Scripts\python.exe`。核心依赖包括加密、Excel、MCP、图片/PDF；`requirements-ocr.txt` 按需安装 OCR，`requirements.txt` 同样安装核心依赖，开发使用 `requirements-dev.txt`。Tk 由 Python/操作系统提供，不能用 pip 安装 tkinter 替代；`doctor` 检查可用性。

源码中使用根目录与 `yintian-fill/`。发布包的填写端附带公共模块，可单独安装。

## 闭环

1. HR 提供用途、字段、期限及 `employee_id,name` 名单。Agent 用 init-config --mode group 准备配置，HR 本人创建：
   ```bash
   python scripts/collection.py create --roster roster.csv --config collection.json --out tasks --mode group
   ```
2. 群内只发 `FORM.yintian-form`，`credentials/` 内个人凭据私下给对应员工；独立核对任务编号与指纹。发送由宿主工具在用户授权下完成，或人工完成；本地脚本不自动发消息。
3. 员工本人：
   ```bash
   python yintian-fill/scripts/fill.py vault-init FORM.yintian-form --vault personal.yintian-vault
   python yintian-fill/scripts/fill.py fill FORM.yintian-form --vault personal.yintian-vault --credential PERSONAL.yintian-credential --out reply.yintian
   ```
4. Agent 接收并报告状态：
   ```bash
   python scripts/collection.py ingest TASK INCOMING
   python scripts/collection.py status TASK
   python scripts/collection.py report TASK
   ```
5. HR 本人复核和汇总：
   ```bash
   python scripts/collection.py review TASK
   python scripts/collection.py export-clear TASK --out result.xlsx --fields phone,id_number,address --purpose "办理保险" --recipient "授权接收方"
   ```

每人只导出最新已通过版本。待处理与最近通过版本分别显示。人工 OCR 复核、退回、重试、迁移与交接见 [本地操作](references/operator.md)。

## Prompt 与 Python

Prompt 负责判断场景、解释步骤、选择工具、沿用用户授权和交接敏感操作。Python 负责身份认证、保险柜取值、本人确认、字段校验、加密、状态、锁和安全输出。具体对照见 [职责与交付](references/engineering.md)。

Agent/MCP 没有解密和人工放行接口。TTY 与文件权限不是同账号恶意进程的强隔离；明文 Excel 后续传播由授权人管理。

## 验证

```bash
python scripts/run_tests.py
python scripts/package_skill.py --out dist/yintian-3.5.0.zip
```

测试使用合成人员、实际加密/SQLite/XLSX；OCR 规则用受控输出验证，不代表真实证件准确率、Intel GPU/NPU 或跨平台认证。旧定向 v2 可用，写入前显式迁移数据库；旧无认证群发只读。

[协议](references/form-format.md) · [隐私边界](references/privacy-extraction-workflow.md) · [配置](references/collection-config.md)

本次 macOS/Python 3.11 的受测依赖可用 requirements-tested-py311.txt 在独立环境复现；它不包含 OCR，也不代表其他平台的依赖锁。

[3.5.0 流程与测试证据](references/validation-3.5.0.md)
