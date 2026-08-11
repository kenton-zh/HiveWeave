# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Rules

- **遇到问题要反思**：为什么会有这个问题？从宏观、治根的角度修复问题，不要“打地鼠”式地修补表面。

- **机制定位：疏通引导优先，堵截只是兜底**（2026-08-03 用户钦定）：任务流程里的机制（工具、提示、脚本、门禁）首要作用是**疏通与引导**——让 agent 走对路径（工具契约清晰、提示可执行、状态可查）；**堵截**（gate 拒绝、硬校验、驳回）只是**兜底**，永远不能拿堵截当疏通用。判断标准：如果发现“agent 反复撞门”（gate 弹回、rework 循环、同一提示反复出现），先问“为什么它会走错”——根因几乎总在工具/提示/状态可见性（疏通层）缺位，而不是“堵得不够狠”。堵截造成的往返成本（每撞一次门 = 一轮完整上下文）远高于它防住的犯错成本。反例（TEST18 第二轮）：柚子 6 次重复回执（ask_agent 无 reply_to 参数 + gate 提示无合同详情）、Vera 把 taskId 写进命令文本（工具参数发现性不足）——都是堵截在治自己制造的病。

### 工具输出：截断前化解，截断只是最后兜底

**触发截断本身就说明上游已经漏了**——正常路径不该产出需要截断的东西。分层（不可颠倒）：

1. **上游（治本）**：工具/服务在返回前就把结果收成可读短契约——限条数、限每条描述长度、按行结构化、禁单行 JSON dump / HTML 残片进对话。Agent 需要细节时再按需 `read_skill` / `read_file`，不要一次灌全文。
2. **中游**：大结果落盘（`.hiveweave/tool_outputs/`），回传短摘要 + 文件句柄；不要把 50KB+ 正文塞进 chat / `conversation_turns`。
3. **下游（兜底）**：`_maybe_save_large_output` / `truncate_tool_output` 是安全带，不是主药方。预览必须**按行 + 按字符双封顶**（单行超长输出不能击穿 head/tail 行截断；阈值触发仍按字节，预览预算按字符）。修截断阈值 ≠ 修根因；发现截断被打穿时，先查是哪个工具产出了病态大输出。

反例（TEST19 晚轮）：`list_available_skills` 把 skills.sh 详情页 HTML 残片当 summary → 单行 73KB → 行截断形同虚设 → 天线 turn ≈20k tokens。药方顺序：先收缩技能列表输出契约，再硬化截断兜底。

### 跨平台 AI 项目记忆（本地，不进远程）

路径：**仓库根目录 [`AI_MEMORY.local.md`](AI_MEMORY.local.md)**（已 gitignore）。

凡参与本项目的编程 Agent（Claude Code / Cursor / Codex / OpenCode / Trae / 其他平台），在写入「项目记忆 / memory」时：
- **不必遵守**各平台自带的记忆路径或存储规则
- **统一读写本文件**；跨会话、跨平台复用同一份记忆
- 个人环境配置仍写 [`CLAUDE.local.md`](CLAUDE.local.md)；本文件只放可复用的项目事实（决策、坑、根因、未完成上下文），禁止密钥
- **写入规范**：只记根因 / 仍在生效的决策 / 可预见的坑 / 未完成上下文；不记测试通过数、提交哈希、过程流水（提交记录可查）。修复完成的事务就地更新原条目，不追加；同主题条目 ≥3 或文件膨胀时按主题压缩合并（沿用文件内「速览 / 关键决策 / 高频踩坑 / 未完成」结构）

## Commands

```bash
# 前端 (Node.js + pnpm)
pnpm install              # 安装前端依赖
pnpm dev                  # 启动 web dev server (turbo, port 5173)
pnpm build                # 构建 web

# 后端 (Python + uvicorn)
cd apps/hiveweave-py
uv sync                   # 安装 Python 依赖 (或 pip install -e .)
uvicorn hiveweave.main:app --host 0.0.0.0 --port 4000 --limit-concurrency 100 --backlog 2048 --timeout-keep-alive 30

# 或用启动脚本 (Windows)
start-all.bat             # 后端 4000 + 前端 5173
start-backend.bat         # Python/FastAPI, 端口 4000
start-frontend.bat        # React/Vite, 端口 5173

# 类型检查（提交前必须跑）
uv run mypy src/hiveweave/ --ignore-missing-imports

# 回归测试（提交前必须跑）
cd apps/hiveweave-py && uv run pytest tests/ -v
```

### Node version

前端需要 Node `>=22.0.0 <24.0.0`。系统同时装有 Node 24 (全局) 和 Node 22 (便携版, `%LOCALAPPDATA%\Programs\node-v22.20.0-win-x64`)。运行 pnpm/node 命令前，将 Node 22 置于 PATH 最前：

**PowerShell（本机默认；可直接粘贴）：**

```powershell
$env:PATH = "$env:LOCALAPPDATA\Programs\node-v22.20.0-win-x64;$env:PATH"
node -v   # 应落在 v22.x
```

**bash / Git Bash / POSIX：**

```bash
export PATH="$LOCALAPPDATA/Programs/node-v22.20.0-win-x64:$PATH"
node -v
```

Windows 也可用 `start-frontend.bat` / `start-all.bat`（脚本内已处理环境时不必手改 PATH）。

## Architecture

### 项目结构

```
apps/hiveweave-py/     @hiveweave Python 后端 — FastAPI (port 4000)
apps/web/              @hiveweave/web   React 19 + Vite + React Flow (port 5173)
```

后端是纯 Python,前端是纯 React。前端通过 pnpm workspace 管理,后端通过 uv 管理。

### Python 后端 (`apps/hiveweave-py/`)

FastAPI + uvicorn,运行在端口 4000。核心模块:

