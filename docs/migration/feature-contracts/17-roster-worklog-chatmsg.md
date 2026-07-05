# 功能契约 17：Roster + WorkLog + ChatMessage

> **本文件是功能契约（层 1），用 spec 语言描述模块的行为契约，不引用实现代码。**
> **任何 AI 工具实现 Python 版本时，必须满足此契约中的所有要求。**

## 元信息

| 项 | 值 |
|---|---|
| 模块编号 | 17 |
| 模块名称 | 人事花名册 + 工作日志 + UI 消息持久化 |
| Elixir 源码 | `apps/hiveweave/lib/hiveweave/services/roster.ex` + `services/dispatch.ex` + `services/chat_message.ex` + `schema/personnel_record.ex` + `schema/work_log.ex` + `schema/chat_message.ex` |
| TS 参考源码 | `packages/core/src/services/roster-service.ts` + `dispatch-service.ts` + `chat-message-service.ts` |
| OpenCode 参考源码 | —（无对应，HiveWeave 自有功能） |
| 状态 | 草稿 |

## 功能概述

三个 per-project DB 持久化服务的组合契约：

1. **Roster（人事花名册）** — 维护每个 agent 的人事记录（职位/部门/职责/状态），由 HR 通过 `update_roster` 工具 upsert，供组织管理读取。
2. **WorkLog（工作日志）** — 记录 agent 的工作行为（讨论/完成/错误/决策等），由 `write_work_log` 工具写入，coordinator 通过 `get_subordinate_logs` 审查下属工作进展。
3. **ChatMessage（UI 消息持久化）** — 持久化 UI 展示层的聊天消息（区别于契约 03 的 `conversation_turns` 后者是 LLM 调用历史），支持流式状态管理、僵尸消息清理、未读背景消息检测、未回复用户消息检测。

三者均落在 per-project DB（见契约 11），通过 `ProjectFactory.query(project_id, ...)` 或 `ProjectFactory.query_for_agent(agent_id, ...)` 路由。

## 接口契约

### Roster 接口

| 函数 | 参数 | 返回值 | 说明 |
|---|---|---|---|
| `update_roster` | `(project_id, agent_id, attrs)` | `{:ok, "Roster updated"}` \| `{:error, reason}` | 按 (project_id, agent_id) upsert 人事记录 |
| `get_roster` | `(project_id)` | `{:ok, formatted_text}` \| `{:error, reason}` | 读取全项目花名册，返回格式化文本 |

**`update_roster` 的 `attrs` 字段：**

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `position` | string | `""` | 职位 |
| `department` | string | `""` | 部门 |
| `responsibilities` | string | `""` | 职责描述 |
| `status` | string | `"active"` | 状态（upsert 时硬编码为 `active`） |
| `updated_by` | string | `agent_id` | 更新者（自动填为被更新的 agent_id） |

### WorkLog 接口

| 函数 | 参数 | 返回值 | 说明 |
|---|---|---|---|
| `write_work_log` | `(project_id, agent_id, session_id, type, summary, details?)` | `{:ok, log_id}` \| `{:error, reason}` | 写入一条工作日志 |
| `dispatch_task` | `(project_id, from_agent_id, to_agent_id, description, session_id)` | `{:ok, %{task_id, from_agent_id, to_agent_id, description}}` \| `{:error, reason}` | coordinator 派发任务时写一条 `type=discussion` 的日志 |
| `get_subordinate_logs` | `(project_id, subordinate_agent_id, limit=10)` | `[log_map]` | 取下属最近 N 条日志（newest first） |
| `get_agent_logs` | `(project_id, agent_id, limit=20)` | `[log_map]` | 取自己的日志（`get_subordinate_logs` 别名） |
| `get_subordinate_logs_since` | `(project_id, subordinate_agent_id, since_ts)` | `[log_map]` | 增量读取（created_at > since_ts，oldest first） |
| `approve_work` | `(project_id, coordinator_id, session_id, subordinate_id, review?)` | `{:ok, log_id}` | 批准下属工作，写 `type=completion` 日志 |
| `reject_work` | `(project_id, coordinator_id, session_id, subordinate_id, feedback)` | `{:ok, log_id}` | 驳回下属工作，写 `type=error` 日志 |

