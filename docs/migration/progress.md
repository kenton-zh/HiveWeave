# 进度追踪表

> **每次工作后必须更新本文件。** 任何 AI 工具接入前先读本文件了解当前状态。

## 全局进度

| 阶段 | 状态 | 进度 | 最后更新 |
|---|---|---|---|
| Phase 0: 功能契约盘点 | ✅ 完成 | 19/19 模块 + 架构审查 + RECONCILE | 2026-07-05 |
| Phase 1: 迁移路径规划 | ✅ 完成 | 5 批次 + 依赖图 + 目录结构 + 测试策略 | 2026-07-05 |
| Phase 2: Python 骨架搭建 | 🔄 进行中 | 批次 1 完成（骨架+DB+26服务模块），批次 2 待开始 | 2026-07-05 |
| Phase 3: 逐模块迁移 | 🔄 进行中 | 批次 1 完成（14/14 子任务），批次 2-5 待开始 | 2026-07-05 |
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

### 原始 13 模块

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
| 13 | ETHOS 提示词 | 🔄 草稿v2完成 | `feature-contracts/13-prompt-ethos.md` | ❌ | 2026-07-05 | v2 大幅修订：16 项遗漏补全 |

### 补充 6 模块（架构审查后发现遗漏）

| # | 模块 | 状态 | 契约文件 | 用户确认 | 最后更新 | 备注 |
|---|---|---|---|---|---|---|
| 14 | Charter（章程+目标+参与度） | 🔄 草稿完成 | `feature-contracts/14-charter.md` | ❌ | 2026-07-05 | 企业公告板+goals sync+userInvolvement |
| 15 | SystemState + Application | 🔄 草稿完成 | `feature-contracts/15-system-state.md` | ❌ | 2026-07-05 | 暂停/恢复+启动恢复+花名迁移 |
| 16 | EventAudit + Telemetry | 🔄 草稿完成 | `feature-contracts/16-observability.md` | ❌ | 2026-07-05 | 事件审计+遥测+crash 记录 |
| 17 | Roster + WorkLog + ChatMsg | 🔄 草稿完成 | `feature-contracts/17-roster-worklog-chatmsg.md` | ❌ | 2026-07-05 | 人事+工作日志+UI消息持久化 |
| 18 | CRUD 服务集 | 🔄 草稿完成 | `feature-contracts/18-crud-services.md` | ❌ | 2026-07-05 | Model/Template/Settings/TeamChat/花名 |
| 19 | HTTP API 层 | 🔄 草稿完成 | `feature-contracts/19-http-api.md` | ❌ | 2026-07-05 | 62 端点、16 分组、ApiKeyAuth |

## 架构审查进度

| 审查项 | 状态 | 发现数 | 已处理 | 最后更新 |
|---|---|---|---|---|
| 契约 01-13 对抗式审查 | ✅ 完成 | ~90 | — | 2026-07-05 |
| 契约 14-19 补写 | ✅ 完成 | — | — | 2026-07-05 |
| 契约 13 提示词修订 | ✅ 完成 | 16 项遗漏 | 16 项已补 | 2026-07-05 |
| 交叉模型审查 | ❌ 跳过（用户决定） | — | — | 2026-07-05 |
| RECONCILE（01-13 审查发现） | ✅ 完成 | 49 | 49（31 有效可操作+8 权衡+7 误读+3 噪声） | 2026-07-05 |
| known-issues.md 更新 | ✅ 完成 | 11 项 A1-A11 | — | 2026-07-05 |

## 孤儿 schema（已决定）

| Schema | 表 | 决策 | 理由 |
|---|---|---|---|
| charter_attachment | charter_attachments | **保留**（Python 补 service） | charter 附件是有意义的功能 |
| merge | merges | **保留**（契约 09 补描述） | merge 操作的冲突记录 |
| meta_index | meta_index | **删除** | 无引用，用途不明 |
| module | modules | **保留**（Python 补 CRUD） | 被 handoff/memory 的 module_id 引用 |
| project_index | project_index | **删除** | 无引用，用途不明 |

## 已决定事项

| 事项 | 决策 | 日期 |
|---|---|---|
| 交叉模型审查 | 跳过 | 2026-07-05 |
| time-context 注入 | 不实现（Elixir 无，保持一致） | 2026-07-05 |
| userInvolvement 默认值 | 以 charter medium 为准 | 2026-07-05 |
| 孤儿 schema 处理 | 3 保留 2 删除（见上表） | 2026-07-05 |

