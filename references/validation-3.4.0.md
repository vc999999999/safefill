# 3.4.0 验收记录

日期：2026-09-08。环境：macOS arm64、Python 3.11.15。仅使用合成身份、合成密码与生成的图片/PDF。

## 实测结果

| 检查 | 结果 |
|---|---|
| 新建虚拟环境安装受测锁 | 39 个依赖安装成功 |
| 全量 pytest | **149 passed，5 subtests passed**；81.05 秒，无跳过 |
| 原直接运行入口 | **9 个模块通过，0 跳过** |
| 独立填写发布包 | 两次 ZIP 字节一致；脱离父项目的新进程完成模板→保险柜→密文→HR 复核 |
| MCP stdio | 真实会话与工具调用通过；越界请求被拒绝；无新增敏感接口 |
| Tk 图片/PDF 组件 | 翻页、缩放、旋转、逐项确认与超时关闭通过 |
| 两份 Skill 结构 | quick_validate 通过，各 36 行 |
| doctor | 核心依赖可用；Tk/PDF 可用；OCR 后端未安装 |

原始检查记录保存于交付工作区的 `yintian-logic-review-2026-09-08/`。旧失败日志保留，未删除失败后重新计分。本轮先观察基线失败，再修复并回归；Agent 场景规范没有被计入上述 Python 测试成绩。

| 证据文件 | SHA-256 |
|---|---|
| before-fix.log | `2060046659793f54a4c1a079df588e52bc20a531ceedbc0b29409641ceceb52b` |
| clean-install.log | `9978c5cf2fc216a09905f3eeca258c6044fae2ff6441e02c2feb52e3dd0b2c41` |
| clean-env-tests.log | `469978987cdc6541efd81e5fff72a323ca48a6737293d9409ca387decb51f358` |
| delivery-checks.log | `630ea831680b06954d0c6be8283b872a083e3496e94be22cc3646bd7dfd544a9` |
| final-pytest.log | `0f7b147f208f11e26217f98e714e4a505af4559e139f9301e9a8b1082ed88184` |
| final-runner.log | `cfbf2119df4343d993f69564b80066d9cd77e1711752413fa76cda4c7cbdc707` |
| final-window.log | `7ccd3e859f96cf61be2ad762bc40baadf276ff27c8eceb8926b66833a6b6337c` |
| final-doctor.json | `e148197027fb96728f312644438ad04bb2277a0267c2c2fa11fd7a8bcf0e3485` |
| check_window.py | `8c849fff07e024ea452459643398d01ac77945f8fa357d538415ed77b18d8e27` |

## 可复现命令

在独立环境安装 `requirements-tested-py311.txt`，然后从源码仓库执行：

```bash
python -m pip install -r requirements-tested-py311.txt
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts:yintian-fill/scripts python -m pytest -q -p no:cacheprovider scripts yintian-fill/scripts
python scripts/run_tests.py
python scripts/package_skill.py --out dist/yintian-3.4.0.zip
```

Windows 可直接使用 `python scripts/run_tests.py`；本次未在 Windows 实机运行。测试源码保留在仓库，发布包只包含运行与使用所需文件。

## 证据边界

- 收件、修改和篡改用真实 AES-GCM/RSA 加密、HMAC、SQLite 事务以及系统锁；不用“最后回答正确”替代协议验证。
- 填写端端到端测试把合成值保存在加密保险柜，由模拟本人输入通过 CLI 确认，输出真实密文，HR 收件、复核并读取生成的 Excel；不读取真实个人文件。
- 独立发布测试将填写 Skill 从 ZIP 中单独取出，移除解压的父项目、移除 PYTHONPATH 后启动新 Python 进程，完成模板检查、保险柜及提交闭环。两次打包比较完整 ZIP 字节。
- MCP 测试使用真实 stdio 子进程、会话初始化与工具调用，检查收件/状态/报告/邀请检查、越界拒绝及工具列表不含解密与人工放行。
- Tk 组件验证使用真实 Pillow、PDFium 与 Tk 控件，自动点击翻页、缩放、旋转与逐项确认。超时测试核查配置为 300000ms，再缩短测试计时触发关闭；没有实际等待五分钟。
- OCR 业务规则用可控输出覆盖漏识别、多候选、低置信度和冲突；本机未安装 rapidocr-openvino，不能计作真实 OCR 冒烟或准确率测试。
- `evals/evals.json` 的 23 个收集者场景与填写端的 10 个场景是更新后的行为规范，未执行真实模型批量评测，不报告通过率或 token 成绩。

## 待验证

Windows/Linux 实机、真实群投递回执、真实员工使用效果、实际证件识别、Intel GPU/NPU/INT8 与比赛平台。当前结果不构成跨平台或工业认证。