**`write_work_log` 工具暴露给 LLM 的参数（`write_work_log` 工具定义）：**

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `type` | enum: `discussion` / `completion` / `error` / `decision` | 否（默认 `discussion`） | 日志类型 |
| `summary` | string | 是 | 日志摘要 |

> 注：`details` 字段在工具层未暴露给 LLM，由服务端按上下文填充（如 `approve_work` 会填 `{subordinate_id, review}`）。

**日志条目返回结构（`get_subordinate_logs` 等）：**

```
{
  id:           string (UUID),
  agent_id:     string,
  type:         string,
  summary:      string,
  details:      map (JSON 解析后的 dict，解析失败返回 {}),
  created_at:   integer (ms epoch)
}
```

### ChatMessage 接口

| 函数 | 参数 | 返回值 | 说明 |
|---|---|---|---|
| `save_message` | `(attrs)` | `{:ok, %{id, role, content, created_at}}` \| `{:error, reason}` | 保存一条 UI 消息 |
| `update_message` | `(agent_id, id, attrs)` | `{:ok, %{id}}` \| `{:error, reason}` | 更新已存在消息的 content/is_read/is_streaming/tool_calls/thinking |
| `update_streaming_messages_done` | `(agent_id)` | `{:ok, _}` \| `{:error, reason}` | 把该 agent 所有 `is_streaming=1` 的消息标记为 `0` |
| `get_messages` | `(agent_id, limit=200)` | `[msg_map]` | 取最近 N 条消息（按时间正序返回，内部用 DESC + reverse） |
| `mark_as_read` | `(agent_id, ids)` | `integer`（标记条数） | 按 id 列表批量标记已读；空列表返回 0 |
| `get_unread_background` | `(agent_id)` | `[msg_map]` | 取未读背景消息（`is_background=1 AND is_read=0`），按 created_at ASC |
| `has_unanswered_user_messages?` | `(agent_id)` | `boolean` | 检测是否存在未回复的用户消息 |
| `clear_stuck_streaming` | `()` | `:ok` | 启动时遍历所有 project，清除 `is_streaming=1` 的僵尸消息 |

**`save_message` 的 `attrs` 字段：**

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `id` | string (UUID) | 自动生成 | 消息 ID |
| `agent_id` | string | 必填 | 所属 agent |
| `role` | string | `"assistant"` | 角色（user/assistant/team/system 等） |
| `content` | string | `""` | 消息文本 |
| `tool_calls` | string (JSON) | `"[]"` | 工具调用 JSON |
| `thinking` | string \| nil | nil | 思维链内容 |
| `is_background` | int(bool) | `0` (false) | 是否背景消息 |
| `is_read` | int(bool) | `1` (true) | 是否已读 |
| `is_streaming` | int(bool) | `0` (false) | 是否正在流式输出 |
| `is_context` | int(bool) | `0` (false) | 是否为注入的上下文消息 |
| `team_from_agent_id` | string \| nil | nil | 群聊来源 agent |
| `team_to_agent_id` | string \| nil | nil | 群聊目标 agent |
| `created_at` | int (ms) | 当前时间 | 创建时间戳 |

> **布尔存储**：SQLite 中以 `0/1` 整数存储，服务层做 bool↔int 转换。`is_read` 默认 `true`（assistant 消息默认已读），`is_background`/`is_streaming`/`is_context` 默认 `false`。

### 输入（Consumes）

| 输入 | 来源 | 格式 | 说明 |
|---|---|---|---|
| `update_roster` 调用 | HR agent 的 `update_roster` 工具 | `{position, department, responsibilities}` | HR 为下属维护花名册 |
| `write_work_log` 调用 | executor/coordinator 的 `write_work_log` 工具 | `{type, summary}` | agent 主动记录工作进展 |
| `dispatch_task` 调用 | coordinator 派发任务时 | `(from, to, description, session_id)` | 派发即写一条 discussion 日志 |
| `approve_work` / `reject_work` 调用 | coordinator 审查工具 | `(coordinator_id, subordinate_id, review/feedback)` | 审查决策落日志 |
| `save_message` 调用 | Streamer / WebSocket handler | `attrs map` | UI 层每条消息持久化 |
| `clear_stuck_streaming` 调用 | 服务启动钩子 | — | 启动时清理僵尸流式消息 |

### 输出（Produces）

