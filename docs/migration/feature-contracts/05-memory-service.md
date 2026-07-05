# 功能契约 05：三层记忆

> **本文件是功能契约（层 1），用 spec 语言描述模块的行为契约。**

## 元信息

| 项 | 值 |
|---|---|
| 模块编号 | 05 |
| 模块名称 | 三层记忆 |
| Elixir 源码 | `services/memory.ex` + `schema/memory.ex` |
| TS 参考源码 | `packages/core/src/memory-service.ts` |
| OpenCode 参考源码 | 无独立记忆模块 |
| 状态 | 草稿 |

## 功能概述

三层 scope 隔离的长期记忆系统，把文本片段注入 agent 的 system prompt。`project` 层全员共享（宪章）；`agent` 层是单个 agent 的私有工作记忆；`archive` 层是被解散 agent 冻结的记忆，按 `module_id` 索引，供继任 agent 通过 revival 协议读取。

## 接口契约

### 输入

| 输入 | 来源 | 格式 | 说明 |
|---|---|---|---|
| project_id | 调用方 | string | 项目标识 |
| agent_id | 调用方 | string | agent 标识 |
| scope | 调用方 | `"project"` / `"agent"` / `"archive"` | 记忆层 |
| module_id | 调用方 | string \| nil | archive 层检索用 |
| content | write_memory | string | 记忆内容 |
| type | write_memory | string | 记忆类型（默认 `"fact"`） |

### 输出

| 输出 | 目标 | 格式 | 说明 |
|---|---|---|---|
| 记忆列表 | Streamer system prompt | `[memory]` | 注入到 dynamic context |
| context 字符串 | Streamer | string \| nil | 拼三层记忆为 Markdown |
| memory id | write_memory 调用方 | `{:ok, id}` | 新记忆的 UUID |

### 副作用

| 副作用 | 触发条件 | 目标 | 说明 |
|---|---|---|---|
| DB 写入 | write_memory | per-project DB `memories` | 插入新记忆 |
| scope 变更 | archive_agent_memories | per-project DB | `agent → archive` 批量转移 |
| 缓存失效 | write_memory 后 | PubSub + ETS | 广播失效，排空后重读 |

## 数据模型

```sql
CREATE TABLE memories (
  id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  scope TEXT DEFAULT 'agent',      -- project | agent | archive
  module_id TEXT,
  type TEXT DEFAULT 'fact',
  content TEXT,
  source_agent_id TEXT,
  metadata TEXT DEFAULT '{}',      -- JSON string
  created_at INTEGER,
  updated_at INTEGER
);
```

## 核心流程

```
1. build_agent_context(project_id, agent_id, module_id):
   a. get_project_memories(project_id) → 缓存 30s
   b. get_agent_memories(project_id, agent_id) → 缓存 5min
   c. get_archived_memories(project_id, module_id) → 缓存 5min（如有 module_id）
   d. 拼接为 Markdown，每层带标题
   e. 三层全空 → 返回 nil

2. write_memory(project_id, opts):
   a. 插入 DB，scope 默认 "agent"
   b. 广播缓存失效

3. archive_agent_memories(project_id, agent_id):
   a. UPDATE memories SET scope='archive' WHERE agent_id=? AND scope='agent'
   b. 返回转移条数
```

## 常量引用

| 常量 | 值 | 说明 |
|---|---|---|
| project 缓存 TTL | `30_000` ms | 宪章易变，短缓存 |
| agent/archive 缓存 TTL | `300_000` ms | 5 分钟 |
| content 截断 | `200` 字符 | build_agent_context 中截断 |

## 已知问题

| 问题编号 | 说明 | Python 迁移处理 |
|---|---|---|
| — | schema 注释写 `agent_private`，实际代码用 `agent` | 统一用 `agent` |
| — | archive 后 agent_id 仍指向已解散 agent | 通过 module_id 检索，不用 agent_id |
| — | TS 版无缓存 | Python 加缓存（对齐 Elixir） |
| — | build_agent_context 空行为差异：Elixir 返回 nil，TS 返回带标题字符串 | 以 Elixir 为准，空时返回 nil |

## 验收标准

- [ ] 三层 scope 隔离：project / agent / archive
- [ ] project 层全 agent 共享，agent 层仅自己可读
- [ ] archive 层按 module_id 检索
- [ ] write_memory 插入 DB 并失效缓存
- [ ] archive_agent_memories 将 scope 从 agent 改为 archive
- [ ] build_agent_context 拼三层为 Markdown
- [ ] 三层全空时返回 nil
- [ ] project 缓存 30s，agent/archive 缓存 5min

## Python 实现建议

- `class MemoryService` + 内存缓存 `dict` + TTL
- aiosqlite 异步读写
- 缓存失效用简单 TTL 过期，不需要 PubSub
