# 功能契约 03：对话历史与压缩

> **本文件是功能契约（层 1），用 spec 语言描述模块的行为契约，不引用实现代码。**

## 元信息

| 项 | 值 |
|---|---|
| 模块编号 | 03 |
| 模块名称 | 对话历史与压缩 |
| Elixir 源码 | `apps/hiveweave/lib/hiveweave/conversation_store.ex` + `compaction/overflow.ex` + `token_utils.ex` |
| TS 参考源码 | `packages/core/src/conversation-store.ts` + `token-utils.ts` |
| OpenCode 参考源码 | `D:\PC_AI\Project\opencode\packages\core\src\session\compaction.ts` + `util/token.ts` |
| 状态 | 草稿 |

## 功能概述

Per-agent 持久化对话历史管理。基于 token budget（非消息条数）裁剪历史，确保不超出模型上下文窗口。当历史接近预算上限时，通过 LLM 将旧对话摘要为结构化 handoff，保留近期完整 turn 不拆分。支持模型切换时触发紧急压缩。历史消息从 DB 懒加载，内存缓存。System 消息不入库（每次由 Streamer 重建）。

## 接口契约

### 输入（Consumes）

| 输入 | 来源 | 格式 | 说明 |
|---|---|---|---|
| agent_id | Streamer | string | 当前 agent |
| project_id | Streamer | string | 当前项目 |
| token_budget | Streamer | int \| nil | 模型上下文窗口 - COMPACTION_BUFFER；nil 时用默认 128K |
| messages（追加时） | Streamer | `[{role, content, tool_calls?, tool_call_id?}]` | 一轮对话的消息（user + assistant + tool） |
| compactor callback | 启动时注入 | `(old_messages) → Promise<string\|null>` | LLM 摘要回调 |

### 输出（Produces）

| 输出 | 目标 | 格式 | 说明 |
|---|---|---|---|
| 历史消息列表 | Streamer | `[{role, content, tool_calls?, tool_call_id?}]` | 已裁剪到 token budget 内，不含 system 消息 |
| compaction 事件 | 日志 | — | 记录压缩触发、旧消息数、摘要字符数 |

### 副作用

| 副作用 | 触发条件 | 目标 | 说明 |
|---|---|---|---|
| 持久化 turn | append_turn 调用 | per-project DB `conversation_turns` | 异步写入，不阻塞主流程 |
| 删除历史 | clear 调用 | per-project DB | 删除指定 agent 的所有 turn |
| 清空全部缓存 | clear_all（启动时） | 内存 | 清空所有 agent 的内存缓存，不删 DB |
| LLM 摘要调用 | compaction 触发 | LLM API | 异步，失败时回退到硬截断 |
| 缓存更新 | compaction 完成 | 内存 | 替换缓存中的消息列表 |

## 核心流程

### 获取历史

```
1. get_history(agent_id, project_id, token_budget):
   a. 查内存缓存
   b. 缓存未命中 → 从 DB 加载 conversation_turns → clean_messages → trim_to_budget → 写缓存
   c. 缓存命中 → clean_messages → trim_to_budget
   d. 返回消息列表（不含 system 消息）
```

### 追加 turn

```
1. append_turn(agent_id, project_id, messages):
   a. 过滤掉 system 消息（不入库，Streamer 每次重建）
   b. 加载现有历史（缓存或 DB）
   c. 合并：existing ++ filtered_new
   d. 异步持久化新 turn 到 DB
   e. 更新内存缓存
   f. 触发 compaction 检查（maybe_trigger_compaction）
```

### Compaction 流程

```
1. maybe_trigger_compaction(agent_id, project_id, key, messages):
   a. 估算总 token 数
   b. 获取 agent 的模型 context_window（DB 查询，默认 128K）
   c. budget = context_window - COMPACTION_BUFFER
   d. 如果 total > budget * 0.85 → 触发异步 compaction

2. do_compaction(agent_id, project_id, messages, budget):
   a. 确定分割点：
      - recent_count = min(PRESERVE_RECENT_MAX, max(PRESERVE_RECENT_MIN, len(messages)/3))
      - 但实际保留的是按 turn 计算的 tail_turns（默认 2，见常量确认）
   b. 分割为 old_messages + recent_messages
   c. 如果 old 为空 → 直接 trim_to_budget
   d. 否则调用 LLM 摘要（call_compactor_llm）：
      - 构建结构化摘要 prompt（Goal/Constraints/Progress/Decisions/Next Steps/Critical Context/Relevant Files）
      - temperature=0.3, max_tokens=2000
      - 工具输出截断到 tool_output_max_chars
   e. 摘要成功 → 构造 system 摘要消息，前置到 recent_messages
   f. 摘要失败 → 回退到 trim_to_budget（硬截断）
   g. 发送 {:compaction_done, key, compacted_messages} 消息更新缓存
```

