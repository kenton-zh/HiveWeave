# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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

前端需要 Node `>=22.0.0 <24.0.0`。系统同时装有 Node 24 (全局) 和 Node 22 (便携版, `%LOCALAPPDATA%\Programs\node-v22.20.0-win-x64`)。运行 pnpm/node 命令前,将 Node 22 加入 PATH:

```bash
export PATH="$LOCALAPPDATA/Programs/node-v22.20.0-win-x64:$PATH"
```

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
| `src/hiveweave/llm/` | LLM 流式调用 (streamer, provider, retry, circuit_breaker) |
| `src/hiveweave/tools/` | 工具执行器 + 74 个注册工具（+5 个 legacy 评审套件） |
| `src/hiveweave/services/` | 业务服务 (org, dispatch, memory, handoff, skill_registry, turn_*, git_worktree, game_time, chat_message, inbox_triage, ...) |
| `src/hiveweave/hooks/` | Lifecycle hooks（OpenCode 风格 registry + points） |
| `src/hiveweave/conversation/` | 对话历史 + token budget + compaction |
| `src/hiveweave/db/` | Meta DB + per-project DB (aiosqlite) |
| `src/hiveweave/realtime/` | WebSocket (phoenix_adapter, channels, pubsub, event_bus) |
| `src/hiveweave/prompts/` | ETHOS 提示词体系 (coordinator, executor, charter) |
| `src/hiveweave/config.py` | pydantic-settings 配置 |
| `src/hiveweave/main.py` | FastAPI app + lifespan |

### Dual-DB pattern

两层 SQLite:

1. **Meta DB** (`apps/hiveweave-py/data/hiveweave.db`, WAL mode) — 全局表: `projects`, `agent_templates`, `llm_models`, `global_settings`, `mcp_servers`, `meta_index`。每个服务器进程一个。（旧 `agent_index`/`permission_rules` 等表已移除/废弃，迁移时 DROP — 见 `db/meta.py:_LEGACY_TABLES_TO_DROP`）
2. **Per-project DB** (每个工作区 `.hiveweave/data.db`, WAL mode) — 项目级表: `agents`, `memories`, `chat_messages`, `handoffs`, `inbox`, `work_logs` 等。按工作区隔离。

`agent_id → project_id` 路由由 `AgentRouter`（`services/agent_router.py`）内存映射完成，启动时遍历所有 per-project DB 重建路由表；`create_agent`/`delete_agent` 时同步更新。完整 agent 数据（name, role, skills 等）在 per-project DB 的 `agents` 表中。

`ensureProjectDb(workspace_path)` 懒创建 per-project DB。

> ADR: [004-dual-db-pattern](docs/adr/004-dual-db-pattern.md)

### LLM 流式调用

`apps/hiveweave-py/src/hiveweave/llm/streamer.py` — httpx 流式 SSE,支持多 provider。同步 httpx 在线程池解析 SSE 后经 queue **边收边推** `_fire_delta`（真流式，避免整包收完才刷新导致 UI 冻住假象）:
- `provider.py`: provider 工厂,映射 `openai`/`anthropic`/`google` 到对应 API
- `retry.py`: 429/503/504/529 重试,指数退避 + jitter,解析 `Retry-After`
- `circuit_breaker.py`: 熔断器,探针锁防止多 Agent 同时冲击不稳定 API
- Token 估算: char-ratio 启发式 (4 chars/token EN, ~1.5 CJK),预留 20K compaction buffer
- 思考模式 (thinking/reasoning): 由 `llm_models` 表的 `supports_thinking` 和 `default_reasoning_effort` 控制,所有 LLM 调用统一生效（不区分用户对话 vs agent 间对话）
- `CONTINUE_SENTINEL`：请求末尾非 user 时追加到 **HTTP 副本**的静态 user 文案（修 gateway tool_call id 400；并写明「回合未收口故再次唤醒 / 非人类新指令」）。不写回持久化历史
- **Doom-loop 防护**：同一工具连续重复调用触发熔断。只读轮询工具豁免 —— `DOOM_LOOP_READONLY_TOOLS`（17 个：get_tasks/read_file/list_subordinates 等）走 `DOOM_LOOP_READONLY_FUSE=15` 保险丝而非默认 3 次；唯一入口 `doom_loop_limit(tool_name)`，容忍度表 `DOOM_LOOP_TOOL_LIMITS`
- 全局 LLM 并发上限 `_LLM_MAX_CONCURRENT`（env `HIVEWEAVE_LLM_MAX_CONCURRENT`，默认 8）；`TOTAL_TIMEOUT_S=540`（env `HIVEWEAVE_STREAM_TOTAL_TIMEOUT_S`；给 agent safety_timeout 600s 留 60s 余量）
- **连续流式总超时**：同 agent `_stream_timeout_streak ≥ 2` → `_park_after_stream_timeouts`（disposition waiting + wait `stream_total_timeout_recovery` + 升级上级，不自动 approve）

