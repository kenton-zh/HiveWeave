<p align="center">
  <h1 align="center">HiveWeave</h1>
</p>
<p align="center"><strong>AI 工程组织</strong> — 多 Agent 层级协作编程平台</p>
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
  <a href="README.zh.md">中文</a>
</p>

---

## 这是什么

HiveWeave 把单 AI Agent 模式替换为**多 Agent 工程组织**。CEO、技术经理、开发工程师、QA、HR——每个角色有自己的职责、记忆、工具和独立工作区。他们招聘、分配任务、审查代码、合并分支、汇报进度。你像管理真实团队一样管理他们。

> **为什么**：单 Agent 工具（Claude Code、Codex、Cursor）跨模块丢上下文、无法并行开发、没有质量闸门。HiveWeave 把工作拆分给专业 Agent，每个拥有独立的上下文和工作区，经过四层把关后才到你眼前。

## 快速开始

```bash
# 克隆仓库
git clone https://github.com/kenton-zh/HiveWeave.git
cd HiveWeave

# 后端（Python/FastAPI，端口 4000）
cd apps/hiveweave-py
uv sync
uvicorn hiveweave.main:app --host 0.0.0.0 --port 4000

# 前端（React/Vite，端口 5173）
cd apps/web
export PATH="$LOCALAPPDATA/Programs/node-v22.20.0-win-x64:$PATH"  # 仅 Windows
pnpm install
pnpm dev
```

或使用启动脚本（Windows）：
```bash
start-all.bat          # 后端 + 前端
start-backend.bat      # 仅后端（端口 4000）
start-frontend.bat     # 仅前端（端口 5173）
```

打开 `http://localhost:5173`，创建第一个项目，认识你的 CEO。

## 架构

```
        你（人类操作者）
          ↕                    ↕（通过 question 工具 / 聊天）
        CEO ─── 专家（按需召唤，最贵模型）
          ↕
        技术经理 / PM / 架构师
          ↕
        QA + Executor（便宜模型执行）

        四层把关：
          Executor → QA（/review）→ 技术经理（规格符合）→ CEO（意图对齐）→ 你（肉眼验收）
```

| 层 | 角色 | 模型 | 职责 |
|:---|------|:---|------|
| 决策 | CEO | 顶级（Claude Opus / GPT-5） | 方向、规格、用户汇报 |
| 规划 | 技术经理 | 强（Claude Sonnet / GPT-4） | 架构、任务拆解、审查 |
| 质量 | QA | 中等 | 五轴代码审查、安全审计、E2E 测试 |
| 执行 | Executor | 便宜（DeepSeek / Haiku / Flash） | 写代码、跑测试、自审 |

## 核心能力

### 多 Agent 组织
- **动态层级** — CEO → HR → 经理 → Executor。协调者规划和审查，执行者写代码。各司其职。
- **招聘流程** — CEO 设计组织 → HR 招人 → 经理拆分领域 → 各自向 HR 招聘下属。三轮招人，匹配真实团队增长。
- **纪律套装** — 每个角色获得一组纪律技能（code-review-and-quality、self-review、security-and-hardening 等），定义"怎么思考"，不只是"用什么工具"。
- **双层技能绑定** — 纪律技能（必绑，定义角色）+ 工具技能（HR 在市场匹配）。HR 服务所有协调者，不只 CEO。

### 上下文隔离
- **按 Agent 隔离** — 前端 Agent 只看前端代码。后端 Agent 只看后端代码。互不污染。
- **按角色配模型** — CEO 用 Claude Opus，Executor 用 DeepSeek Flash。贵 token 花在决策上，便宜 token 花在执行上。
- **直达对话** — 你可以直接跟任意层级 Agent 对话。前端有问题？直接找前端开发。不用通过 CEO 中转。

