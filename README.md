# SafeFill Skills

SafeFill 是一套无需预先名单的私密信息收集流程：

```text
HR 说明用途和字段
  → safefill-collect 生成统一模板
  → 员工用 safefill-fill 填写并生成加密回执
  → 员工本人发送回执
  → safefill-collect 解密并汇总 Excel
```

两个可独立安装的 Skill 位于 `skills/safefill-collect` 和 `skills/safefill-fill`。新任务默认使用开放模板：HR 不准备名单、凭据或人工密码，员工回执文件名显示姓名，文件内容仍加密；附件导出到 Excel 同名目录。员工更正时携带本人上一次回执，收集端只采用同一随机编号的最新通过版本。开放模板保护内容机密性，但身份是自报的；需要强身份绑定时才使用兼容的名单凭据模式。

开发验证：

```bash
python -m pytest skills/safefill-collect/scripts -q -p no:cacheprovider
node skills/safefill-collect/tests/browser_contract.cjs
python skills/safefill-collect/scripts/package_skill.py --out /absolute/path/safefill-skills.zip
```
