# 常量与不变量（层 2）

> **本文件记录所有需要精确复制的常量、配置值、不变量。** 这些是设计决策，不是实现细节，迁移到 Python 时必须保持一致。
> 每个常量必须标注来源（Elixir 文件路径 或 TS 文件路径）和精确值。

## 游戏时间

| 常量 | 值 | 来源 | 说明 |
|---|---|---|---|
| `REAL_SECONDS_PER_GAME_DAY` | `3600`（1 真实小时 = 1 游戏天） | `packages/shared/src/game-time.ts:2` + `apps/hiveweave/lib/hiveweave/game_time/server.ex:17` | 游戏时间与真实时间的换算 |
| 游戏秒/天 | `86400`（标准 24h 分解） | 同上 | 游戏时间使用标准 24 小时制 |
| Game time tick 间隔 | `5` 秒 | `apps/hiveweave/lib/hiveweave/game_time/server.ex` + `apps/server/src/game-time-scheduler.ts` | 模拟时钟推进间隔 |
| 停滞检测间隔 | `60` 秒（Elixir） | `apps/hiveweave/lib/hiveweave/game_time/server.ex` | 每 60 秒检查一次 agent 停滞 |
| 停滞阈值（processing） | `5` 分钟 | 同上 | processing 状态超 5 分钟触发升级 |
| 停滞阈值（idle） | `10` 分钟 | 同上 | idle 状态超 10 分钟触发升级 |
| 停滞升级 cooldown | `10` 分钟 | 同上 | per-agent，防止重复升级 |
| TS 停滞阈值 | `15` 分钟 | `apps/server/src/game-time-scheduler.ts:71` | TS 实现的 idle 阈值（单一值，不区分状态） |

> ✅ **已确认（以 OpenCode 为准）**：OpenCode 是 CLI 工具，无停滞检测机制（无 server 常驻进程）。采用 **Elixir 的双阈值模型**（processing 5min / idle 10min），因为：(1) Elixir 是 active backend 且逻辑更完善；(2) 区分"卡在工作中"和"完全没心跳"能减少误报。TS 的 15 分钟单一阈值是简化实现，不采用。

## Token 预算与压缩

> **对照说明**：OpenCode（`packages/core/src/session/compaction.ts`）是 HiveWeave TS 参考实现的源头。OpenCode 的 compaction 采用 token-budget 而非 turn-count，但 `tail_turns` 仍作为 config 项暴露（默认 2）。HiveWeave Elixir 在此基础上调大了部分值。

| 常量 | 值 | 来源 | OpenCode 对照 | 说明 |
|---|---|---|---|---|
| `@tail_turns` / `DEFAULT_TAIL_TURNS` | `4`（Elixir）/ `2`（TS） | `conversation_store.ex:33` / `token-utils.ts:65` | `2`（`config.ts:156` 默认值） | compaction 时保留的完整 turn 数 |
| `@prune_protect_tokens` | `40_000` | `conversation_store.ex:35` | —（OpenCode 无此概念，用 `keep.tokens` 替代） | 保护最近 40K tokens 的工具输出不被裁剪 |
| `@prune_minimum_tokens` | `20_000` | `conversation_store.ex:37` | —（同上） | 裁剪下限 |
| `@tool_output_max_chars` | `2_000` | `conversation_store.ex` | `2_000`（`compaction.ts:14`） | 单条工具输出截断阈值 |
| `COMPACTION_BUFFER` | `20_000` | `token-utils.ts` | `20_000`（`compaction.ts:12` `DEFAULT_BUFFER`） | 输出预留 |
| `OUTPUT_TOKEN_MAX` | `32_000` | `token-utils.ts` | —（OpenCode 用 model 配置，无全局硬上限） | 输出硬上限 |
| `PRESERVE_RECENT_MIN` | `2_000` | `token-utils.ts:61` | —（OpenCode 用单一 `DEFAULT_KEEP_TOKENS=8_000`） | compaction 保留近期消息预算下限 |
| `PRESERVE_RECENT_MAX` | `8_000` | `token-utils.ts:62` | `8_000`（`compaction.ts:13` `DEFAULT_KEEP_TOKENS`） | compaction 保留近期消息预算上限 |
| CJK token 估算 | `1.5` 字符/token | `token-utils.ts` | —（OpenCode 用 `CHARS_PER_TOKEN=4`，不区分 CJK） | 中日韩 token 估算 |
| 非 CJK token 估算 | `4` 字符/token | `token-utils.ts` | `4`（`util/token.ts:3` `CHARS_PER_TOKEN`） | 拉丁字符 token 估算 |
| 估算精度 | `±15%` | `token-utils.ts` | —（同算法） | char-ratio 启发式的精度 |

