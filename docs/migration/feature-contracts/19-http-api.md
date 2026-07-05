# 功能契约 19：HTTP API 层

> **本文件是功能契约（层 1），用 spec 语言描述模块的行为契约，不引用实现代码。**
> **任何 AI 工具实现 Python 版本时，必须满足此契约中的所有要求。**

## 元信息

| 项 | 值 |
|---|---|
| 模块编号 | 19 |
| 模块名称 | HTTP API 层 |
| Elixir 源码 | `apps/hiveweave/lib/hiveweave_web/router.ex` + `controllers/*.ex` + `plugs/api_key_auth.ex` |
| TS 参考源码 | `apps/server/src/routes/*.ts`（Fastify 路由） |
| OpenCode 参考源码 | 无（CLI 工具无 HTTP API） |
| 状态 | 草稿 |

## 功能概述

HiveWeave 后端的 HTTP REST API 层。共 68 个路由条目（按 controller 分组：Root / Health / Settings / Projects / Org / Chat / Permissions / Extra），覆盖项目管理、组织树、聊天触发、权限审批、LLM 模型注册、Agent 模板、通信、用户通知、闹钟、工作日志、调试追踪、文件浏览。HTTP 仅负责触发与查询，流式 token 与状态推送走 WebSocket（契约 12）。所有 `/api/*` 端点走 `:api` pipeline：JSON only、CORS、ApiKeyAuth；`GET /` 与 `GET /api/health` 免认证。无 API 版本前缀（无 `/api/v1`）。统一错误格式 `{error: "message"}` 或 `{error: "message", detail: "..."}`。

> 注：若将 PATCH/PUT 同动作的 5 对路由（projects/:id、org/agents/:id、permissions/rules/:agent_id、llm-models/:id 各 1 对，settings POST/PUT upsert 1 对）视为同一端点的两个 HTTP 方法别名，则"去重后端点数"为 63；再排除 `GET /`（HTML 非业务端点）则为 62。本契约按路由表实际条目数 68 编写。

## 总体规范

| 项 | 规范 | 说明 |
|---|---|---|
| API 前缀 | `/api` | 无 `/v1` 版本前缀 |
| 内容类型 | `application/json` only | `:api` pipeline `accepts: ["json"]` |
| 认证 | ApiKeyAuth plug | 读取 `HIVEWEAVE_API_KEY` env；未设置时跳过校验（dev/test 默认全放行） |
| 认证凭据来源 | `Authorization: Bearer <key>` header 或 `x-api-key` header 或 `?api_key=<key>` query | 三选一，使用 `secure_compare` 防时序攻击 |
| 免认证路径 | `GET /`、`GET /api/health` | 始终放行 |
| CORS | CORSPlug | 允许 origin：`http://localhost:5173`、`http://localhost:3200`、`http://localhost:4000` |
| 错误响应 | `{error: "message"}` 或 `{error: "message", detail: "..."}` | 401 由 ApiKeyAuth 返回 `{error: "Unauthorized — invalid or missing API key"}` |
| 字段命名 | 同时返回 snake_case 与 camelCase | 兼容前端；如 `workspace_path` 与 `workspacePath` 并存 |
| 端口 | 4000 | 见 `constants.md` |
| 端点总数 | 68 路由条目（去重 PATCH/PUT 别名后 63，再排除 `GET /` HTML 页为 62） | 见功能概述注释 |

## 接口契约

> 因端点数多，下面按 controller 分组，每组用一个表格描述。表格列：方法 / 路径 / 功能 / 请求体 / 响应 / 状态码 / 对应服务契约。

### 1. Root（1 端点）

| 方法 | 路径 | 功能 | 请求体 | 响应 | 状态码 | 对应服务契约 |
|---|---|---|---|---|---|---|
| GET | `/` | HTML 落地页（静态） | — | HTML 文档，列出 WS 通道与主要端点 | 200 | 无（无业务逻辑） |

### 2. Health（1 端点）

| 方法 | 路径 | 功能 | 请求体 | 响应 | 状态码 | 对应服务契约 |
|---|---|---|---|---|---|---|
| GET | `/api/health` | 健康检查（免认证） | — | `{status: "ok", version: "0.2.0", timestamp: <ms>}` | 200 | 无 |

### 3. Settings（4 端点）— 全局键值设置 CRUD

| 方法 | 路径 | 功能 | 请求体 | 响应 | 状态码 | 对应服务契约 |
|---|---|---|---|---|---|---|
| GET | `/api/settings` | 列出所有全局设置 | — | `{settings: [{key, value, updated_at}]}` | 200 | SettingsService（meta DB `global_settings` 表） |
| GET | `/api/settings/:key` | 查单个设置 | — | `{setting: {key, value, updated_at}}` | 200 / 404 | 同上 |
| POST | `/api/settings` | upsert（key 不存在则插入，存在则更新） | `{key, value}` | `{setting: {key, value, updated_at}}` | 200 / 500 | 同上 |
| PUT | `/api/settings` | upsert（同 POST） | `{key, value}` | 同上 | 同上 | 同上 |