| 输出 | 目标 | 格式 | 说明 |
|---|---|---|---|
| 格式化花名册文本 | coordinator/HR 工具返回 | markdown 文本 | `## Personnel Roster\n\n<entries>` |
| 工作日志列表 | coordinator `read_work_logs` 工具 | `[log_map]` | 用于审查下属工作 |
| UI 消息列表 | 前端 WebSocket / REST | `[msg_map]` | 前端 ChatPanel 渲染 |
| 未回复用户消息标志 | Streamer 调度决策 | `boolean` | true 时触发 agent 重新处理 |

### 副作用

| 副作用 | 触发条件 | 目标 | 说明 |
|---|---|---|---|
| 写 `personnel_records` | `update_roster` | per-project DB | 先 DELETE 再 INSERT（upsert 实现） |
| 写 `work_logs` | `write_work_log` / `dispatch_task` / `approve_work` / `reject_work` | per-project DB | INSERT，details 字段 JSON 序列化 |
| 写 `chat_messages` | `save_message` / TeamChat `record_message` | per-project DB | INSERT |
| 更新 `chat_messages` | `update_message` / `mark_as_read` / `update_streaming_messages_done` | per-project DB | UPDATE |
| 清僵尸流式标志 | 服务启动 | 所有 per-project DB `chat_messages` | `UPDATE ... SET is_streaming=0 WHERE is_streaming=1` |

## 数据模型

### `personnel_records` 表（per-project DB）

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | UUID |
| `project_id` | TEXT | NOT NULL | 项目 ID |
| `agent_id` | TEXT | NOT NULL | Agent ID |
| `position` | TEXT | | 职位 |
| `department` | TEXT | | 部门 |
| `responsibilities` | TEXT | | 职责 |
| `notes` | TEXT | | 备注（schema 定义，当前服务未写入） |
| `status` | TEXT | DEFAULT `'active'` | active / inactive / archived |
| `updated_by` | TEXT | | 更新者 agent_id |
| `updated_at` | INTEGER | | 更新时间（ms） |

> **唯一性**：(project_id, agent_id) 隐式唯一（服务层用 DELETE + INSERT 实现 upsert）。表无 UNIQUE 约束，依赖服务层保证。

### `work_logs` 表（per-project DB）

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | UUID |
| `agent_id` | TEXT | NOT NULL | 执行 agent |
| `project_id` | TEXT | | 项目 ID |
| `session_id` | TEXT | | 会话 ID（dispatch/write_work_log 写入） |
| `task_id` | TEXT | | 任务 ID（schema 定义，dispatch 未写入） |
| `action` | TEXT | | 动作类型（schema 定义，dispatch 未使用，见已知问题） |
| `type` | TEXT | | 日志类型：`discussion` / `completion` / `error` / `decision` |
| `summary` | TEXT | | 摘要 |
| `content` | TEXT | | 内容（表定义，当前服务未写入） |
| `details` | TEXT | DEFAULT `'{}'` | JSON 字符串，结构化详情 |
| `metadata` | TEXT | DEFAULT `'{}'` | JSON 字符串，元数据（schema 定义，当前服务未写入） |
| `created_at` | INTEGER | | 创建时间（ms） |

### `chat_messages` 表（per-project DB）

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | UUID |
| `agent_id` | TEXT | NOT NULL | 所属 agent |
| `role` | TEXT | NOT NULL | user / assistant / team / system |
| `content` | TEXT | | 消息文本 |
| `thinking` | TEXT | | 思维链 |
| `tool_calls` | TEXT | | 工具调用 JSON |
| `tool_call_id` | TEXT | | 工具调用 ID（表定义，当前服务未写入） |
| `is_streaming` | INTEGER | DEFAULT 0 | 流式中标志 |
| `is_background` | INTEGER | DEFAULT 0 | 背景消息标志 |
| `is_read` | INTEGER | DEFAULT 1 | 已读标志 |
| `is_context` | INTEGER | DEFAULT 0 | 上下文注入消息标志 |
| `team_from_agent_id` | TEXT | | 群聊来源 |
| `team_to_agent_id` | TEXT | | 群聊目标 |
| `images` | TEXT | | 图片 JSON（schema 定义） |
| `metadata` | TEXT | | 元数据 JSON |
| `created_at` | INTEGER | | 创建时间（ms） |

