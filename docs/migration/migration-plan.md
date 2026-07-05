# 迁移路径规划（Phase 1）

> **本文件定义 Elixir→Python 迁移的模块顺序、依赖关系、并行策略。**
> 遵循 Migration Compass 三定律：Never Big-Bang / Parallel Before Replace / Every Step Deployable。

## 技术栈（决策 4 已确认）

| 组件 | 版本 | 职责 |
|---|---|---|
| Python | 3.12+ | 运行时 |
| FastAPI | 0.115+ | Web 框架（HTTP + WebSocket + SSE） |
| LangGraph | 0.2+ | agent 编排、状态机、检查点 |
| Pydantic | v2 | 数据校验、结构化输出 |
| aiosqlite | 0.20+ | 异步 SQLite |
| SQLAlchemy | 2.0+ | ORM（可选，schema 定义用） |
| httpx | 0.27+ | LLM 流式调用、HTTP 客户端 |
| OpenTelemetry | 1.20+ | 可观测性 |
| Redis（可选） | 7+ | PubSub 广播（多实例时） |
| uvicorn | 0.30+ | ASGI 服务器 |

## 目录结构

```
apps/hiveweave-py/
├── pyproject.toml
├── alembic.ini              # DB migration
├── migrations/              # Alembic 迁移脚本
├── src/
│   └── hiveweave/
│       ├── __init__.py
│       ├── main.py          # FastAPI app + lifespan
│       ├── config.py        # 配置（环境变量、端口、DB 路径）
│       ├── deps.py          # FastAPI 依赖注入
│       │
│       ├── db/              # 契约 11：两层 SQLite
│       │   ├── __init__.py
│       │   ├── meta.py      # Meta DB（全局：projects/agents/llm_models/...）
│       │   ├── project.py   # Per-project DB 工厂
│       │   ├── schema.py    # SQLAlchemy/Pydantic 模型定义
│       │   └── migrations.py # 运行时迁移（ALTER TABLE）
│       │
│       ├── llm/             # 契约 01：LLM 流式调用
│       │   ├── __init__.py
│       │   ├── streamer.py  # 流式调用 + tool loop
│       │   ├── circuit_breaker.py
│       │   ├── retry.py
│       │   └── provider.py  # provider 工厂
│       │
│       ├── tools/           # 契约 02：工具执行器
│       │   ├── __init__.py
│       │   ├── executor.py  # ToolExecutor 分发
│       │   ├── bash.py
│       │   ├── file.py
│       │   ├── patch.py
│       │   ├── grep.py
│       │   ├── websearch.py
│       │   ├── review.py
│       │   ├── question.py
│       │   ├── todowrite.py
│       │   └── permissions.py # 工具级权限检查
│       │
│       ├── conversation/    # 契约 03：对话历史与压缩
│       │   ├── __init__.py
│       │   ├── store.py     # 持久化 + token budget
│       │   ├── compaction.py # LLM 摘要压缩
│       │   └── token_utils.py
│       │
│       ├── agents/          # 契约 04：多 agent 编排
│       │   ├── __init__.py
│       │   ├── runtime.py   # asyncio task 生命周期
│       │   ├── supervisor.py # DynamicSupervisor 等价
│       │   └── trigger.py   # trigger_subordinate/coordinator
│       │
│       ├── services/        # 契约 05-10, 14-18：业务服务
│       │   ├── __init__.py
│       │   ├── memory.py    # 05
│       │   ├── inbox.py     # 06
│       │   ├── handoff.py   # 06
│       │   ├── game_time.py # 07
│       │   ├── permission.py # 08
│       │   ├── approval.py  # 08
│       │   ├── git_worktree.py # 09
│       │   ├── skill_registry.py # 10
│       │   ├── mcp.py       # 10
│       │   ├── charter.py   # 14
│       │   ├── system_state.py # 15
│       │   ├── event_audit.py # 16
│       │   ├── telemetry.py # 16
│       │   ├── roster.py    # 17
│       │   ├── work_log.py  # 17
│       │   ├── chat_message.py # 17
│       │   ├── model.py     # 18
│       │   ├── template.py  # 18
│       │   ├── settings.py  # 18
│       │   ├── team_chat.py # 18
│       │   ├── names.py     # 18
│       │   ├── org.py       # 组织 CRUD
│       │   └── dispatch.py  # 任务分派
│       │
│       ├── prompts/         # 契约 13：ETHOS 提示词
│       │   ├── __init__.py
│       │   ├── identity.py  # build_identity_prompt
│       │   ├── context.py   # build_context_prompt
│       │   ├── involvement.py
│       │   ├── goals.py
│       │   ├── coordinator.py # CEO/HR/Generic
│       │   ├── executor.py  # 6 子函数分发
│       │   └── caveman.py   # CAVEMAN 风格段
│       │
│       ├── realtime/        # 契约 12：实时通信
│       │   ├── __init__.py
│       │   ├── channels.py  # WebSocket 3 channel
│       │   ├── pubsub.py    # 进程内广播 / Redis
│       │   └── event_bus.py # StatusEventBus
│       │
│       ├── api/             # 契约 19：HTTP API
│       │   ├── __init__.py
│       │   ├── router.py    # 主路由
│       │   ├── auth.py      # ApiKeyAuth
│       │   ├── health.py
│       │   ├── settings.py
│       │   ├── projects.py
│       │   ├── org.py
│       │   ├── chat.py
│       │   ├── permissions.py
│       │   ├── models.py
│       │   ├── templates.py
│       │   ├── communications.py
│       │   ├── alarms.py
│       │   ├── logs.py
│       │   ├── debug.py
│       │   └── filesystem.py
│       │
│       └── utils/
│           ├── __init__.py
│           ├── time_context.py
│           └── slugify.py
│
└── tests/
    ├── conftest.py
    ├── parallel/            # 并行对比测试
    │   ├── test_01_streaming.py
    │   ├── test_02_tools.py
    │   └── ...
    └── unit/
```

