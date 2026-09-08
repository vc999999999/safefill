# 隐填 · 端到端加密的私密信息收集管理

职能部门可以批量生成离线邀请，员工在本地 Chrome/Edge 中填写并加密身份信息，再通过私聊交回 `.yintian` 文件。管理端只在本机内存解密，并用 RapidOCR + OpenVINO 复核证件附件。进度报告保留 `employee_id` 和姓名，但不含手机号、身份证号、住址、OCR 原文或附件，因此是数据最小化报告，不是匿名数据。

## 安装

```bash
# Linux/macOS
python3.11 -m venv .venv
# Windows
py -3.11 -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
python -m pip install -r requirements.txt
```

注：`rapidocr-openvino==1.4.4` 只支持 `openvino<=2024.0.0`，因此仓库钉 `openvino==2024.0.0`，二者不可拆开升级。该版 openvino 的预编译动态库在 macOS 26 上无法加载；在这类新系统上可改用已实测兼容的组合：

```bash
python -m pip install "openvino==2025.4.1"  # 仍提供 openvino.runtime（弃用警告，不影响功能）
python -m pip install --no-deps "rapidocr-openvino==1.4.4"
python -m pip install pyclipper "opencv-python>=4.5.1.48" "numpy<3" six "Shapely!=2.0.4,>=1.7.1" PyYAML Pillow tqdm
```

## 快速开始

### 1. 准备名单

```csv
employee_id,name
E001,张三
E002,李四
```

### 2. 生成并修改收集配置

```bash
python scripts/collection.py init-config --out collection.json
```

必须填写真实、具体的：用途、截止时间、保存期限、联系人和更正方式。默认模板包括姓名、手机号、身份证号、住址和身份证正反面。

### 3. 创建任务

`create` 必须由职能人员在未被 Agent 控制或录制的终端运行。TTY 检查只确认终端可交互，不能证明操作者是人：

```bash
python scripts/collection.py create \
  --roster roster.csv \
  --config collection.json \
  --out tasks/ \
  --mode directed        # 或 --mode group（默认 directed）
```

双模式说明：

- **directed 定向型（默认）**：为每人生成带独立随机认证令牌的 `INV-*.html` 和一份 `INV-*.yintian-form` JSON；身份有令牌绑定，适合身份证号等高敏感定向收集。
- **group 群发型**：不生成令牌，全员共用单份 `FORM.yintian-form` JSON（无个人邀请文件）；提交信封以 `GRP-<employee_id>` 标识，版本上限按工号计数；身份没有令牌绑定，任何拿到表单的人都能为任何工号提交，靠复核时名单比对兜底，只适合低敏感、群公告分发的场景。

命令会：

- 生成 RSA-OAEP-3072 任务密钥；
- 用随机任务密码以 scrypt 派生密钥、AES-256-GCM 包裹私钥（`yintian-key/1` 信封）；
- directed 模式为每人生成一个文件名不含姓名、带独立随机认证令牌的 `INV-*.html` 和 `INV-*.yintian-form` JSON；group 模式只生成一份 `FORM.yintian-form` JSON；
- 生成本地 `invite-index.csv`。

任务密码只显示一次，丢失不可恢复。不要将密码放入聊天、命令参数、任务目录或在线文档。

### 4. 私聊发出邀请并收回密文

员工侧是双通道填写：有 AI 环境的员工用配套的 yintian-fill Skill 读取 `.yintian-form` JSON，在本机离线填写并加密；没有 AI 环境的员工用新版 Chrome/Edge 打开邀请 HTML（兜底通道）。两通道产出的 `.yintian` 信封同构，收集端 `ingest` 无差别接收。HTML 通道流程：

1. 本地填写并选择 JPG/PNG/WebP/PDF 附件；
2. 查看遮罩预览并确认告知；
3. 浏览器使用 AES-256-GCM + RSA-OAEP-3072/SHA-256 加密；
4. 私聊交回生成的 `.yintian` 文件。

单附件最大 5 MB，每份提交总计最大 15 MB，`.yintian` 文件最大 32 MB。页面不会联网，也不包含私钥或任务密码；成功导出后会清空表单，请关闭页面。

人数较多时可用离线分发辅助脚本避免复制错发，并跟踪催办（纯本地运行，不发起任何网络请求，只读取名单级的 `invite-index.csv` 和状态数据库，不接触密文与表单敏感值）：

```bash
python scripts/distribute.py verify tasks/YT-...      # 校验每份邀请 HTML 齐全且非空
python scripts/distribute.py messages tasks/YT-...    # 按人生成含姓名、工号、邀请文件名的私聊文案
python scripts/distribute.py pending tasks/YT-...     # 列出尚未交回的员工并生成催办文案
```