| 目录 | 职责 |
|------|------|
| `src/hiveweave/api/` | FastAPI 路由 (16 个模块, 122 路由) |
| `src/hiveweave/agents/` | Agent + Supervisor + trigger |
| `src/hiveweave/llm/` | LLM 流式调用 (`streamer/` 包, provider, retry, circuit_breaker) |
| `src/hiveweave/tools/` | 工具执行器 + 5 个 legacy 评审套件 |
| `src/hiveweave/services/` | 业务服务 (org, dispatch, memory, handoff, skill_registry, turn_*, `git_worktree/` 包, `tasks/` 包 + `task.py` shim, game_time, chat_message, inbox_triage, ...) |
| `src/hiveweave/hooks/` | Lifecycle hooks（OpenCode 风格 registry + points） |
| `src/hiveweave/conversation/` | 对话历史 + token budget + compaction |
| `src/hiveweave/db/` | Meta DB + per-project DB (aiosqlite) |
| `src/hiveweave/realtime/` | WebSocket (phoenix_adapter, channels, pubsub, event_bus) |
| `src/hiveweave/prompts/` | ETHOS 提示词体系 (coordinator, executor, charter) |
| `src/hiveweave/config.py` | pydantic-settings 配置 |
| `src/hiveweave/main.py` | FastAPI app + lifespan |

### Dual-DB pattern

两层 SQLite:

1. **Meta DB** (`apps/hiveweave-py/data/hiveweave.db`, DELETE journal mode) — 全局表: `projects`, `agent_templates`, `llm_models`, `global_settings`, `mcp_servers`, `meta_index`。每个服务器进程一个。（旧 `agent_index`/`permission_rules` 等表已移除/废弃，迁移时 DROP — 见 `db/meta.py:_LEGACY_TABLES_TO_DROP`）
2. **Per-project DB** (每个工作区 `.hiveweave/data.db`, DELETE journal mode) — 项目级表: `agents`, `memories`, `chat_messages`, `handoffs`, `inbox`, `work_logs` 等。按工作区隔离。DELETE 模式为避免 Windows WAL 孤儿化/代际分叉损坏（TEST18 2026-08-05 根因）；单后端 + per-workspace 写锁 + busy_timeout 串行化并发访问。

`agent_id → project_id` 路由由 `AgentRouter`（`services/agent_router.py`）内存映射完成，启动时遍历所有 per-project DB 重建路由表；`create_agent`/`delete_agent` 时同步更新。完整 agent 数据（name, role, skills 等）在 per-project DB 的 `agents` 表中。

`ensureProjectDb(workspace_path)` 懒创建 per-project DB。

> ADR: [004-dual-db-pattern](docs/adr/004-dual-db-pattern.md)（路由机制部分已被 [006-agent-router-routing](docs/adr/006-agent-router-routing.md) supersede）

### LLM 流式调用

`apps/hiveweave-py/src/hiveweave/llm/streamer/` — 包（`__init__.py` 再导出；旧单体 `streamer.py` 已拆）。httpx 流式 SSE，支持多 provider。同步 httpx 在线程池解析 SSE 后经 queue **边收边推** `_fire_delta`（真流式，避免整包收完才刷新导致 UI 冻住假象）:
- 包内域模块：`core.py` / `http_stream.py` / `tool_loop.py` / `sse.py` / `doom_loop.py` / `constants.py` 等；外部仍 `from hiveweave.llm.streamer import …`
- `provider.py`: provider 工厂,映射 `openai`/`anthropic`/`google` 到对应 API
- `retry.py`: 429/503/504/529 重试,指数退避 + jitter,解析 `Retry-After`
- `circuit_breaker.py`: 熔断器,探针锁防止多 Agent 同时冲击不稳定 API
- Token 估算: char-ratio 启发式 (4 chars/token EN, ~1.5 CJK),预留 20K compaction buffer
- 思考模式 (thinking/reasoning): 由 `llm_models` 表的 `supports_thinking` 和 `default_reasoning_effort` 控制,所有 LLM 调用统一生效（不区分用户对话 vs agent 间对话）
- `CONTINUE_SENTINEL`：请求末尾非 user 时追加到 **HTTP 副本**的静态 user 文案（修 gateway tool_call id 400；并写明「回合未收口故再次唤醒 / 非人类新指令」）。不写回持久化历史
- **Doom-loop 防护**：同一工具连续重复调用触发熔断。只读轮询工具豁免 —— `DOOM_LOOP_READONLY_TOOLS`（17 个：get_tasks/read_file/list_subordinates 等）走 `DOOM_LOOP_READONLY_FUSE=15` 保险丝而非默认 3 次；唯一入口 `doom_loop_limit(tool_name)`，容忍度表 `DOOM_LOOP_TOOL_LIMITS`
- 全局 LLM 并发上限 `_LLM_MAX_CONCURRENT`（env `HIVEWEAVE_LLM_MAX_CONCURRENT`，默认 8）；`TOTAL_TIMEOUT_S=540`（env `HIVEWEAVE_STREAM_TOTAL_TIMEOUT_S`；给 agent safety_timeout 600s 留 60s 余量）
- **连续流式总超时**：同 agent `_stream_timeout_streak ≥ 2` → `_park_after_stream_timeouts`（disposition waiting + wait `stream_total_timeout_recovery` + 升级上级，不自动 approve）
- **模型分级 + 同级故障切换**（`services/model.py` + `services/policy.py`）：两级 tier — `management`（CEO/Coordinator，好模型）/ `executor`（Executor/QA/HR，便宜模型）。每级两槽位：primary + backup，`global_settings` 四键配置（`model_tier_{management|executor}_{primary|backup}`，值存 model_id 或 UUID）。`resolve_model(tier, skip_model_ids)` 严格按 primary→backup→tier列→legacy pool 解析，**禁跨级**。`model_tier_for_agent(agent)` 由 `infer_role_family` 映射。首次 429/5xx 同 turn 自动切同级 backup（`_resolve_failover_backup`，同 api_key 跳过）；streamer circuit fallback 校验 tier 一致性。`pick_from_pool` 全池 RR 已降级为无 tier 配置时的兼容回退。`ensure_channel_models` 仍按名 upsert Ark Plan/Coding 双渠道；`is_rate_limit_error` 命中的 429 不计入放弃、独立冷却 `RATE_LIMIT_RESUME_COOLDOWN_S=120` 后 resume（`agents/agent.py`）

