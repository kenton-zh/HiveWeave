# 功能契约 18：CRUD 服务集（Model/Template/Settings/TeamChat/Names）

> **本文件是功能契约（层 1），用 spec 语言描述模块的行为契约，不引用实现代码。**
> **任何 AI 工具实现 Python 版本时，必须满足此契约中的所有要求。**

## 元信息

| 项 | 值 |
|---|---|
| 模块编号 | 18 |
| 模块名称 | CRUD 服务集（LLM 模型 / Agent 模板 / 全局设置 / 群聊 / 花名） |
| Elixir 源码 | `services/model.ex` + `services/template.ex` + `services/settings.ex` + `services/team_chat.ex` + `names.ex` + `schema/llm_model.ex` + `schema/agent_template.ex` + `schema/global_setting.ex` |
| TS 参考源码 | `packages/core/src/services/model-service.ts` + `template-service.ts` + `settings-service.ts` + `team-chat-service.ts` + `packages/shared/src/names.ts` |
| OpenCode 参考源码 | —（无对应，HiveWeave 自有功能） |
| 状态 | 草稿 |

## 功能概述

五个轻量 CRUD/工具服务的组合契约，覆盖 HiveWeave 的"配置层 + 群聊 + 命名"：

1. **ModelService（LLM 模型注册表）** — Meta DB 中的 `llm_models` 表 CRUD，管理可用 LLM 端点（name/model_id/base_url/api_key/context_window/max_output/supports_thinking 等）。`list_models` 对 api_key 脱敏（仅返回前 8 字符 + `...`）。
2. **TemplateService（Agent 人格模板）** — Meta DB 中的 `agent_templates` 表，存可被 HR 浏览并用于 `hire_agent` 预填的 agent 人格模板（source/division/name/role/color/emoji/vibe/description/prompt_body）。
3. **SettingsService（全局设置）** — Meta DB 中的 `global_settings` 表，简单 key-value 存储（如 `operatorName`），upsert 语义。
4. **TeamChatService（多 agent 群聊）** — per-project DB 中复用 `chat_messages` 表（`role='team'`）+ 独立的 `team_chat_dedupe` 去重表。1 分钟窗口内 (from, to, content) 三元组重复则丢弃。
5. **Names（花名生成）** — 纯函数模块，8 个风格池（poetic_single / nature_pairs / modern_short / bold / elegant / playful / three_char / four_char）。`generate_flower_name` 随机选池随机选名；`is_flower_name?` 校验 1-4 字 CJK。启动时用于迁移 CEO/HR 的非花名显示名。

前三个服务落 Meta DB（见契约 11），TeamChat 落 per-project DB，Names 无 DB。

## 接口契约

### ModelService 接口

| 函数 | 参数 | 返回值 | 说明 |
|---|---|---|---|
| `list_models` | `()` | `[model_map]` | 列出所有模型（按 created_at ASC），api_key 脱敏 |
| `get_model` | `(id)` | `{:ok, model_map}` \| `{:error, :not_found}` | 取单个模型（api_key 完整返回） |
| `create_model` | `(attrs)` | `{:ok, %{id, name, model_id}}` \| `{:error, reason}` | 创建模型 |
| `update_model` | `(id, attrs)` | `{:ok, id}` \| `{:error, "No fields to update"}` \| `{:error, reason}` | 更新模型（仅非 nil 字段） |
| `delete_model` | `(id)` | `:ok` \| `{:error, reason}` | 删除模型 |
| `get_active_models` | `()` | `[%{id, name, model_id}]` | 取所有 is_active=1 的模型 |
| `seed_default_model` | `()` | `{:ok, model}` \| `{:ok, :already_seeded}` \| `{:error, reason}` | 启动时种子默认模型（见核心流程） |

**`list_models` 返回结构（api_key 脱敏）：**

```
{
  id, name, model_id, base_url,
  api_key: "<前8字符>..." 或 nil,
  context_window, max_output_tokens,
  supports_thinking: bool,
  is_active: bool
}
```

**`get_model` 返回结构（api_key 完整）：** 同上但 `api_key` 为完整明文。

**`create_model` 的 `attrs` 字段：**

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `id` | string (UUID) | 自动生成 | 模型 ID |
| `name` | string | `""` | 显示名 |
| `model_id` | string | `""` | 模型标识（发给 LLM API 的 model 字段） |
| `base_url` | string | `""` | API endpoint |
| `api_key` | string | `""` | API 密钥 |
| `context_window` | int | `128_000` | 上下文窗口 |
| `max_output_tokens` | int | `8_192` | 最大输出 token |
| `is_active` | bool | `true`（除非显式传 false） | 是否启用 |

