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
| `src/hiveweave/api/` | FastAPI 路由 (17 个模块, 112 路由) |
| `src/hiveweave/agents/` | Agent + Supervisor + trigger |
| `src/hiveweave/llm/` | LLM 流式调用 (streamer, provider, retry, circuit_breaker) |
| `src/hiveweave/tools/` | 工具执行器 + 66 个内置工具 |
| `src/hiveweave/services/` | 业务服务 (org, dispatch, memory, handoff, skill_registry, turn_*, git_worktree, game_time, chat_message, ...) |
| `src/hiveweave/conversation/` | 对话历史 + token budget + compaction |
| `src/hiveweave/db/` | Meta DB + per-project DB (aiosqlite) |
| `src/hiveweave/realtime/` | WebSocket (phoenix_adapter, channels, pubsub, event_bus) |
| `src/hiveweave/prompts/` | ETHOS 提示词体系 (coordinator, executor, charter) |
| `src/hiveweave/config.py` | pydantic-settings 配置 |
| `src/hiveweave/main.py` | FastAPI app + lifespan |

### Dual-DB pattern

两层 SQLite:

1. **Meta DB** (`apps/hiveweave-py/data/hiveweave.db`, WAL mode) — 全局表: `projects`, `agent_index`, `agent_templates`, `llm_models`, `global_settings`, `mcp_servers`, `permission_rules` 等。每个服务器进程一个。
2. **Per-project DB** (每个工作区 `.hiveweave/data.db`, DELETE journal mode) — 项目级表: `agents`, `memories`, `chat_messages`, `handoffs`, `inbox`, `work_logs` 等。按工作区隔离。

`agent_index` 表（Meta DB）提供路由: agent_id → project_id。完整 agent 数据（name, role, skills 等）在 per-project DB 的 `agents` 表中。

`ensureProjectDb(workspace_path)` 懒创建 per-project DB。

### LLM 流式调用

`apps/hiveweave-py/src/hiveweave/llm/streamer.py` — httpx 流式 SSE,支持多 provider。同步 httpx 在线程池解析 SSE 后经 queue **边收边推** `_fire_delta`（真流式，避免整包收完才刷新导致 UI 冻住假象）:
- `provider.py`: provider 工厂,映射 `openai`/`anthropic`/`google` 到对应 API
- `retry.py`: 429/503/504/529 重试,指数退避 + jitter,解析 `Retry-After`
- `circuit_breaker.py`: 熔断器,探针锁防止多 Agent 同时冲击不稳定 API
- Token 估算: char-ratio 启发式 (4 chars/token EN, ~1.5 CJK),预留 20K compaction buffer
- 思考模式 (thinking/reasoning): 由 `llm_models` 表的 `supports_thinking` 和 `default_reasoning_effort` 控制,所有 LLM 调用统一生效（不区分用户对话 vs agent 间对话）

### 对话管理

`apps/hiveweave-py/src/hiveweave/conversation/store.py`:
- **Token-budget 裁剪**: 按 token 预算裁剪历史,不按消息数。Turn 级裁剪 — 不拆分 `assistant(tool_calls)` / `tool(result)` 对
- **智能压缩**: 旧 turn 被淘汰时,`compaction.py` 通过 LLM 摘要为结构化 handoff,prepend 到近期历史
- **懒加载**: 历史从 DB 首次访问时加载,之后内存缓存
- **消息队列**: Agent busy 时消息进 `_message_queue`,通过 `asyncio.Lock` 串行处理。排队消息逐条调用 LLM,不合并

### 工具系统

`apps/hiveweave-py/src/hiveweave/tools/executor.py` — 66 个内置工具,按类别:

