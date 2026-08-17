# AGENTS.md

Guidance for AI coding sessions working in this repo. Keep it terse; every line should answer "would an agent miss this without help?".

**Canonical detail:** prefer [`CLAUDE.md`](CLAUDE.md) for architecture, commands, and pitfalls; keep this file as a short agent-facing digest and sync when those change.

**跨平台 AI 项目记忆（本地，不进远程）：** 读写仓库根目录 [`AI_MEMORY.local.md`](AI_MEMORY.local.md)（已 gitignore）——**不**使用各平台自带记忆路径或存储规则，跨会话/跨平台复用同一份。个人环境配置写 [`CLAUDE.local.md`](CLAUDE.local.md)；本文件只放可复用的项目事实（决策、坑、根因、未完成上下文），禁止密钥。详见 `CLAUDE.md` §跨平台 AI 项目记忆。

**记忆写入规范（防流水账）：** 只记**根因、仍在生效的决策、可预见的坑、未完成上下文**四类。**不记**：测试通过数/基线数字、提交哈希、过程流水、实现细节清单（提交记录与代码注释里可查）。修复完成的事务**就地更新/删除原条目**，不追加新条目；同主题条目 ≥3 或文件膨胀时**按主题压缩合并**（参考文件内「速览/关键决策/高频踩坑/未完成」结构，旧条目吸收后删除）。会话结束时若本次有新事实，先查是否已被既有条目覆盖，再决定更新还是新增。

## Workflow rules

**代码超 20 行必须子代理审计**（用户 2026-08-01 钦定）：任何一次代码编写/修改，若新增或修改的代码**合计超过 20 行**（单文件或多文件累计），必须在交付/声明完成前派子代理（code-review / general）审计，修复其发现的问题后再汇报。≤20 行的小改动可自审后直接汇报。测试代码同样适用。

**工具输出分层**（详见 `CLAUDE.md`）：截断触发 = 上游已漏。先在工具侧收成短契约（限条/限描述/禁单行 dump），大结果落盘回传句柄；`truncate_tool_output` 只是最后兜底（须按行+字符双封顶）。修截断 ≠ 修根因。

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

**PowerShell (default on this machine; paste as-is):**

```powershell
$env:PATH = "$env:LOCALAPPDATA\Programs\node-v22.20.0-win-x64;$env:PATH"
node -v   # expect v22.x
```

**bash / Git Bash / POSIX:**

```bash
export PATH="$LOCALAPPDATA/Programs/node-v22.20.0-win-x64:$PATH"
node -v
```

Or use `start-frontend.bat` / `start-all.bat` when those scripts already set up the env.

## Repo shape

```
apps/hiveweave-py/   Python/FastAPI backend (port 4000)
apps/web/            @hiveweave/web  React 19 + Vite + React Flow (port 5173)
```

`pnpm-workspace.yaml` advertises `apps/*`; `turbo.json` defines `build`, `dev`, `typecheck` tasks for the web app.

## Two-tier SQLite

1. **Meta DB** — `apps/hiveweave-py/data/hiveweave.db` (DELETE). Global tables: `projects`, `agent_templates`, `llm_models`, `global_settings`, `mcp_servers`, `meta_index`. Override with `HIVEWEAVE_META_DB_PATH`.（旧 `agent_index`/`permission_rules` 等已废弃，迁移时 DROP）
2. **Per-project DB** — one per workspace, **DELETE journal mode**（避免 Windows WAL 孤儿化/代际分叉损坏，TEST18 2026-08-05）。Project-scoped tables: `agents`, `memories`, `chat_messages`, `handoffs`, `inbox`, `work_logs` 等。按工作区隔离。

## Key modules (`apps/hiveweave-py/src/hiveweave/`)

| Path | Purpose |
|------|---------|
| `config.py` | pydantic-settings, env prefix `HIVEWEAVE_` |
| `main.py` | FastAPI app + lifespan (startup/shutdown) |
| `llm/streamer/` | httpx 流式 SSE, tool loop（包；`__init__` 再导出） |
| `llm/provider.py` | Provider factory (openai/anthropic/google/fallback) |
| `llm/retry.py` | 429/503/504/529 retry, exponential backoff |
| `llm/circuit_breaker.py` | 熔断器 + probe lock |
| `tools/executor.py` | ToolExecutor, 注册工具（数量易变，勿硬编码；5 个 legacy 评审套件）, permission matrix |
| `conversation/store.py` | Token-budget trimming, turn-level, lazy-loaded |
| `conversation/compaction.py` | LLM summary of evicted turns |
| `conversation/token_utils.py` | Char-ratio token estimation |
| `realtime/phoenix_adapter.py` | Phoenix Channels WebSocket compat (`/socket/websocket`) |
| `services/org.py` | Agent CRUD, tree traversal |
| `services/dispatch.py` | Task dispatch between agents |
| `services/memory.py` | Three-layer memory |
| `services/git_worktree/` | Per-agent worktree, checkpoint/merge/rollback（包） |
| `services/task.py` | TaskService shim → `services/tasks/*` |