### 4. Projects（10 端点）— 项目 CRUD + 工作空间 + 游戏时间 + 目标

| 方法 | 路径 | 功能 | 请求体 | 响应 | 状态码 | 对应服务契约 |
|---|---|---|---|---|---|---|
| GET | `/api/projects` | 列出所有项目 | — | `{projects: [Project]}` | 200 | ProjectService |
| POST | `/api/projects` | 创建项目，自动创建 CEO+HR+QA 三角色，启动 ProjectSupervisor | `{name, description?, workspacePath?, orgParadigm?, charterJson?, language?}`（language 默认 `zh`） | `{project: Project, mainAgentId: <ceo_id>}` | 200 / 422 | ProjectService + OrgService（创建 CEO/HR/QA） |
| GET | `/api/projects/:id` | 查项目详情，若未运行则自动启动（auto-boot） | — | `{project: Project}` | 200 / 404 | ProjectService + ProjectSupervisor |
| PATCH | `/api/projects/:id` | 更新项目 description / org_paradigm | `{description?, orgParadigm?}` | `{project: Project}` | 200 / 404 / 500 | ProjectService |
| PUT | `/api/projects/:id` | 同 PATCH | 同上 | 同上 | 同上 | 同上 |
| PUT | `/api/projects/:id/workspace` | 更新工作空间路径，含 `.hiveweave/` 迁移逻辑（见"特别流程 3"） | `{workspacePath}`（可为空、相同、或新路径） | `{ok: true, project: Project}` | 200 / 400 / 404 / 500 | ProjectService + ProjectFactory + ProjectSupervisor |
| DELETE | `/api/projects/:id` | 删除项目，含清理逻辑（见"特别流程 4"） | — | `{ok: true, dbLeftover: bool}` 或 `{ok: true, dbLeftover: true, warning: "..."}` | 200 / 404 | ProjectService + ProjectFactory + GitWorktreeService |
| GET | `/api/projects/:id/game-time` | 查游戏时间 | — | `{gameSeconds, projectId, formatted: "Day N HH:MM"}` | 200 | 契约 7（GameTimeService） |
| GET | `/api/projects/:id/goals` | 查项目章程（charter_json 解析） | — | `{goals: <parsed charter or null>}` | 200 / 404 | ProjectService + CharterService |
| PUT | `/api/projects/:id/goals` | 更新章程（序列化整个 params 为 charter_json），并 touch goals_version | 任意 goals 对象 | `{ok: true, project: Project}` | 200 / 404 / 500 | ProjectService + CharterService |

### 5. Org / Agents（9 端点）— Agent CRUD + 树 + 子节点 + 模块

| 方法 | 路径 | 功能 | 请求体 | 响应 | 状态码 | 对应服务契约 |
|---|---|---|---|---|---|---|
| GET | `/api/org` | 组织树（query: `projectId`） | — | `{tree: <tree>}` | 200 | 契约 4（OrgService.build_tree） |
| GET | `/api/org/agents` | 列出 agent（query: `projectId` 或 `project_id`） | — | `{agents: [Agent]}` | 200 | OrgService.list_agents |
| GET | `/api/org/agents/:id` | 查单个 agent | — | `{agent: Agent}` | 200 / 404 | OrgService.get_agent |
| GET | `/api/org/agents/:id/children` | 查直接子节点 | — | `{children: [Agent]}` | 200 / 404 | OrgService.get_children |
| POST | `/api/org/agents` | 创建 agent（short_id 自动生成） | `{name, projectId?, role?, parentId?, goal?, backstory?, permissionType?, modelId?}`（role 默认 `executor`，permissionType 默认 `executor`） | `{agent: Agent}` | 200 / 422 | OrgService.create_agent |
| PATCH | `/api/org/agents/:id` | 更新 agent（含 camelCase → snake_case 归一化） | `{name?, goal?, status?, backstory?, modelId?, parentId?, permissionType?, moduleId?}` | `{agent: Agent}` | 200 / 404 / 422 | OrgService.update_agent |
| PUT | `/api/org/agents/:id` | 同 PATCH | 同上 | 同上 | 同上 | 同上 |
| DELETE | `/api/org/agents/:id` | 删除 agent | — | `{ok: true}` | 200 / 404 / 500 | OrgService.delete_agent |
| GET | `/api/org/modules` | 列出项目模块（query: `projectId`） | — | `{modules: [Module]}` | 200 | ProjectFactory（per-project DB `modules` 表） |

### 6. Chat（11 端点）— 发送 + 历史 + 未读 + 收件箱 + 暂停/恢复 + 重置 + 解析模型

