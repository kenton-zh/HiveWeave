# HiveWeave 团队活动可视化方案（v4 · 审计合并定稿版）

> 本版为自包含定稿，合并 v2（事实基线）与 v3（体验/性能升级），并应用对 v3 的子代理审计修正（7 处承重错误 + 若干措辞修正）。v2/v3 作废，实施以本文为准。
> 所有事实陈述均经三轮代码审计核实，关键处附文件位置；无法核实的推断均已标注。

---

## 一、目标与非目标

**目标**：让用户直观看到「团队干了啥」+「每个 agent 当前和过去的状态」；任意任务可从派发到 closed 全链路重播，消除"翻四张表拼时间线"的调试方式。

**两个视图，一套聚合接口**：
- **TeamTimeline（团队泳道总览）**：agent 为行、时间为列，任务块按状态着色——回答"这一周每个人干了什么"。
- **TaskTimelinePanel（任务全链路回放）**：单任务事件流——回答"TEST18 到底卡在哪"。

**非目标（本方案明确不做）**：ChatDev 式实时日志流回放；LLM token 级流式重放；`chat_messages.task_id` 迁移（单独立项评估）；任务编辑操作（只读视图）。

---

## 二、现状事实基线（三轮审计核实）

### 有利条件

1. **`task_events` 表记录绝大多数状态流转**（`db/schema.py:471-484`，索引 `idx_task_events_task(task_id, created_at)`），`_transition`/`_transition_multi` 同事务写入（`services/tasks/transitions.py:55-165`）。
2. **`inbox`/`handoffs` 带 `task_id` 列**，dispatch、stall 催办、relay FYI、handoff 创建均携带（`tasks` 表主键即 id）。
3. **后端 Task Ledger REST 完备**（`api/tasks.py` 9 个端点），前端完全未接（store/rest.ts/UI 三空白）。
4. **推送基础设施大部分现成**：`realtime/event_bus.py` 的 `StatusEventBus` 单例，`publish(channel, event, agent_id=?)` 非阻塞投递、慢消费者丢最旧（event_bus.py:168-209）；前端 `activityFeed` WS 管线现成（store.ts:350-467，RAF 节流，含 agent_health/model_resolved 两个拦截分支先例）。
5. **Tailwind 3.4 已装且在用**（package.json devDependencies + tailwind.config.js 自定义 `colors.g.*` token + 697 处原子类使用）；`utils/role-styles.ts` 已有集中色表先例。
6. `utils/game-time.ts` 已有游戏时间换算（`realMsToGameSeconds`/`decomposeGameSeconds`）；无路由库、`location.hash` 零使用，深链是干净空地。

### 坑（必须正视）

1. **`task_events` 有写入盲区与旁路**：
   - **旁路写入点**（不经 `_transition`，共 4 处）：`create_task` 直写 task.created/claimed（crud.py:163-183）、`reassign_task` 写 task.reassigned（claim.py:186-203）、`archive_task` 写 task.archived（close.py:701-717，注释明言不走 _TRANSITIONS）、verify_spawn 写 task.verify_rehang（tools/tasks/verify_spawn.py:593-610）。**不存在共享的 INSERT 辅助函数**，各点直接用通用 `_execute`。
   - **无事件裸 UPDATE**（4 处）：org.py dismiss 批量改派/置 cancelled（org.py:651-793）、obligation.py 依赖唤醒 blocked→running（:567-572）、close.py merge 阻塞回置 approved（:464-478）、api PATCH 换 assignee（crud.py:470-516）。
