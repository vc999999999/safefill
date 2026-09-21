# SafeFill · 收集者 6.0.0

HR 说明用途、字段、截止时间和联系人，Agent 生成请求包，接收员工密文回执并调用脚本在本机导出 Excel/附件。正常命令输出仅含回执编号、计数、状态与结果路径，不含员工姓名、字段值或识别内容；HR 自行打开本地结果。

完整指令见 [SKILL.md](SKILL.md)，格式和边界见随包携带的 [PROTOCOL.md](references/PROTOCOL.md)，字段定义见 [collection-config.md](references/collection-config.md)。安装整个 Skill 目录即可，不依赖填写端目录或仓库根文件。

任务总目录可放一份供 Agent 读取的明文 `wiki.md`，记录各用途的业务背景与字段说明。Agent 只选取本次适用内容，将需要对外告知的说明写入请求字段 `notes`，不自动外发整份 Wiki。涉及免填条件时先确认与 `required` 一致；当前备注只供 AI 理解，不提供条件必填引擎。

```bash
python scripts/collector.py create-request --config collection.json --out tasks
python scripts/collector.py list-tasks tasks
python scripts/collector.py status TASK_DIR
python scripts/collector.py collect TASK_DIR INCOMING_DIR --out result.xlsx
python scripts/collector.py notice TASK_DIR INVITE_ID --out NOTICE.yintian-notice
```

请求包和通知可由获授权的宿主发送工具原样交付，或交 HR 转发；员工回执由员工本人发送。请求摘要校验一致性，员工仍须通过可信发放渠道核对任务编号和公钥指纹。

新回执为 `yintian-submission/5`。Ed25519 签名验证修订归属，最高已签名修订决定当前状态，文件名和收件顺序不能改变新旧关系；最高修订冲突时阻断导出与人工放行，由员工签发更高唯一修订解决。签名不认证现实员工身份。旧任务由对应旧版工具单独完成或重新创建，不静默迁移。

Excel 第二列为完整回执编号；待复核、无效、退回或冲突的当前记录不混入结果。`exclusions` 按编号给出原因与下一步，`duplicate_name_groups` 只给同名记录的编号组。`status` 仅涵盖收到的回执，不能判断谁未提交；不要求新增员工名单。

保存期限到达即拒绝解密。OCR 比对默认关闭，明确启用后的异常由 HR 本人在本地裁定或退回重交。`decide`、任务交接与销毁仍需 HR 本人交互终端；Agent 不读取附件代替看图裁定，也不获取交接密码。Agent 不打开 Excel 或解密附件来生成含员工值的聊天总结。