| 方法 | 路径 | 功能 | 请求体 | 响应 | 状态码 | 对应服务契约 |
|---|---|---|---|---|---|---|
| POST | `/api/chat` | 触发 agent 聊天（流式响应走 WebSocket）。含专家命令路由（见"特别流程 1"）与 busy 重试（见"特别流程 2"） | `{agentId, message, images?}` | 正常：`{ok: true, userMessageId}`；专家路由：`{ok: true, routed: true, expert, started?}`；排队：`{ok: true, queued: true, expert}`；busy 重试成功：`{ok: true, userMessageId, reset: true}` | 200 / 404 / 409 / 500 | 契约 1 + 契约 4 + Inbox + ChatMessage |
| GET | `/api/chat/history/:agentId` | 历史消息（限 200 条） | — | `{messages: [Message]}` | 200 | ChatMessageService |
| GET | `/api/chat/unread/:agentId` | 未读背景消息 | — | `{messages: [Message], count}` | 200 / 404 | ChatMessageService.get_unread_background |
| POST | `/api/chat/mark-read` | 批量标记已读 | `{ids: [...], agentId}` | `{ok: true, count}` | 200 | ChatMessageService.mark_as_read |
| GET | `/api/chat/inbox/:agentId` | 收件箱 | — | `{messages: [Inbox], unreadCount}` | 200 | 契约 6（InboxService） |
| POST | `/api/chat/inbox` | 发送 agent 间消息 | `{fromAgentId, toAgentId, content, type?, subject?, priority?, metadata?}` | `{ok: true, message: Inbox}` | 200 / 500 | InboxService.send_message |
| POST | `/api/chat/pause` | 暂停系统 | — | `{paused: true}` | 200 | SystemState.pause |
| POST | `/api/chat/resume` | 恢复系统 | — | `{paused: false}` | 200 | SystemState.resume |
| GET | `/api/chat/paused` | 查暂停状态 | — | `{paused: bool}` | 200 | SystemState.paused? |
| POST | `/api/chat/reset-processing/:agentId` | 强制重置 agent 处理状态（force_reset 信号 + 广播 idle） | — | `{ok: true, agentId, processing: false}` | 200 / 404 | Agent.force_reset + WebSocket 广播 |
| GET | `/api/chat/resolved-model/:agentId` | 查 agent 解析后的实际模型 | — | `{agentId, modelName, modelId, source: "auto"\|"none"}` | 200 / 404 | 契约 1（Streamer.resolve_model） |

### 7. Extra — ChatMessages / Todos / Questions（5 端点）

| 方法 | 路径 | 功能 | 请求体 | 响应 | 状态码 | 对应服务契约 |
|---|---|---|---|---|---|---|
| GET | `/api/chat/messages/:agentId` | 查 agent 消息（per-project DB，限 200 条，含 thinking/images 字段） | — | `[Message]`（数组直返，非包裹） | 200 | ProjectFactory.query_for_agent（per-project `chat_messages` 表） |
| GET | `/api/chat/todos/:agentId` | 查 agent 待办 | — | `{todos: [{id, content, status, priority}]}` | 200 | ProjectFactory（per-project `todos` 表） |
| POST | `/api/chat/todos/:agentId` | 覆盖写 agent 待办（先 DELETE 再 INSERT） | `{todos: [{content?, task?, status?, priority?}]}` | `{ok: true, todos}` | 200 | 同上 |
| GET | `/api/chat/questions` | 查待答问题（query: `agentId` 或 `projectId`） | — | `{questions: [{id, agentId, question, answer, status, createdAt, answeredAt}]}` | 200 | ProjectFactory（per-project `questions` 表） |
| POST | `/api/chat/questions/:id/answer` | 回答问题，广播 PubSub + 投递到 agent GenServer | `{answer, agentId}` | `{ok: true, answer}` | 200 / 400 / 500 | 同上 + PubSub + Agent 投递 |

> 注：`chat_questions_index` 路由在 router.ex 中无 `/:id` 段，但 answer 路由有。`POST /api/chat/questions/:id/answer` 必须传 `agentId` 用于定位 per-project DB 与 GenServer。

### 8. Permissions（6 端点）— 权限规则 + 待审批