2. **`rework` 状态在事件流中永不出现**：打回是 `_transition_multi` 原子一行 `reviewing→running`（reason_code=`review_rework`）。
3. **`emit_task_event` 不写 task_events 表**（只做 progress floor + work_log，progress.py:41-84）——补写盲区不能指望它。
4. **`cancelled` 不在 `_TRANSITIONS`**（dismiss/archive 路径直写），`_transition` 遇到会抛 Illegal transition。
5. **`chat_messages` 无 `task_id`** → 第一期不进时间轴。
6. **`work_logs.task_id` 仅 dispatch 路径写**；`details` JSON 兜底只覆盖 `type='task_event'` 那批（`$.task_id` 键），turn_result 的任务引用埋在 `waiting_on[].ref`。
7. **WS 桥接缺口**：phoenix_adapter 只桥接 `lobby:`/`agent:` 两类 topic，`project:` 落入 unknown-topic 分支不转发（phoenix_adapter.py:374-381）；前端从未 join project 频道（ws.ts 仅一处注释）。bus 侧往 `project:{pid}` 的三处发布实际无人消费。
8. **前端 WS 入口有硬门**：ws.ts:410 `typeof payload.agentId === "string"` 会静默丢弃无 agentId 的载荷。
9. **共享单 aiosqlite 连接**（db/project.py:88，per-workspace LRU ≤50），aiosqlite 单工作线程 → 语句串行；WAL 已开（project.py:94，文件 docstring 过时写 DELETE，以代码为准）。全库无只读连接/第二连接/uri=True 先例。
10. **索引缺口**：单任务聚合缺 work_logs/inbox/handoffs 的 task_id 索引；**时间窗聚合更缺**——task_events 现有索引首列是 task_id、handoffs 无任何 created_at 索引（确定全表扫），work_logs 首列 agent_id（近全扫），仅 inbox 可走 created_at 索引。
11. **`agents` 无 current_task**，靠 assignee 反查 + disposition + last_active_at 推断；**沉默/健康事件不持久化**（只广播 WS）→ 过去的沉默不可回放。
12. 所有时间戳为现实毫秒，无 game_date 字段；`list_tasks` 排除归档任务。

---

## 三、总体设计

```
┌─────────────────────────────────────────────────────┐
│  左栏 activeView 第三值: "timeline"                   │
│  ┌───────────────────────────────────────────────┐  │
│  │ TeamTimeline（团队泳道总览）                     │  │
│  │  行 = agent（组织层级）列 = 时间（现实+游戏Day）  │  │
│  │  块 = task_segments 按状态着色 + 当前时刻红线     │  │
│  └───────────────────────────────────────────────┘  │
│            │ 点击任务块 → setSelectedTaskId          │
└────────────┼────────────────────────────────────────┘
             ▼
┌─────────────────────────────────────────────────────┐
│  右栏 rightPanelTab 新增: "task"（独立于 agent 门控）  │
│  ┌───────────────────────────────────────────────┐  │
│  │ TaskTimelinePanel（单任务全链路）                │  │
│  │  元信息卡 + 垂直事件流（按游戏日分组折叠）        │  │
│  └───────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────┘
```

**渲染裁决（带止损线）**：维持自绘 DOM+SVG，不上现成甘特库。理由：交互面窄（pan/zoom/click/hover/brush，无拖拽改期/依赖连线），库样式与 Tailwind 主题打架的定制成本不低。**止损线写进 P2 验收**：TeamTimeline 系文件合计超 800 行仍手感粗糙 → 降级为 CSS Grid 静态泳道 + 原生滚动 + 离散时间档位（1h/6h/1Day）。

**数据流裁决（写死）**：**WS 事件只作失效信号，不作数据源**。数据源永远是 REST 拉取——丢事件、乱序、断连全部无害，最坏退化为 30s 兜底轮询体验。

---

## 四、后端设计

### 4.1 共享事件写入辅助 + WS 发布收口【v4 修订：替代 v3"一行 publish"】

v3 假设 `_transition` 是唯一收口，不成立（二.1 的 4 个旁路点）。改法：

在 `services/tasks/db.py`（或 events.py）新增共享辅助 `insert_task_event(conn, task_id, event_type, from_status, to_status, actor_id, payload)`，**内部完成 INSERT task_events + `status_event_bus.publish`**。然后把全部 9 个写入点改经此辅助：

- `_transition` / `_transition_multi`（transitions.py）
- `create_task`、`reassign_task`、`archive_task`、verify_rehang 四个旁路点
- P1 补写的 dismiss / obligation 两处盲区（见 4.5）

