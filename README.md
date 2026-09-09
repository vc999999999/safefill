# SafeFill Skills

SafeFill 是一个面向多人私密资料收集的双 Skill 集合仓库。HR 通过对话生成统一模板，员工通过对话填写本人资料并得到加密回执，HR 收回后由 Agent 在授权环境中统一校验并导出 Excel。

> Fill locally. Share securely.

[查看双 Skill 协作流程图](https://vc999999999.github.io/safefill/flowchart.html)

## 两个 Skill

| Skill | 使用者 | 职责 | 说明 |
|---|---|---|---|
| `safefill-collect` | HR、行政等收集者 | 对话补齐需求、生成开放模板、收取加密回执并汇总 Excel | [README](skills/safefill-collect/README.md) · [SKILL.md](skills/safefill-collect/SKILL.md) |
| `safefill-fill` | 提交资料的员工 | 检查模板、对话补齐本人资料、确认并生成加密回执 | [README](skills/safefill-fill/README.md) · [SKILL.md](skills/safefill-fill/SKILL.md) |

`skills/` 下的两个目录都是独立 Skill。收集者和填写者可以在不同设备上只安装自己需要的一端；开发、回归测试和组合打包时，两个目录需保持同级。

## 默认使用方式：开放模板，对话完成

新任务默认采用 `open` 模式：HR 不准备员工名单、个人凭据、任务密码或配置文件，也不需要运行终端。Agent 只补问尚未说明的用途、字段、必填性、截止时间和联系人，然后生成一份 `FORM.yintian-form`。

员工把模板交给安装了 `safefill-fill` 的 Agent，在当前对话中提供本人的字段和明确指定的附件。Agent 做确定性校验并生成 `姓名-随机短码.yintian`；姓名在文件名中可见，文件载荷仍加密。员工本人把回执发送给 HR。HR 指出回执目录和 Excel 输出位置后，`safefill-collect` 完成收件、解密校验、最新版本选择以及 Excel/附件导出。

这意味着当前 Agent 会实际处理用户主动提供的明文。加密保护回执在传输和静态保存时的内容，不把明文对正在执行填写或汇总的 Agent 隐藏。若部署方不允许 Agent 接触明文，可继续使用兼容的本地保险柜、人工复核和白名单导出流程。

开放模板的身份是提交者自报，不能防止同名、冒名、模板转发或垃圾提交。只有 HR 明确要求预先限定人员或绑定工号时，才启用兼容的 `group` 名单凭据模式。`directed`、加密保险柜、OCR、本地证据窗口、加密任务交接和旧格式只读能力继续保留。

收集端和填写端的可选 OCR 均支持用 `YINTIAN_OCR_DEVICE` 选择 `CPU/GPU/NPU/AUTO`，并用 `YINTIAN_MODEL_DIR` 指向准备好的 INT8 模型目录。默认开放流程的附件不启用 OCR 绑定，除非 HR 明确要求原件和值自动比对。模型分工、授权回退和已验证范围见[识别说明](skills/safefill-collect/references/recognition.md)、[隐私边界](skills/safefill-collect/references/privacy-extraction-workflow.md)与[验证记录](skills/safefill-collect/references/validation.md)。

## 工作流程

```text
HR 说明用途、字段、期限和联系人
  → safefill-collect 生成统一 FORM.yintian-form
  → 员工用 safefill-fill 补齐本人资料并生成加密回执
  → 员工本人把回执发送给 HR
  → safefill-collect 校验并汇总 Excel 与附件目录
```

1. Agent 从对话提取需求，只合并询问缺项；`name` 自动作为必填字段，不向 HR 索取名单。
2. HR 发送统一模板；员工 Agent 只使用当前任务中明确提供的本人资料，并在员工确认后加密。
3. 首次回执生成随机记录编号；更正时必须携带本人上一次回执沿用编号，不能按姓名猜测覆盖。
4. 收集端不信任文件名，以解密后的姓名和字段为准；异常、待复核或非最新记录不进入 Excel。
5. 附件解密到 Excel 同名目录，单元格保存相对路径；明文输出只留给获授权的 HR。

## 安装

安装为 Agent Skill 时，请选择具体的 `skills/safefill-collect` 或 `skills/safefill-fill` 目录，不要把仓库根目录或 `skills/` 当成一个 Skill。每个 Skill 目录都应整体安装，不能只复制 `SKILL.md`，因为运行时还需要同目录下的脚本和参考文件。

以下命令只供 Skill 安装者或维护者使用，不是 HR/员工业务流程步骤。首次本地安装示例（macOS / Linux）：

```bash
git clone https://github.com/vc999999999/safefill.git
cd safefill

python3 -m venv skills/safefill-collect/.venv
skills/safefill-collect/.venv/bin/python -m pip install -r skills/safefill-collect/requirements.txt
skills/safefill-collect/.venv/bin/python skills/safefill-collect/scripts/collection.py doctor

python3 -m venv skills/safefill-fill/.venv
skills/safefill-fill/.venv/bin/python -m pip install -r skills/safefill-fill/requirements.txt
skills/safefill-fill/.venv/bin/python skills/safefill-fill/scripts/fill.py --help
```

Windows 中将 `.venv/bin/python` 换为 `.venv\Scripts\python.exe`。核心流程不强制安装 OCR 或配置 API Key；本地 OCR 只在需要时额外安装各端的 `requirements-ocr.txt`。

## 关键文件与边界

| 文件 | 用途 | 应留在哪里 |
|---|---|---|
| `FORM.yintian-form` / 定向表单 | 收集规则与收集方公钥 | 开放模板可统一发放；定向表单私发 |
| `*.yintian-credential` | 兼容 `group` 模式的个人认证凭据 | 只私下发给对应本人 |
| `*.yintian-vault` | 员工的加密个人保险柜 | 只留在员工设备 |
| `姓名-短码.yintian` | 本次加密提交；仅文件名显示姓名 | 由员工本人按授权渠道交回收集者 |
| `result.xlsx` / `result-attachments/` | 最新通过记录与解密附件 | 只留在获授权的 HR / 接收方 |

- 默认开放流程中，填写 Agent 和 HR Agent 会处理各自获授权的明文；不得扩大字段、扫描磁盘、猜测缺值、把他人明文贴进聊天或自动替员工发送回执。
- 开放任务私钥始终加密，本地随机密钥存放在任务目录之外的受限目录；跨设备仅使用加密任务包。兼容模式的密码不得放入聊天、命令参数或环境变量。
- OCR 结果只是候选，不代替填写者确认和 HR 复核。
- 仓库不保存真实名单、凭据、明文资料、附件、提交文件、保险柜、导出表格或项目外文稿。

更完整的操作与权限说明见[收集者文档](skills/safefill-collect/README.md)、[填写者文档](skills/safefill-fill/README.md)和[隐私边界](skills/safefill-collect/references/privacy-extraction-workflow.md)。

## 仓库结构

```text
safefill/
├── README.md
├── skills/                  # 可安装的 Skill 集合
│   ├── safefill-collect/       # 收集者 Skill，可独立安装
│   │   ├── SKILL.md         # Agent 入口与权限边界
│   │   ├── agents/          # Skill 界面元数据
│   │   ├── scripts/         # 收集、复核、导出与测试
│   │   ├── references/      # 配置、文件协议、操作与验证说明
│   │   └── assets/          # 邀请模板、MCP 示例与项目流程图
│   └── safefill-fill/        # 填写者 Skill，可独立安装
│       ├── SKILL.md
│       ├── agents/
│       ├── scripts/
│       └── references/
└── .github/workflows/      # 自动测试与项目流程图发布
```

## 开发与验证

在仓库根目录运行：

```bash
python -m pip install -r skills/safefill-collect/requirements-dev.txt
python -m pytest skills/safefill-collect/scripts -q -p no:cacheprovider
node skills/safefill-collect/tests/browser_contract.cjs
python skills/safefill-collect/scripts/package_skill.py --out /absolute/path/safefill-skills.zip
```

打包脚本使用白名单，生成的交付包顶层只包含 `safefill-collect/` 和 `safefill-fill/`。当前回归为 168 项测试及 17 项子用例；已验证范围与未验证项见 [validation.md](skills/safefill-collect/references/validation.md)。

现有 `.yintian*` 扩展名和格式标识作为兼容文件协议保留，不影响 SafeFill Skill 的安装与使用。