## Agent types & org

- **CEO** (root): 行政五权 `DISPATCH`/`REVIEW`/`MERGE`/`SOURCE_READ`/`MANAGE_ORG` + `DOC_WRITE`；**无 SOURCE_WRITE/bash/test/staffing**（硬门）。定组织、审中层里程碑、终验对用户（`message_user`），不写业务代码、不日常直派叶子。
- **Coordinator / 中层 (player-coach)**: 协调权 + `SOURCE_WRITE`/`BASH_SHELL`/`TEST_RUN`/`BROWSE` —— 自己搭骨架/写关键路径（有独立 worktree，同 executor 契约）；hire/dismiss/transfer。受限写白名单（`COORDINATOR_WRITE_PREFIXES`）适用于项目根。
- **Executor** (叶子): 可读写代码、运行测试、写工作日志。不能 spawn 下级。
- **QA** (`test_engineer`/`qa_engineer`): 含 SOURCE_WRITE（缺它 write_file 被硬门死 —— Echo 事故）。
- **HR**: 同受限写白名单，无源码写。

未知 family 兜底 READONLY。CEO/HR 强制项目根（无 worktree）；executor + builder coordinator 由 `agent_gets_write_worktree()` 判定有 worktree。CEO（root）+ HR（CEO 下级）项目创建时自动创建，HR 招 expert agents。

**团队开会（规格已定，未实现）：** [`docs/spec/team-meeting.md`](docs/spec/team-meeting.md)。不要用 inbox 群发或 `chat()`+skip `append_turn` 冒充开会。

## Game time

`REAL_SECONDS_PER_GAME_DAY = 3600` (1 real hour per game day). 5s tick persists time + fires alarms. 每 30s 扫 orphan streaming.

**Stall 检测三层机制**（不要混淆）：
1. **Inbox stall / awaiting-reply 催办 — 已禁用**（`_check_stalled` / `_nudge_awaiting_replies` no-op）。回复义务由 turn exit 的 `expect_report` / `ask` + 收件人 ID 检查强制执行。
2. **Task dwell 时钟 — 平台自愈，不 inbox 催人**（`_nudge_stale_ledger`）：记 dwell / auto-submit / VERIFY 改派 / MERGE PROXY；**不**发 `[TASK STALL]` 等进度催。到期只 `[WAIT_TIMEOUT]` 醒等待方。
3. **沉默观测看门狗 — 自醒 + 红框**（`_check_silent_agents`）：`SILENCE_THRESHOLD_MS = 10min` 无产出 → 唤醒本人 + agent_health 红框；`SILENCE_NOTIFY_MS = 30min` 持续失联 → **只打日志**，不 inbox 上级。

## Environment variables

- `HIVEWEAVE_OPENCODE_API_KEY` — OpenCode API key (required)
- `HIVEWEAVE_META_DB_PATH` — override Meta DB path
- `HIVEWEAVE_API_KEY` — API key auth (unset = open)
- `HIVEWEAVE_CORS_ORIGINS` — CORS whitelist

## File size / split discipline

Prefer domain modules (~200–800 lines). **Do not** pile new logic into shell/shim files:
- `agents/agent.py`, `tools/task_tools.py` (shim), `components/ChatPanel.tsx`, `api.ts` (barrel)
- `services/task.py` (shim → `services/tasks/*`), `services/git_worktree/` package `__init__.py`, `llm/streamer/` package `__init__.py`

Put new logic in domain modules: `agents/{completion,recovery,streaming,watcher,helpers}/`, `tools/tasks/*`, `services/tasks/*`, `services/git_worktree/{service_*,reconcile,ensure,…}`, `llm/streamer/{core,tool_loop,http_stream,…}`, `chat/*`, `api/{ws,rest}.ts`.

**Atomic reviewable splits:** when turning a monolith into a package, stage/commit the deleted old module together with every replacement file in one reviewable boundary; keep compatible re-exports on `__init__`. Do not treat import-only smoke (e.g. `test_p3_split_import_smoke.py`) as the sole acceptance for a core split — map or add focused happy-path + one failure/timeout/recovery check on the public entrypoints.

## Frontend

React 19 + Zustand (`store.ts`). React Flow org chart. Key panels: ChatPanel (shell; logic in `chat/`), OrgTree, AgentNode. API via `api.ts` barrel → `api/rest.ts` + `api/ws.ts`. WebSocket via `phoenix.js`. Electron entry: `apps/web/electron/main.cjs`.

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
