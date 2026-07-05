# 功能契约 11：两层 SQLite

> **本文件是功能契约（层 1），用 spec 语言描述模块的行为契约。**

## 元信息

| 项 | 值 |
|---|---|
| 模块编号 | 11 |
| 模块名称 | 两层 SQLite |
| Elixir 源码 | `repo/meta.ex` + `repo/project_factory.ex` |
| TS 参考源码 | `packages/db/src/client.ts` + `packages/db/src/schema/` |
| OpenCode 参考源码 | `D:\PC_AI\Project\opencode\packages\core\src\database\database.ts` |
| 状态 | 草稿 |

## 功能概述

两层 SQLite 数据库架构：(1) **Meta DB** — 全局单例，存项目元信息、agent 模板、LLM 模型、全局设置、权限规则；(2) **Per-project DB** — 每个项目一个，存 agent、记忆、聊天消息、交接、收件箱等项目级数据。Per-project DB 用 DELETE journal mode（避免 Windows WAL 文件问题）。懒加载，内存缓存。

## 接口契约

### Meta DB

| 函数 | 参数 | 返回值 | 说明 |
|---|---|---|---|
| `Meta.query` | `(sql, params)` | `{:ok, result}` | 全局表查询 |
| `Meta.query!` | `(sql, params)` | `result` | 同上（抛异常） |

### Per-project DB

| 函数 | 参数 | 返回值 | 说明 |
|---|---|---|---|
| `ProjectFactory.ensure_project_db` | `(workspace_path)` | `:ok` | 懒创建 per-project DB |
| `ProjectFactory.query` | `(project_id, sql, params)` | `{:ok, result}` | 项目级查询 |
| `ProjectFactory.query_for_agent` | `(agent_id, sql, params)` | `{:ok, result}` | 通过 agent_id 反查 project_id |
| `ProjectFactory.evict_project_db` | `(project_id)` | `:ok` | 从缓存中移除 |
| `ProjectFactory.get_project_db_for_agent` | `(agent_id)` | `db` | 获取 agent 对应的 DB 连接 |

## 数据模型

### Meta DB 表

| 表 | 说明 |
|---|---|
| `projects` | 项目元信息（id, name, workspace_path, language, game_time_acc, goals_json） |
| `agents` | **全局 agent 注册表**（含 project_id, workspace_path 冗余字段；agent→project 路由依赖此表） |
| `agent_templates` | Agent 模板 |
| `llm_models` | LLM 模型注册 |
| `global_settings` | 全局键值设置 |
| `permission_rules` | 权限规则 |
| `permission_requests` | 审批请求 |
| `mcp_servers` | MCP 服务器配置 |

> **RECONCILE — Meta DB 缺 agents 表（有效可操作，关键事实错误）**：契约原未列 `agents` 表。
> 源码 `ProjectFactory.resolve_project/1`（project_factory.ex:243）通过 `Meta.one(from a in Agent,
> where: a.id == ^agent_id, select: a.project_id)` 做 agent→project 路由；`Application` 启动时
> 也从 Meta 的 agents 表恢复项目。**Elixir 的 agents 表在 Meta DB（全局），不在 per-project DB** —— 这
> 与 TS 参考实现（agents 在 per-project DB）是**关键架构差异**，Python 迁移须对齐 Elixir。

### Per-project DB 表

| 表 | 说明 |
|---|---|
| `memories` | 三层记忆 |
| `chat_messages` | 聊天消息 |
| `conversation_turns` | 对话历史（compaction 用） |
| `handoffs` | 任务交接 |
| `inbox` | 收件箱 |
| `work_logs` | 工作日志 |
| `scheduled_alarms` | 闹钟 |
| `game_time_state` | 游戏时间状态 |
| `agent_events` | Agent 事件审计 |
| `personnel_records` | 人事记录 |
| `agent_charters` | Agent 章程 |
| `questions` | 向用户提问 |
| `todos` | Agent 待办 |
| `permission_requests` | 工具权限审批（项目级副本） |
| `team_chat_dedupe` | 团队聊天去重 |
| `modules` | 模块登记 |

> **RECONCILE**：契约原把 `agents`、`merges` 列在 per-project 表，实际源码 `init_project_tables`
> 并不创建这两张表（agents 在 Meta DB；merges 在源码中不存在）。已移除 `agents`/`merges`，补全
> `questions`/`todos`/`team_chat_dedupe`/`modules`/`permission_requests`。

## 核心流程

### Per-project DB 创建

> **RECONCILE — `project.db` 应为 `data.db`（有效可操作，关键事实错误）**：契约原写
> `project.db`，但源码 `config.exs:30` 配置 `project_db_filename: "data.db"`，
> `project_factory.ex:318` 实际路径为 `<workspace>/.hiveweave/data.db`。沿用 `project.db` 会导致
> Python 实现无法读取既有 Elixir 创建的数据库文件。已全部改为 `data.db`。

```
1. ensure_project_db(workspace_path) / ensure_repo(project_id):
   a. DB 路径 = workspace_path + "/.hiveweave/data.db"
   b. workspace_path 优先取 projects 表，回退取 agents 表的冗余 workspace_path 字段
   c. 如果已缓存池且存活 → 返回缓存
   d. mkdir_p 创建 .hiveweave 目录
   e. 启动 DBConnection + Exqlite.Connection 池（pool_size=5）
   f. 设置 PRAGMA：
      - journal_mode = DELETE（避免 Windows WAL 文件）
      - busy_timeout = 5000
   g. 执行建表 SQL（IF NOT EXISTS）+ 运行时 ALTER 迁移
   h. 缓存池 pid
   i. 返回 {:ok, pool}
```

### 并发模型