| 方法 | 路径 | 功能 | 请求体 | 响应 | 状态码 | 对应服务契约 |
|---|---|---|---|---|---|---|
| GET | `/api/permissions/rules/:agent_id` | 查 agent 生效规则（mode + allowed/denied/ask/mcp/bound） | — | `{permissionMode, allowedTools, deniedTools, askTools, mcpServers, boundSkills}`（数组字段，JSON 解码后） | 200 / 404 | 契约 8（PermissionService） |
| PATCH | `/api/permissions/rules/:agent_id` | 更新规则（仅更新提供的字段） | `{permissionMode?, allowedTools?, deniedTools?, askTools?, mcpServers?, boundSkills?}` | 同 GET 响应 | 200 / 404 / 500 | 同上 |
| PUT | `/api/permissions/rules/:agent_id` | 同 PATCH | 同上 | 同上 | 同上 | 同上 |
| GET | `/api/permissions/pending/:agent_id` | 查 agent 待审批请求 | — | `[PermissionRequest]`（数组直返） | 200 / 404 | ApprovalService（meta DB `permission_requests` 表） |
| GET | `/api/permissions/pending/project/:project_id` | 查项目所有 agent 的待审批 | — | `[PermissionRequest]` | 200 | 同上 |
| POST | `/api/permissions/respond` | 审批响应 | `{requestId, approved, remember?, userNote?}` | `{ok: true, request: PermissionRequest}` | 200 / 400 / 404 | ApprovalService.resolve_request |

### 9. Extra — LLM Models（7 端点）— 模型注册 CRUD + 探测测试

| 方法 | 路径 | 功能 | 请求体 | 响应 | 状态码 | 对应服务契约 |
|---|---|---|---|---|---|---|
| GET | `/api/llm-models` | 列出所有模型（api_key 脱敏返回） | — | `{models: [Model]}` | 200 | ModelService（meta DB `llm_models` 表） |
| POST | `/api/llm-models` | 创建模型 | `{name, modelId?, baseUrl?, apiKey?, contextWindow?, maxOutputTokens?, supportsThinking?, defaultReasoningEffort?, temperature?, isActive?}` | `{ok: true, id}` | 200 / 500 | 同上 |
| GET | `/api/llm-models/:id` | 查单个模型 | — | `{model: Model}` | 200 / 404 / 500 | 同上 |
| PATCH | `/api/llm-models/:id` | 更新模型（仅更新提供的字段） | 同 create 字段子集 | `{ok: true}` | 200 / 404 / 500 | 同上 |
| PUT | `/api/llm-models/:id` | 同 PATCH | 同上 | 同上 | 同上 | 同上 |
| DELETE | `/api/llm-models/:id` | 删除模型 | — | `{ok: true}` | 200 / 404 / 500 | 同上 |
| POST | `/api/llm-models/:id/test` | 探测请求（见"特别流程 5"） | — | 成功：`{ok: true, latencyMs, response}`；失败：`{ok: false, latencyMs, error}` | 200 / 404 / 500 | ModelService + 外部 HTTP 探测 |

### 10. Extra — Templates（3 端点）— Agent 模板库

| 方法 | 路径 | 功能 | 请求体 | 响应 | 状态码 | 对应服务契约 |
|---|---|---|---|---|---|---|
| GET | `/api/agent-templates` | 列出模板（query: `division?`、`role?` 过滤） | — | `{templates: [Template]}` | 200 | TemplateService（meta DB `agent_templates` 表） |
| GET | `/api/agent-templates/divisions` | 列出所有部门（去重排序） | — | `{divisions: [string]}` | 200 | 同上 |
| GET | `/api/agent-templates/:id` | 查单个模板 | — | `{template: Template}` | 200 / 404 | 同上 |

### 11. Extra — Communications（2 端点）— 跨 agent 通信

| 方法 | 路径 | 功能 | 请求体 | 响应 | 状态码 | 对应服务契约 |
|---|---|---|---|---|---|---|
| GET | `/api/communications` | 列出通信（per-project `inbox` 表，query: `limit?` 默认 50、`projectId?`） | — | `[Comm]`（数组直返） | 200 | 契约 6（InboxService） |
| POST | `/api/communications` | 创建通信 | `{toAgentId, fromAgentId?, type?, content?, subject?, priority?, metadata?}` | `{ok: true, message}` | 200 / 500 | InboxService.send_message |

### 12. Extra — UserPings（2 端点）— 用户通知

| 方法 | 路径 | 功能 | 请求体 | 响应 | 状态码 | 对应服务契约 |
|---|---|---|---|---|---|---|
| GET | `/api/user-pings` | 列出用户 ping（per-project `agent_events` 表 event_type=`user_ping`，query: `limit?` 默认 50、`unreadOnly?`） | — | `[Ping]`（数组直返） | 200 | CommunicationService（agent_events） |
| POST | `/api/user-pings/:id/read` | 标记 ping 已读 | — | `{ok: true}` | 200 | 同上（当前实现为 no-op，预留） |

### 13. Extra — Alarms（3 端点）— 游戏时间闹钟

