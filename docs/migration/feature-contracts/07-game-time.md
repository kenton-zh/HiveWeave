# 功能契约 07：游戏时间

> **本文件是功能契约（层 1），用 spec 语言描述模块的行为契约。**

## 元信息

| 项 | 值 |
|---|---|
| 模块编号 | 07 |
| 模块名称 | 游戏时间 |
| Elixir 源码 | `game_time/server.ex` |
| TS 参考源码 | `apps/server/src/game-time-scheduler.ts` + `packages/core/src/game-time-service.ts` + `packages/core/src/alarm-service.ts` |
| OpenCode 参考源码 | 无（CLI 工具无游戏时间） |
| 状态 | 草稿 |

## 功能概述

每个项目独立的模拟时钟，1 真实小时 = 1 游戏天（86400 游戏秒）。每 5 秒 tick 一次，持久化游戏时间、派发到期闹钟、广播时间更新、检测停滞 agent 并向上级升级。闹钟和游戏时间均持久化到 per-project SQLite，重启可恢复。

## 接口契约

### 输入

| 输入 | 来源 | 格式 | 说明 |
|---|---|---|---|
| tick 信号 | 定时器 | 每 5s | 触发时间推进 |
| alarm | set_alarm 工具 | `{from_agent_id, to_agent_id, purpose, fire_at_game_seconds}` | 调度闹钟 |
| cancel | cancel_alarm | `{alarm_id}` | 取消闹钟 |

### 输出

| 输出 | 目标 | 格式 | 说明 |
|---|---|---|---|
| 游戏时间 | get_project_time 工具 | `game_seconds` | 当前模拟时间 |
| 实际时间 | get_real_time 工具 | ISO string | 当前现实时间 |
| 到期闹钟 | InboxService | alarm 消息 | 到时通知 agent |
| 停滞升级 | InboxService | escalation 消息 | 通知上级 |
| 时间更新 | PubSub project:<id> | `{gameSeconds, realTimestamp}` | 前端显示 |

### 副作用

| 副作用 | 触发条件 | 目标 | 说明 |
|---|---|---|---|
| 持久化游戏时间 | 每 tick | per-project DB `game_time_state` | 单行 id='singleton' |
| 闹钟状态更新 | 闹钟到期 | per-project DB `scheduled_alarms` | status → 'fired' |
| 发送 alarm 消息 | 闹钟到期 | InboxService | 通知 to_agent_id |
| 发送升级消息 | 停滞检测 | InboxService | 通知上级 |
| 广播时间 | 每 tick | PubSub project:<id> | 前端更新 |
| 触发 coordinator | 停滞检测 | Agent.trigger_coordinator | 唤醒上级处理 |

## 数据模型

### game_time_state 表

```sql
CREATE TABLE game_time_state (
  id TEXT PRIMARY KEY DEFAULT 'singleton',
  game_seconds INTEGER DEFAULT 0,
  updated_at INTEGER
);
```

### scheduled_alarms 表

```sql
CREATE TABLE scheduled_alarms (
  id TEXT PRIMARY KEY,
  project_id TEXT,
  from_agent_id TEXT,
  to_agent_id TEXT,
  purpose TEXT,
  fire_at_game_seconds INTEGER,
  status TEXT DEFAULT 'pending',   -- pending | fired | cancelled
  fired_at INTEGER,
  created_at INTEGER
);
```

## 核心流程

### 时间推进（绝对时间模型）

> **RECONCILE 说明**：源码 `server.ex` 使用**绝对时间模型**而非增量累加。GenServer 持有
> `real_started_at`（基准真实时间戳），每次 tick 用 `game_seconds = div((now - real_started_at) * 86400, 3600)`
> 重新推导当前游戏时间。这样即使跳过若干 tick（如 GC 暂停）时间仍准确。重启时从 DB 读出
> `game_seconds`，反算 `real_started_at = now - div(seconds * 3600, 86400)` 恢复基准。
> 契约原描述"计算游戏时间增量"易误读为累加模型，已修正为绝对推导。