> 注：`create_model` 不写入 `supports_thinking` / `default_reasoning_effort` / `temperature`（schema 定义但 create 未处理，见已知问题）。

**`update_model` 支持的字段：** `name` / `model_id` / `base_url` / `api_key` / `context_window` / `max_output_tokens` / `is_active`（bool）。仅非 nil 字段更新；无任何字段时返回 `{:error, "No fields to update"}`。

### TemplateService 接口

| 函数 | 参数 | 返回值 | 说明 |
|---|---|---|---|
| `list_templates` | `(opts?)` | `[template_map]` | 列出模板（支持 source/division/search 过滤，LIMIT 50） |
| `get_template` | `(id)` | `{:ok, template_map}` \| `{:error, :not_found}` | 取单个模板（含 prompt_body） |
| `create_template` | `(attrs)` | `{:ok, %{id, name}}` \| `{:error, reason}` | 创建模板 |

**`list_templates` 的 `opts` 过滤参数：**

| 字段 | 类型 | 说明 |
|---|---|---|
| `source` | string | 精确匹配 source（如 `agency-agents` / `custom`） |
| `division` | string | 精确匹配 division |
| `search` | string | name OR description 模糊匹配（LIKE %search%） |

> 排序：`ORDER BY source, division, name`。LIMIT 50。

**`list_templates` 返回结构（不含 prompt_body）：**

```
{ id, source, division, name, role, color, emoji, vibe, description }
```

**`get_template` 返回结构（含 prompt_body）：** 同上 + `prompt_body`。

**`create_template` 的 `attrs` 字段：**

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `id` | string (UUID) | 自动生成 | 模板 ID |
| `source` | string | `"custom"` | 来源 |
| `division` | string | `""` | 部门 |
| `name` | string | `""` | 模板名 |
| `role` | string | `"specialist"` | 角色类型 |
| `color` | string | `""` | 颜色 |
| `emoji` | string | `""` | 表情 |
| `vibe` | string | `""` | 风格 |
| `description` | string | `""` | 描述 |
| `prompt_body` | string | `""` | 提示词正文 |

> **HR 集成**：HR 通过 `list_agent_templates` 工具调用 `list_templates`，`hire_agent` 时传 `templateId` 预填 role/goal/backstory（见契约 04）。

### SettingsService 接口

| 函数 | 参数 | 返回值 | 说明 |
|---|---|---|---|
| `get` | `(key)` | `value` \| `nil` | 取一个值；不存在/异常返回 nil |
| `set` | `(key, value)` | `{:ok, value}` \| `{:error, reason}` | upsert（DELETE + INSERT） |
| `all` | `()` | `%{key => value}` | 取所有设置为 map；异常返回 `%{}` |
| `delete` | `(key)` | `:ok` | 删除一个值；异常也返回 `:ok` |

> `set` 的 value 用 `to_string(value)` 转字符串存储。

### TeamChatService 接口

| 函数 | 参数 | 返回值 | 说明 |
|---|---|---|---|
| `record_message` | `(agent_id, from_agent_id, to_agent_id, content, opts?)` | `:ok` \| `:duplicate` \| `{:error, reason}` | 记录群聊消息（带去重） |
| `get_history` | `(agent_id, limit=50)` | `[msg_map]` | 取群聊历史（role='team'，按时间正序） |

**`record_message` 行为：**

1. 计算 dedupe_key = MD5("{from}:{to}:{content}")，hex lowercase
2. 查 `team_chat_dedupe` 表：1 分钟窗口内是否有相同 dedupe_key
3. 重复 → 返回 `:duplicate`，不写入
4. 不重复 → INSERT 到 `chat_messages`（role='team', is_background=0, is_read=0, is_streaming=0, team_from_agent_id, team_to_agent_id）
5. 同时 INSERT dedupe_key 到 `team_chat_dedupe` 表
6. 返回 `:ok`

**`get_history` 返回结构：**

```
{
  id, agent_id, content,
  from_agent_id: team_from_agent_id,
  to_agent_id: team_to_agent_id,
  created_at
}
```

> 排序：内部 DESC + reverse 得到正序（同 ChatMessage 的 `get_messages` 模式）。

### Names 接口

| 函数 | 参数 | 返回值 | 说明 |
|---|---|---|---|
| `generate_flower_name` | `()` | `string` | 随机生成一个花名 |
| `is_flower_name?` | `(name)` | `boolean` | 校验是否为花名（1-4 字 CJK） |

