# Elixir 后端改造现状审计

## 概要

另一个 AI 已经在 `apps/hiveweave/` 创建了完整的 Phoenix 项目骨架，核心基础设施（监督树、Agent GenServer、Circuit Breaker、LLM Streamer、19 个 Schema、3 个 Channel、前端 api.ts 迁移）均已落地，且 Rev 2 计划中的 3 个 Kairo 预留接口也已实现。**但 BEAM 运行时未安装，代码无法运行**，且存在 6 个阻塞性 bug 和多个未实现模块。

## 已完成清单

### 架构层（完成度高）
- `application.ex` — 完整监督树：Telemetry → MetaRepo → Endpoint → PubSub → Presence → TaskSupervisor → Finch → CircuitBreaker → EventAudit → ProjectSupervisor，`rest_for_one` 策略 + 启动后异步 boot 已有项目
- `project_supervisor.ex` — DynamicSupervisor 管理 per-project 子监督树（AgentSupervisor + GameTime）
- `agents/agent.ex` — Agent GenServer，含 Kairo 预留字段（position/target/face/action），状态机 idle→processing→idle，Task.Supervisor 隔离 LLM 调用，`:DOWN` 监听崩溃恢复
- `agents/agent_supervisor.ex` — per-project DynamicSupervisor
- `agents/agent_registry.ex` — Registry 模块

### LLM 层（完成度高）
- `llm/streamer.ex` — Finch + 手动 SSE buffer 解析（parse_sse/extract_data/sse_to_chunk），CircuitBreaker 集成，180s 超时，支持 reasoning_content
- `llm/circuit_breaker.ex` — 三态机 + **probe_owner 锁已实现**（Rev 2 修复落地），fallback 链（primary → fallback → all_down）
- `llm/provider_factory.ex` — provider 映射
- `llm/retry.ex` — 重试逻辑

### 数据层（完成度中）
- `repo/meta.ex` — Meta DB Ecto Repo（WAL mode，复用现有 `packages/db/data/hiveweave.db`）
- `repo/project_factory.ex` — per-project Repo 工厂（骨架，见 bug #4）
- 19 个 Schema：Agent, AgentCharter, AgentEvent, AgentTemplate, ChatMessage, ConversationTurn, GlobalSetting, Handoff, Inbox, LlmModel, Memory, Merge, MetaIndex, Module, PermissionRequest, PersonnelRecord, Project, ProjectIndex, ScheduledAlarm, WorkLog

### Web 层（完成度高）
- `router.ex` — 完整复制 TS 版所有 API 路由（projects/org/chat/permissions/llm-models/templates/communications/alarms/logs/fs/health）
- 3 个 Channel：`lobby_channel`（全局状态）、`agent_channel`（聊天流 + 状态 + 收件箱）、`project_channel`（游戏时间 + 状态）
- `agent_channel.ex` — 关键细节正确：subscribe BEFORE chat、stream_chunk/done/error 事件翻译、status_change 广播
- `presence.ex` — Phoenix Presence，map 格式 metadata（Kairo 兼容）
- 7 个 Controller：chat/extra/health/org/permissions/projects/root/settings

### 前端集成（已启动）
- `api.ts` — 已从 SSE 迁移到 `phoenix.js` Socket + Channel，ws://localhost:4000/socket，含 reconnect 策略和心跳
- ChatPanel 仍用旧事件 shape，api.ts 做翻译层

### Kairo 兼容（3/3 完成）
1. Agent state 预留 position/target/face/action — ✅
2. Presence metadata 用 map — ✅
3. Inbox 双模投递（deliver_sync stub）— ✅

### 测试（7 个文件）
- circuit_breaker_test（6 个 test）
- provider_factory_test, retry_test
- agent_test, project_test（schema）
- org_test（service）
- token_utils_test

## 阻塞性 Bug（6 个，必须修复才能运行）

### Bug 1: BEAM 运行时未安装
- **现象**：`erl` 命令不存在；`erl_crash.dump` 显示 `cannot get bootfile 'C:\Users\99744\otp26/bin/start.boot'`
- **原因**：系统没有安装 Erlang/OTP，或安装后未加入 PATH
- **修复**：安装 Erlang/OTP 26+ 和 Elixir，或使用 Elixir 官方 Windows installer（含 bundled OTP）

### Bug 2: ProjectSupervisor.spawn_agents 缩进/格式错误
- **位置**：`project_supervisor.ex:84-98`
- **现象**：`case HiveWeave.Agents.AgentSupervisor.start_agent(...)` 块的缩进错乱，`_pid ->` 分支嵌套在错误的位置
- **影响**：编译可能通过但逻辑错误——已启动的 Agent 不会跳过，可能重复启动
- **修复**：重新格式化 spawn_agents/1 函数，确保 `case Process.whereis(name)` 的两个分支正确嵌套

