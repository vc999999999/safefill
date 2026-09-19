# SafeFill · 填写者 6.0.0

员工把 `REQUEST-*.yintian-request` 交给 Agent（文件名含任务编号）。Agent 读取机器请求包，从本机加密保险柜按字段 ID 精确匹配，只询问缺项；提交前必须把完整取值、来源和显式映射逐项展示给员工确认。员工不打开表单、不编辑 JSON、不运行终端。

Agent 内部流程（`$WORK` 为 Agent 用 `mktemp -d` 建的 0700 私有目录，每次确认文件用新文件名）：

```bash
python scripts/fill.py inspect REQUEST-XXX.yintian-request    # 也识别 NOTICE.yintian-notice 退回通知
python scripts/fill.py vault-status --request REQUEST-XXX.yintian-request
printf '%s' "$JSON" | python scripts/fill.py vault-stage --answers - --confirmation-out "$WORK/change-1.yintian-confirmation"
python scripts/fill.py vault-apply --confirmation "$WORK/change-1.yintian-confirmation"
python scripts/fill.py vault-preview REQUEST-XXX.yintian-request --mapping "$WORK/map.json" --confirmation-out "$WORK/submit-1.yintian-confirmation"
python scripts/fill.py vault-fill REQUEST-XXX.yintian-request --confirmation "$WORK/submit-1.yintian-confirmation" --out-dir OUTPUT_DIR
python scripts/fill.py receipt-inspect RECEIPT.yintian        # 辨认回执属于哪个任务/哪次提交
```

`inspect` 会在公钥不一致或已过保存期限时给出 `stop_reason`，已过截止但仍可提交时给出 `deadline_notice`；返回的 `verify_hint` 要求与发放方带外核对 `task_id` 尾 6 位与 `key_fingerprint`，不一致即停手。收到 `.yintian-notice` 退回/补正通知时 `inspect` 直接返回结构化原因与下一步。`vault-status` 的 `same_type_entries` 列出与缺项类型相同的现有条目，供 Agent 提出显式映射；脚本自身绝不按类型代用。

`vault-stage` 推荐经标准输入传入 answers JSON（明文不落盘）；也接受 `0700` 目录中的 `0600` 明文临时 JSON，读后即删。两种方式的输出都是加密确认文件；`vault-apply` 只应用员工看过的变更，且只校验本次要写的条目在确认后未被改动（补录无关条目不作废）。`vault-preview` 在必填缺项时返回 `ready: false` 与已匹配值、缺项和候选条目（不写确认文件）；就绪时生成 30 分钟有效的确认文件，绑定请求包、映射、完整取值和旧回执——取值或请求变化才要求重新预览，无关保险柜变更不再影响提交确认。

默认保险柜存放在系统用户数据目录，密钥存放在独立的系统用户密钥目录；`YINTIAN_VAULT_DIR`/`YINTIAN_VAULT_KEY_DIR` 可重定向。CLI 自定义 `--vault`/`--key-file` 后本会话每条 `vault-*` 命令都要带同一组参数。

输出回执名为 `姓名-随机短码.yintian`，姓名可见，内容仍端到端加密。每次成功提交都会写入本地提交登记，更正/补交时 preview 与 fill 自动沿用该任务最近一次回执（`previous_source: "registry"`），无需员工找旧文件；`--fresh` 强制生成全新记录，`--previous` 手动指定优先于登记。输出目录已有同一任务旧回执且未用旧回执时会提示改用 `--previous`。员工本人发送回执，Agent 不自动外发。

可选识别：`vault-scan` 输出标准字段 id（`name/id_number/phone/address`，并由身份证号派生 `birth_date/gender`）的候选。VLM 由用户通过 `vlm-setup --model MODEL [--revision REVISION]` 选择，安装到独立环境和系统缓存；SafeFill 不限定模型或 revision。识别时使用 `vault-scan 图片 --vlm --model MODEL [--revision REVISION]`。安装需要联网，运行使用本地模型；普通 OCR 与 VLM 依赖相互隔离。
