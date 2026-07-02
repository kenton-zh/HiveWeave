# HiveWeave v1.5 - Elixir Backend

> Status: **Active backend.** Elixir/Phoenix backend at `apps/hiveweave/` (port 4000) is the primary backend. The legacy TS/Fastify backend at `apps/server/` (port 3200) is kept as reference only — do NOT start it.

## What Changed

v1.5 migrates the backend from TypeScript/Fastify to **Elixir/Phoenix** to fix 3 systemic bugs:

1. **Zombie PROCESSING state** - LLM stream hangs → GenServer crashes → supervisor restarts cleanly
2. **State sync delay** - SSE + 3s polling → Phoenix Channels WebSocket push
3. **Cascading agent failures** - Single event loop → BEAM process isolation

Plus 4 production hardeings:
- **Circuit Breaker** with probe lock (prevents 5 agents stampeding a flaky provider)
- **Telemetry** observability (all LLM/agent events traced)
- **Event Audit** lightweight table (debug timeline queries)
- **Supervisor max_restarts** (crash storm prevention)

## Quick Start

### Prerequisites
- Elixir 1.17+ / Erlang/OTP 26
- Node.js 22+ (for frontend)

### Run

**Erlang/Elixir are at non-standard paths and NOT in system PATH.** Use the startup scripts from the repo root:

```bash
# Option 1: Start everything (backend + frontend in separate windows)
start-all.bat

# Option 2: Start individually
start-backend.bat    # Elixir/Phoenix on port 4000
start-frontend.bat   # React/Vite on port 5173
```

The scripts automatically prepend `C:\Users\99744\otp26\bin` and `C:\Users\99744\elixir\bin` to PATH. Do NOT run `mix phx.server` directly without setting PATH first.

### Tests

```bash
cd apps/hiveweave
mix test
# 38 tests, 0 failures
```

## Architecture

```
HiveWeave.Application (rest_for_one)
├── HiveWeave.Telemetry         # attach_many handler on app start
├── HiveWeave.Repo.Meta         # Exqlite (WAL mode) - global tables
├── HiveWeaveWeb.Endpoint       # Phoenix.Endpoint (port 4000, Bandit adapter)
├── HiveWeave.PubSub            # Phoenix.PubSub - cross-process messaging
├── HiveWeaveWeb.Presence       # Phoenix.Presence - status tracking
├── Task.Supervisor             # for tool execution
├── HiveWeave.LLM.CircuitBreaker  # 3-state machine with probe lock
├── HiveWeave.EventAudit        # lightweight table logger
└── HiveWeave.ProjectSupervisor # DynamicSupervisor - per-project children
    ├── HiveWeave.Agents.AgentSupervisor (per project)
    │   └── Agent GenServer (3-state: idle/processing/idle)
    └── HiveWeave.GameTime.Server (per project)
```

## Future-Proofing for v2 (开罗风 Office)

The `Agent` GenServer state has fields reserved for the future pixel office feature:

```elixir
defstruct [
  ...
  position: nil,    # tile coordinates (Kairo-style office)
  target: nil,      # target agent/room (for walk-to-peer messaging)
  face: :down,      # sprite direction
  action: :idle,    # current action (mirrors status in v1.5)
  ...
]
```

These fields are nil in v1.5 but exist in the struct so v2 doesn't need a state migration.

## Two-Tier SQLite

- **Meta DB**: `packages/db/data/hiveweave.db` (WAL mode) - global tables
- **Per-project DB**: `<workspace>/.hiveweave/data.db` (DELETE journal mode) - per-project

Override with `HIVEWEAVE_DB_PATH` env var.

## Code Structure

