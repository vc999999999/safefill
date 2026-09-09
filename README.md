# SafeFill Skills

SafeFill 是一个面向多人私密资料收集的双 Skill 集合仓库。收集者发布统一表单，填写者在本人设备上取值并加密，HR 只在获授权的本地环境中复核和导出 Excel。

> Fill locally. Share securely.

[查看双 Skill 协作流程图](https://vc999999999.github.io/safefill/flowchart.html)

## 两个 Skill

| Skill | 使用者 | 职责 | 说明 |
|---|---|---|---|
| `safefill-collect` | HR、行政等收集者 | 建任务、分发、加密收件、进度跟踪、本地复核与汇总 | [README](skills/safefill-collect/README.md) · [SKILL.md](skills/safefill-collect/SKILL.md) |
| `safefill-fill` | 提交资料的员工 | 检查表单、从本地保险柜取值、本人确认并生成加密提交 | [README](skills/safefill-fill/README.md) · [SKILL.md](skills/safefill-fill/SKILL.md) |

`skills/` 下的两个目录都是独立 Skill。收集者和填写者可以在不同设备上只安装自己需要的一端；开发、回归测试和组合打包时，两个目录需保持同级。

## 工作流程

```text
收集者 safefill-collect
  │
  ├─ 群发：FORM.yintian-form + 私发每人的 .yintian-credential
  └─ 定向：每人的独立表单
              │
              ▼
填写者 safefill-fill
  └─ 本地取值、本人确认、生成 reply.yintian
              │
              ▼
收集者 safefill-collect
  └─ 认证收件、版本跟踪、本地复核、按白名单导出 Excel
```

1. Agent 根据用途、字段、名单和期限准备任务配置。
2. HR 本人在未被 Agent 控制或录制的终端创建任务。
3. 员工用 `safefill-fill` 检查表单，在本人终端解锁保险柜并生成 `.yintian` 密文。
4. Agent 可协助收件、查看状态和生成最小化进度报告，但不取得解密权限。
5. HR 本人在本地复核，并仅导出当前最新且已通过的记录。

## 安装

安装为 Agent Skill 时，请选择具体的 `skills/safefill-collect` 或 `skills/safefill-fill` 目录，不要把仓库根目录或 `skills/` 当成一个 Skill。每个 Skill 目录都应整体安装，不能只复制 `SKILL.md`，因为运行时还需要同目录下的脚本和参考文件。

首次本地安装示例（macOS / Linux）：

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
| `FORM.yintian-form` / 定向表单 | 收集规则与收集方公钥 | 按任务模式公开或定向发放 |
| `*.yintian-credential` | 群发模式的个人认证凭据 | 只私下发给对应本人 |
| `*.yintian-vault` | 员工的加密个人保险柜 | 只留在员工设备 |
| `*.yintian` | 本次加密提交 | 按授权渠道交回收集者 |
| `result.xlsx` | 通过复核后的白名单导出 | 只留在获授权的 HR / 接收方 |

- Agent 不获取密码、私钥、保险柜内容、表单明文或明文 Excel；只有填写者明确授权时，宿主才可处理其指定的原始附件并生成 OCR 候选。
- 密码不得放入聊天、命令参数或环境变量。
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

打包脚本使用白名单，生成的交付包顶层只包含 `safefill-collect/` 和 `safefill-fill/`。已验证范围与未验证项见 [validation.md](skills/safefill-collect/references/validation.md)。

现有 `.yintian*` 扩展名和格式标识作为兼容文件协议保留，不影响 SafeFill Skill 的安装与使用。
