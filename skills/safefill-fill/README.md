# SafeFill · 填写者 7.0.0

员工把请求包或补正通知交给自己的 Agent，复用本机加密保险柜资料、补齐缺项，经本人确认后生成签名加密回执。不新增填写表单或弹窗。

完整指令见 [SKILL.md](SKILL.md)，格式和边界见随包携带的 [PROTOCOL.md](references/PROTOCOL.md)。安装整个 Skill 目录即可，不依赖收集端目录或仓库根文件。

可在 `vault-status` 返回的 `wiki_path` 保存业务背景和填写偏好。该 `wiki.md` 位于保险柜同目录，是允许 Agent 读取的明文备注，不是加密资料；Agent 结合本次目的和请求字段备注理解，不自动发给收集者，也不能用备注豁免必填校验。

核心环境需要 Python 3.11–3.13。在 Skill 目录内，用所选环境的 Python 安装和检查：

```bash
python -m pip install -r requirements.txt
python scripts/fill.py doctor
```

补录时 Agent 会提供两种选择：

- 私有对话：告知必要值，在会话中核对新旧值及提交预览。这些内容会进入填写模型或宿主日志；`--answers FILE` 只接受读后删除的临时副本，不能传唯一原件。
- 本地文本：自行把字段含义和值保存为至多 16 KiB 的 UTF-8 `.txt`，只提供路径。脚本调用 OpenVINO 提取并保留原件；Agent 只获得元数据及 `review` 路径，本人自行打开核对后在对话中确认。后续复用这些条目也不回传值，Agent 不读取、回显或截图源文件与 review。

本地文本需在独立环境安装 `requirements-vlm.txt`，用 `vlm-setup --model MODEL [--revision REV]` 缓存兼容 OpenVINO GenAI `LLMPipeline` 的模型，再运行 `vault-stage --text-file ...`；具体参数和确认步骤见 [SKILL.md](SKILL.md)。图片 VLM 不一定兼容文本提取。可选图片 `vault-scan` 的候选仍在私有会话核对，普通 OCR 使用 Python 3.11 和 `requirements-ocr.txt`。

源文本和 review 是本地明文，文件名应避免私密值；成功消费凭据后脚本尝试删除 review，取消、过期或失败可能留下核对文件，由 Agent 按已知路径清理本次产物，不读取内容或删除用户源文本。普通删除不保证安全擦除，流程不隔离同一系统账户权限。文本提取失败不会自动读取原文或改用云端；只有本人改选对话后才通过聊天补录。

员工本人发送匿名命名的 `.yintian` 回执，保险柜、密钥和确认文件不外发。HR 脚本本机解密导出，HR Agent 只获得匿名状态与路径。更正沿用本机签名身份及编号、递增修订；已有 v2 保险柜兼容，旧回执/旧任务使用对应旧版工具处理或重新发起。

当前文本路径使用替身模型与合成资料验证，**真实 OpenVINO GenAI 文本推理尚未验证**；具体模型和设备需实际运行确认。
