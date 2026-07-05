# 功能契约 12：实时通信

> **本文件是功能契约（层 1），用 spec 语言描述模块的行为契约。**

## 元信息

| 项 | 值 |
|---|---|
| 模块编号 | 12 |
| 模块名称 | 实时通信 |
| Elixir 源码 | `hiveweave_web/channels/lobby_channel.ex` + `agent_channel.ex` + `project_channel.ex` + `user_socket.ex` |
| TS 参考源码 | `apps/server/src/routes/chat.ts`（SSE） + `packages/core/src/status-event-bus.ts` |
| OpenCode 参考源码 | 无（CLI 工具无实时通信） |
| 状态 | 草稿 |

## 功能概述

基于 WebSocket 的双向实时通信。三个频道：`lobby:status`（全局状态 + 活动流）、`agent:<id>`（单 Agent 聊天 + 流式 token）、`project:<id>`（项目级事件）。Streamer 通过 PubSub 发布事件，Channel 订阅后推送客户端。TS 遗留后端用 SSE 单向流，Python 迁移采用 WebSocket（对齐 Elixir）。

## 接口契约

### 频道设计

| 频道 | 事件 | 方向 | Payload | 说明 |
|---|---|---|---|---|
| `lobby:status` | join | ← client | — | 返回 `{agentIds, paused}` |
| | init | → client | `{agentIds, paused}` | 初始快照（join 后异步 push） |
| | status_change | → client | `{agentId, processing}` | agent 状态变更 |
| | activity | → client | ActivityEvent | 工作活动流 |
| | org_changed | → client | `{}` | 组织结构变更 |
| | ping | ← client | — | 心跳，回复 `{pong: <timestamp>}` |
| `agent:<id>` | join | ← client | — | 返回 `{agentId, name, role, history, inbox}` |
| | chat | ← client | `{message, images?}` | 发送消息；agent busy 时回 error |
| | cancel | ← client | — | 取消当前处理 |
| | ping | ← client | — | 心跳，回复 `{pong: <timestamp>}` |
| | stream_chunk | → client | `{text, delta?, reasoning?, deltaId?, seq?}` | 流式 token |
| | stream_tool | → client | `{type, name, input/output, id}` | 工具事件 |
| | done | → client | `{}` | 流式结束 |
| | error | → client | `{message}` | 错误（含 busy：`{message: "Agent is busy"}`） |
| | message_id | → client | `{role, id}` | 消息 ID |
| | status_change | → client | `{agentId, processing}` | agent 状态变更（本 agent） |
| `project:<id>` | join | ← client | `{projectId}` | — |
| | game_time | → client | `{gameSeconds, realTimestamp}` | 游戏时间更新 |
| | agent_hired | → client | `data` | 新 agent |
| | dispatch | → client | `data` | 任务派发 |
| | status_change | → client | `{agentId, processing}` | agent 状态变更 |

> **RECONCILE — ping 事件（部分噪声）**：审查员称"三个 channel 都实现了 ping"，实际源码
> `lobby_channel.ex:38` 和 `agent_channel.ex:107` 实现了 `handle_in("ping", ...)`，但
> **`project_channel.ex` 未实现 ping**（无 handle_in）。已为 lobby/agent 补充 ping 事件，
> project 频道不列。

> **RECONCILE — agent/project 频道 status_change（有效可操作）**：源码三个频道均实现
> `handle_info({:status_change, agent_id, status}, socket)` 推送 `status_change`。契约原仅在
> lobby 频道列出，已为 agent/project 频道补充。

### ActivityEvent 结构

```
{
  agentId: string
  agentName: string
  type: "thinking" | "text" | "tool_use" | "tool_result" | "done" | "error" | "text_delta" | "thinking_delta"
  content?: string
  deltaId?: string
  toolName?: string
  toolInput?: string
  toolResult?: string
  errorMessage?: string
  timestamp: number
}
```

### StatusEventBus 接口

| 函数 | 参数 | 返回值 | 说明 |
|---|---|---|---|
| `set_processing` | `(agent_id, value)` | — | 设置 agent 处理状态 |
| `is_processing` | `(agent_id)` | `bool` | 查询是否处理中 |
| `is_paused` | — | `bool` | 系统是否暂停 |
| `pause` / `resume` | — | — | 暂停/恢复所有 agent |
| `get_all_processing` | — | `[agent_id]` | 所有处理中的 agent |
| `subscribe` | `(listener)` | unsubscribe fn | 订阅状态变更 |
| `emit_activity` | `(event)` | — | 发布活动事件 |
| `get_recent_activity` | — | `[ActivityEvent]` | 最近 100 条活动 |
| `subscribe_activity` | `(listener)` | unsubscribe fn | 订阅活动（含 replay） |

