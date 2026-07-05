# 功能契约 01：LLM 流式调用

> **本文件是功能契约（层 1），用 spec 语言描述模块的行为契约，不引用实现代码。**
> **任何 AI 工具实现 Python 版本时，必须满足此契约中的所有要求。**

## 元信息

| 项 | 值 |
|---|---|
| 模块编号 | 01 |
| 模块名称 | LLM 流式调用 |
| Elixir 源码 | `apps/hiveweave/lib/hiveweave/llm/streamer.ex` + `circuit_breaker.ex` |
| TS 参考源码 | `packages/agent-runtime/src/agent-runtime.ts` + `stream-timeout.ts` + `retry-utils.ts` + `provider-factory.ts` |
| OpenCode 参考源码 | `D:\PC_AI\Project\opencode\packages\llm\src\llm.ts` + `packages\core\src\session\runner\llm.ts` |
| 状态 | 草稿 |

## 功能概述

向 LLM 发起 OpenAI 兼容的流式请求，逐 token 转发给前端，同时解析 tool_calls。如果有 tool_calls，执行工具后将结果追加到消息列表，重新请求 LLM，如此循环直到 LLM 不再返回 tool_calls 或达到最大轮次。整个过程中需要处理超时、重试、熔断、上下文溢出、空响应、doom loop 检测等多种异常场景。

## 接口契约

### 输入（Consumes）

| 输入 | 来源 | 格式 | 说明 |
|---|---|---|---|
| agent 对象 | OrgService | `{id, project_id, role, permission_type, ...}` | 当前调用的 agent 信息 |
| user message | 用户/协调者 | string | 触发本次流式调用的消息 |
| opts | 调用方 | `{trigger: bool, from_agent_id: string\|nil}` | 是否为协调者触发（非用户直接消息） |
| conversation history | ConversationStore | `[{role, content, tool_calls?, tool_call_id?}]` | 从 DB 加载的历史消息，已按 token budget 裁剪 |
| model config | DB model registry | `{model_id, api_key, base_url, context_window, max_output_tokens, supports_thinking, reasoning_effort}` | 从 DB 解析的模型配置 |
| tools | ToolExecutor.get_tools | `[{type:"function", function:{name, description, parameters}}]` | 按 agent 角色和权限类型过滤的可用工具 |

### 输出（Produces）

| 输出 | 目标 | 格式 | 说明 |
|---|---|---|---|
| stream events | 前端（WebSocket） | `{type: "start"\|"text_delta"\|"thinking_delta"\|"tool_use"\|"tool_result"\|"error"\|"done", ...}` | 逐 token 流式事件 |
| assistant message | DB chat_messages | `{role:"assistant", content, tool_calls, is_streaming, ...}` | 最终持久化的助手消息 |
| tool turn messages | ConversationStore | `[{role:"assistant",...}, {role:"tool",...}]` | 每轮的工具调用+结果，追加到对话历史 |
| LLM trace | EventAudit | `{round_num, input_tokens, output_tokens, finish_reason, ...}` | 每轮 LLM 调用的审计日志 |
| activity events | lobby:status PubSub | `{type:"thinking"\|"working", agentId, ...}` | 前端 Live Activity 显示 |

### 副作用

| 副作用 | 触发条件 | 目标 | 说明 |
|---|---|---|---|
| 创建 placeholder 消息 | 流式开始前 | DB chat_messages | `is_streaming=true, content=""`，保证刷新页面能看到正在进行的对话 |
| 更新消息内容 | 每轮结束 | DB chat_messages | 追加文本、tool_calls，保证崩溃后不丢数据 |
| 清除 streaming 标志 | 流式结束（成功/失败/崩溃） | DB chat_messages | `is_streaming=false`，必须有重试机制（3 次退避） |
| 清除 zombie streaming 标志 | **应用启动时**（`application.ex` boot） | DB chat_messages | `clear_stuck_streaming()` 清除所有 `is_streaming=true` 的残留行（上次崩溃遗留），防止前端永远显示"正在输入" |
| 广播 chunk | 每个 token 到达 | PubSub → WebSocket | 实时转发给前端 |
| 广播 activity | 流式开始 | PubSub lobby:status | "正在思考..." |
| Telemetry 计时 | 流式开始/结束 | Telemetry | LLM 调用耗时监控 |