**发布频道：`lobby`，不是 `project:{pid}`**【v4 修订】。phoenix_adapter 的 `_map_event` 对未映射 type 原样透传（phoenix_adapter.py:79-100），lobby:status 是前端唯一已就绪的全局频道。事件载荷固定为：

```python
{"kind": "task_event", "project_id": pid, "task_id": tid,
 "event_type": "...", "to_status": "...", "ts": ms}
```

前端按 `project_id` 过滤（见 5.4）。

### 4.2 统一事件 Schema（聚合层输出）

```python
{
  "id": str, "ts": int,               # 源表主键 / created_at 现实毫秒
  "type": str,                        # 见下
  "task_id": str,
  "agent_id": str | None,
  "from_agent_id": str | None, "to_agent_id": str | None,
  "from_status": str | None,          # task.created 时为 NULL
  "to_status": str | None,
  "reason_code": str | None,          # review_rework 等，从 payload 提取
  "title": str, "detail": str | dict,
}
```

`type` 全集（对代码实际形态）：`task.created`、`task.transition`（含打回的 reviewing→running，靠 reason_code 识别）、`task.reassigned`（from==to 同状态换人）、`task.verify_rehang`、`task.archived`、`handoff.created`、`inbox.message`、`work_log`。解析器必须处理 from_status=NULL 与 from==to 的行。

### 4.3 两个端点

新建 `api/timeline.py`（注册进 `api/router.py` 的 `_SUB_ROUTERS`），服务层 `services/tasks/timeline.py`。

**端点 1：单任务事件流**

```
GET /api/projects/{project_id}/timeline/tasks/{task_id}?limit=500
```

四路查询按 `created_at` 归并，**包在同一只读事务内**（`BEGIN…COMMIT` 包住四条 SELECT，WAL 读事务首次读钉快照，消除撕裂；注意 Python sqlite3 默认 isolation_level 下 SELECT 不隐式开事务，必须显式 BEGIN）：

- `task_events WHERE task_id = ?` —— 复用 `TaskEventService.get_task_history`，不写平行 SQL；
- `handoffs WHERE task_id = ?` —— 每 handoff **只出 1 条**（创建时间 + 当前 status/updated_at；状态流转是同行 UPDATE，无历史，不承诺轨迹）；
- `inbox WHERE task_id = ?` —— dispatch、stall 催办、relay FYI；
- `work_logs WHERE task_id = ? OR (type='task_event' AND json_extract(details,'$.task_id') = ?)` —— 在 `DispatchService.get_work_logs_for_task` 基础上扩展；json_extract 有项目先例（services/memory.py 等 5 处），兜底范围限定 type='task_event'，不夸大覆盖。

返回 `{task, agents: {id: {name, role}}, events: [按 ts 升序], max_event_ts, truncated}`。

**端点 2：团队活动段**

```
GET /api/projects/{project_id}/timeline/activity?since_ms=&until_ms=&limit=2000
    [&cursor_ts=&if_changed_since=<max_event_ts>]
```

返回：

```python
{
  "agents": [{id, name, role, parent_id, status, last_active_at}],
  "task_segments": [
    {"task_id", "title", "assignee_id", "creator_id", "reviewer_id",
     "status", "started_at", "ended_at"}   # ended_at=None 表示进行中
  ],
  "active_assignments": [
    {"agent_id", "task_id", "task_title", "since",
     "kind": "busy" | "waiting"}
     # busy: claimed/running/rework
     # waiting: blocked/submitted/reviewing/approved/verifying（含 assignee 与 reviewer_id 两类持有者）
  ],
  "window": {...}, "max_event_ts": int,
  "changed": bool,       # if_changed_since 无变化时返回 {changed:false}，等价 304
  "truncated": bool      # 超 limit 显式标记，前端提示缩窗，不静默截断
}
```

分页策略：`cursor_ts` 游标替代硬窗翻页，前端「加载更早」向后翻；`if_changed_since` 让轮询/WS 触发的重拉在无变化时 O(1) 返回。

### 4.4 只读连接池【v4 修订：补 v3 漏掉的两个坑】

