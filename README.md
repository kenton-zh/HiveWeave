# HiveWeave

AI 工程组织 — 多 Agent 层级协作编程平台。

> **不是"AI 编程工具"，而是一个会自我演化的 AI 工程组织。**

Agent 有职级、记忆可继承、离职有交接。用户可以像管理真实团队一样管理 AI 团队——有组织架构、任务分配、编制调整、审批流转。每个 Agent 在独立的 Git 工作区中开发，经上级审核通过后才合并至主分支。

## 为什么不用单 Agent

Claude Code、Cursor 这类工具是一个"全能型"Agent 独立完成所有工作。HiveWeave 换了个思路：**像真实工程团队一样分工协作**。这个架构带来了四个单 Agent 无法实现的优势：

| 优势 | 做法 | 效果 |
|------|------|------|
| **模型成本优化** | 协调者（CEO/架构师）用高级模型做规划与审查；执行者（叶子 Agent）用廉价模型写代码 | 把昂贵的 token 花在决策上，重复性的编码交给便宜模型 |
| **上下文隔离** | 前端 Agent 只装前端代码，后端 Agent 只装后端代码；用户直接和具体负责人对话 | 单 Agent 上下文里塞满整个项目容易混淆；按职责隔离后每个 Agent 专注自己那部分 |
| **并行隔离开发** | 每个 Agent 拥有独立 Git worktree，在自己的分支上写代码 | 多个 Agent 同时开发不同模块互不干扰 |
| **审核后合并** | 执行者完成后上报，协调者审查代码与 worktree 状态，通过才合并到主分支 | 代码质量有闸门，不合格直接驳回返工 |

## 核心特性

### 模型分级策略
- **按角色分配模型** — 系统维护 `default_model_coordinator` 和 `default_model_executor` 两套独立模型配置；协调者用 Claude/GPT-4 级别模型做规划与审查，执行者用 DeepSeek 级别模型写代码
- **单 Agent 粒度覆盖** — 每个 Agent 可单独指定模型，灵活混合多供应商（OpenAI / Anthropic / DeepSeek / Groq 等）
- **成本可控** — 决策 token 少但贵，编码 token 多但便宜，总体成本远低于全程使用顶级模型

### 精准对话与上下文隔离
- **直达任意层级** — 用户可以直接与子节点、甚至叶子 Agent 对话，不必事事通过 CEO 中转。前端有问题找前端 Agent，后端有问题找后端 Agent，各修各的
- **上下文按职责隔离** — 每个 Agent 的上下文只装载自己负责的代码和职责，前端 Agent 只看前端代码，后端 Agent 只看后端代码，不会把整个项目塞进一个上下文里导致混淆
- **超大型项目按模块拆分** — 可以为每个模块分配专属 Agent（如 `auth-agent`、`payment-agent`、`search-agent`），每个 Agent 只维护自己负责的模块代码，上下文精简、职责清晰
- **多面板并行对话** — 前端可同时打开多个 Agent 的对话面板，分别给前端、后端、测试 Agent 下达指令，互不干扰

### Git Worktree 隔离开发
- **独立工作区** — 每个 Agent 获得隔离的 Git worktree（`.hiveweave/worktrees/<shortId>/`），在专属分支 `hw/<shortId>/<task>` 上开发
- **检查点机制** — Agent 可随时 `checkpoint` 保存进度（轻量 commit），支持回滚到任意检查点
- **审核合并流程** — 执行者 `report_completion` → 协调者 `review_code` 审查日志与 worktree 状态 → `approve_work` 合并到 main / `reject_work` 驳回返工
- **安全回滚** — 审核不通过可 `rollback` 到之前的检查点，不会污染主分支

### Agent 组织管理
- **动态层级架构** — CEO → HR → 技术负责人/架构师/经理 → 叶子 Agent，上级协调不写代码，叶子才写代码
- **职责分离** — 协调者只有只读文件权限（不能直接写代码），执行者没有派发权限（不能 spawn 下级），权限矩阵强制分工
- **人事档案系统** — 职级、履历、权限类型、任职记录，像管理真实团队一样管理 Agent
- **模板系统** — 从预设模板一键创建 Agent（test_engineer、code_reviewer、security_auditor 等）

### 三层记忆与交接继承
- **三层记忆模型** — 项目共享记忆 / Agent 私有记忆 / 归档记忆
- **记忆继承** — Agent 离职时正式交接（解散 → 总结 → 移交 → 归档 → 可复活），记忆可沿袭给继任者
- **Merge 合并** — 冲突检测 → 仲裁 → 合成新记忆，支持多 Agent 知识融合

### BEAM 进程级容错
- **进程隔离** — 每个 Agent 运行在独立 BEAM 进程中，单个 Agent 崩溃不会拖垮其他 Agent 或系统
- **熔断器** — LLM 提供商故障时自动熔断，带探针锁防止多个 Agent 同时冲击不稳定的 API
- **自动重试** — 429/503/504/529 指数退避 + jitter，解析 `Retry-After` 头
- **Supervisor 重启策略** — `max_restarts` 限制防止崩溃风暴

### 长任务支持
- **多轮工具循环** — 执行者单次触发最多 80 轮工具调用，协调者最多 60 轮
- **自重触发** — 处理完成后如有新 inbox 消息自动再次触发，支持超越单次上限的长任务链
- **上下文压缩** — token 预算裁剪 + LLM 摘要压缩（compactor），超长对话不溢出
- **游戏时间调度** — 15 真实分钟 = 1 游戏天，停滞 Agent（15+ 分钟无活动）自动升级到上级处理

### 工具系统（30+ 内置工具）
- **文件操作** — `read_file` / `write_file` / `edit_file` / `apply_patch` / `delete_file` / `move_file` / `create_directory` / `list_files`
- **代码搜索** — `grep` / `glob` / `search_files`
- **命令执行** — `bash`（带自毁命令阻断、路径沙箱）
- **网络** — `websearch` / `fetch_url`
- **代码审查** — `review`（runCodeReview / runSecurityAudit / runTestReview / runPerfAudit / runFullReview）
- **MCP 集成** — `mcp_list_tools` / `mcp_call`，可对接任意 MCP 服务器
- **协作工具** — `dispatch_task` / `report_completion` / `message_superior` / `send_message` / `review_code` / `approve_work` / `reject_work`
- **组织管理** — `hire_agent` / `list_subordinates` / `read_work_logs` / `write_work_log` / `write_memory` / `read_project_memory`
- **按角色分层授权** — 协调者拥有 git worktree 管理工具，执行者拥有文件写入工具，互不越权

### 通信与协作
- **跨级直达通信** — 可在组织架构任意层级间发出消息
- **团队聊天** — TeamChat 支持多 Agent 群组讨论
- **收件箱系统** — 消息投递 + 定时提醒 + 优先级
- **CAVEMAN 通信风格** — Agent 间通信使用简洁技术语言，去掉寒暄和客套，降低 token 消耗

### 实时可视化
- **组织架构图** — React Flow 驱动的可视化层级图，拖拽可缩放
- **多面板对话** — 同时与多个 Agent 对话
- **Agent 详情面板** — 实时查看 Agent 状态、记忆、待办、工作日志
- **实时流式** — Phoenix Channels WebSocket 推送，token 级实时响应
- **Electron 桌面端** — 可作为桌面应用运行

### 扩展性
- **MCP 协议** — Model Context Protocol 支持，可对接任意 MCP 服务器扩展工具能力
- **ClawHub** — Skill 市场 / 插件注册机制
- **定时闹钟** — 基于游戏时间的定时消息触发，支持长周期任务调度

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