**花名校验规则：** 正则 `^[\u4e00-\u9fff]{1,4}$`（CJK Unified Ideographs，1-4 字）。`nil`/非字符串/含非 CJK 字符（如 "CEO"/"HR"/英文名）均返回 `false`。

### 输入（Consumes）

| 输入 | 来源 | 格式 | 说明 |
|---|---|---|---|
| Model CRUD 调用 | REST API `/api/models` | attrs map | 前端模型管理 UI |
| Template CRUD 调用 | REST API + HR 工具 | attrs map | HR 浏览模板库 |
| Settings 调用 | REST API + 服务内部 | `(key, value)` | 全局配置读写 |
| TeamChat `record_message` | DispatchService / CommunicationService | `(agent_id, from, to, content)` | agent 间群聊消息 |
| `seed_default_model` 调用 | 服务启动钩子 | — | 启动时种子默认模型 |
| 花名迁移调用 | 服务启动钩子 | agent 列表 | CEO/HR 无花名时生成 |

### 输出（Produces）

| 输出 | 目标 | 格式 | 说明 |
|---|---|---|---|
| 模型列表 | 前端模型管理 UI | `[model_map]`（api_key 脱敏） | 列表展示 |
| 单个模型 | LLM 调用层 | `model_map`（api_key 完整） | Streamer 取模型配置 |
| 模板列表 | HR 工具返回 | `[template_map]` | HR 浏览模板 |
| 设置值 | 各服务 | `value` \| `nil` | 配置读取 |
| 群聊历史 | 前端 TeamComms 面板 | `[msg_map]` | 群聊渲染 |
| 花名 | agent 创建/迁移 | `string` | 显示名 |

### 副作用

| 副作用 | 触发条件 | 目标 | 说明 |
|---|---|---|---|
| 写 `llm_models` | `create_model` / `update_model` | Meta DB | INSERT / UPDATE |
| 删 `llm_models` | `delete_model` | Meta DB | DELETE |
| 写 `agent_templates` | `create_template` | Meta DB | INSERT |
| 写 `global_settings` | `set` | Meta DB | DELETE + INSERT（upsert） |
| 删 `global_settings` | `delete` | Meta DB | DELETE |
| 写 `chat_messages`（role='team'） | `record_message` | per-project DB | INSERT（非重复时） |
| 写 `team_chat_dedupe` | `record_message` | per-project DB | INSERT dedupe_key |
| 种子默认模型 | 启动钩子 | Meta DB `llm_models` | 首次启动时 INSERT |
| 迁移花名 | 启动钩子 | per-project DB `agents` | UPDATE 非花名 agent 的 name |

## 数据模型

### `llm_models` 表（Meta DB）

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | UUID（string，非自增） |
| `name` | TEXT | NOT NULL DEFAULT '' | 显示名 |
| `model_id` | TEXT | NOT NULL DEFAULT '' | 模型标识 |
| `base_url` | TEXT | NOT NULL DEFAULT '' | API endpoint |
| `api_key` | TEXT | NOT NULL DEFAULT '' | API 密钥（明文存储） |
| `context_window` | INTEGER | NOT NULL DEFAULT 128000 | 上下文窗口 |
| `max_output_tokens` | INTEGER | NOT NULL DEFAULT 8192 | 最大输出 token |
| `supports_thinking` | INTEGER | NOT NULL DEFAULT 0 | 是否支持思维链（0/1） |
| `default_reasoning_effort` | TEXT | | 默认推理努力（schema 定义，CRUD 未处理） |
| `temperature` | TEXT | | 温度（schema 定义，CRUD 未处理） |
| `is_active` | INTEGER | NOT NULL DEFAULT 1 | 是否启用（0/1） |
| `created_at` | INTEGER | NOT NULL | 创建时间（ms） |
| `updated_at` | INTEGER | NOT NULL | 更新时间（ms） |

### `agent_templates` 表（Meta DB）

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | UUID（string） |
| `source` | TEXT | NOT NULL DEFAULT '' | 来源（如 `agency-agents` / `custom`） |
| `division` | TEXT | NOT NULL DEFAULT '' | 部门 |
| `name` | TEXT | NOT NULL | 模板名 |
| `role` | TEXT | NOT NULL DEFAULT 'specialist' | 角色类型 |
| `color` | TEXT | NOT NULL DEFAULT '' | 颜色 |
| `emoji` | TEXT | NOT NULL DEFAULT '' | 表情 |
| `vibe` | TEXT | NOT NULL DEFAULT '' | 风格 |
| `description` | TEXT | NOT NULL DEFAULT '' | 描述 |
| `prompt_body` | TEXT | NOT NULL DEFAULT '' | 提示词正文 |
| `original_file` | TEXT | NOT NULL DEFAULT '' | 原始文件（schema 定义） |
| `created_at` | INTEGER | NOT NULL | 创建时间（ms） |