> **与 `conversation_turns` 的区别（重要）**：
> - `conversation_turns`（契约 03）— **LLM 调用历史**，用于 compaction / token budget 裁剪。System 消息不入库，每次由 Streamer 重建。
> - `chat_messages`（本契约）— **UI 展示层消息**，包含 user/assistant/team 全角色，含流式状态/已读状态/背景标志，供前端 ChatPanel 渲染。两者独立，不互相同步。

## 核心流程

### Roster upsert

```
1. update_roster(project_id, agent_id, attrs):
   a. 生成新 UUID 作为 id
   b. DELETE FROM personnel_records WHERE project_id=? AND agent_id=?
   c. INSERT 新记录（status 硬编码 'active'，updated_by=agent_id，updated_at=now_ms）
   d. 成功返回 {:ok, "Roster updated"}，失败返回 {:error, reason}
```

### Roster 读取

```
1. get_roster(project_id):
   a. SELECT personnel_records LEFT JOIN agents（取 name/role/short_id）
   b. ORDER BY department, position
   c. 每条格式化为多行文本：
      "<name> (<short_id>) — <role>\n  Position: <position>\n  Department: <department>\n  Responsibilities: <responsibilities>\n  Status: <status>"
   d. 条目间用 "\n---\n" 连接
   e. 整体前缀 "## Personnel Roster\n\n"
   f. 空结果返回 "Roster is empty. No personnel records found."
```

### WorkLog 写入

```
1. write_work_log(project_id, agent_id, session_id, type, summary, details):
   a. 生成 UUID
   b. details 若为 map → JSON 序列化；若为 string → 直用；nil → "{}"
   c. type 为 nil → 默认 "discussion"
   d. INSERT 到 work_logs
   e. 返回 {:ok, log_id}

2. dispatch_task(project_id, from, to, description, session_id):
   a. details = JSON({from_agent_id, to_agent_id, description})
   b. write_work_log(project_id, from, session_id, "discussion", description, details)
   c. 返回 {:ok, %{task_id: log_id, from_agent_id, to_agent_id, description}}

3. approve_work / reject_work:
   a. 构造 summary（含 subordinate_id 和 review/feedback）
   b. write_work_log with type="completion" (approve) / "error" (reject)
   c. details 含 {subordinate_id, review/feedback}
```

### WorkLog 读取

```
1. get_subordinate_logs(project_id, agent_id, limit=10):
   a. SELECT id, agent_id, type, summary, details, created_at
   b. WHERE agent_id=? ORDER BY created_at DESC LIMIT ?
   c. details 字段 JSON 反序列化为 map（失败返回 {}）
   d. 返回 list of map

2. get_subordinate_logs_since(project_id, agent_id, since_ts):
   a. 同上但 WHERE created_at > since_ts，ORDER BY created_at ASC（oldest first）
   b. 用于增量读取
```

### ChatMessage 保存

```
1. save_message(attrs):
   a. id 缺省 → 生成 UUID
   b. role 缺省 → "assistant"
   c. content 缺省 → ""
   d. tool_calls 缺省 → "[]"
   e. is_background 缺省 → 0；is_read 缺省 → 1；is_streaming 缺省 → 0；is_context 缺省 → 0
   f. created_at 缺省 → 当前 ms
   g. bool 转 int（true→1, false→0）
   h. INSERT 到 chat_messages
   i. 返回 {:ok, %{id, role, content, created_at}}
```

### ChatMessage 流式状态管理

```
1. update_streaming_messages_done(agent_id):
   a. UPDATE chat_messages SET is_streaming=0 WHERE agent_id=? AND is_streaming=1
   b. 用于 safety_timeout / :DOWN handler，防止崩溃后僵尸流式消息

2. clear_stuck_streaming()（启动时）:
   a. 从 Meta DB 读取所有 project_id
   b. 对每个 project 执行：UPDATE chat_messages SET is_streaming=0 WHERE is_streaming=1
   c. 单个 project 失败仅 warning 日志，不中断整体
   d. 整体异常被 rescue，返回 :ok
```

### ChatMessage 未回复用户消息检测