```
1. 每 5s tick：
   a. 绝对推导：elapsed_real = now - real_started_at
   b. game_seconds = div(elapsed_real * 86400, 3600)
   c. 更新 state.current_game_seconds
   d. 持久化到 DB（INSERT OR REPLACE game_time_state 单行）
   e. 检查到期闹钟（fire_at_game_seconds <= game_seconds 且未 fired）
   f. 每 12 ticks（60s）spawn 检测停滞 agent
   g. 广播 {:game_time_tick, game_seconds} 到 project:<id>
```

> **优雅停机持久化**：`Application.prep_stop/1` 在监督树拆除前对所有项目 GenServer 调用
> `:persist`，最后一次推导并写回 DB，避免重启后时间回退。

### 闹钟调度

```
1. set_alarm(agent_id, purpose, fire_at_game_seconds):
   a. normalize_alarm（兼容 atom/string key，缺 id 则生成 UUID）
   b. 插入 scheduled_alarms（status='pending', fired=0）
   c. 返回 alarm_id

2. tick 中检查到期闹钟：
   a. 内存过滤：fire_at_game_seconds <= current_game_seconds 且 not fired
   b. 对每个到期闹钟：
      - fire_alarm：广播 {:alarm_fired, alarm} 到 agent:<to> 和 project:<id>
      - mark_alarm_fired：UPDATE fired=1, fired_at=now, status='fired'
      - 如果有 to_agent_id → InboxService.send_message(alarm) + Agent.trigger_subordinate
   c. 从内存列表移除已 fired 闹钟

3. cancel_alarm(alarm_id):
   a. UPDATE scheduled_alarms SET status='cancelled' WHERE id=?
   b. 从内存列表移除
```

> **RECONCILE — agent 解散后闹钟清理缺失（已知问题）**：源码 `Org.dismiss_agent/2` 仅做
> 软删除（status='archived'）+ 归档记忆，**不清理**该 agent 相关的 `scheduled_alarms`。
> 若被 dismiss 的 agent 是某个闹钟的 `to_agent_id`，闹钟到期时仍会尝试 `Inbox.send_message`
> 和 `Agent.trigger_subordinate`（目标进程已不存在，trigger 静默失败）。`server.ex` 也无
> dismiss 钩子。Python 迁移应在 dismiss 流程中 `UPDATE scheduled_alarms SET status='cancelled'
> WHERE to_agent_id = ? AND status='pending'`，避免对已归档 agent 触发无效闹钟。

### 停滞检测

```
1. 每 60s（12 ticks）spawn 一个一次性进程 check_stalled_agents(project_id)：
   a. Org.list_agents(project_id) 取所有 agent
   b. 仅对 status='active' 的 agent 调 GenServer.call(:get_state, 3s)
   c. cond:
      - status==:processing 且 current_job.started_at 超过 5min → 停滞
      - 其他状态：last_heartbeat 超过 10min → 停滞
      - GenServer 3s 超时 → 停滞（"GenServer timeout"）
      - 进程不存在（noproc）→ 跳过（未启动，非停滞）
   d. 收集 stalled 列表 → send 给 GameTime GenServer（{:stalled_agents, stalled}）

2. GameTime GenServer 收到 {:stalled_agents, stalled} 后逐个 escalate_stall：
   a. cooldown 去重：Process.get({:stall_alert, agent.id}) 取上次升级时间
   b. 10min 内已升级过 → 仅 log，跳过
   c. 否则 Process.put 记录时间，执行 do_escalate_stall
   d. 有 parent_id → Inbox.send_message(escalation) + Agent.trigger_coordinator(parent)
   e. 无 parent（CEO）→ PubSub 广播 {:user_ping, ...} 到 project:<id>
```