### `global_settings` 表（Meta DB）

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `key` | TEXT | PRIMARY KEY | 设置键 |
| `value` | TEXT | NOT NULL DEFAULT '' | 设置值（字符串） |
| `updated_at` | INTEGER | NOT NULL DEFAULT 0 | 更新时间（ms） |

### `team_chat_dedupe` 表（per-project DB）

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | UUID |
| `agent_id` | TEXT | NOT NULL | 所属 agent |
| `dedupe_key` | TEXT | NOT NULL | MD5(from:to:content) hex |
| `created_at` | INTEGER | | 创建时间（ms） |

> **去重窗口**：1 分钟（60_000 ms）。查询条件 `created_at > now - 60000`。表无自动清理，旧记录累积（见已知问题）。

### `chat_messages` 表（per-project DB，TeamChat 复用）

见契约 17。TeamChat 写入时 `role='team'`，`is_background=0`，`is_read=0`，`is_streaming=0`，填 `team_from_agent_id` / `team_to_agent_id`。

## 核心流程

### ModelService CRUD

```
1. list_models():
   a. SELECT id, name, model_id, base_url, api_key, context_window, max_output_tokens, supports_thinking, is_active
   b. ORDER BY created_at ASC
   c. api_key 脱敏：key && String.slice(key, 0, 8) <> "..."（nil 保持 nil）
   d. supports_thinking / is_active：integer(0/1) → boolean
   e. 异常返回 []

2. get_model(id):
   a. SELECT 同上 WHERE id=? LIMIT 1
   b. 命中 → {:ok, model_map}（api_key 完整）
   c. 未命中 → {:error, :not_found}
   d. 异常 → {:error, inspect(e)}

3. create_model(attrs):
   a. id 缺省 → 生成 UUID
   b. context_window 缺省 → 128_000
   c. max_output_tokens 缺省 → 8_192
   d. is_active：attrs[:is_active] != false → 1，否则 0
   e. now = 当前 ms
   f. INSERT（created_at=now, updated_at=now）
   g. 返回 {:ok, %{id, name, model_id}}

4. update_model(id, attrs):
   a. 收集非 nil 字段：name/model_id/base_url/api_key/context_window/max_output_tokens
   b. is_active 用 Map.has_key? 判断（支持显式设 false）
   c. 总是追加 updated_at=now
   d. 若只有 updated_at（无其他字段）→ {:error, "No fields to update"}
   e. 否则动态构造 SET 子句 UPDATE

5. delete_model(id): DELETE WHERE id=?

6. get_active_models(): SELECT id, name, model_id WHERE is_active=1 ORDER BY created_at ASC
```

### seed_default_model（启动种子）

> 注：Elixir 端未实现 `seed_default_model`（搜索未找到），TS 端 `seedDefaultModel` 存在。本契约描述期望行为，Python 必须实现。

```
1. seed_default_model():
   a. 检查 llm_models 表是否已有记录
   b. 若已有 → 返回 {:ok, :already_seeded}（不重复种子）
   c. 若为空：
      - 读取环境变量 OPENCODE_API_KEY
      - 若 OPENCODE_API_KEY 为空 → 返回 {:error, :no_api_key}（不种子）
      - 创建默认模型：
        name = "DeepSeek V4 Flash Free"
        model_id = "deepseek-v4-flash-free"
        base_url = "https://opencode.ai/zen/v1"
        api_key = OPENCODE_API_KEY
        context_window = 200_000
        max_output_tokens = 8_192
        supports_thinking = false
        is_active = true
      - INSERT
      - 返回 {:ok, model}
```

> 见 `constants.md` 模型种子章节。注意 README 提示：该 gateway 不支持 tool-calling，agent 用工具需另配模型。

### TemplateService CRUD

```
1. list_templates(opts):
   a. 构建 WHERE：source=?（精确）/ division=?（精确）/ (name LIKE ? OR description LIKE ?)（模糊）
   b. 多条件 AND 连接
   c. SELECT id, source, division, name, role, color, emoji, vibe, description
   d. ORDER BY source, division, name LIMIT 50
   e. 异常返回 []

2. get_template(id):
   a. SELECT 同上 + prompt_body WHERE id=? LIMIT 1
   b. 命中 → {:ok, template_map}；未命中 → {:error, :not_found}

3. create_template(attrs):
   a. id 缺省 → UUID；source 缺省 → "custom"；role 缺省 → "specialist"
   b. 其他字段缺省 → ""
   c. now = 当前 ms
   d. INSERT
   e. 返回 {:ok, %{id, name}}
```