### 模型切换压缩

```
1. maybe_compact_on_model_switch(agent_id, project_id, opts):
   a. 比较 old_context_window vs new_context_window
   b. 如果新窗口更小，且当前 token 数可能超出 → 立即触发 compaction
   c. 返回 :compacted 或 :ok
```

### 消息清理（clean_messages）

```
1. 移除 system 消息（Streamer 负责重建）
2. 移除孤立的 tool 消息（没有匹配的 tool_call_id）
3. 不拆分 assistant(tool_calls) + tool(result) 的 turn 对
```

### Token 预算裁剪（trim_to_budget）

```
1. 估算总 token 数
2. 如果在预算内 → 直接返回
3. 超出预算 → 从最旧的 turn 开始移除
4. 每次移除一个完整 turn（user + assistant + 关联的 tool 消息）
5. 保留最近 tail_turns 个 turn 不移除
6. 如果移除到只剩 tail_turns 仍超预算 → 截断工具输出
7. 如果仍超 → 截断消息内容
```

## 消息格式

### 持久化到 DB 的格式

```
conversation_turns 表：
- id (UUID)
- agent_id (string)
- project_id (string)
- messages (JSON string，一整轮的消息数组)
- created_at (timestamp)
```

### 传给 Streamer 的格式

```json
[
  {"role": "user", "content": "..."},
  {"role": "assistant", "content": "...", "tool_calls": [{"id":"...", "type":"function", "function":{"name":"...", "arguments":"..."}}]},
  {"role": "tool", "tool_call_id": "...", "content": "..."},
  {"role": "assistant", "content": "..."},
  ...
]
```

### Compaction 摘要消息格式

```json
{
  "role": "system",
  "content": "## Previous Conversation Summary\n\n<LLM 生成的结构化摘要>\n\n---\nBelow is the recent conversation:"
}
```

### DeepSeek 前缀缓存友好的消息布局

```
[System 1] identity prompt（常量，不变）→ prefix cache hit
[System 2] dynamic context（memories, handoffs, inbox）→ 可能变化
[System 3] compaction summary（如有）→ 压缩后变化
[User/Assistant/Tool...] conversation history → 不断追加
```

## 错误处理

| 错误场景 | 处理方式 | 说明 |
|---|---|---|
| DB 加载失败 | 返回空列表 | 不阻塞 agent 运行 |
| LLM 摘要调用失败 | 回退到硬截断（trim_to_budget） | 不丢失近期消息 |
| 摘要返回空 | 视为失败，回退到硬截断 | — |
| DB 持久化失败 | 仅日志记录 | 异步写入，不阻塞主流程 |
| 模型 context_window 查不到 | 默认 128_000 | — |

## 常量引用

| 常量 | 值 | 所在章节 |
|---|---|---|
| `tail_turns` | `2` | Token 预算与压缩（已确认，OpenCode 默认值） |
| `COMPACTION_BUFFER` | `20_000` | 同上 |
| `OUTPUT_TOKEN_MAX` | `32_000` | 同上 |
| `PRESERVE_RECENT_MIN` | `2_000` | 同上（TS 值，OpenCode 用单一 DEFAULT_KEEP_TOKENS=8_000） |
| `PRESERVE_RECENT_MAX` | `8_000` | 同上（OpenCode DEFAULT_KEEP_TOKENS） |
| `@prune_protect_tokens` | `40_000` | 同上（Elixir 特有，保护近期工具输出） |
| `@prune_minimum_tokens` | `20_000` | 同上 |
| `@tool_output_max_chars` | `2_000` | 同上 |
| `@compaction_trigger_ratio` | `0.85` | — |
| `SUMMARY_OUTPUT_TOKENS` | `4_096` | — |
| 摘要 temperature | `0.3` | — |
| 摘要 max_tokens | `2_000` | — |
| 默认 context_window | `128_000` | — |
| CJK token 估算 | `1.5` 字符/token | — |
| 非 CJK token 估算 | `4` 字符/token | — |

## 已知问题

| 问题编号 | 说明 | Python 迁移处理 |
|---|---|---|
| C1 | Elixir tail_turns=4 vs TS=2 | 已确认用 2（OpenCode 默认） |
| — | Elixir `token_utils.ex` 的 `@default_tail_turns 20` 是 dead code | 忽略 |
| — | Elixir `@preserve_recent_min/max` 用的是消息条数（10/30），TS 用的是 token 数（2000/8000） | Python 用 token 数（对齐 TS/OpenCode） |