`get_project_db_by_project_id` 是全局共享单连接、语句串行；timeline 的 4 表归并查询若排同一条队列，会抬高 agent 高频写的尾延迟。改法：

- `db/project.py` 新增 `get_project_db_readonly(project_id)`：`aiosqlite.connect(f"file:{path}?mode=ro", uri=True)`（aiosqlite 0.22.1 原样转发 kwargs，uri=True 可用），独立池 2 条，与写路径物理隔离（WAL 天然支持并发读）。
- **坑一·事务隔离**：aiosqlite 只串行化单条 execute、**不串行化事务块**。两个并发 timeline 请求共用一条连接会 BEGIN 交错（"cannot start a transaction within a transaction"）。池中每条连接挂一把 `asyncio.Lock`，请求获取连接 = 持锁直到 COMMIT 释放。
- **坑二·驱逐联动**：主缓存 LRU 驱逐不打 `_evicted_workspaces` 标记、`evict_project_db` 只关 `_cache` 内连接。只读池必须：① 接入 `evict_project_db`（驱逐时同关只读连接）；② 打开前检查 `_evicted_workspaces` 拒绝已驱逐 workspace；③ 打开失败降级回共享连接（只覆盖打开失败，陈旧靠 ①② 兜）。
- 此为全库首创路径（零先例），代码量 ~40 行而非 v3 估的 ~20 行。

### 4.5 task_segments 切段算法

`actor_id` 是触发者不是持有者（block/unblock/unclaim 的 actor 为 NULL，reviewing 段 actor 是评审人），不可用来定段。算法：

1. 段起点取 `tasks.created_at`；
2. **assignee 游标**由 `task.claimed`（首次认领）、`task.reassigned`（payload 带 from/to_assignee）、unclaim 类事件驱动；unclaim→重新认领之间生成 assignee=None 的「待认领」空段；
3. 状态段边界由全部 task_events 的 to_status 驱动；from_status=NULL 与 from==to 的行单独处理；
4. **末段用 `tasks.assignee_id` 当前值校准**——兜住残余盲区，保证不张冠李戴（P1 补写后盲区只剩 close.py 回置一处低频场景）；
5. blocked 段保留 assignee（block/unblock 不动 assignee_id），归 waiting 类。

### 4.6 盲区补写【v4 修订：入口形态按实际可行性重写】

**P1 并入两处（复用既有 event_type，不发明新形态）**：

- **obligation.py 依赖唤醒**（:567-572）：blocked→running 在 `_TRANSITIONS` 合法，上下文已有未使用的 `ts = TaskService()`（:548）——裸 UPDATE 替换为 `_transition` 调用即可，<10 行；`_transition` 顺带清 blocked_reason/wait_kind/wake_at，与原语义一致且更完整。
- **org.py dismiss 批量**（:651-793）：**`_transition` 走不通**（cancelled 不在状态机、blocked→claimed 非法），`emit_task_event` 不写事件表——唯一可行入口是**经 4.1 共享辅助直接 INSERT task_events**：批量改派分支写 `task.reassigned`，批量取消分支写 `task.archived`（两个 event_type 均已存在）。这是 v3 未承认但绕不开的形态，量级仍 <10 行。

**P1 同做·PATCH 换 assignee 改道**【v4 修订：限定 API 层】：`api/tasks.py` 的 update 端点检测 assignee_id 变更时改走 `reassign_task`——**只在端点层改，不动 crud.update_task**，因为 dispatch.py:176（transfer）与 verify_spawn.py:566 内部直调 update_task，crud 层改道会波及它们。须披露的语义变化：reassign_task 会把 reviewing 任务强制打回 claimed 且 progress 重置 10、对 archived/terminal 任务抛错（现状不抛）；前端从不调 PATCH /tasks，影响面仅外部 API 调用方。

**P3 留下一处**：close.py merge 阻塞回置 approved（低频）。

### 4.7 索引迁移（6 个，`db/schema.py` PROJECT_DB_INDEXES，幂等）