## 模块依赖图

```
Layer 0 (基础设施)
  11-database ──────┐
  15-system-state ──┤ (依赖 11)
                    │
Layer 1 (核心服务)   │
  03-conversation ──┤ (依赖 11)
  05-memory ────────┤ (依赖 11)
  06-inbox-handoff ─┤ (依赖 11)
  07-game-time ─────┤ (依赖 11)
  08-permission ────┤ (依赖 11)
  09-git-worktree ──┤ (依赖 11)
  10-mcp-skill ─────┤ (依赖 11)
  14-charter ───────┤ (依赖 11)
  16-observability ─┤ (依赖 11)
  17-roster-worklog ┤ (依赖 11)
  18-crud-services ─┘ (依赖 11)

Layer 2 (业务逻辑)
  01-llm-streaming ──── (依赖 03,08,13)
  02-tool-executor ──── (依赖 08,10,11)
  13-prompt-ethos ───── (依赖 14,05,10)

Layer 3 (编排)
  04-agent-orchestration ── (依赖 01,02,03,05,06,07,08,13)

Layer 4 (接入层)
  12-realtime ──── (依赖 04)
  19-http-api ──── (依赖 所有 service)
```

## 迁移批次（5 批，每批可独立部署验证）

### 批次 1：基础设施 + 核心服务（Layer 0 + Layer 1）

**目标**：搭建 Python 骨架，实现所有无外部依赖的数据层和服务层。

| 序号 | 模块 | 契约 | 依赖 | 预估工时 | 并行对比测试 |
|---|---|---|---|---|---|
| 1.1 | 项目骨架 | — | — | 0.5 天 | — |
| 1.2 | 两层 SQLite | 11 | — | 1 天 | DB 读写对比 |
| 1.3 | SystemState + Application | 15 | 11 | 0.5 天 | 启动流程对比 |
| 1.4 | 对话历史与压缩 | 03 | 11 | 1.5 天 | token 计算对比 |
| 1.5 | 三层记忆 | 05 | 11 | 1 天 | 读写对比 |
| 1.6 | 收件箱与交接 | 06 | 11 | 1 天 | 状态机对比 |
| 1.7 | 游戏时间 | 07 | 11 | 1 天 | 时间计算对比 |
| 1.8 | 权限与审批 | 08 | 11 | 1 天 | 权限矩阵对比 |
| 1.9 | Git worktree | 09 | 11 | 1 天 | 7 操作对比 |
| 1.10 | MCP 与技能 | 10 | 11 | 1.5 天 | 技能绑定对比 |
| 1.11 | Charter | 14 | 11 | 0.5 天 | goals sync 对比 |
| 1.12 | Observability | 16 | 11 | 0.5 天 | 事件审计对比 |
| 1.13 | Roster + WorkLog + ChatMsg | 17 | 11 | 1 天 | 持久化对比 |
| 1.14 | CRUD 服务集 | 18 | 11 | 1 天 | CRUD 对比 |

**批次 1 内部并行策略**：
- 1.1 → 1.2 必须先完成（串行）
- 1.3-1.14 可全部并行（均只依赖 11）
- 建议分 3 组并行：A组(1.3-1.6) / B组(1.7-1.10) / C组(1.11-1.14)

**批次 1 完成标志**：
- 所有 service 层可独立运行
- 并行对比测试全部通过
- Elixir DB 可被 Python 读取（data.db 兼容）

### 批次 2：业务逻辑层（Layer 2）

**目标**：实现 LLM 流式调用、工具执行器、提示词构建。

| 序号 | 模块 | 契约 | 依赖 | 预估工时 | 并行对比测试 |
|---|---|---|---|---|---|
| 2.1 | ETHOS 提示词 | 13 | 14,05,10 | 2 天 | 提示词输出对比 |
| 2.2 | LLM 流式调用 | 01 | 03,08,13 | 2 天 | SSE 流对比 |
| 2.3 | 工具执行器 | 02 | 08,10,11 | 2 天 | 73 dispatch 对比 |

