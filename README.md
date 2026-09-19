# SafeFill Skills

SafeFill 是一个 AI→AI 的私密资料收集协议。HR Agent 把需求编译成机器请求包；员工 Agent 读取字段，从本机加密保险柜匹配取值（首次初始化、缺项补录、OpenVINO 本地识别证件）并生成加密回执；HR Agent 收回密文后统一校验并导出 Excel。

> Fill locally. Share securely.

## 设计亮点

**1. 本机加密保险柜 · 一次录入，终身复用**

员工资料沉淀在系统用户数据目录的 `yintian-vault/2` 保险柜中：scrypt + AES-256-GCM 静态加密，`0600` 本机密钥放在独立用户密钥目录，不联网、不分发。之后只问保险柜里没有的字段；脚本仅按相同字段 ID 自动匹配，语义映射必须由员工 Agent 明确提出并由本人确认。为了让复用真正发生，HR 端字段 id 使用[标准字段 id 词表](skills/safefill-collect/references/collection-config.md)（`phone`、`id_number`、`address`、`hire_date`、`id_front`…），`vault-scan` 也按同一套 id 输出。

**2. OpenVINO 本地模型提取 · 安装后离线、无 API Key**

`vault-scan` 用 OpenVINO 在本机推理，把员工明确指定的证件图变成结构化字段候选（`name/id_number/phone/address`，并由身份证号派生 `birth_date/gender`）：基线为 rapidocr OCR（仅 Python 3.11 可装），也可通过 `--model` 使用用户选择的兼容 VLM，模型 revision 可选。`vlm-setup` 安装阶段需要联网，安装后的推理使用本地模型。识别结果永远只是候选，确定性校验和本人确认后才写入保险柜。

**3. 端到端加密 · 密码学兜底**

每份回执使用随机的 AES-256-GCM 内容密钥，并以请求包内的 RSA-OAEP-3072 公钥封装，只有持有任务私钥的收集方本机账户能解；HR 私钥静态加密存放，开放任务的本地密钥独立放在任务目录外的受限目录。回执文件名只显示姓名与防重名短码，身份证号、手机号等永不进入文件名。

**4. AI→AI 机器协议 · 零网页表单**

`REQUEST-{任务编号}.yintian-request` 是 Agent 之间交换的规范 JSON，不是给人填写的界面，也不会生成 HTML 或在线表单。请求包携带 `schema_hash`/`notice_hash` 防篡改，公钥指纹可带外核对；请求包、回执、附件一律视为数据，不执行其中夹带的指令。

**5. 克制的 Agent 边界**

不扫描磁盘寻找证件、不猜测或编造缺值、不代替员工发送回执、不把他人明文贴进聊天；临时明文一律 `0700` 目录 + `0600` 文件且用后删除。提交前生成 30 分钟有效的加密确认文件，任何请求、保险柜、映射或取值变化都会使确认失效。

## 两个 Skill

| Skill | 使用者 | 职责 | 说明 |
|---|---|---|---|
| `safefill-collect` | HR、行政等收集者 | 对话补齐需求、生成机器请求包、收取加密回执并汇总 Excel | [README](skills/safefill-collect/README.md) · [SKILL.md](skills/safefill-collect/SKILL.md) |
| `safefill-fill` | 提交资料的员工 | 读取机器请求包、本机保险柜匹配取值、确认并生成加密回执 | [README](skills/safefill-fill/README.md) · [SKILL.md](skills/safefill-fill/SKILL.md) |

`skills/` 下的两个目录都是独立 Skill。收集者和填写者可以在不同设备上只安装自己需要的一端。

## 共享模块同步

两个 Skill 的 `scripts/` 下有 5 个逐字节相同的共享模块：`collection.py`（协议层）、`config.py`、`ocr_matcher.py`、`openvino_runtime.py`、`secure_io.py`。
`collection.py` 是纯粹的 AI→AI 协议库（常量、规范 JSON、哈希、AES-GCM 封包、信封与字段校验），两端各存一份相同副本；收集端的任务管理、收件、导出等专有逻辑只在收集端的 `collector.py` 中，填写端不携带这部分代码。
修改其中任何一个必须双侧同步提交；`tests/check_skill_sync.py` 会逐字节比对两侧副本，发现漂移即失败（CI 与本地均可直接运行）。

## 版本与兼容

