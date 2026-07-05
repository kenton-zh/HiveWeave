# 功能契约 14：项目章程与企业目标

> **本文件是功能契约（层 1），用 spec 语言描述模块的行为契约，不引用实现代码。**
> **任何 AI 工具实现 Python 版本时，必须满足此契约中的所有要求。**

## 元信息

| 项 | 值 |
|---|---|
| 模块编号 | 14 |
| 模块名称 | 项目章程与企业目标（Charter） |
| Elixir 源码 | `services/charter.ex` + `schema/agent_charter.ex` + `schema/charter_attachment.ex` + `schema/project.ex` |
| TS 参考源码 | `packages/core/src/charter-service.ts`（部分对应） |
| OpenCode 参考源码 | —（OpenCode 无章程概念） |
| 状态 | 草稿 |

## 功能概述

管理两类持久化项目级元数据：(1) **项目章程**（`agent_charters` 表，Meta DB，每项目至多一条，CEO 撰写，包含 title/content/status）；(2) **企业目标工作簿**（`projects.charter_json` 字段，JSON 格式，包含 objective/focus/keyResults/userInvolvement）。同时维护 goals 脏标记同步机制，使每个 agent 在 goals 变更后能感知并重新读取。`charter_attachments` 是孤儿 schema（有表定义、无 service 操作）。

## 接口契约

### 输入（Consumes）

| 输入 | 来源 | 格式 | 说明 |
|---|---|---|---|
| save_charter 入参 | CEO agent 工具调用 | `{project_id, agent_id, attrs{title, content, status?}}` | status 缺省为 "active" |
| update_goals 入参 | CEO agent 工具调用 | `{project_id, attrs{objective?, focus?, key_results?, user_involvement?}}` | 部分字段更新，未提供字段保留旧值 |
| key_results 元素 | 同上 | `string` 或 `{text, status, owner}` | 接受字符串数组或对象数组，统一归一化为对象 |
| goals 版本查询 | Streamer 每轮 | `{project_id, agent_id}` | 用于 goals_dirty? 检查 |

### 输出（Produces）

| 输出 | 目标 | 格式 | 说明 |
|---|---|---|---|
| read_charter 返回 | 调用方 | `{id, project_id, agent_id, title, content, status, created_at, updated_at, formatted}` | formatted 为 Markdown 格式字符串；无记录返回 nil |
| read_goals 返回 | 调用方 | `{objective, focus, keyResults, userInvolvement}` \| nil | 旧格式纯文本自动包装为 `{objective: text, focus: nil, keyResults: []}` |
| goals_dirty? 返回 | Streamer | `boolean` | 决定是否在本轮注入 goals workbook |

### 副作用

| 副作用 | 触发条件 | 目标 | 说明 |
|---|---|---|---|
| DELETE + INSERT agent_charters | save_charter | Meta DB | 先删后插，保证每项目至多一条 |
| UPDATE projects.charter_json | update_goals | Meta DB | 写入 JSON 字符串 |
| bump goals 版本 | update_goals 成功后 | ETS `:hiveweave_goals_sync` | 写 `{{:version, project_id}, monotonic_ns}` |
| 标记 agent 已读 | Streamer 注入 goals 后 | ETS `:hiveweave_goals_sync` | 写 `{{:read, project_id, agent_id}, version}` |
| PubSub 广播 cache_inval | save_charter 成功后 | `cache_inval:<project_id>` topic | 消息 `{:memory_cache_inval, project_id}` |

## 核心流程

### save_charter（先 DELETE 再 INSERT）

```
1. ensure_table（CREATE TABLE IF NOT EXISTS agent_charters）
2. 生成 UUID + 当前毫秒时间戳
3. 删除该 project_id 的所有现存章程行
4. 插入新行（id, project_id, agent_id, title, content, status, now, now）
5. 广播 cache_inval 到 PubSub
6. 返回新章程 map
```

### read_charter

```
1. ensure_table
2. SELECT ... WHERE project_id = ? ORDER BY created_at DESC LIMIT 1
3. 命中 → 构造 formatted Markdown（## Project Charter: <title> + content + 状态/时间脚注）
4. 未命中或出错 → nil
```

### update_goals（合并写入）

```
1. 归一化 key_results：string → {text, status:"doing", owner:nil}；object → 补全缺省字段
2. 读取现有 goals 作为合并基准
3. 逐字段合并：新值非空则覆盖，否则保留旧值
4. userInvolvement 缺省值 = "宏观决策+技术选型"（对应 medium 级别）
5. JSON 编码为 {objective, focus, keyResults, userInvolvement}
6. UPDATE projects SET charter_json = ?
7. 成功 → touch_goals_version(project_id) + 返回成功
```

### Goals 脏标记同步

```
1. touch_goals_version(project_id)：用单调时钟纳秒值作为新版本号写入 ETS
2. goals_dirty?(project_id, agent_id)：
   a. 取项目当前版本 v_cur；取 agent 上次读取版本 v_read
   b. v_cur 为 nil（从未版本化）→ dirty 当且仅当 v_read 为 nil（agent 首次）
   c. v_cur 非 nil → dirty 当且仅当 v_cur != v_read
3. Streamer 注入 goals 后调用 set_agent_goals_version(project_id, agent_id, v_cur)
4. 新 agent 首次必读（v_read 为 nil）
```

## 状态机（如适用）

