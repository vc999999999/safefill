# SafeFill · 填写者 7.0.0

员工把请求包或补正通知交给自己的 Agent，复用本机加密保险柜资料、补齐缺项，经本人确认后生成签名加密回执。补录可选私有对话，或把资料保存为本地文本只给路径，由脚本调用 OpenVINO 提取、本人在本地核对文件确认。

Agent 指令见 [SKILL.md](SKILL.md)，格式与边界见 [PROTOCOL.md](references/PROTOCOL.md)。安装整个 Skill 目录即可，不依赖收集端目录或仓库根文件。

核心环境 Python 3.11–3.13，在 Skill 目录内：

```bash
python -m pip install -r requirements.txt
python scripts/fill.py doctor
```

- 本地文本提取：独立环境安装 `requirements-vlm.txt`，`vlm-setup --model MODEL [--revision REV]` 缓存兼容 OpenVINO GenAI `LLMPipeline` 的模型。
- 可选图片识别：普通 OCR 需 Python 3.11 与 `requirements-ocr.txt`；VLM 复用上面的独立环境。
- 保险柜与密钥默认在系统用户目录，可用 `YINTIAN_VAULT_DIR`/`YINTIAN_VAULT_KEY_DIR` 重定向；保险柜同目录的 `wiki.md` 是允许 Agent 读取的明文业务备注。

当前文本路径使用替身模型与合成资料验证，**真实 OpenVINO GenAI 文本推理尚未验证**；具体模型和设备需实际运行确认。
