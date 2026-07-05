# 功能契约 16：可观测性（EventAudit + Telemetry）

> **本文件是功能契约（层 1），用 spec 语言描述模块的行为契约，不引用实现代码。**
> **任何 AI 工具实现 Python 版本时，必须满足此契约中的所有要求。**

## 元信息

| 项 | 值 |
|---|---|
| 模块编号 | 16 |
| 模块名称 | 可观测性（EventAudit + Telemetry） |
| Elixir 源码 | `event_audit.ex` + `telemetry.ex` + `schema/agent_event.ex` |
| TS 参考源码 | `packages/core/src/telemetry.ts`（部分对应，TS 无独立 audit 表） |
| OpenCode 参考源码 | —（OpenCode 用日志，无遥测总线） |
| 状态 | 草稿 |

## 功能概述

两层可观测性：(1) **EventAudit**——轻量事件审计日志，将关键事件异步写入 per-project DB 的 `agent_events` 表，支持 timeline 查询；非完整 Event Sourcing，仅用于读/timeline。(2) **Telemetry**——基于 `:telemetry` 库的事件分发系统，统一 attach 一组 handler 处理 LLM streaming、Agent 生命周期、Circuit breaker 事件；其中 `agent.crash` 事件会自动写入 EventAudit。两者解耦：Telemetry 是进程内同步分发，EventAudit 是异步持久化。

## 接口契约

### 输入（Consumes）

| 输入 | 来源 | 格式 | 说明 |
|---|---|---|---|
| EventAudit.log 调用 | 业务代码/Telemetry crash handler | `(agent_id, event_type, payload{})` | payload 任意 map，JSON 编码后存 payload 列 |
| EventAudit.timeline 调用 | 调试/查询接口 | `(agent_id, since_ms?)` | since_ms 缺省为 1 小时前 |
| Telemetry 事件 emit | 业务代码（streamer/agent/circuit_breaker） | `(event_name, measurements{}, metadata{})` | 9 类事件，见事件清单 |
| 监督树启动 | Application | — | Telemetry supervisor init 时 attach handlers |

### 输出（Produces）

| 输出 | 目标 | 格式 | 说明 |
|---|---|---|---|
| timeline 返回 | 调用方 | `[{id, agent_id, event_type, payload, created_at}]` | 按 created_at DESC，LIMIT 100 |
| 日志输出 | Logger | `[Telemetry] <event> measurements=... metadata=...` | dispatch_handler 统一记录 |
| crash 审计写入 | per-project DB agent_events | event_type="crash" | payload 含 reason 字符串 |

### 副作用

| 副作用 | 触发条件 | 目标 | 说明 |
|---|---|---|---|
| INSERT agent_events | EventAudit.log 调用 | per-project DB | 异步 Task 执行，失败仅记 warning |
| Logger.info/debug/warning | 每个 Telemetry 事件 | 日志 | dispatch_handler 统一 info；LLM/agent 事件 debug；crash warning |
| EventAudit.log(:crash) | agent_crash 事件 | per-project DB | 自动联动，reason 转字符串 |

## 核心流程

### EventAudit.log（异步写入）

```
1. 生成 UUID + 当前毫秒时间戳
2. payload JSON 编码
3. event_type 转字符串
4. 启动 Task 异步执行：通过 ProjectFactory.query_for_agent 路由到对应 per-project DB
5. INSERT INTO agent_events (id, agent_id, event_type, payload, created_at)
6. Task 内异常捕获，仅记 warning，不影响调用方
7. log 调用本身立即返回 :ok（非阻塞）
```

### EventAudit.timeline

```
1. since_ms 缺省 = 当前时间 - 1 小时
2. SELECT id, agent_id, event_type, payload, created_at
   FROM agent_events WHERE agent_id = ? AND created_at > ?
   ORDER BY created_at DESC LIMIT 100
3. 行转 map（列名 zip 行值）
4. 异常返回空列表
```

### Telemetry 启动与 attach

```
1. Telemetry supervisor init 时调用 attach_handlers
2. attach_many("hiveweave-logger", 9 个事件名, dispatch_handler, nil)
3. dispatch_handler：统一 Logger.info 记录事件名 + measurements + metadata
4. supervisor 无子进程（预留 metrics reporter）
```

