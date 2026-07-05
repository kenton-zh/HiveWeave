# 功能契约 10：MCP 与技能

> **本文件是功能契约（层 1），用 spec 语言描述模块的行为契约。**

## 元信息

| 项 | 值 |
|---|---|
| 模块编号 | 10 |
| 模块名称 | MCP 与技能 |
| Elixir 源码 | `skill_registry.ex` + `tool_executor.ex`（mcp_* dispatch） |
| TS 参考源码 | `packages/core/src/mcp/mcp-service.ts` + `packages/core/src/clawhub-service.ts` |
| OpenCode 参考源码 | `D:\PC_AI\Project\opencode\packages\opencode\src\tool/` |
| 状态 | 草稿 |

## 功能概述

**技能（Skill）**：SKILL.md 风格的指令文档，绑定后注入 Agent system prompt 的 "Active Skills" 段（仅摘要），运行时通过 `read_skill` 按需加载全文。来源三层：外部文件系统 → 内置注册表 → ClawHub 远程市场。

**MCP**：外部工具能力扩展。Agent 通过 `bind_mcp` 绑定 MCP 服务器，通过 `mcp_call` 调用外部工具。

## 接口契约

### 技能接口

| 函数 | 参数 | 返回值 | 说明 |
|---|---|---|---|
| `list_available_skills` | `(search?)` | formatted string | 列出可用技能（工具输出格式） |
| `get_skill_detail` | `(slug)` | formatted string | 技能详情 |
| `read_skill` | `(slug, bound_skills?)` | string | 读取技能全文（SKILL.md 内容） |
| `bind_skill` | `(agentId, skillName)` | `:ok` / `:error` | 绑定技能到 agent |
| `unbind_skill` | `(agentId, skillName)` | `:ok` / `:error` | 解绑 |
| `build_active_skills_section` | `(bound_skills_json)` | string | 注入 system prompt 的摘要段 |

### MCP 接口

| 函数 | 参数 | 返回值 | 说明 |
|---|---|---|---|
| `mcp_configure` | `(name, transport, command?, url?)` | `:ok` | 配置 MCP 服务器（admin only） |
| `bind_mcp` | `(agentId, mcpServer)` | `:ok` / `:error` | 绑定 MCP 服务器到 agent |
| `unbind_mcp` | `(agentId, mcpServer)` | `:ok` / `:error` | 解绑 |
| `list_available_mcp` | — | formatted string | 列出已配置的 MCP 服务器 |
| `mcp_list_tools` | — | formatted string | 列出所有已绑定 MCP 的工具 |
| `mcp_call` | `(server, tool, arguments)` | string | 调用 MCP 工具 |

## 数据模型

### Agent 表字段

- `bound_skills TEXT DEFAULT '[]'` — JSON 数组，**当前已绑定**技能 slug 列表（bind/unbind 修改此字段；prompt 注入用此字段）
- `mcp_servers TEXT DEFAULT '[]'` — JSON 数组，已绑定 MCP 服务器名列表
- `skills TEXT DEFAULT '[]'` — **初始技能快照**（hire 时由 `input["skills"]` 写入，**不可变记录**）

> **RECONCILE — skills vs bound_skills 关系**：源码 `hire_agent`（tool_executor.ex:1918）在创建
> agent 时同时写两个字段：`skills: Jason.encode!(initial_skills)` 和
> `bound_skills: Jason.encode!(initial_skills)` —— 即 **bound_skills 初始化为 skills 的副本**。
> 此后 `bind_skill`/`unbind_skill` 只改 `bound_skills`，`skills` 永远保留"入职时携带的技能"记录。
> prompt 构建（`build_active_skills_section`）和 `read_skill` 的"已绑定"判断都用 `bound_skills`。
> Python 迁移应保留此语义：`skills` 为不可变入职记录，`bound_skills` 为运行时可变集合。

### mcp_servers 表（meta DB）

```sql
CREATE TABLE mcp_servers (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  transport TEXT NOT NULL DEFAULT 'http',  -- stdio | http
  command TEXT DEFAULT '',
  url TEXT DEFAULT '',
  created_at INTEGER
);
```