```
1. has_unanswered_user_messages?(agent_id):
   a. SQL: EXISTS(
        SELECT 1 FROM chat_messages m1
        WHERE m1.agent_id=?
          AND m1.role='user'
          AND m1.is_background=0
          AND NOT EXISTS(
            SELECT 1 FROM chat_messages m2
            WHERE m2.agent_id=m1.agent_id
              AND m2.role='assistant'
              AND m2.is_background=0
              AND m2.created_at >= m1.created_at
          )
      )
   b. 含义：存在前台 user 消息，其后（含同时刻）没有任何前台 assistant 消息响应
   c. 返回 boolean；任何异常返回 false（fail-safe）
```

### ChatMessage 未读背景消息

```
1. get_unread_background(agent_id):
   a. SELECT ... WHERE agent_id=? AND is_background=1 AND is_read=0
   b. ORDER BY created_at ASC（oldest first，便于按序处理）
   c. 返回 list of map；异常返回 []
```

### ChatMessage 批量标记已读

```
1. mark_as_read(agent_id, ids):
   a. ids 为空 → 返回 0
   b. 构造 IN (?, ?, ...) 占位符
   c. UPDATE chat_messages SET is_read=1 WHERE id IN (...)
   d. 返回标记条数（length(ids)）
   e. 异常返回 0
```

## 状态机（如适用）

### ChatMessage 流式状态

| 当前状态 | 触发条件 | 目标状态 | 动作 |
|---|---|---|---|
| (新消息) | `save_message` with `is_streaming=1` | `streaming` | INSERT，前端显示流式动画 |
| `streaming` | 流式完成 | `done` | `update_message(is_streaming=0)` |
| `streaming` | agent 崩溃/超时 | `done` | `update_streaming_messages_done` 批量清零 |
| `streaming` | 服务重启 | `done` | `clear_stuck_streaming` 启动时全量清零 |

### PersonnelRecord 状态

| 当前状态 | 触发条件 | 目标状态 | 动作 |
|---|---|---|---|
| (无记录) | `update_roster` | `active` | INSERT，status='active' |
| `active` | `update_roster`（再次） | `active` | DELETE + INSERT（status 重置为 active） |

> 注：当前实现中 `update_roster` 总是写入 `status='active'`，无法通过该接口切换为 `inactive`/`archived`。状态转换需 DB 直接操作或扩展接口。

## 错误处理

| 错误场景 | 处理方式 | 重试策略 | 升级策略 |
|---|---|---|---|
| DB INSERT/UPDATE 失败 | 返回 `{:error, reason}` | 不重试 | 由调用方决定 |
| `get_roster` 异常 | rescue 后返回 `{:error, "Failed to read roster"}` | 不重试 | — |
| `get_messages` / `get_unread_background` 异常 | rescue 后返回 `[]` | 不重试 | —（fail-empty） |
| `has_unanswered_user_messages?` 异常 | rescue 后返回 `false` | 不重试 | —（fail-safe，不误触发） |
| `mark_as_read` 异常 | rescue 后返回 `0` | 不重试 | — |
| `clear_stuck_streaming` 单 project 失败 | warning 日志，继续下一个 | 不重试 | 整体不中断 |
| `clear_stuck_streaming` 整体异常 | rescue 后返回 `:ok` | 不重试 | —（启动钩子不阻塞） |
| `details` JSON 解析失败 | 返回 `{}` | 不重试 | — |
| `save_message` 异常 | rescue 后返回 `{:error, e}` | 不重试 | — |

## 常量引用

| 常量 | 值 | 所在章节 |
|---|---|---|
| Per-project DB journal mode | `DELETE` | 数据库 |
| Per-project DB busy_timeout | `5000` ms | 数据库 |
| Per-project DB pool_size | 单连接（已确认） | 数据库 |
| `get_messages` 默认 limit | `200` | 本契约 |
| `get_subordinate_logs` 默认 limit | `10` | 本契约 |
| `get_agent_logs` 默认 limit | `20` | 本契约 |
| `get_history`（TeamChat）默认 limit | `50` | 本契约 |
| 时间戳单位 | 毫秒（ms epoch） | 本契约 |

## 已知问题

