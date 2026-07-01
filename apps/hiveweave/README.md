# HiveWeave v1.5 - Elixir Backend

> Status: Migration in progress. Old TS backend at `apps/server/` still functional. New Elixir backend at `apps/hiveweave/` (port 4000) is parallel-runnable.

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

```bash
# Terminal 1: Start Elixir backend on port 4000
cd apps/hiveweave
mix deps.get
mix phx.server

# Terminal 2: Start frontend (proxies to Elixir backend)
cd apps/web
pnpm install
pnpm dev
# In browser, the frontend should use VITE_WS_URL=ws://localhost:4000/socket
```

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
│   │   │   ├── agent.ex                # Agent GenServer
│   │   │   ├── agent_supervisor.ex     # per-project agent supervisor
│   │   │   └── agent_registry.ex       # Registry
│   │   ├── llm/
│   │   │   ├── streamer.ex             # LLM streaming
│   │   │   ├── circuit_breaker.ex      # circuit breaker
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
│   │   └── services/                   # business logic
│   │       ├── org.ex
│   │       ├── chat_message.ex
│   │       └── inbox.ex
│   └── hiveweave_web/
│       ├── endpoint.ex                 # Phoenix.Endpoint
│       ├── router.ex                   # HTTP routes
│       ├── user_socket.ex              # WebSocket entry
│       ├── presence.ex                 # Presence tracker
│       ├── error_view.ex
│       ├── channels/
│       │   ├── lobby_channel.ex        # global status
│       │   ├── project_channel.ex      # per-project events
│       │   └── agent_channel.ex        # per-agent chat stream
│       └── controllers/
│           ├── settings_controller.ex
│           ├── projects_controller.ex
│           ├── org_controller.ex
│           ├── chat_controller.ex
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
| Phase 2: Agent GenServer | ✅ Done | 3-state machine |
| Phase 2: LLM Streamer | ✅ Stub | Skeleton with CB integration |
| Phase 2: 7 tools | ⚠️ Skipped | Out of v1.5 scope (effect translation complex) |
| Phase 2: Token utils | ✅ Done | CJK-aware estimation |
| Phase 2: ConversationStore | ✅ Done | Lazy load + cache |
| Phase 2: Permissions | ⚠️ Skipped | (planned for v1.5.1) |
| Phase 2: MCP | ⚠️ Skipped | (planned for v1.5.1) |
| Phase 3: Core services | ✅ Partial | Org, ChatMessage, Inbox done; rest stubbed |
| Phase 3: GameTime + Alarm | ✅ Done | Per-project GenServer |
| Phase 3: HTTP controllers | ✅ Partial | Settings, Projects, Org, Chat, Health done; rest stubbed |
| Phase 4: Frontend api.ts | ✅ Done | SSE → phoenix.js Channel |
| Phase 5: ExUnit tests | ✅ Done | 38 tests, 0 failures |
| Phase 5: AGENTS.md update | ✅ Done | Updated for new structure |

## What Was Stubbed (Need Follow-up)

1. **LLM Streamer** - the actual HTTP streaming logic is placeholder. Needs real Req integration with the opencode/DeepSeek APIs.
2. **7 Tools** (bash, grep, apply-patch, question, todowrite, websearch, review) - these need to be ported from TS, including the Effect-TS → behaviour translation.
3. **Permissions system** - the TS version has 1300+ lines of tool permission logic. Needs full port.
4. **MCP service** - the TS version uses @modelcontextprotocol/sdk. Elixir equivalent is less mature.
5. **GitWorktreeService** - coordinator-only git worktree management.
6. **ClawHubService** - skill marketplace.
7. **File/Shell/Web/TeamChat services** - basic CRUD operations.
8. **Compaction** - the smart compaction callback in conversation store.
9. **Project-level Ecto Repo wiring** - currently `ensureProjectDb` is a stub; needs proper eager-start in ProjectSupervisor.
10. **LiveView integration** (out of v1.5 scope)

## How to Run End-to-End

1. Start Elixir backend: `cd apps/hiveweave && mix phx.server` (port 4000)
2. Configure frontend: set `VITE_WS_URL=ws://localhost:4000/socket` in `apps/web/.env`
3. Start frontend: `cd apps/web && pnpm dev` (port 5173)
4. Open browser at http://localhost:5173
5. Create a project, add agents, chat with them

For now, LLM calls will fail because the LLM Streamer is a stub. To enable real LLM calls:
- Set `OPENCODE_API_KEY` env var before starting mix phx.server
- Implement the actual Req streaming in `lib/hiveweave/llm/streamer.ex`

## Notes

- The TS backend (`apps/server/`) is **NOT removed** - the v1.5 plan calls for parallel operation during transition
- Use Strangler Fig pattern: gradually move routes from TS to Elixir
- TS endpoint: http://localhost:3200
- Elixir endpoint: http://localhost:4000