> ADR: [008-model-tiering-failover](docs/adr/008-model-tiering-failover.md)

### 文件拆分纪律（包化）

偏好域模块（约 200–800 行）。**不要**往壳/shim 堆新逻辑：
- shim / 桶：`services/task.py` → `services/tasks/*`；`services/git_worktree/` 与 `llm/streamer/` 的 `__init__.py` 只做再导出；`agents/agent.py`、`tools/task_tools.py`、前端 `ChatPanel.tsx` / `api.ts` 同理
- 新逻辑进域模块：`services/tasks/*`、`services/git_worktree/{service_*,reconcile,ensure,…}`、`llm/streamer/{core,tool_loop,http_stream,…}` 等

**原子可评审**：单体 → 包时，旧文件删除与全部替代文件必须同一提交边界（可评审 diff / CI 能同时看到删除与替代）；`__init__` 保留兼容导出。验收不能只靠 import smoke（如 `test_p3_split_import_smoke.py`）——至少映射或补公共入口的正向 + 一条失败/超时/恢复用例。

### 对话管理

`apps/hiveweave-py/src/hiveweave/conversation/store.py`:
- **Token-budget 裁剪**: 按 token 预算裁剪历史,不按消息数。Turn 级裁剪 — 不拆分 `assistant(tool_calls)` / `tool(result)` 对
- **智能压缩**: 旧 turn 被淘汰时,`compaction.py` 通过 LLM 摘要为结构化 handoff,prepend 到近期历史
- **懒加载**: 历史从 DB 首次访问时加载,之后内存缓存
- **消息队列**: Agent busy 时消息进 `_message_queue`,通过 `asyncio.Lock` 串行处理。排队消息逐条调用 LLM,不合并

### 工具系统

`apps/hiveweave-py/src/hiveweave/tools/executor.py` — 注册工具（pipeline）+ 5 个 legacy 评审套件（`run_tests`/`run_code_review`/`run_security_audit`/`run_perf_audit`/`run_full_review`，走 review.py），按类别:

| 类别 | 工具 |
|------|------|
| 文件操作 | `read_file`, `write_file`, `edit_file`, `list_files`, `search_files`, `create_directory`, `delete_file`, `delete_directory`, `move_file` |
| 代码执行 | `bash`, `run_command`, `start_dev_server`, `lookup_dev_server`, `run_tests`, `run_code_review`, `run_full_review`, `run_security_audit`, `run_perf_audit` |
| 补丁 | `apply_patch` |
| 搜索 | `grep`, `websearch`, `webfetch`, `browse` |
| Git worktree | `git_worktree_create`, `git_worktree_list`, `git_worktree_remove`, `git_worktree_status`, `git_worktree_checkpoint`, `git_worktree_merge` |
| 沟通 | `send_message`, `message_peer`, `message_subordinate`, `message_superior`, `message_team`, `message_user`, `ask_agent`, `notify_agent` |
| 回合出口 | `commit_turn`（每轮必须；TurnResult） |
| 组织管理 | `hire_agent`, `dismiss_agent`, `transfer_agent`, `list_subordinates`, `view_org_chart`, `read_roster`, `update_roster` |
| 任务 | `dispatch_task`, `claim_task`, `submit_task`, `review_task`, `approve_work`, `reject_work`, `create_task`, `get_tasks`, `update_task_status`, `report_completion`, `request_review`, `cancel_task`, `unclaim_task`, `waive_attestation` |
| 技能 | `list_available_skills`, `read_skill`, `bind_skill`, `unbind_skill` |
| Charter/Goals | `read_charter`, `save_charter`, `read_goals`, `update_goals` |
| 记忆/日志 | `read_memory`, `write_memory`, `read_work_logs`, `write_work_log`, `update_progress` |
| 定时 | `schedule_alarm`, `list_alarms`, `cancel_alarm` |
| 其他 | `question`, `todowrite`, `review`, `list_agent_templates` |

权限矩阵（`services/policy.py`，按 role family 授予 Capability，硬门在 `hard_check`）:
- **CEO** (`role=ceo`，`infer_role_family` 优先于 permission_type): 行政五权 `DISPATCH`/`REVIEW`/`MERGE`（升级兜底）/`SOURCE_READ`/`MANAGE_ORG` + **`DOC_WRITE`**（任意文档；`classify_write_kind` 硬拒源码/配置）；**无 SOURCE_WRITE/bash/test/staffing**。终验对用户走 `message_user`（在 `CEO_TOOLS` 表内）。派工硬门：create/dispatch 的 assignee 只能是**直属中层**（`validate_ceo_dispatch_target`）
- **Coordinator / 中层 (player-coach)**: 协调权 + `SOURCE_WRITE`/`BASH_SHELL`/`TEST_RUN`/`BROWSE`——可自己搭骨架/写关键路径，有独立 worktree（同 executor 契约）；受限写白名单（`COORDINATOR_WRITE_PREFIXES`）仍适用于项目根
- **Executor**: 可读写代码,运行测试,不能 spawn 下级
- **QA** (`test_engineer`/`qa_engineer`): 含 SOURCE_WRITE（缺它 write_file 会被硬门死 —— Echo 事故）
- **HR**: 同受限写白名单，无源码写
- 工具表拆分：`CEO_TOOLS` vs `COORDINATOR_BUILDER_TOOLS`（`services/permission.py`）；`COORDINATOR_TOOLS` 保留为别名兼容旧 import。**未知 family 兜底 READONLY**（禁止 unknown→READWRITE 泄漏工具列表）
- 代码任务 assignee 门槛：`validate_executor_assignee` 按「目标须具备 SOURCE_WRITE」判定（executor/qa/builder coordinator 可，family=ceo 硬拒）
- deny 提示分 ceo vs builder coordinator（`pipeline.build_deny_hint`），不再笼统说 "read-only role"

