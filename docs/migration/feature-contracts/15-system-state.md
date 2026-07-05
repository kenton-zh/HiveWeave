# 功能契约 15：系统状态与启动恢复

> **本文件是功能契约（层 1），用 spec 语言描述模块的行为契约，不引用实现代码。**
> **任何 AI 工具实现 Python 版本时，必须满足此契约中的所有要求。**

## 元信息

| 项 | 值 |
|---|---|
| 模块编号 | 15 |
| 模块名称 | 系统状态与启动恢复（SystemState + Application） |
| Elixir 源码 | `services/system_state.ex` + `application.ex` |
| TS 参考源码 | `apps/server/src/index.ts`（启动清理） + `game-time-scheduler.ts`（停滞/清理） |
| OpenCode 参考源码 | —（OpenCode 是 CLI，无长驻进程） |
| 状态 | 草稿 |

## 功能概述

两类职责：(1) **全局系统状态**——通过进程内共享内存维护 `system_paused` 布尔标志，agent chat 时检查并拒绝；同时每小时触发一次孤儿审批清理 sweep。(2) **应用启动恢复**——监督树按序拉起各子进程；启动后台异步执行运行时迁移（language/workspace_path 列）、projects 表自愈恢复（从 agents 表反推）、花名迁移（CEO/HR 无花名则生成）、僵尸 streaming 清理、孤儿审批清理、逐项目 boot、唤醒有待办工作的 agent；停机时持久化所有项目的 game time。

## 接口契约

### 输入（Consumes）

| 输入 | 来源 | 格式 | 说明 |
|---|---|---|---|
| 系统暂停查询 | agent chat 入口 | — | 读 ETS `:hiveweave_system_state` 的 `:system_paused` 键 |
| 监督树启动 | OTP Application | — | `Application.start/2` 触发 |
| SIGINT/SIGTERM | 操作系统 | — | 触发 `prep_stop` 钩子 |
| 每小时定时器 | `:timer.send_interval` | `:hourly_cleanup` 消息 | 间隔 3_600_000 ms |

### 输出（Produces）

| 输出 | 目标 | 格式 | 说明 |
|---|---|---|---|
| paused? 返回 | agent chat | `boolean` | true 时 chat 返回 :paused 错误 |
| 启动后状态 | 调用方 | 监督树 pid | 各子进程按序运行 |
| 持久化 game time | per-project DB | — | 停机时写回 |

### 副作用

| 副作用 | 触发条件 | 目标 | 说明 |
|---|---|---|---|
| 写 :system_paused 标志 | pause/resume 调用 | ETS | true/false |
| 孤儿审批清理 | 每小时定时器 + 启动时 | Meta DB permission_requests | 调用 Approval.cleanup_orphaned_requests |
| ALTER TABLE ADD COLUMN | 启动时缺列 | Meta DB projects/agents | language/workspace_path 列 |
| 恢复 projects 行 | agents 有 workspace_path 但 projects 无行 | Meta DB projects | 插入 "Recovered Project" |
| 回填 agents.workspace_path | projects 有行但 agents 缺列值 | Meta DB agents | 用 projects.workspace_path 填充 |
| 更新 agent 花名 | CEO/HR 无花名 | Meta DB agents | generate_flower_name 后 UPDATE name |
| 清除 zombie streaming | 启动时 | per-project DB chat_messages | is_streaming=true 的行重置 |
| 持久化 game time | prep_stop | per-project DB | 调用 GameTime.Server :persist |

## 核心流程

### 系统暂停/恢复

```
1. paused?：读 ETS :system_paused，缺省 false
2. pause：ETS 写 {:system_paused, true}
3. resume：ETS 写 {:system_paused, false}
4. agent chat 入口检查 paused? → true 时返回 :paused 错误，不执行
```

### 每小时审批清理 sweep

```
1. SystemState GenServer init 时设置 :timer.send_interval(3_600_000, :hourly_cleanup)
2. 收到 :hourly_cleanup → 调用 Approval.cleanup_orphaned_requests()
3. 异常捕获并记录 warning，不崩溃 GenServer
```

### Application 启动监督树（rest_for_one 策略）

```
1. Approval.ensure_table()（ETS 表，由本进程持有）
2. 按序拉起子进程：
   SystemState → Telemetry → Repo.Meta → ProjectRegistry →
   Phoenix Endpoint → PubSub → Presence → Task.Supervisor →
   Finch(pool_size=20, 可选 HTTPS_PROXY) → CircuitBreaker →
   EventAudit → ConversationStore → Repo.ProjectFactory → ProjectSupervisor
3. 监督树启动成功 → Task.start(boot_existing_projects) 异步执行启动恢复
```

