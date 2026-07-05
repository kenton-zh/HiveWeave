# 跨 AI / 跨对话交接日志

> **本文件是迁移工程的"值班日志"。** 任何 AI 工具接入或离开迁移工作时，必须在此追加记录。
> 只追加，不删除，保持完整历史。最新的记录在最上面。

---

## 接入记录模板

```
### [接入] YYYY-MM-DD HH:MM | AI工具名 | 会话ID
- 接入原因：
- 打算做什么：
- 当前 progress.md 状态：
- 备注：
```

## 完成记录模板

```
### [完成] YYYY-MM-DD HH:MM | AI工具名 | 会话ID
- 做了什么：
- 改了哪些文件：
- 遇到的问题：
- 下一个接手需要注意：
- progress.md 已更新：是/否
```

---

## 日志

### [完成] 2026-07-05 | TRAE (Claude) | Phase 1 迁移路径规划
- 做了什么：完成 Phase 1 迁移路径规划，包含模块依赖图、5 批次迁移顺序、目录结构、并行对比测试策略、风险评估
- 改了哪些文件：
  - 创建 `migration-plan.md`（迁移路径规划文档，~330 行）
  - 更新 `progress.md`（Phase 0 → ✅ 完成，Phase 1 → ✅ 完成）
- 核心内容：
  - 技术栈确认：Python 3.12 + FastAPI + LangGraph + Pydantic v2 + aiosqlite + httpx
  - 目录结构：`apps/hiveweave-py/src/hiveweave/` 下分 db/llm/tools/conversation/agents/services/prompts/realtime/api/utils 11 个包
  - 依赖图：4 层（基础设施 → 核心服务 → 业务逻辑 → 编排 → 接入层）
  - 5 批次：批次1(14模块并行) → 批次2(3模块) → 批次3(1模块) → 批次4(2模块) → 批次5(集成验证)
  - 关键路径：14.5 天（单人全职），总工时 25 天
  - 并行对比测试：相同输入对比输出，LLM 输出只比工具调用序列，DB 状态精确对比
  - 切换标准：功能+性能(P50≤Elixir×1.5)+稳定性(24h无崩溃)+兼容性(前端无感)
- 下一个接手需要注意：
  - Phase 0 和 Phase 1 均已完成
  - 下一步是 Phase 2：Python 骨架搭建（批次 1.1 项目骨架 + 1.2 两层 SQLite）
  - 5 个待定项已在 migration-plan.md 末尾列出，建议在 Phase 2 开始前确认
  - 19 个功能契约位于 `docs/migration/feature-contracts/`，用户尚未逐个确认（progress.md 中用户确认列全为 ❌）
- progress.md 已更新：是

### [完成] 2026-07-05 | TRAE (Claude) | Phase 0 架构审查 + RECONCILE + 补写 6 契约
- 做了什么：
  1. 用户发现 2 个缺失模块（企业公告板/Charter、用户参与度配置）
  2. 3 个并行 Explore 审计发现 17 个服务层遗漏 + 16 项提示词遗漏 + 62 个 HTTP 端点无契约
  3. 补写 6 个新契约（14-19）：Charter/SystemState/Observability/Roster-WorkLog-ChatMsg/CRUD服务/HTTP API
  4. 大幅修订契约 13（ETHOS 提示词）：补全 16 项遗漏（消息布局修正、6 个 executor 子函数、CAVEMAN 纪律、6 种组织范式、7 阶段生命周期等）
  5. 执行 RECONCILE：3 组并行处理 49 项审查发现（31 有效可操作 + 8 有效权衡 + 7 契约误读 + 3 噪声）
  6. known-issues.md 新增 11 项架构审查发现（A1-A11），含 3 个 🔴 严重问题
- 改了哪些文件：
  - 创建 `feature-contracts/14-charter.md` 到 `19-http-api.md`（6 个新契约）
  - 重写 `feature-contracts/13-prompt-ethos.md`（193→400 行）
  - 修复 `feature-contracts/01-12` 共 12 个契约（RECONCILE 修复）
  - 更新 `known-issues.md`（新增 A1-A11）
  - 更新 `progress.md`
- 关键发现：
  - 🔴 A1: compaction summary 被 clean_messages 删除（Elixir 真实 bug，数据丢失）
  - 🔴 A2: 默认 permission mode 安全漏洞（未知 mode 返回 :allow）
  - 🔴 A3: run_command 绕过自毁命令检查
  - 🔴 契约 11 事实错误：project.db → data.db；Meta DB 缺 agents 表
- 下一个接手需要注意：
  - 19 个契约草稿全部完成，但用户尚未逐个确认
  - known-issues.md 现有 22 项问题（11 原始 + 11 架构审查），Python 实现时必须剔除
  - 5 个孤儿 schema 已决定：3 保留 2 删除
  - time-context 注入不实现，userInvolvement 默认 medium
- progress.md 已更新：是

### [完成] 2026-07-05 | TRAE (Claude) | Phase 0 功能契约盘点
- 做了什么：完成全部 13 个模块的功能契约草稿
- 改了哪些文件：
  - 创建 `feature-contracts/00-template.md`（契约模板）
  - 创建 `feature-contracts/01-llm-streaming.md` 到 `13-prompt-ethos.md`（13 个契约文件）
  - 更新 `progress.md`（Phase 0 → 🔄 进行中，13/13 草稿完成）
