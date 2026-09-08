---
name: yintian-fill
description: 隐填填写端——员工在自己电脑上由 AI 协助填写需求格式文件并本机加密。只要用户收到 yintian-form 需求格式文件或 INV 邀请、说"帮我填/帮我写"、隐填填写、填报身份信息、填写邀请、fill invite、生成 .yintian 交回，就应使用本 Skill，即使用户没有点名。它核对任务指纹、可选本地 OCR 扫描证件给出遮罩候选、把 values.json 加密成与浏览器邀请页同构的 .yintian 密文。边界：只在员工本机运行、只读显式指定的文件、不联网、明文不出本机、绝不外发 values.json 或任何明文、不修改需求格式文件、不代编虚假取值；收集任务的创建、接收、复核属于收集端 yintian-skill，不在本 Skill 范围。
metadata:
  compatibility: "Python 3.9+；依赖 requirements.txt（仅 cryptography 必需）；scan-idcard 需可选 OCR 依赖 rapidocr-openvino + OpenVINO。"
---

# 隐填 · 填写端：员工本机填写与本机加密

本 Skill 是收集端闭环的员工侧：

```text
收到 yintian-form 需求格式文件 → inspect 核对任务编号与公钥指纹
→ （可选）scan-idcard 本地 OCR 给出遮罩候选 → 员工把真实取值写入 values.json
→ seal 本机加密产出 .yintian → 私聊交回 .yintian → 删除 values.json
```

先把本 `SKILL.md` 所在目录的绝对路径记为 `SKILL_ROOT`，并把该 Skill 虚拟环境中的 Python 绝对路径记为 `PYTHON`。所有脚本都用这两个绝对路径调用，不依赖当前工作目录。本 Skill 通过仓库相对路径复用收集端 `scripts/collection.py` 的加密与校验实现，`yintian-fill/` 必须与收集端 `scripts/` 同级。

## 三个命令

```bash
# 1. 查看告知内容、字段清单、任务编号与公钥指纹（只读，不修改文件）
"$PYTHON" "$SKILL_ROOT/scripts/fill.py" inspect FORM.yintian-form

# 2. 可选：本地 OCR 扫描一张证件图，遮罩输出姓名/身份证号/手机号候选
"$PYTHON" "$SKILL_ROOT/scripts/fill.py" scan-idcard IMAGE

# 3. 校验 values.json 并本机加密产出 .yintian 提交文件
"$PYTHON" "$SKILL_ROOT/scripts/fill.py" seal FORM.yintian-form --values values.json --out 姓名.yintian
```

`values.json` 形如 `{"values": {字段id: 值}, "attachments": {字段id: 文件路径}}`；group 模式另在顶层带 `employee_id`，seal 时提交标识自动为 `GRP-<工号>`；directed 模式的邀请编号与令牌来自格式文件本身。seal 的逐字段校验与收集端复核完全一致，必填缺失或校验失败会一次列出全部问题且不会产出文件。填错了改完重跑 seal 即可，收集端只认同一邀请的最新有效版本。

## 安全边界

- inspect 先行：先核对任务编号与公钥指纹（key_id）与发放人口头/电话告知的一致，再填写；不一致一律停手。
- 只读员工显式指定的文件（格式文件、values.json、逐个附件、单张证件图）；不遍历目录、不扫描磁盘找证件。
- 纯本地运行，不联网；明文只在员工本机内存与 values.json 中出现，绝不外发明文，只交回 .yintian 密文。
- 不修改需求格式文件；seal 产出的信封与浏览器邀请页字节级同构（AES-256-GCM + RSA-OAEP-3072/SHA-256，AAD 绑定任务与邀请标识）。
- scan-idcard 输出一律遮罩（身份证露前 3 后 4），多候选只列歧义由员工本人确认；OCR 为可选依赖，缺失时友好报错，不影响 inspect/seal。
- 绝不代编或"补全"身份证号等取值；不知道就引导员工如实填写或用 scan-idcard 从本人证件提取。
- values.json 含明文，seal 成功后提醒员工删除；权限宽于 0600 时给出警告。

与收集端的关系一句话：本 Skill 只负责员工侧"填与加密"，产出交由收集端 `yintian-skill` 的 ingest/review 接收复核；任务创建、进度、报告都不在本 Skill 范围。