| 类别 | 工具 |
|------|------|
| 文件操作 | `read_file`, `write_file`, `edit_file`, `list_files`, `search_files`, `create_directory`, `delete_file`, `delete_directory`, `move_file` |
| 代码执行 | `bash`, `run_tests`, `run_code_review`, `run_full_review`, `run_security_audit`, `run_perf_audit` |
| 补丁 | `apply_patch` |
| 搜索 | `grep`, `websearch`, `webfetch` |
| Git worktree | `git_worktree_create`, `git_worktree_list`, `git_worktree_remove`, `git_worktree_status`, `git_worktree_checkpoint`, `git_worktree_merge` |
| 沟通 | `send_message`, `message_peer`, `message_subordinate`, `message_superior`, `message_team`, `ask_agent`, `notify_agent` |
| 回合出口 | `commit_turn`（每轮必须；TurnResult） |
| 组织管理 | `hire_agent`, `dismiss_agent`, `transfer_agent`, `list_subordinates`, `view_org_chart`, `read_roster`, `update_roster` |
| 任务 | `dispatch_task`, `claim_task`, `submit_task`, `review_task`, `approve_work`, `reject_work`, `create_task`, `get_tasks`, `update_task_status`, `report_completion`, `request_review` |
| 技能 | `list_available_skills`, `read_skill`, `bind_skill`, `unbind_skill` |
| Charter/Goals | `read_charter`, `save_charter`, `read_goals`, `update_goals` |
| 记忆/日志 | `read_memory`, `write_memory`, `read_work_logs`, `write_work_log`, `update_progress` |
| 定时 | `schedule_alarm`, `list_alarms`, `cancel_alarm` |
| 其他 | `question`, `todowrite`, `review`, `list_agent_templates` |

权限矩阵控制:
- **Coordinator**: 只读文件,不能写代码,可 hire/dismiss/transfer agent
- **Executor**: 可读写代码,运行测试,不能 spawn 下级

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

### Streaming 僵尸自愈（不要靠人工清）

`chat_messages.is_streaming=1` 三种含义：正常流式（PROCESSING）/ 卡住中的流 / 真孤儿（agent 已 idle 但标志未关）。

系统收尾必须确认写库成功：
- `ChatMessageService.finalize_streaming_message` — `update_message` 返回 False 时 agent-wide 兜底
- Agent `_finalize_streaming_turn` — completion/error/cancel/timeout/finally 统一走这里；确认成功后才清 `_streaming_msg_id`
- 新一轮 chat 开始前清该 agent 残留 streaming
- game_time 每 30s（`STREAMING_SWEEP_TICKS`）扫孤儿：非 PROCESSING 的 `is_streaming=1` 清掉；PROCESSING 的保留（避免误杀）
- 启动时仍全量清崩溃残留

**不要**把「手动 SQL 清僵尸」当成常规运维；那是自愈失效时的最后手段。

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

### Game time

模拟项目时间,`REAL_SECONDS_PER_GAME_DAY = 3600` (1 真实小时 = 1 游戏天)。5 秒 tick 持久化时间并触发到期告警。每 30s 扫 orphan streaming；每 2min 查停滞 Agent / 未回复 ask。停滞 Agent (15+ 分钟无活动) 触发升级到上级。

### 前端

React 19 + Zustand (`store.ts`)。React Flow 渲染组织架构图。关键面板: ChatPanel, OrgTree, AgentNode, GoalsPanel, QuestionDialog。API 调用通过 `api.ts` → FastAPI 路由 (`/api/*`)。WebSocket 通过 phoenix.js。Electron 桌面端支持 (`apps/web/electron/main.cjs`)。

### 环境变量

- `HIVEWEAVE_OPENCODE_API_KEY` — OpenCode API key (所有 AI 请求)
- `HIVEWEAVE_META_DB_PATH` — 覆盖 Meta DB 路径 (默认: `apps/hiveweave-py/data/hiveweave.db`)
- `HIVEWEAVE_API_KEY` — API key auth (未设则开放)
- `HIVEWEAVE_EXTERNAL_SKILLS_DIR` — 外部技能目录 (SKILL.md 格式)
- 其他 provider keys: `HIVEWEAVE_OPENAI_API_KEY`, `HIVEWEAVE_ANTHROPIC_API_KEY` 等

### 网络代理

开发环境 HTTP/HTTPS 代理: `http://192.168.110.26:7890`

需要网络访问的工具（pip, uv, httpx, curl 等）配置环境变量:
```bash
export HTTP_PROXY=http://192.168.110.26:7890
export HTTPS_PROXY=http://192.168.110.26:7890
```

## Agent Diagnosis