## 核心流程

### Agent chat 流程

```
1. 客户端 join "agent:<id>"：
   a. Org.get_agent(agent_id) 取 agent，不存在 → {:error, "agent_not_found"}
   b. ensure_project_booted(agent.project_id)：若项目未运行则 ProjectSupervisor.start_project
      （会 spawn 该项目所有 agent 的 GenServer）—— 用于后端重启后的崩溃恢复
   c. 订阅 PubSub "agent:<id>" + "project:<projectId>"（仅 join 时一次，幂等）
   d. 返回 {agentId, name, role, history(50), inbox}
2. 客户端发 "chat" {message}：
   a. save_message(user) → push "message_id" {role:"user", id}
   b. find_agent_pid：未运行 → push "error" {message:"Agent not running"}
   c. Agent.chat(pid, message)：
      - :ok → 进入流式
      - {:error, :busy} → push "error" {message:"Agent is busy"}（不进入流式）
3. Agent 产出流式事件 → PubSub broadcast 到 "agent:<id>" topic
4. AgentChannel 收到 → push "stream_chunk" / "stream_tool" 到客户端
5. tool_use / tool_result / done 类型额外 broadcast 到 "lobby:status"
6. text_delta / thinking_delta 不转发到 lobby（避免重复渲染）
7. 流式结束 → push "done"
```

> **RECONCILE — busy 状态机（有效可操作）**：源码 `AgentChannel.handle_in("chat", ...)`
> 对 `Agent.chat/3` 返回 `{:error, :busy}` 时 push `"error"` 事件 `{message: "Agent is busy"}`，
> **不进入流式**，客户端可稍后重试。契约原仅在验收标准提"agent busy 时返回 error"，未在事件表/
> 流程中描述，已补充。

> **RECONCILE — join 自动启动（契约误读）**：审查员建议"明确 join 不自动启动 agent"。但源码
> `AgentChannel.join/3` **确实调用** `ensure_project_booted/1`，后者在项目未运行时调
> `ProjectSupervisor.start_project`，而 `start_project_children` 会 `spawn_agents` 为该项目
> **所有**已持久化 agent 启动 GenServer。这是**故意的崩溃恢复机制**（后端重启后前端打开 agent
> 面板即拉起项目）。契约应如实记录此行为，而非声明"不自动启动"。Python 迁移保留此行为：join
> 时按需拉起项目+agent。安全上无风险（agent 不会自动开始干活，仅 GenServer 就绪等待 chat）。

### PubSub 事件转发规则

| 事件类型 | agent:<id> | lobby:status | 说明 |
|---|---|---|---|
| text_delta | ✅ | ❌ | 仅 agent 频道 |
| thinking_delta | ✅ | ❌ | 仅 agent 频道 |
| tool_use | ✅ | ✅ | 双频道 |
| tool_result | ✅ | ✅ | 双频道 |
| done | ✅ | ✅ | 双频道 |
| error | ✅ | ✅ | 双频道 |

### Delta seq 去重

```
1. 每个 text_delta / thinking_delta 分配单调递增 seq
2. 前端用 lowest-seq 去重
3. 避免流式重连时重复渲染
```

> **RECONCILE — delta seq 重连/重启未定义（有效可操作）**：源码 `streamer.ex` 的 seq 计数器存于
> **agent GenServer 的 process dictionary**（`Process.get(:hw_seq_counter, 0)`，每次 `+1`）。
> 生命周期如下：
> - **客户端 WebSocket 重连**：seq **不重置**。服务端 agent GenServer 进程未变，计数器继续递增；
>   客户端重新 join 后收到的 delta seq 接续之前的序号。客户端用 `deltaId` + 最低 seq 去重。
> - **agent GenServer 重启**（崩溃恢复 / 项目重 boot）：seq **从 0 重置**，因为 process dictionary
>   随进程消亡。新的流式会话从 0 开始，客户端按新会话处理。
> - seq 仅用于"同一次流式会话内"的增量排序去重，**不是全局持久序号**，前端不可跨会话比较。
>
> Python 迁移建议：用 `asyncio` 协程内局部变量 `seq = 0; seq += 1` 维护，语义与 process dictionary
> 一致（协程重启即重置）。如需跨重启稳定可改为从 DB 读取最后 seq，但源码未这么做，保持一致即可。

