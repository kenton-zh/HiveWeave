# AGENTS.md

Guidance for AI coding sessions working in this repo. Keep it terse; every line should answer "would an agent miss this without help?".

## Architecture

**Single Python backend + React frontend**:
- **Python/FastAPI backend** (`apps/hiveweave-py/`) — port **4000**. This is the only backend.
- **React frontend** (`apps/web/`) — Vite dev server on port **5173**, connects to Python backend at 4000.

## Commands

### Starting the project

```bash
# Option 1: Start everything (backend + frontend in separate windows)
start-all.bat

# Option 2: Start individually
start-backend.bat    # Python/FastAPI on port 4000
start-frontend.bat   # React/Vite on port 5173
```

### Backend (Python)

```bash
cd apps/hiveweave-py
uv sync                                          # Install deps
uvicorn hiveweave.main:app --port 4000           # Start server
```

Environment: copy `apps/hiveweave-py/.env.example` → `apps/hiveweave-py/.env`, set `HIVEWEAVE_OPENCODE_API_KEY`.

### Frontend (Node.js)

```bash
pnpm install                # Install deps
pnpm dev                    # Vite dev server :5173
pnpm build                  # Build
```

### Node version (Windows)

Required: Node `>=22.0.0 <24.0.0`. System has Node 24 (global) and Node 22 (portable at `%LOCALAPPDATA%\Programs\node-v22.20.0-win-x64`). Prepend Node 22 to PATH:

```bash
export PATH="$LOCALAPPDATA/Programs/node-v22.20.0-win-x64:$PATH"
```

## Repo shape

```
apps/hiveweave-py/   Python/FastAPI backend (port 4000)
apps/web/            @hiveweave/web  React 19 + Vite + React Flow (port 5173)
```

`pnpm-workspace.yaml` advertises `apps/*`; `turbo.json` defines `build`, `dev`, `typecheck` tasks for the web app.

## Two-tier SQLite

1. **Meta DB** — `apps/hiveweave-py/data/hiveweave.db` (WAL). Global tables: `projects`, `agent-templates`, `llm-models`, `global-settings`. Override with `HIVEWEAVE_META_DB_PATH`.
2. **Per-project DB** — one per workspace, **WAL mode**. Project-scoped tables: `agents`, `memories`, `chat-messages`, `handoffs`, `inbox`, `conversation-turns`, etc.

## Key modules (`apps/hiveweave-py/src/hiveweave/`)

| Path | Purpose |
|------|---------|
| `config.py` | pydantic-settings, env prefix `HIVEWEAVE_` |
| `main.py` | FastAPI app + lifespan (startup/shutdown) |
| `llm/streamer.py` | httpx 流式 SSE, tool loop |
| `llm/provider.py` | Provider factory (openai/anthropic/google/fallback) |
| `llm/retry.py` | 429/503/504/529 retry, exponential backoff |
| `llm/circuit_breaker.py` | 熔断器 + probe lock |
| `tools/executor.py` | ToolExecutor, 11 built-in tools, permission matrix |
| `conversation/store.py` | Token-budget trimming, turn-level, lazy-loaded |
| `conversation/compaction.py` | LLM summary of evicted turns |
| `conversation/token_utils.py` | Char-ratio token estimation |
| `realtime/phoenix_adapter.py` | Phoenix Channels WebSocket compat (`/socket/websocket`) |
| `services/org.py` | Agent CRUD, tree traversal |
| `services/dispatch.py` | Task dispatch between agents |
| `services/memory.py` | Three-layer memory |
| `services/git_worktree.py` | Per-agent worktree, checkpoint/merge/rollback |

## Agent types & org

- **Coordinator**: read subordinate logs/code, approve/reject, spawn/dismiss. Cannot write code.
- **Executor**: read/write code, run tests, write work logs. Cannot spawn sub-agents.

CEO auto-created per project. HR under CEO. Expert agents on-demand.

## Game time

`REAL_SECONDS_PER_GAME_DAY = 3600` (1 real hour per game day). Game seconds use 86400/day. 5s tick persists time + fires alarms. Stalled agents: 10min stall → nudge, ~40min+ → escalate.

## Environment variables

- `HIVEWEAVE_OPENCODE_API_KEY` — OpenCode API key (required)
- `HIVEWEAVE_META_DB_PATH` — override Meta DB path
- `HIVEWEAVE_API_KEY` — API key auth (unset = open)
- `HIVEWEAVE_CORS_ORIGINS` — CORS whitelist

## Frontend

React 19 + Zustand (`store.ts`). React Flow org chart. Key panels: ChatPanel, OrgTree, AgentNode. API via `api.ts` → `/api/*`. WebSocket via `phoenix.js`. Electron entry: `apps/web/electron/main.cjs`.