| 问题编号 | 说明 | Python 迁移处理 |
|---|---|---|
| E5（新） | `work_logs` 表有 `action` 和 `type` 两个字段，schema 定义了 `action`（约定用于 code_write/test_write/review/analysis/decision/communication 等细粒度动作），但 `dispatch.ex` 实际只用 `type`（discussion/completion/error/decision 四类粗粒度）。`write_work_log` 工具也只暴露 `type`。`task_id`/`content`/`metadata` 字段表有定义但服务未写入 | Python 实现建议：统一为单一字段。可选择：(a) 保留 `type` 4 类（对齐当前行为）；(b) 扩展为 `action` 细粒度类型（code_write/test_write/review/analysis/decision/communication），需同步扩展工具 schema。本契约不强制，由实现者决策并记录 |
| E6（新） | `update_roster` 的 upsert 用 DELETE + INSERT 实现，非原子（中间窗口该 agent 无记录）。`status` 硬编码为 `'active'`，无法通过接口切换为 `inactive`/`archived` | Python 用 SQLite `INSERT OR REPLACE` 或 `INSERT ... ON CONFLICT` 原子 upsert；`status` 应可由调用方传入，默认 `active` |
| E7（新） | `personnel_records` 表无 UNIQUE 约束，唯一性依赖服务层 DELETE+INSERT 顺序。并发写入可能产生重复 | Python 加 `UNIQUE(project_id, agent_id)` 约束 + `ON CONFLICT` upsert |
| E8（新） | `chat_messages` 表的 `tool_call_id` / `images` / `metadata` 字段在 `chat_message.ex` 服务中未写入（`save_message` 不处理 `images`，尽管 schema 和任务描述提到）。`update_message` 不支持更新 `images` | Python 实现应补全 `images` 字段的保存（前端可能上传图片），`save_message` 接受 `images` 参数 |
| E4 | 空 recipients 可能崩溃 | 本契约不直接涉及，但 TeamChat（契约 18）调用 `save_message` 时需防御 |

## Python 实现建议

- **框架/库**：
  - SQLAlchemy 2.x（async） + aiosqlite 作为 per-project DB 驱动
  - 每个 per-project DB 一个独立的 `AsyncSession` 工厂（见契约 11）
  - Pydantic v2 做 attrs 校验
- **架构模式**：
  - 三个独立的 repository class：`RosterRepository` / `WorkLogRepository` / `ChatMessageRepository`
  - 每个 repository 接受 `project_id` 或 `agent_id`，内部解析到正确的 per-project DB 连接
  - ORM 模型类对应三张表，字段类型用 SQLAlchemy `String` / `Integer` / `Boolean`（Boolean 在 SQLite 存为 0/1）
- **关键实现点**：
  - **区分 `conversation_turns` 和 `chat_messages`**：前者是 LLM 历史（契约 03），后者是 UI 展示。两者独立表，独立 repository，不互相同步
  - **布尔字段存储**：SQLite 无原生 bool，用 `Integer` 存 0/1，Pydantic 模型层做 bool↔int 转换
  - **`has_unanswered_user_messages?`**：用 SQLAlchemy `exists()` 子查询实现，注意 `created_at >=` 比较（含同时刻）
  - **`clear_stuck_streaming`**：启动钩子，遍历 Meta DB 的 `projects` 表，对每个 per-project DB 执行 UPDATE。单个失败不中断
  - **`mark_as_read`**：用 `IN` 子句批量更新，空列表直接返回 0 不发 SQL
  - **JSON 字段**：`details` / `metadata` / `tool_calls` 存为 TEXT，读取时 JSON 反序列化，失败返回空 dict/list
  - **时间戳**：统一用 `int(time.time() * 1000)` 毫秒
- **注意事项**：
  - `update_roster` 的 upsert 用 SQLite `INSERT OR REPLACE INTO ... ON CONFLICT(project_id, agent_id) DO UPDATE SET ...` 原子操作，替代 DELETE+INSERT
  - `status` 字段应可由调用方传入，默认 `active`，支持后续 `inactive`/`archived` 转换
  - `work_logs` 的 `action` vs `type` 字段二选一，建议统一为 `type`（对齐当前行为）并补全 task_id/content/metadata 写入
  - `chat_messages` 补全 `images` 字段保存（前端图片消息）
  - 所有 `rescue` 对应 Python 的 `try/except`，fail-safe 路径（返回 []/false/0）必须保留

## 验收标准