### 5. 接收和复核

```bash
python scripts/collection.py ingest tasks/YT-... received/
python scripts/collection.py review tasks/YT-...
python scripts/collection.py status tasks/YT-...
python scripts/collection.py report tasks/YT-... --formats xlsx json
```

`review` 会交互询问任务密码，然后在内存中：

- 验证密文完整性、任务、邀请认证令牌和模板绑定；
- 检查手机号、身份证号、必填字段和告知确认；
- 对图片和扫描 PDF 使用 OpenVINO OCR；
- 比较手填值和 OCR 候选，冲突只进入人工复核，不自动覆盖。

报告只包含员工编号、姓名、状态、缺失/冲突字段名和时间，不包含手机号、身份证号、住址、邀请认证令牌、OCR 原文、附件或密钥。它仍含直接身份标识，只能上传到已获批且访问受控的平台。

### 5b. 导出交付（export-clear）

确需把明文汇总交给下游（如行政订酒店）时，使用 `export-clear`——只能由职能人员在自己未被 Agent 控制或录制的终端运行，Agent 不得代跑、不得经手密码：

```bash
python scripts/collection.py export-clear tasks/YT-... \
  --out result.xlsx \
  --fields name phone_cn id_card \
  --mask last4 \
  --purpose "行政统一订酒店" --recipient "行政部 王五"
```

- `--fields` 白名单逐列指定导出字段；`--mask last4|mid4` 对导出值脱敏（保留后 4 位 / 遮蔽中间 4 位），只导出确有必要的数据；
- `--purpose` / `--recipient` 必填，作为交付审计说明；
- getpass 交互询问任务密码，并要求再次输入任务 ID 二次确认；
- 明文 XLSX 只在本机落盘（0600 权限），命令会打印显著的明文警告。**系统无法管控文件落盘后的传播**：用后请立即删除，仅通过获批渠道交付给 `--recipient` 指定的接收方。

### 6. 按需人工查看

```bash
python scripts/collection.py reveal tasks/YT-... INV-...
```

`reveal` 只能由授权人员在未被 Agent 控制或录制的终端运行，不是 MCP 工具。它重新询问密码、显示单份明文，并尝试清空当前屏幕；终端滚动历史、会话录像和崩溃记录仍可能保留内容。

## 任务交接和到期清理

```bash
python scripts/collection.py export-task tasks/YT-... --out task.yintian-task
python scripts/collection.py import-task task.yintian-task --out tasks/
python scripts/collection.py purge tasks/YT-...
```

`export-task` 导出的是整体加密认证的 `.yintian-task` 交接包（`yintian-task/3`）：先在内存打包任务内容，再用随机生成的一次性交接密码经 scrypt（n=32768）派生密钥、以 AES-256-GCM 整体加密，AAD 绑定格式与任务 ID。交接密码只在导出终端显示一次，丢失不可恢复，必须通过另一安全渠道单独告知接收方，不得与交接包同渠道发送。导入时交互询问交接密码；密码错误或任何篡改都会被 GCM 认证直接拒绝，且不产生任何落盘。交接包不包含任务密码，任务密码须另行交接。因交接密码的显示与输入，`export-task`/`import-task` 只能由授权人员在未被 Agent 控制或录制的终端运行。第一版是单写入者交接，不支持多台电脑并发维护。

同一邀请的提交版本数默认最多 10 个（可用 `YINTIAN_MAX_VERSIONS_PER_INVITE` 环境变量调整），超限提交拒绝存储且不影响已有版本。

v1 任务可继续执行 `status`、`report`、`export-task` 和 `purge`，但不能再接收、复核或查看明文；如需继续收集，请新建 v2 任务。导入端仍兼容旧的 `yintian-task/1`、`yintian-task/2` 明文交接包，但会打印"未加密、不能认证来源，仅在信任渠道下导入"的显著告警；导出统一使用加密的 `yintian-task/3`。

保存期限到达后任务停止接收、复核、生成报告、导出和查看。`purge` 要求再次输入任务 ID，然后删除该任务目录并留下匿名计数；它不会安全擦除磁盘残留，也不会删除收件目录、已导出的交接包或备份。密码丢失仍可清理；确需提前删除时增加 `--allow-early`。

## 临时文件清理

每个阶段结束后可让 Agent 运行 cleanup（默认 dry-run，只打印分类计划，不改动文件）：

```bash
python scripts/cleanup.py tasks/YT-...            # 分类列出"将删除"和"保留"，附大小、mtime 和理由
python scripts/cleanup.py tasks/YT-... --apply    # 实际删除
python scripts/cleanup.py tasks/YT-... --json     # 机器可读输出，供 Agent/MCP 使用
```