| 当前状态 | 触发条件 | 目标状态 | 动作 |
|---|---|---|---|
| goals 未版本化 | update_goals 成功 | goals 已版本化 | 写入版本号 |
| agent 未读 | goals_dirty? 为真 + Streamer 注入 | agent 已读 | 写入 agent 读取版本 |
| goals 已版本化 | 再次 update_goals | 版本号递增 | 覆盖版本号，所有 agent 重新变脏 |

## 错误处理

| 错误场景 | 处理方式 | 重试策略 | 升级策略 |
|---|---|---|---|
| DELETE 失败 | 返回 {:error, reason}，不继续 INSERT | 不重试 | 日志记录 |
| INSERT 失败 | 返回 {:error, reason} | 不重试 | 日志记录 |
| read_goals JSON 解析失败 | 视为旧格式纯文本，包装为 {objective: text} | — | — |
| update_goals 失败 | 返回 {:error, reason} | 不重试 | 日志记录 |
| ETS 表不存在 | ensure_goals_sync_table 幂等创建 | — | — |

## 常量引用

| 常量 | 值 | 所在章节 |
|---|---|---|
| User Involvement 级别 | `high / medium / low` | ETHOS 提示词 |
| 默认 userInvolvement | `medium`（"宏观决策+技术选型"） | 本契约（与契约 13 标注的 high 冲突，见已知问题） |
| 组织范式 | `solo / flat_squad / tech_lead / pm_architect / pod / pipeline` | ETHOS 提示词 |
| Meta DB journal mode | `WAL` | 数据库 |

## 已知问题

| 问题编号 | 说明 | Python 迁移处理 |
|---|---|---|
| — | `charter_attachments` 表有 schema 定义但无任何 service 读写，属孤儿 schema | 迁移时标注为未实现；保留表定义以兼容，但 service 层可不实现 |
| — | 默认 userInvolvement 在 charter.ex 为 "宏观决策+技术选型"（medium），但契约 13 标注默认 high | 需用户确认：建议以 charter.ex 的 medium 为准（写入 DB 的实际默认），streamer 兜底为 high 仅在 charter 缺失时 |
| — | ETS 脏标记是进程内内存，多实例部署失效 | 单实例用内存字典；多实例需 Redis 或 DB 版本号字段 |
| — | save_charter 先 DELETE 再 INSERT 非事务，中途崩溃会丢章程 | Python 用单连接事务包裹 DELETE+INSERT |
| — | goals 版本号用单调时钟纳秒，重启后重置但 agent 读取版本也丢失（ETS 非持久） | 重启后所有 agent 视为首次，强制重读 goals（行为可接受） |

## Python 实现建议

- **框架/库**：`pydantic` 描述 goals JSON schema（`GoalsWorkbook{objective, focus, keyResults: list[KeyResult], userInvolvement}`）；`aiosqlite` 写 Meta DB
- **架构模式**：`CharterService` 类 + `GoalsSyncRegistry` 单例；goals 脏标记用进程内 `dict[project_id, monotonic_int]` + `dict[(project_id, agent_id), int]`
- **注意事项**：
  - save_charter 的 DELETE+INSERT 必须在同一事务内
  - key_results 归一化逻辑要支持 string / dict / 混合输入
  - read_goals 对非 JSON 旧格式做兼容包装
  - 多实例部署时 goals 版本号改用 Redis INCR 或 projects 表新增 `goals_version` 整数列

## 验收标准

- [ ] save_charter 先删后插，保证每项目至多一条章程
- [ ] save_charter 成功后广播 cache_inval 到 PubSub
- [ ] read_charter 返回 formatted Markdown 字符串
- [ ] read_charter 无记录时返回 nil
- [ ] update_goals 部分字段更新，未提供字段保留旧值
- [ ] update_goals 接受 string 数组和 object 数组两种 key_results 输入
- [ ] update_goals 缺省 userInvolvement 为 "宏观决策+技术选型"（medium）
- [ ] update_goals 成功后调用 touch_goals_version
- [ ] read_goals 对非 JSON 旧格式纯文本做兼容包装
- [ ] goals_dirty? 首次 agent（v_read=nil）返回真
- [ ] goals_dirty? 版本变更后返回真
- [ ] goals_dirty? agent 读取后版本未变返回假
- [ ] set_agent_goals_version 写入后 dirty 状态翻转为假
- [ ] charter_attachments 标注为孤儿 schema（无 service）

## 并行对比测试方案

| 测试场景 | Elixir 行为 | Python 预期行为 | 验证方法 |
|---|---|---|---|
| save_charter 后 read | 返回新章程，formatted 含 title/content | 同 | 对比 formatted 字符串 |
| 同 project 二次 save | 旧章程被删除，仅剩新行 | 同 | 查表行数=1 |
| update_goals 部分字段 | 未提供字段保留旧值 | 同 | 读回对比 JSON |
| key_results 字符串数组输入 | 归一化为 {text, status:"doing", owner:nil} | 同 | 读回对比 |
| goals 变更后 agent dirty | goals_dirty? 返回真 | 同 | 调用 dirty 检查 |
| agent 读取后 dirty 翻转 | goals_dirty? 返回假 | 同 | 调用 dirty 检查 |
| 旧格式纯文本 charter_json | 包装为 {objective: text} | 同 | read_goals 对比 |

---

> **填写规则**：
> 1. 用 spec 语言，不用代码语言。说"做什么"，不说"怎么做"。
> 2. 所有常量值引用 `constants.md`，不在此处重复定义。
> 3. 所有已知问题引用 `known-issues.md`，不在此处重复描述。
> 4. 每个契约必须经过用户确认后才能标记为"已确认"。
> 5. "Python 实现建议"是建议不是约束，实现者可以根据情况调整，但必须满足验收标准。