> ✅ **已确认（以 OpenCode 为准）**：`tail_turns` 采用 **2**（OpenCode 默认值）。理由：(1) OpenCode 是成熟项目，其默认值经过大量验证；(2) OpenCode 的 compaction 核心逻辑是 token-budget 而非 turn-count，`tail_turns=2` 只是辅助保留最近交互的完整 turn，更大的值会保留过多旧上下文反而稀释 token 预算；(3) HiveWeave Elixir 的 `4` 是自行调大的值，但 Elixir 的 compaction 逻辑实际上也以 token-budget 为主（`@prune_protect_tokens`），`tail_turns` 只是回退方案的边界。Python 迁移采用 OpenCode 的 `2`，与 token-budget 主逻辑配合更好。
>
> **补充说明**：Elixir `token_utils.ex` 中还有一个 `@default_tail_turns 20`（`token_utils.ex:14`），但该值未被 `conversation_store.ex` 使用（后者用自己的 `@tail_turns 4`），属于 dead code，忽略。

## 工具执行

| 常量 | 值 | 来源 | 说明 |
|---|---|---|---|
| 单轮工具数上限 | `5` | `streamer.ex:488` | 每轮 LLM 调用最多执行 5 个工具 |
| 工具执行超时 | `120_000` ms（120 秒） | `streamer.ex:565`（`Task.yield(task, 120_000)`） | 单个工具执行超时 |
| 工具输出存文件阈值 | `>2000 行 或 50KB` | `tool_executor.ex` + `tool-output-store.ts` | 超阈值存临时文件，返回预览 |
| 工具输出临时文件保留 | `7` 天 | 同上 | 自动清理周期 |
| 大输出预览格式 | head 60% + tail 40% + 截断标记 | `token-utils.ts` `truncateToolOutput` | 超 4000 字符触发 |

## LLM 调用超时（TS 三层防线）

| 常量 | 值 | 来源 | 说明 |
|---|---|---|---|
| 请求级超时 | `180_000` ms（180 秒） | `agent-runtime.ts` `timeoutFetch` | 级别 ①：HTTP 请求 AbortSignal |
| 首 chunk 超时 | `90_000` ms（90 秒） | `stream-timeout.ts` `withIdleTimeout` | 级别 ②：首 chunk 容忍 90s（thinking 模型延迟） |
| 后续 chunk idle 超时 | `60_000` ms（60 秒） | 同上 | 级别 ②：后续 chunk 间隔超 60s 触发 |
| Turn 级超时 | `300_000` ms（300 秒） | `agent-runtime.ts` `HW_TURN_IDLE_MS` | 级别 ③：单轮总超时 |

## LLM 调用超时（Elixir）

| 常量 | 值 | 来源 | 说明 |
|---|---|---|---|
| `safety_timeout` | `10` 分钟（600 秒） | `agent.ex:438` | agent 级安全超时 |
| 空响应重试退避 | `5s / 15s / 45s` | `agent.ex:497` | 指数退避，最多 3 次 |
| mid-round reminder | `80%` 轮次时注入 | `streamer.ex:276` | 提示"开始收尾" |
| 连续无文字轮次 | `3` 轮 | `streamer.ex:622` | 只调工具不出文字 → 注入系统提示 |

## 重试与熔断

| 常量 | 值 | 来源 | 说明 |
|---|---|---|---|
| `MAX_RETRIES` | `2` | `retry-utils.ts` | 最多重试 2 次 |
| 可重试状态码 | `429, 503, 504, 529` | 同上 | — |
| 退避策略 | 指数退避 + `[0.8, 1.2]` jitter | 同上 | Retry-After 优先 |
| overflow 正则数 | `18` 条 | 同上 | 覆盖 OpenAI/Anthropic/Google/DeepSeek |
| 熔断器三态 | `closed / open / half_open` | `circuit_breaker.ex` | Elixir 特有，TS 无对应 |

## 数据库