### 对话管理

`apps/hiveweave-py/src/hiveweave/conversation/store.py`:
- **Token-budget 裁剪**: 按 token 预算裁剪历史,不按消息数。Turn 级裁剪 — 不拆分 `assistant(tool_calls)` / `tool(result)` 对
- **智能压缩**: 旧 turn 被淘汰时,`compaction.py` 通过 LLM 摘要为结构化 handoff,prepend 到近期历史
- **懒加载**: 历史从 DB 首次访问时加载,之后内存缓存
- **消息队列**: Agent busy 时消息进 `_message_queue`,通过 `asyncio.Lock` 串行处理。排队消息逐条调用 LLM,不合并

### 工具系统

`apps/hiveweave-py/src/hiveweave/tools/executor.py` — 74 个注册工具（pipeline）+ 5 个 legacy 评审套件（`run_tests`/`run_code_review`/`run_security_audit`/`run_perf_audit`/`run_full_review`，走 review.py），按类别:

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
- **Coordinator**: 只读源码 + 受限写白名单（`COORDINATOR_WRITE_PREFIXES`：docs/、.hiveweave/shared/ 等 + charter.md/goals.md/spec.md），可 hire/dismiss/transfer、dispatch/review/cancel_task/unclaim_task/waive_attestation。不能写源码
- **Executor**: 可读写代码,运行测试,不能 spawn 下级
- **QA** (`test_engineer`/`qa_engineer`): 含 SOURCE_WRITE（缺它 write_file 会被硬门死 —— Echo 事故）
- **HR**: 同 coordinator 受限写白名单，无源码写
- deny 提示如实返回硬门真实原因 + coordinator/HR 写白名单（`pipeline.build_deny_hint`），不再笼统说 "read-only role"

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

### Inbox triage（trigger 前结构化 digest）

`services/inbox_triage.py` — 唤醒主模型前先 staging→ready，避免 raw inbox 洪水：

1. 平台确定性 digest（类别/优先级/折叠重复 progress/建议处理顺序）
2. 跑 hook `inbox.triage.enrich`
3. 仅 `ready` 批次注入 `## Inbox digest`；`## Messages (detail)` 只展开 ask / task_transition / approval / `expect_report`（progress、普通 command 看 digest，避免双写）。无 digest 时仍走全量 detail。Background 有 digest 时只留条数提示
4. per-agent asyncio 锁；`running` TTL 过期 → `expired` 后重建；fail-closed → batch `failed` + 返回 `None`
5. busy 且 triage 未就绪：仍 `enqueue_wake`（占位文案 + inbox ids），不丢 wake
6. `build_trigger_context` 第四返回值 `wake_category`（最高优先级类别）写入 chat opts；`disposition=complete` 时仅任务类 wake（`wake_category=task_transition` / `source=task` 等）放行，防 done_slice 空转
7. `get_tasks` 等同轮轮询：硬拒 + telemetry `poll_hard_reject`

### Agent 类型与组织

- **Coordinator** (架构师/经理): 可读下级日志/代码,审批工作,hire/dismiss/transfer agent。不能写代码。
- **Executor** (叶子 Agent): 可读写代码,运行测试,写工作日志。不能 spawn 下级。

CEO (root) 和 HR (CEO 下级) 在项目创建时自动创建。HR 负责招聘 expert agents。HR 根据角色匹配表绑定纪律技能（MANDATORY），搜索 skills.sh 绑定工具技能。

**Naming**: executor 的 `role` 必须是「模块短名 + 工种」（如「签到排行榜工程师」），禁止一排裸「前端工程师」。Coordinator 用领域职称（如「游戏逻辑架构师」）。

### TurnResult 出口闸门（回合必须有返回值）

每轮对话视为一次函数调用，不能空转收工：

| 工具 | 用途 |
|------|------|
| `commit_turn` | 提交 TurnResult：`phase=in_progress\|waiting\|blocked\|done_slice` |
| `ask_agent` | 需要对方回复（结构化意图，不靠文案猜） |
| `notify_agent` | 单向通知，不要求回复 |

实现: `services/turn_result.py`, `turn_session.py`, `turn_exit.py`, `tools/turn_tools.py`。  
`_handle_completion` 跑 exit gates：未 `commit_turn` / 未回 ask / 有未完成义务 → 拦截并续跑（最多 3 次）。`phase=in_progress` 自动续跑。