## 核心流程

### 技能绑定

```
1. bind_skill(agentId, skillName):
   a. 校验权限（resolve_and_update_agent）：仅允许 操作自身 / 直属下属 / CEO+HR 可操作项目内任意 agent
   b. 检查技能是否存在（外部 → 内置 → ClawHub）
   c. 检查去重（已绑定则报错）
   d. UPDATE agent SET bound_skills = bound_skills + [skillName]
   e. skills 字段不动（不可变入职记录）
```

### 技能读取

```
1. read_skill(slug, bound_skills?):
   a. 优先级：外部文件（.compressed.md > .md）→ 内置 → ClawHub
   b. 返回 SKILL.md 全文
   c. 用于 agent 按需加载完整指令
```

### Active Skills 段注入

```
1. build_active_skills_section(bound_skills_json):
   a. 解析 bound_skills JSON
   b. 对每个 slug 获取 name + description
   c. 拼接为 Markdown 段
   d. 提示语："可用 read_skill 加载完整指令"
   e. 注入到 system prompt 的 dynamic context 部分
```

### MCP 绑定

```
1. bind_mcp(agentId, mcpServer):
   a. 校验权限（resolve_and_update_agent）：与 bind_skill 完全相同的门禁
      （自身 / 直属下属 / CEO+HR 项目内任意 agent）；跨项目拒绝
   b. 检查去重（已绑定则报错）
   c. UPDATE agent SET mcp_servers = mcp_servers + [mcpServer]
```

> **RECONCILE — bind_mcp 无权限校验（噪声，权限实际存在）**：审查员误报"任何 agent 可绑定
> 任意 MCP server"。源码 `dispatch("bind_mcp", ...)` 调用 `resolve_and_update_agent/3`，该函数
> （tool_executor.ex:3226）对 bind_mcp 与 bind_skill 使用**同一套**权限门禁：仅允许操作自身、
> 直属下属，或 CEO/HR 操作项目内任意 agent；跨项目一律拒绝。契约已补充此门禁说明。

### MCP 调用

```
1. mcp_call(server, tool, arguments):
   a. 检查 server 是否在当前 agent.mcp_servers 列表中（否则报 "not bound"）
   b. guess_mcp_url(server) 解析 URL（源码硬编码 5 个，未知 server 用 phash2 哈希到端口）
   c. POST JSON-RPC {method: "tools/call", params: {name, arguments}}
   d. receive_timeout = 30_000 ms（30s 超时，源码 execute_mcp_call/4）
   e. 解析 result.content，提取 text 字段
```

> **RECONCILE — mcp_call 无超时（噪声，超时实际存在）**：审查员误报"MCP 调用无超时"。
> 源码 `execute_mcp_call/4`（tool_executor.ex:4609）用 `Req.post(url, ..., receive_timeout: 30_000)`
> 设置 **30s 超时**。契约已补充。Python 迁移应用 `httpx` 的 `timeout=30.0`。

> **RECONCILE — stdio 进程管理（Elixir 无，Python 需实现）**：源码 Elixir MCP 实现
> **HTTP-only**（已知问题 E1），无 stdio transport，因此无子进程生命周期管理。Python 迁移若
> 用官方 `mcp` SDK 支持 stdio，需自行管理子进程：spawn 子进程 → stdin/stdout 传 JSON-RPC →
> unbind/agent dismiss/服务停止时 terminate 子进程 → 异常退出时重启。契约原已知问题已标注
> Elixir HTTP-only，此处补强 Python stdio 生命周期要求。

### ClawHub 降级行为

```
1. list_available_skills / get_skill_detail / read_skill 调 ClawHub 时：
   a. HTTP 请求 receive_timeout = 5_000 ms（5s）
   b. 任何异常/超时/非 200 → rescue → {:error, :clawhub_unavailable}
   c. list_available_skills：ClawHub 失败 → 仅返回 外部 + 内置 技能（不报错）
   d. get_skill_detail / read_skill：外部/内置未命中 + ClawHub 失败 → 返回 "not found" 提示
2. ClawHub 永远是 best-effort，不可用不影响核心功能
```

