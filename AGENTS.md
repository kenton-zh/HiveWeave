# AGENTS.md

Guidance for OpenCode sessions working in this monorepo. Keep it terse; every line should answer "would an agent miss this without help?".

## Architecture (IMPORTANT)

This is a **monorepo with two backends**:
- **Elixir/Phoenix backend** (`apps/hiveweave/`) — the **active** backend, runs on port **4000**. This is what you should use.
- **TS/Fastify backend** (`apps/server/`) — the **legacy** backend (port 3200), kept as reference. Do NOT start it.
- **React frontend** (`apps/web/`) — Vite dev server on port **5173**, connects to Elixir backend at 4000.

The TS packages (`packages/core/`, `packages/agent-runtime/`) are the **reference implementation** — the Elixir backend is being ported from them.

## Commands

### Starting the project (Elixir backend + React frontend)

**Erlang/Elixir are at non-standard paths and NOT in system PATH.** Use the startup scripts:

```bash
# Option 1: Start everything (backend + frontend in separate windows)
start-all.bat

# Option 2: Start individually
start-backend.bat    # Elixir/Phoenix on port 4000
start-frontend.bat   # React/Vite on port 5173
```

The scripts automatically set `PATH` to include:
- Erlang/OTP 26: `C:\Users\99744\otp26\bin`
- Elixir: `C:\Users\99744\elixir\bin`

**Do NOT run `mix phx.server` directly** — it will fail with "mix not found" unless you manually set the PATH first:
```powershell
$env:PATH = "C:\Users\99744\otp26\bin;C:\Users\99744\elixir\bin;$env:PATH"
cd apps\hiveweave; mix phx.server
```

### TS/Node commands (for reference packages only)

```bash
# Setup
pnpm install                         # All deps; uses pnpm@10.31.0 (see packageManager)
pnpm db:push                         # Drizzle schema push — run after schema changes only

# Dev (legacy TS backend — do NOT use, use start-backend.bat instead)
pnpm dev                             # Server :3200 + Web :5173
pnpm -C apps/web dev                 # Web only (Vite)

# Verify
pnpm turbo typecheck                 # tsc --noEmit across all packages
pnpm build                           # tsc/vite build
```

### Node version (Windows)

Required: Node `>=22.0.0 <24.0.0`. System has both Node 24 (global) and Node 22 (portable at `%LOCALAPPDATA%\Programs\node-v22.20.0-win-x64`). Prepend Node 22 to PATH before any pnpm/node command:

```bash
export PATH="$LOCALAPPDATA/Programs/node-v22.20.0-win-x64:$PATH"
```

## Repo shape

```
apps/server/      @hiveweave/server     Fastify API (port 3200), tsx watch
apps/web/         @hiveweave/web        React 19 + Vite + React Flow (port 5173)
packages/shared/  @hiveweave/shared     Zod schemas, types, charter, game-time, flower-name utils
packages/db/      @hiveweave/db         Drizzle ORM + better-sqlite3, schema, seed
packages/core/    @hiveweave/core       Services, tools, MCP, token utils
packages/agent-runtime/  @hiveweave/agent-runtime  Vercel `ai` SDK wrapper, retry, overflow
```

Dependency chain: `server → core → db → shared` and `server → agent-runtime → shared`. Web only depends on `shared`. `pnpm-workspace.yaml` advertises packages under `apps/*` and `packages/*`; `turbo.json` defines `build` (with `^build` dep), `dev` (cache:false, persistent), `typecheck` (with `^build` dep).

## Two-tier SQLite

1. **Meta DB** — `packages/db/data/hiveweave.db` (WAL). Global tables: `projects`, `agent-templates`, `llm-models`, `global-settings`. One per server process. Override with `HIVEWEAVE_DB_PATH` (see `packages/db/src/client.ts:10`).
2. **Per-project DB** — one per workspace, **DELETE journal mode** (no `-wal`/`-shm` files; avoids Windows `SQLITE_IOERR_SHMOPEN` after force-kill). Project-scoped tables: `agents`, `memories`, `chat-messages`, `handoffs`, `inbox`, `conversation-turns`, etc.

Lifecycles: `ensureProjectDb(workspacePath)` lazily creates a per-project DB. Agent lookups go `lookupAgentWorkspace()` → `getProjectDbForAgent()`. Per-project DBs are cached — call `evictProjectDb()` if you need to drop one.

## Server-side quirks

- `apps/server/src/index.ts` imports `./env.js` **first** to load `.env` (at `apps/server/.env`, not repo root) before any other module reads `process.env`. See `apps/server/src/env.ts`. Copy `apps/server/.env.example` → `apps/server/.env`.
- `seedDefaultModel()` reads `OPENCODE_API_KEY` (not `DEEPSEEK_API_KEY` despite the example). It seeds `DeepSeek V4 Flash Free` against `https://opencode.ai/zen/v1`.
- On startup, server clears any `is_streaming=True` rows (zombie messages from prior crashes) and renames legacy CEO/HR agents to a "flower name" (花名) if they don't have one (`isFlowerName()` / `generateFlowerName()` in `packages/shared/src/names.ts`).
- The 5-second `runGameTimeTick()` persists time and fires due alarms; stalled agents (15+ min idle) trigger escalation to superiors. Defined in `apps/server/src/game-time-scheduler.ts`.