### SettingsService CRUD

```
1. get(key):
   a. SELECT value WHERE key=?
   b. 命中 → value；未命中 → nil；异常 → nil

2. set(key, value):
   a. DELETE WHERE key=?（先删）
   b. INSERT (key, to_string(value), now_ms)
   c. 成功 → {:ok, value}；异常 → {:error, inspect(e)}

3. all():
   a. SELECT key, value ORDER BY key
   b. 转 %{key => value}；异常 → %{}

4. delete(key):
   a. DELETE WHERE key=?
   b. 总是返回 :ok（异常也返回 :ok）
```

### TeamChat record_message

```
1. record_message(agent_id, from, to, content, opts):
   a. dedupe_key = MD5("{from}:{to}:{content}") → hex lowercase
   b. cutoff = now_ms - 60_000
   c. 查 team_chat_dedupe：SELECT id WHERE agent_id=? AND dedupe_key=? AND created_at > cutoff
   d. 若存在 → 返回 :duplicate（不写 chat_messages）
   e. 若不存在：
      i.  INSERT 到 chat_messages（role='team', is_background=0, is_read=0, is_streaming=0, team_from_agent_id=from, team_to_agent_id=to, created_at=now_ms）
      ii. INSERT 到 team_chat_dedupe（id=UUID, agent_id, dedupe_key, created_at=now_ms）
      iii. 返回 :ok
   f. save 失败 → {:error, reason}；save_dedupe_key 失败 → 静默（rescue :ok）
```

### TeamChat get_history

```
1. get_history(agent_id, limit=50):
   a. SELECT id, agent_id, content, team_from_agent_id, team_to_agent_id, created_at
   b. WHERE role='team' AND agent_id=?
   c. ORDER BY created_at DESC LIMIT ?
   d. Enum.reverse → 正序（oldest first）
   e. 返回 [msg_map]；异常返回 []
```

### Names 花名生成

```
1. generate_flower_name():
   a. 从 8 个池中随机选 1 个池
   b. 从该池中随机选 1 个名字
   c. 返回该名字

2. is_flower_name?(name):
   a. nil → false
   b. 非字符串 → false
   c. 正则匹配 ^[\u4e00-\u9fff]{1,4}$（1-4 字 CJK Unified Ideographs）
   d. 匹配 → true；否则 → false
```

### 启动时花名迁移

```
1. 服务启动钩子：
   a. 遍历所有 project 的 agents
   b. 对每个 agent，若 is_flower_name?(agent.name) == false：
      - new_name = generate_flower_name()
      - UPDATE agents SET name=new_name WHERE id=agent.id
      - 典型场景：legacy CEO/HR agent 名称为 "CEO"/"HR" 等非花名
   c. 日志记录迁移
```

> 见 AGENTS.md："启动时...rename legacy CEO/HR agents to a flower name (花名) if they don't have one"。

## 状态机（如适用）

### Model 启用状态

| 当前状态 | 触发条件 | 目标状态 | 动作 |
|---|---|---|---|
| (无) | `create_model(is_active=true)` | `active` | INSERT is_active=1 |
| (无) | `create_model(is_active=false)` | `inactive` | INSERT is_active=0 |
| `active` | `update_model(is_active=false)` | `inactive` | UPDATE is_active=0 |
| `inactive` | `update_model(is_active=true)` | `active` | UPDATE is_active=1 |
| any | `delete_model` | (删除) | DELETE |

### TeamChat 去重状态

| 当前状态 | 触发条件 | 目标状态 | 动作 |
|---|---|---|---|
| (无 dedupe_key) | `record_message` 新三元组 | `recorded` | 写 chat_messages + dedupe_key |
| `recorded` | 1 分钟内相同三元组 | `recorded`（不变） | 返回 :duplicate，不写 |
| `recorded` | 超过 1 分钟后相同三元组 | `recorded`（新记录） | 写新 chat_messages + 新 dedupe_key |

## 错误处理

