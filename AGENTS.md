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
2. **Per-project DB** — one per workspace, **DELETE journal mode**. Project-scoped tables: `agents`, `memories`, `chat-messages`, `handoffs`, `inbox`, `conversation-turns`, etc.

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

`REAL_SECONDS_PER_GAME_DAY = 3600` (1 real hour per game day). Game seconds use 86400/day. 5s tick persists time + fires alarms. Stalled agents (15+ min) escalate.

## Environment variables

- `HIVEWEAVE_OPENCODE_API_KEY` — OpenCode API key (required)
- `HIVEWEAVE_META_DB_PATH` — override Meta DB path
- `HIVEWEAVE_API_KEY` — API key auth (unset = open)
- `HIVEWEAVE_CORS_ORIGINS` — CORS whitelist

## Frontend

React 19 + Zustand (`store.ts`). React Flow org chart. Key panels: ChatPanel, OrgTree, AgentNode. API via `api.ts` → `/api/*`. WebSocket via `phoenix.js`. Electron entry: `apps/web/electron/main.cjs`.

## Cursor Cloud specific instructions

Linux cloud env. Ignore the Windows `.bat` scripts and the `.nvmrc`/Node-22-portable-PATH hack from `## Node version` above — the VM already has Node 22, `pnpm`, and `uv` (uv is on the login PATH via `~/.profile`). The startup update script runs `uv sync --extra dev --directory apps/hiveweave-py` + `pnpm install`, so deps are already installed.

Run services (do NOT use the `.bat` files):
- Backend: `uv run uvicorn hiveweave.main:app --host 0.0.0.0 --port 4000` from `apps/hiveweave-py`.
- Frontend: `pnpm dev` from repo root (Turbo → Vite on 5173). Vite proxies `/api` and the Phoenix WebSocket to `localhost:4000`, so start the backend first.

Gotchas:
- Backend boots fine with no LLM key (logs `seed_default_model_no_api_key` warning, but startup completes). Agents can't actually think/act until a model+key is configured — either set `STEP_API_KEY` (plain env, NOT `HIVEWEAVE_`-prefixed; read by `seed_default_model`) before boot, or add a model in-app via Settings. Not needed just to create projects / load the UI.
- Tests: `uv run pytest tests/` — all pass in <1s, but the process hangs at teardown (lingering game-time loop / async task), so wrap it: `timeout 120 uv run pytest tests/ -q`.
- Typecheck: `mypy` is NOT a declared dependency. Run it with `uv run --with mypy mypy src/hiveweave/ --ignore-missing-imports` (from `apps/hiveweave-py`). There are ~56 pre-existing type errors — not a regression.
- No ESLint/ruff config exists; frontend "build" typecheck is `pnpm --filter @hiveweave/web build` (`tsc -b && vite build`).
- UI project creation uses a folder-picker modal: navigate to the parent dir (type the parent path, Enter) then click the target folder in the list. Typing the full target path directly resets the picker.
- SQLite DBs auto-create: Meta DB at `apps/hiveweave-py/data/hiveweave.db`; per-project DB at `<workspace>/.hiveweave/data.db`.