```sql
-- 单任务聚合（端点 1）
CREATE INDEX IF NOT EXISTS idx_work_logs_task ON work_logs(task_id);
CREATE INDEX IF NOT EXISTS idx_inbox_task ON inbox(task_id);
CREATE INDEX IF NOT EXISTS idx_handoffs_task ON handoffs(task_id);
-- 时间窗聚合（端点 2）【v4 新增：v3 漏项，游标分页救不了全表扫】
CREATE INDEX IF NOT EXISTS idx_task_events_created ON task_events(created_at);
CREATE INDEX IF NOT EXISTS idx_handoffs_created ON handoffs(created_at);
CREATE INDEX IF NOT EXISTS idx_work_logs_created ON work_logs(created_at);
```

（per-project DB 本身按项目隔离，无需 project_id 前缀。）

### 4.8 明确不做的（防镀金）

- 不为 json_extract 兜底建表达式索引/生成列；
- 不做服务端事件聚合缓存表（等真实 profile）；
- 不把 handoffs 改成历史表；
- 不加 `chat_messages.task_id` 迁移（单独立项）；
- 不持久化沉默/健康事件（第一期用现有信号推断）；
- 不在第一期补 close.py 回置盲区（低频，P3）。

---

## 五、前端设计

### 5.1 挂载点（完整清单）

关键约束：**右栏内容区整体被 `selectedAgentId` 门控**（App.tsx:702，未选 agent 渲染占位符，Goals 内容也不例外），任务选中不蕴含 agent 选中（切项目会置 null）。task tab 必须放门控之外：

1. `src/store.ts`：
   - `activeView` 联合类型加 `"timeline"`；
   - `rightPanelTab` 联合类型加 `"task"`，**同时补入现有的 `"debug"`**（现状靠 `"debug" as any` 绕过，顺手清掉 App.tsx 两处 as any）；
   - 新增 `selectedTaskId: string | null` + `setSelectedTask()`；
   - 新增 `notifyTaskEvent(projectId, taskId)` action + 防抖标记（见 5.4）。
2. `src/App.tsx`：
   - 左栏 tab bar 加第三个视图切换按钮（timeline 入口）；
   - 左栏内容区二元三元改三路分支；
   - 右栏 tab bar：「任务」按钮放独立 `{selectedTaskId && (...)}` 分支，与 agent 块平级；
   - 右栏内容区改三路：`rightPanelTab === "task" && selectedTaskId ? <TaskTimelinePanel/> : !selectedAgentId ? 占位符 : 现有面板`；
   - `handleSwitchProject` 与 `detachDeletedProject` 清 `selectedTaskId`，tab 为 task 时回落；
   - OrgTree.handleSelect 在 task tab 激活时保持不动（不误切走正在看的任务）。

### 5.2 组件（新建 `src/components/timeline/`，行数预算）

| 文件 | 职责 | 预算 |
|------|------|------|
| `types.ts` | TimelineEvent / TaskSegment / ActiveAssignment 类型 | ~80 |
| `utils.ts` | 状态颜色表（**显式含 cancelled**：slate-400+斜纹）、事件标题生成、时间格式化 | ~120 |
| `usePanZoom.ts` | 缩放平移 hook（从 OrgTree.tsx:840-981 内联实现提取重写，后续 OrgTree 可反向复用） | ~120 |
| `TeamTimeline.tsx` | 泳道容器：布局、当前时刻线、点击联动（只留容器） | ~400 |
| `TeamTimelineLane.tsx` | 单泳道行 + 任务段渲染 | ~200 |
| `TaskTimelinePanel.tsx` | 元信息卡 + 垂直事件流（按游戏日分组折叠）——**全新写**，MessageBubble 与 ChatMessage 形状强耦合不可复用，借鉴 WorkLogEntry 的徽章+摘要+折叠模式 | ~350 |
| `TaskPicker.tsx` | 搜索下拉（现有 list 端点，排除归档）**+ task_id 直达输入框**（归档任务唯一入口） | ~150 |
| `MiniMap.tsx` | 底部 40px 密度总览条 + 可拖视口框（粗粒度 div/CSS 密度条实现，不追求丝滑拖拽） | ~80（偏紧，超则砍 brush 保预设档位） |
| `TimeAxis.tsx` | 时间刻度 + Day 标签 | ~80 |
| `TimelineTooltip.tsx` | hover 浮层卡片 | ~50 |
| `useDeepLink.ts` | URL hash 同步 `view/taskId/since/until` | ~60 |

