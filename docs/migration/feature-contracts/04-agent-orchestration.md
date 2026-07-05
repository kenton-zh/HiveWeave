# 功能契约 04：多 Agent 编排

> **本文件是功能契约（层 1），用 spec 语言描述模块的行为契约，不引用实现代码。**

## 元信息

| 项 | 值 |
|---|---|
| 模块编号 | 04 |
| 模块名称 | 多 Agent 编排 |
| Elixir 源码 | `apps/hiveweave/lib/hiveweave/agents/agent.ex` + `agents/agent_supervisor.ex` + `services/org.ex` + `services/dispatch.ex` + `project_supervisor.ex` |
| TS 参考源码 | `packages/core/src/org-service.ts` + `packages/core/src/dispatch-service.ts` |
| OpenCode 参考源码 | 无（OpenCode 是单 agent CLI 工具，无多 agent 编排） |
| 状态 | 草稿 |

## 功能概述

每个 agent 是一个独立的长生命周期进程（Elixir GenServer → Python asyncio task），维护自己的状态机（idle/processing）。组织结构是一棵树：CEO 为根，HR 和其他 coordinator 为中间节点，executor 为叶子。Agent 之间通过 inbox 消息和 handoff 机制协作。Coordinator 可以向下属 dispatch_task、review/approve/reject 工作；Executor 执行任务后 report_completion。Agent 崩溃时自动重启，空响应时退避重试并最终升级到上级。

## 接口契约

### 输入（Consumes）

| 输入 | 来源 | 格式 | 说明 |
|---|---|---|---|
| chat message | 用户 WebSocket / trigger | string + opts | 用户直接消息或系统触发 |
| trigger 信号 | dispatch_task / handoff / inbox | `{agent_id, trigger_type}` | 触发 agent 处理待处理内容 |
| cancel 信号 | 用户 | — | 取消当前处理 |
| agent config | DB agents 表 | `{id, project_id, name, role, permission_type, model_id, ...}` | agent 元信息 |

### 输出（Produces）

| 输出 | 目标 | 格式 | 说明 |
|---|---|---|---|
| status 广播 | PubSub lobby:status | `{type:"processing"/"idle", agentId, ...}` | 前端 Live Activity |
| stream events | PubSub agent:<id> | `{type:"start"/"text_delta"/.../"done"}` | 流式对话事件 |
| LLM 调用 | Streamer | — | 调用 `Streamer.stream(state, message, opts, parent)` |
| inbox 消息 | 其他 agent | InboxService | 跨 agent 通信 |
| handoff | HandoffService | — | 任务交接 |

### 副作用

| 副作用 | 触发条件 | 目标 | 说明 |
|---|---|---|---|
| 状态变更 | chat / LLM 完成 | agent 进程状态 | idle → processing → idle |
| 广播 status | 状态变更时 | PubSub | 前端实时更新 |
| 创建 LLM task | chat 调用时 | Task.Supervisor | 异步执行 Streamer |
| 安全超时定时器 | chat 调用时 | agent 进程 | 10 分钟后强制回 idle |
| 空响应重试 | LLM 返回 :empty | agent 进程 | 5s/15s/45s 退避，最多 3 次 |
| 升级通知 | 空响应重试耗尽 | InboxService | 通知上级 agent |
| inbox 标记已读 | LLM 产出非空输出 | DB inbox | 空响应时保持未读以便重试 |

## 核心流程

### Agent 生命周期

```
1. 项目启动时 ProjectSupervisor 为每个已持久化的 agent 启动 GenServer
2. agent init：
   a. 从 DB 加载 agent 配置（id, project_id, name, role, permission_type, model_id）
   b. 查询项目语言（zh/en）缓存到 state
   c. 异步预热对话历史缓存
   d. 设置 last_heartbeat
3. agent 运行中等待消息
4. agent 崩溃 → DynamicSupervisor 自动重启（max_restarts=5, max_seconds=60）
5. agent 被 dismiss → stop_agent 终止进程
```

