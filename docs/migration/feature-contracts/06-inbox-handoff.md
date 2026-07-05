# 功能契约 06：收件箱与交接

> **本文件是功能契约（层 1），用 spec 语言描述模块的行为契约。**

## 元信息

| 项 | 值 |
|---|---|
| 模块编号 | 06 |
| 模块名称 | 收件箱与交接 |
| Elixir 源码 | `services/inbox.ex` + `services/handoff.ex` + `schema/inbox.ex` + `schema/handoff.ex` |
| TS 参考源码 | `packages/core/src/inbox-service.ts` + `packages/core/src/handoff-service.ts` |
| OpenCode 参考源码 | 无（OpenCode 是单 agent CLI） |
| 状态 | 草稿 |

## 功能概述

**收件箱**：Agent 间消息投递系统。支持三种消息类型（superior/peer/alarm）、三级优先级（low/normal/urgent）、expect_report 标记。未读消息在 recipient 下次 trigger 时注入 context。

**交接**：任务在 coordinator 与 subordinate 之间的生命周期管理。从派发到审批的全流程跟踪，支持 rework 重做。`context_delivered` 防重复注入，`expect_report + reported_up` 防漏报。

## 接口契约

### Inbox 接口

| 函数 | 参数 | 返回值 | 说明 |
|---|---|---|---|
| `send_message` | `(from, to, msg_type, content, opts)` | `{:ok, msg}` | 发送消息，广播 inbox_update |
| `get_pending_messages` | `(agent_id, opts?)` | `[msg]` | 未读消息，ASC 排序，limit 50 |
| `get_inbox` | `(agent_id, opts?)` | `[msg]` | 全部消息（含已读），DESC，limit 50 |
| `get_unread_count` | `(agent_id)` | `int` | 未读计数 |
| `mark_as_read` | `(agent_id, msg_id)` | `:ok` | 单条标记 |
| `mark_all_read` | `(agent_id, msg_type?)` | `:ok` | 按 type 批量标记 |
| `mark_read_by_ids` | `(agent_id, msg_ids)` | `:ok` | **按 ID 批量标记**，避免竞态 |

### Handoff 接口

| 函数 | 参数 | 返回值 | 说明 |
|---|---|---|---|
| `create_handoff` | `(project_id, from, to, summary, opts)` | `{:ok, id}` | 去重：相同 from→to+summary 的 active handoff 不新建 |
| `get_pending_handoffs` | `(project_id, to)` | `[handoff]` | status=pending 且 context_delivered=0 |
| `get_accepted_handoffs` | `(project_id, to)` | `[handoff]` | status=accepted 且 context_delivered=0 |
| `accept_pending_handoffs` | `(project_id, to)` | `count` | pending → accepted 批量 |
| `complete_handoff` | `(project_id, to, handoff_id?)` | `{:ok, %{completed: bool}}` | accepted → completed |
| `approve_handoff` | `(project_id, from, to)` | `{:ok, %{approved: bool}}` | completed → approved |
| `reopen_handoff` | `(project_id, from, to)` | `{:ok, %{reopened: bool}}` | completed → accepted，重置 context_delivered=0 |
| `mark_delivered` | `(project_id, handoff_ids)` | `:ok` | 批量标记已注入 context |
| `get_unreported_accepted_handoffs` | `(project_id, to)` | `[handoff]` | expect_report=1 且 reported_up=0 |
| `mark_reported_up` | `(project_id, to)` | `count` | 标记已向上汇报 |

## 数据模型

### inbox 表

```sql
CREATE TABLE inbox (
  id TEXT PRIMARY KEY,
  from_agent_id TEXT NOT NULL,
  to_agent_id TEXT NOT NULL,
  message TEXT,
  read INTEGER DEFAULT 0,
  created_at INTEGER,
  message_type TEXT,              -- superior | peer | alarm
  expect_report INTEGER DEFAULT 0,
  priority TEXT DEFAULT 'normal'  -- low | normal | urgent
);
```

### handoffs 表

```sql
CREATE TABLE handoffs (
  id TEXT PRIMARY KEY,
  from_agent_id TEXT,
  to_agent_id TEXT,
  summary TEXT,
  status TEXT,                          -- pending | accepted | completed | approved
  created_at INTEGER,
  module_id TEXT,
  expect_report INTEGER DEFAULT 0,
  reported_up INTEGER DEFAULT 0,
  updated_at INTEGER,
  context_delivered INTEGER DEFAULT 0   -- 防重复注入
);
```

## 状态机

### Handoff 状态机

```
pending ──accept──→ accepted ──complete──→ completed ──approve──→ approved (终态)
                                          completed ──reopen──→ accepted (rework，重置 context_delivered)
```

## 核心流程

### 消息投递

```
1. send_message(from, to, type, content, opts):
   a. 插入 DB inbox 表
   b. 广播 {:inbox_update, msg} 到 agent:<to> topic
   c. 返回 {:ok, msg}
```

### Trigger context 构建（涉及 inbox + handoff）

```
1. 获取 pending_handoffs + accepted_handoffs（context_delivered=0）
2. 获取 inbox 未读消息
3. 分离 rework 消息和其他消息
4. 构建 blocks：
   - Pending Tasks block（handoffs）
   - Rework block（被拒绝的工作）
   - Messages block（inbox 消息）
   - Subordinate Logs block（coordinator 专属）
   - Report Required block（unreported handoffs）
5. mark_delivered(handoff_ids)
6. inbox 消息在 LLM 产出非空输出后才 mark_read_by_ids
```

## 已知问题

| 问题编号 | 说明 | Python 迁移处理 |
|---|---|---|
| — | Elixir inbox ASC 排序，TS DESC | 用 ASC（FIFO 语义，旧消息优先处理） |
| — | Elixir 有 mark_read_by_ids，TS 无 | 必须保留，避免竞态 |
| — | Elixir handoff 有去重，TS 无 | 保留去重 |
| — | Elixir handoff 有 context_delivered，TS 无 | 必须实现，防重复注入 |
| — | Elixir complete_handoff 只完成 accepted，TS 会 fallback 到 pending | 以 Elixir 为准 |
| — | read_at 字段存在但从不写入 | 如需已读时间戳，自行补逻辑 |
| — | inbox 表有大量 dead 迁移列（subject/content/status/is_read/metadata） | 忽略 |

## 验收标准

- [ ] send_message 插入 DB 并广播
- [ ] get_pending_messages 返回未读消息，ASC 排序
- [ ] mark_read_by_ids 按 ID 批量标记（避免竞态）
- [ ] create_handoff 去重（相同 from→to+summary 的 active 不新建）
- [ ] accept_pending_handoffs 批量 pending → accepted
- [ ] complete_handoff 只完成 accepted（不 fallback 到 pending）
- [ ] approve_handoff completed → approved（终态）
- [ ] reopen_handoff completed → accepted，重置 context_delivered=0
- [ ] mark_delivered 批量标记 context_delivered=1
- [ ] get_unreported_accepted_handoffs 找出 expect_report=1 且 reported_up=0
- [ ] trigger context 包含 5 个 blocks
- [ ] inbox 消息在 LLM 非空输出后才标记已读