复用项：游戏时间换算 re-export `src/utils/game-time.ts`；WorkLogPanel.tsx:53-68 的 `formatTime`/`formatClock` 提为共享 util（WorkLogPanel 反向引用）。泳道行排序用端点 2 的 `agents[].parent_id` 自建树（store 无 org tree 数据，OrgTree/OfficeView 均为组件本地 state，单一数据源）。

### 5.3 视觉清单（Tailwind 现成，零新依赖）

| 项 | 做法 |
|---|---|
| 语义色 token | `utils.ts` 集中一张表（先例：`utils/role-styles.ts`），走 Tailwind 色板 + `colors.g.*` 主题 token【v4 修正：项目无 CSS 变量体系，全库零 `:root`/`var(--…)`，主题就是 tailwind.config.js hex token】：running=emerald-500、waiting=amber-400、blocked=orange-500、reviewing=violet-500、approved/done=sky-500、cancelled=slate-400+斜纹、待认领空段=灰底虚线边框。禁止散落 hex |
| 布局骨架 | 左列 agent 栏 `sticky left-0`（头像色点+名字+角色徽章+实时健康点，复用 store agentHealth），顶部时间轴 `sticky top-0`。注：项目内 sticky 零先例，属首创但无冲突 |
| 当前时刻 | 红色 1px 竖线 + 顶端脉冲圆点（CSS animation） |
| 游戏时间 | Day 边界竖带（bg-slate-100 交替）+ 顶部副刻度「Day N」 |
| 任务块 | 高 22px 圆角，空间够时内嵌「等评审 3h12m」相对时长；hover tooltip（标题/状态/起止/负责人/评审人） |
| 空段纹理 | 待认领段 `repeating-linear-gradient` 斜纹，与无任务空白区分 |
| Mini-map | 底部 40px 总览条 + 可拖视口框（「好看」里性价比最高项） |
| 动效 | 块 fade-in 150ms、视口 transform transition，全 CSS |
| 三态 | 空态照抄 WorkLogPanel:621-626 先例；错误+重试照抄 MonitorPanel:357-365 / GoalsPanel:158-167 先例；**骨架屏无先例需新写**【v4 修正】——tailwind.config.js 已定义未使用的 shimmer 动画可直接用 |

### 5.4 WS 失效信号（前端侧）【v4 修订：频道与插入点改正】

- 后端发到 `lobby:status`（4.1），前端在 `api/ws.ts` 现有 lobby 分支（:409 activity 监听旁）加 `channel.on("task_event")`——**不走 addActivity 的 agentId 硬门路径**（:410 会丢弃），也**不进 activityFeed**（会污染 Logs/WorkLogPanel 渲染）；
- 路由到 store 新 action `notifyTaskEvent`：按 `project_id === selectedProjectId` 过滤，置失效标记，防抖 1s 后重拉 activity 端点；若当前打开的 TaskTimelinePanel 的 task_id 匹配则同步重拉事件流；
- 30s 兜底轮询保留（仅 timeline 视图挂载期间，随卸载停止——与 OrgTree 的 9-11s 轮询同模式），与 WS 双通道；
- 预算 ~30 行（含 ws.ts 分支 + store action + 防抖），**P1 落地**（后端 P1 已发事件，前端同接，避免 P1→P2 之间广播无人消费）。

### 5.5 便捷性清单