### Chat 流程

```
1. chat(agent_pid, message, opts):
   a. 如果 status == :processing → 返回 {:error, :busy}
   b. 如果系统暂停 → 返回 {:error, :paused}
   c. 取消之前的安全定时器
   d. 异步启动 LLM task：Streamer.stream(state, message, opts, parent)
   e. 设置 10 分钟安全超时定时器
   f. 状态 → :processing
   g. 广播 processing status
   h. 返回 :ok（不阻塞等待 LLM 完成）

2. LLM task 完成后（handle_info {ref, result}）：
   a. 取消安全定时器
   b. 处理结果：
      - {:ok, text, tool_history, thinking} → 正常完成
      - {:empty, tool_history, thinking} → 空响应重试
      - {:error, reason} → 错误处理
   c. 状态 → :idle
   d. 广播 idle status
   e. 自检：如有未读 inbox 消息，自动 re-trigger
```

### Trigger 流程

```
1. trigger_subordinate(agent_id) — 触发下属 executor
2. trigger_coordinator(agent_id) — 触发 coordinator（仅当有未读消息）

3. do_trigger(agent_id, trigger_type):
   a. 延迟 100ms（等 DB 写入落盘）
   b. 从 DB 获取 agent
   c. 如果 agent 已 archived/dismissed → 跳过
   d. coordinator：检查是否有 pending inbox 消息，无则跳过
   e. 检查 agent 是否正在 processing → 跳过（会在完成后自检 re-trigger）
   f. accept_pending_handoffs
   g. build_trigger_context（构建上下文）：
      - Pending Tasks block（handoffs）
      - Rework block（被拒绝的工作）
      - Messages block（inbox 消息）
      - Subordinate Logs block（coordinator 专属）
      - Report Required block（coordinator 专属，unreported handoffs）
   h. 保存为 background user 消息
   i. 调用 GenServer.call({:chat, context, [trigger: true, ...]})
   j. inbox 消息在 LLM 产出非空输出后才标记已读
```

### 空响应重试

```
1. LLM 返回 {:empty, ...}：
   a. retry_count + 1
   b. 如果 retry_count > 3：
      - 升级到上级：InboxService.send_message(alarm, "连续3次空响应")
      - 保存错误消息到 chat
      - 标记 pending inbox 消息为已读（避免无限循环）
      - 状态 → idle，重置 retry_count
      - 不自检 re-trigger（避免循环）
   c. 否则：
      - 按 [5s, 15s, 45s] 退避
      - 保留 inbox 消息未读
      - 重新触发 chat
```

### 安全超时

```
1. 10 分钟安全超时（600_000 ms）：
   a. 强制终止 LLM task
   b. 状态 → idle
   c. 广播 idle status
   d. 日志记录
```

### Cancel 流程

```
1. cancel(agent_pid):
   a. 终止 LLM task
   b. 取消安全定时器
   c. 广播 done event（error: "cancelled"）
   d. 状态 → idle
   e. 广播 idle status
```

## 组织结构

### 角色类型

| 类型 | 角色 | 权限 | 说明 |
|---|---|---|---|
| Coordinator | CEO | coordinator | 组织根节点，可 spawn/dismiss agent、approve/reject 工作、管理 worktree |
| Coordinator | HR | coordinator | 人员管理（hire/transfer/dismiss），无 management 工具 |
| Coordinator | 架构师/经理 | coordinator | 中间层管理，有 management + worktree，无 hire |
| Executor | 通用执行者 | executor | 叶子节点，写代码/执行任务 |
| Executor | QA | executor + qa_review | 写测试代码 + 运行审查工具 |
| Executor | Test Engineer | executor（受限） | 只读文件 + bash 运行测试 |
| Executor | Auditor | executor（受限） | 只读文件 + bash + 审查工具 |

### 组织范式（6 种）