| 常量 | 值 | 来源 | 说明 |
|---|---|---|---|
| Meta DB 路径 | `packages/db/data/hiveweave.db`（可 `HIVEWEAVE_DB_PATH` 覆盖） | `packages/db/src/client.ts:10` | 全局 DB |
| Per-project DB 路径 | `<workspace>/.hiveweave/data.db` | 同上 | 每项目一个 |
| Meta DB journal mode | `WAL` | 同上 | 全局 DB 用 WAL |
| Per-project DB journal mode | `DELETE` | 同上 | 避免 Windows `SQLITE_IOERR_SHMOPEN` |
| Per-project DB busy_timeout | `5000` ms | `project_factory.ex:334` | `5000`（`database.ts:29`） | Elixir 和 OpenCode 一致 |
| Per-project DB pool_size | `5`（Elixir）/ 单连接（TS） | `project_factory.ex:332` / `client.ts` | 单连接（Effect SqlClient） | ⚠️ 见下方确认 |
| Agent 短 ID 格式 | `A001, A002, ...` 递增 | `org-service.ts` `generateNextShortId` | —（OpenCode 用 UUID） | 节省 token |

> ✅ **已确认（以 OpenCode 为准）**：Per-project DB 采用 **单连接**（对齐 OpenCode/TS）。理由：(1) OpenCode 使用 Effect `SqlClient`（`database.ts:27`），本质是单连接串行执行；(2) SQLite 是单写者模型，多连接并不能提升写并发，读并发在 100 agent 量级下通过 async 调度已足够；(3) aiosqlite 本身就是单连接异步包装，与 OpenCode 的 Effect SqlClient 模型一致；(4) 单连接避免了 SQLite 的 `SQLITE_BUSY` 错误和锁竞争问题。Meta DB（全局）可考虑小 pool（如 3）用于读并发，但 per-project DB 保持单连接。

## 前缀缓存优化（DeepSeek）

| 常量 | 值 | 来源 | 说明 |
|---|---|---|---|
| 消息布局 | identity(静态) → context(静态) → history → dynamicContext(合并到 user) | `agent-runtime.ts` | 98%+ 缓存命中率 |
| 前缀哈希算法 | FNV-1a 32-bit | `token-utils.ts` `computePrefixHash` | 检测前缀缓存漂移 |
| Anthropic 缓存提示 | inline cache hints | `agent-runtime.ts:1057` `applyCacheHints` | Anthropic/Bedrock 专用 |

## 环境变量

| 变量 | 默认值 | 来源 | 说明 |
|---|---|---|---|
| `HIVEWEAVE_DB_PATH` | `packages/db/data/hiveweave.db` | `client.ts:10` | 覆盖 meta DB 路径 |
| `PORT` | `4000`（Elixir，`config/dev.exs:4`）/ `3200`（TS） | — | ✅ 已确认：Python 用 4000，前端零改动 |
| `HIVEWEAVE_DIAG` | 未设置 | — | `1` 或 `true` 启用 verbose 调试日志 |
| `BASH_SANDBOX` | 未设置 | — | `docker` 启用沙盒 |
| `OPENCODE_API_KEY` | — | `seedDefaultModel` | 种子模型用 |
| `HTTPS_PROXY` | — | — | 受限网络代理 |

> ✅ **已确认（以 OpenCode 为准）**：Python 后端采用 **端口 4000**。理由：(1) OpenCode 是 CLI 工具无端口概念，不构成对照；(2) 前端 `api.ts:22` 已硬编码 `ws://localhost:4000/socket`，REST 走 `/api` 代理；(3) Python 是**替换** Elixir 不是并行，接管 4000 端口最自然，前端零改动。如需并行对比测试，Python 可临时用 4001，通过环境变量 `PORT=4001` 覆盖。

## 模型种子

| 模型 | endpoint | 用途 | 来源 |
|---|---|---|---|
| DeepSeek V4 Flash Free | `https://opencode.ai/zen/v1` | 免费默认模型，200K context | `seedDefaultModel` |
| DeepSeek V4 Flash Paid | `https://opencode.ai/zen/v1` | 付费模型，1M context | 同上 |

## ETHOS 提示词

| 常量 | 值 | 来源 | 说明 |
|---|---|---|---|
| 三原则 | Boil the Lake / Search Before Building / User Involvement | `streamer.ex` `build_identity_prompt` | 注入所有角色共享前言 |
| User Involvement 级别 | `high / medium / low` | charter `userInvolvement` 字段 | high=全问用户，medium=技术自主+产品必问，low=仅通知 |
| 角色纪律四件套 | 何时不做 / 输出格式 / 验证清单 / 反合理化表 | 每个角色必备 | — |
| 组织范式 | `solo / flat_squad / tech_lead / pm_architect / pod / pipeline`（6 种） | — | 各注入必经流程 |

## Finch HTTP 客户端

| 常量 | 值 | 来源 | 说明 |
|---|---|---|---|
| pool_size | `20` | `application.ex` | Finch 连接池大小 |