## 核心流程

```
1. 模型切换压缩检查：如果 agent 的 context_window 缓存值变化，触发 maybe_compact_on_model_switch
2. 熔断器检查：检查 provider 的 circuit breaker 状态
   - closed → 放行
   - open + 冷却已过 → half_open，当前调用者成为探针
   - open + 冷却未过 → 切换到 fallback provider（如配置）
   - 无 fallback → 返回错误
3. 加载对话历史：从 ConversationStore 按 token budget 获取历史消息
4. 解析工具集：按 agent 的 permission_type + role 获取可用工具
5. 构建消息列表：system prompt + history + user message
6. 创建 placeholder 助手消息（is_streaming=true）
7. 广播 start 事件 + thinking activity
8. 进入 tool loop（最多 N 轮，N 由角色决定）：
   a. 上下文溢出检查：估算 token 数，超 usable 则 trim_context_if_needed
   b. 中轮提醒：达到 80% 最大轮次时注入"开始收尾"系统提示
   c. 构建 request_body：model, messages, stream=true, temperature, max_tokens, tools, reasoning_effort
   d. 发起 HTTP 流式请求（带重试，最多 3 次）
   e. 逐 chunk 接收：
      - text delta → 累积文本 + 广播 text_delta
      - reasoning delta → 累积推理 + 广播 thinking_delta
      - tool_calls delta → 累积工具调用
      - usage → 记录 token 使用量
      - finish_reason → 标记结束
   f. 处理 finish_reason：
      - "length" / "content_filter" + 有 tool_calls → 丢弃不完整的 tool_calls，追加警告
      - "length" → 追加截断警告
      - "content_filter" → 追加过滤警告
   g. 如果有 tool_calls（且 finish_reason 正常）：
      - 截断到每轮最多 5 个 tool_calls
      - 如果累计文本为空，广播占位文本"好的，开始处理。"（仅 UI 提示，不计为真实输出）
      - 执行每个工具（通过 ToolExecutor，120s 超时）
      - 追加 assistant(tool_calls) + tool(result) 到消息列表
      - 广播 tool_use + tool_result 事件
      - 递增 round_num，回到步骤 a
   h. 如果无 tool_calls：
      - 检查是否有真实文本（排除占位符）
      - 无真实文本 → 返回 :empty 触发空响应重试
      - 有真实文本 → 剥离占位符前缀，流式结束
9. 最终化：更新助手消息（content + tool_calls + is_streaming=false），广播 done 事件
10. 强制清理：try/after 确保 is_streaming 一定被清除（即使崩溃）
```

## 状态机

### Tool Loop 状态

| 当前状态 | 触发条件 | 目标状态 | 动作 |
|---|---|---|---|
| idle | 收到 stream 调用 | streaming | 创建 placeholder，广播 start |
| streaming | LLM 返回 text + 无 tool_calls | done | 保存最终内容，广播 done |
| streaming | LLM 返回 tool_calls | executing_tools | 执行工具，广播 tool_use/tool_result |
| executing_tools | 所有工具执行完毕 | streaming | 追加结果到消息，重新请求 LLM |
| streaming | 达到最大轮次 | done | 无工具调用 LLM 生成总结 |
| streaming | LLM 返回空响应 | empty_retry | 触发退避重试 |
| streaming | HTTP 错误（429/5xx） | retrying | 指数退避重试（最多 3 次） |
| streaming | 超时 | retrying | 退避重试 |
| any | 熔断器 open | fallback_or_error | 切换 fallback provider 或返回错误 |
| done/empty_retry/error | — | idle | 强制清除 is_streaming 标志 |

### 熔断器状态