| 范式 | 结构 | 流程节点 |
|---|---|---|
| solo | 单 agent | 必须自审 |
| flat_squad | 扁平团队 | 交叉审查 |
| tech_lead | 技术负责人 | tech_lead 审查所有 |
| pm_architect | PM+架构师 | 双线汇报 |
| pod | 小队 | 队内自审 + 跨队审查 |
| pipeline | 流水线 | 阶段门禁 |

## 状态机

### Agent 主状态机

| 当前状态 | 触发条件 | 目标状态 | 动作 |
|---|---|---|---|
| idle | chat 调用 | processing | 启动 LLM task + 安全定时器，广播 processing |
| processing | LLM 完成（正常） | idle | 广播 idle，自检 re-trigger |
| processing | LLM 完成（空响应） | idle 或 processing | 退避重试或升级 |
| processing | LLM 完成（错误） | idle | 广播 idle |
| processing | 安全超时（10min） | idle | 终止 task，广播 idle |
| processing | cancel | idle | 终止 task，广播 done(cancelled) |
| processing | 崩溃 | idle（重启后） | DynamicSupervisor 重启 |
| idle | trigger | processing | 同 chat |

## 错误处理

| 错误场景 | 处理方式 | 重试策略 | 升级策略 |
|---|---|---|---|
| agent busy（正在 processing） | 返回 {:error, :busy} | trigger 跳过，LLM 完成后自检 | — |
| 系统暂停 | 返回 {:error, :paused} | — | — |
| 空响应 | 退避重试 | 3 次（5s/15s/45s） | 通知上级 |
| LLM task 崩溃 | 代理进程不崩溃（Task.Supervisor async_nolink） | — | 状态回 idle |
| agent 进程崩溃 | DynamicSupervisor 重启 | max_restarts=5/60s | 超限后停止重启 |
| GenServer.call 超时 | 5s 超时 | — | 日志记录，跳过 |
| trigger 找不到进程 | catch :exit, {:noproc, _} | — | 日志记录，跳过 |

## 常量引用

| 常量 | 值 | 所在章节 |
|---|---|---|
| 安全超时 | `600_000` ms（10 分钟） | LLM 调用超时 |
| 空响应重试次数 | `3` | — |
| 空响应退避 | `5_000 / 15_000 / 45_000` ms | — |
| chat call 超时 | `30_000` ms | — |
| get_state call 超时 | `5_000` ms | — |
| trigger 延迟 | `100` ms | — |
| DynamicSupervisor max_restarts | `5` | — |
| DynamicSupervisor max_seconds | `60` | — |
| 最大 tool 轮次（CEO） | `60` | — |
| 最大 tool 轮次（HR） | `40` | — |
| 最大 tool 轮次（coordinator/manager） | `50` | — |
| 最大 tool 轮次（executor） | `80` | — |
| 停滞检测间隔 | `60` 秒 | 游戏时间 |
| 停滞阈值（processing） | `5` 分钟 | 游戏时间 |
| 停滞阈值（idle） | `10` 分钟 | 游戏时间 |
| 停滞升级 cooldown | `10` 分钟 | 游戏时间 |

## 已知问题

| 问题编号 | 说明 | Python 迁移处理 |
|---|---|---|
| T2 | TS 无原生并发隔离 | asyncio task + LangGraph checkpoints |
| T3 | TS 无原生容错 | 自建 task 重启逻辑 |
| E4 | 空收件人可能崩溃（待验证） | Pydantic 验证 |
| — | OpenCode 无多 agent 编排（单 agent CLI） | 本模块以 Elixir 为 P0 参考 |

## Python 实现建议

- **架构模式**：
  - `class Agent` 对应一个 asyncio task，不是 OS 进程
  - `class AgentManager` 管理所有 agent task（对应 AgentSupervisor + ProjectSupervisor）
  - agent 状态用 `enum` 而非原子
  - LLM 调用用 `asyncio.create_task()` 而非 `Task.Supervisor.async_nolink`
  - 安全超时用 `asyncio.call_later()` 或 `asyncio.wait_for()`
  - 崩溃重启：agent task 包在 `try/except` 中，except 时记录日志并可选重启