## Agent runtime (`@hiveweave/agent-runtime`)

Wraps Vercel `ai` SDK (`streamText`/`generateText`):

- `provider-factory.ts` maps `openai`/`anthropic`/`google` strings to AI SDK providers. Anything else falls back to `@ai-sdk/openai-compatible` (DeepSeek, Groq, TogetherAI, …).
- `retry-utils.ts`: up to 2 retries on 429/503/504/529, exponential backoff with jitter, parses `Retry-After` (OpenAI + Anthropic headers).
- `token-utils.ts` (in `core`): char-ratio heuristic (4 chars/token EN, ~1.5 CJK); reserves `COMPACTION_BUFFER` (20K) for output; `COMPACTION_BUFFER`/`PRESERVE_RECENT_MIN`/`PRESERVE_RECENT_MAX`/`DEFAULT_TAIL_TURNS` exported.
- `tool-output-store.ts`: tool output > 2K lines or 50KB gets saved to temp files (7-day retention), truncated preview returned. Mirrors OpenCode's pattern.

## Conversation store

`conversationStore` (`packages/core/src/conversation-store.ts`) persists per-agent history to `conversation_turns`:

- **Token-budget trimming**, not message-count. Turn-level — never splits `assistant(tool_calls)` / `tool(result)` pairs.
- **Smart compaction** via a `compactor` LLM callback — evicts oldest turns, prepends a structured handoff summary to recent history.
- **DeepSeek prefix-cache aware**: identity prompt (1st system msg) stays constant; dynamic context (memories, handoffs) in a 2nd system msg; compacted prefix in a 3rd.
- Lazy-loaded from DB on first access, then cached in memory. `conversationStore.clearAll()` is called on server startup.

## Tools (`packages/core/src/tools/`)

7 built-in tools, each an Effect-based function. The `ToolExecutor` (`packages/core/src/tool-executor.ts`) wraps them with permission gating + a tool-binding registry.

`bash`, `grep`, `apply-patch`, `question`, `todowrite`, `websearch`, **`review`** (runs `runCodeReview`/`runSecurityAudit`/`runTestReview`/`runPerfAudit`/`runFullReview`).

MCP integration lives in `packages/core/src/mcp/mcp-service.ts` (`mcpService`).

## Agent types & org

- **Coordinator** (架构师/经理): read subordinate logs/code, approve/reject work, spawn/dismiss agents, trigger integration tests, control worktrees. Cannot write code.
- **Executor** (叶子): read/write code, run tests, write work logs. Cannot spawn sub-agents or read other agents' private memory.

CEO is auto-created per project. HR is under CEO. Expert agents (`test_engineer`, `code_reviewer`, `security_auditor`, `web_perf_auditor`) are on-demand executors — only invoked when scheduled.

### ETHOS 工程约束框架（注入所有角色）

三层提示词架构：ETHOS 共享层 → 角色类型约束层 → 角色专属剧本层。定义在 `streamer.ex` 的 `build_identity_prompt`（静态）+ `build_context_prompt`（动态）中。

**三原则**（注入所有角色共享前言）：
1. **Boil the Lake** — 完整实现，边界处理不能"以后再说"
2. **Search Before Building** — 先搜索成熟模式，再从零设计
3. **User Involvement**（可调） — 用户参与度三级：high（全问用户）/ medium（技术自主+产品必问）/ low（仅通知）。通过 charter 的 `userInvolvement` 字段配置，`build_involvement_block` 每轮动态注入。让渡的是决策权，不是诚实义务。

**角色纪律四件套**（每个角色必备）：何时不做 / 输出格式 / 验证清单 / 反合理化表。

**工具权限矩阵**（`tool_executor.ex` `get_tools/2`）：
- `coordinator_tools` — 管理工具 + worktree + readonly_file + self_skill_tools
- `executor_tools` — full_file + core + self_skill_tools（通用执行者）
- `executor_tools_for_qa` — full_file + qa_review_tools + self_skill_tools（QA 写测试代码）
- `executor_tools_for_test_engineer` — bash + readonly_file（无 write，不写应用代码）
- `executor_tools_for_auditor` — bash + readonly_file + qa_review_tools（无 write，只报告不改）
- `hire_tools` — hire_agent + list_agent_templates + transfer/dismiss/update_roster（HR 独占）

**技能自主添加**：所有角色都有 `bind_skill` + `list_available_skills`，可随项目推进自主绑定新技能。初始技能是起点，不是终点。

**agent_templates 集成**：HR 可通过 `list_agent_templates` 浏览模板库，在 `hire_agent` 时传 `templateId` 预填 role/goal/backstory。显式参数覆盖模板值。