> **RECONCILE — cooldown 重启丢失（已知问题）**：源码 `escalate_stall/3` 将 per-agent
> cooldown 时间戳存在 **GameTime GenServer 的 process dictionary**（`Process.get/put`），
> 纯内存、非持久化。**服务重启后 cooldown 全部丢失**，重启后首次停滞检测会对所有停滞
> agent 立即升级（无去重）。stalled 检测本身 spawn 在一次性进程里，故必须把结果 send 回
> 长寿命的 GameTime GenServer 才能让 cooldown 跨检测周期生效。Python 迁移建议：用一张
> `stall_cooldowns` 表（或 per-project 内存 dict + 启动时从 DB 恢复）持久化
> `{agent_id, last_escalation_at}`，重启后仍可去重。

## 常量引用

| 常量 | 值 | 所在章节 |
|---|---|---|
| `REAL_SECONDS_PER_GAME_DAY` | `3600` | 游戏时间 |
| 游戏秒/天 | `86400` | 同上 |
| 时间缩放比 | `24`（86400/3600） | 同上 |
| tick 间隔 | `5_000` ms | 同上 |
| 停滞检测间隔 | `60` s（12 ticks） | 同上 |
| 停滞阈值（processing） | `5` 分钟 | 同上（已确认） |
| 停滞阈值（idle） | `10` 分钟 | 同上（已确认） |
| 停滞升级 cooldown | `10` 分钟 | 同上 |

## 已知问题

| 问题编号 | 说明 | Python 迁移处理 |
|---|---|---|
| E3 | Elixir 和 TS 停滞阈值不同 | 已确认用 Elixir 双阈值 5min/10min |
| — | Elixir 游戏时间存 per-project DB 表，TS 存 meta DB projects 字段 | 用 per-project DB 表（对齐 Elixir） |
| — | Elixir get_current_time 在 GenServer 不存在时返回 0 | 容错处理 |
| — | Elixir stall 检测用 GameTime GenServer 的 process dictionary 跟踪 cooldown（纯内存） | **重启即丢失**；Python 应持久化 `{agent_id, last_escalation_at}` 到 DB，启动时恢复 |
| — | `Org.dismiss_agent/2` 不清理被解散 agent 的 pending 闹钟 | dismiss 流程中 `UPDATE scheduled_alarms SET status='cancelled' WHERE to_agent_id=? AND status='pending'` |
| — | 时间模型为绝对推导（基于 real_started_at），非增量累加 | Python 用 `(now - started_at) * 86400 / 3600` 绝对推导，与源码一致 |

## 验收标准

- [ ] 1 真实小时 = 1 游戏天（3600 秒 = 86400 游戏秒）
- [ ] 每 5 秒 tick 推进游戏时间
- [ ] 游戏时间持久化到 per-project DB
- [ ] 重启后从 DB 恢复游戏时间
- [ ] set_alarm 创建 pending 闹钟
- [ ] 到期闹钟自动触发，发 inbox 消息
- [ ] 闹钟状态 pending → fired
- [ ] cancel_alarm 将状态改为 cancelled
- [ ] 每 60s 检测停滞 agent
- [ ] processing 5min / idle 10min 触发升级
- [ ] per-agent 10min cooldown 防重复升级
- [ ] 有上级 → inbox escalation + 触发 coordinator
- [ ] 无上级（CEO）→ user_ping 广播
- [ ] 广播时间到 project:<id> 频道

## Python 实现建议

- `asyncio.create_task(game_time_tick())` 每 5s 执行
- **绝对时间模型**（对齐源码）：存储 `real_started_at`，`game_seconds = (now - started_at) * 86400 / 3600`；重启时从 DB 读 `game_seconds` 反算 `started_at`，而非增量累加
- 优雅停机：注册 `atexit` / signal handler 最后一次推导并写回 DB
- 停滞检测 cooldown 用 `dict[agent_id, last_escalation_time]` 跟踪，**并持久化到 DB**（启动时恢复），避免重启后丢失去重
- 闹钟用 DB 查询而非内存定时器（重启可恢复）；agent dismiss 时同步取消其 pending 闹钟
- stall 检测 spawn 在独立 task，结果回传给长寿命协程以保持 cooldown 跨周期生效
