# SafeFill Skills

SafeFill 是一对协作收集资料的 Agent Skills：HR Agent 把需求变成机器请求包，员工端脚本从本机加密保险柜复用资料，由员工通过私有对话或本地文本补录并确认，最后生成签名加密回执；收集端验证后在本机导出 Excel 与附件。

> Fill locally. Share securely.

**补录可选择本地文本：员工自行保存资料，脚本调用 OpenVINO 提取，Agent 只获得字段状态和本地核对文件路径。** 普通对话补录仍允许填写 Agent 处理必要明文；收集端脚本本机解密到 Excel/附件，HR Agent 只拿匿名摘要与路径。本项目不新增填写表单或弹窗。

## 两个可独立安装的 Skill

| Skill | 使用者 | 职责 | 文档 |
|---|---|---|---|
| `safefill-collect` | HR、行政等收集者 | 生成请求、验证回执、处理补正、汇总本地 Excel | [SKILL.md](skills/safefill-collect/SKILL.md) · [README](skills/safefill-collect/README.md) |
| `safefill-fill` | 提交资料的员工 | 保险柜复用、对话或本地文本补录、确认与签名加密提交 | [SKILL.md](skills/safefill-fill/SKILL.md) · [README](skills/safefill-fill/README.md) |

Agent 负责需求理解、字段语义、命令编排与结果说明；确定性脚本负责加密、签名、修订选择、校验和确认绑定。通用环境准备、文件操作与获授权的发送交给宿主已有能力；没有网站、后台、账号或名单系统。

两端支持可选本地 `wiki.md`：收集端放在任务总目录，填写端放在保险柜文件同目录。它是供各自 Agent 读取的明文业务备注，不在加密保险柜内部，不保存具体敏感值。Agent 按本次目的选用内容，仅在本人要求时维护；收集端可把选定的字段说明写入请求的 `fields[].notes`，填写端 `inspect` 会展示，原始 Wiki 不自动外发。备注不改变必填规则，冲突先澄清，不新增条件规则引擎。

## 工作流程

1. HR 说明用途、字段、期限和联系人。收集 Agent 补齐缺失需求，使用[标准字段 ID](skills/safefill-collect/references/collection-config.md)生成 `REQUEST-{task_id}.yintian-request`，交 HR 或获授权的宿主工具原样转发。
2. 员工 Agent 核对任务编号与发放方公钥指纹，查询保险柜字段元数据。相同 ID 自动匹配；不同 ID 的语义映射由 Agent 提出，在员工私有会话中明确确认。
3. 首次录入、缺项或修改时，Agent 主动告知可在私有会话补录，也可自行将字段含义和值写入本地 UTF-8 `.txt`。对话使用 `vault-stage --answers`；文本使用 `vault-stage --text-file LOCAL.txt --request REQUEST --model MODEL`，由脚本调用本地模型提取，只返回字段状态和 review 路径。本人核对完整新旧值、明确确认后，`vault-apply` 入库。
4. `vault-preview` 准备本次取值、来源、映射与附件摘要。选择过本地文本的条目在后续复用和映射中不回传值：缺项时仅返回元数据，就绪后使用本地 review，Agent 不读取文件；普通资料可在私有会话展示。本人确认后，`vault-fill` 生成匿名命名的签名加密回执，员工本人发送。可选图片 `vault-scan` 的候选仍在本人私有会话中核对。
5. HR Agent 调用 `collect`；脚本验证、解密并导出 Excel/附件，Agent 只返回路径、数量和按回执编号标识的异常。HR 自行查看本地结果。

员工取消或拒绝时停止写入/提交。确认凭据绑定精确内容，有效期 30 分钟；有关内容变更后需重新展示并确认，无关保险柜条目变更不作废。凭据已生成或 `ready:true` 不代表真人已经同意，确认仍依赖宿主交互和 Agent 遵守流程。

## 核心保证与边界

**资料跨任务复用。** 保险柜使用 scrypt + AES-256-GCM 加密，资料只需在缺项或变更时重新输入；标准字段 ID 减少重复匹配。保险柜与密钥分别存放在系统用户数据目录和密钥目录，可用 `YINTIAN_VAULT_DIR`、`YINTIAN_VAULT_KEY_DIR` 重定向。POSIX 使用受限权限；Windows 依赖本机用户目录权限，不把 POSIX mode 位视为 ACL 隔离证明。

**加密与可验证更正。** 回执使用随机 AES-256-GCM 内容密钥，由请求包 RSA-OAEP-3072 公钥封装；Ed25519 签名绑定完整信封和修订号。回执编号由任务与提交者公钥派生，持有他人回执文件不能取得更正权。同一编号按最高已签名修订处理，收件乱序不能覆盖新值；同修订分叉阻断导出，需更高唯一修订解决。签名证明密钥持有连续性，不证明现实员工身份。

**减少资料进入模型。** 文本补录由脚本读取用户指定的源文件并本地推理；完整候选、旧值与预览写入受限权限的 `REVIEW-*.txt`，本人自行在编辑器中核对，不自动弹窗。Agent 不读取、回显或截图源文件/review；文本 stage 的源文件与 review 均受摘要绑定，修改使确认失效，成功消费凭据后脚本尝试删除 review，源文件保留。对话及图片识别路径仍向填写 Agent 返回必要明文，stdin 和输出可能留在宿主日志。源文本和 review 本身是本地明文，这不防御拥有同一系统账户权限的主动读取。

