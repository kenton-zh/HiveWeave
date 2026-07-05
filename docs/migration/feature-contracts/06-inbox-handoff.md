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

> **已知限制（RECONCILE）**：源码 `handoff.ex` 中**无 cancel 和 timeout 转换**。handoff 一旦创建，只能沿上述路径流转至 `approved` 终态，或通过 `reopen` 回退。无超时自动取消机制——长期 pending/accepted 的 handoff 不会被自动清理。Python 迁移时如需超时取消，需新增状态转换逻辑（源码无此能力）。

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
5. mark_delivered(handoff_ids)          ← LLM 调用前执行
6. 调用 LLM
7. LLM 产出非空输出 → mark_read_by_ids(inbox_msg_ids)
   LLM 产出空输出   → 保留 inbox 未读（pending_inbox_msg_ids 不清空），等待重试
```

#### 崩溃恢复行为（RECONCILE）

`mark_delivered`（步骤 5）在 LLM 调用**前**执行，`mark_read_by_ids`（步骤 7）在 LLM 非空输出**后**执行。两者不在同一事务中，崩溃时存在不对称恢复行为：

| 崩溃时机 | handoff 状态 | inbox 状态 | 恢复行为 |
|---|---|---|---|
| 步骤 5 之后、LLM 调用中崩溃 | `context_delivered=1`（已标记） | 未读（未标记） | handoff **不重注入**（delivered 不可逆）；inbox 消息保留未读，下次 trigger 重新注入 |
| LLM 空输出重试中崩溃 | `context_delivered=1`（已标记） | 未读（未标记） | 同上 |

> **有效权衡**：handoff 的 `context_delivered=1` 是**不可逆**的——即使 LLM 空输出或崩溃，handoff 也不会重新注入 context。这意味着 agent 可能"漏看" handoff 内容。设计取舍是避免无限重注入循环，代价是空输出场景下 handoff 信息丢失。inbox 消息则相反，保留未读直到非空输出，确保消息不丢失但可能重复注入。

#### 空输出重试机制（RECONCILE）

LLM 空输出时（`{:empty, _, _}`），agent 按指数退避重试（5s → 15s → 45s，最多 3 次）：
- `pending_inbox_msg_ids` **保留不清空**，重试时可重新标记已读
- 超过 3 次重试后，上报上级并**强制标记 inbox 已读**（避免 `escalate → retrigger → empty → escalate` 无限循环）
- handoff 的 `context_delivered` 在首次 trigger 时已标记，重试不重注入

## 已知问题

| 问题编号 | 说明 | Python 迁移处理 |
|---|---|---|
| — | Elixir inbox ASC 排序，TS DESC | 用 ASC（FIFO 语义，旧消息优先处理） |
| — | Elixir 有 mark_read_by_ids，TS 无 | 必须保留，避免竞态 |
| H1 | **mark_all_read 与 mark_read_by_ids 竞态**：两者都是直接 SQL UPDATE，无事务包裹。`mark_read_by_ids` 的存在正是为避免 `mark_all_read` 在 trigger 场景的竞态（新消息在 build_context 和 mark_all_read 之间到达会被误标记已读）。 | **有效权衡**：trigger 场景只用 `mark_read_by_ids`，不用 `mark_all_read`；`mark_all_read` 供前端手动操作。同一 agent 的 GenServer 序列化消息处理，不会并发；不同 agent 操作各自 inbox（`WHERE to_agent_id = ?`），不冲突。Python 侧保持同样分工即可。 |
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