### Bug 3: ProjectRegistry 未创建
- **位置**：`project_supervisor.ex:112` — `Registry.lookup(HiveWeave.ProjectRegistry, project_id)`
- **现象**：`stop_project/1` 引用了 `HiveWeave.ProjectRegistry`，但 Application children 中没有启动这个 Registry
- **影响**：`stop_project` 永远返回 `{:error, :not_found}`；项目无法正确停止
- **修复**：在 Application children 中添加 `{Registry, keys: :unique, name: HiveWeave.ProjectRegistry}`，并在 `start_project_children/2` 中注册 `{:via, Registry, {HiveWeave.ProjectRegistry, project_id}}`

### Bug 4: ProjectFactory 是空壳
- **位置**：`repo/project_factory.ex`
- **现象**：`init_meta_tables` 是 no-op；`get_repo` 只查内存 Map，没有实际创建 Ecto Repo 的逻辑；没有 `start_project_repo` 实现
- **影响**：per-project 数据库无法使用，所有 per-project 查询（inbox/chat_messages/agents 等）无处可去
- **修复**：实现动态 Repo 启动——为每个 project 创建 Ecto Repo 进程，journal_mode=DELETE via after_connect，挂在 ProjectSupervisor 下

### Bug 5: Inbox 查询用错了数据库
- **位置**：`services/inbox.ex:33` — `Ecto.Adapters.SQL.query(Meta, sql, ...)`
- **现象**：Inbox 是 per-project 表，但代码用 Meta DB（全局库）查询
- **影响**：查询会失败（Meta DB 没有 inbox 表）或查到错误数据
- **修复**：改为通过 ProjectFactory 获取 project repo，用 project repo 查询

### Bug 6: Circuit Breaker probe 锁无超时释放
- **位置**：`llm/circuit_breaker.ex:76` — `probe_owner: self()`
- **现象**：probe_owner 设为 CircuitBreaker 自身 PID（不是调用方 Agent PID），且没有 `Process.monitor` 监视探测方 Agent
- **影响**：如果探测 Agent 在 LLM 调用中崩溃且未调用 report_success/report_failure，probe_owner 永远不为 nil，Circuit 卡在 half_open
- **修复**：记录调用方 PID（需从 `handle_call` 的 `_from` 提取），`Process.monitor` 调用方，`:DOWN` 时清除 probe_owner

## 未实现模块（v1.5 计划要求但未做）

| 模块 | 计划章节 | 状态 |
|------|---------|------|
| 工具执行器 + 7 个工具 | 第 5 章 5.3.1 | 完全未开始 |
| 记忆服务 | 第 5 章 5.3 | 完全未开始 |
| 交接服务 | 第 5 章 5.3 | 完全未开始 |
| 审批服务 | 第 5 章 5.3 | 完全未开始 |
| 人事档案服务 | — | 完全未开始 |
| GameTime 闹钟触发 | 第 5 章 5.3 | Server 存在但无闹钟逻辑 |
| Worktree 服务 | — | 完全未开始 |
| MCP 集成 | — | 完全未开始 |
| LiveView (ChatPanel/GoalsPanel) | 第 5 章 5.4.1 | 完全未开始 |
| Office Channel + slot | 第 3 章 3.3 | 完全未开始 |
| 热代码升级框架 | 第 11 章 | 完全未开始 |
| Electron + BEAM 打包 Spike | 第 2 章 | 完全未开始 |
| ETS 注册表/缓存 | 第 10 章 10.1 | 完全未开始 |
| Supervisor 死亡策略 (CrashCounter) | 第 9 章 | 完全未开始 |
| Telemetry 文件 reporter | 第 7 章 | Telemetry 模块存在但无 reporter |

## 评估结论

**完成度约 35%**——骨架搭建质量高（架构正确、Kairo 兼容、前端已迁移），但 6 个阻塞性 bug 导致代码无法运行，且核心业务模块（工具/记忆/审批/交接/闹钟）完全未实现。

**建议的下一步优先级：**

1. **P0：安装 BEAM 运行时** — 不装 Erlang/Elixir 一切都是空谈
2. **P0：修复 6 个阻塞性 bug** — 让后端能编译能启动
3. **P0：实现 ProjectFactory 动态 Repo** — per-project 数据层是所有业务逻辑的基础
4. **P1：实现工具执行器** — Agent 没有工具就是纯聊天机器人
5. **P1：实现 GameTime 闹钟** — 当前 TS 版的核心调度机制
6. **P1：补全 Inbox/ChatMessage 的 project-db 路由** — 修复 Bug 5 的延伸
7. **P2：LiveView + Office Channel** — Pixel Office 活过来的前端
8. **P2：ETS + Supervisor 死亡策略** — 生产强化

## 验证步骤

修复完成后按以下顺序验证：

1. `elixir --version` 确认 BEAM 运行时安装
2. `cd apps/hiveweave && mix deps.get && mix compile` 确认编译通过
3. `mix test` 确认 38 个测试通过
4. `mix phx.server` 确认 Phoenix 启动，端口 4000 可访问
5. 前端 `pnpm -C apps/web dev` 确认 WebSocket 连接到 localhost:4000
6. 发送一条聊天消息，确认 Agent 回复
7. `Process.exit(pid, :kill)` 确认 Agent 自动恢复
8. 手动触发 3 次 LLM 失败，确认 Circuit Breaker 打开 + fallback