### Git worktree 隔离（executor）

- hire executor 时自动 `GitWorktreeService.create` → 写入 `agents.workspace_path`
- 软失败（`success=false`）必须写 `worktree_error`（两条 hire 路径: `executor.py` / `org_tools.py`）
- `create` 在目录已删但 git 仍登记时会 **prune + 挂回已有分支**（不 `-B` 抹提交）
- Agent 每轮 chat 清空 `_workspace_path` 缓存；executor 无有效 worktree 时 **懒创建并写回 DB**
- 启动 lifespan 按 `permission_type='executor'` 恢复缺失 worktree
- **审查口径**: coordinator 审代码读 `.hiveweave/worktrees/<shortId>/`，不要只看项目根 main 就判「没改」；approve 后须 `git_worktree_merge`
- **`evidence.files_changed` 规范化**（`worktree_review.normalize_evidence_path` / `normalize_files_changed`）：剥 worktree 前缀；只剥路径段 `./`，保留 `.editorconfig` 这类点文件前导 `.`。submit / approve 共用
- **分支命名（P0 稳定化）**：一律 `compute_branch_name(short_id, task_id)` — 有任务 → `hw/<sid>/t-<task_id 前8位>`，无任务 → `hw/<sid>/work`。旧 slug 命名 `hw/<sid>/<task-slug>` 仅作解析/清理存量兜底（`_branch_name` 已标 LEGACY），根治 description 重算导致的分支增生
- **删除安全链**：`delete()` 默认 `git branch -d`（拒绝删未合并分支）；未合并时透出 `preserved_branch={branch, head, reason}` 绝不强删；仅 `discard=True` 走 CAS 强删
- **启动对账**：`reconcile_worktrees(workspace_path)` 三方核对（注册表/磁盘/任务表）回收孤儿 worktree，未合并分支只报告不强删；supervisor heal 后调用

### 中断恢复与自主唤醒（agent 生命周期）

- **安全超时**：`SAFETY_TIMEOUT_MS = 600_000`（**10 分钟**，不是 45 分钟）单轮 chat 兜底
- **统一错误计数**：`_handle_safety_timeout` 纳入 `_consecutive_errors`，与 LLM 错误同账；超限 → 放弃本轮 + `_escalate_turn_interruption` 升级上级 + 举红
- **inbox watcher 复活**：`_ensure_watcher_alive()` — cancel/强制重置后 watcher 可能被永久杀死，agent 收信不再自主唤醒；chat()/enqueue_wake() 入口均调用复活
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
- inbox 分级：progress/ACK（含「全部完成」）`wake=0` 不触发 LLM；`waiting_human` 时仅用户/新任务可唤醒
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

### Game time

模拟项目时间,`REAL_SECONDS_PER_GAME_DAY = 3600` (1 真实小时 = 1 游戏天)。5 秒 tick 持久化时间并触发到期告警。每 30s 扫 orphan streaming；每 2min（`STALL_CHECK_TICKS`）查停滞 Agent / 未回复 ask。

- **停滞催办**：`STALL_IDLE_MS = 10min` 无响应 → 催办（`STALL_COOLDOWN_MS = 15min` 防重复）；同一对未回复催满 `STALL_ESCALATION_THRESHOLD = 3` 次（≈40min+）才升级到上级 —— 不是「15 分钟直接升级」
- **失联观测看门狗**（`_check_silent_agents`，挂在 STALL_CHECK 块内）：agent **10 分钟无任何产出**（`SILENCE_THRESHOLD_MS`）→ 唤醒 + 广播 agent_health error 举红；失联持续 30 分钟（`SILENCE_NOTIFY_MS`）→ 升级通知上级（30min 冷却）；恢复产出后自动广播 ok 解除红框

### 前端

React 19 + Zustand (`store.ts`)。React Flow 渲染组织架构图。关键面板: ChatPanel, OrgTree, AgentNode, GoalsPanel, QuestionDialog。API 调用通过 `api.ts` → FastAPI 路由 (`/api/*`)。WebSocket 通过 phoenix.js。Electron 桌面端支持 (`apps/web/electron/main.cjs`)。

### 环境变量

- `HIVEWEAVE_OPENCODE_API_KEY` — OpenCode API key (所有 AI 请求)
- `HIVEWEAVE_META_DB_PATH` — 覆盖 Meta DB 路径 (默认: `apps/hiveweave-py/data/hiveweave.db`)
- `HIVEWEAVE_API_KEY` — API key auth (未设则开放)
- `HIVEWEAVE_EXTERNAL_SKILLS_DIR` — 外部技能目录 (SKILL.md 格式)
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