| 方法 | 路径 | 功能 | 请求体 | 响应 | 状态码 | 对应服务契约 |
|---|---|---|---|---|---|---|
| GET | `/api/projects/:project_id/alarms` | 列出项目闹钟（query: `includeFired?`） | — | `{alarms: [Alarm], currentGameSeconds, realTimestamp}` | 200 | 契约 7（AlarmService，per-project `scheduled_alarms` 表） |
| POST | `/api/projects/:project_id/alarms` | 创建闹钟 | `{fromAgentId?, toAgentId?, purpose?, fireAtGameSeconds}` | `{ok: true, alarm: Alarm}` | 200 / 400 / 500 | GameTime.Server.schedule_alarm |
| DELETE | `/api/projects/:project_id/alarms/:id` | 取消闹钟 | — | `{ok: true}` | 200 / 400 | GameTime.Server.cancel_alarm |

### 14. Extra — WorkLogs（2 端点）— 工作日志

| 方法 | 路径 | 功能 | 请求体 | 响应 | 状态码 | 对应服务契约 |
|---|---|---|---|---|---|---|
| GET | `/api/logs/:agentId` | 查 agent 工作日志（query: `limit?` 默认 50） | — | `{logs: [WorkLog]}` | 200 | ProjectFactory（per-project `work_logs` 表） |
| GET | `/api/logs/:agentId/subordinates` | 查下属 agent 工作日志（每个 10 条） | — | `{agentId, subordinates: {<child_id>: {name, role, logs}}}` | 200 / 404 | 契约 4 + DispatchService.get_agent_logs |

### 15. Extra — Debug（1 端点）— 调试追踪

| 方法 | 路径 | 功能 | 请求体 | 响应 | 状态码 | 对应服务契约 |
|---|---|---|---|---|---|---|
| GET | `/api/debug/agents/:agentId/traces` | 查 agent LLM 追踪（conversation_turns + agent_events 中 llm_round/chat_done/chat_start/llm_fail） | — | `{turns: [Turn], events: [Event]}` | 200 | ProjectFactory（per-project DB） |

### 16. Extra — Filesystem（1 端点）— 文件浏览

| 方法 | 路径 | 功能 | 请求体 | 响应 | 状态码 | 对应服务契约 |
|---|---|---|---|---|---|---|
| GET | `/api/fs/browse` | 浏览文件系统（见"特别流程 6"），query: `path?`（省略则用用户主目录） | — | `{currentPath, parentPath, entries: [{name, path, fullPath, isDir, size, modified}], drives, isRoot}` 或错误时 `{error, path, parent, entries: []}` | 200 | FileService（含 blocklist + Windows 盘符逻辑） |

## 特别流程

### 1. POST /api/chat 专家命令路由

```
1. 解析 message：匹配正则 ^/(review|test|audit|perf)\s+(.+)$（大小写不敏感）
2. 命令映射：review→code_reviewer，test→test_engineer，audit→security_auditor，perf→web_perf_auditor
3. 在 agent 同项目内按 role 查找专家 agent
4. 若未找到专家 → 退回普通处理
5. 若专家未启动 → 启动后路由消息，响应 {ok: true, routed: true, expert, started: true}
6. 若专家已启动且空闲 → 直接路由，响应 {ok: true, routed: true, expert}
7. 若专家 processing → 投递到专家 inbox（队列），向原 agent 回一条"已排队"assistant 消息，响应 {ok: true, queued: true, expert}
8. 专家路由后 conn.state=sent → 跳过普通处理
```

### 2. POST /api/chat busy 重试逻辑

```
1. 调用 Agent.chat(pid, message, images)
2. 返回 {:error, :busy} → 发送 {:force_reset} 信号给 agent 进程
3. sleep 500ms
4. 再次调用 Agent.chat
5. 成功 → 响应 {ok: true, userMessageId, reset: true}
6. 仍 busy → 409 {error: "Agent is busy after reset"}
```

### 3. PUT /api/projects/:id/workspace 迁移逻辑

```
1. new_path 为空 → 停 ProjectSupervisor、evict DB pool、清空 workspace_path
2. new_path == 旧路径 → no-op 返回当前 project
3. new_path 变更：
   a. 校验新路径非空字符串且是目录（否则 400）
   b. 停 ProjectSupervisor、evict 旧 DB pool
   c. sleep 500ms 等 OS 释放 SQLite 句柄
   d. 旧 .hiveweave/ 存在 → cp_r 到新位置，删除旧目录
   e. 更新 project.workspace_path
   f. 同步 UPDATE agents.workspace_path（防止 DB 恢复时开错 workspace）
   g. 重启 ProjectSupervisor（重新注册 agents）
```

### 4. DELETE /api/projects/:id 清理逻辑