**批次 2 内部并行策略**：
- 2.1 必须先完成（2.2 依赖提示词）
- 2.2 和 2.3 可并行（2.2 依赖 13 但不依赖 02；2.3 依赖 08/10 但不依赖 01）

**批次 2 完成标志**：
- 可发起单次 LLM 调用并执行工具
- 提示词与 Elixir 输出一致
- SSE 流格式与前端兼容

### 批次 3：Agent 编排（Layer 3）

**目标**：实现多 agent 生命周期管理。

| 序号 | 模块 | 契约 | 依赖 | 预估工时 | 并行对比测试 |
|---|---|---|---|---|---|
| 3.1 | Agent 编排 | 04 | 01,02,03,05,06,07,08,13 | 3 天 | 多 agent 协作场景对比 |

**批次 3 完成标志**：
- agent 可触发、执行、回报
- 空响应重试 + 升级机制工作
- 停滞检测 + 安全超时工作
- 并行对比：相同任务在 Elixir 和 Python 下 agent 行为一致

### 批次 4：接入层（Layer 4）

**目标**：实现 WebSocket 和 HTTP API。

| 序号 | 模块 | 契约 | 依赖 | 预估工时 | 并行对比测试 |
|---|---|---|---|---|---|
| 4.1 | 实时通信 | 12 | 04 | 2 天 | WS 事件对比 |
| 4.2 | HTTP API | 19 | 所有 | 2 天 | 62 端点对比 |

**批次 4 内部并行策略**：
- 4.1 和 4.2 可并行

**批次 4 完成标志**：
- 前端可无感切换到 Python 后端
- 所有 62 个 HTTP 端点响应格式一致
- WebSocket 3 channel 事件一致

### 批次 5：集成验证 + 切换

**目标**：端到端验证、性能测试、灰度切换。

| 序号 | 任务 | 依赖 | 预估工时 |
|---|---|---|---|
| 5.1 | 端到端场景测试 | 4 | 2 天 |
| 5.2 | 性能基准测试 | 5.1 | 1 天 |
| 5.3 | 灰度切换（端口 4000 切换） | 5.2 | 0.5 天 |

## 关键路径

```
1.1 → 1.2 → 1.4 → 2.1 → 2.2 → 3.1 → 4.2 → 5.1 → 5.3
```

关键路径预估：0.5 + 1 + 1.5 + 2 + 2 + 3 + 2 + 2 + 0.5 = **14.5 天**

非关键路径可并行，总工时约 **25 天**（单人全职）。

## 并行对比测试策略

### 测试框架

每个模块的并行对比测试遵循契约中的"并行对比测试方案"章节：

1. **相同输入**：Elixir 和 Python 收到相同的 API 请求 / agent 消息
2. **对比输出**：HTTP 响应 / WS 事件 / DB 状态 / SSE 流
3. **容差**：
   - LLM 输出：不做精确对比（LLM 有随机性），对比工具调用序列
   - 时间戳：±1s
   - token 计数：±5%
   - DB 状态：精确对比（JSON 深度相等）

### 测试数据

- 使用 `packages/db/data/hiveweave.db`（现有 Meta DB）作为测试数据
- 创建专用测试项目，包含 3-5 个 agent 的组织树
- 覆盖 6 种组织范式各至少 1 个场景

### 切换标准

| 维度 | 标准 |
|---|---|
| 功能 | 19 个模块并行对比测试全部通过 |
| 性能 | P50 响应时间 ≤ Elixir × 1.5 |
| 稳定性 | 连续运行 24h 无崩溃 |
| 兼容性 | 前端无感切换（无 JS 修改） |

## 风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| LangGraph 检查点机制与 Elixir GenServer 行为不一致 | 中 | 高 | 契约 04 已明确差异；Python 侧用 LangGraph checkpoint + 自建 supervisor |
| aiosqlite 单连接并发瓶颈 | 低 | 中 | 契约 11 已确认单连接模型；asyncio 序列化 + busy_timeout |
| SSE 流格式与前端不兼容 | 中 | 高 | 批次 2 完成后立即与前端联调 |
| 提示词输出差异导致 agent 行为偏移 | 中 | 高 | 批次 2 并行对比测试覆盖 9 种角色 |
| Redis 依赖（如需多实例） | 低 | 低 | 单实例进程内广播够用；预留 Redis 接口 |

## 待定项（Phase 1 需确认）

| 项 | 说明 | 建议 |
|---|---|---|
| 实时广播方案 | 进程内广播 vs Redis PubSub | 单实例用进程内广播，预留 Redis 接口 |
| supervisor 容错 | asyncio task 重启 vs multiprocessing | asyncio task + LangGraph checkpoint（100 并发够用） |
| ORM 选择 | SQLAlchemy vs 纯 SQL | SQLAlchemy 2.0（schema 定义 + 迁移），热路径用纯 SQL |
| 测试框架 | pytest vs unittest | pytest（生态更好） |
| 包管理 | uv vs poetry vs pip | uv（最快） |
