<p align="center">
  <h1 align="center">HiveWeave</h1>
</p>
<p align="center"><strong>AI Engineering Organization</strong> — Multi-Agent Hierarchical Collaborative Platform</p>
<p align="center"><em>不是 AI 编程工具，而是一个会自我演化的 AI 工程组织</em></p>

<p align="center">
  <a href="https://github.com/kenton-zh/HiveWeave"><img alt="GitHub last commit" src="https://img.shields.io/github/last-commit/kenton-zh/HiveWeave?style=flat-square" /></a>
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/license-MIT-blue.svg?style=flat-square" /></a>
  <img alt="Python" src="https://img.shields.io/badge/python-3.12+-3776AB?style=flat-square&logo=python&logoColor=white" />
  <img alt="React" src="https://img.shields.io/badge/react-19-61DAFB?style=flat-square&logo=react&logoColor=black" />
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-0.115+-009688?style=flat-square&logo=fastapi&logoColor=white" />
</p>

<p align="center">
  <a href="README.md">English</a> |
  <a href="README.md">中文</a>
</p>

---

## What is HiveWeave / 这是什么

HiveWeave replaces the single-AI-agent model with a **multi-agent engineering organization**. Instead of one AI doing everything, you get a CEO, managers, engineers, QA, and HR — each with their own role, memory, tools, and worktree. They hire, delegate, review, merge, and report. You manage them like a real team.

HiveWeave 把单 AI Agent 模式替换为**多 Agent 工程组织**。CEO、技术经理、开发工程师、QA、HR——每个角色有自己的职责、记忆、工具和独立工作区。他们招聘、分配任务、审查代码、合并分支、汇报进度。你像管理真实团队一样管理他们。

> **Why**: Single-agent tools (Claude Code, Codex, Cursor) lose context across modules, can't parallelize, and have no quality gate. HiveWeave splits the work across specialized agents with isolated contexts, independent worktrees, and a four-layer review chain before anything reaches you.

## Quick Start / 快速开始

```bash
# Clone
git clone https://github.com/kenton-zh/HiveWeave.git
cd HiveWeave

# Backend (Python/FastAPI, port 4000)
cd apps/hiveweave-py
uv sync
uvicorn hiveweave.main:app --host 0.0.0.0 --port 4000

# Frontend (React/Vite, port 5173)
cd apps/web
export PATH="$LOCALAPPDATA/Programs/node-v22.20.0-win-x64:$PATH"  # Windows only
pnpm install
pnpm dev
```

Or use the startup scripts (Windows):
```bash
start-all.bat          # Backend + Frontend
start-backend.bat      # Backend only (port 4000)
start-frontend.bat     # Frontend only (port 5173)
```

Open `http://localhost:5173` to create your first project and meet your CEO.

## Architecture / 架构

```
你 (Human Operator)
  ↕                    ↕ (via question tool / chat)
CEO ─── 专家 (Expert, on-demand, most expensive model)
  ↕
技术经理 (Tech Lead / PM / Architect)
  ↕
QA + Executor (cheaper models for execution)

四层把关 (Four-Layer Review Gate):
  Executor → QA(/review) → 技术经理(spec compliance) → CEO(intent fit) → 你(eye check)
```

| Layer / 层 | Role / 角色 | Model / 模型 | Responsibility / 职责 |
|:---|------|:---|------|
| Decision / 决策 | CEO | Premium (Claude Opus / GPT-5) | Direction, spec, user reporting |
| Planning / 规划 | Tech Lead | Strong (Claude Sonnet / GPT-4) | Architecture, task breakdown, review |
| Quality / 质量 | QA | Moderate | Five-axis code review, security audit, E2E testing |
| Execution / 执行 | Executor | Cheap (DeepSeek / Haiku / Flash) | Write code, run tests, self-review |

## Core Capabilities / 核心能力

### Multi-Agent Organization / 多 Agent 组织
- **Dynamic hierarchy** — CEO → HR → Managers → Executors. Coordinators plan and review; Executors write code. Never the other way around.
- **Hiring flow** — CEO designs org → HR hires → Managers break down domains → HR hires more. Three-wave staffing that matches real team growth.
- **Discipline suites** — Each role gets a discipline skill set (code-review-and-quality, self-review, security-and-hardening, etc.) that defines HOW they think, not just WHAT tools they use.
- **Two-tier skill binding** — Discipline skills (mandatory, role-defining) + Tool skills (marketplace-matched by HR). HR serves every coordinator, not just the CEO.

### Context Isolation / 上下文隔离
- **Per-agent context** — Frontend agent only loads frontend code. Backend agent only loads backend. No cross-contamination.
- **Per-agent model routing** — CEO uses Claude Opus. Executor uses DeepSeek Flash. Expensive tokens on decisions; cheap tokens on execution.
- **Direct chat** — You can talk directly to any agent at any level. Frontend issue? Talk to the frontend dev. Don't route through CEO.

### Git Worktree Development / Git 工作区隔离
- **Isolated worktrees** — Each agent gets its own `git worktree` (`hw/<shortId>/<task>`). No conflicts between parallel agents.
- **Checkpoint + rollback** — Agents checkpoint before risky changes. Rollback without polluting main.
- **Review → Merge gate** — Executor reports completion → QA reviews → Manager approves → CEO signs off → Merge to main. Four gates before code reaches you.