### crash 事件自动联动 EventAudit

```
1. 业务代码调用 Telemetry.agent_crash(agent_id, reason)
2. :telemetry.execute([:hiveweave, :agent, :crash], ..., {agent_id, reason})
3. dispatch_handler 收到事件 → Logger.warning 记录
4. （源码中 handle_agent_crash 单独 handler）→ 调用 EventAudit.log(id, :crash, {reason: inspect(reason)})
5. EventAudit 异步写入 per-project DB
```

### Telemetry 事件 emit

```
1. 业务代码调用对应 emit 函数（如 llm_stream_start(provider, model)）
2. 函数内部 :telemetry.execute(event_name, measurements, metadata)
3. 已 attach 的 dispatch_handler 被同步调用
4. handler 记录日志；crash 事件额外写 EventAudit
```

## 事件类型清单

| 事件名 | emit 函数 | measurements | metadata | 说明 |
|---|---|---|---|---|
| `[:hiveweave, :llm, :stream_start]` | llm_stream_start | `{system_time}` | `{provider, model}` | LLM 流开始 |
| `[:hiveweave, :llm, :stream_chunk]` | llm_stream_chunk | `{latency_ms}` | `{provider}` | 流 chunk 延迟 |
| `[:hiveweave, :llm, :stream_done]` | llm_stream_done | `{duration_ms}` | `{provider, model, status}` | 流完成 |
| `[:hiveweave, :llm, :stream_fail]` | llm_stream_fail | `{system_time}` | `{provider, reason}` | 流失败 |
| `[:hiveweave, :agent, :chat_start]` | agent_chat_start | `{system_time}` | `{agent_id, from}` | agent chat 开始 |
| `[:hiveweave, :agent, :chat_done]` | agent_chat_done | `{duration_ms}` | `{agent_id, tokens}` | agent chat 完成 |
| `[:hiveweave, :agent, :crash]` | agent_crash | `{system_time}` | `{agent_id, reason}` | agent 崩溃（自动写 EventAudit） |
| `[:hiveweave, :circuit, :open]` | circuit_open | `{system_time}` | `{provider}` | 熔断器打开 |
| `[:hiveweave, :circuit, :close]` | circuit_close | `{system_time}` | `{provider}` | 熔断器关闭 |

## 状态机（如适用）

| 当前状态 | 触发条件 | 目标状态 | 动作 |
|---|---|---|---|
| 未 attach | Telemetry supervisor init | 已 attach | attach_many 注册 9 个事件 handler |
| 已 attach | 业务代码 emit 事件 | 分发中 | 同步调用 dispatch_handler |
| 分发中 | crash 事件 | 联动写审计 | 调用 EventAudit.log(:crash) |

## 错误处理

| 错误场景 | 处理方式 | 重试策略 | 升级策略 |
|---|---|---|---|
| EventAudit INSERT 失败 | Task 内 try/rescue 记 warning | 不重试 | 不影响调用方（异步） |
| EventAudit.timeline 查询失败 | try/rescue 返回空列表 | 不重试 | 调用方得到 [] |
| per-project DB 不存在 | ProjectFactory 路由失败 | 不重试 | Task 内捕获记 warning |
| Telemetry handler 异常 | :telemetry 库默认行为 | — | 不影响 emit 调用方 |
| payload JSON 编码失败 | —（源码用 encode! 会抛异常） | Python 用默认 json.dumps + try | 调用方应保证 payload 可序列化 |

## 常量引用

| 常量 | 值 | 所在章节 |
|---|---|---|
| timeline 默认时间窗 | `1` 小时 | 本契约（EventAudit） |
| timeline LIMIT | `100` 行 | 本契约（EventAudit） |
| agent_events.payload 默认值 | `"{}"` | 本契约（schema 默认） |
| 熔断器三态 | `closed / open / half_open` | 重试与熔断 |

## 已知问题

