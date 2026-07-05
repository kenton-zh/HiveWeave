# 进度追踪表

> **每次工作后必须更新本文件。** 任何 AI 工具接入前先读本文件了解当前状态。

## 全局进度

| 阶段 | 状态 | 进度 | 最后更新 |
|---|---|---|---|
| Phase 0: 功能契约盘点 | 🔄 进行中 | 13/13 模块草稿完成 | 2026-07-05 |
| Phase 1: 迁移路径规划 | ⏳ 未开始 | — | — |
| Phase 2: Python 骨架搭建 | ⏳ 未开始 | — | — |
| Phase 3: 逐模块迁移 | ⏳ 未开始 | 0/13 模块 | — |
| Phase 4: 并行验证 | ⏳ 未开始 | — | — |
| Phase 5: 切换上线 | ⏳ 未开始 | — | — |

## 前置确认项（4 项 ⚠️ 待定常量）

| 常量 | 确认值 | 来源依据 | 状态 |
|---|---|---|---|
| `tail_turns` | `2` | OpenCode 默认值（`config.ts:156`） | ✅ 已确认 |
| 停滞阈值 | processing 5min / idle 10min | Elixir 双阈值模型（OpenCode 无此机制） | ✅ 已确认 |
| 端口 | `4000` | 前端兼容性（`api.ts:22` 硬编码 4000） | ✅ 已确认 |
| Per-project DB 连接池 | 单连接 | OpenCode Effect SqlClient 单连接模型 | ✅ 已确认 |

## 模块进度（Phase 0：功能契约盘点）

| # | 模块 | 状态 | 契约文件 | 用户确认 | 最后更新 | 备注 |
|---|---|---|---|---|---|---|
| 01 | LLM 流式调用 | 🔄 草稿完成 | `feature-contracts/01-llm-streaming.md` | ❌ | 2026-07-05 | 含 SSE 解析、多 provider、三层超时 |
| 02 | 工具执行器 | 🔄 草稿完成 | `feature-contracts/02-tool-executor.md` | ❌ | 2026-07-05 | 73 个 dispatch、权限矩阵 |
| 03 | 对话历史与压缩 | 🔄 草稿完成 | `feature-contracts/03-conversation-store.md` | ❌ | 2026-07-05 | token budget、compaction、doom loop |
| 04 | 多 agent 编排 | 🔄 草稿完成 | `feature-contracts/04-agent-orchestration.md` | ❌ | 2026-07-05 | trigger、级联、escalation |
| 05 | 三层记忆 | 🔄 草稿完成 | `feature-contracts/05-memory-service.md` | ❌ | 2026-07-05 | project/agent/archive、缓存失效 |
| 06 | 收件箱与交接 | 🔄 草稿完成 | `feature-contracts/06-inbox-handoff.md` | ❌ | 2026-07-05 | priority、状态机、去重 |
| 07 | 游戏时间 | 🔄 草稿完成 | `feature-contracts/07-game-time.md` | ❌ | 2026-07-05 | 3600秒/天、alarms、停滞检测 |
| 08 | 权限与审批 | 🔄 草稿完成 | `feature-contracts/08-permission-approval.md` | ❌ | 2026-07-05 | 异步审批、glob 规则 |
| 09 | Git worktree | 🔄 草稿完成 | `feature-contracts/09-git-worktree.md` | ❌ | 2026-07-05 | 7 操作、coordinator-only |
| 10 | MCP 与技能 | 🔄 草稿完成 | `feature-contracts/10-mcp-skill.md` | ❌ | 2026-07-05 | stdio+HTTP、技能绑定 |
| 11 | 两层 SQLite | 🔄 草稿完成 | `feature-contracts/11-database.md` | ❌ | 2026-07-05 | meta+per-project、journal mode |
| 12 | 实时通信 | 🔄 草稿完成 | `feature-contracts/12-realtime-channel.md` | ❌ | 2026-07-05 | PubSub、Channel、状态广播 |
| 13 | ETHOS 提示词 | 🔄 草稿完成 | `feature-contracts/13-prompt-ethos.md` | ❌ | 2026-07-05 | 三层、involvement、角色纪律 |

## 模块进度（Phase 3：逐模块迁移）

> Phase 3 开始后在此表追踪每个模块的 Python 实现进度。

| # | 模块 | 状态 | Python 文件 | 测试 | 并行对比 | 最后更新 |
|---|---|---|---|---|---|---|
| 01 | LLM 流式调用 | ⏳ | — | — | — | — |
| 02 | 工具执行器 | ⏳ | — | — | — | — |
| 03 | 对话历史与压缩 | ⏳ | — | — | — | — |
| 04 | 多 agent 编排 | ⏳ | — | — | — | — |
| 05 | 三层记忆 | ⏳ | — | — | — | — |
| 06 | 收件箱与交接 | ⏳ | — | — | — | — |
| 07 | 游戏时间 | ⏳ | — | — | — | — |
| 08 | 权限与审批 | ⏳ | — | — | — | — |
| 09 | Git worktree | ⏳ | — | — | — | — |
| 10 | MCP 与技能 | ⏳ | — | — | — | — |
| 11 | 两层 SQLite | ⏳ | — | — | — | — |
| 12 | 实时通信 | ⏳ | — | — | — | — |
| 13 | ETHOS 提示词 | ⏳ | — | — | — | — |

## 状态图例

- ⏳ 未开始
- 🔄 进行中
- ✅ 完成
- ⚠️ 有问题（见备注）
- ❌ 阻塞（见备注）
