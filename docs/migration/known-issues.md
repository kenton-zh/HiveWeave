# 已知问题清单（层 3）

> **本文件列出 Elixir/TS 实现中已知的 bug、quirky behavior、技术债。这些在迁移到 Python 时必须显式剔除，用正确方式重做。**
> 每个问题必须标注：来源、现象、根因、Python 实现的正确做法。

## Elixir 后端已知问题

### E1: MCP 集成简化实现

| 项 | 内容 |
|---|---|
| 来源 | `apps/hiveweave/lib/hiveweave/tool_executor.ex:2988` + `skill_registry.ex` |
| 现象 | MCP 仅支持 HTTP JSON-RPC，`guess_mcp_url` 硬编码 4 个已知服务（github/filesystem/browser/database），无 stdio transport、无动态服务发现、无 MCP server 进程管理。`mcp_list_tools` 只返回静态列表，注释明确写"we don't have live connections" |
| 根因 | Elixir 生态无官方 MCP SDK，只能手写 HTTP 客户端 |
| Python 正确做法 | 使用官方 `mcp` Python SDK，完整支持 stdio + HTTP transport + 动态服务发现 |

### E2: per-agent worktree slug 中文处理

| 项 | 内容 |
|---|---|
| 来源 | `apps/hiveweave/lib/hiveweave/services/git_worktree.ex` |
| 现象 | `slugify` 保留中文（`一-鿿` 范围），40 字符上限 |
| 根因 | 设计决策，非 bug |
| Python 正确做法 | 对齐此行为，中文 slug 保留，但用 Python 正则实现。需测试中文+英文混合 slug 的分支名合法性 |

### E3: game time 停滞阈值与 TS 不一致

| 项 | 内容 |
|---|---|
| 来源 | Elixir `game_time/server.ex`（5/10 分钟）vs TS `game-time-scheduler.ts`（15 分钟） |
| 现象 | 两个实现的停滞检测阈值不同 |
| 根因 | 两套实现独立开发，未对齐 |
| Python 正确做法 | 需用户确认取哪个值（见 `constants.md` 的 ⚠️ 标记） |

### E4: 空 recipients 可能崩溃（待验证）

| 项 | 内容 |
|---|---|
| 来源 | `tool_executor.ex` `send_message` dispatch（待验证） |
| 现象 | 疑似 `recipients=[]` 时未做防御性检查 |
| 根因 | 待验证，可能是 BEAM 的 defensive programming 习惯不足 |
| Python 正确做法 | Pydantic 校验 recipients 非空，空列表返回明确错误提示 |

## TS 后端已知问题

### T1: trigger_integration 是 placeholder

| 项 | 内容 |
|---|---|
| 来源 | `packages/core/src/tool-executor.ts:334` |
| 现象 | `trigger_integration` 仅写日志，未真正跑集成测试 |
| 根因 | 未实现 |
| Python 正确做法 | 实现真正的集成测试触发逻辑，或明确标注为未实现 |

### T2: 无原生并发隔离

| 项 | 内容 |
|---|---|
| 来源 | TS 整体架构 |
| 现象 | 单进程 event loop，agent 状态在内存 Map 中，无进程级隔离。`runningAutoTriggers` Set 防重入是手动管理 |
| 根因 | Node.js 单进程模型限制 |
| Python 正确做法 | asyncio task 隔离 + 状态持久化（LangGraph 检查点），CPU 密集任务丢 ProcessPool |

### T3: 无原生容错

| 项 | 内容 |
|---|---|
| 来源 | TS 整体架构 |
| 现象 | 无 supervisor，无自动重启。进程崩溃=全部 agent 状态丢失 |
| 根因 | Node.js 无 OTP 等价物 |
| Python 正确做法 | 自建 task 重启逻辑 + LangGraph 检查点恢复 |

### T4: 无跨进程广播

| 项 | 内容 |
|---|---|
| 来源 | `statusEventBus` |
| 现象 | 进程内 EventEmitter，多实例部署需 Redis |
| 根因 | Node.js 无原生 PubSub |
| Python 正确做法 | 单实例用进程内广播（100 并发够用），多实例预留 Redis PubSub 接口 |

## 两个实现共同的问题

### C1: tail_turns / DEFAULT_TAIL_TURNS 不一致

| 项 | 内容 |
|---|---|
| 来源 | Elixir `conversation_store.ex`（4）vs TS `token-utils.ts`（2） |
| 现象 | compaction 保留的完整 turn 数不同 |
| 根因 | 两套实现独立开发 |
| Python 正确做法 | 需用户确认取哪个值 |

### C2: 端口不一致

| 项 | 内容 |
|---|---|
| 来源 | Elixir 4000 vs TS 3200 |
| 现象 | 两个后端用不同端口 |
| 根因 | 历史原因 |
| Python 正确做法 | 需用户确认用哪个端口 |

### C3: per-project DB 连接数不一致

| 项 | 内容 |
|---|---|
| 来源 | Elixir pool_size=5 vs TS 单连接 |
| 现象 | Elixir 用 DBConnection 池，TS 用 better-sqlite3 单连接 |
| 根因 | 语言生态差异 |
| Python 正确做法 | 需用户确认用 aiosqlite 单连接还是 async pool |

## 待发现的问题

> Phase 0 功能契约盘点过程中发现的新问题追加在这里。格式同上。