### boot_existing_projects（异步启动恢复）

```
0a. language 列迁移：PRAGMA table_info(projects) 检查 → 缺则 ALTER TABLE ADD COLUMN language TEXT DEFAULT 'zh'；再将 NULL/'en' 更新为 'zh'
0b. workspace_path 列迁移：PRAGMA table_info(agents) 检查 → 缺则 ALTER TABLE ADD COLUMN workspace_path TEXT
0c. recover_projects_from_agents（见下）
1. clear_stuck_streaming（清除 is_streaming=true 的 zombie 行）
2. cleanup_orphaned_requests（孤儿审批清理）
3. 列出所有 projects（id + workspace_path）
4. migrate_flower_names（见下）
5. 逐项目 ProjectSupervisor.start_project(id, workspace_path)
6. 延迟 1s 后 wake_agents_with_pending_work（有待办 inbox/handoff 的 agent 触发 trigger_subordinate）
```

### recover_projects_from_agents（双向自愈）

```
1. 合并 projects 表 + agents 表的所有 project_id
2. 对每个 project_id 取 projects.ws 和 agents.ws：
   a. 两者一致 → 无操作
   b. projects 无行 + agents 有 ws → 插入 "Recovered Project"（on_conflict replace workspace_path）
   c. projects 有行 + agents 缺 ws → 回填 agents.workspace_path
   d. 两者都有但不一致 → 信任 projects 表，修正 agents
   e. 两者都无 → 记 warning，无法恢复
```

### migrate_flower_names

```
1. 遍历每个 project 的 role in [ceo, hr, CEO, HR] 的 agent
2. 若 agent.name 不是花名（is_flower_name? 为假）→ generate_flower_name + UPDATE name
3. 在 ProjectSupervisor.start_project 之前完成，使新 GenServer 用新名
```

### 优雅停机（prep_stop）

```
1. prep_stop 在监督树拆除前调用
2. 从 ProjectRegistry 取所有 project_id
3. 对每个项目的 GameTime.Server 进程调用 :persist（5s 超时）
4. 进程不存在或调用失败 → 忽略
```

## 状态机（如适用）

| 当前状态 | 触发条件 | 目标状态 | 动作 |
|---|---|---|---|
| 运行中 | pause() | 暂停 | 写 :system_paused=true；agent chat 拒绝 |
| 暂停 | resume() | 运行中 | 写 :system_paused=false |
| 启动中 | 监督树就绪 | 启动恢复中 | 异步 boot_existing_projects |
| 启动恢复中 | 全部项目 boot 完成 | 运行中 | 唤醒待办 agent |
| 运行中 | SIGINT/SIGTERM | 停机中 | prep_stop 持久化 game time |

## 错误处理

| 错误场景 | 处理方式 | 重试策略 | 升级策略 |
|---|---|---|---|
| 迁移 ALTER TABLE 失败 | try/rescue 记 warning，继续启动 | 不重试 | 不阻塞启动 |
| recover_projects 失败 | try/rescue 记 warning | 不重试 | 不阻塞启动 |
| clear_stuck_streaming 失败 | try/rescue 记 warning | 不重试 | 不阻塞启动 |
| 单项目 boot 失败 | 记 warning，继续下一项目 | 不重试 | 不影响其他项目 |
| 花名迁移失败 | try/rescue 记 warning | 不重试 | 不阻塞启动 |
| 每小时清理失败 | try/rescue 记 warning | 下个周期重试 | 不崩溃 GenServer |
| persist 超时 | 5s 超时后忽略 | 不重试 | 该项目 game time 可能丢失 |
| 已启动项目再次 start | 返回 {:error, {:already_started, pid}} | — | 视为成功 |

## 常量引用

| 常量 | 值 | 所在章节 |
|---|---|---|
| 每小时清理间隔 | `3_600_000` ms | 本契约（SystemState） |
| Finch pool_size | `20` | Finch HTTP 客户端 |
| persist 超时 | `5_000` ms | 本契约（prep_stop） |
| wake_agents 延迟 | `1_000` ms | 本契约（boot 流程） |
| 默认 language | `zh` | 本契约（迁移默认值） |
| 游戏时间 tick 间隔 | `5` 秒 | 游戏时间 |
| 停滞检测间隔 | `60` 秒 | 游戏时间 |
| 停滞阈值（processing/idle） | `5`/`10` 分钟 | 游戏时间 |
| 端口 | `4000` | 环境变量 |

## 已知问题

