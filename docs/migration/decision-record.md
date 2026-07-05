# 决策记录

> 本文件记录迁移工程的关键决策及其理由。任何 AI 工具质疑决策时，先读这里的依据。

## 决策 0：以 OpenCode 原项目为 P0 参考源

**决策**：迁移过程中遇到不确定的设计决策时，以 `D:\PC_AI\Project\opencode`（OpenCode 原项目）为最高优先参考。

**日期**：2026-07-05

**决策依据**：
- HiveWeave 的 TS 参考实现（`packages/core/`、`packages/agent-runtime/`）本身就是从 OpenCode 衍生的
- OpenCode 是成熟的 TS 项目，经过大量生产验证，其 compaction、token 估算、工具执行等核心逻辑是 HiveWeave TS 实现的源头
- Elixir 实现在移植过程中可能引入偏差或简化（如 MCP 降级为 HTTP-only），OpenCode 是更可靠的"原始设计意图"来源

**参考优先级**：P0 OpenCode → P1 HiveWeave TS → P2 HiveWeave Elixir

**已用 OpenCode 确认的决策**：
- `tail_turns = 2`（OpenCode 默认值，Elixir 自行调大为 4 不采用）
- 停滞检测：OpenCode 无此机制（CLI 工具），采用 Elixir 的双阈值模型
- Per-project DB 单连接（OpenCode Effect SqlClient 是单连接模型）
- 端口 4000（OpenCode 无端口概念，基于前端兼容性决定）

---

## 决策 1：整体换栈 Python，而非混合架构

**决策**：将 Elixir/Phoenix 后端整体迁移到 Python（FastAPI + LangGraph + Pydantic AI），而非保留 Elixir 壳 + Python 微服务的混合架构。

**日期**：2026-07-05

**决策依据**：
- 用户确认并发上限 100 agent。在此量级下，BEAM 的"百万轻量进程"优势不再关键，asyncio task 足以覆盖
- Python 在 LLM SDK 首发、agent 编排框架（LangGraph）、AI 代码生成可靠度上对 Elixir 形成代差优势
- 代码由 AI 开发，Python 语料远多于 Elixir，AI 生成错误率低，迭代更快
- 混合架构在 100 并发下显得过度工程，两套代码库维护成本超过收益
- 完整对比分析见根目录 `backend-stack-comparison.html`

**反对意见（已考虑但被推翻）**：
- BEAM 的 supervisor 进程级隔离是原生优势，Python 无等价物 → 100 并发下可用 asyncio task + 状态持久化（LangGraph 检查点）近似补齐
- Elixir 已实现的工程资产（OTP 监督树、CircuitBreaker、crash_recovery）难以等价复刻 → 这些在 Python 中需重写，但 LangGraph 的 durable execution + 自建 supervisor 逻辑可覆盖核心需求

**被否决的方案**：
1. 保留 Elixir + Python 微服务混合架构 — 100 并发下过度工程
2. 换 TypeScript — TS 相对 Elixir 在并发/容错维度没有拉开差距，但 Python 在 LLM 生态上有代差优势
3. 保留 Elixir 不迁 — AI 代码生成可靠度弱，LLM 生态落后，MCP 降级

---

## 决策 2：三层对照策略，而非逐行代码对照

**决策**：功能对照分三层（功能契约 / 常量不变量 / 已知问题），而非把 Elixir 代码逐行翻译到 Python。

**理由**：
- 逐行翻译会把 Elixir 实现中的 bug 一起搬到 Python
- 用 spec 语言描述行为契约，让 AI 在 Python 实现时按契约做，自然不会复制实现层面的错误
- 那些在 Elixir 里因 BEAM 特性而隐藏的 bug（如 race condition 被 GenServer 串行化掩盖），在 Python 里会暴露，反而能被发现和修复

**三层定义**：
- 层 1（功能契约）：1:1 对照，但用 spec 描述输入/输出/副作用，不引用实现代码
- 层 2（常量不变量）：精确复制，如游戏时间 3600 秒/天、token budget 区间
- 层 3（已知问题）：显式列出 Elixir/TS 的 bug 和技术债，迁移时剔除并用正确方式重做

---

## 决策 3：采用 Migration Compass + Codebase Migration Planner 方法论

**决策**：迁移工程遵循 Migration Compass 三定律 + Codebase Migration Planner 的 assess/plan/track/risks 命令。

**来源**：
- Migration Compass: clawhub `@jcools1977/migration-compass`
- Codebase Migration Planner: clawhub `@charlie-morrison/codebase-migration-planner`
- writing-plans: obra/superpowers

**三定律**：
1. Never Big-Bang — 一次只改一个模块
2. Parallel Before Replace — Python 实现必须与 Elixir 并行对比后才能替换
3. Every Step Must Be Deployable — 迁移过程中任何一点都能部署

**为什么不直接用 superpowers/spec-kit**：它们是通用开发流程，没有专门处理"跨语言 1:1 功能重构"的对照和并行验证机制。Migration Compass 的 Type 3（Language Migration）+ Parallel Run 策略正好覆盖。

---

## 决策 4：目标技术栈选型

**决策**：Python 3.12 + FastAPI + LangGraph + Pydantic AI + OpenTelemetry/Phoenix

**各组件职责**：
| 组件 | 职责 | 对应 Elixir 模块 |
|---|---|---|
| FastAPI | Web 框架，SSE/WebSocket | Phoenix Endpoint + Channels |
| LangGraph | agent 编排，状态机，检查点，HITL | Agent GenServer + Handoff/Approval |
| Pydantic AI | 结构化输出，类型安全 | Effect + Zod |
| OpenTelemetry + Phoenix | 可观测性 | Telemetry |
| asyncio | 并发模型 | BEAM 进程 |
| aiosqlite + SQLAlchemy | 两层 SQLite | Ecto + Exqlite |
| httpx | LLM 流式调用 | Req + Finch |

**待定项**（需在 Phase 1 确认）：
- 实时广播方案：FastAPI 无原生 PubSub，候选：Redis PubSub / Postgres LISTEN / 进程内广播（单实例够用）
- supervisor 式容错：自建 asyncio task 重启逻辑 vs multiprocessing 硬隔离

---

## 决策 5：源码冻结

**决策**：迁移期间不修改 `apps/hiveweave/` 和 `apps/server/`+`packages/` 的源码。

**理由**：它们是迁移的参照源，必须保持稳定。任何修改都会导致功能契约失效。Python 新实现放在 `apps/hiveweave-py/`（待创建）。
