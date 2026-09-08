# 隐填 · 填写端（yintian-fill）

员工在自己电脑上填写"隐填"需求格式文件并本机加密，产出 `.yintian` 密文交回发放人。全程离线，明文不出本机。

## 安装

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # 仅需 cryptography
# 可选：证件扫描 OCR
.venv/bin/pip install rapidocr-openvino==1.4.4 openvino==2024.0.0
```

本 Skill 复用收集端加密实现，`yintian-fill/` 需与收集端 `scripts/` 同级（完整仓库内即满足）。

## 三分钟上手

```bash
# 0. 收到发放人私聊发来的 FORM.yintian-form
# 1. 先核对：任务编号、公钥指纹须与发放人告知的一致
.venv/bin/python scripts/fill.py inspect FORM.yintian-form

# 2. 可选：扫描本人证件，候选一律遮罩展示，自己确认取值
.venv/bin/python scripts/fill.py scan-idcard 证件照.png

# 3. 按 inspect 列出的字段写 values.json
cat > values.json <<'EOF'
{
  "values": {"name": "张三", "phone": "13800138000", "id_number": "110105198503071230", "address": "北京市朝阳区"},
  "attachments": {"id_front": "front.png", "id_back": "back.png"}
}
EOF
chmod 600 values.json

# 4. 本机加密，产出密文
.venv/bin/python scripts/fill.py seal FORM.yintian-form --values values.json --out 张三.yintian

# 5. 私聊把 张三.yintian 交回发放人，然后删除 values.json
```

group（群组）模式没有邀请令牌，`values.json` 顶层另加 `"employee_id": "E001"`，提交标识自动为 `GRP-E001`；directed（定向）模式的邀请编号与令牌已内含在格式文件中。

## FAQ

- **填错了怎么办？** 改 values.json 重跑 seal，把新 `.yintian` 再交回一次即可；收集端复核时同一邀请的最新有效版本自动取代旧版本。
- **seal 报错不产文件？** 校验与收集端复核完全同规则（必填、手机号、身份证校验码、日期、选项、附件 5MB/15MB 上限），错误会一次列全，逐条改完重跑。
- **没有 OCR 依赖能用吗？** 能。inspect 和 seal 只需要 cryptography；scan-idcard 缺失依赖时会提示安装，也可以直接手动填写。
- **values.json 能发给发放人参考吗？** 不能。它是明文，只存在于你自己电脑上；交回的只有 `.yintian` 密文，用完请删除 values.json。
- **格式文件被改过了会怎样？** inspect/seal 会校验字段清单哈希、告知内容哈希与公钥指纹，任一不一致都会拒绝并提示重新索取。

## 测试

```bash
python -m pytest scripts/test_fill.py -q   # 或直接 python scripts/test_fill.py
```
