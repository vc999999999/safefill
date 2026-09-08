> 3.5.0 支持通过 `--agent-ocr` 交接宿主 Agent 候选，无需 API。请先阅读 [授权与候选格式](references/recognition.md)。核心脚本依然不联网。

# 隐填填写端 3.5.0

收到 HR 模板后，让 Skill 查看用途、字段、期限和指纹。密码与真实取值由本人在独立终端输入。

Python 3.11+，安装根目录 `requirements-core.txt`。源码与收集端一起使用；发布包内的填写 Skill 附带公共模块，可单独安装。

```bash
python scripts/fill.py inspect FORM.yintian-form --json
python scripts/fill.py vault-init FORM.yintian-form --vault personal.yintian-vault
python scripts/fill.py fill FORM.yintian-form --vault personal.yintian-vault --credential GRP-E001.yintian-credential --out reply.yintian
```

源码路径为 `yintian-fill/scripts/fill.py`，上面使用安装后的填写 Skill 目录。定向邀请不传 `--credential`。

`vault-init` 隐藏输入字段与指定附件，密码至少 12 字符，输入 `SAVE` 后只保存加密保险柜。`vault-edit` 同样参数更新。附件字节一并加密，不创建明文临时文件。

`fill` 只匹配本次字段。本人补缺项、独立核对 HR 指纹、查看本次取值及附件数量，输入任务编号确认后产出密文。取消或失败不产出文件；原保险柜不被本次填报自动改写。

自定义字段用 `--mapping mapping.json`，如 `{"mobile":"phone"}`。多候选不会自动选择；跨类型映射拒绝。

只交回 `.yintian`，不发送保险柜或个人凭据到群里。工具不接管其他密码管理器，不扫描个人磁盘。

手工兼容：`seal FORM --values values.json --credential PERSONAL --out reply.yintian`，仍需本人终端确认；明文 JSON 由本人清理，不交给 Agent。