- 研究方法：
  - 模块 01-04：直接读 Elixir + TS + OpenCode 源码
  - 模块 05-13：并行 3 个 Explore subagent 研究 9 个模块（记忆/收件箱/交接、游戏时间/权限审批、Git worktree/MCP/实时通信）
- 关键发现：
  - 01 LLM 流式：Elixir 30s 超时 vs TS 三层防线（180s/90s/60s/300s），Python 采用 TS 三层防线
  - 02 工具执行器：Elixir 73 个 dispatch 分支 vs TS 68 个，以 Elixir 为准
  - 03 对话历史：OpenCode compaction 是 HiveWeave TS 的源头，tail_turns=2
  - 04 多 agent：OpenCode 无此功能，以 Elixir 为 P0 参考
  - 06 交接：Elixir 有 context_delivered 防重复注入 + create_handoff 去重，TS 无
  - 08 权限：Elixir readonly 22 个工具 vs TS 5 个，审批超时 120s vs 300s
  - 10 MCP：Elixir 简化为 HTTP-only（E1），Python 用官方 mcp SDK 支持 stdio+HTTP
- 下一个接手需要注意：
  - 所有 13 个契约文件状态为"草稿"，等待用户逐模块确认
  - 用户确认后标记为"已确认"才能进入实现阶段
  - 契约文件位于 `docs/migration/feature-contracts/` 目录
  - 下一步：等待用户审查契约，或开始 Phase 1（迁移路径规划）
- progress.md 已更新：是

### [完成] 2026-07-05 | TRAE (Claude) | 4项常量确认
- 做了什么：以 `D:\PC_AI\Project\opencode` 为 P0 参考源，确认 4 项 ⚠️ 待定常量
- 改了哪些文件：
  - `constants.md`：4 项 ⚠️ 全部改为 ✅，补充 OpenCode 对照值列，记录额外发现（Elixir token_utils.ex 的 dead code `@default_tail_turns 20`）
  - `README.md`：新增"参考源码优先级"章节（P0 OpenCode → P1 TS → P2 Elixir），列出关键 OpenCode 文件
  - `decision-record.md`：新增"决策 0：以 OpenCode 为 P0 参考源"
  - `progress.md`：新增"前置确认项"表格，4 项全部 ✅
- 确认结果：
  - `tail_turns = 2`（OpenCode 默认）
  - 停滞阈值 = Elixir 双阈值 5min/10min（OpenCode 无此机制）
  - 端口 = 4000（前端兼容性）
  - DB 连接池 = 单连接（对齐 OpenCode Effect SqlClient）
- 下一个接手需要注意：
  - 4 项常量已全部确认，Phase 0 障碍清除
  - **OpenCode 路径 `D:\PC_AI\Project\opencode` 是 P0 参考源**，所有 AI 工具遇设计决策不确定时先查 OpenCode
  - 下一步：开始 Phase 0 功能契约盘点，创建 `feature-contracts/00-template.md` 模板
- progress.md 已更新：是

### [完成] 2026-07-05 | TRAE (Claude) | 初始会话
- 做了什么：建立迁移工程的本地追踪文件结构 + Git 检查点
- 改了哪些文件：
  - 创建 `docs/migration/` 下 6 个追踪文件
  - 清理临时脚本（`_fix_addagent.cjs`、`_inspect.js`、`create_summary.py`、`PoE2LI_*`、空 `SUMMARY.md`）
  - 更新 `.gitignore` 增加 `_fix*.cjs`、`_fix*.js`、`_inspect*.js`、`*_chat_history.*` 等模式
  - 在 `main` 分支提交 commit `3e7282e` "checkpoint: pre-migration snapshot"
  - 创建并切换到分支 `migration/elixir-to-python`
- 遇到的问题：无
- 下一个接手需要注意：
  - **当前已在 `migration/elixir-to-python` 分支上**，所有迁移工作在此分支进行
  - `main` 分支 `3e7282e` 是冻结点，源码不再修改
  - 如需回退：`git checkout main`
  - 下一步：等待用户确认是否开始 Phase 0（功能契约盘点），或先确认 4 项 ⚠️ 待定常量
- progress.md 已更新：否（尚未开始实际迁移工作）

### [接入] 2026-07-05 | TRAE (Claude) | 初始会话
- 接入原因：迁移工程初始化
- 打算做什么：建立迁移工程的本地追踪文件结构，不开始实际迁移工作
- 当前 progress.md 状态：刚创建，全部未开始
- 备注：
  - 已完成根目录 `backend-stack-comparison.html` 的三方对比调研
  - 已在 clawhub 找到 Migration Compass 和 Codebase Migration Planner 两个迁移专用 skill
  - 已与用户确认三层对照策略（功能契约/常量/已知问题）避免迁移 bug
  - 已与用户确认并发上限 100 agent，影响并发模型选型
  - 下一步：等待用户确认是否开始 Phase 0（功能契约盘点），逐模块过审

---
