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

### 时间推进

```
1. 每 5s tick：
   a. 计算游戏时间增量：elapsed_real * 86400 / 3600
   b. 更新 game_seconds
   c. 持久化到 DB
   d. 检查到期闹钟
   e. 每 12 ticks（60s）检测停滞 agent
   f. 广播时间到 project:<id>
```

### 闹钟调度

```
1. set_alarm(agent_id, purpose, fire_at_game_seconds):
   a. 插入 scheduled_alarms（status='pending'）
   b. 返回 alarm_id

2. tick 中检查到期闹钟：
   a. SELECT * WHERE status='pending' AND fire_at_game_seconds <= current_game_seconds
   b. 对每个到期闹钟：
      - UPDATE status='fired', fired_at=now
      - 如果有 to_agent_id → InboxService.send_message(alarm)
      - 触发 Agent.trigger_coordinator 或 trigger_subordinate
```

### 停滞检测

```
1. 每 60s 检查所有 active agent：
   a. 获取 agent 状态（processing/idle）
   b. processing 状态：current_job.started_at 超过 5 分钟 → 停滞
   c. 其他状态：last_heartbeat 超过 10 分钟 → 停滞
   d. 检查 per-agent cooldown（10 分钟内不重复升级）
   e. 有 parent_id → 发 inbox escalation + 触发 coordinator
   f. 无 parent（CEO）→ PubSub 广播 :user_ping
```

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
| — | Elixir stall 检测用 process dictionary 跟踪 cooldown | Python 用内存 dict 跟踪 |

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
- 游戏时间用 `game_seconds = (now - started_at) * 86400 / 3600` 计算
- 停滞检测用 `dict[agent_id, last_escalation_time]` 跟踪 cooldown
- 闹钟用 DB 查询而非内存定时器（重启可恢复）