```
1. stop_project_bounded(3s) — 终止 ProjectSupervisor（含 agent GenServers + 在飞 LLM Task）
2. stop_repo_bounded(5s) — ProjectFactory 标记 deleting，杀连接池：
   a. 找到池对应的 DBConnection.ConnectionPool.Pool supervisor
   b. 逐个 :sys.terminate 连接进程（3s 超时），失败则 Process.exit(:kill)
   c. 终止池 supervisor
   d. force_global_gc — 多趟 GC（8 趟，每趟遍历所有进程 + sleep 300ms）回收 NIF 资源（sqlite3_finalize → sqlite3_close_v2）
3. meta DB 删除该 project 的所有 agents
4. meta DB 删除 project 记录
5. cleanup_project_workspace(workspace_path)：
   a. kill_workspace_dev_servers — PowerShell 杀掉命令行引用 workspace 路径的 node/esbuild/next-server/vite 进程
   b. cleanup_git_worktrees — git worktree list/remove + branch -D hw/* + worktree prune
   c. sleep 1s
   d. force_remove_dir(.hiveweave) 重试 5 次（每次失败 sleep 3s + GC）
   e. 失败 → 尝试重命名为 <dir>._hw_del_<ts>，spawn 异步清理（30 次 × 3s）
   f. Windows 优先用 cmd /c rd /s /q（路径 \ 标准化），并验证目录确实消失（rd 返回 0 但目录仍在的"pending delete"陷阱）
6. clear_deleting 标志位（允许重建）
7. 任何步骤异常 → 仍返回 200 {ok: true, dbLeftover: true, warning: "..."}，保证前端能从列表移除
```

### 5. POST /api/llm-models/:id/test 探测请求

```
1. 加载模型，base_url 去尾斜杠
2. POST {base_url}/chat/completions
   body: {model: model.model_id, messages: [{role:"user", content:"Say 'OK' and nothing else."}], max_tokens: 10}
   headers: content-type: application/json, authorization: Bearer {api_key}
   receive_timeout: 15_000ms
3. :timer.tc 测延迟
4. 200 → 解析 choices[0].message.content 作为 response_text，返回 {ok: true, latencyMs, response}
5. 非 200 → {ok: false, latencyMs, error: "HTTP {status}"}
6. 网络错误 → {ok: false, latencyMs, error: <reason>}
7. 模型未找到 → 404
8. 异常 → {ok: false, latencyMs: 0, error: <message>}
```

### 6. GET /api/fs/browse blocklist + Windows 盘符逻辑

```
1. path 省略 → 用 System.user_home() 或 "C:\\"
2. sanitize_browse_path:
   a. Path.expand + 反斜杠转正斜杠
   b. blocklist（前缀匹配，大小写不敏感）：/etc/passwd, /etc/shadow, /root/, /var/run/, /proc/, /sys/, /Windows/System32/config/, /Windows/System32/drivers/etc/, C:/Windows/System32/config/, C:/Windows/System32/drivers/etc/
   c. 命中 → 抛 "Access denied to system path"
3. 路径不存在或是文件 → 返回空 entries（isRoot=true，含 drives）
4. 列目录：File.ls + 每项 stat，返回 name/path/fullPath/isDir/size/modified(ISO)
5. drives 字段：
   - Windows：枚举 C..Z，File.exists? 检测，返回存在的盘符列表（如 ["C:\\","D:\\"]）
   - 非 Windows：返回 ["/"]
6. parentPath：Path.dirname，根目录时为 nil
7. isRoot：parent == path
```

## 核心流程

### 请求处理通用流程

```
1. Phoenix Router 接收请求，匹配 scope（"/" 或 "/api"）
2. /api scope 走 :api pipeline：
   a. accepts ["json"]
   b. ApiKeyAuth plug：免认证路径放行；否则校验 HIVEWEAVE_API_KEY（未设置则放行）
   c. CORSPlug：注入 CORS 头
3. 路由到对应 controller action
4. action 调用 service（Org/ChatMessage/Inbox/ProjectFactory 等）
5. 序列化结果（同时输出 snake_case + camelCase）
6. json(conn, payload) 返回 JSON
7. 异常 → try/rescue 返回 {error: ...} 或空数组/空对象（容错降级）
```

### Project auto-boot 流程

```
1. GET /api/projects/:id 命中项目但未运行（Registry 查无）
2. 调用 ProjectSupervisor.start_project(project.id, workspace_path || "")
3. 已启动（{:already_started, _}）→ 视为成功
4. 失败 → IO.warn，但不影响返回（仍返回 project 详情）
```

## 错误处理

| 错误场景 | 处理方式 | 重试策略 | 升级策略 |
|---|---|---|---|
| ApiKeyAuth 失败 | 401 `{error: "Unauthorized — invalid or missing API key"}` + halt | 无 | 无 |
| 资源未找到 | 404 `{error: "Not found"}` / `{error: "Agent not found"}` / `{error: "Model not found"}` / `{error: "Template not found"}` | 无 | 无 |
| 参数校验失败 | 422 `{errors: {field: [msg]}}`（Ecto changeset） | 无 | 无 |
| Agent busy（重置后仍 busy） | 409 `{error: "Agent is busy after reset"}` | force_reset 后重试 1 次 | 无 |
| 内部错误 | 500 `{error: "Failed to ..."}` | 无 | 日志记录 |
| Project 删除清理失败 | 仍返回 200 `{ok: true, dbLeftover: true, warning: "..."}` | 异步清理 30 次 × 3s | 日志 + clear_deleting |
| per-project DB 查询失败（如项目未启动） | try/rescue 返回空数组或空对象 | 无 | 无 |
| 文件浏览路径被 blocklist | 返回 200 + `{error, path, parent: nil, entries: []}` | 无 | 无 |
| LLM 模型探测失败 | 返回 200 + `{ok: false, latencyMs, error}` | 无 | 无 |

