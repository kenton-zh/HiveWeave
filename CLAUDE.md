# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# 前端 (Node.js + pnpm)
pnpm install              # 安装前端依赖
pnpm dev                  # 启动 web dev server (turbo, port 5173)
pnpm build                # 构建 web

# 后端 (Python + uvicorn)
cd apps/hiveweave-py
uv sync                   # 安装 Python 依赖 (或 pip install -e .)
uvicorn hiveweave.main:app --host 0.0.0.0 --port 4000 --limit-concurrency 100 --backlog 2048 --timeout-keep-alive 30

# 或用启动脚本 (Windows)
start-all.bat             # 后端 4000 + 前端 5173
start-backend.bat         # Python/FastAPI, 端口 4000
start-frontend.bat        # React/Vite, 端口 5173
```

### Node version

前端需要 Node `>=22.0.0 <24.0.0`。系统同时装有 Node 24 (全局) 和 Node 22 (便携版, `%LOCALAPPDATA%\Programs\node-v22.20.0-win-x64`)。运行 pnpm/node 命令前,将 Node 22 加入 PATH:

```bash
export PATH="$LOCALAPPDATA/Programs/node-v22.20.0-win-x64:$PATH"
```

## Architecture

### 项目结构

```
apps/hiveweave-py/     @hiveweave Python 后端 — FastAPI (port 4000)
apps/web/              @hiveweave/web   React 19 + Vite + React Flow (port 5173)
```

后端是纯 Python,前端是纯 React。前端通过 pnpm workspace 管理,后端通过 uv 管理。

### Python 后端 (`apps/hiveweave-py/`)

FastAPI + uvicorn,运行在端口 4000。核心模块:

| 目录 | 职责 |
|------|------|
| `src/hiveweave/api/` | FastAPI 路由 (19 个模块, 96 路由) |
| `src/hiveweave/agents/` | Agent + Supervisor + trigger |
| `src/hiveweave/llm/` | LLM 流式调用 (streamer, provider, retry, circuit_breaker) |
| `src/hiveweave/tools/` | 工具执行器 + 11 个内置工具 |
| `src/hiveweave/services/` | 23 个业务服务 (org, dispatch, memory, handoff, ...) |
| `src/hiveweave/conversation/` | 对话历史 + token budget + compaction |
| `src/hiveweave/db/` | Meta DB + per-project DB (aiosqlite) |
| `src/hiveweave/realtime/` | WebSocket (phoenix_adapter, channels, pubsub, event_bus) |
| `src/hiveweave/prompts/` | ETHOS 提示词体系 |
| `src/hiveweave/config.py` | pydantic-settings 配置 |
| `src/hiveweave/main.py` | FastAPI app + lifespan |

### Dual-DB pattern

两层 SQLite:

1. **Meta DB** (`apps/hiveweave-py/data/hiveweave.db`, WAL mode) — 全局表: `projects`, `agent-templates`, `llm-models`, `global-settings`。每个服务器进程一个。
2. **Per-project DB** (每个工作区一个, DELETE journal mode) — 项目级表: `agents`, `memories`, `chat-messages`, `handoffs`, `inbox` 等。按工作区隔离。

`ensureProjectDb(workspace_path)` 懒创建 per-project DB。

### LLM 流式调用

`apps/hiveweave-py/src/hiveweave/llm/streamer.py` — httpx 流式 SSE,支持多 provider:
- `provider.py`: provider 工厂,映射 `openai`/`anthropic`/`google` 到对应 API
- `retry.py`: 429/503/504/529 重试,指数退避 + jitter,解析 `Retry-After`
- `circuit_breaker.py`: 熔断器,探针锁防止多 Agent 同时冲击不稳定 API
- Token 估算: char-ratio 启发式 (4 chars/token EN, ~1.5 CJK),预留 20K compaction buffer

### 对话管理

`apps/hiveweave-py/src/hiveweave/conversation/store.py`:
- **Token-budget 裁剪**: 按 token 预算裁剪历史,不按消息数。Turn 级裁剪 — 不拆分 `assistant(tool_calls)` / `tool(result)` 对
- **智能压缩**: 旧 turn 被淘汰时,`compaction.py` 通过 LLM 摘要为结构化 handoff,prepend 到近期历史
- **懒加载**: 历史从 DB 首次访问时加载,之后内存缓存

### 工具系统

`apps/hiveweave-py/src/hiveweave/tools/executor.py` — 11 个内置工具: `bash`, `file`, `patch`, `grep`, `websearch`, `review`, `question`, `todowrite`, `security`。权限矩阵控制:
- **Coordinator**: 只读文件,不能写代码
- **Executor**: 可读写代码,运行测试

MCP 集成在 `apps/hiveweave-py/src/hiveweave/services/mcp.py`。

### 实时通信

`apps/hiveweave-py/src/hiveweave/realtime/phoenix_adapter.py` — 兼容前端 phoenix.js WebSocket 协议 (`/socket/websocket`)。3 个 channel: lobby, project, agent。

### Agent 类型与组织

- **Coordinator** (架构师/经理): 可读下级日志/代码,审批工作,spawn/dismiss agent。不能写代码。
- **Executor** (叶子 Agent): 可读写代码,运行测试,写工作日志。不能 spawn 下级。

CEO (root) 每项目自动创建。HR (CEO 下级) 负责招聘。Expert agents 按需调用。

### Game time

模拟项目时间,`REAL_SECONDS_PER_GAME_DAY = 3600` (1 真实小时 = 1 游戏天)。5 秒 tick 持久化时间并触发到期告警。停滞 Agent (15+ 分钟无活动) 触发升级到上级。

### 前端

React 19 + Zustand (`store.ts`)。React Flow 渲染组织架构图。关键面板: ChatPanel, OrgTree, AgentNode, GoalsPanel, QuestionDialog。API 调用通过 `api.ts` → FastAPI 路由 (`/api/*`)。WebSocket 通过 phoenix.js。Electron 桌面端支持 (`apps/web/electron/main.cjs`)。

### 环境变量

- `HIVEWEAVE_OPENCODE_API_KEY` — OpenCode API key (所有 AI 请求)
- `HIVEWEAVE_META_DB_PATH` — 覆盖 Meta DB 路径 (默认: `apps/hiveweave-py/data/hiveweave.db`)
- `HIVEWEAVE_API_KEY` — API key auth (未设则开放)
- 其他 provider keys: `HIVEWEAVE_OPENAI_API_KEY`, `HIVEWEAVE_ANTHROPIC_API_KEY` 等

### 网络代理

开发环境 HTTP/HTTPS 代理: `http://192.168.110.26:7890`

需要网络访问的工具（pip, uv, httpx, curl 等）配置环境变量:
```bash
export HTTP_PROXY=http://192.168.110.26:7890
export HTTPS_PROXY=http://192.168.110.26:7890
```

## Migration history

本项目从 Elixir/Phoenix + Node.js/Fastify 双后端迁移到 Python/FastAPI 单后端。迁移文档在 `docs/migration/`。

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool. When in doubt, invoke the skill.

Key routing rules:
- Product ideas/brainstorming → invoke /office-hours
- Strategy/scope → invoke /plan-ceo-review
- Architecture → invoke /plan-eng-review
- Design system/plan review → invoke /design-consultation or /plan-design-review
- Full review pipeline → invoke /autoplan
- Bugs/errors → invoke /investigate
- QA/testing site behavior → invoke /qa or /qa-only
- Code review/diff check → invoke /review
- Visual polish → invoke /design-review
- Ship/deploy/PR → invoke /ship or /land-and-deploy
- Save progress → invoke /context-save
- Resume context → invoke /context-restore
- Author a backlog-ready spec/issue → invoke /spec
