# 进度追踪表

> **每次工作后必须更新本文件。** 任何 AI 工具接入前先读本文件了解当前状态。

## 全局进度

| 阶段 | 状态 | 进度 | 最后更新 |
|---|---|---|---|
| Phase 0: 功能契约盘点 | ⏳ 未开始 | 0/13 模块 | 2026-07-05 |
| Phase 1: 迁移路径规划 | ⏳ 未开始 | — | — |
| Phase 2: Python 骨架搭建 | ⏳ 未开始 | — | — |
| Phase 3: 逐模块迁移 | ⏳ 未开始 | 0/13 模块 | — |
| Phase 4: 并行验证 | ⏳ 未开始 | — | — |
| Phase 5: 切换上线 | ⏳ 未开始 | — | — |

## 模块进度（Phase 0：功能契约盘点）

| # | 模块 | 状态 | 契约文件 | 用户确认 | 最后更新 | 备注 |
|---|---|---|---|---|---|---|
| 01 | LLM 流式调用 | ⏳ 未开始 | `feature-contracts/01-llm-streaming.md` | ❌ | — | 含 SSE 解析、多 provider、三层超时 |
| 02 | 工具执行器 | ⏳ 未开始 | `feature-contracts/02-tool-executor.md` | ❌ | — | 73 个 dispatch、权限矩阵 |
| 03 | 对话历史与压缩 | ⏳ 未开始 | `feature-contracts/03-conversation-store.md` | ❌ | — | token budget、compaction、doom loop |
| 04 | 多 agent 编排 | ⏳ 未开始 | `feature-contracts/04-agent-orchestration.md` | ❌ | — | trigger、级联、escalation |
| 05 | 三层记忆 | ⏳ 未开始 | `feature-contracts/05-memory-service.md` | ❌ | — | project/agent/archive、缓存失效 |
| 06 | 收件箱与交接 | ⏳ 未开始 | `feature-contracts/06-inbox-handoff.md` | ❌ | — | priority、状态机、去重 |
| 07 | 游戏时间 | ⏳ 未开始 | `feature-contracts/07-game-time.md` | ❌ | — | 3600秒/天、alarms、停滞检测 |
| 08 | 权限与审批 | ⏳ 未开始 | `feature-contracts/08-permission-approval.md` | ❌ | — | 异步审批、glob 规则 |
| 09 | Git worktree | ⏳ 未开始 | `feature-contracts/09-git-worktree.md` | ❌ | — | 7 操作、coordinator-only |
| 10 | MCP 与技能 | ⏳ 未开始 | `feature-contracts/10-mcp-skill.md` | ❌ | — | stdio+HTTP、技能绑定 |
| 11 | 两层 SQLite | ⏳ 未开始 | `feature-contracts/11-database.md` | ❌ | — | meta+per-project、journal mode |
| 12 | 实时通信 | ⏳ 未开始 | `feature-contracts/12-realtime-channel.md` | ❌ | — | PubSub、Channel、状态广播 |
| 13 | ETHOS 提示词 | ⏳ 未开始 | `feature-contracts/13-prompt-ethos.md` | ❌ | — | 三层、involvement、角色纪律 |

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