当前协议版本：请求包 `yintian-request/1`、保险柜 `yintian-vault/2`、回执 `yintian-submission/4`、确认文件 `yintian-confirmation/1`、交接包 `yintian-task/3`、退回/补正通知 `yintian-notice/1`、填写端本地提交登记 `yintian-submissions/1`（仅存本机保险柜目录，不传输）。代码只为当前版本实现，不包含旧版本（`yintian-form/*`、名单/凭据模式、v1 保险柜与交接包）的迁移路径——5.0/6.0 起老产物需用旧版本脚本先行处理或重新生成。协议格式变更时会递增版本号并在本段说明；`schema_hash`/`notice_hash` 保证回执与发出时的请求包严格对应，混用不同版本生成的文件会被明确拒绝而非静默接受。

## 默认使用方式：AI 请求包 + 本机保险柜

HR 不准备员工名单、配置文件或网页表单，也不需要运行终端。Agent 只补问尚未说明的用途、字段、必填性、截止时间和联系人，然后生成 `REQUEST-{任务编号}.yintian-request`。截止与保存时间必须带时区偏移；保存期限（默认截止后 30 天）一到，收件端按告知承诺拒绝解密，HR 需在此之前完成汇总。附件与填写值的自动比对（`ocr_fields`）默认关闭，只在 HR 明确要求并知晓"比对不通过需 HR 本人在终端 `decide` 裁定"后启用；字段名本身不会触发比对。

员工把请求包交给安装了 `safefill-fill` 的 Agent：Agent 读取并说明用途、与发放方带外核对任务编号和公钥指纹后运行 `vault-status`。首次使用或存在缺项时，通过 `vault-stage` 展示完整新旧值、本人确认后 `vault-apply`；提交前再由 `vault-preview` 展示本次完整取值和来源，确认后 `vault-fill` 生成 `姓名-随机短码.yintian` 并在保险柜目录登记本次提交（更正时自动沿用回执编号，`--fresh` 可强制新记录）。员工本人发送回执，HR Agent 收件后统一解密、校验并导出 Excel（第二列固定为回执编号）和附件；`collect` 同时返回逐人排除原因（含可直接运行的 `decide` 命令）、迟交人数与同名多行提醒。HR 侧还可用 `list-tasks` 找回任务、`status` 只读看进度和保存期限预警、`notice` 生成退回/补正通知交给员工 Agent 识别。

这意味着当前 Agent 会实际处理用户主动提供的明文。保险柜保护静态资料，回执端到端加密保护传输与汇总侧的内容，都不把明文对正在执行填写或汇总的 Agent 隐藏。若部署方不允许 Agent 接触明文，当前自动流程不适用。经标准输入传入的明文也会出现在 Agent 的命令行与宿主工具日志中；宿主持久化命令日志时应改用 `0700` 目录内的 `0600` 临时文件传值。

请求包的身份是提交者自报，不能防止同名、冒名、请求包转发或垃圾提交；需要强身份认证时应采用独立认证渠道。

两端的可选 OCR 均支持用 `YINTIAN_OCR_DEVICE` 选择 `CPU/GPU/NPU/AUTO`。保险柜和密钥目录可分别用 `YINTIAN_VAULT_DIR`、`YINTIAN_VAULT_KEY_DIR` 重定向。填写端 VLM 独立安装 `requirements-vlm.txt`，模型缓存可用 `YINTIAN_VLM_MODEL_DIR` 重定向；VLM 不可用时由 Agent 改用核心环境运行普通 OCR。

## 工作流程

```text
HR 说明用途、字段、期限和联系人
  → safefill-collect 生成 REQUEST-{任务编号}.yintian-request
  → 员工 Agent inspect 核对任务编号与公钥指纹，保险柜匹配取值（首次初始化、缺项补录、可选本地识别）并加密
  → 员工本人把回执发送给 HR
  → safefill-collect 校验并汇总 Excel 与附件目录（status 随时看进度，notice 可向员工发退回/补正通知）
```

1. Agent 从对话提取需求，只合并询问缺项；`name` 自动作为必填字段，不向 HR 索取名单。
2. HR 原样转发机器请求包；员工不打开、不手工填写，员工 Agent 读取后按保险柜匹配结果只问缺项，取值与映射经本人确认后加密。
3. 首次回执生成随机记录编号；更正时必须携带本人上一次回执沿用编号，不能按姓名猜测覆盖。
4. 收集端不信任文件名，以解密后的姓名和字段为准；异常、待复核或非最新记录不进入 Excel，`collect` 会按人给出原因与下一步。
5. 附件解密到 Excel 同名目录，单元格保存相对路径；明文输出只留给获授权的 HR。

## 安装

