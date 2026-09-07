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
  --out tasks/
```

命令会：

- 生成 RSA-OAEP-3072 任务密钥；
- 使用随机任务密码加密私钥；
- 为每人生成一个文件名不含姓名、带独立随机认证令牌的 `INV-*.html`；
- 生成本地 `invite-index.csv`。

任务密码只显示一次，丢失不可恢复。不要将密码放入聊天、命令参数、任务目录或在线文档。

### 4. 私聊发出邀请并收回密文

员工使用新版 Chrome/Edge 打开自己的 HTML：

1. 本地填写并选择 JPG/PNG/WebP/PDF 附件；
2. 查看遮罩预览并确认告知；
3. 浏览器使用 AES-256-GCM + RSA-OAEP-3072/SHA-256 加密；
4. 私聊交回生成的 `.yintian` 文件。

单附件最大 5 MB，每份提交总计最大 15 MB，`.yintian` 文件最大 32 MB。页面不会联网，也不包含私钥或任务密码；成功导出后会清空表单，请关闭页面。

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

`.yintian-task` 是普通 ZIP 任务交接包，不包含任务密码，但包含明文名单标识、邀请页、状态数据库和报告；它不是整体加密包，内部 SHA-256 清单只能发现传输损坏，不能认证恶意篡改。只通过已认证且加密的渠道交接，并在导入前核对来源。第一版是单写入者交接，不支持多台电脑并发维护。

v1 任务可继续执行 `status`、`report`、`export-task` 和 `purge`，但不能再接收、复核或查看明文；如需继续收集，请新建 v2 任务。新版可导入旧的 `yintian-task/1` 交接包，导出统一使用 `yintian-task/2`，避免旧程序误接收新结构。

保存期限到达后任务停止接收、复核、生成报告、导出和查看。`purge` 要求再次输入任务 ID，然后删除该任务目录并留下匿名计数；它不会安全擦除磁盘残留，也不会删除收件目录、已导出的交接包或备份。密码丢失仍可清理；确需提前删除时增加 `--allow-early`。

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

MCP 拒绝访问 `YINTIAN_VAULT_DIR` 之外的任务目录和收件目录。旧版 OCR 和字段提取模块仅供内部复用，没有明文命令行入口，也不通过 MCP 返回明文。

## 测试

```bash
python scripts/test_self.py
python scripts/test_collection.py
```

浏览器端仍需在 Chrome/Edge 手工验证一次完整的填写、附件、遮罩确认和下载流程。

## INT8 量化与基准

可用 NNCF 对 OCR 的 det/cls/rec 模型做 INT8 训练后量化：

```bash
python -m pip install "nncf>=2.9"
# 无真实证件图时自动生成合成校准图；有脱敏样本时用 --calib-dir 指定图片目录
python scripts/quantize_ocr.py --out models/int8 --num-calib 40
```

再通过自定义模型目录对比 FP32 与 INT8 的设备和延迟：

```bash
python scripts/benchmark.py 样张.jpg --device CPU
python scripts/benchmark.py 样张.jpg --device CPU --model-dir models/int8
# 等价于 YINTIAN_MODEL_DIR=models/int8 python scripts/benchmark.py 样张.jpg
```

`YINTIAN_MODEL_DIR` 目录下缺少同名模型文件时自动回退到 RapidOCR 包内模型，不影响现有流程；基准报告会列出实际使用的模型路径和总大小。
