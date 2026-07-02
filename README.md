# HiveWeave

AI 工程组织 — 多 Agent 层级协作编程助手。

> **不是"AI 编程工具"，而是一个会自我演化的 AI 工程组织。**

Agent 有职级、记忆可继承、离职有交接。用户可以像管理真实团队一样管理 AI 团队——有组织架构、任务分配、编制调整、审批流转。

## 核心特性

### Agent 管理
- **动态层级组织架构** — 架构师 / 经理 / 叶子 Agent，上级协调不写代码，叶子才写代码
- **人事档案系统** — 职级、履历、权限类型（协调型 / 执行型）、任职记录
- **模板系统** — 从预设模板一键创建 Agent
- **编制控制** — Agent 扩张需审批闸门

### 记忆系统
- **三层记忆模型** — 项目共享记忆 / Agent 私有记忆 / 归档记忆
- **记忆继承** — Agent 离职时正式交接，记忆可沿袭
- **Handoff 交接** — 解散 → 总结 → 移交 → 归档 → 可复活
- **Merge 合并** — 冲突检测 → 仲裁 → 合成新记忆

### AI 引擎
- **多模型支持** — 通过 Provider Factory 接入 OpenAI / DeepSeek / Anthropic 等模型
- **Agent Runtime** — 基于 `ai` SDK 的 Agent 执行引擎（TS 参考实现），Elixir 后端镜像其设计，内置重试、退避、错误分类、上下文溢出检测
- **Effect 函数式编程** — 类型安全的工具执行链（TS 参考实现）
- **工具系统** — `bash` / `read_file` / `write_file` / `edit_file` / `apply_patch` / `grep` / `glob` / `websearch` / `fetch_url` / `mcp_call` / `review` 等 30+ 内置工具，按角色分层授权

### 通信与协作
- **跨级直达通信** — 可在组织架构任意层级间发出消息
- **团队聊天** — TeamChat 支持多 Agent 群组讨论
- **收件箱系统** — 消息投递 + 定时提醒
- **用户 Pings** — Agent 向用户反馈进度/问题的红点通知机制

### 权限与审批
- **权限矩阵** — 协调型/执行型权限预设
- **审批流** — 申请 → 等待 → 响应/取消，Manager 验收权
- **升级规则** — 阻塞自动上报

### 扩展性与集成
- **MCP 协议** — Model Context Protocol 支持，可对接任意 MCP 服务器
- **ClawHub** — Skill 市场 / 插件注册机制
- **游戏时间系统** — 加速时间线模拟，支持长时间运行场景
- **定时闹钟** — 基于游戏时间的定时消息触发

### 前端可视化
- **组织架构图** — React Flow 驱动的可视化层级图，拖拽可缩放
- **多面板对话** — 同时与多个 Agent 对话
- **Agent 详情面板** — 实时查看 Agent 状态、记忆、待办
- **红点通知** — 来自 Agent 的待办/消息提醒

## 技术栈

| 层级 | 技术 |
|------|------|
| **活跃后端** | Elixir 1.17 + Phoenix + Bandit（端口 4000） |
| **遗留后端** | Node.js + Fastify + TypeScript（端口 3200，仅参考，勿启动） |
| **前端** | React 19 + Vite + React Flow + Electron（端口 5173） |
| **数据库** | SQLite + Ecto（Elixir）/ Drizzle ORM（TS 遗留） |
| **AI SDK** | `ai` SDK + Provider Factory（TS 参考）；Elixir 原生 OpenAI 兼容流式实现 |
| **函数式编程** | Effect（TS 参考）/ Elixir 原生 |
| **MCP** | `@modelcontextprotocol/sdk`（TS）/ 内置 `mcp_call`（Elixir） |
| **沙箱** | Docker（TS `BASH_SANDBOX=docker`） |
| **运行时** | Erlang/OTP 26 + Node.js 22.x + pnpm 10 + Turbo |

## 项目结构

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
│   ├── server/                     # ⚠️ 遗留后端 — TS/Fastify (port 3200)，仅参考，勿启动
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
└── docs/
    └── AI工程组织_MVP蓝图.md        # 产品蓝图
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

## 快速开始

> **Erlang/Elixir 在非标准路径，不在系统 PATH 中。** 请使用启动脚本，它们会自动设置 PATH。

```bash
# 启动后端 + 前端（分别在独立窗口）
start-all.bat

# 或单独启动
start-backend.bat     # Elixir/Phoenix，端口 4000
start-frontend.bat    # React/Vite，端口 5173

# 浏览器打开 http://localhost:5173
```

TS 包的依赖安装与构建（仅参考实现需要）：

```bash
# 安装依赖（Node 22 + pnpm 10）
pnpm install

# 初始化 TS 遗留数据库（仅使用 TS 后端时需要）
pnpm db:push

# 类型检查 / 构建
pnpm turbo typecheck
pnpm build
```

## 文档

- [MVP 技术蓝图](./docs/AI工程组织_MVP蓝图.md) — 产品定位、架构决策、设计原则
