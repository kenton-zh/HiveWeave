# HiveWeave 技术栈总结

## 项目概述

**HiveWeave** — AI 工程组织多 Agent 层级协作编程平台。不是"AI 编程工具"，而是一个会自我演化的 AI 工程组织。

## 技术栈总览

| 层级 | 技术 | 版本/说明 |
|------|------|-----------|
| **后端** | Python + FastAPI + Uvicorn | Python >=3.12, 端口 4000 |
| **前端** | React 19 + Vite + React Flow + Electron | 端口 5173 |
| **数据库** | SQLite + aiosqlite | Meta DB (WAL) + Per-project DB (DELETE journal) |
| **AI/LLM** | httpx 流式 SSE + provider factory | OpenAI 兼容,多 provider |
| **实时通信** | phoenix.js (前端) + phoenix_adapter (后端) | WebSocket, 3 channel |
| **包管理** | pnpm 10.31 (前端) + uv (后端) | monorepo |
| **构建工具** | Turbo | 前端任务编排 |
| **运行时** | Node.js >=22 <24 + Python >=3.12 | |
| **沙箱** | Docker (可选, BASH_SANDBOX=docker) | |

## 目录结构

```
hiveweave/
├── apps/
│   ├── hiveweave-py/              # Python/FastAPI 后端 (port 4000)
│   │   ├── src/hiveweave/
│   │   │   ├── agents/            # Agent + Supervisor + trigger
│   │   │   ├── api/               # FastAPI 路由 (19 模块, 96 路由)
│   │   │   ├── conversation/      # 对话历史 + 压缩 + token budget
│   │   │   ├── db/                # Meta DB + per-project DB
│   │   │   ├── llm/               # streamer, circuit_breaker, provider, retry
│   │   │   ├── prompts/           # ETHOS 提示词体系
│   │   │   ├── realtime/          # phoenix_adapter, channels, pubsub, event_bus
│   │   │   ├── services/          # 23 services (org, dispatch, memory, ...)
│   │   │   ├── tools/             # executor + 11 工具模块
│   │   │   ├── config.py
│   │   │   └── main.py
│   │   ├── data/                  # Meta DB (gitignored)
│   │   ├── pyproject.toml
│   │   └── uv.lock
│   └── web/                       # React 19 + Vite + Electron (port 5173)
│       └── src/
│           ├── components/        # ChatPanel, OrgTree, AgentNode, ...
│           ├── utils/             # game-time, role-styles, ...
│           ├── store.ts           # 全局状态
│           └── api.ts             # API 调用封装
├── docs/
│   └── migration/                 # 迁移历史文档
├── package.json                   # 根 (pnpm workspace, 仅前端)
├── pnpm-workspace.yaml
├── turbo.json
└── start-*.bat                    # 启动脚本
```

## 数据模型

19 张表,分布在 Meta DB 和 per-project DB 中:

| 表 | 用途 |
|----|------|
| `agents` | Agent 定义与配置 |
| `agent-templates` | Agent 预设模板 |
| `projects` | 项目定义 |
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
| `agent-charters` / `charter-attachments` | 项目章程 |

## 核心架构特性

1. **模型分级策略** — 协调者用高级模型做规划与审查；执行者用廉价模型写代码
2. **精准对话与上下文隔离** — 用户可直接与任意层级 Agent 对话,每个 Agent 只装载自己负责的代码
3. **Git Worktree 隔离开发** — 每个 Agent 拥有独立 Git worktree,审核后才合并
4. **三层记忆与交接继承** — 项目共享记忆 / Agent 私有记忆 / 归档记忆
5. **asyncio 任务级容错** — 每个 Agent 运行在独立 asyncio task 中,单个 Agent 崩溃不影响整体
6. **长任务支持** — 多轮工具循环、自重触发、上下文压缩
7. **30+ 内置工具** — 文件操作、代码搜索、命令执行、网络、代码审查、MCP 集成、协作工具

## 快速开始

```bash
# 启动后端 + 前端
start-all.bat

# 或单独启动
start-backend.bat     # Python/FastAPI, 端口 4000
start-frontend.bat    # React/Vite, 端口 5173

# 浏览器打开 http://localhost:5173
```

后端依赖安装:
```bash
cd apps/hiveweave-py
uv sync               # 或 pip install -e .
```

前端依赖安装:
```bash
pnpm install
```