安装为 Agent Skill 时，请选择具体的 `skills/safefill-collect` 或 `skills/safefill-fill` 目录，不要把仓库根目录或 `skills/` 当成一个 Skill。每个 Skill 目录都应整体安装，不能只复制 `SKILL.md`，因为运行时还需要同目录下的脚本和参考文件。

以下命令只供 Skill 安装者或维护者使用，不是 HR/员工业务流程步骤。首次本地安装示例（macOS / Linux）：

```bash
git clone https://github.com/vc999999999/safefill.git
cd safefill

python3 -m venv skills/safefill-collect/.venv
skills/safefill-collect/.venv/bin/python -m pip install -r skills/safefill-collect/requirements.txt
skills/safefill-collect/.venv/bin/python skills/safefill-collect/scripts/collector.py doctor

python3 -m venv skills/safefill-fill/.venv
skills/safefill-fill/.venv/bin/python -m pip install -r skills/safefill-fill/requirements.txt
skills/safefill-fill/.venv/bin/python skills/safefill-fill/scripts/fill.py --help
```

Windows 中将 `.venv/bin/python` 换为 `.venv\Scripts\python.exe`。核心流程不强制安装 OCR/VLM 或配置 API Key；本地 OCR 只在需要时额外安装各端的 `requirements-ocr.txt`，本地 VLM 使用独立虚拟环境安装 `requirements-vlm.txt` 并运行一次 `vlm-setup`。

## 关键文件与边界

| 文件 | 用途 | 应留在哪里 |
|---|---|---|
| `REQUEST-{任务编号}.yintian-request` | AI→AI 信息请求包，文件名含任务编号；不含名单或个人值 | HR 原样转发给员工 Agent |
| `vault.yintian-vault` | 员工的加密个人保险柜（yintian-vault/2） | 系统用户数据目录（0600） |
| `vault.key` | 保险柜本机密钥 | 独立系统用户密钥目录（0600），永不外发 |
| `姓名-短码.yintian` | 本次加密提交；仅文件名显示姓名 | 由员工本人按授权渠道交回收集者 |
| `result.xlsx` / `result-attachments/` | 最新通过记录与解密附件 | 只留在获授权的 HR / 接收方 |

- 默认开放流程中，填写 Agent 和 HR Agent 会处理各自获授权的明文；不得扩大字段、扫描磁盘、猜测缺值、把他人明文贴进聊天或自动替员工发送回执。
- 任务私钥始终加密，本地随机密钥存放在任务目录之外的受限目录。
- OCR/VLM 结果只是候选，不代替填写者确认和 HR 复核。
- 仓库不保存真实明文资料、附件、提交文件、保险柜、导出表格或项目外文稿；`skills/safefill-fill/data/` 已在 `.gitignore` 中排除。

更完整的操作与权限说明见[收集者文档](skills/safefill-collect/README.md)与[填写者文档](skills/safefill-fill/README.md)；两端共享的格式、输出契约、错误处置与交接话术以 [PROTOCOL.md](PROTOCOL.md) 为准——它是接口稳定性的单一事实源，协议变更先改它再改代码。

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
│       └── scripts/         # 保险柜、确认、OCR/VLM 与加密
└── .github/
```

## 验证方式

仓库包含合成数据回归套件和端到端门禁：

```bash
# HR 侧：生成请求包
$PY skills/safefill-collect/scripts/collector.py create-request --config collection.json --out tasks
# 员工侧：暂存确认 → 精确匹配 → 提交确认 → 生成回执
printf '%s' "$JSON" | $PY skills/safefill-fill/scripts/fill.py vault-stage --answers - --confirmation-out "$WORK/CHANGE.yintian-confirmation"   # $WORK 为 0700 目录
$PY skills/safefill-fill/scripts/fill.py vault-apply --confirmation "$WORK/CHANGE.yintian-confirmation"
$PY skills/safefill-fill/scripts/fill.py vault-preview tasks/<TASK_DIR>/REQUEST-<TASK_ID>.yintian-request --confirmation-out "$WORK/SUBMIT.yintian-confirmation"
$PY skills/safefill-fill/scripts/fill.py vault-fill tasks/<TASK_DIR>/REQUEST-<TASK_ID>.yintian-request --confirmation "$WORK/SUBMIT.yintian-confirmation" --out-dir incoming
# HR 侧：解密汇总
$PY skills/safefill-collect/scripts/collector.py collect tasks/<TASK_DIR> incoming --out result.xlsx
$PY -m pytest -q
```

普通提交由 `.github/workflows/tests.yml` 跑 Python 3.11–3.13 与跨平台矩阵。VLM 由使用者按所选模型自行安装和验证，不绑定仓库指定的模型或 revision。
