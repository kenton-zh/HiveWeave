# Changelog

本仓库采用 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式维护版本记录。
版本号待首次正式发布时确定（当前为 Unreleased）。

## [Unreleased]

### Added
- 后端（2026-08-04）：
  - conversation 压缩加固：原子持久化（DELETE+INSERT 合并为 execute_transaction）、去重+冷却防失败循环、token 预算对齐模型 `max_output_tokens`、专用压缩模型路由（`HIVEWEAVE_COMPACTOR_MODEL_ID`）与降级链
  - db/project.py 新增 `execute_transaction`（BEGIN IMMEDIATE + per-workspace 写锁）与 `get_workspace_write_lock`；memory/lessons/save_memory 写路径接入写锁
  - inbox 修复 aiosqlite Row `.get()` 崩溃（静默吞错 → 改为索引访问 + 显式日志）
  - telemetry 新增压缩/持久化失败计数器与 `bump()` 通用计数
- 测试（2026-08-04）：`tests/test_compaction_hardening.py` 覆盖原子回滚、去重冷却、预算重试、并发事务串行化；`test_test18_fix_regressions.py` 增补 inbox 回归用例

### Fixed
- 文档-代码漂移（2026-08-04）：README / README.zh / TECH_STACK 中「交接继承」「三层记忆」宣称与实现不符——修正为「设计目标，尚未实现」（agent 私有层激活；项目/归档层未接线）。ADR-006..010 为本地文档（`docs/` 不入库），仅文字引用（README 提及 ADR-010 编号，无链接）