1. **深链 URL hash**（`view/taskId/since/until`）——调试时把链接贴给同事/AI 即同视角（TEST18 复盘核心痛点）；
2. **一键复制任务链路为 Markdown**——TaskTimelinePanel 顶部按钮，事件流渲染成紧凑表格，贴进对话框让 AI 分析「卡哪了」，本功能最高频下游动作；
3. 键盘：`j/k` 切任务、`←/→` 平移、`+/-` 缩放、`0` 回现在、`Esc` 清选中（P3）；
4. 时间刷选 brush + 预设档位（最近 1h / 最近 1 游戏日 / 全部）；
5. 筛选条：agent 多选 chip / 状态多选 / 标题关键字，纯客户端过滤；
6. 「跳到现在」浮动按钮（视口不含当前时刻时出现）；
7. 异常视觉锚点：stall 催办、review_rework 打回节点红色左边框 + 图标（P3）；
8. 事件按游戏日分组折叠（P1 随 TaskTimelinePanel 落地）。

---

## 六、分期交付

### P1：任务全链路回放 + 数据可信度

范围：
- 后端：6 个索引（4.7）；`insert_task_event` 共享辅助 + lobby WS 发布（4.1）+ 9 个写入点改经辅助；obligation/dismiss 两处盲区补写（4.6）；PATCH 端点层改道；`get_project_db_readonly` 只读池（4.4）；端点 1（含四路同事务）；
- 前端：TaskTimelinePanel + TaskPicker + 右栏 task tab + 5.1 全部挂载改动 + rest.ts 函数 + WS 失效分支（5.4）。

验收：
- 曾卡住的任务一屏看到完整流转链；打回显示 reviewing→running + `review_rework` 标注；换人显示 task.reassigned（from→to）；归档任务可 task_id 直达；
- dismiss 一批 agent 后时间轴无瞬移（批量 cancelled/reassigned 事件在位）；
- PATCH 换 assignee 产生 task.reassigned 且 dispatch/verify_spawn 内部路径行为不变（回归）；
- WS 断连/丢事件场景实测退化为 30s 轮询体验，无视图卡死；
- 只读连接并发两请求无 BEGIN 交错；evict 后重开正常。

### P2：团队泳道总览（好看版）

范围：端点 2（含切段算法、游标分页）；TeamTimeline 全套 + MiniMap/TimeAxis/Tooltip；5.3 视觉清单；便捷清单 1/2/4/5/6；深链 hook。

验收：一眼看出谁忙谁闲、哪个任务多人交接；评审/等待段正确归 waiting 色；点击任务块跳详情；深链可分享；**A1 止损线检查**（系文件合计行数 + 手感，超线触发降级方案）。

### P3：打磨与扩展

close.py 回置盲区收尾；PATCH 语义变化文档化后的进一步收口评估；cursor 分页按实测数据量决定提前与否；键盘快捷键（5.5.3）、异常锚点（5.5.7）；沉默/健康历史持久化（`agent_activity_events`）；`chat_messages.task_id` 迁移；OrgTree/像素办公室点 agent → timeline 定位高亮。

---

## 七、风险与对策

| 风险 | 对策 |
|------|------|
| WS 事件丢失/乱序导致视图不更新 | WS 只作失效信号不作数据源（写死）；30s 兜底轮询对时 |
| lobby 广播串项目 | 载荷带 project_id，前端按 selectedProjectId 过滤 |
| 只读连接路径漂移（db 被删/驱逐） | 接入 evict_project_db + `_evicted_workspaces` 检查；打开失败降级共享连接 |
| 只读池并发事务交错 | 每连接一把 asyncio.Lock，请求持锁至 COMMIT |
| 时间窗聚合全表扫 | 4.7 三个 created_at 索引先行；truncated 显式标记不静默截断 |
| 自绘甘特行数/手感失控 | A1 止损线：超 800 行降级静态泳道 + 离散档位 |
| 盲区补写引入新事件形态 | 只复用 task.archived/task.reassigned 既有 event_type；dismiss 经共享辅助直 INSERT（唯一可行入口，已论证）；回归测试跟在每个补写点后 |
| PATCH 改道语义变化（reviewing→claimed + progress 重置） | 仅 API 端点层改道，crud 层不动（保护 dispatch/verify_spawn 内部调用方）；变更写进端点文档 |
| Mini-map/brush 交互蔓延 | P2 验收行数检查；超预算先砍 brush 保预设档位 |
| work_logs 任务关联覆盖率低、时间轴有空洞 | json_extract 兜底限定 type='task_event'（明确预期）；turn_result 的 waiting_on[].ref 提取列 P3 评估 |