`--apply` 只删除三类可再生的临时产物：atomic_write 中断留下的孤儿临时文件（超过 24 小时未变，可用 `--stale-lock-hours` 调整）、写入进程已退出的 `.write.lock` 死锁、`__pycache__` 下的 `.pyc`。名单、邀请、`.yintian` 密文、报告、`state.sqlite3`、交接包等用户数据一律列入"保留"并给出处理指引（`purge` / `export-task` / 用户手动确认），绝不删除；符号链接一律不跟随、不删除。指定 `--vault` 或设置 `YINTIAN_VAULT_DIR` 时，所有扫描路径必须在保险箱内；不给路径且未配置保险箱时直接报错，绝不默认扫描当前目录。

## MCP 安全接口

```bash
export YINTIAN_VAULT_DIR=/绝对路径/隐填任务目录
python scripts/mcp_server.py
```

只暴露：

- `openvino_status`
- `ingest_encrypted_submissions`
- `collection_status`
- `generate_redacted_report`
- `verify_invites`
- `list_pending_reminders`
- `cleanup_temp_files`

MCP 拒绝访问 `YINTIAN_VAULT_DIR` 之外的任务目录和收件目录。

## 配套：填写端 Skill yintian-fill

`yintian-fill/` 是员工侧的独立 Skill，与本收集端构成双 Skill 闭环：

```text
yintian-skill（HR/职能侧）          yintian-fill（员工侧）
create --mode directed|group  →   分发 .yintian-form JSON
                              →   fill.py inspect    查看表单字段与告知
                                  fill.py scan-idcard 本地 OCR 读取证件
                                  fill.py seal       本机加密封口
                              ←   产出同构 .yintian 信封，私聊交回
ingest / review / report
```

- 它读取 `yintian-form/1` JSON（directed 模式为每人一份 `INV-*.yintian-form`，group 模式为共用的 `FORM.yintian-form`），在员工本机离线填写并加密，产出与浏览器 HTML 同构的 `.yintian` 信封；group 模式信封 `invite_id` 为 `GRP-<employee_id>`。
- 明文只在员工本机出现；没有 AI 环境的员工仍可用邀请 HTML 兜底。

## 测试

```bash
python scripts/run_tests.py     # 自动发现并运行全部 scripts/test_*.py（含 yintian-fill/scripts/，需先安装 requirements.txt 依赖）
# 也可单独直跑任一模块：
python scripts/test_ocr_matcher.py
python scripts/test_distribute.py
python scripts/test_cleanup.py
python scripts/test_mcp_vault.py
python scripts/test_collection.py
# 或使用 pytest（安装 requirements-dev.txt 后）：
python -m pytest scripts/ -q
PYTHONPATH=. python -m pytest yintian-fill/scripts/ -q   # 填写端：import 根仓 scripts/ 需仓库根在 PYTHONPATH
```

依赖缺失时不静默假绿：pytest 下相关用例按 skip 处理，`run_tests.py` 汇总会显示跳过数量；单个模块直跑时也会明确打印 SKIP 而非 PASS。

浏览器端仍需在 Chrome/Edge 手工验证一次完整的填写、附件、遮罩确认和下载流程。

## INT8 量化与基准

```bash
python -m pip install -r requirements-quantization.txt
# 无真实证件图时自动生成合成校准图；有脱敏样本时用 --calib-dir 指定图片目录
python scripts/quantize_ocr.py --out models/int8 --num-calib 40
```

实测结论（Apple M2，CPU，合成样张）：对 det/rec 做激活式训练后量化会破坏输出（det 丢行、rec 解码为空），因此脚本对 det/rec 采用 INT8 逐通道权重压缩、仅 cls 使用校准量化（--calib-dir/--num-calib 只对 cls 生效）。该组合下文本一致率 100%，模型总体积 15.44 MB → 6.21 MB（约 2.5 倍压缩）；CPU 延迟基本持平（约 1.04 倍，属波动范围），延迟收益需要 GPU/NPU 设备。

再通过自定义模型目录对比 FP32 与 INT8 的文本一致率和延迟：

```bash
python scripts/benchmark.py 样张.jpg --device CPU
python scripts/benchmark.py 样张目录/ --device CPU --model-dir models/int8
# 等价于 YINTIAN_MODEL_DIR=models/int8 python scripts/benchmark.py 样张.jpg
```

`YINTIAN_MODEL_DIR` 目录下缺少同名模型文件时自动回退到 RapidOCR 包内模型，不影响现有流程；基准报告会列出实际使用的模型路径和总大小。
