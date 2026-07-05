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
| `projects` | 项目元信息（id, name, workspace_path, language, game_time_acc） |
| `agent_templates` | Agent 模板 |
| `llm_models` | LLM 模型注册 |
| `global_settings` | 全局键值设置 |
| `permission_rules` | 权限规则 |
| `permission_requests` | 审批请求 |
| `mcp_servers` | MCP 服务器配置 |

### Per-project DB 表

| 表 | 说明 |
|---|---|
| `agents` | Agent 信息 |
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
| `merges` | 合并记录 |

## 核心流程

### Per-project DB 创建

```
1. ensure_project_db(workspace_path):
   a. DB 路径 = workspace_path + "/.hiveweave/project.db"
   b. 如果已缓存 → 返回缓存
   c. 打开/创建 SQLite
   d. 设置 PRAGMA：
      - journal_mode = DELETE（避免 Windows WAL 文件）
      - busy_timeout = 5000
   e. 执行建表 SQL（IF NOT EXISTS）
   f. 缓存连接
   g. 返回 :ok
```

### Agent 到 DB 路由

```
1. query_for_agent(agent_id, sql, params):
   a. 从 agent_cache 查 agent_id → project_id
   b. 未命中 → 查 meta DB: SELECT project_id FROM agents WHERE id=?
   c. 缓存映射
   d. 调用 query(project_id, sql, params)
```

## 常量引用

| 常量 | 值 | 所在章节 |
|---|---|---|
| Meta DB 默认路径 | `packages/db/data/hiveweave.db` | 数据库 |
| `HIVEWEAVE_DB_PATH` | 覆盖 meta DB 路径 | 环境变量 |
| Per-project DB 路径 | `<workspace>/.hiveweave/project.db` | — |
| journal_mode | `DELETE` | 数据库 |
| busy_timeout | `5000` ms | 数据库 |
| Per-project DB 连接池 | 单连接 | 数据库（已确认） |

## 已知问题

| 问题编号 | 说明 | Python 迁移处理 |
|---|---|---|
| C3 | Elixir pool_size=5 vs TS 单连接 | 已确认用单连接（对齐 OpenCode） |
| — | Windows WAL 文件问题（SQLITE_IOERR_SHMOPEN） | 用 DELETE journal mode |
| — | Elixir agent_cache 需手动 evict | 提供 evict_project_db 接口 |

## 验收标准

- [ ] Meta DB 全局单例
- [ ] Per-project DB 每项目一个
- [ ] Per-project DB 用 DELETE journal mode
- [ ] busy_timeout = 5000ms
- [ ] ensure_project_db 懒创建
- [ ] query_for_agent 通过 agent_id 反查 project_id
- [ ] DB 连接内存缓存
- [ ] evict_project_db 可清除缓存
- [ ] 建表 SQL 用 IF NOT EXISTS
- [ ] Per-project DB 单连接
- [ ] DB 路径 = `<workspace>/.hiveweave/project.db`

## Python 实现建议

- `aiosqlite` 异步单连接
- Meta DB 全局单例
- Per-project DB 用 `dict[project_id, aiosqlite.Connection]` 缓存
- agent → project 路由用 `dict[agent_id, project_id]` 缓存
- PRAGMA 在连接时设置
- 参考 OpenCode `database.ts` 的 pragma 配置