## Python 实现建议

- **架构模式**：
  - `class ConversationStore` 单例，内存缓存用 `dict[tuple[project_id, agent_id], list[dict]]`
  - `compacted_prefix_cache: dict[tuple[project_id, agent_id], str]` 存压缩摘要
  - `async def get_history()` / `async def append_turn()` / `async def clear()`
  - DB 操作用 aiosqlite 异步

- **Compaction**：
  - 参照 OpenCode `compaction.ts` 的结构化摘要模板（Goal/Constraints/Progress/Decisions/Next Steps/Critical Context/Relevant Files）
  - LLM 摘要失败时回退到硬截断
  - 摘要结果作为 system 消息前置

- **Token 估算**：
  - 参照 OpenCode `util/token.ts`：`CHARS_PER_TOKEN = 4`
  - 但 HiveWeave 区分 CJK（1.5）和非 CJK（4），Python 侧保留这个区分

- **DeepSeek 前缀缓存**：
  - system 消息分三层：identity（常量）/ dynamic（memories, handoffs）/ compaction summary
  - history 在 system 消息之后

## 验收标准

- [ ] `get_history()` 返回裁剪后的消息列表，不含 system 消息
- [ ] `append_turn()` 过滤 system 消息后持久化到 DB
- [ ] 内存缓存命中时不查 DB
- [ ] `clear()` 删除 DB 记录和内存缓存
- [ ] `clear_all()` 清空内存缓存（不删 DB）
- [ ] token 估算区分 CJK（1.5）和非 CJK（4）
- [ ] 裁剪以 turn 为单位，不拆分 assistant(tool_calls) + tool(result) 对
- [ ] 保留最近 tail_turns=2 个完整 turn 不移除
- [ ] 总 token 超过 budget * 0.85 时触发异步 compaction
- [ ] compaction 用 LLM 摘要旧消息，生成结构化 handoff
- [ ] 摘要模板包含 Goal/Constraints/Progress/Decisions/Next Steps/Critical Context/Relevant Files
- [ ] 摘要失败时回退到硬截断
- [ ] 模型切换时检查是否需要紧急压缩
- [ ] 孤立的 tool 消息（无匹配 tool_call_id）被清理
- [ ] 摘要消息作为 system 消息前置到 recent_messages
- [ ] DeepSeek 前缀缓存友好：identity prompt 常量，dynamic context 分离
- [ ] compaction summary 的 tool 输出截断到 2000 字符

## 并行对比测试方案

| 测试场景 | Elixir 行为 | Python 预期行为 | 验证方法 |
|---|---|---|---|
| 首次获取历史 | DB 加载 → 缓存 | 相同 | 新 agent 首次调用，对比 DB 查询 |
| 缓存命中 | 不查 DB | 相同 | 连续调用两次，对比 DB 查询次数 |
| 追加 turn | 过滤 system → 持久化 → 更新缓存 | 相同 | 追加含 system 的消息，对比 DB 内容 |
| token 裁剪 | 按预算裁剪旧 turn | 相同 | 发送大量消息，对比裁剪后的列表 |
| turn 不拆分 | assistant(tool_calls)+tool(result) 不被拆开 | 相同 | 构造跨 turn 的消息，验证裁剪边界 |
| compaction 触发 | 85% 预算时触发 | 相同 | 发送足够多消息，对比触发时机 |
| compaction 摘要 | LLM 生成结构化摘要 | 相同 | mock LLM，对比摘要 prompt 和结果格式 |
| compaction 回退 | LLM 失败 → 硬截断 | 相同 | mock LLM 返回错误，对比回退行为 |
| 模型切换压缩 | 新窗口更小时触发 | 相同 | 切换到更小 context_window 模型，对比触发 |
| 孤立 tool 清理 | 无匹配 tool_call_id 的 tool 消息被移除 | 相同 | 构造孤立 tool 消息，对比清理结果 |
| clear | DB 记录 + 缓存都清除 | 相同 | clear 后查 DB 和缓存 |
| clear_all | 仅清缓存 | 相同 | clear_all 后查缓存为空，DB 不变 |

---

> **填写规则**：
> 1. 用 spec 语言，不用代码语言。说"做什么"，不说"怎么做"。
> 2. 所有常量值引用 `constants.md`，不在此处重复定义。
> 3. 所有已知问题引用 `known-issues.md`，不在此处重复描述。
> 4. 每个契约必须经过用户确认后才能标记为"已确认"。
> 5. "Python 实现建议"是建议不是约束，实现者可以根据情况调整，但必须满足验收标准。
