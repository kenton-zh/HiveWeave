# HiveWeave 后端迁移工程：Elixir/TS → Python

> **本文件是跨 AI、跨工具、跨对话的统一信息源。**
> 任何 AI 工具（Claude Code / OpenCode / Cursor / Codex 等）接入迁移工作前，**必须先读本目录下所有文件**，并在完成工作后更新对应文件。
> 本文件本身只记录工程元信息和当前状态，具体内容在子文件中。

## 工程元信息

| 项 | 值 |
|---|---|
| 工程名称 | HiveWeave 后端迁移：Elixir/TS → Python |
| 启动日期 | 2026-07-05 |
| 决策来源 | `backend-stack-comparison.html`（根目录）+ 用户确认 |
| 当前阶段 | **Phase 0：功能契约盘点**（未开始） |
| 目标栈 | Python 3.12 + FastAPI + LangGraph + Pydantic AI + OpenTelemetry/Phoenix |
| 源栈（当前） | Elixir/Phoenix（`apps/hiveweave/`，active）+ TS/Fastify（`apps/server/`+`packages/`，reference） |
| 并发上限 | 100 agent（用户确认，影响并发模型选型） |
| 代码开发方 | AI 开发，工作量/难度不在考虑范围，只关心最终效果 |
| **Git 冻结点** | `main` 分支 commit `3e7282e` — "checkpoint: pre-migration snapshot" |
| **迁移分支** | `migration/elixir-to-python`（基于 `3e7282e` 创建） |
| **回退方式** | `git checkout main` 即可回到迁移前状态 |

## 当前状态速览

| 阶段 | 状态 | 负责人/工具 | 最后更新 |
|---|---|---|---|
| Phase 0: 功能契约盘点 | ⏳ 未开始 | — | — |
| Phase 1: 迁移路径规划 | ⏳ 未开始 | — | — |
| Phase 2: Python 骨架搭建 | ⏳ 未开始 | — | — |
| Phase 3: 逐模块迁移 | ⏳ 未开始 | — | — |
| Phase 4: 并行验证 | ⏳ 未开始 | — | — |
| Phase 5: 切换上线 | ⏳ 未开始 | — | — |

## 目录结构

```
docs/migration/
├── README.md                      ← 本文件，工程元信息+当前状态（任何 AI 先读这个）
├── decision-record.md             ← 决策记录：为什么迁、迁什么、不迁什么
├── feature-contracts/             ← 功能契约清单（层 1，必须 1:1）
│   ├── 00-template.md             ← 契约模板
│   ├── 01-llm-streaming.md        ← LLM 流式调用
│   ├── 02-tool-executor.md        ← 工具执行器
│   ├── 03-conversation-store.md   ← 对话历史与压缩
│   ├── 04-agent-orchestration.md  ← 多 agent 编排
│   ├── 05-memory-service.md       ← 三层记忆
│   ├── 06-inbox-handoff.md        ← 收件箱与交接
│   ├── 07-game-time.md            ← 游戏时间
│   ├── 08-permission-approval.md  ← 权限与审批
│   ├── 09-git-worktree.md         ← Git worktree 隔离
│   ├── 10-mcp-skill.md            ← MCP 与技能
│   ├── 11-database.md             ← 两层 SQLite
│   ├── 12-realtime-channel.md     ← 实时通信
│   └── 13-prompt-ethos.md         ← ETHOS 提示词体系
├── constants.md                   ← 常量与不变量（层 2，精确复制）
├── known-issues.md                ← 已知问题清单（层 3，显式不迁移）
├── migration-plan.md              ← 迁移路径规划（Phase 1 产出）
├── progress.md                    ← 进度追踪表（每次工作后更新）
└── handoff-log.md                 ← 跨 AI/跨对话交接日志
```

## AI 工具接入协议

**任何 AI 工具开始迁移工作前，按以下顺序操作：**

1. **读本目录所有文件**，至少读 `README.md`、`progress.md`、`handoff-log.md`、`decision-record.md`
2. 在 `handoff-log.md` 追加一条"接入记录"，写明：接入时间、AI 工具名、会话 ID、打算做什么
3. 检查 `progress.md` 当前状态，确认自己要做的模块未被标记为"进行中"
4. 开始工作前，将自己负责的模块在 `progress.md` 标记为"进行中"
5. 完成工作后：
   - 更新 `progress.md`（标记完成度、记录关键变更）
   - 在 `handoff-log.md` 追加"完成记录"，写明：做了什么、改了哪些文件、遇到什么问题、下一个接手的人需要注意什么
6. **不要删除前人的记录**，只追加，保持完整历史

## 迁移方法论

本工程采用以下方法论组合（来源见 `decision-record.md`）：

- **Migration Compass**（clawhub `@jcools1977/migration-compass`）：三定律 + 四阶段 + 六类型，提供安全迁移路径
- **Codebase Migration Planner**（clawhub `@charlie-morrison/codebase-migration-planner`）：assess/plan/track/risks/effort 命令
- **writing-plans**（obra/superpowers）：No Placeholders + per-task Interfaces + Global Constraints
- **三层对照策略**（本工程自定义，见下文）

## 三层对照策略

为避免"把 bug 也迁移过来"，功能对照分三层：

| 层 | 文件 | 对照方式 | 说明 |
|---|---|---|---|
| 层 1：功能契约 | `feature-contracts/*.md` | 1:1，但用 spec 描述 | 每个模块的输入/输出/副作用，不引用实现代码 |
| 层 2：常量不变量 | `constants.md` | 精确复制 | 游戏时间、token budget、超时值、journal mode 等 |
| 层 3：已知问题 | `known-issues.md` | 显式剔除 | 列出 Elixir/TS 实现中的 bug、quirk、技术债，迁移时用正确方式做 |

## 迁移定律（来自 Migration Compass）

1. **Never Big-Bang** — 一次只改一个模块，验证，继续或回滚
2. **Parallel Before Replace** — Python 实现必须与 Elixir 并行运行对比后才能替换
3. **Every Step Must Be Deployable** — 迁移过程中任何一点都能部署

## 重要约束（全局）

- **不修改 `apps/hiveweave/` 和 `apps/server/`+`packages/` 的源码**，它们是迁移的参照源，冻结不动
- Python 新实现放在 `apps/hiveweave-py/`（待创建）
- 所有功能契约必须经过用户确认后才能进入实现阶段
- 每个模块迁移完成后，必须有并行对比测试证明行为等价
