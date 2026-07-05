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
| | init | → client | `{agentIds, paused}` | 初始快照 |
| | status_change | → client | `{agentId, processing}` | agent 状态变更 |
| | activity | → client | ActivityEvent | 工作活动流 |
| | org_changed | → client | `{}` | 组织结构变更 |
| `agent:<id>` | join | ← client | — | 返回 `{agentId, name, role, history, inbox}` |
| | chat | ← client | `{message, images?}` | 发送消息 |
| | cancel | ← client | — | 取消当前处理 |
| | stream_chunk | → client | `{text, delta?, reasoning?, deltaId?, seq?}` | 流式 token |
| | stream_tool | → client | `{type, name, input/output, id}` | 工具事件 |
| | done | → client | `{}` | 流式结束 |
| | error | → client | `{message}` | 错误 |
| | message_id | → client | `{role, id}` | 消息 ID |
| `project:<id>` | join | ← client | `{projectId}` | — |
| | game_time | → client | `{gameSeconds, realTimestamp}` | 游戏时间更新 |
| | agent_hired | → client | `data` | 新 agent |
| | dispatch | → client | `data` | 任务派发 |

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
1. 客户端 join "agent:<id>" → 返回 agent 信息 + 最近 50 条历史
2. 客户端发 "chat" {message} → 调用 Agent.chat(pid, message)
3. Agent 产出流式事件 → PubSub broadcast 到 "agent:<id>" topic
4. AgentChannel 收到 → push "stream_chunk" / "stream_tool" 到客户端
5. tool_use / tool_result / done 类型额外 broadcast 到 "lobby:status"
6. text_delta / thinking_delta 不转发到 lobby（避免重复渲染）
7. 流式结束 → push "done"
```

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

## 常量引用

| 常量 | 值 | 所在章节 |
|---|---|---|
| recentActivity 缓冲区 | `100` 条 | 实时通信 |
| join 历史消息数 | `50` 条 | — |
| check_origin | localhost:5173, 4000, 3200 | — |
| 认证 | `HIVEWEAVE_API_KEY`（可选） | — |

## 已知问题

| 问题编号 | 说明 | Python 迁移处理 |
|---|---|---|
| T4 | TS 无跨进程广播，用 SSE 单向流 | Python 用 WebSocket（对齐 Elixir） |
| — | PubSub 订阅必须在 join 时一次，不能在 chat 中重复 | 确保订阅幂等 |
| — | delta 不转发到 lobby | 保留此规则 |
| — | delta seq 去重 | 保留 seq 机制 |
| — | LobbyChannel join 后异步 push init | 用 WebSocket 的 send_json 实现 |
| — | AgentChannel join 时自动启动未运行的项目 | 容错处理 |

## 验收标准

- [ ] WebSocket 连接，三频道（lobby/agent/project）
- [ ] join lobby:status 返回 agentIds + paused
- [ ] join agent:<id> 返回 agent 信息 + 最近 50 条历史
- [ ] chat 事件触发 Agent.chat
- [ ] cancel 事件触发 Agent.cancel
- [ ] 流式 token 通过 stream_chunk 推送
- [ ] tool_use/tool_result 通过 stream_tool 推送
- [ ] text_delta/thinking_delta 不转发到 lobby
- [ ] tool_use/tool_result/done 转发到 lobby
- [ ] delta seq 单调递增
- [ ] agent busy 时返回 error
- [ ] recentActivity 缓冲 100 条
- [ ] 系统暂停/恢复功能
- [ ] PubSub 订阅在 join 时一次（幂等）

## Python 实现建议

- FastAPI WebSocket：`@app.websocket("/socket")`
- 频道管理：`dict[channel_name, set[WebSocket]]`
- PubSub：内存 `asyncio.Queue` 或 `blinker` 库
- StatusEventBus：`class StatusEventBus` + `set[agent_id]` + `list[ActivityEvent]`
- 100 并发下用 in-process 广播，Redis PubSub 接口预留
- 认证：`HIVEWEAVE_API_KEY` header 校验（`secrets.compare_digest` 防时序攻击）