---

## 八、文件落点汇总

**后端新增**
- `services/tasks/timeline.py` — 事件聚合 + 切段 + 只读事务查询
- `api/timeline.py` — 两个 REST 端点

**后端修改**
- `api/router.py` — `_SUB_ROUTERS` 注册（一行）
- `db/schema.py` — 6 个索引
- `db/project.py` — `get_project_db_readonly` + 池锁 + evict 联动（~40 行）
- `services/tasks/db.py`（或 events.py）— `insert_task_event` 共享辅助（INSERT + bus.publish，~30 行）
- `services/tasks/transitions.py` / `crud.py` / `claim.py` / `close.py` / `tools/tasks/verify_spawn.py` — 事件写入改经共享辅助（机械改动，每点近零增量）
- `services/org.py` — dismiss 盲区补写（经辅助直 INSERT，<10 行）
- `services/obligation.py` — 裸 UPDATE 改 `_transition`（<10 行）
- `api/tasks.py` — update 端点 assignee 变更改道 reassign_task（~10 行，仅端点层）

**前端新增**
- `components/timeline/{types,utils,usePanZoom,TeamTimeline,TeamTimelineLane,TaskTimelinePanel,TaskPicker,MiniMap,TimeAxis,TimelineTooltip,useDeepLink}`

**前端修改**
- `src/store.ts` — 联合类型扩展（含补 "debug"）+ selectedTaskId + notifyTaskEvent（~30 行）
- `src/App.tsx` — 左栏按钮与三路分支、右栏平级 task tab、切项目清理、handleSelect 策略、清两处 as any
- `src/api/rest.ts` — 两个 fetch 函数 + 响应类型
- `src/api/ws.ts` — lobby task_event 监听分支
- `src/components/WorkLogPanel.tsx` — formatTime/formatClock 提取共享后反向引用（小改）

**不动的**：`chat_messages` 表、phoenix_adapter（lobby 桥接已够用）、game-time tick、TaskService 状态机、close.py 回置路径（P3）、crud.update_task（保护内部调用方）。

---

## 九、开发流程约束（AGENTS.md）

- 单次代码改动 >20 行必须子代理审计后交付（本方案自身即经三轮审计产出）；
- 新逻辑全部落领域模块，不进 shim（agent.py / task_tools.py / ChatPanel.tsx / api.ts / services/task.py）；
- 工具输出分层：timeline 端点自带 limit/truncated 契约，不把大结果直接 dump 给调用方；
- 状态枚举不硬编码数量，但颜色表须显式枚举全部落库状态（含 cancelled）。

---

## 十、版本留档

- **v1**：初版方案（两视图 + 聚合接口骨架）。
- **v2**：子代理审计修订——task_events 盲区、rework 形态、事件类型全集、切段算法重写（assignee 游标）、handoffs 无历史、json_extract 收窄、索引迁移、cancelled 状态、右栏 selectedAgentId 门控等 8 处修正。
- **v3**：体验/性能升级——WS 失效信号（B1）、盲区补写提前（B2）、只读连接 + 事务一致性（B3）、游标分页（B4）、视觉/便捷清单（A2/A3）、自绘裁决带止损线（A1）。
- **v4（本版）**：对 v3 的审计修正——publish 频道改 lobby（project 频道端到端不可达）；publish 收口改共享辅助（_transition 非唯一写入点，共 9 点位）；dismiss 补写入口改直 INSERT（_transition/emit_task_event 均走不通）；PATCH 改道限定 API 层（保护 dispatch/verify_spawn）；只读池补事务锁与驱逐联动；补 3 个时间窗索引；前端 WS 分支绕开 agentId 硬门、不进 activityFeed；CSS 变量/骨架屏先例两处措辞修正；前端 WS 分支移入 P1。
