# SafeFill Skills

SafeFill 是一个 AI→AI 的私密资料收集协议。HR Agent 把需求编译成机器请求包；员工 Agent 读取字段，从本机加密保险柜匹配取值（首次初始化、缺项补录、OpenVINO 本地识别证件）并生成加密回执；HR Agent 收回密文后统一校验并导出 Excel。

> Fill locally. Share securely.

## 设计亮点

**1. 本机加密保险柜 · 一次录入，终身复用**

员工资料沉淀在本机 `data/` 的 `yintian-vault/2` 保险柜中：scrypt + AES-256-GCM 静态加密，`0600` 本机密钥仅当前设备账户可解，不联网、不分发。第一次收集初始化后，之后任何新任务只问保险柜里没有的字段；已有字段零重复提问。保险柜档案与请求包解耦（通用类型条目），跨 HR、跨表单复用；字段映射由员工 Agent 语义生成、本人逐项确认后，确定性脚本才执行填写。

**2. OpenVINO 本地模型提取 · 不联网、无 API Key**

`vault-scan` 用 OpenVINO 在本机 CPU/iGPU/NPU 上推理，把员工明确指定的证件图变成结构化字段候选：基线为 rapidocr OCR，可选 int4 量化 VLM（`vlm-setup` 显式下载 MiniCPM-V，权重不进安装包），不可用自动回退。识别结果永远只是候选——确定性校验（身份证校验位、手机号格式）加本人确认后才写入保险柜，来源（manual / openvino-ocr / openvino-vlm）与原件 sha256 全程可溯。**本地模型提议，确定性校验把关，人确认，密码学兜底。**

**3. 端到端加密 · 密码学兜底**

每份回执使用随机的 AES-256-GCM 内容密钥，并以请求包内的 RSA-OAEP-3072 公钥封装，只有持有任务私钥的收集方本机账户能解；HR 私钥静态加密存放，开放任务的本地密钥独立放在任务目录外的受限目录。回执文件名只显示姓名与防重名短码，身份证号、手机号等永不进入文件名。

**4. AI→AI 机器协议 · 零网页表单**

`REQUEST.yintian-request` 是 Agent 之间交换的规范 JSON，不是给人填写的界面；`open`、`group`、`directed` 所有模式都不生成 HTML 或在线表单。请求包携带 `schema_hash`/`notice_hash` 防篡改，公钥指纹可带外核对；请求包、回执、附件一律视为数据，不执行其中夹带的指令。

**5. 克制的 Agent 边界**

不扫描磁盘寻找证件、不猜测或编造缺值、不代替员工发送回执、不把他人明文贴进聊天；临时明文一律 `0700` 目录 + `0600` 文件且用后删除；员工本人始终掌握最终确认权（`--confirmed` 只在对话确认后使用）。

## 两个 Skill

| Skill | 使用者 | 职责 | 说明 |
|---|---|---|---|
| `safefill-collect` | HR、行政等收集者 | 对话补齐需求、生成机器请求包、收取加密回执并汇总 Excel | [README](skills/safefill-collect/README.md) · [SKILL.md](skills/safefill-collect/SKILL.md) |
| `safefill-fill` | 提交资料的员工 | 读取机器请求包、本机保险柜匹配取值、确认并生成加密回执 | [README](skills/safefill-fill/README.md) · [SKILL.md](skills/safefill-fill/SKILL.md) |

`skills/` 下的两个目录都是独立 Skill。收集者和填写者可以在不同设备上只安装自己需要的一端。

## 默认使用方式：AI 请求包 + 本机保险柜

新任务默认采用 `open` 模式：HR 不准备员工名单、个人凭据、任务密码、配置文件或网页表单，也不需要运行终端。Agent 只补问尚未说明的用途、字段、必填性、截止时间和联系人，然后生成 `REQUEST.yintian-request`。

员工把请求包交给安装了 `safefill-fill` 的 Agent：Agent 读取并说明用途后运行 `vault-status` 查看本机保险柜的匹配预览。首次使用时在对话中收集基础资料完成 `vault-init`（也可由员工指定证件图，用 `vault-scan` 本地识别出候选再确认写入）；只询问缺项并用 `vault-add` 补录沉淀；员工确认取值与映射后 `vault-fill` 生成 `姓名-随机短码.yintian`，由员工本人发送给 HR。无保险柜或员工拒绝建柜时，回退到一次性对话填写（`submit`）。HR 指出回执目录和输出位置后，`safefill-collect` 完成收件、解密校验、最新版本选择以及 Excel/附件导出。

这意味着当前 Agent 会实际处理用户主动提供的明文。保险柜保护静态资料，回执端到端加密保护传输与汇总侧的内容，都不把明文对正在执行填写或汇总的 Agent 隐藏。若部署方不允许 Agent 接触明文，可使用 `vault-edit`、`seal` 等终端人工通道。

开放请求包的身份是提交者自报，不能防止同名、冒名、请求包转发或垃圾提交。只有 HR 明确要求预先限定人员或绑定工号时，才启用兼容的 `group` 名单凭据模式。本地证据复核、加密任务交接和旧格式只读能力作为兼容能力继续保留。