| 错误场景 | 处理方式 | 重试策略 | 升级策略 |
|---|---|---|---|
| `list_models` 异常 | rescue 后返回 `[]` | 不重试 | —（fail-empty） |
| `get_model` 异常 | 返回 `{:error, inspect(e)}` | 不重试 | — |
| `create_model` / `update_model` / `delete_model` 异常 | 返回 `{:error, inspect(e)}` | 不重试 | — |
| `update_model` 无字段 | 返回 `{:error, "No fields to update"}` | 不重试 | — |
| `list_templates` 异常 | rescue 后返回 `[]` | 不重试 | — |
| `get_template` 异常 | 返回 `{:error, inspect(e)}` | 不重试 | — |
| `create_template` 异常 | 返回 `{:error, inspect(e)}` | 不重试 | — |
| `settings.get` 异常 | rescue 后返回 `nil` | 不重试 | —（fail-null） |
| `settings.set` 异常 | 返回 `{:error, inspect(e)}` | 不重试 | — |
| `settings.all` 异常 | rescue 后返回 `%{}` | 不重试 | —（fail-empty） |
| `settings.delete` 异常 | rescue 后返回 `:ok` | 不重试 | —（fail-silent） |
| `record_message` save 失败 | 返回 `{:error, reason}` | 不重试 | — |
| `record_message` save_dedupe_key 失败 | rescue 后静默 `:ok` | 不重试 | —（dedupe_key 丢失，可能下次重复，可接受） |
| `is_duplicate?` 异常 | rescue 后返回 `false` | 不重试 | —（fail-open，宁可重复不丢消息） |
| `get_history` 异常 | 返回 `[]` | 不重试 | — |
| `seed_default_model` 无 API key | 返回 `{:error, :no_api_key}` | 不重试 | 日志提示 |

## 常量引用

| 常量 | 值 | 所在章节 |
|---|---|---|
| Meta DB journal mode | `WAL` | 数据库 |
| Per-project DB journal mode | `DELETE` | 数据库 |
| Per-project DB busy_timeout | `5000` ms | 数据库 |
| TeamChat 去重窗口 | `60_000` ms（1 分钟） | 本契约 |
| `list_templates` LIMIT | `50` | 本契约 |
| `get_history` 默认 limit | `50` | 本契约 |
| 默认 context_window | `128_000` | 本契约 |
| 默认 max_output_tokens | `8_192` | 本契约 |
| 种子模型 context_window | `200_000` | 模型种子 |
| 种子模型 endpoint | `https://opencode.ai/zen/v1` | 模型种子 |
| 种子模型 model_id | `deepseek-v4-flash-free` | 模型种子 |
| 花名 CJK 范围 | `\u4e00-\u9fff`（1-4 字） | 本契约 |
| 花名池数量 | `8` | 本契约 |
| `OPENCODE_API_KEY` | 环境变量 | 环境变量 |
| 时间戳单位 | 毫秒（ms epoch） | 本契约 |

## 已知问题

| 问题编号 | 说明 | Python 迁移处理 |
|---|---|---|
| E9（新） | `llm_models` schema 定义了 `default_reasoning_effort` / `temperature` 字段，但 `model.ex` 的 `create_model` / `update_model` 均未处理这两个字段。`list_models` / `get_model` 的 SELECT 也不含这两列 | Python 实现应补全：`create_model` / `update_model` 支持这两字段，`list_models` / `get_model` 返回这两字段 |
| E10（新） | `create_model` 不写入 `supports_thinking`（schema 默认 false），无法通过 create 设置 thinking 模型 | Python `create_model` 应支持 `supports_thinking` 参数 |
| E11（新） | `settings.set` 用 DELETE + INSERT 实现 upsert，非原子。`global_settings` 表有 PRIMARY KEY(key) 约束，但 DELETE+INSERT 中间窗口该 key 无值 | Python 用 `INSERT ... ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at` 原子 upsert |
| E12（新） | `team_chat_dedupe` 表无自动清理，旧 dedupe_key 记录持续累积。1 分钟窗口外的记录永不被查询但永不被删 | Python 实现建议：启动时或定期清理 `created_at < now - 60000` 的记录；或用 SQLite TTL 机制 |
| E13（新） | `agent_templates` 表的 `original_file` 字段 schema 定义但 `template.ex` 服务完全未使用 | Python 可保留字段但标记为 deprecated，或用于追溯模板来源文件 |
| E14（新） | `seed_default_model` 在 Elixir 端未实现（搜索未找到），但 TS 端 `seedDefaultModel` 存在且 README 描述了该行为。Elixir 端通过 `config/config.exs` 的静态配置 `:llm_providers` 替代 | Python 必须实现 `seed_default_model`（对齐 TS 行为），首次启动时若 llm_models 表为空且 OPENCODE_API_KEY 存在则种子 |
| E15（新） | 花名迁移在启动时遍历所有 project 所有 agent，可能影响启动速度（大量 agent 时） | Python 可批量查询 + 批量 UPDATE，或异步迁移不阻塞启动 |
| C2 | 端口不一致 | 本契约不直接涉及（端口在契约 11/19） |
| E4 | 空 recipients 可能崩溃 | `record_message` 的 from/to 应校验非空 |