| 当前状态 | 触发条件 | 目标状态 | 动作 |
|---|---|---|---|
| closed | 连续失败 ≥ 3 次 | open | 记录 opened_at，启动 60s 冷却 |
| open | 冷却时间过 + 新请求 | half_open | 当前调用者成为探针 |
| half_open | 探针成功 | closed | 重置失败计数 |
| half_open | 探针失败 | open | 重新计时 |
| half_open | 探针进程崩溃 | open | 释放探针锁，重新计时 |
| open | 新请求 + 冷却未过 | fallback | 返回 fallback provider |

## 错误处理

| 错误场景 | 处理方式 | 重试策略 | 升级策略 |
|---|---|---|---|
| HTTP 429（限流） | 指数退避重试 | 最多 2 次，解析 Retry-After header | 重试耗尽 → 熔断器计数+1 → 返回用户友好错误 |
| HTTP 5xx（服务端） | 指数退避重试 | 最多 2 次 | 同上 |
| HTTP 401（认证失败） | 不重试 | — | 返回"API Key 无效" |
| 请求超时（30s 无响应） | 重试 | 最多 3 次 | 返回"请求超时" |
| 流式 idle 超时（首 chunk 90s / 后续 60s） | 中断流，重试 | 最多 2 次 | 归类为网络错误 |
| 空响应（无文本无 tool_calls） | 指数退避重试 | 最多 3 次（5s/15s/45s） | 重试耗尽 → 通知上级 agent |
| 上下文溢出 | trim_context_if_needed | — | 修剪后继续 |
| 连续无文字轮次（3 轮只调工具不出文字） | 注入系统提示 | — | 提示"请输出文字描述你的操作" |
| doom loop（同一工具+同一参数 3 次） | 中断循环 | — | 返回错误 |
| 熔断器 open | 切换 fallback provider | — | 无 fallback → 返回"所有 provider 不可用" |

> **[RECONCILE 补充] 上下文溢出的两道防线 — trim_context vs compaction 职责边界**
>
| 机制 | 所在模块 | 触发时机 | 触发条件 | 同步/异步 | 行为 |
|---|---|---|---|---|---|
| `trim_context_if_needed` | Streamer（本契约） | tool loop 每轮 LLM 请求**前** | 估算 token > usable budget（硬溢出） | **同步** — 阻塞当前轮 | 保留首 2 条 + 末 N 条，中间消息尝试 LLM 摘要（`compact_with_llm`），失败则从前端硬截断。摘要存为 system 消息 |
| `maybe_trigger_compaction` | ConversationStore（契约 03） | `append_turn` 后（每轮结束**后**） | total > budget * 0.85（软阈值，预防性） | **异步** — 不阻塞主流程 | 分割 old/recent，LLM 摘要 old 消息为结构化 handoff，前置 system 摘要消息。失败回退 `trim_to_budget` |
>
> **关键区别**：trim_context 是**轮内同步硬溢出处理**（即将超限时紧急裁剪），compaction 是**轮后异步预防性压缩**（85% 时提前压缩避免未来溢出）。两者独立运行，compaction 的摘要不会传递给 trim_context（因 clean_messages 过滤 system 消息，见契约 03 已知问题）。Python 实现应保留这两道防线，但建议让 compaction 摘要通过独立的 `compacted_prefix_cache` 传递，而非混入 history。

## 常量引用