> **RECONCILE — ClawHub 降级未描述（有效可操作）**：源码 `search_clawhub/1`、
> `fetch_clawhub_detail/1` 均用 `rescue` 捕获异常返回 `{:error, :clawhub_unavailable}`，
> 上层 `list_available_skills` 静默降级到"仅内置+外部"。契约原未描述此降级链，已补充。

## 已知问题

| 问题编号 | 说明 | Python 迁移处理 |
|---|---|---|
| E1 | Elixir MCP 简化为 HTTP-only，4 个硬编码 URL，无 stdio | 使用官方 `mcp` Python SDK，支持 stdio + HTTP；需自管 stdio 子进程生命周期 |
| — | Elixir mcp_configure 持久化但 mcp_call 不读取配置表（用 guess_mcp_url 硬编码） | 修正：mcp_call 从 mcp_servers 表读取配置 |
| — | Elixir guess_mcp_url 硬编码 5 个 URL | 删除硬编码，从配置表读取 |
| — | TS 有 SkillHub 备用 API，Elixir 无 | 可选实现 |
| — | YAML frontmatter 解析用手写正则 | 用 `python-frontmatter` 库 |
| — | bind_mcp/bind_skill 共用 resolve_and_update_agent 权限门禁 | 保留：自身/直属下属/CEO+HR 项目内任意 agent |
| — | mcp_call 有 30s 超时（receive_timeout） | Python 用 `httpx` timeout=30.0 |
| — | ClawHub 5s 超时，失败静默降级到内置+外部 | 保留 best-effort 降级链 |
| — | skills 字段不可变（入职快照），bound_skills 运行时可变 | 保留语义：bound_skills 初始化为 skills 副本 |

## 验收标准

- [ ] list_available_skills 列出外部 + 内置 + ClawHub 技能
- [ ] ClawHub 不可用时降级为仅 外部 + 内置（best-effort，5s 超时）
- [ ] get_skill_detail 返回技能详情
- [ ] read_skill 返回 SKILL.md 全文
- [ ] bind_skill 校验权限（自身/直属下属/CEO+HR 任意）+ 去重
- [ ] bind_mcp 使用与 bind_skill 相同的权限门禁
- [ ] unbind_skill 校验存在性
- [ ] build_active_skills_section 注入摘要段（基于 bound_skills）
- [ ] skills 字段为不可变入职快照，bound_skills 运行时可变
- [ ] mcp_configure 写入 mcp_servers 表（admin only）
- [ ] bind_mcp 绑定 MCP 服务器到 agent
- [ ] mcp_list_tools 列出所有已绑定 MCP 的工具
- [ ] mcp_call 调用 MCP 工具（30s 超时）
- [ ] mcp_call 检查 server 是否已绑定到当前 agent
- [ ] MCP 支持 stdio + HTTP 两种 transport（Python 新增 stdio）
- [ ] stdio transport 管理子进程生命周期（spawn/terminate/restart）
- [ ] mcp_call 从 mcp_servers 表读取配置（不硬编码 URL）

## Python 实现建议

- 技能：`class SkillRegistry` + 外部目录扫描 + 内置字典 + ClawHub API
- MCP：用官方 `mcp` Python SDK（`mcp.client.stdio` + `mcp.client.http`）
- stdio 子进程生命周期：`asyncio.create_subprocess_exec` spawn，`Process.terminate()` 清理，异常时重启
- MCP 调用超时：`httpx.AsyncClient(timeout=30.0)` 或 `asyncio.wait_for(..., timeout=30)`
- ClawHub：`httpx` async 请求，5s 超时，失败静默降级，10 分钟缓存
- YAML frontmatter：`python-frontmatter` 库
- 技能/MCP 绑定权限：CEO/HR 可操作任意 agent，coordinator 可操作直属下属，其余仅自己（对齐 resolve_and_update_agent）
- skills 字段：hire 时写入后只读；bound_skills：bind/unbind 修改，初始化为 skills 副本
