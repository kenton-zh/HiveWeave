# HiveWeave 技术栈总结

> 基于当前工作区快照生成

## 项目概述

**HiveWeave** — AI 工程组织多 Agent 层级协作编程平台。不是"AI 编程工具"，而是一个会自我演化的 AI 工程组织。

## 技术栈总览

| 层级 | 技术 | 版本/说明 |
|------|------|-----------|
| **活跃后端** | Elixir + Phoenix + Bandit | Erlang/OTP 26, 端口 4000 |
| **遗留后端** | Node.js + Fastify + TypeScript | 端口 3200，仅参考，勿启动 |
| **前端** | React 19 + Vite + React Flow + Electron | 端口 5173 |
| **数据库** | SQLite + Ecto（Elixir）/ Drizzle ORM（TS 遗留） | 19 张表 |
| **函数式编程** | Effect（TS 参考）/ Elixir 原生 | |
| **AI SDK** | Elixir 原生 OpenAI 兼容流式实现 | TS 参考：`ai` SDK + Provider Factory |
| **MCP** | `@modelcontextprotocol/sdk`（TS）/ 内置 `mcp_call`（Elixir） | |
| **包管理** | pnpm 10.31.0 | monorepo |
| **构建工具** | Turbo | 缓存 + 任务编排 |
| **运行时** | Node.js >=22.0.0 <24.0.0 | TypeScript 5.8 |
| **沙箱** | Docker（TS `BASH_SANDBOX=docker`） | |

## 目录结构

```
hiveweave/
├── apps/
│   ├── hiveweave/                  # ✅ 活跃后端 — Elixir/Phoenix (port 4000)
│   │   └── lib/
│   │       ├── hiveweave/
│   │       │   ├── agents/         # Agent GenServer (3-state + self-retrigger)
│   │       │   ├── llm/            # streamer, circuit_breaker, provider_factory, retry
│   │       │   ├── services/       # 16 services (org, dispatch, memory, handoff, ...)
│   │       │   ├── tool_executor.ex # 30+ tools (bash, file ops, grep, websearch, MCP, ...)
│   │       │   ├── conversation_store.ex
│   │       │   └── compaction/     # context overflow handling
│   │       └── hiveweave_web/
│   │           ├── channels/       # WebSocket (lobby, project, agent)
│   │           ├── controllers/    # 8 controllers + API key auth plug
│   │           └── router.ex
│   ├── server/                     # ⚠️ 遗留后端 — TS/Fastify (port 3200)，仅参考
│   │   └── src/
│   │       ├── routes/             # API 路由 (chat, org, projects, fs, mcp, ...)
│   │       └── game-time-scheduler.ts
│   └── web/                        # 前端应用 — React 19 + Vite + Electron (port 5173)
│       └── src/
│           ├── components/         # 组件 (ChatPanel, OrgTree, AgentNode, ...)
│           ├── store.ts            # 全局状态
│           └── api.ts              # API 调用封装
├── packages/
│   ├── agent-runtime/              # Agent 执行引擎（TS 参考实现）
│   │   └── src/
│   │       ├── agent-runtime.ts    # 核心运行时 (streamText/generateText)
│   │       ├── permissions.ts      # 工具权限控制
│   │       └── provider-factory.ts # 模型 Provider 工厂
│   ├── core/                       # 业务核心服务 (25+ services，TS 参考实现)
│   │   └── src/
│   │       ├── services/           # OrgService, ModelService, DispatchService, ...
│   │       ├── tools/              # bash, grep, apply-patch, question, todowrite, websearch
│   │       ├── mcp/                # MCP 服务管理
│   │       └── index.ts            # 统一导出
│   ├── db/                         # 数据库层
│   │   └── src/schema/             # 19 张表 (agents, projects, memories, ...)
│   └── shared/                     # 跨包共享类型/工具
│       └── src/                    # agent, api, charter, game-time, handoff, memory, ...
├── docs/
│   └── AI工程组织_MVP蓝图.md        # 产品蓝图
├── package.json                    # 根 package.json (pnpm workspace)
├── pnpm-workspace.yaml             # workspace 配置
├── turbo.json                      # Turbo 任务编排
├── tsconfig.base.json              # TS 基础配置
└── start-all.bat / start-backend.bat / start-frontend.bat  # 启动脚本
```

## 数据模型（19 张表）

| 表 | 用途 |
|----|------|
| `agents` | Agent 定义与配置 |
| `agent-templates` | Agent 预设模板 |
| `projects` | 项目定义 |
| `project-index` | 项目索引 |
| `memories` | 记忆存储 |
| `handoffs` | 交接记录 |
| `merges` | 合并记录 |
| `chat-messages` | 聊天消息 |
| `conversation-turns` | 对话轮次 |
| `inbox` | 收件箱 |
| `personnel-records` | 人事档案 |
| `modules` | 模块/组件 |
| `llm-models` | LLM 模型配置 |
| `permission-requests` | 权限请求 |
| `work-logs` | 工作日志 |
| `scheduled-alarms` | 定时闹钟 |
| `meta-index` | 元索引 |
| `agent-charters` / `charter-attachments` | 项目章程 |

## 核心架构特性

1. **模型分级策略** — 协调者用高级模型做规划与审查；执行者用廉价模型写代码
2. **精准对话与上下文隔离** — 用户可直接与任意层级 Agent 对话，每个 Agent 只装载自己负责的代码
3. **Git Worktree 隔离开发** — 每个 Agent 拥有独立 Git worktree，审核后才合并
4. **三层记忆与交接继承** — 项目共享记忆 / Agent 私有记忆 / 归档记忆
5. **BEAM 进程级容错** — 每个 Agent 运行在独立 BEAM 进程中，单个 Agent 崩溃不影响整体
6. **长任务支持** — 多轮工具循环、自重触发、上下文压缩
7. **30+ 内置工具** — 文件操作、代码搜索、命令执行、网络、代码审查、MCP 集成、协作工具

## 快速开始

```bash
# 启动后端 + 前端
start-all.bat

# 或单独启动
start-backend.bat     # Elixir/Phoenix，端口 4000
start-frontend.bat    # React/Vite，端口 5173

# 浏览器打开 http://localhost:5173
```

TS 包依赖安装与构建（仅参考实现需要）：
```bash
pnpm install
pnpm db:push
pnpm turbo typecheck
pnpm build
```
