# SafeFill · 填写者 3.5.0

从本人的加密保险柜选择模板要求的资料，确认后生成 .yintian 密文。整个文件夹可独立安装，不需要收集者源码。

在本文件夹中用 Python 3.11+ 安装并运行：

```bash
python -m pip install -r requirements.txt
python scripts/fill.py inspect FORM.yintian-form --json
python scripts/fill.py vault-init FORM.yintian-form --vault personal.yintian-vault
python scripts/fill.py fill FORM.yintian-form --vault personal.yintian-vault --credential PERSONAL.yintian-credential --out reply.yintian
```

群发模板需要 HR 私下发给本人的凭据；定向模板不传 --credential。先通过独立渠道核对公钥。vault-init、vault-edit、fill、seal 均由本人在独立终端运行，Agent 不旁观密码或保险柜内容。

保险柜只在本人输入密码后解锁。本次字段按显式映射、同名同类型或唯一语义类型匹配；多候选和缺项交给本人处理。--mapping mapping.json 可以明确指定字段对应关系。

本地 OCR 为可选能力；需要时额外安装 requirements-ocr.txt。本人明确授权宿主查看指定原件后，Agent 可生成候选；fill 增加 --agent-ocr agent-result.json 即可交接，无需独立 API。云端宿主会处理被授权的附件，详见 [识别授权与格式](references/recognition.md)。

只交回 .yintian，不发保险柜、凭据或明文候选到群里。候选须在本人终端另外确认并与资料一起加密；收集者仍须对照原件人工复核。手工 seal --values values.json 保留为兼容入口，本人负责清理明文输入与候选文件。