> **RECONCILE — 单连接并发模型未描述（契约误读 + 有效可操作）**：契约原"常量引用"写
> "Per-project DB 连接池 | 单连接"，但**源码实际用 `pool_size: 5`**（project_factory.ex:332），
> 并非单连接。已知问题 C3 已承认"Elixir pool_size=5 vs TS 单连接"，Python 迁移目标才是单连接。
> 此处澄清两者差异并描述 Python 单连接并发模型。

- **Elixir 源码**：每项目一个 DBConnection 池，`pool_size: 5`，journal_mode=DELETE，
  busy_timeout=5000ms。并发查询由池分发到 5 个连接；遇 SQLITE_BUSY 会等待 busy_timeout。
- **Python 迁移目标**：`aiosqlite` 单连接（对齐 OpenCode/TS），并发请求由 asyncio 事件循环
  **序列化**执行（sqlite 单写者）。`busy_timeout=5000` 防止偶发锁竞争时的立即失败。单连接足以
  支撑单进程内的 agent 并发（agent 数通常 < 20，查询短），且避免 Windows WAL 文件问题。

### Agent 到 DB 路由

```
1. resolve_project(agent_id)（GenServer call）:
   a. 从 agent_cache（内存 dict）查 agent_id → project_id
   b. 未命中 → 查 Meta DB: SELECT project_id FROM agents WHERE id=?
   c. 缓存映射（agent_id → project_id）
2. query_for_agent(agent_id, sql, params):
   a. resolve_project 取 project_id
   b. with_project_recovery：遇连接错误 evict 池后重试一次
   c. ensure_repo(project_id) 取/建池
   d. run_query(pool, sql, params)
```

## 常量引用

| 常量 | 值 | 所在章节 |
|---|---|---|
| Meta DB 默认路径 | `packages/db/data/hiveweave.db` | 数据库 |
| `HIVEWEAVE_META_DB_PATH` | 覆盖 meta DB 路径（Elixir）；TS 用 `HIVEWEAVE_DB_PATH` | 环境变量 |
| Meta DB journal_mode | `WAL`（config.exs） | 数据库 |
| Per-project DB 路径 | `<workspace>/.hiveweave/data.db` | — |
| Per-project journal_mode | `DELETE` | 数据库 |
| busy_timeout | `5000` ms | 数据库 |
| Elixir per-project 池大小 | `pool_size: 5`（DBConnection） | 数据库（源码实际值） |
| Python per-project 池大小 | 单连接（aiosqlite，迁移目标） | 数据库（迁移目标） |

## 已知问题

| 问题编号 | 说明 | Python 迁移处理 |
|---|---|---|
| C3 | Elixir pool_size=5 vs TS 单连接 | Python 用单连接（对齐 OpenCode/TS），asyncio 序列化 + busy_timeout |
| — | Windows WAL 文件问题（SQLITE_IOERR_SHMOPEN） | per-project 用 DELETE journal mode |
| — | Elixir agent_cache 需手动 evict | 提供 evict_project_db 接口 |
| — | agents 表在 Meta DB（Elixir）vs per-project DB（TS）的关键架构差异 | Python 对齐 Elixir：agents 全局存 Meta DB |
| — | evict 时如有在途查询可能被中断（graceful stop 3s 超时后 kill） | 有效权衡：源码已 best-effort（3s graceful→kill），Python 单连接同理 |

> **RECONCILE — agent_cache evict 竞态（有效权衡）**：源码 `evict/1`（project_factory.ex:212）
> 先 `GenServer.stop(pool, :normal, 3_000)` 优雅停止池，3s 超时则 `Process.exit(pool, :kill)`。
> 若此时有在途查询，优雅停止会等待；超时后 kill 会中断在途查询（调用方收到 exit，`query_for_agent`
> 有 catch 兜底返回 `{:error, :exit}`）。这是 evict 的固有代价，源码已用 3s 宽限 + kill 兜底 +
> 调用方 catch 三重缓解。Python 单连接场景同理：evict 时 close 连接，在途查询收到 `OperationalError`，
> 调用方需 catch 重试。修复成本（引用计数等所有在途查询完成才 evict）高于接受成本。

## 验收标准

- [ ] Meta DB 全局单例（WAL mode）
- [ ] Meta DB 含 agents 表（全局 agent 注册，agent→project 路由依赖）
- [ ] Per-project DB 每项目一个
- [ ] Per-project DB 用 DELETE journal mode
- [ ] busy_timeout = 5000ms
- [ ] ensure_project_db 懒创建
- [ ] query_for_agent 通过 agent_id 反查 project_id（Meta DB agents 表）
- [ ] DB 连接内存缓存
- [ ] evict_project_db 可清除缓存（3s graceful stop + kill 兜底）
- [ ] 建表 SQL 用 IF NOT EXISTS + 运行时 ALTER 迁移
- [ ] Elixir 源码 per-project 池 pool_size=5；Python 迁移用单连接
- [ ] DB 路径 = `<workspace>/.hiveweave/data.db`（非 project.db）

## Python 实现建议

- `aiosqlite` 异步单连接（迁移目标，非源码的 pool_size=5）
- Meta DB 全局单例（WAL mode 可保留，或对齐 per-project 用 DELETE）
- agents 表放 Meta DB（对齐 Elixir 架构，非 TS 的 per-project）
- Per-project DB 用 `dict[project_id, aiosqlite.Connection]` 缓存
- agent → project 路由用 `dict[agent_id, project_id]` 缓存，未命中查 Meta DB agents 表
- PRAGMA 在连接时设置（journal_mode=DELETE, busy_timeout=5000）
- evict 时 close 连接，调用方 catch `OperationalError` 重试（对齐源码 with_project_recovery）
- 参考 OpenCode `database.ts` 的 pragma 配置