## 模块进度（Phase 3：逐模块迁移）

> Phase 3 开始后在此表追踪每个模块的 Python 实现进度。

### 批次 1：基础设施 + 核心服务（Layer 0 + Layer 1）✅ 完成

| 序号 | 模块 | 契约 | Python 文件 | 状态 | 测试 | 最后更新 |
|---|---|---|---|---|---|---|
| 1.1 | 项目骨架 | — | `pyproject.toml`, `main.py`, `config.py` | ✅ | import OK | 2026-07-05 |
| 1.2 | 两层 SQLite | 11 | `db/schema.py`, `db/meta.py`, `db/project.py` | ✅ | DB 读写测试通过 | 2026-07-05 |
| 1.3 | SystemState | 15 | `services/system_state.py` | ✅ | 集成测试通过 | 2026-07-05 |
| 1.4 | 对话历史与压缩 | 03 | `conversation/token_utils.py`, `compaction.py`, `store.py` | ✅ | 集成测试通过 | 2026-07-05 |
| 1.5 | 三层记忆 | 05 | `services/memory.py` | ✅ | 集成测试通过 | 2026-07-05 |
| 1.6 | 收件箱与交接 | 06 | `services/inbox.py`, `services/handoff.py` | ✅ | 集成测试通过 | 2026-07-05 |
| 1.7 | 游戏时间 | 07 | `services/game_time.py` | ✅ | 集成测试通过 | 2026-07-05 |
| 1.8 | 权限与审批 | 08 | `services/permission.py`, `services/approval.py` | ✅ | 集成测试通过 | 2026-07-05 |
| 1.9 | Git worktree | 09 | `services/git_worktree.py` | ✅ | 集成测试通过 | 2026-07-05 |
| 1.10 | MCP 与技能 | 10 | `services/skill_registry.py`, `services/mcp.py` | ✅ | 集成测试通过 | 2026-07-05 |
| 1.11 | Charter | 14 | `services/charter.py` | ✅ | 集成测试通过 | 2026-07-05 |
| 1.12 | Observability | 16 | `services/event_audit.py`, `services/telemetry.py` | ✅ | 集成测试通过 | 2026-07-05 |
| 1.13 | Roster+WorkLog+ChatMsg | 17 | `services/roster.py`, `work_log.py`, `chat_message.py` | ✅ | 集成测试通过 | 2026-07-05 |
| 1.14 | CRUD 服务集 | 18 | `services/model.py`, `template.py`, `settings.py`, `team_chat.py`, `names.py` | ✅ | 集成测试通过 | 2026-07-05 |
| 补充 | 组织 CRUD | 04 | `services/org.py`, `services/dispatch.py` | ✅ | 集成测试通过 | 2026-07-05 |

**批次 1 集成验证**：30 个模块（4 DB + 3 conversation + 23 services）全部导入成功，13 个服务类实例化通过。

### 批次 2：业务逻辑层（Layer 2）✅ 完成

| 序号 | 模块 | 契约 | Python 文件 | 状态 | 测试 | 最后更新 |
|---|---|---|---|---|---|---|
| 2.1 | ETHOS 提示词 | 13 | `prompts/identity.py`, `context.py`, `involvement.py`, `goals.py`, `coordinator.py`, `executor.py` | ✅ | 集成测试通过 | 2026-07-05 |
| 2.2 | LLM 流式调用 | 01 | `llm/streamer.py`, `provider.py`, `retry.py`, `circuit_breaker.py` | ✅ | 集成测试通过 | 2026-07-05 |
| 2.3 | 工具执行器 | 02 | `tools/executor.py`, `bash.py`, `file.py`, `patch.py`, `grep.py`, `websearch.py`, `review.py`, `question.py`, `todowrite.py` | ✅ | 集成测试通过（含功能测试） | 2026-07-05 |

**批次 2 集成验证**：19 个模块（4 LLM + 10 tools + 7 prompts）全部导入成功，功能测试通过（bash 执行、文件读写、patch、grep、ToolExecutor 完整链路）。

### 批次 3-5 ⏳ 待开始

## 状态图例

- ⏳ 未开始
- 🔄 进行中
- ✅ 完成
- ⚠️ 有问题（见备注）
- ❌ 阻塞（见备注）