MCP 集成在 `apps/hiveweave-py/src/hiveweave/services/mcp.py`。

### 技能系统

`apps/hiveweave-py/src/hiveweave/services/skill_registry.py` — 三层来源:
1. **外部文件系统** (`EXTERNAL_SKILLS_DIR`, SKILL.md 格式)
2. **内置注册表** (`BUILTIN_SKILLS`, 18 个方法论技能: `self-review`, `incremental-implementation`, `test-driven-development`, `frontend-ui-engineering` 等)
3. **skills.sh 远程市场** (`https://www.skills.sh`, 8s 超时,失败静默降级)

技能绑定流程:
- HR 调 `list_available_skills(search="keyword")` → 返回带序号的结果（`#1`, `#2`, `#3`），存入 per-agent 缓存
- HR 在 `hire_agent` 的 `skills` 参数中用 `"#N"` 引用工具技能（避免拼写错误），纪律技能用完整 slug
- `hire_agent` 内部校验所有 slug 有效性（内置 + skills.sh），无效 slug 拒绝招聘
- 序号全局连续递增，多次搜索不冲突

### 实时通信

`apps/hiveweave-py/src/hiveweave/realtime/phoenix_adapter.py` — 兼容前端 phoenix.js WebSocket 协议 (`/socket/websocket`)。3 个 channel: lobby, project, agent。

事件分发（`realtime/event_bus.py`）：`tool_call_start`/`tool_call_end`/`done`/`error`/`agent_health` → agent + lobby 频道；`agent_health` 事件结构 `{type, agentId, projectId, health: "error"|"ok", message, at}`，前端 OrgTree 节点据此变红/恢复。

> ADR: [003-phoenix-protocol-debt](docs/adr/003-phoenix-protocol-debt.md)

### Lifecycle Hooks

进程内扩展点（**不是** realtime `StatusEventBus` / UI fan-out）：OpenCode 风格 `(input, output)` 可变输出链。

| | |
|--|--|
| 实现 | `hiveweave/hooks/`（`registry.py` + `points.py`） |
| 注册 | `@hooks.on(point, priority=…, fail="open"|"closed", timeout_s=…)` |
| 语义 | 同 point 按 priority 升序；`fail=open` 吞错续跑；`fail=closed` 抛 `HookClosedError`（调用方必须 fail-closed，不可当 enrichment 噪声） |
| 规范 / ADR | [docs/spec/lifecycle-hooks.md](docs/spec/lifecycle-hooks.md)、[005-lifecycle-hooks](docs/adr/005-lifecycle-hooks.md) |

已挂点（见 `points.py` / `CATALOG_VERSION`）：`inbox.triage.enrich`、`agent.turn.before` / `after`、`tool.execute.before` / `after`、`trigger.context.build`、`conversation.compact.before`。  
首个消费方：inbox triage 在 platform digest 之后跑 `inbox.triage.enrich`（LLM/插件可改 `output["digest"]`，尚未默认接线付费 enricher）。  
内置 handler：`agent.turn.after` → `task_advance_nudge`（有可行动义务却未推进且非合法 waiting 时注入 `[TASK ADVANCE]`；调用 `defer_task_advance`（不推进）或本 wake 已 defer 则停催，直到外部再次唤醒）。纪律技能 `task-advance`。

### Inbox（trigger 上下文）

平台**不再**做消息类别/优先级 triage（`classify` / digest 排序已停用；`inbox_triage.py` 保留但 trigger 不走）。  
`build_trigger_context` 按时间序注入全文 Messages；`reply_required` 仅来自结构化 `expect_report` / `message_type=ask`。  
`admit_wake` **一律放行**；仅显式 `wake=False` 才 background。将来可用 per-agent 助理模型做 triage。

**去重**：`team_chat_dedupe`（MD5(from,to,content) + 60s 窗口，fail-open）覆盖两条路径——`TeamChatService.record_message` 与 trigger digest 写库前的 `check_and_mark`（重复 digest 跳过落库但 agent 仍被唤醒，超时重试语义不变）。MD5 只用于完全相同内容判重，不做意图分类。

### Agent 类型与组织

- **CEO** (root): 行政与里程碑终审——定组织、审中层里程碑、终验对用户（`message_user`）。不写业务代码、不日常直派叶子（硬门）。
- **Coordinator / 中层** (架构师/经理, player-coach): 拆派审 + 自己搭骨架/写关键路径（有独立 worktree）；hire/dismiss/transfer agent。自交任务 wake 上级而非自己。
- **Executor** (叶子 Agent): 可读写代码,运行测试,写工作日志。不能 spawn 下级。

CEO (root) 和 HR (CEO 下级) 在项目创建时自动创建。HR 负责招聘 expert agents。HR 根据角色匹配表绑定纪律技能（MANDATORY），搜索 skills.sh 绑定工具技能。hire 时 permission_mode 按 family 选定（builder coordinator/executor 可写，CEO/HR readonly）。

**Naming**: executor 的 `role` 必须是「模块短名 + 工种」（如「签到排行榜工程师」），禁止一排裸「前端工程师」。Coordinator 用领域职称（如「游戏逻辑架构师」）。

> ADR: [007-agent-role-permission-matrix](docs/adr/007-agent-role-permission-matrix.md)

### TurnResult 出口闸门（回合必须有返回值）

每轮对话视为一次函数调用，不能空转收工：

| 工具 | 用途 |
|------|------|
| `commit_turn` | 提交 TurnResult：`phase=in_progress\|waiting\|blocked\|done_slice` |
| `ask_agent` | 需要对方回复（结构化意图，不靠文案猜） |
| `notify_agent` | 单向通知，不要求回复 |