**组织范式流程节点**：6 种范式（solo/flat_squad/tech_lead/pm_architect/pod/pipeline）各注入必经流程（如 solo 必须自审，flat_squad 交叉审查，pipeline 阶段门禁）。

## `GitWorktreeService` (coordinator-only)

`packages/core/src/git-worktree-service.ts` — gives each leaf agent an isolated worktree under `.hiveweave/worktrees/<shortId>/` on branch `hw/<shortId>/<task-slug>`. Coordinators can `create`, `checkpoint` (lightweight commit), `merge` (fast-forward into main then cleanup), and `rollback` (git reset --hard). Tools that drive this live in `ToolExecutor` around `tool-executor.ts:1468`.

## Game time

`REAL_SECONDS_PER_GAME_DAY = 900` (15 real min per game day) — `packages/shared/src/game-time.ts:2`. Agents schedule alarms at game-time offsets; server's `runGameTimeTick` (5s) fires due ones. Stalled agents (15+ min inactivity) escalate to superiors.

## Frontend

React 19 + Zustand (`store.ts`). React Flow renders the org chart (`OrgTree`, `AgentNode`). Key panels: `ChatPanel`, `GoalsPanel`, `QuestionDialog`. API calls go through `api.ts` → Fastify routes under `/api/*`. Web has an Electron entry (`apps/web/electron/main.cjs`).

## Services in `@hiveweave/core`

| Service | Purpose |
|---|---|
| `OrgService` | CRUD for agents, tree traversal, role lookup |
| `DispatchService` | Task dispatch between agents |
| `MemoryService` | Three-layer memory (project / agent-private / archive) |
| `HandoffService` | Agent handoff lifecycle (dismiss → summarize → transfer → archive) |
| `InboxService` | Message delivery with urgency levels |
| `ApprovalService` | Async approval flow (request → wait → resolve/cancel) |
| `RosterService` | Personnel records (position, department, responsibilities) |
| `ProjectService` | Project CRUD, goals, enterprise metadata |
| `SettingsService` | Global key-value settings (e.g. `operatorName`) |
| `GameTimeService` | Per-project simulated clock (`getGameTimeService(db)`) |
| `AlarmService` | Scheduled alarms on game time |
| `ModelService` | LLM model registry CRUD |
| `TemplateService` | Agent template CRUD |
| `FileService` | Filesystem read/write within project workspace |
| `ShellService` | Bash execution; Docker sandbox via `BASH_SANDBOX=docker` |
| `WebService` | URL fetching with JSDOM |
| `ClawHubService` | Plugin / skill marketplace |
| `TeamChatService` | Multi-agent group chat |
| `ChatMessageService` | Chat message persistence |
| `PermissionService` | Tool permission checking |
| `CommunicationService` | Cross-agent communication + user ping notifications |
| `GitWorktreeService` | Per-agent worktrees, checkpoint / merge / rollback (coordinator-only) |
| `statusEventBus` | Pub/sub for agent status updates |
| `token-utils` | `estimateTokens`, `calculateHistoryBudget`, `truncateToolOutput`, compaction constants |
| `time-context` | `buildTimeContextBlock`, `prefixTriggerMessage`, `prefixInterAgentMessage` |
| `env-check` | Environment validation helper |

## Effect usage

`ToolExecutor` and tool implementations use Effect (`effect` + `@effect/schema` + `@effect/platform`) for typed errors + composable execution. `agent-runtime` deliberately avoids Effect to keep the AI SDK wrapper lightweight.

## Environment variables

- `HIVEWEAVE_DB_PATH` — override meta DB path (default: `packages/db/data/hiveweave.db`)
- `PORT` — server port (default: 3200)
- `HIVEWEAVE_DIAG` — set to `1` or `true` to enable verbose `[Streamer-DIAG]` / `[Agent-DIAG]` logs for debugging multi-agent LLM request/response/tool-call parsing. Off by default. Compile-time flag (requires restart to toggle).
- `BASH_SANDBOX` — `docker` to sandbox `bash` tool
- `OPENCODE_API_KEY` — read by `seedDefaultModel`; per-provider keys (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`, …) are read by the model registry
- `HTTPS_PROXY` — for restricted networks (see `apps/server/.env.example`)

<!-- CODEGRAPH_START -->
## CodeGraph

In repositories indexed by CodeGraph (a `.codegraph/` directory exists at the repo root), reach for it BEFORE grep/find or reading files when you need to understand or locate code:

- **MCP tool** (when available): `codegraph_explore` answers most code questions in one call — the relevant symbols' verbatim source plus the call paths between them, including dynamic-dispatch hops grep can't follow. Name a file or symbol in the query to read its current line-numbered source. If it's listed but deferred, load it by name via tool search.
- **Shell** (always works): `codegraph explore "<symbol names or question>"` prints the same output.

If there is no `.codegraph/` directory, skip CodeGraph entirely — indexing is the user's decision.
<!-- CODEGRAPH_END -->