## 常量引用

| 常量 | 值 | 所在章节 |
|---|---|---|
| recentActivity 缓冲区 | `100` 条 | 实时通信 |
| join 历史消息数 | `50` 条 | — |
| check_origin | localhost:5173, 4000, 3200 | — |
| 认证 | `HIVEWEAVE_API_KEY`（条件强制：环境变量未设 → 开放；已设 → 强制校验） | — |

## 已知问题

| 问题编号 | 说明 | Python 迁移处理 |
|---|---|---|
| T4 | TS 无跨进程广播，用 SSE 单向流 | Python 用 WebSocket（对齐 Elixir） |
| — | PubSub 订阅必须在 join 时一次，不能在 chat 中重复 | 确保订阅幂等 |
| — | delta 不转发到 lobby | 保留此规则 |
| — | delta seq 去重：seq 存 agent 进程字典，重连不重置，agent 重启从 0 重置 | 用协程局部变量维护，语义一致 |
| — | LobbyChannel join 后异步 push init | 用 WebSocket 的 send_json 实现 |
| — | AgentChannel join 时 ensure_project_booted（自动拉起项目+所有 agent GenServer） | 保留：崩溃恢复机制，agent 不会自动干活 |
| — | 认证条件强制：HIVEWEAVE_API_KEY 未设→开放，已设→强制 | Python 用 `secrets.compare_digest` 校验，env 未设则跳过 |
| — | lobby/agent 频道有 ping，project 频道无 ping | 保留：project 无 ping |
| — | agent/project 频道也推送 status_change（不止 lobby） | 保留：三频道均推 status_change |

## 验收标准

- [ ] WebSocket 连接，三频道（lobby/agent/project）
- [ ] join lobby:status 返回 agentIds + paused
- [ ] join agent:<id> 返回 agent 信息 + 最近 50 条历史
- [ ] join agent:<id> 时按需 ensure_project_booted（拉起项目）
- [ ] chat 事件触发 Agent.chat
- [ ] agent busy 时 push error {message:"Agent is busy"}，不进入流式
- [ ] agent 未运行时 push error {message:"Agent not running"}
- [ ] cancel 事件触发 Agent.cancel
- [ ] 流式 token 通过 stream_chunk 推送
- [ ] tool_use/tool_result 通过 stream_tool 推送
- [ ] text_delta/thinking_delta 不转发到 lobby
- [ ] tool_use/tool_result/done 转发到 lobby
- [ ] delta seq 单调递增（重连不重置，agent 重启从 0 重置）
- [ ] lobby/agent 频道响应 ping（project 频道无 ping）
- [ ] agent/project 频道推送 status_change
- [ ] 认证：HIVEWEAVE_API_KEY 已设则强制，未设则开放
- [ ] recentActivity 缓冲 100 条
- [ ] 系统暂停/恢复功能
- [ ] PubSub 订阅在 join 时一次（幂等）

## Python 实现建议

- FastAPI WebSocket：`@app.websocket("/socket")`
- 频道管理：`dict[channel_name, set[WebSocket]]`
- PubSub：内存 `asyncio.Queue` 或 `blinker` 库
- StatusEventBus：`class StatusEventBus` + `set[agent_id]` + `list[ActivityEvent]`
- 100 并发下用 in-process 广播，Redis PubSub 接口预留
- 认证（条件强制）：env `HIVEWEAVE_API_KEY` 未设→跳过校验（开放）；已设→用 `secrets.compare_digest` 校验 `api_key`/`apiKey` 参数（防时序攻击）
- delta seq：协程内 `seq = 0; seq += 1`，语义对齐 Elixir process dictionary（重连不重置，协程重启从 0 重置）
- busy 处理：agent chat 返回 busy → push error `{message:"Agent is busy"}`，不进入流式
- join 时按需 ensure_project_booted（拉起项目+agent GenServer，对齐崩溃恢复）
- ping：lobby/agent 频道响应 `{pong: <timestamp>}`（project 频道无 ping）
- status_change：三频道均推送