## 常量引用

| 常量 | 值 | 所在章节 |
|---|---|---|
| PORT | `4000` | constants.md → 环境变量 |
| HIVEWEAVE_API_KEY | 未设置（dev 放行） | 本契约 |
| CORS origins | `localhost:5173, 3200, 4000` | 本契约 |
| 免认证路径 | `GET /`, `GET /api/health` | 本契约 |
| 历史消息上限 | `200`（chat history / chat_messages） | 本契约 |
| 子节点工作日志条数 | `10` | 本契约 |
| recentActivity 缓冲 | `100` | 契约 12 |
| LLM 探测超时 | `15_000` ms | 本契约 |
| force_reset 后 sleep | `500` ms | 本契约 |
| Project 删除 GC 趟数 | `8` | 本契约 |
| Project 删除 force_remove_dir 重试 | `5` 次 × 3s | 本契约 |
| 异步清理重试 | `30` 次 × 3s | 本契约 |
| blocklist 条目 | `10` 条（/etc/passwd, /etc/shadow, /root/, /var/run/, /proc/, /sys/, /Windows/System32/config/, /Windows/System32/drivers/etc/, C:/Windows/System32/config/, C:/Windows/System32/drivers/etc/） | 本契约 |

## 已知问题

| 问题编号 | 说明 | Python 迁移处理 |
|---|---|---|
| — | Elixir 用 Exqlite NIF + DBConnection 连接池，删除项目时需杀连接 + GC 回收 NIF 资源才能释放 Windows 文件句柄 | Python 用 aiosqlite（单连接），删除时直接 close 连接即可；无需 GC 趟数，但仍需重试文件删除以应对 Windows 句柄释放延迟 |
| — | Elixir 同时输出 snake_case + camelCase 字段（前端兼容） | Python 保留双字段输出（Pydantic 模型可同时声明 alias） |
| — | `chat_questions_answer` 路由含 `:id` 段但 `chat_questions_index` 无 | Python 显式区分 `/api/chat/questions` 与 `/api/chat/questions/{id}/answer` |
| — | `user_ping_read` 当前为 no-op | Python 可实现真实已读标记（如 agent_events 加 read 字段或单独表） |
| — | `POST /api/communications` 错误信息为 "Failed to create question"（复制粘贴遗留） | Python 修正为 "Failed to create communication" |
| — | `llm_model_update` 的 sets 列表实现 bug（每次 `[{sets, ...}]` 嵌套而非追加） | Python 用 dict 累积 set 子句，避免该 bug |
| — | per-project DB 查询通过 `ProjectFactory.query_for_agent(agent_id, ...)` 路由，依赖 agent 解析 project | Python 显式查 `agents.project_id` 后开 DB |
| — | Windows `rd /s /q` 路径需 `\` 标准化，否则 `/.hiveweave` 被当作 switch | Python 用 `shutil.rmtree` 或 `subprocess.run(["cmd","/c","rd","/s","/q",path])` 时同样标准化 |

## Python 实现建议

- **框架**：FastAPI + Pydantic v2（schema 校验 + alias 同时输出 snake_case/camelCase）
- **路由组织**：APIRouter 按 controller 分组（router_health、router_settings、router_projects、router_org、router_chat、router_permissions、router_extra），main app `include_router(prefix="/api")`
- **认证**：`api_key = Depends(verify_api_key)` 作为 FastAPI dependency；免认证路径用 `dependencies=[]` 跳过；用 `secrets.compare_digest` 防时序攻击
- **CORS**：`CORSMiddleware`，allow_origins=`["http://localhost:5173","http://localhost:3200","http://localhost:4000"]`
- **错误处理**：全局异常处理器 `@app.exception_handler`，统一返回 `{error: ...}`；404/422/409/500 用 `HTTPException(status_code, detail)` 或自定义异常
- **删除项目清理**：用 `asyncio` + `subprocess` 杀进程；`shutil.rmtree` 删目录；Windows 下 `rd /s /q` 备选；aiosqlite close 后文件句柄立即释放，无需 GC 趟数
- **LLM 探测**：用 `httpx.AsyncClient` POST，`timeout=15.0`，`time.perf_counter()` 测延迟
- **文件浏览**：`pathlib.Path` + `os.stat`；blocklist 用 `Path.resolve()` 后前缀匹配；Windows 盘符用 `string.ascii_uppercase` 枚举
- **per-project DB 路由**：维护 `dict[project_id, aiosqlite.Connection]` 缓存；查询前先 `SELECT project_id FROM agents WHERE id=?` 解析
- **响应序列化**：Pydantic 模型用 `model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)` 同时输出双字段
- **HTML 落地页**：`@app.get("/", response_class=HTMLResponse)` 返回静态 HTML
- **健康检查**：`@app.get("/api/health")` 不加 `Depends(verify_api_key)`

## 验收标准

- [ ] 68 个路由条目全部实现，方法/路径/响应格式与本契约一致（去重 PATCH/PUT 别名后 63 个唯一端点）
- [ ] `GET /` 返回 HTML 落地页
- [ ] `GET /api/health` 免认证返回 `{status:"ok", version:"0.2.0", timestamp}`
- [ ] ApiKeyAuth：`HIVEWEAVE_API_KEY` 未设置时放行；设置后必须传 Bearer / x-api-key / api_key 三选一；错误返回 401
- [ ] CORS 头正确注入三个 localhost origin
- [ ] 所有响应同时包含 snake_case 与 camelCase 字段
- [ ] POST /api/projects 自动创建 CEO+HR+QA 并返回 mainAgentId
- [ ] GET /api/projects/:id 未运行时自动 boot
- [ ] DELETE /api/projects/:id 完整清理（supervisor/连接池/agents 记录/project 记录/.hiveweave 目录/git worktrees），失败仍返回 200 + dbLeftover
- [ ] PUT /api/projects/:id/workspace 三分支逻辑（清空/相同/迁移）正确
- [ ] POST /api/chat 专家命令路由（/review /test /audit /perf）正确
- [ ] POST /api/chat busy 重试逻辑（force_reset + sleep 500ms + 重试 1 次）正确
- [ ] POST /api/chat/reset-processing/:agentId 广播 idle 状态
- [ ] POST /api/llm-models/:id/test 探测请求 15s 超时 + 延迟测量 + 200/非 200/异常三分支
- [ ] GET /api/fs/browse blocklist 命中返回错误；Windows 盘符枚举正确；省略 path 用主目录
- [ ] 错误格式统一 `{error: "..."}`；404/422/409/500 状态码正确
- [ ] per-project DB 查询通过 agent_id 解析 project_id 后路由
- [ ] 异常容错降级（返回空数组/空对象而非 500）

## 并行对比测试方案

| 测试场景 | Elixir 行为 | Python 预期行为 | 验证方法 |
|---|---|---|---|
| GET /api/health | `{status:"ok", version:"0.2.0", timestamp}` | 同（timestamp 不同但字段一致） | curl 对比 |
| POST /api/projects | 创建项目 + CEO/HR/QA，返回 project + mainAgentId | 同 | curl 对比，检查 meta DB agents 表 |
| DELETE /api/projects/:id | 清理后 .hiveweave/ 消失，dbLeftover 反映清理结果 | 同（aiosqlite close 后无需 GC） | 删除后检查工作空间目录 |
| POST /api/chat /review <module> | 路由到 code_reviewer，返回 routed:true | 同 | mock 专家 agent，对比响应 |
| POST /api/chat busy | force_reset 后重试，返回 reset:true 或 409 | 同 | mock Agent.chat 返回 busy |
| PUT /api/projects/:id/workspace 迁移 | .hiveweave/ 从旧路径 cp 到新路径，agents.workspace_path 同步 | 同 | 迁移后检查两路径 |
| POST /api/llm-models/:id/test | 探测请求，返回 latencyMs + response | 同 | mock httpx 响应 |
| GET /api/fs/browse blocklist | 命中返回错误 entries:[] | 同 | 请求 /etc/passwd |
| GET /api/fs/browse Windows drives | 返回存在盘符列表 | 同 | 在 Windows 上请求 |
| POST /api/permissions/respond | 更新 status + 通知 ApprovalService | 同 | 对比 meta DB permission_requests |
| 字段命名 | snake_case + camelCase 并存 | 同 | 任意 GET 端点对比响应字段 |
| ApiKeyAuth | 无 key 返回 401 | 同 | 设 HIVEWEAVE_API_KEY 后 curl 不带 key |

---

> **填写规则**：
> 1. 用 spec 语言，不用代码语言。说"做什么"，不说"怎么做"。
> 2. 所有常量值引用 `constants.md`，不在此处重复定义。
> 3. 所有已知问题引用 `known-issues.md`，不在此处重复描述。
> 4. 每个契约必须经过用户确认后才能标记为"已确认"。
> 5. "Python 实现建议"是建议不是约束，实现者可以根据情况调整，但必须满足验收标准。