```
apps/hiveweave/
├── lib/
│   ├── hiveweave/
│   │   ├── application.ex              # supervision tree
│   │   ├── telemetry.ex                # telemetry handlers
│   │   ├── event_audit.ex              # event audit log
│   │   ├── project_supervisor.ex       # per-project dynamic supervisor
│   │   ├── token_utils.ex              # token estimation
│   │   ├── conversation_store.ex       # conversation history
│   │   ├── agents/
│   │   │   ├── agent.ex                # Agent GenServer (3-state + self-retrigger)
│   │   │   └── agent_supervisor.ex     # per-project agent supervisor
│   │   ├── compaction/
│   │   │   ├── context_overflow.ex     # context overflow detection
│   │   │   └── overflow.ex             # overflow handling
│   │   ├── llm/
│   │   │   ├── streamer.ex             # OpenAI-compatible streaming + tool loop (1643 lines)
│   │   │   ├── circuit_breaker.ex      # circuit breaker with probe lock
│   │   │   ├── provider_factory.ex     # OpenAI-compatible provider
│   │   │   └── retry.ex                # retry logic
│   │   ├── game_time/
│   │   │   └── server.ex               # per-project game clock
│   │   ├── repo/
│   │   │   ├── meta.ex                 # Ecto.Repo for meta DB
│   │   │   └── project_factory.ex      # per-project Repo factory
│   │   ├── schema/                     # 19 Ecto schemas
│   │   │   ├── agent.ex
│   │   │   ├── project.ex
│   │   │   ├── chat_message.ex
│   │   │   ├── conversation_turn.ex
│   │   │   ├── memory.ex
│   │   │   ├── handoff.ex
│   │   │   ├── inbox.ex
│   │   │   ├── permission_request.ex
│   │   │   ├── scheduled_alarm.ex
│   │   │   ├── work_log.ex
│   │   │   ├── personnel_record.ex
│   │   │   ├── module.ex
│   │   │   ├── merge.ex
│   │   │   ├── agent_template.ex
│   │   │   ├── llm_model.ex
│   │   │   ├── global_setting.ex
│   │   │   ├── agent_event.ex          # for event audit
│   │   │   ├── project_index.ex
│   │   │   ├── meta_index.ex
│   │   │   ├── agent_charter.ex
│   │   │   └── charter_attachment.ex
│   │   ├── services/                   # 16 business logic services
│   │   │   ├── org.ex                  # agent CRUD, tree traversal
│   │   │   ├── dispatch.ex             # task dispatch between agents
│   │   │   ├── memory.ex               # three-layer memory
│   │   │   ├── handoff.ex              # agent handoff lifecycle
│   │   │   ├── inbox.ex                # message delivery
│   │   │   ├── approval.ex             # async approval flow
│   │   │   ├── git_worktree.ex         # per-agent worktrees (coordinator-only)
│   │   │   ├── roster.ex               # personnel records
│   │   │   ├── model.ex                # LLM model registry
│   │   │   ├── template.ex             # agent template CRUD
│   │   │   ├── charter.ex              # project charter
│   │   │   ├── team_chat.ex            # multi-agent group chat
│   │   │   ├── permission.ex           # tool permission checking
│   │   │   ├── settings.ex             # global key-value settings
│   │   │   ├── system_state.ex         # system state
│   │   │   └── chat_message.ex         # chat message persistence
│   │   ├── application.ex              # supervision tree
│   │   ├── telemetry.ex                # telemetry handlers
│   │   ├── event_audit.ex              # event audit log
│   │   ├── project_supervisor.ex       # per-project dynamic supervisor
│   │   ├── token_utils.ex              # token estimation (CJK-aware)
│   │   ├── conversation_store.ex       # conversation history + compaction
│   │   ├── tool_executor.ex            # 30+ tools, 3825 lines
│   │   └── skill_registry.ex           # skill registry
│   └── hiveweave_web/
│       ├── endpoint.ex                 # Phoenix.Endpoint
│       ├── router.ex                   # HTTP routes
│       ├── user_socket.ex              # WebSocket entry
│       ├── presence.ex                 # Presence tracker
│       ├── error_view.ex
│       ├── plugs/
│       │   └── api_key_auth.ex         # API key authentication
│       ├── channels/
│       │   ├── lobby_channel.ex        # global status
│       │   ├── project_channel.ex      # per-project events
│       │   └── agent_channel.ex        # per-agent chat stream
│       └── controllers/
│           ├── settings_controller.ex
│           ├── projects_controller.ex
│           ├── org_controller.ex
│           ├── chat_controller.ex
│           ├── permissions_controller.ex
│           ├── extra_controller.ex
│           ├── root_controller.ex
│           └── health_controller.ex
├── test/
│   ├── test_helper.exs
│   └── hiveweave/
│       ├── token_utils_test.exs
│       ├── llm/
│       │   ├── circuit_breaker_test.exs
│       │   ├── provider_factory_test.exs
│       │   └── retry_test.exs
│       ├── services/
│       │   └── org_test.exs
│       └── schema/
│           ├── agent_test.exs
│           └── project_test.exs
├── config/
│   ├── config.exs
│   ├── dev.exs
│   ├── prod.exs
│   └── test.exs
├── mix.exs
└── mix.lock
```