- [ ] `update_roster(project_id, agent_id, {position, department, responsibilities})` 后，`get_roster(project_id)` 返回的文本包含该 agent 的 position/department/responsibilities
- [ ] 同一 agent 多次 `update_roster`，`get_roster` 只返回一条记录（upsert 语义）
- [ ] `write_work_log` 后 `get_subordinate_logs` 返回的列表包含该条目，`details` 为 dict
- [ ] `dispatch_task` 写入的日志 `type='discussion'`，`details` 含 from/to/description
- [ ] `approve_work` 写入 `type='completion'`；`reject_work` 写入 `type='error'`
- [ ] `get_subordinate_logs_since` 只返回 `created_at > since_ts` 的条目，按 ASC 排序
- [ ] `save_message` 后 `get_messages` 能取到该消息，默认值正确（role=assistant, is_read=1, is_background=0, is_streaming=0）
- [ ] `update_streaming_messages_done(agent_id)` 后，该 agent 所有 `is_streaming=1` 的消息变为 `0`
- [ ] `clear_stuck_streaming()` 在启动时执行，所有 project 的 `is_streaming=1` 消息被清零；单 project 失败不中断
- [ ] `has_unanswered_user_messages?` 在仅有 user 消息无 assistant 响应时返回 `true`；有 assistant 响应后返回 `false`
- [ ] `has_unanswered_user_messages?` 忽略 `is_background=1` 的消息（背景 user 消息不触发未回复检测）
- [ ] `get_unread_background` 只返回 `is_background=1 AND is_read=0` 的消息，按 ASC 排序
- [ ] `mark_as_read(agent_id, [])` 返回 0，不发 SQL
- [ ] `mark_as_read(agent_id, [id1, id2])` 后，两条消息 `is_read=1`，返回 2
- [ ] 所有异常路径 fail-safe：`get_messages` 异常返回 `[]`，`has_unanswered_user_messages?` 异常返回 `false`，`mark_as_read` 异常返回 `0`
- [ ] `chat_messages` 表与 `conversation_turns` 表独立，`save_message` 不写 `conversation_turns`，`append_turn`（契约 03）不写 `chat_messages`

## 并行对比测试方案

| 测试场景 | Elixir 行为 | Python 预期行为 | 验证方法 |
|---|---|---|---|
| Roster upsert | DELETE + INSERT，status='active' | INSERT OR REPLACE，status='active' | 同一 agent 调用 2 次 update_roster，查 DB 只有 1 条记录 |
| Roster 格式化输出 | `## Personnel Roster\n\n<name> (<short>) — <role>\n  Position: ...` | 同格式 | 对比 `get_roster` 输出文本逐行一致 |
| WorkLog write + read | type/summary/details JSON | 同 | 写入后 get_subordinate_logs 返回 details 为 dict |
| WorkLog 增量读取 | created_at > since，ASC | 同 | 写 3 条，since=第2条 created_at，应返回第 2、3 条 |
| ChatMessage save + get | 默认值正确，DESC+reverse | 同 | save 后 get_messages 返回正序，含该条 |
| 流式清零 | update_streaming_messages_done | 同 | save 2 条 is_streaming=1，调用后查 DB 全为 0 |
| 启动清僵尸 | clear_stuck_streaming 遍历所有 project | 同 | 人为插入 is_streaming=1，重启服务后查 DB |
| 未回复检测 | user 消息后无 assistant → true | 同 | save user 消息，调用 has_unanswered_user_messages? 返回 true；再 save assistant 消息后返回 false |
| 未读背景 | is_background=1 AND is_read=0 | 同 | save 2 条背景消息（1 已读 1 未读），get_unread_background 只返回 1 条 |
| 批量已读 | IN 子句，空列表返回 0 | 同 | mark_as_read([], []) 返回 0；mark_as_read([id1,id2]) 返回 2 |
| 异常 fail-safe | get_messages 异常返回 [] | 同 | mock DB 异常，验证返回值 |

---

> **填写规则**：
> 1. 用 spec 语言，不用代码语言。说"做什么"，不说"怎么做"。
> 2. 所有常量值引用 `constants.md`，不在此处重复定义。
> 3. 所有已知问题引用 `known-issues.md`，不在此处重复描述。
> 4. 每个契约必须经过用户确认后才能标记为"已确认"。
> 5. "Python 实现建议"是建议不是约束，实现者可以根据情况调整，但必须满足验收标准。