**收集端不把明文回传模型。** 收件、状态和汇总命令只返回编号、数量、字段级原因与路径，同名提醒仅返回编号组；HR 自行查看本地 Excel。回执文件名仅含编号、修订号及随机后缀。

**本地识别是可选能力。** 文本提取使用兼容 OpenVINO GenAI `LLMPipeline` 的模型，只处理至多 16 KiB 的 UTF-8 `.txt`；OCR 与兼容 VLM 处理指定图片。两类模型不能假定互换，提取结果都只是候选，须本人核对。文本提取失败时停止，不自动读取原文或换云端；用户明确选择后可回到对话补录。模型安装需要联网，安装后推理使用本地文件；这不代表宿主 Agent 离线。设备枚举或本地文件哈希校验不等于所有设备、模型质量或来源已获验证。

**收集状态有明确范围。** `status` 只统计收到的回执，不声称知道谁没交；`notice` 用于补正。保存期限默认截止后 30 天，到期拒绝解密，无宽限。OCR 比对默认关闭；明确启用后，异常由 HR 本人本地裁定或退回重交。人工裁定、任务交接和销毁仍有本人终端步骤。

## 版本与兼容

当前格式：请求 `yintian-request/1`，回执 `yintian-submission/5`，保险柜 `yintian-vault/2`，确认 `yintian-confirmation/1`，交接包 `yintian-task/3`，补正通知 `yintian-notice/1`，本机登记 `yintian-submissions/1`。

已有 v2 保险柜可继续使用，其加密内容新增签名身份。v4 回执和 DB1 旧任务不原地迁移，使用对应旧版工具单独完成，或重新创建请求；新版明确拒绝旧任务，不混收旧回执。新任务的回执版本由请求包 `format_version` 指定。

## 安装与环境

分别整体安装 `skills/safefill-collect` 或 `skills/safefill-fill`，不要只复制 `SKILL.md` 或把仓库根目录当作一个 Skill。每端已包含运行脚本和协议参考，不依赖另一端或仓库根文档。

核心环境使用 Python 3.11–3.13，填写端不需要图形窗口；Tk 仅用于收集端原有的 OCR 人工裁定。以下是安装者的 macOS/Linux 示例，Agent 可使用宿主已有环境完成相同准备：

```bash
python3 -m venv skills/safefill-collect/.venv
skills/safefill-collect/.venv/bin/python -m pip install -r skills/safefill-collect/requirements.txt
skills/safefill-collect/.venv/bin/python skills/safefill-collect/scripts/collector.py doctor
python3 -m venv skills/safefill-fill/.venv
skills/safefill-fill/.venv/bin/python -m pip install -r skills/safefill-fill/requirements.txt
skills/safefill-fill/.venv/bin/python skills/safefill-fill/scripts/fill.py doctor
```

Windows 使用 `.venv\Scripts\python.exe`。对话核心流程不需要 OCR/VLM 或 API Key；普通 OCR 额外安装 `requirements-ocr.txt`，其固定后端需要 Python 3.11。VLM 与本地文本提取复用独立环境的 `requirements-vlm.txt` 和 `vlm-setup --model MODEL [--revision REV]`，选择各自兼容的模型。OCR 设备由 `YINTIAN_OCR_DEVICE` 配置；本地模型目录和设备分别由 `YINTIAN_VLM_MODEL_DIR`、`YINTIAN_VLM_DEVICE` 配置，能否运行以具体模型和设备实测为准。

## 文件与维护

| 产物 | 应留在哪里 |
|---|---|
| `REQUEST-*.yintian-request`、`*.yintian-notice` | 按授权渠道原样交付，不含员工值 |
| `RECEIPT-{invite_id}-{revision}-{随机6位}.yintian` | 由员工本人交给 HR，内容加密 |
| `vault.yintian-vault`、`vault.key`、确认凭据、本机登记 | 员工本机，不交给 HR 或贴入对话 |
| 文本补录源文件、`REVIEW-*.txt` | 员工本机明文，本人自行查看；Agent 不读取，源文件由本人保管或删除 |
| 两端各自的 `wiki.md` | 本地明文业务背景与偏好，允许各自 Agent 读取，不自动打包或同步 |
| `result.xlsx`、同名附件目录 | 获授权的 HR 本机，Agent 不读取内容 |
| HR 数据库、私钥与解密密钥 | HR 本机；跨设备只通过加密任务包交接 |

[PROTOCOL.md](PROTOCOL.md) 是接口维护源，两个 Skill 的 `references/PROTOCOL.md` 是独立安装副本。另有 5 个共享脚本：`collection.py`、`config.py`、`ocr_matcher.py`、`openvino_runtime.py`、`secure_io.py`。修改共享内容后同步副本，`tests/check_skill_sync.py` 比对脚本和协议并检查 Skill 内部文档引用。

```bash
python tests/check_skill_sync.py
python -m pytest -q
```

回归测试使用合成数据。文本路径在当前开发环境使用替身模型验证，尚未完成真实 OpenVINO GenAI 文本推理验证。自动测试验证协议与失败路径，不替代真实 Agent 交互、原有 HR 人工裁定及具体本地模型和设备的实际验证。仓库不得提交真实员工资料、密钥、保险柜、回执或导出结果。