实现: `services/turn_result.py`, `turn_session.py`, `turn_exit.py`, `tools/turn_tools.py`。  
`_handle_completion` 跑 exit gates：未 `commit_turn` / 未回 ask / 有未完成义务 → 拦截并续跑（最多 3 次）。`phase=in_progress` 自动续跑。

- **reply_required 硬门**：本 turn 处理的 inbox 消息带 `reply_required`（`expect_report` / `message_type=ask`）时，agent 必须在本 turn 内对该 sender 成功调用 send_message 类工具（送达证据 `get_sent_recipients_since`）才能退出；纯文字输出不算回复 → `UNREPLIED_ASKS` **永不 soft-pass**（`commit_turn` 预检直接 REJECT，不 `end_turn`；兜底 `evaluate_turn_exit` 也不被 soft-pass 压制）。豁免 user/system/已归档/不存在的 sender（`agent.py:_handle_completion` / `turn_exit.collect_unreplied_asks`）。预检与兜底统一走 reply_contract（已读≠已回）。`WAIT_WITHOUT_ASK` 预检解析花名/short_id/UUID（不得传空 `name_by_id`）
- **doom loop 正反馈缓解**：同 turn 内同参数 `commit_turn` 已接受时返回差异化提示（"已提交，勿再同参调用"），打破「相同结果→相同决策」循环；熔断阈值 commit_turn=8

### Git worktree 隔离（executor + builder coordinator）

- 写 worktree 资格统一由 `agent_gets_write_worktree()` 判定：executor + 具备 SOURCE_WRITE 的 builder coordinator；CEO/HR 强制项目根
- hire 时自动 `GitWorktreeService.create` → 写入 `agents.workspace_path`
- 软失败（`success=false`）必须写 `worktree_error`（两条 hire 路径: `executor.py` / `org_tools.py`）
- `create` 在目录已删但 git 仍登记时会 **prune + 挂回已有分支**（不 `-B` 抹提交）
- Agent 每轮 chat 清空 `_workspace_path` 缓存；有写树资格的 agent 无有效 worktree 时 **懒创建并写回 DB**（不再清 builder coordinator 的树）
- 启动 lifespan 按 `agent_gets_write_worktree` 恢复缺失 worktree（原 executor-only SQL 过滤已放宽）
- **审查口径**: 审代码读 `.hiveweave/worktrees/<shortId>/`，不要只看项目根 main 就判「没改」；approve 后须 `git_worktree_merge`
- **`evidence.files_changed` 规范化**（`worktree_review.normalize_evidence_path` / `normalize_files_changed`）：剥 worktree 前缀；只剥路径段 `./`，保留 `.editorconfig` 这类点文件前导 `.`。submit / approve 共用
- **分支命名（P0 稳定化）**：一律 `compute_branch_name(short_id, task_id)` — 有任务 → `hw/<sid>/t-<task_id 前8位>`，无任务 → `hw/<sid>/work`。旧 slug 命名 `hw/<sid>/<task-slug>` 仅作解析/清理存量兜底（`_branch_name` 已标 LEGACY），根治 description 重算导致的分支增生
- **删除安全链**：`delete()` 默认 `git branch -d`（拒绝删未合并分支）；未合并时透出 `preserved_branch={branch, head, reason}` 绝不强删；仅 `discard=True` 走 CAS 强删
- **启动对账**：`reconcile_worktrees(workspace_path)` 三方核对（注册表/磁盘/任务表）回收孤儿 worktree，未合并分支只报告不强删；supervisor heal 后调用

### 任务账本、审查与合并门禁

- **progress 语义**：生命周期完成度（非「是否已批准」）——claimed=10 / running=20 / submitted=90 / reviewing=92 / **approved=95** / verifying=97 / **closed=100**。approved 仍须 merge+VERIFY，100 仅属于 closed。
- **assign = claim**：dispatch/指派即落账 `claimed`（`ensure_assignee_claimed`）；`claim_task` 幂等；`promote_assigned_created` 修存量
- **CREATOR_MUST_MERGE**：creator 的任务 `approved` 后重新计入其义务（必须 merge 或转交），修「assigned 但 created 不算义务」的账本漏洞
- **提交契约**：`creator_id == assignee_id`（自交）时 `[TASK SUBMITTED]` + trigger 发给 **org parent**（中层→CEO），不发自己
- **审查门禁**：`review_task` 禁 reviewer==assignee；VERIFY 任务额外禁「父任务实现者 / `evidence.merged_by` 合并人」自批（`merged_by` 在 submit 覆盖 evidence 时保留）；VERIFY 的 creator 落 CEO（审权不落回 merger）
- **merge 门禁**：`git_worktree_merge` 合调用者**自己 short_id 的分支**时，要求对应任务已 `approved` 且批准人 ≠ 调用者（`_check_self_merge_gate`）；合下级分支按原逻辑
- **merge 代理**：merger 失联/逾期时 `services/merge_proxy.py` 沿 parent 链找有 MERGE capability 的祖先发 `[MERGE PROXY]` 并触发；`task.mark_verifying` 清理陈旧 MERGE PENDING inbox
- **VERIFY spawn**：approve+merge 后 spawn 独立 QA 验证任务（`_find_independent_qa` 排除原实现者与 merger）；QA 缺席 → VERIFY blocked → hire 后 `retry_qa_blocked_verify_tasks` 重挂
- **验收串行化（HARD RULE）**：同项目同一时刻**只允许一个 VERIFY 在 MAIN 上跑集成/E2E**（共享端口 + DB 不可并发独占）。`_nudge_one_verify_task` 串行锁：已有 in-flight VERIFY（claimed/running/submitted/reviewing/verifying/rework）时新 VERIFY 保持 created 排队，前置收口后由泵（`nudge_pending_verify_tasks`，`_close_verify_and_parent` + game_time tick 双触发）续推。**blocked VERIFY 仅在「有自动解封路径」（depends_on 非空或 timer wake_at 非空）时算 in-flight**；无路径的 parked blocked（如手工「批量验收」挂起）与无 assignee 的 QA 死区不占锁（2026-08-11 死锁复盘，`blocked_task_has_wake_path`）。`update_task_status` block 必须带 `dependsOnTaskIds` 或 `wakeAt`，否则硬拒。**worktree 内禁跑 E2E/集成验收**——只许单测/静态检查，验证一律在 MAIN + 系统 VERIFY 内做

