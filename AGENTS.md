# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Commands

```bash
pnpm install              # Install all dependencies
pnpm dev                  # Start dev server + web (turborepo parallel)
pnpm build                # Full build (turbo build)
pnpm typecheck            # Type-check all packages
pnpm -C apps/server dev   # Run server only (tsx watch, port 3200)
pnpm -C apps/web dev      # Run web only (Vite dev server, port 5173)
pnpm db:push              # Push Drizzle schema to SQLite
pnpm db:studio            # Open Drizzle Studio for DB inspection
```

- No test suite exists yet. When adding tests, configure the test runner per-package.
- `pnpm db:push` is **not** a migration — it diff-pushes schema directly. Only run after schema changes.

### Node version

The project requires Node `>=22.0.0 <24.0.0`. The system has both Node 24 (global) and Node 22 (portable, at `%LOCALAPPDATA%\Programs\node-v22.20.0-win-x64`). Before running any pnpm/node command, prepend Node 22 to PATH:

```bash
export PATH="$LOCALAPPDATA/Programs/node-v22.20.0-win-x64:$PATH"
```

`better-sqlite3` is a native module — if you switch Node versions, you must `rm -rf node_modules && pnpm install` to rebuild it.

## Architecture

### Monorepo layout (pnpm + Turborepo)

```
apps/server/     @hiveweave/server     Fastify API (port 3200)
apps/web/        @hiveweave/web        React 19 + Vite + React Flow (port 5173)
packages/shared/ @hiveweave/shared     Zod schemas, types, game-time utils, charter logic
packages/db/     @hiveweave/db         Drizzle ORM + better-sqlite3, schema definitions
packages/core/   @hiveweave/core       Business logic: 20+ services, tools, MCP, token utils
packages/agent-runtime/  @hiveweave/agent-runtime  AI SDK wrapper, retry, overflow detection
```

Dependency chain: `server → core → db → shared` and `server → agent-runtime → shared`. The web app only depends on `shared`.

### Dual-DB pattern

There are **two SQLite database tiers**:

1. **Meta DB** (`data/hiveweave.db`, WAL mode) — global tables: `projects`, `agent-templates`, `llm-models`, `global-settings`. One per server process.
2. **Per-project DB** (one per workspace, DELETE journal mode) — project-scoped tables: `agents`, `memories`, `chat-messages`, `handoffs`, `inbox`, etc. Isolated per workspace.

`ensureProjectDb(workspacePath)` lazily creates a per-project DB. Agent lookups go through `lookupAgentWorkspace()` → `getProjectDbForAgent()`.

### Agent runtime and AI SDK

`@hiveweave/agent-runtime` wraps Vercel `ai` SDK (`streamText`/`generateText`):

- **ProviderFactory** (`provider-factory.ts`): Maps `openai`/`anthropic`/`google` provider strings to AI SDK providers. Any unrecognized provider falls back to `@ai-sdk/openai-compatible` (covers DeepSeek, Groq, TogetherAI, etc.).
- **Retry logic** (`retry-utils.ts`): Up to 2 retries on status 429/503/504/529, exponential backoff with jitter, `Retry-After` header parsing (OpenAI + Anthropic rate-limit headers).
- **Context overflow detection**: Estimates tokens via char-ratio heuristic (4 chars/token English, ~1.5 for CJK), reserves `COMPACTION_BUFFER` (20K) for model output, trims history before hitting the model's context window.
- **ToolOutputStore** (`tool-output-store.ts`): When tool output exceeds 2K lines or 50KB, the full output is saved to temp files (7-day retention) and a truncated preview is returned to the agent. Aligned with OpenCode's truncation pattern.

### Conversation management

`conversationStore` (`packages/core/src/conversation-store.ts`) persists per-agent conversation history to the `conversation_turns` table:

- **Token-budget trimming**: History is trimmed by token budget (derived from model context window), not message count. Turn-level trimming — never splits `assistant(tool_calls)` / `tool(result)` pairs.
- **Smart compaction**: When old turns must be evicted, a `compactor` callback can summarize them via LLM into a structured handoff prepended to recent history.
- **DeepSeek prefix caching**: Identity prompt (first system msg) stays constant for cache hits; dynamic context (memories, handoffs) goes in a second system message; compacted prefix as a third.
- **Lazy loading**: History loaded from DB on first access, then cached in memory.

