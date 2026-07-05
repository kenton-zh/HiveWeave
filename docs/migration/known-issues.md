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

## 架构审查发现的问题（RECONCILE 后确认）

> 以下问题在 Phase 0 架构审查中发现，经 RECONCILE（对照源码验证）后确认。按严重度排序。

### A1: compaction summary 被 clean_messages 删除（数据丢失）🔴

| 项 | 内容 |
|---|---|
| 来源 | `conversation_store.ex:221-225`（摘要存为 role:system）+ `conversation_store.ex:454`（clean_messages 过滤 system） |
| 现象 | LLM 压缩摘要存为 `role: "system"` 消息，下次 `get_history` 时 `clean_messages` 会过滤所有 system 消息，导致摘要丢失 |
| 根因 | clean_messages 设计目的是过滤 identity/context prompt（也是 system role），但误杀了压缩摘要 |
| Python 正确做法 | 用独立 `compacted_prefix_cache` 字段存储摘要，不混入 history 消息列表；或用特殊 role 标记（如 `system_summary`）区分 |

### A2: 默认 permission mode 安全漏洞 🔴

| 项 | 内容 |
|---|---|
| 来源 | `permission.ex:165`（默认 "executor"）+ `permission.ex:59`（`_ -> :allow`） |
| 现象 | `get_permission_mode` 默认返回 `"executor"`，不匹配任何 mode 分支，落到 `_ -> :allow`，即未知 mode 默认允许所有操作 |
| 根因 | fallback 逻辑用 :allow 而非 :ask/:deny |
| Python 正确做法 | 默认 mode 改为 `:ask` 或 `:deny`；验收标准必须包含"未知 mode 不返回 :allow" |

### A3: run_command 绕过自毁命令检查 🔴

| 项 | 内容 |
|---|---|
| 来源 | `tool_executor.ex` — `run_command_tool()` 在 `core_tools` 中（所有角色可用），dispatch 注释"deliberately NO self-destructive check" |
| 现象 | `bash` 工具有自毁命令检查（rm -rf /, format, shutdown 等），但 `run_command` 工具故意不做此检查，对所有角色开放 |
| 根因 | 设计决策（run_command 用于非 bash 场景），但形成了安全绕过路径 |
| Python 正确做法 | 对所有命令执行类工具统一做自毁检查，或显式标注 run_command 为可信场景 |

### A4: agent 解散后闹钟未清理 🟡

| 项 | 内容 |
|---|---|
| 来源 | `services/org.ex` dismiss_agent + `game_time/server.ex` scheduled_alarms |
| 现象 | agent 被 dismiss 后，其 `to_agent_id` 的 scheduled_alarms 不被清理，闹钟可能触发到不存在的 agent |
| 根因 | dismiss 流程未考虑闹钟清理 |
| Python 正确做法 | dismiss agent 时同步 cancel 其所有 pending 闹钟 |

### A5: 停滞检测 cooldown 重启丢失 🟡

| 项 | 内容 |
|---|---|
| 来源 | `game_time/server.ex` — cooldown 用 Process.get/put（纯内存） |
| 现象 | 停滞检测的 cooldown 状态存储在进程字典中，服务器重启后丢失，可能导致重启后立即重复升级 |
| 根因 | 进程字典是纯内存状态 |
| Python 正确做法 | cooldown 状态持久化到 DB 或文件 |

### A6: cancel 时不清理工具任务和 streaming 标志 🟡

| 项 | 内容 |
|---|---|
| 来源 | `agent.ex:461-476` cancel 实现 |
| 现象 | cancel 有三个问题：(1) 工具任务 `async_nolink` 不被终止，继续后台运行；(2) 未调用 `update_streaming_messages_done`（对比 safety_timeout/force_reset 都调用了）；(3) 未标记 pending inbox 已读 |
| 根因 | cancel 实现不完整 |
| Python 正确做法 | cancel 时：(1) 终止工具 task；(2) 清理 zombie streaming 标志；(3) 标记 pending inbox 已读 |

### A7: handoff 无 cancel/timeout 转换 🟡

| 项 | 内容 |
|---|---|
| 来源 | `services/handoff.ex` |
| 现象 | handoff 状态机只能沿 pending→accepted→completed→approved 流转或 reopen 回退，无 cancel 或 timeout 自动转换 |
| 根因 | 设计决策（假设 agent 不会永久不处理） |
| Python 正确做法 | 如需要，新增 pending 超时自动 cancel 转换；如不需要，标注为已知限制 |

### A8: 三层记忆无访问控制 🟢

| 项 | 内容 |
|---|---|
| 来源 | `services/memory.ex` get_agent_memories 不校验调用者身份 |
| 现象 | 任何 agent 理论上可读他人 private 记忆（安全性由调用方 Streamer 只传自身 id 保证） |
| 根因 | 信任调用方，未做服务层校验 |
| Python 正确做法 | 可加 caller_id 校验加固，但非必须（调用方受控） |

### A9: rollback 前不自动 checkpoint 🟢

| 项 | 内容 |
|---|---|
| 来源 | `services/git_worktree.ex` rollback 操作 |
| 现象 | rollback（git reset --hard）前不自动 checkpoint，可能丢失未提交的工作 |
| 根因 | 设计决策（调用方负责） |
| Python 正确做法 | 安全建议：rollback 前自动 checkpoint |

### A10: async 写入 conversation_turns 可能丢数据 🟢

| 项 | 内容 |
|---|---|
| 来源 | `conversation_store.ex:124-126` Task.start fire-and-forget |
| 现象 | persist_turn 用 Task.start 异步写入，失败仅 log warning，崩溃时最近 1 轮数据丢失 |
| 根因 | 性能优先（不阻塞 LLM 流） |
| Python 正确做法 | 接受异步写入（性能优先），Python 用 `asyncio.create_task` + 可选 WAL buffer |

### A11: 100ms trigger 延迟 workaround 🟢

| 项 | 内容 |
|---|---|
| 来源 | `agent.ex:179` Process.sleep(100) |
| 现象 | do_trigger 中 100ms 延迟是 workaround，等 DB 写入落盘 |
| 根因 | trigger 在 DB 写后立即 spawn，SQLite 需要时间落盘 |
| Python 正确做法 | 接受此 workaround，用 `asyncio.sleep(0.1)`；或用 DB 事务回调替代 |