> ADR: [009-task-ledger-review-merge-gates](docs/adr/009-task-ledger-review-merge-gates.md)

### 中断恢复与自主唤醒（agent 生命周期）

- **安全超时**：`SAFETY_TIMEOUT_MS = 600_000`（**10 分钟**，不是 45 分钟）单轮 chat 兜底
- **统一错误计数**：`_handle_safety_timeout` 纳入 `_consecutive_errors`，与 LLM 错误同账；超限 → 放弃本轮 + `_escalate_turn_interruption` 升级上级 + 举红
- **inbox watcher 复活**：`_ensure_watcher_alive()` — cancel/强制重置后 watcher 可能被永久杀死，agent 收信不再自主唤醒；chat()/enqueue_wake() 入口均调用复活
- **重启 wait 恢复**：lifespan 对所有项目跑 `recover_wait_timeouts`（幂等）——`agent_waits` 已到期的立即按超时处理，未到期的武装一次性 `call_later`；stop 时 `cancel_wait_recovery_timers`。重启后 parked agent 不再永久停摆
- **agent_health 红框**：`_broadcast_agent_health("error"|"ok", message)` 经 event_bus 广播到 agent + lobby 频道；前端 store `agentHealth` map → OrgTree 节点变红提示（不进 activity feed）

### Streaming 僵尸自愈（不要靠人工清）

`chat_messages.is_streaming=1` 三种含义：正常流式（PROCESSING）/ 卡住中的流 / 真孤儿（agent 已 idle 但标志未关）。

系统收尾必须确认写库成功：
- `ChatMessageService.finalize_streaming_message` — `update_message` 返回 False 时 agent-wide 兜底
- Agent `_finalize_streaming_turn` — completion/error/cancel/timeout/finally 统一走这里；确认成功后才清 `_streaming_msg_id`
- 新一轮 chat 开始前清该 agent 残留 streaming
- game_time 每 30s（`STREAMING_SWEEP_TICKS`）扫孤儿：非 PROCESSING 的 `is_streaming=1` 清掉；PROCESSING 的保留（避免误杀）
- 启动时仍全量清崩溃残留

**不要**把「手动 SQL 清僵尸」当成常规运维；那是自愈失效时的最后手段。

> ADR: [001-streaming-zombie-self-heal](docs/adr/001-streaming-zombie-self-heal.md)

### Org chart dirty-flag 机制

`OrgService` 维护 `_org_version` 和 `_agent_org_version` 两个内存 dict:
- `create_agent`/`dismiss_agent`/`transfer_agent`/`update_agent`(name/role/parent_id) 时 bump `org_version`
- Agent 首次对话检查 `org_dirty` → 注入精简通讯录（花名 + short_id + role + 层级）→ 清除标记
- 未变更时不注入，零 token 浪费（仿照 goals dirty 机制）

### Org hire / dismiss 硬不变式

软提示词挡不住「dismiss 重招」与组织膨胀，工具边界硬拒绝：

- `services/org_invariants.validate_hire`：active 花名唯一、executor 岗位唯一、禁裸角色名、executor 不得挂 CEO、直属 ≤7、禁挂 archived parent、保留名（归零/知远）
- `dismiss_agent` 闭合生命周期：开任务转交上级或归档、inbox 全 ACK、取消闹钟、清 worktree
- `InboxService.send_message` 拒投 archived；stall / reply-watchdog / post-merge nudge 只碰 active，且 `supersede_watchdog_messages` 先清旧催办再插新（upsert）
- 纠偏优先序：`transfer_agent` → `bind_skill` → 才 `dismiss`+hire
- **VERIFY 重挂**：hire_agent 成功路径自动 `retry_qa_blocked_verify_tasks(project_id)`（`tools/task_tools.py`）— 新 QA 到岗把 blocked 的 VERIFY 任务改回 created 并唤醒（绕过 `_TRANSITIONS` 的定向 SQL 纠偏，治「QA 缺席 → VERIFY 死区」）

### 合法 Idle（P0–P2）

不要把「有消息 / 有义务 / 跑 LLM / UI 忙」绑成一件事：

- `disposition`（waiting_human / blocked / complete / …）与 `execution`（idle/processing）正交；前端主文案跟 disposition
- `phase=in_progress` **不再**无限续跑；有义务且有进展时最多再 1 个 slice
- gate 只校验：缺 commit 最多修 1 次；账本不一致停泊；连续无进展 → blocked
- `hire_agent` 成功后工具回执提醒 NEXT ACTION（向请求方 `send_message`）；回合出口闸门 `HIRE_UNREPORTED` 拦截「招完却未发消息就 done_slice」——**不**替 AI 自动发 inbox
- `waiting_human` / `complete` 等 disposition 不再拦截 peer 消息唤醒
- 平台保留端口 `4000/5173/4173`；项目用 `start_dev_server`；禁止裸 `vite`/`npm run dev` 默认撞 5173

**P1**
- Wait Contract：`commit_turn(waiting|blocked)` 持久化到 `agent_waits`（ref / wake_on / expires_at / obligation_version）；唤醒须匹配 contract
- single-flight：busy 时 trigger 入队；收工后 300ms 合并窗口 coalesce 多次 trigger
- `GET /api/debug/agents/{id}/runtime` → RuntimeSnapshot（execution / disposition / waits / obligations）

**P2**
- `prepare_spawn_command` / `spawn_project_process`：拦截保留端口，裸 vite 自动改写到项目端口
- `heal_project_executor_worktrees`：`start_project_agents` 前自愈缺失 worktree
- `GET /api/debug/metrics`：wake 原因 / 无进展熔断 / inbox dedupe 计数

