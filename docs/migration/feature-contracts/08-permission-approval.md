# 功能契约 08：权限与审批

> **本文件是功能契约（层 1），用 spec 语言描述模块的行为契约。**

## 元信息

| 项 | 值 |
|---|---|
| 模块编号 | 08 |
| 模块名称 | 权限与审批 |
| Elixir 源码 | `services/permission.ex` + `services/approval.ex` + `schema/permission_request.ex` |
| TS 参考源码 | `packages/core/src/permission-service.ts` + `packages/core/src/approval-service.ts` |
| OpenCode 参考源码 | `D:\PC_AI\Project\opencode\packages\opencode\src\tool\permission.ts` |
| 状态 | 草稿 |

## 功能概述

两级权限系统：(1) `PermissionService` 同步评估工具调用，返回 `allow`/`deny`/`ask`，支持 4 种预设模式 + glob 规则匹配；(2) `ApprovalService` 异步审批流，当工具被判定为 `ask` 时创建请求并阻塞等待用户响应或超时。

## 接口契约

### Permission 接口

| 函数 | 参数 | 返回值 | 说明 |
|---|---|---|---|
| `evaluate` | `(agent, tool_name, input?)` | `:allow` / `:deny` / `:ask` | 权限评估入口 |
| `matches_pattern?` | `(tool_name, patterns, input?)` | `boolean` | glob 规则匹配 |
| `get_permission_mode` | `(agent)` | `string` | 获取权限模式（默认 `"executor"`） |

### Approval 接口

| 函数 | 参数 | 返回值 | 说明 |
|---|---|---|---|
| `request_permission` | `(agent_id, project_id, tool_name, tool_args, desc)` | `:ok` / `{:error, {:rejected, reason}}` / `{:error, :timeout}` | 异步审批请求 |
| `resolve_request` | `(request_id, decision, user_note?)` | `:ok` / `{:error, :not_found}` | 用户响应审批 |
| `get_pending_requests` | `(project_id)` | `[request]` | 待处理审批列表 |
| `load_saved_rules` | `(agent_id)` | `[string]` | 已保存的永久 allow 规则 |
| `remember_approval` | `(agent_id, project_id, tool_pattern)` | `:ok` | 保存永久规则 |
| `cleanup_orphaned_requests` | — | `:ok` | 清理孤儿请求（启动时） |

## 数据模型

### permission_requests 表（meta DB）

```sql
CREATE TABLE permission_requests (
  id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  project_id TEXT,
  tool_name TEXT NOT NULL,
  tool_arguments TEXT DEFAULT '{}',
  description TEXT DEFAULT '',
  status TEXT DEFAULT 'pending',  -- pending | approved | rejected | timeout | orphaned
  remember INTEGER DEFAULT 0,
  user_note TEXT,
  created_at INTEGER,
  updated_at INTEGER
);
```

### permission_rules 表（meta DB，Elixir 独有）

```sql
CREATE TABLE permission_rules (
  id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  project_id TEXT,
  tool_pattern TEXT NOT NULL,
  action TEXT DEFAULT 'allow',
  created_at INTEGER
);
```

## 核心流程

### 权限评估

```
1. evaluate(agent, tool_name, input):
   a. 获取 agent 的 permission_mode（readonly/readwrite/full/custom）
   b. 检查顺序（deny 最高优先）：
      1. deny rules → 命中即 :deny
      2. ask rules → 命中即 :ask
      3. allow rules → 命中即 :allow
      4. fallback（依 mode）：
         - readonly: 在 preset 中 → :allow，否则 :ask
         - readwrite: 同上
         - full: :allow
         - custom: :ask
         - 未知: :allow
   c. shell_tools（bash, run_command）在 readonly 下强制 :deny
```

### 审批流程

```
1. ToolExecutor 调用 evaluate 返回 :ask
2. 检查 load_saved_rules 是否有永久 allow → 命中则跳过审批
3. request_permission:
   a. 写 meta DB permission_requests（status='pending'）
   b. 广播 :permission_request 到前端
   c. 阻塞等待结果（120s 超时）
4. 前端响应 → resolve_request:
   a. UPDATE status = 'approved' / 'rejected'
   b. 通知等待的调用方
5. 如果 remember=true:
   a. remember_approval → 写 permission_rules
6. 超时 → 返回 {:error, :timeout}
7. 启动时清理孤儿请求（status='pending' → 'orphaned'）
```

## 权限模式

| 模式 | 说明 | Fallback 行为 |
|---|---|---|
| `readonly` | 只读工具集（22 个） | preset 外 → :ask |
| `readwrite` | 读写工具集 | preset 外 → :ask |
| `full` | 全部允许 | 一律 :allow |
| `custom` | 自定义规则 | 一律 :ask |
| 未知/默认 | — | 一律 :allow |

### readonly preset 工具（22 个）

read_file, list_files, grep, read_skill, read_project_memory, read_charter, read_goals, get_project_time, get_real_time, question, todowrite, message_superior, send_message, write_work_log, check_agent_status, read_roster, list_subordinates, list_all_agents, read_work_logs, get_skill_detail, list_available_skills, list_available_mcp, git_worktree_list, git_worktree_status

### shell_tools 强制拒绝

`bash` 和 `run_command` 在 readonly 模式下强制 :deny，即使不在 deny 列表中。

## 常量引用

| 常量 | 值 | 所在章节 |
|---|---|---|
| 审批超时 | `120_000` ms | 工具执行 |
| 权限缓存 TTL（TS） | `30_000` ms | — |
| 旧请求清理周期（TS） | `3_600_000` ms | — |
| 旧请求保留天数 | `7` 天 | — |

## 已知问题

| 问题编号 | 说明 | Python 迁移处理 |
|---|---|---|
| — | Elixir 审批超时 120s，TS 300s | 用 120s（对齐 Elixir active backend） |
| — | Elixir readonly 22 个工具，TS 仅 5 个 | 用 Elixir 的 22 个（更完善） |
| — | Elixir fallback 返回 :ask，TS 返回 :deny | 用 :ask（给用户选择权） |
| — | Elixir param-pattern 大小写不敏感，TS 敏感 | 大小写不敏感 |
| — | Elixir saved_rules 用独立表，TS 用 agent JSON 字段 | 用独立表（更灵活） |
| — | Elixir orphan 状态为 'orphaned'，TS 为 'rejected' | 用 'orphaned'（语义更准确） |
| — | Elixir get_permission_mode 默认 "executor"，但无此分支 → 落到 :allow | 修正：默认应为 readonly 或自定义 |

## 验收标准

- [ ] evaluate 返回 allow/deny/ask
- [ ] deny 规则优先级最高
- [ ] readonly 模式下 shell_tools 强制 deny
- [ ] readonly/readwrite 模式 fallback 返回 :ask
- [ ] full 模式一律 :allow
- [ ] custom 模式一律 :ask
- [ ] request_permission 创建 pending 请求并广播
- [ ] resolve_request 更新状态并通知等待方
- [ ] 审批超时 120s 返回 {:error, :timeout}
- [ ] remember=true 时保存永久规则到 permission_rules
- [ ] load_saved_rules 返回已保存的 allow 规则
- [ ] 启动时清理孤儿请求（pending → orphaned）
- [ ] glob 规则匹配大小写不敏感
- [ ] readonly preset 包含 22 个只读工具

## Python 实现建议

- `class PermissionService` + 30s 缓存
- `class ApprovalService` + asyncio.Event 实现异步等待
- 权限模式用 enum
- glob 匹配用 `fnmatch` 或自定义正则
- 审批请求用 `asyncio.Future` 实现等待/超时