### Agent tool system

Six built-in tools in `packages/core/src/tools/`: `bash`, `grep`, `apply-patch`, `question`, `todowrite`, `websearch`. Each exported as an Effect-based function. The `ToolExecutor` (`tool-executor.ts`) wraps them with:
- **Permission gating**: Coordinator agents can only read, not execute. Executor agents can write code and run tests.
- **Tool-binding registry**: Agents can be assigned specific tools, skills, and MCP servers via a config-driven registry.
- **MCP integration**: `mcpService` (`mcp/mcp-service.ts`) manages MCP server lifecycle and tool discovery.

### Agent types and permissions

Two agent permission types (from `shared/src/agent.ts`):
- **Coordinator** (架构师/经理): Can read subordinate logs/code, approve/reject work, spawn/dismiss agents, trigger integration tests. Cannot write code.
- **Executor** (叶子Agent): Can read/write code, run tests, write work logs. Cannot spawn sub-agents or read other agents' private memory.

### Organization structure

Agents form a dynamic tree hierarchy. The CEO (root) is auto-created per project. HR (under CEO) handles staffing. Expert agents (test_engineer, code_reviewer, security_auditor, web_perf_auditor) are on-demand executors — they're only invoked when scheduled, avoiding idle token burn.

### Game time system

Simulated project time at 15 real-minutes per game day (`REAL_SECONDS_PER_GAME_DAY = 900`). A 5-second tick persists time and fires due alarms. Agents can schedule alarms at game-time offsets, and stalled agents (15+ min inactivity) trigger escalation to superiors.

### Frontend state

React 19 + Zustand for global state (`store.ts`). React Flow renders the org chart. Key panels: ChatPanel, OrgTree, AgentNode, GoalsPanel, QuestionDialog. API calls go through `api.ts` → Fastify routes under `/api/*`. Web supports Electron embedding (`pnpm electron:dev`).

### Key services in `@hiveweave/core`

| Service | Purpose |
|---------|---------|
| `OrgService` | CRUD for agents, tree traversal, role lookup |
| `DispatchService` | Task dispatch between agents |
| `MemoryService` | Three-layer memory (project/agent-private/archive) |
| `HandoffService` | Agent handoff lifecycle (dismiss → summarize → transfer → archive) |
| `InboxService` | Message delivery with urgency levels |
| `ApprovalService` | Async approval flow (request → wait → resolve/cancel) |
| `RosterService` | Personnel records (position, department, responsibilities) |
| `ProjectService` | Project CRUD, goals, enterprise metadata |
| `SettingsService` | Global key-value settings (e.g., operatorName) |
| `GameTimeService` | Per-project simulated clock |
| `AlarmService` | Scheduled alarms on game time |
| `ModelService` | LLM model registry CRUD |
| `TemplateService` | Agent template CRUD |
| `FileService` | Filesystem read/write within project workspace |
| `ShellService` | Bash execution with Docker sandbox support |
| `WebService` | URL fetching with JSDOM |
| `ClawHubService` | Plugin/skill marketplace |
| `TeamChatService` | Multi-agent group chat |
| `ChatMessageService` | Chat message persistence |
| `PermissionService` | Tool permission checking |
| `CommunicationService` | Cross-agent communication tracking + user ping notifications |

### Effect usage

The codebase uses Effect (`effect` + `@effect/schema`) in `ToolExecutor` and tool implementations for typed error handling and composable execution chains. The agent-runtime package intentionally avoids Effect to keep the AI SDK wrapper lightweight.

### Environment variables

- `PORT` — server port (default: 3200)
- `DB_PATH` — override meta DB path (default: `packages/db/data/hiveweave.db`)
- `BASH_SANDBOX` — set to `docker` for Docker sandboxing of bash tool
- Model API keys: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`, etc. — per provider, read from env by the model registry