| 问题编号 | 说明 | Python 迁移处理 |
|---|---|---|
| — | EventAudit.log 用 Task.start 异步写，Task 失败则事件丢失（fire-and-forget） | Python 用 asyncio.create_task fire-and-forget，或可选改用队列持久化 |
| — | telemetry.ex 中 handle_agent_crash 单独定义但 attach_many 只注册了 dispatch_handler，crash→EventAudit 联动在当前 attach 路径下未触发 | Python 需显式注册 crash handler 调用 EventAudit.log，确保联动生效 |
| — | agent_events 表由 ProjectFactory.init_project_tables 创建，EventAudit 不负责建表 | Python 保持：建表归 ProjectFactory，EventAudit 只读写 |
| — | timeline 返回的 map 用列名 zip 行值，payload 是 JSON 字符串未解码 | Python 可选择返回字符串或解码为 dict，需与前端约定 |
| — | Telemetry supervisor 无子进程，metrics reporter 是预留 | Python 可后续接入 OpenTelemetry exporter |

## Python 实现建议

- **框架/库**：
  - 日志：`structlog` 或 `loguru`（结构化日志，对应 Logger.info/debug/warning）
  - 遥测：`OpenTelemetry`（SDK + tracer/meter）或简化为自建事件总线
  - 审计：直接 `aiosqlite` 写 per-project DB
- **架构模式**：
  - `EventAudit` 单例，`log` 用 `asyncio.create_task` fire-and-forget
  - `Telemetry` 类暴露 9 个 emit 方法，内部调用已注册的 handler 列表
  - crash 联动：在 Telemetry 的 crash emit 内直接调用 `EventAudit.log`
  - handler 注册用观察者模式（list of callbacks）
- **注意事项**：
  - timeline 查询走 per-project DB（通过 agent_id 路由），不是 Meta DB
  - 异步写入失败不应阻塞业务调用方
  - payload 在 Python 侧建议解码为 dict 返回（比 Elixir 字符串更友好）
  - 多实例部署时事件分发用进程内即可，跨实例可预留 OpenTelemetry collector

## 验收标准

- [ ] EventAudit.log 异步写入 agent_events 表，调用方不阻塞
- [ ] EventAudit.log 写入失败仅记 warning，不影响调用方
- [ ] EventAudit.timeline 默认返回最近 1 小时事件
- [ ] EventAudit.timeline 按 created_at DESC 排序，LIMIT 100
- [ ] EventAudit.timeline 查询失败返回空列表
- [ ] Telemetry 启动时 attach 9 个事件 handler
- [ ] 9 类事件均有对应 emit 函数
- [ ] dispatch_handler 对每个事件记录日志
- [ ] agent.crash 事件自动联动 EventAudit.log(:crash)
- [ ] crash 的 payload 包含 reason 字段
- [ ] LLM 事件 metadata 含 provider/model
- [ ] circuit 事件 metadata 含 provider
- [ ] agent_events 表由 ProjectFactory 创建，EventAudit 不建表
- [ ] emit 调用是同步分发（不阻塞业务），EventAudit 写入是异步

## 并行对比测试方案

| 测试场景 | Elixir 行为 | Python 预期行为 | 验证方法 |
|---|---|---|---|
| EventAudit.log 后 timeline | 返回含该事件的列表 | 同 | 查 agent_events 表 |
| timeline 默认时间窗 | 仅返回最近 1 小时 | 同 | 插入 2 小时前事件，确认不在结果 |
| timeline LIMIT | 最多 100 行 | 同 | 插入 150 条，确认返回 100 |
| emit llm_stream_start | 日志记录事件 + provider/model | 同 | 查日志输出 |
| emit agent_crash | 日志 + EventAudit 写入 crash 行 | 同 | 查 agent_events event_type=crash |
| emit circuit_open | 日志记录 provider | 同 | 查日志 |
| EventAudit 写入失败 | 记 warning，调用方正常 | 同 | 模拟 DB 错误 |
| payload 序列化 | JSON 字符串 | 同（或 dict） | 查 payload 列 |

---

> **填写规则**：
> 1. 用 spec 语言，不用代码语言。说"做什么"，不说"怎么做"。
> 2. 所有常量值引用 `constants.md`，不在此处重复定义。
> 3. 所有已知问题引用 `known-issues.md`，不在此处重复描述。
> 4. 每个契约必须经过用户确认后才能标记为"已确认"。
> 5. "Python 实现建议"是建议不是约束，实现者可以根据情况调整，但必须满足验收标准。