| 问题编号 | 说明 | Python 迁移处理 |
|---|---|---|
| E3 | Elixir 停滞阈值 5/10min vs TS 15min | 采用 Elixir 双阈值（constants.md 已确认） |
| T3 | TS 无 supervisor 无自动重启 | Python 用 FastAPI lifespan + asyncio 任务重启逻辑 |
| — | SQLite 不支持 ADD COLUMN IF NOT EXISTS，需 PRAGMA 预检查 | Python 用 PRAGMA table_info 检查后再 ALTER |
| — | recover_projects 用 insert_all 绕过 changeset 校验 | Python 直接 SQL INSERT，提供 name+created_at 必填字段 |
| — | prep_stop 时若 GameTime 进程已死则跳过 | Python 用 try/except 包裹 persist 调用 |
| — | boot_existing_projects 异步执行，监督树启动时 projects 可能未就绪 | Python 用 lifespan 启动后 background task，不阻塞 HTTP 服务 |

## Python 实现建议

- **框架/库**：`FastAPI` lifespan 事件（startup/shutdown）；`asyncio.create_task` 启动后台恢复；`aiosqlite` 做 PRAGMA/ALTER
- **架构模式**：
  - `SystemState` 单例类，`paused` 用 `asyncio.Event` 或模块级 `bool` + lock
  - 每小时清理用 `asyncio.create_task` + `asyncio.sleep(3600)` 循环
  - 启动恢复用 lifespan startup 触发的 background task
  - 停机持久化用 lifespan shutdown
- **注意事项**：
  - SQLite migration 用 PRAGMA table_info 检查列存在性，不可直接 ADD COLUMN IF NOT EXISTS
  - recover_projects 的双向自愈逻辑要完整实现四种分支
  - wake_agents 的 1s 延迟保留（等 per-project DB 就绪）
  - 多实例部署时 pause 标志需 Redis 共享

## 验收标准

- [ ] paused? 默认返回 false
- [ ] pause() 后 paused? 返回 true，agent chat 返回 :paused 错误
- [ ] resume() 后 paused? 返回 false
- [ ] 每小时定时器触发 Approval.cleanup_orphaned_requests
- [ ] 每小时清理异常不崩溃系统
- [ ] 启动时执行 language 列迁移（缺则 ADD COLUMN，默认 'zh'）
- [ ] 启动时执行 workspace_path 列迁移
- [ ] recover_projects 双向自愈：projects 缺行 + agents 有 ws → 重建 projects 行
- [ ] recover_projects 双向自愈：projects 有行 + agents 缺 ws → 回填 agents
- [ ] recover_projects 两者不一致时信任 projects 表
- [ ] 启动时清除 is_streaming=true 的 zombie 行
- [ ] 启动时清理孤儿审批请求
- [ ] CEO/HR 无花名时启动迁移为花名
- [ ] 花名迁移在 ProjectSupervisor.start_project 之前完成
- [ ] 逐项目 boot，单项目失败不影响其他
- [ ] wake_agents 唤醒有待办 inbox/handoff 的 agent
- [ ] 优雅停机时持久化所有项目 game time
- [ ] persist 超时或进程不存在时忽略
- [ ] 监督树策略 rest_for_one（前者崩溃后续重启）

## 并行对比测试方案

| 测试场景 | Elixir 行为 | Python 预期行为 | 验证方法 |
|---|---|---|---|
| pause 后 chat | 返回 :paused 错误 | 同 | 调用 chat 接口对比 |
| resume 后 chat | 正常执行 | 同 | 调用 chat 接口对比 |
| 启动时缺 language 列 | ALTER ADD COLUMN + 默认 'zh' | 同 | 查 PRAGMA + 表数据 |
| projects 行被删 + agents 有 ws | 重建 "Recovered Project" | 同 | 查 projects 表 |
| agents 缺 ws + projects 有行 | 回填 agents.workspace_path | 同 | 查 agents 表 |
| CEO 无花名启动 | name 变为花名 | 同 | 查 agents.name |
| is_streaming=true 残留 | 启动后清除 | 同 | 查 chat_messages |
| 有待办 inbox 的 agent | 启动后被唤醒 | 同 | 观察 agent 状态变化 |
| SIGTERM 停机 | game time 持久化 | 同 | 重启后 game time 一致 |

---

> **填写规则**：
> 1. 用 spec 语言，不用代码语言。说"做什么"，不说"怎么做"。
> 2. 所有常量值引用 `constants.md`，不在此处重复定义。
> 3. 所有已知问题引用 `known-issues.md`，不在此处重复描述。
> 4. 每个契约必须经过用户确认后才能标记为"已确认"。
> 5. "Python 实现建议"是建议不是约束，实现者可以根据情况调整，但必须满足验收标准。