> ADR: [002-idle-architecture](docs/adr/002-idle-architecture.md)

### 语言无关：禁止用文案猜意图（HARD RULE）

**假设用户与 Agent 可用任意自然语言（中/英/法/…）。平台逻辑必须在任何语言下行为一致。**

禁止：
- 用正则/关键词/「像不像要回复」扫描 **自由文本** 来推断意图（如 `请回复`、`report back`、`全部完成`、标题含「页面」→ UI 策略等）
- 因文案分类错误导致 digest 截断、错误标 progress、错误催办

必须：
- **结构化字段写死意图**：`expect_report` / `message_type=ask`（要回复）、工具名、账本 status、平台协议前缀（仅代码发出的英文常量如 `[TASK SUBMITTED]`）
- **未回复检测（简单）**：A 发 B 且 `expect_report=1` → 查 B 是否有回信指向 A（花名/ID）；turn exit `UNREPLIED_ASKS` 提醒。**不**扫自然语言
- 需要对方回复 → `ask_agent` 或 `expect_report=true`；FYI → `notify_agent`
- **不做**平台侧消息分类/优先级；**不做**提交方 attestation 硬闸（提交方证据充分性由审查方判定，平台不替代审查方做"够不够"的裁决）；**不做**周期性 stall/unreplied 催办（推进靠 `agent.turn.after` task-advance hook）
- **做**审查方执行证据硬闸（P0-2）：approve 代码类任务时，审查方本人必须持有本任务的新鲜 `test_run` attestation（至少跑过测试），否则拒绝 approve。这与"提交方硬闸"是两条不同的线——前者保证执行下限（审查方不能 12 秒空批），后者维持不做（证据够不够仍是审查方的判断权）

入口：`reply_policy.resolve_expect_report`、`turn_exit.collect_unreplied_asks`。

### Game time

模拟项目时间,`REAL_SECONDS_PER_GAME_DAY = 3600` (1 真实小时 = 1 游戏天)。5 秒 tick 持久化时间并触发到期告警。每 30s 扫 orphan streaming。

**Stall 检测三层机制**（区分清楚，不要混淆）：

1. **Inbox stall / awaiting-reply 催办 — 已禁用**（`_check_stalled` / `_nudge_awaiting_replies` no-op）。回复义务由 turn exit 的 `expect_report` / `ask` + 收件人 ID 检查强制执行，不做周期性催办。
2. **Task stall 催办 — 活跃**（`_nudge_stale_ledger` 内的 `TASK_STALL_THRESHOLDS` 段）。按任务状态停留时间催办：running>20min / submitted>10min / reviewing>10min / rework>10min / created>5min / claimed>5min。超过 `STALL_ESCALATION_THRESHOLD`(3) 次后升级到上级。与 P0-3 stall break 互斥：近 5 分钟内被 STALL BREAK 的 agent 不再收到 task stall nudge。
3. **沉默观测看门狗 — 活跃**（`_check_silent_agents`）：agent **10 分钟无任何产出**（chat_messages assistant 行 / work_logs）→ 唤醒 + 红框；持续 30 分钟 → 通知上级。覆盖"接活后当场死亡、名下无待回复消息"的盲区。

**P0-3 跨轮 STALL BREAK 账本**（`agents/agent.py`）：streamer 的 `tool_loop_stall` 检测（同轮内连续无进展工具调用）触发 `[STALL BREAK]` 结束当前 turn。跨轮账本 `_stall_break_ledger` 记录每次 stall break 时间戳；30 分钟内第 2 次 → agent disposition=blocked + `[AGENT STUCK]` 升级上级。防止"有产出但无进展"的 agent 无限空转。

- **peer-review 死锁拆解已禁用**（互审卡住由领导催，平台不自动拆）

### 前端

React 19 + Zustand (`store.ts`)。React Flow 渲染组织架构图。关键面板: ChatPanel, OrgTree, AgentNode, GoalsPanel, QuestionDialog。API 调用通过 `api.ts` → FastAPI 路由 (`/api/*`)。WebSocket 通过 phoenix.js。Electron 桌面端支持 (`apps/web/electron/main.cjs`)。

像素办公室视图（游戏化方向，2026-07-24 定调）: `OfficeView.tsx`（薄 React 宿主）+ `office/OfficeScene.ts` + `OfficeActor.ts`，**PixiJS 8.x**（不用 Godot/游戏引擎；Phaser 3 仅作迁移备选）。架构原则: canvas 层只渲染、订阅 store/WS 事件，不跑模拟逻辑；文本密集 UI（聊天/组织树/任务）用**像素皮肤 DOM 窗口**（九宫格 border-image + 像素字体 + image-rendering: pixelated），不画进 canvas；素材管线 Aseprite（帧动画图集）+ Tiled（等距 tilemap，扩张阶段）。详见 `docs/AI工程组织_MVP蓝图.md` §11。

### 环境变量

- `HIVEWEAVE_OPENCODE_API_KEY` — OpenCode API key (所有 AI 请求)
- `HIVEWEAVE_META_DB_PATH` — 覆盖 Meta DB 路径 (默认: `apps/hiveweave-py/data/hiveweave.db`)
- `HIVEWEAVE_API_KEY` — API key auth (未设则开放)
- `HIVEWEAVE_EXTERNAL_SKILLS_DIR` — 外部技能目录 (SKILL.md 格式)
- `HIVEWEAVE_ARK_API_KEY` / `HIVEWEAVE_ARK_BASE_URL` / `HIVEWEAVE_ARK_MODEL_ID` — 火山引擎 Ark 主通道（Plan）
- `HIVEWEAVE_ARK_CODING_API_KEY` / `HIVEWEAVE_ARK_CODING_BASE_URL` / `HIVEWEAVE_ARK_CODING_MODEL_ID` — Ark Coding 第二通道（模型池轮询分摊限流；与主 key 相同则跳过）
- `HIVEWEAVE_MODEL_POOL_ENABLED` — 模型池开关（tier 配置就绪后自动降级为兼容回退）
- **global_settings 模型分级键**（DB 级，Settings API 或 UI 配置）：`model_tier_management_primary` / `model_tier_management_backup` / `model_tier_executor_primary` / `model_tier_executor_backup` — 值存 `llm_models.id` 或 `model_id`；未配置时回退到旧 `default_coordinator_model` / `default_executor_model`
- 其他 provider keys: `HIVEWEAVE_OPENAI_API_KEY`, `HIVEWEAVE_ANTHROPIC_API_KEY` 等

