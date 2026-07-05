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

- `bound_skills TEXT DEFAULT '[]'` — JSON 数组，已绑定技能 slug 列表
- `mcp_servers TEXT DEFAULT '[]'` — JSON 数组，已绑定 MCP 服务器名列表
- `skills TEXT DEFAULT '[]'` — 初始技能（创建时预设）

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
   a. 校验权限：CEO/HR 可绑定任意 agent，其他角色只能绑定自己
   b. 检查技能是否存在（外部 → 内置 → ClawHub）
   c. 检查去重（已绑定则报错）
   d. UPDATE agent SET bound_skills = bound_skills + [skillName]
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

### MCP 调用

```
1. mcp_call(server, tool, arguments):
   a. 检查 server 是否已绑定到当前 agent
   b. 连接 MCP 服务器（stdio 或 HTTP）
   c. 调用 tool，传 arguments
   d. 返回结果字符串
```

## 已知问题

| 问题编号 | 说明 | Python 迁移处理 |
|---|---|---|
| E1 | Elixir MCP 简化为 HTTP-only，4 个硬编码 URL，无 stdio | 使用官方 `mcp` Python SDK，支持 stdio + HTTP |
| — | Elixir mcp_configure 持久化但 mcp_call 不读取配置表 | 修正：mcp_call 从 mcp_servers 表读取配置 |
| — | Elixir guess_mcp_url 硬编码 5 个 URL | 删除硬编码，从配置表读取 |
| — | TS 有 SkillHub 备用 API，Elixir 无 | 可选实现 |
| — | YAML frontmatter 解析用手写正则 | 用 `python-frontmatter` 库 |

## 验收标准

- [ ] list_available_skills 列出外部 + 内置 + ClawHub 技能
- [ ] get_skill_detail 返回技能详情
- [ ] read_skill 返回 SKILL.md 全文
- [ ] bind_skill 校验权限 + 去重
- [ ] unbind_skill 校验存在性
- [ ] build_active_skills_section 注入摘要段
- [ ] mcp_configure 写入 mcp_servers 表（admin only）
- [ ] bind_mcp 绑定 MCP 服务器到 agent
- [ ] mcp_list_tools 列出所有已绑定 MCP 的工具
- [ ] mcp_call 调用 MCP 工具
- [ ] MCP 支持 stdio + HTTP 两种 transport
- [ ] mcp_call 从 mcp_servers 表读取配置（不硬编码 URL）

## Python 实现建议

- 技能：`class SkillRegistry` + 外部目录扫描 + 内置字典 + ClawHub API
- MCP：用官方 `mcp` Python SDK（`mcp.client.stdio` + `mcp.client.http`）
- ClawHub：`httpx` async 请求，10 分钟缓存
- YAML frontmatter：`python-frontmatter` 库
- 技能绑定权限：CEO/HR 可操作任意 agent，其他角色仅自己
