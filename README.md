# 隐填（Yintian）

面向多人私密资料收集的双 Skill 项目：收集者负责建任务、加密收件与本地汇总，填写者负责在本人设备上选取资料并生成加密提交。

| Skill | 使用者 | 入口 |
|---|---|---|
| `yintian-skill` | HR / 行政等收集者 | [`yintian-skill/SKILL.md`](yintian-skill/SKILL.md) |
| `yintian-fill` | 提交资料的员工 | [`yintian-fill/SKILL.md`](yintian-fill/SKILL.md) |

两个目录都是可独立安装的 Skill；完整协作流程见[项目流程图](https://vc999999999.github.io/yintian/flowchart.html)。运行说明、依赖和安全边界分别保存在各 Skill 目录内。

```text
yintian/
├── yintian-skill/   # 收集者 Skill
├── yintian-fill/    # 填写者 Skill
└── .github/         # 项目测试与流程图发布
```

仓库不保存真实名单、凭据、表单明文、附件、提交文件、保险柜、导出表格或项目外发布稿。