### 网络代理

> 个人开发环境配置见 CLAUDE.local.md（已 gitignore，不进仓库）

## Agent Diagnosis

### 后端重启后必须重新激活项目

重启后所有项目 `is_started=0`，agent 不会起来；必须先激活（GET 方法）：

```bash
curl http://localhost:4000/api/projects            # 查项目 id
curl http://localhost:4000/api/projects/<id>/activate
```

排查 agent 卡死时优先看 debug API：`GET /api/debug/agents/{id}/runtime`（execution/disposition/waits/obligations）和 `GET /api/debug/metrics`（wake / `stream_total_timeout` / `poll_hard_reject` / inbox dedupe 等）。

### 已知坑

- `supervisor.restart_agent`（max_restarts=5 崩溃重启）目前**无调用方，是死代码**；agent 中断恢复实际走 `_consecutive_errors` + `_escalate_turn_interruption`（见「中断恢复与自主唤醒」）
- 文档若写「45 分钟安全超时」均为过期说法，实际 `SAFETY_TIMEOUT_MS = 600_000`（10 分钟）；流式信封超时是另一条线：`TOTAL_TIMEOUT_S=540`（「请求总超时」）
- 人工 chat nudge 若返回 `offDuty:true` 则未跑 LLM（下班自动回复）；须项目已 activate 且 agent 在班
- approve+merge 后 VERIFY 无人接会 `blocked`；招 QA 后 `retry_qa_blocked_verify_tasks` 重挂，或 waive

### 查看 Agent 状态

> 个人开发环境配置见 CLAUDE.local.md（已 gitignore，不进仓库）

### 查看 Agent 对话和收件箱

> 个人开发环境配置见 CLAUDE.local.md（已 gitignore，不进仓库）

### 行为数据导出（分析 agent 实际做了什么）

**任务链路/卡点诊断首选 Timeline 端点**（2026-08-04 落地，只读、返回已归并好的事件流，不用自己拼表）：

```bash
# 团队活动段：谁忙谁闲、哪个任务段异常长/多次改派/反复返工（先扫异常）
curl "http://localhost:4000/api/projects/<UUID>/timeline/activity?since_ms=0&until_ms=<now_ms>&limit=2000"

# 单任务全链路：打回(review_rework)/改派(reassigned)/催办/交接全在里面（再精确诊断）
curl "http://localhost:4000/api/projects/<UUID>/timeline/tasks/<task_id>?limit=500"
```

- 两阶段侦察：先 activity 找异常段，再拉单任务链——比全表 dump 省一个数量级 token
- 响应带 `truncated` 标记（超 limit 截最旧，不静默）；`if_changed_since=<max_event_ts>` 可无变化短路
- 覆盖边界：work_logs 任务关联有洞（turn_result 引用埋在 `waiting_on[].ref`，未进时间轴）；「agent 当时为什么这么决策」在 chat_messages，不在任务链——任务链是骨架不是全部
- 前端「任务」tab 的「复制 MD」按钮可一键导出同一份链路为 Markdown（人→AI 通道）；深链 `#view=timeline&task=<id>&since=&until=` 人 AI 同视角

**需要原始表/对话内容时用 project-export**（比直读 sqlite 安全——SQL 白名单 + 截断）：

```bash
# 项目 id 从 GET /api/projects 查（UUID，不是名字！TEST19 = 1a0b0ab5-696e-4f79-9159-e7783eec6512）
curl "http://localhost:4000/api/debug/project-export?project_id=<UUID>&truncate=0" > all.json
```

- **默认导出 12 个核心行为表**：agents / tasks / task_events / agent_runs / run_steps / tool_attestations / chat_messages / work_logs / handoffs / inbox / memories / verification_cases
- `tables=a,b,c` 过滤；`truncate=N` 长文本截断（0=不截断）；`max_rows=N` 每表行数上限（默认 20000）
- `run_steps` 已 LEFT JOIN agent_runs 附带 `agent_id`（孤儿行 agent_id 为 null）
- **task.archived 的 reason 在事件 `payload` JSON 里**（导出列无独立 reason 字段，脚本需解析 payload）
- inbox 回复契约：ask 的 `reply_contract_id`，回复行的 `reply_to` 指向它（**不是** ask 的 id）；`message_type=ask` + `expect_report=1` 表示有回复义务
- `chat_messages.role=user` 行是系统注入（Messages/Goals），不是真人消息；`role=team` 是 agent 间消息
- 示例聚合脚本：`%LOCALAPPDATA%\Temp\opencode\test19_export\analyze*.py`（Counter 统计工具错误分布、task_events 时间线、ask 契约闭合率）
- 注意：此端点无鉴权（同其他 debug 端点）；`uv run python -c` 内联会挂，分析脚本要落成文件跑

### 查看后端日志
```bash
# 日志文件: tasks/ 目录下最新的 .output 文件
# 搜索错误/超时
grep -E "error|timeout|watchdog|completion_save_failed|finalize_streaming|orphan_streaming|worktree_soft_fail|worktree_recovered" tasks/<最新>.output

# 跟踪最近 activity
tail -30 tasks/<最新>.output
```

### 清除僵尸消息（最后手段；正常应靠自愈）

> 个人开发环境配置见 CLAUDE.local.md（已 gitignore，不进仓库）

## Migration history

本项目从 Elixir/Phoenix + Node.js/Fastify 双后端迁移到 Python/FastAPI 单后端。迁移文档在 `docs/migration/`。