### Git Worktree 开发
- **隔离工作区** — 每个 Agent 拥有独立 `git worktree`（`hw/<shortId>/<task>`）。并行开发零冲突。
- **检查点 + 回滚** — 风险操作前 checkpoint，随时回滚。不影响主分支。
- **审查 → 合并关卡** — Executor 提交 → QA 审查 → 经理审批 → CEO 签字 → 合并到主分支。四关之后代码才到你面前。

### 记忆与交接
- **三层记忆** — 项目记忆（共享）、Agent 记忆（私有）、归档记忆（离职 Agent）。知识跨会话持久化。
- **交接继承** — Agent 离职时记忆总结并移交继任者。知识零丢失。
- **持续学习** — Agent 可以把成功流程 `skillify` 固化为技能，从失败中 `learn` 经验。跨项目模式由 Boss 助理记录。

### 模型预算分层
- **按角色分模型** — 协调者用顶级模型做规划审查，执行者用便宜模型写代码。
- **专家通道** — 团队解决不了的问题，CEO 召唤专家 Agent 使用最贵模型。AI 提炼后的问题比人直接提问更精准，同样花费得到更好答案。
- **灵活配置** — 每个 Agent 可单独覆盖模型配置。混合使用 OpenAI、Anthropic、DeepSeek、Groq 等多供应商。

### 实时可视化
- **组织架构图** — React Flow 驱动。拖拽缩放，看清汇报关系。
- **多面板对话** — 同时跟多个 Agent 聊天。前端开发一个面板，后端开发另一个。
- **实时流式** — WebSocket token 级推送。实时看到 Agent 在打字。

## 技术栈

| 层 | 技术 | 备注 |
|:---|------|------|
| 后端 | Python 3.12 + FastAPI + Uvicorn | 端口 4000，96 路由，19 API 模块 |
| 前端 | React 19 + Vite + React Flow + Zustand | 端口 5173，支持 Electron 桌面端 |
| 数据库 | SQLite + aiosqlite | 双 DB：Meta DB（WAL）+ Per-project DB |
| AI/LLM | httpx SSE 流式 + Provider Factory | OpenAI、Anthropic、DeepSeek、Groq、Google |
| 实时通信 | phoenix.js + phoenix_adapter（WebSocket） | 3 频道：lobby、project、agent |
| 沙箱 | Docker（可选） | `BASH_SANDBOX=docker` |
| 包管理 | pnpm 10 + uv | Monorepo + Python 包 |

## 项目结构

```
hiveweave/
├── apps/
│   ├── hiveweave-py/                  # 后端 — Python/FastAPI（端口 4000）
│   │   └── src/hiveweave/
│   │       ├── agents/                # Agent 生命周期 + Supervisor + trigger
│   │       ├── api/                   # 19 个 FastAPI 路由模块，96 路由
│   │       ├── llm/                   # Streamer、provider factory、retry、circuit_breaker
│   │       ├── services/              # 23 个服务（org、dispatch、memory、handoff、MCP 等）
│   │       ├── tools/                 # 11 个内置工具（bash、file、grep、patch、review 等）
│   │       ├── conversation/          # Token budget、compaction、conversation store
│   │       ├── db/                    # Meta DB + Per-project DB（aiosqlite）
│   │       ├── realtime/              # phoenix_adapter、channels、pubsub、event_bus
│   │       └── prompts/               # ETHOS 提示词体系（identity + context）
│   └── web/                           # 前端 — React 19 + Vite + Electron（端口 5173）
├── docs/
│   ├── migration/                     # 迁移历史（Elixir/TS → Python）
│   └── PoE2LI-team-config.md          # 示例团队配置
├── start-all.bat                      # Windows 启动脚本
└── CLAUDE.md                          # AI 工具指令
```

## 工作流程