## Python 实现建议

- **框架/库**：
  - SQLAlchemy 2.x（async） + aiosqlite
  - Meta DB 一个全局 `AsyncSession` 工厂；per-project DB 各自工厂（见契约 11）
  - Pydantic v2 做 attrs 校验
  - 花名生成用 `random.choice`（无需加密随机）
- **架构模式**：
  - 每个服务一个 repository class：`ModelRepository` / `TemplateRepository` / `SettingsRepository` / `TeamChatRepository`
  - `NamesService` 为纯函数模块（无状态，无 DB），可直接用模块级函数或 staticmethod
  - Meta DB 表的 repository 接受 Meta DB session；TeamChat repository 接受 `agent_id` 解析到 per-project DB
- **关键实现点**：
  - **api_key 脱敏**：`list_models` 返回 `api_key[:8] + "..." if api_key else None`；`get_model` 返回完整 api_key。注意：仅 `list_models` 脱敏，`get_model` 不脱敏（Streamer 需完整 key 调 LLM）
  - **`update_model` 动态 SQL**：用 SQLAlchemy `update().where().values(**non_nil_fields)` 构造，避免手拼 SQL。`is_active` 用 `col in attrs` 判断（支持显式 false）
  - **`update_model` 空字段检测**：若除 `updated_at` 外无其他字段，返回错误（不发 SQL）
  - **`settings.set` 原子 upsert**：`INSERT ... ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at`
  - **`team_chat_dedupe` 去重**：MD5 用 `hashlib.md5(f"{from}:{to}:{content}".encode()).hexdigest()`。查询 `created_at > now - 60000`
  - **`team_chat_dedupe` 清理**：建议启动时 `DELETE FROM team_chat_dedupe WHERE created_at < ?`（now - 60000），避免无限增长
  - **花名池**：8 个 list 常量，`generate_flower_name` 用 `random.choice(random.choice(pools))`（先选池再选名，等概率）。`is_flower_name?` 用 `re.match(r"^[\u4e00-\u9fff]{1,4}$", name)`
  - **`seed_default_model`**：启动钩子，查 `SELECT COUNT(*) FROM llm_models`，为 0 且 `os.environ.get("OPENCODE_API_KEY")` 非空时 INSERT 种子模型
  - **花名迁移**：启动钩子，遍历所有 project 的 agents，`is_flower_name?(name) == False` 则 `generate_flower_name()` 并 UPDATE。建议批量处理避免 N+1 查询
- **注意事项**：
  - Meta DB 用 WAL，per-project DB 用 DELETE（见契约 11）
  - `list_templates` 的 search 用 LIKE，注意 SQL 注入防护（参数化）
  - `record_message` 的 `save_dedupe_key` 失败应静默（rescue :ok），因为 dedupe 是优化非必需
  - `is_duplicate?` 异常 fail-open（返回 false，宁可重复不丢消息）
  - `settings.delete` 异常 fail-silent（返回 :ok），因为删除是幂等的
  - 补全 Elixir 未实现的字段处理：`default_reasoning_effort` / `temperature` / `supports_thinking` 在 create/update 中支持

## 验收标准