- **并发模型**：
  - 100 并发上限下，asyncio task 足够
  - 不需要进程隔离（不像 BEAM 的 process）
  - 用 `asyncio.Lock` 防止同一 agent 并发处理（对应 GenServer 的消息队列）

- **Trigger 机制**：
  - `asyncio.create_task(trigger_subordinate(agent_id))`
  - coordinator trigger 前检查 inbox 是否有未读消息

- **空响应重试**：
  - `asyncio.sleep(delay)` 实现退避
  - 升级消息通过 InboxService 发送

- **组织树**：
  - DB 存储父子关系（parent_id 字段）
  - `OrgService.get_children(project_id, agent_id)` 查直接下属
  - `OrgService.list_agents(project_id)` 查所有 agent

## 验收标准

- [ ] 每个 agent 是独立的 asyncio task，状态为 idle/processing
- [ ] chat 调用时如果正在 processing，返回 {:error, :busy}
- [ ] chat 调用后状态变为 processing，广播 status
- [ ] LLM task 完成后状态变为 idle，广播 status
- [ ] 10 分钟安全超时后强制回 idle
- [ ] cancel 终止 LLM task 并回 idle
- [ ] 空响应按 5s/15s/45s 退避重试，最多 3 次
- [ ] 空响应重试耗尽后升级到上级 agent
- [ ] trigger_subordinate 触发下属处理待处理内容
- [ ] trigger_coordinator 仅当有未读 inbox 消息时触发
- [ ] agent 正在 processing 时 trigger 跳过（等完成后自检）
- [ ] LLM 产出非空输出后才标记 inbox 消息为已读
- [ ] trigger context 包含 Pending Tasks / Rework / Messages / Subordinate Logs / Report Required blocks
- [ ] agent 崩溃后自动重启（max_restarts=5/60s）
- [ ] 系统暂停时返回 {:error, :paused}
- [ ] 项目启动时为所有持久化的 agent 启动 task
- [ ] agent 被 dismiss 后 task 终止

## 并行对比测试方案

| 测试场景 | Elixir 行为 | Python 预期行为 | 验证方法 |
|---|---|---|---|
| 正常 chat | idle → processing → idle | 相同 | 发送消息，对比状态转换和广播事件 |
| busy 时 chat | 返回 {:error, :busy} | 相同 | 在 processing 时再发 chat |
| 安全超时 | 10min 后回 idle | 相同 | mock LLM 不返回，对比超时行为 |
| cancel | 终止 task，回 idle | 相同 | processing 时 cancel |
| 空响应重试 | 5s/15s/45s 退避 | 相同 | mock LLM 返回空，对比重试间隔 |
| 空响应升级 | 3 次后通知上级 | 相同 | mock 连续空响应，对比升级消息 |
| trigger_subordinate | 触发下属处理 | 相同 | dispatch_task 后对比 trigger 行为 |
| trigger_coordinator 无消息 | 跳过 | 相同 | 无未读消息时 trigger |
| trigger 时 busy | 跳过，等自检 | 相同 | processing 时 trigger |
| inbox 标记已读 | 非空输出后才标记 | 相同 | 空响应后检查 inbox 状态 |
| 崩溃重启 | 自动重启 | 相同 | kill agent task，对比重启 |
| 项目启动 | 为所有 agent 启动 task | 相同 | 重启服务，对比 agent task 数量 |

---

> **填写规则**：
> 1. 用 spec 语言，不用代码语言。说"做什么"，不说"怎么做"。
> 2. 所有常量值引用 `constants.md`，不在此处重复定义。
> 3. 所有已知问题引用 `known-issues.md`，不在此处重复描述。
> 4. 每个契约必须经过用户确认后才能标记为"已确认"。
> 5. "Python 实现建议"是建议不是约束，实现者可以根据情况调整，但必须满足验收标准。