### Memory & Handoff / 记忆与交接
- **Three-tier memory** — Project memory (shared), Agent memory (private), Archived memory (former agents). Knowledge persists across sessions.
- **Handoff inheritance** — When an agent is dismissed, their memory is summarized and transferred to a successor. No knowledge loss.
- **Continuous learning** — Agents can `skillify` successful workflows and `learn` from failures. Cross-project patterns are logged by the Boss Assistant.

### Model Budget Layering / 模型预算分层
- **按角色分模型** — Coordinators get premium models for planning and review. Executors get cheap models for coding.
- **专家通道** — When the team hits a wall, CEO summons an Expert agent running the most expensive model. AI-refined questions get better answers per dollar.
- **Configurable** — Each agent can individually override its model. Mix providers across OpenAI, Anthropic, DeepSeek, Groq, etc.

### Real-time Dashboard / 实时可视化
- **Org chart** — React Flow-powered visualization. Drag, zoom, see who reports to whom.
- **Multi-panel chat** — Talk to multiple agents simultaneously. Frontend dev in one panel, backend dev in another.
- **Live streaming** — Token-level streaming via WebSocket. Watch agents type in real-time.

## Tech Stack / 技术栈

| Layer / 层 | Stack | Notes |
|:---|------|------|
| Backend | Python 3.12 + FastAPI + Uvicorn | Port 4000, 96 routes, 19 API modules |
| Frontend | React 19 + Vite + React Flow + Zustand | Port 5173, Electron desktop support |
| Database | SQLite + aiosqlite | Dual-DB: Meta DB (WAL) + Per-project DB |
| AI/LLM | httpx SSE streaming + Provider Factory | OpenAI, Anthropic, DeepSeek, Groq, Google |
| Realtime | phoenix.js + phoenix_adapter (WebSocket) | 3 channels: lobby, project, agent |
| Sandbox | Docker (optional) | `BASH_SANDBOX=docker` |
| Package | pnpm 10 + uv | Monorepo + Python packages |

## Project Structure / 项目结构

```
hiveweave/
├── apps/
│   ├── hiveweave-py/                  # Backend — Python/FastAPI (port 4000)
│   │   └── src/hiveweave/
│   │       ├── agents/                # Agent lifecycle + Supervisor + trigger
│   │       ├── api/                   # 19 FastAPI router modules, 96 routes
│   │       ├── llm/                   # Streamer, provider factory, retry, circuit_breaker
│   │       ├── services/              # 23 services (org, dispatch, memory, handoff, MCP, ...)
│   │       ├── tools/                 # 11 built-in tools (bash, file, grep, patch, review, ...)
│   │       ├── conversation/          # Token budget, compaction, conversation store
│   │       ├── db/                    # Meta DB + Per-project DB (aiosqlite)
│   │       ├── realtime/              # phoenix_adapter, channels, pubsub, event_bus
│   │       └── prompts/               # ETHOS prompt system (identity + context)
│   └── web/                           # Frontend — React 19 + Vite + Electron (port 5173)
├── docs/
│   ├── migration/                     # Migration history (Elixir/TS → Python)
│   └── PoE2LI-team-config.md          # Example team configuration
├── start-all.bat                      # Windows startup script
└── CLAUDE.md                          # AI tooling instructions
```

## How It Works / 工作流程

```
1. 创建项目 → CEO + HR 自动生成
2. CEO 摸底 (EXPLORE) → 读文档 → 选组织范式 → 设计纪律套装
3. CEO → HR: "招一个后端经理，纪律用 Manager Suite"
4. HR: 绑定纪律技能 (必绑) → 搜市场补工具技能 → 创建 Agent
5. 后端经理到位 → EXPLORE 自己的领域 → 拆任务 → 向 HR 招下属
6. Executor 写代码 → self-review 自审 → QA 审查 → 经理验收 → CEO 对齐 → 你肉眼看
7. 每个肉眼可见的节点后 → 下一批任务
```

## Features / 特性

| Feature / 特性 | Description |
|:---|------|
| **Role-based models** | CEO/Expert get premium LLMs; Executors get cheap ones. Cost-effective at scale. |
| **Worktree isolation** | Each agent has independent `git worktree`. Parallel agents, zero conflicts. |
| **CAVEMAN comms** | Agent-to-agent messages are terse and technical. No pleasantries, no token waste. |
| **4-layer review gate** | Executor → QA → Manager → CEO → You. Nothing reaches you unverified. |
| **Natural language user involvement** | Not enum config. "我只在前端功能完成后验收" — CEO interprets and honors it. |
| **Asyncio task isolation** | Agent crash doesn't crash the system. Circuit breaker + exponential retry for LLM outages. |
| **Game time scheduling** | 15 real minutes = 1 game day. Stalled agents auto-escalate. Alarms on simulated clock. |
| **MCP protocol** | Tool extension via Model Context Protocol. Bind MCP servers per agent. |
| **ClawHub marketplace** | Remote skill marketplace. HR searches and binds skills dynamically. |
| **30+ built-in tools** | bash, grep, file ops, patch, websearch, question, todowrite, review, security, MCP tools. |

## Documentation / 文档

- [CLAUDE.md](./CLAUDE.md) — AI tooling instructions & architecture deep-dive
- [Migration History](./docs/migration/) — Elixir/TS → Python migration records
- [PoE2LI Team Config](./docs/PoE2LI-team-config.md) — Example team configuration template

## Contributing / 贡献

HiveWeave is in active development. The project is built by AI agents (CEO + team) with human oversight at key verification nodes. See [CLAUDE.md](./CLAUDE.md) for the full development workflow.

---

<p align="center">
  Built with HiveWeave — an AI engineering organization that builds itself.
</p>