| 常量 | 值 | 所在章节 |
|---|---|---|
| 单轮工具数上限 | `5` | 工具执行 |
| 工具执行超时 | `120_000` ms | 工具执行 |
| `@request_timeout_ms`（Elixir） | `30_000` ms | LLM 调用超时（Elixir） |
| `@stream_idle_ms`（Elixir） | `30_000` ms | LLM 调用超时（Elixir） |
| 请求级超时（TS 防线①） | `180_000` ms | LLM 调用超时（TS 三层防线） |
| 首 chunk 超时（TS 防线②） | `90_000` ms（可 env 覆盖 `HW_STREAM_FIRST_MS`） | 同上 |
| 后续 chunk idle 超时（TS 防线②） | `60_000` ms（可 env 覆盖 `HW_STREAM_IDLE_MS`） | 同上 |
| Turn 级超时（TS 防线③） | `300_000` ms | 同上 |
| `safety_timeout`（Elixir） | `600_000` ms（10 分钟） | 同上 |
| 空响应重试退避 | `5s / 15s / 45s` | 同上 |
| mid-round reminder | `80%` 轮次时注入 | 同上 |
| 连续无文字轮次 | `3` 轮 | 同上 |
| `MAX_RETRIES` | `2` | 重试与熔断 |
| 可重试状态码 | `429, 503, 504, 529` | 同上 |
| 退避策略 | 指数退避 + `[0.8, 1.2]` jitter | 同上 |
| 熔断器三态 | `closed / open / half_open` | 同上 |
| 熔断器失败阈值 | `3` 次 | 同上 |
| 熔断器冷却时间 | `60_000` ms | 同上 |
| `COMPACTION_BUFFER` | `20_000` | Token 预算与压缩 |
| `OUTPUT_TOKEN_MAX` | `32_000` | 同上 |
| `MAX_TURNS`（安全上限） | `200` | — |
| `DOOM_LOOP_THRESHOLD` | `3` | — |
| 最大 tool 轮次（CEO） | `60` | — |
| 最大 tool 轮次（HR） | `40` | — |
| 最大 tool 轮次（coordinator/manager） | `50` | — |
| 最大 tool 轮次（executor） | `80` | — |
| 默认占位文本 | `"好的，开始处理。\n"` | — |
| `temperature` | `0.7` | — |
| Finch pool_size | `20` | Finch HTTP 客户端 |

## 已知问题

| 问题编号 | 说明 | Python 迁移处理 |
|---|---|---|
| E1 | MCP 简化为 HTTP-only | 不影响本模块 |
| C2 | 端口不一致 | 已确认用 4000 |
| — | Elixir `@default_tail_turns 20` 是 dead code | 忽略，不迁移 |
| — | Elixir 超时值（30s）与 TS 三层防线（180s/90s/60s/300s）差异大 | Python 采用 TS 三层防线模型（更完善），见决策依据 |
| — | **[RECONCILE 修正]** 原 error handling 表写"首 chunk 120s"是将工具执行超时（`streamer.ex:565` `Task.yield(task, 120_000)`，用于并行工具）误当作首 chunk 超时。实际 Elixir 首 chunk/stream idle 超时为 `@stream_idle_ms = 30_000`（30s）；TS 首 chunk 为 90s。Python 采用 TS 值 90s | 已统一为 90s |

> **超时值决策说明**：Elixir `streamer.ex` 使用单一 `@request_timeout_ms = 30_000` + `@stream_idle_ms = 30_000`，而 TS 参考实现有三层防线（180s 请求级 / 90s 首 chunk / 60s idle / 300s turn 级）。TS 的三层防线更完善：(1) 区分"首 chunk 等待"和"后续 chunk 间隔"；(2) 有 turn 级总超时兜底。Python 迁移采用 TS 三层防线模型。Elixir 的 30s 过于激进，thinking 模型（如 o1）首 token 可能需要 60-90s。

## Python 实现建议

- **框架/库**：
  - HTTP 客户端：`httpx`（async + streaming），或 `aiohttp`
  - SSE 解析：手动解析 `data: ...` 行，或用 `httpx-sse` 库
  - 不用 Vercel AI SDK（TS 专用），Python 侧直接调 OpenAI 兼容 API
  - 可选：`openai` Python SDK 的 `AsyncOpenAI` + `stream=True`，但需确认它支持 tool_calls streaming

- **架构模式**：
  - `async def stream(agent, message, opts) -> AsyncGenerator[StreamEvent, None]`
  - tool loop 用 `while` 循环，每轮 `async for chunk in sse_stream` 逐 chunk yield
  - 熔断器：可用 `pybreaker` 库，或自建三态机（状态少，自建更可控）
  - 超时：`asyncio.wait_for` + `httpx.AsyncClient.timeout`，三层防线分别用不同的 timeout 值
  - 重试：`tenacity` 库，或自建（需解析 Retry-After header）