- [ ] `create_model` 后 `list_models` 包含该模型，api_key 显示为 `<前8字符>...`
- [ ] `get_model(id)` 返回完整 api_key（不脱敏）
- [ ] `update_model(id, {name: "new"})` 后 `get_model(id).name == "new"`
- [ ] `update_model(id, {})` 返回 `{:error, "No fields to update"}`
- [ ] `update_model(id, {is_active: false})` 能将 active 模型设为 inactive（支持显式 false）
- [ ] `delete_model(id)` 后 `get_model(id)` 返回 `{:error, :not_found}`
- [ ] `get_active_models` 只返回 `is_active=1` 的模型
- [ ] `seed_default_model()` 在空表 + OPENCODE_API_KEY 存在时，插入 DeepSeek V4 Flash Free 模型
- [ ] `seed_default_model()` 在表已有模型时返回 `:already_seeded`，不重复插入
- [ ] `seed_default_model()` 在 OPENCODE_API_KEY 缺失时不种子，返回错误
- [ ] `list_templates(source: "custom")` 只返回 source='custom' 的模板
- [ ] `list_templates(search: "test")` 返回 name 或 description 含 "test" 的模板
- [ ] `list_templates` 最多返回 50 条
- [ ] `get_template(id)` 返回含 `prompt_body` 的完整模板
- [ ] `create_template` 后 `get_template` 能取到，默认值正确（source='custom', role='specialist'）
- [ ] `settings.set("k", "v")` 后 `settings.get("k") == "v"`
- [ ] `settings.set("k", "v2")` 后 `settings.get("k") == "v2"`（upsert，不产生重复 key）
- [ ] `settings.all()` 返回包含所有设置的 dict
- [ ] `settings.delete("k")` 后 `settings.get("k") == nil`
- [ ] `settings.get("nonexistent")` 返回 `nil`
- [ ] `record_message` 首次写入返回 `:ok`，chat_messages 表多一条 role='team' 记录
- [ ] 1 分钟内相同 (from, to, content) 再次 `record_message` 返回 `:duplicate`，不写新记录
- [ ] 超过 1 分钟后相同三元组 `record_message` 返回 `:ok`，写新记录
- [ ] `get_history(agent_id)` 返回 role='team' 的消息，按时间正序
- [ ] `generate_flower_name()` 返回 1-4 字 CJK 字符串
- [ ] `is_flower_name?("霜月")` 返回 `true`
- [ ] `is_flower_name?("CEO")` 返回 `false`（非 CJK）
- [ ] `is_flower_name?(nil)` 返回 `false`
- [ ] `is_flower_name?("五个字的名称")` 返回 `false`（超 4 字）
- [ ] 启动时 legacy CEO（name="CEO"）被迁移为花名，`is_flower_name?` 返回 `true`
- [ ] 启动时已有花名的 agent 不被重命名

## 并行对比测试方案

| 测试场景 | Elixir 行为 | Python 预期行为 | 验证方法 |
|---|---|---|---|
| 模型 list 脱敏 | api_key 前 8 字符 + "..." | 同 | create model with key="sk-1234567890"，list_models 返回 "sk-12345..." |
| 模型 get 完整 | api_key 完整 | 同 | get_model 返回完整 "sk-1234567890" |
| 模型 update 空字段 | {:error, "No fields to update"} | 同 | update_model(id, {}) 返回错误 |
| 模型 update is_active=false | 支持 false | 同 | update_model(id, {is_active:false}) 后 get_model.is_active==false |
| 模板 search 过滤 | name OR description LIKE | 同 | create 2 模板，search 匹配其中 1 个 name |
| 模板 LIMIT 50 | 最多 50 条 | 同 | 插入 51 条，list_templates 返回 50 条 |
| 设置 upsert | DELETE + INSERT，无重复 key | ON CONFLICT upsert | set("k","v") 再 set("k","v2")，all() 只有一个 k |
| 设置 fail-null | get 异常返回 nil | 同 | mock DB 异常，验证返回 nil |
| 群聊去重 | 1 分钟内重复 :duplicate | 同 | record_message 两次相同三元组，第二次返回 :duplicate |
| 群聊超窗口 | 超过 1 分钟可再次写入 | 同 | 等待 > 1 分钟（或 mock 时间），第三次返回 :ok |
| 群聊历史正序 | DESC + reverse | 同 | 写 3 条，get_history 返回 [1,2,3] 顺序 |
| 花名生成 | 1-4 字 CJK | 同 | 调用 100 次，所有结果 is_flower_name?==true |
| 花名校验 | CEO→false, 霜月→true, 5字→false, nil→false | 同 | 4 个用例验证 |
| 种子模型 | （Elixir 未实现） | 首次启动 + API key 存在时种子 | 空表 + OPENCODE_API_KEY，启动后 llm_models 有 1 条 DeepSeek 记录 |
| 花名迁移 | CEO/HR 启动时改名 | 同 | 创建 name="CEO" 的 agent，重启服务后 name 为花名 |

---

> **填写规则**：
> 1. 用 spec 语言，不用代码语言。说"做什么"，不说"怎么做"。
> 2. 所有常量值引用 `constants.md`，不在此处重复定义。
> 3. 所有已知问题引用 `known-issues.md`，不在此处重复描述。
> 4. 每个契约必须经过用户确认后才能标记为"已确认"。
> 5. "Python 实现建议"是建议不是约束，实现者可以根据情况调整，但必须满足验收标准。