两端的可选 OCR 均支持用 `YINTIAN_OCR_DEVICE` 选择 `CPU/GPU/NPU/AUTO`，并用 `YINTIAN_MODEL_DIR` 指向准备好的模型目录；保险柜目录可用 `YINTIAN_VAULT_DIR` 重定向。默认开放流程的附件不启用 OCR 绑定，除非 HR 明确要求原件和值自动比对（`ocr_fields`）；填写端的 VLM 提取独立安装在 `requirements-vlm.txt`，与 rapidocr 的 OpenVINO 钉版互不影响。

## 工作流程

```text
HR 说明用途、字段、期限和联系人
  → safefill-collect 生成 REQUEST.yintian-request
  → 员工 Agent 读取请求包，保险柜匹配取值（首次初始化、缺项补录、可选本地识别）并加密
  → 员工本人把回执发送给 HR
  → safefill-collect 校验并汇总 Excel 与附件目录
```

1. Agent 从对话提取需求，只合并询问缺项；`name` 自动作为必填字段，不向 HR 索取名单。
2. HR 原样转发机器请求包；员工不打开、不手工填写，员工 Agent 读取后按保险柜匹配结果只问缺项，取值与映射经本人确认后加密。
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

Windows 中将 `.venv/bin/python` 换为 `.venv\Scripts\python.exe`。核心流程不强制安装 OCR/VLM 或配置 API Key；本地 OCR 只在需要时额外安装各端的 `requirements-ocr.txt`，本地 VLM 再独立安装 `requirements-vlm.txt` 并运行一次 `vlm-setup`。

## 关键文件与边界

| 文件 | 用途 | 应留在哪里 |
|---|---|---|
| `REQUEST.yintian-request` | 默认 AI→AI 信息请求包；不含名单或个人值 | HR 原样转发给员工 Agent |
| `FORM.yintian-form` / 定向表单 | 旧 `group/direct` 兼容协议 | 按兼容模式公开或私发 |
| `*.yintian-credential` | 兼容 `group` 模式的个人认证凭据 | 只私下发给对应本人 |
| `data/vault.yintian-vault` | 员工的加密个人保险柜（yintian-vault/2） | 只留在员工设备 `data/`（0600） |
| `data/vault.key` | 保险柜本机密钥 | 只留在员工设备 `data/`（0600），永不外发 |
| `姓名-短码.yintian` | 本次加密提交；仅文件名显示姓名 | 由员工本人按授权渠道交回收集者 |
| `result.xlsx` / `result-attachments/` | 最新通过记录与解密附件 | 只留在获授权的 HR / 接收方 |

- 默认开放流程中，填写 Agent 和 HR Agent 会处理各自获授权的明文；不得扩大字段、扫描磁盘、猜测缺值、把他人明文贴进聊天或自动替员工发送回执。
- 开放任务私钥始终加密，本地随机密钥存放在任务目录之外的受限目录；跨设备仅使用加密任务包。兼容模式的密码不得放入聊天、命令参数或环境变量。
- OCR/VLM 结果只是候选，不代替填写者确认和 HR 复核。
- 仓库不保存真实名单、凭据、明文资料、附件、提交文件、保险柜、导出表格或项目外文稿；`skills/safefill-fill/data/` 已在 `.gitignore` 中排除。

更完整的操作与权限说明见[收集者文档](skills/safefill-collect/README.md)与[填写者文档](skills/safefill-fill/README.md)。

## 仓库结构

```text
safefill/
├── README.md
├── skills/                  # 可安装的 Skill 集合
│   ├── safefill-collect/       # 收集者 Skill，可独立安装
│   │   ├── SKILL.md         # Agent 入口与权限边界
│   │   ├── agents/          # Skill 界面元数据
│   │   ├── scripts/         # 收集、解密校验与 Excel 导出
│   │   └── references/      # 收集配置说明
│   └── safefill-fill/        # 填写者 Skill，可独立安装
│       ├── SKILL.md
│       ├── agents/
│       ├── scripts/         # 保险柜、填写、OCR/VLM 提取与加密
│       └── data/            # 运行时生成的保险柜与密钥（0700，不入库）
└── .github/
```

## 验证方式

仓库不附带回归测试套件；以端到端冒烟为准（合成数据，不产生真实明文残留）：

```bash
# HR 侧：生成请求包
$PY skills/safefill-collect/scripts/collection.py create-request --config collection.json --out tasks
# 员工侧：初始化保险柜 → 匹配预览 → 生成回执
$PY skills/safefill-fill/scripts/fill.py vault-init --answers TEMP.json
$PY skills/safefill-fill/scripts/fill.py vault-status --request tasks/*/REQUEST.yintian-request
$PY skills/safefill-fill/scripts/fill.py vault-fill tasks/*/REQUEST.yintian-request --out-dir incoming --confirmed
# HR 侧：解密汇总
$PY skills/safefill-collect/scripts/collection.py collect tasks/<TASK_DIR> incoming --out result.xlsx
```

现有 `.yintian*` 扩展名和格式标识作为兼容文件协议保留，不影响 SafeFill Skill 的安装与使用。