## Migration Status

| Phase | Status | Notes |
|-------|--------|-------|
| Phase 0: Project scaffold | ✅ Done | Phoenix + Ecto + Exqlite + Bandit |
| Phase 0: 19 Ecto schemas | ✅ Done | One per table in existing schema |
| Phase 0: Meta Repo | ✅ Done | WAL mode, Ecto direct connect |
| Phase 0: Application supervision | ✅ Done | rest_for_one strategy |
| Phase 1: Phoenix Channel framework | ✅ Done | 3 channels (lobby/project/agent) |
| Phase 1: Circuit Breaker | ✅ Done | With probe lock |
| Phase 1: Telemetry | ✅ Done | 9 events wired |
| Phase 1: Event Audit | ✅ Done | agent_events table |
| Phase 1: Agent Supervisor | ✅ Done | per-project supervisor |
| Phase 2: Agent GenServer | ✅ Done | 3-state machine + self-retrigger for long tasks |
| Phase 2: LLM Streamer | ✅ Done | Full OpenAI-compatible streaming + tool loop (1643 lines, 40-80 rounds per role) |
| Phase 2: Tool Executor | ✅ Done | 30+ tools, 3825 lines (bash, file ops, grep, glob, apply-patch, websearch, MCP, review, ...) |
| Phase 2: Token utils | ✅ Done | CJK-aware estimation |
| Phase 2: ConversationStore | ✅ Done | Lazy load + cache + LLM compaction |
| Phase 2: Permissions | ✅ Done | Permission service + 3-state gating (allow/ask/deny) + saved rules |
| Phase 2: MCP | ✅ Done | mcp_list_tools / mcp_call in tool executor |
| Phase 2: Compaction | ✅ Done | context_overflow + overflow modules |
| Phase 3: Core services | ✅ Done | 16 services (org, dispatch, memory, handoff, inbox, approval, git_worktree, roster, model, template, charter, team_chat, permission, settings, system_state, chat_message) |
| Phase 3: GameTime + Alarm | ✅ Done | Per-project GenServer |
| Phase 3: GitWorktreeService | ✅ Done | create/checkpoint/merge/rollback/remove/list/status (coordinator-only) |
| Phase 3: HTTP controllers | ✅ Done | 8 controllers + API key auth plug |
| Phase 4: Frontend api.ts | ✅ Done | SSE → phoenix.js Channel |
| Phase 5: ExUnit tests | ✅ Done | 38 tests, 0 failures |
| Phase 5: AGENTS.md update | ✅ Done | Updated for new structure |

## Remaining Limitations

1. **bash tool Windows-bound** — Elixir `tool_executor.ex` uses `cmd /c` (no Unix toolchain); the TS reference implementation uses Git Bash. Cross-platform support pending.
2. **No CLI entry** — Server-only (HTTP + WebSocket). No `escript` or CLI task for headless/command-line use. External integration requires HTTP API calls.
3. **No Docker/Linux deployment** — Startup scripts are Windows `.bat` with hardcoded Erlang/Elixir paths. Not yet containerized.

## How to Run End-to-End

1. Start Elixir backend: `start-backend.bat` from repo root (port 4000)
2. Start frontend: `start-frontend.bat` from repo root (port 5173)
3. Open browser at http://localhost:5173
4. Create a project (point `workspacePath` to your codebase), add agents, chat with them

LLM calls require a valid API key. Set `OPENCODE_API_KEY` (or provider-specific keys) in `apps/server/.env` before starting. The default seeded model is `DeepSeek V4 Flash Free` against `https://opencode.ai/zen/v1` — note this gateway does NOT support tool-calling, so configure a tool-capable model (e.g. DeepSeek, OpenAI, Anthropic) via the Agent config UI for agents to use tools.

## Notes

- The TS backend (`apps/server/`) is kept as **reference** only — do NOT start it. Use the Elixir backend (`apps/hiveweave/`) on port 4000.
- TS packages (`packages/core/`, `packages/agent-runtime/`) remain the **reference implementation** — the Elixir backend mirrors their design.