```
1. 创建项目 → CEO + HR 自动生成
2. CEO 摸底（EXPLORE）→ 读文档 → 选组织范式 → 设计纪律套装
3. CEO → HR："招一个后端经理，纪律用 Manager Suite"
4. HR：绑定纪律技能（必绑）→ 搜市场补工具技能 → 创建 Agent
5. 后端经理到位 → EXPLORE 自己的领域 → 拆任务 → 向 HR 招下属
6. Executor 写代码 → self-review 自审 → QA 审查 → 经理验收 → CEO 对齐 → 你肉眼看
7. 每个肉眼可见的节点通过后 → 下一批任务
```

## 特性

| 特性 | 说明 |
|:---|------|
| **按角色配模型** | CEO/专家用顶级 LLM；Executor 用便宜模型。规模化成本可控。 |
| **Worktree 隔离** | 每个 Agent 独立 `git worktree`。并行开发，零冲突。 |
| **CAVEMAN 通信** | Agent 间消息简洁技术化。无寒暄，不浪费 token。 |
| **四层把关** | Executor → QA → 经理 → CEO → 你。未经验证的代码到不了你面前。 |
| **自然语言参与度** | 不是枚举配置。"我只在前端功能完成后验收"——CEO 理解并遵守。 |
| **Asyncio 任务隔离** | Agent 崩溃不拖垮系统。熔断器 + 指数退避应对 LLM 故障。 |
| **游戏时间调度** | 15 真实分钟 = 1 游戏天。停滞 Agent 自动升级。基于模拟时钟的定时闹钟。 |
| **MCP 协议** | 通过 Model Context Protocol 扩展工具。按 Agent 绑定 MCP 服务器。 |
| **ClawHub 市场** | 远程技能市场。HR 动态搜索和绑定技能。 |
| **30+ 内置工具** | bash、grep、文件操作、patch、websearch、question、todowrite、review、security、MCP 工具。 |

## 文档

- [CLAUDE.md](./CLAUDE.md) — AI 工具指令与架构深度文档
- [迁移历史](./docs/migration/) — Elixir/TS → Python 迁移记录
- [PoE2LI 团队配置](./docs/PoE2LI-team-config.md) — 示例团队配置模板

## 致谢

HiveWeave 构建于以下项目的思想、代码和工作流之上：

| 项目 | 我们借鉴了什么 |
|:---|------|
| **[OpenCode](https://github.com/anomalyco/opencode)** | LLM 流式架构、token 估算（4 字符/token）、对话压缩、工具输出截断、熔断器模式。所有核心逻辑的 P0 参考源。 |
| **[gstack](https://github.com/garrytan/gstack)** | 工程规范流程系统 — `/spec` `/plan-eng-review` `/review` `/qa` `/ship` 管线。融入 HiveWeave 的**纪律套装**模型，用于 Agent 角色定义。技能路由规则和 ETHOS 原则同样源自于此。 |
| **[Harbor](https://github.com/laude-institute/harbor)** | 沙箱级网络隔离与按 Agent 白名单。用于 SWE-Marathon 评测。 |
| **[SWE-Marathon](https://github.com/abundant-ai/swe-marathon)** | 长程软件工程基准。HiveWeave 的目标验证套件，用于证明多 Agent 架构有效性。 |
| **[LangGraph](https://github.com/langchain-ai/langgraph)** | Agent 编排原语——状态机、检查点、持久化执行模式。 |
| **[FastAPI](https://github.com/fastapi/fastapi)** | 原生支持 WebSocket/SSE 的 Web 框架。 |
| **[React Flow](https://github.com/xyflow/xyflow)** | 组织架构图可视化引擎。 |

> **站在巨人的肩膀上**：这里列出的每个项目都解决了一个我们不需要重新解决的难题。我们组装、适配，并在其上叠加了多 Agent 协作层。

## 贡献

HiveWeave 正在活跃开发中。项目由 AI Agent（CEO + 团队）在人类的关键验证节点监督下构建。详见 [CLAUDE.md](./CLAUDE.md) 了解完整开发工作流。

---

<p align="center">
  由 HiveWeave 构建 — 一个会自我演化的 AI 工程组织。
</p>