- **注意事项**：
  - 占位文本"好的，开始处理。"只在 UI 广播，不计入 LLM 真实输出，最终保存时剥离
  - `is_streaming` 标志的强制清理必须用 `try/finally`，且需重试机制（DB 连接可能瞬断）
  - reasoning_content 和 content 必须分通道，不能混用（reasoning 是模型内心独白，可能是英文）
  - doom loop 检测：记录最近 3 次 (tool_name, arguments) 对，检测重复
  - max_tool_rounds 按角色区分，Python 侧用 dict 映射
  - 中轮提醒在 80% 轮次时注入，不是硬性截断

## 验收标准

- [ ] 发起流式请求后，前端能逐 token 收到 text_delta 事件
- [ ] LLM 返回 tool_calls 时，前端收到 tool_use 事件，工具执行后收到 tool_result 事件
- [ ] 工具执行后，自动重新请求 LLM，循环直到无 tool_calls
- [ ] 达到最大轮次时，做一次无工具的总结调用
- [ ] 流式结束（成功/失败/崩溃）后，`is_streaming` 标志一定被清除
- [ ] HTTP 429/5xx 自动重试，最多 2 次，解析 Retry-After header
- [ ] 流式 idle 超时（首 chunk 90s / 后续 60s）自动中断并重试
- [ ] 空响应（无文本无 tool_calls）触发退避重试（5s/15s/45s）
- [ ] 连续 3 轮只调工具不出文字时，注入系统提示
- [ ] 同一工具+同一参数连续 3 次时，中断 doom loop
- [ ] 上下文溢出时自动修剪历史消息
- [ ] 熔断器三态正确工作（closed/open/half_open）
- [ ] reasoning_content 和 content 分通道处理
- [ ] 占位文本不被计入 LLM 真实输出
- [ ] 每轮 LLM 调用记录审计日志（token 用量、finish_reason）
- [ ] 中轮提醒在 80% 轮次时注入

## 并行对比测试方案

| 测试场景 | Elixir 行为 | Python 预期行为 | 验证方法 |
|---|---|---|---|
| 简单文本对话（无工具） | 流式返回文本，保存 1 条 assistant 消息 | 相同 | 发送"你好"，对比 SSE 事件序列和 DB 消息 |
| 工具调用对话 | 流式返回 text + tool_calls，执行工具，重新请求 | 相同 | 发送需要调工具的消息，对比工具执行结果和后续 LLM 响应 |
| 多轮工具调用 | 循环执行工具直到完成 | 相同 | 发送复杂任务，对比轮次数和最终结果 |
| HTTP 429 限流 | 重试 2 次，退避 | 相同 | mock API 返回 429，对比重试次数和退避时间 |
| 流式超时 | 30s 超时（Elixir）| 90s 首 chunk / 60s idle 超时（Python） | mock API 不响应，对比超时时间（注意：值不同是预期的） |
| 空响应 | 退避重试 3 次 | 相同 | mock API 返回空 content + 空 tool_calls，对比重试行为 |
| 最大轮次 | 达到上限后做总结调用 | 相同 | mock LLM 每轮都返回 tool_calls，对比总结调用 |
| 熔断器 | 连续 3 次失败后 open | 相同 | mock 连续失败，对比熔断器状态转换 |
| 上下文溢出 | 修剪历史消息 | 相同 | 发送超长历史，对比修剪后的消息列表 |
| 页面刷新 | placeholder 消息可见 | 相同 | 流式中刷新页面，确认能看到正在进行的消息 |

---

> **填写规则**：
> 1. 用 spec 语言，不用代码语言。说"做什么"，不说"怎么做"。
> 2. 所有常量值引用 `constants.md`，不在此处重复定义。
> 3. 所有已知问题引用 `known-issues.md`，不在此处重复描述。
> 4. 每个契约必须经过用户确认后才能标记为"已确认"。
> 5. "Python 实现建议"是建议不是约束，实现者可以根据情况调整，但必须满足验收标准。