### 查看 Agent 状态
```bash
# Meta DB（全局）— agent_index 路由表
cd D:\PC_AI\Project\HiveWeave
uv run python -c "
import sqlite3
conn = sqlite3.connect('apps/hiveweave-py/data/hiveweave.db')
conn.row_factory = sqlite3.Row
for a in conn.execute('SELECT agent_id, name, role, status, project_id FROM agent_index WHERE status!=\"archived\"').fetchall():
    print(f'[{a[\"status\"]}] {a[\"name\"]} ({a[\"role\"]}) project={a[\"project_id\"]}')
conn.close()
"
```

### 查看 Agent 对话和收件箱
```bash
# Per-project DB — chat_messages + inbox
uv run python -c "
import sqlite3, os
# 先查 Meta DB 找 workspace_path
mconn = sqlite3.connect('apps/hiveweave-py/data/hiveweave.db')
ws = mconn.execute('SELECT workspace_path FROM projects WHERE name=\"TEST\"').fetchone()
mconn.close()
pdb = os.path.join(os.path.expandvars(ws[0]), '.hiveweave', 'data.db')
conn = sqlite3.connect(pdb)
conn.row_factory = sqlite3.Row

# 最近对话
for m in conn.execute('SELECT role, is_background, substr(content,1,200) as c FROM chat_messages ORDER BY created_at DESC LIMIT 15').fetchall():
    print(f'[{m[\"role\"]} bg={m[\"is_background\"]}] {m[\"c\"][:120]}')

# 未读收件箱
for i in conn.execute('SELECT from_agent_id, to_agent_id, read, substr(message,1,150) as m FROM inbox ORDER BY created_at DESC LIMIT 10').fetchall():
    print(f'from={i[\"from_agent_id\"][:12]} to={i[\"to_agent_id\"][:12]} read={i[\"read\"]}: {i[\"m\"][:80]}')

# 工作日志
for l in conn.execute('SELECT agent_id, type, substr(summary,1,150) as s FROM work_logs ORDER BY created_at DESC LIMIT 10').fetchall():
    print(f'[{l[\"type\"]}] {l[\"s\"]}')
conn.close()
"
```

### 查看后端日志
```bash
# 日志文件: tasks/ 目录下最新的 .output 文件
# 搜索错误/超时
grep -E "error|timeout|watchdog|completion_save_failed|finalize_streaming|orphan_streaming|worktree_soft_fail|worktree_recovered" tasks/<最新>.output

# 跟踪最近 activity
tail -30 tasks/<最新>.output
```

### 清除僵尸消息（最后手段；正常应靠自愈）

产品已有：启动清残留、每轮 chat 前清、`finalize_streaming_*` 收尾、game_time 30s 孤儿扫描。  
仅当自愈失效（例如后端未加载新代码）时才手动：

```bash
uv run python -c "
import sqlite3, os
pdb = os.path.join(os.path.expandvars('D:\\\\PC_AI\\\\Project\\\\TEST'), '.hiveweave', 'data.db')
conn = sqlite3.connect(pdb)
c = conn.execute(\"UPDATE chat_messages SET is_streaming=0, content=CASE WHEN content IS NULL OR TRIM(content)='' THEN '[对话被中断]' ELSE content END WHERE is_streaming=1\")
conn.commit(); print(f'Cleared {c.rowcount} zombie(s)'); conn.close()
"
```

查 executor worktree 是否绑定：

```bash
uv run python -c "
import sqlite3, os
from pathlib import Path
m=sqlite3.connect('apps/hiveweave-py/data/hiveweave.db')
ws=m.execute('SELECT workspace_path FROM projects WHERE name=\"TEST\"').fetchone()[0]
m.close()
c=sqlite3.connect(str(Path(ws)/'.hiveweave'/'data.db')); c.row_factory=sqlite3.Row
for a in c.execute(\"SELECT short_id,name,permission_type,workspace_path,worktree_error FROM agents ORDER BY short_id\"):
    wp=a['workspace_path']; ok=bool(wp and Path(wp).exists() and (Path(wp)/'.git').exists())
    print(a['short_id'], a['name'], a['permission_type'], 'wt_ok='+str(ok), a['worktree_error'])
c.close()
"
```

## Migration history

本项目从 Elixir/Phoenix + Node.js/Fastify 双后端迁移到 Python/FastAPI 单后端。迁移文档在 `docs/migration/`。
