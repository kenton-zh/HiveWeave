# HiveBoard — 实时协作看板（产品需求）

> 给 CEO 的部分到此为止（本节及以上）。以下为产品需求本身。

## 背景

小团队需要一个轻量、可自托管的多人协作看板，覆盖"开个会、列个清单、分个工"的日常场景。不要 Trello 全功能，只要核心够用、实时、可演示。

## 用户与场景

- 3–5 人小团队同时在线
- 创建/编辑/删除卡片，拖拽在列间移动
- 所有人实时看到彼此的操作
- 刷新或重启后状态不丢

## 功能需求

1. **看板**：至少 3 列（如 待办 / 进行中 / 已完成），列可重命名
2. **卡片**：标题 + 描述 + 创建者 + 时间戳；支持增删改
3. **拖拽**：卡片可在列内排序、可跨列移动；操作对所有人实时可见
4. **在线用户**：显示当前在线用户列表；可选——高亮正在操作的用户
5. **持久化**：看板状态落盘，服务进程重启后恢复

## 非功能需求

- 实时同步延迟 ≤ 1 秒（同局域网内）
- 单服务进程，资源占用合理
- 可一条命令拉起前后端

## 验收标准

1. 启动后端 + 前端，浏览器访问看到空看板
2. 创建一张卡片，拖到"进行中"列
3. 开第二个浏览器窗口模拟第二用户，两边看到同一看板；一方拖动卡片，另一方 1 秒内可见
4. 重启后端，看板状态恢复
5. **提供可重复运行的验证流程**，覆盖以上 1–4；核心交互路径（创建、拖拽、实时同步、重启恢复）需被自动化执行并断言结果——不能仅靠静态截图判定通过

## 不做（Out of Scope）

- 用户认证 / 权限系统（单租户即可）
- 附件、评论、子任务
- 移动端适配（桌面 Web 即可）
- 历史版本 / 审计日志

---
---

# 【仅小申本人查看，不要发给 CEO】TEST22 观察清单

本次跑的是平台修复后的回归测试。CEO 只拿到上面的产品需求、没有任何开发规则——**测试的就是"只给需求时 agent 能否规范地开发"**。你在运行中盯以下信号，跑完我会再做一次 DB 取证对照 TEST21 基线。

## 量化基线（TEST21 → TEST22 目标）

| 指标 | TEST21 实测 | TEST22 目标 | 看哪儿 |
|---|---|---|---|
| 取消率 | 35%（7/20） | <10% | `tasks` 表 status=cancelled 计数 |
| 超时烧时占比 | ~25%（4×9min） | <5% | `agent_runs` status=error + error_reason 含"总超时" |
| SILENCE WATCHDOG 误报 | 6 次 | 0 | `inbox` message LIKE '%SILENCE WATCHDOG%' |
| reassign 静默漂移 | task_events 0 条 | 0（应有事件） | `task_events` event_type LIKE '%reassign%' |
| STALL BREAK 误 park | 4/4 误伤 | 0 次"park 后 5min 内有成功 run" | `agent_events` + `agent_runs` 时间对齐 |

## 关键修复点的行为信号

### M1 — files_changed 混列剥离
- **要看到**：当某 sub-task 的代码已部分随兄弟分支合到 main、申报清单混入"已在 main"文件时，review **不再被整单硬拒**，而是剥离后只审 diverged 文件
- **不要看到**：任何 `archived_reason` 含"files_changed 包含已在 main"或"Review blocked by files_changed gate"的 cancel
- **副作用**：取消率里"files_changed 家族"应归零

### M2 — reassign 记账 + 证据链绑实现者
- **要看到**：任何 reassign 都在 `task_events` 留事件（from/to/actor/原因）
- **要看到**：reassign 后审查取证按**原实现者** worktree，不再因"当前 assignee worktree 无代码"cancel
- **不要看到**：submitted by 与 assignee 不一致却不留痕迹的情况

### M4 — 超时语义拆分
- **要看到**：长 turn（写大文件、多工具调用）不再被 540s 总超时整轮斩首；idle 才杀
- **看 `/api/debug/metrics`**：应有 `stream_idle_timeout` 与 `stream_total_timeout` 分列计数

### M6 — STALL BREAK 进展赦免 + 文案去处方化
- **要看到**：AGENT STUCK 消息**只陈述事实**（次数、最近成功 run 时间、名下任务），不再写 "Please reassign / dismiss_agent + hire_agent" 这类处方
- **要看到**：completed 收口的 turn 不再累加 STALL BREAK 账本
- **不要看到**：agent 成功 run 后几分钟内被 park

### M7 — watchdog 义务感知
- **要看到**：HR 完成招聘后无待办时不再被反复举红；或举红前先报告"无义务"状态
- **不要看到**：同一合法 idle 的 agent 被精确 30 分钟复读举红

### M14 — browse evaluate + VERIFY 证据规范
- **最关键的实证点**：拖拽是本次的核心交互。看 VERIFY 阶段是否真的**自动化执行了拖拽并断言结果**，而不是又交一堆"工具栏截图 + 空画布"
- **证据截图**应能看到画布上有卡片被移动（非空状态）
- 若 browse evaluate 仍未上，这条会再次暴露——记下来下次补

## 你在运行中可顺手做的几件事

1. **观察 CEO 是否自发规范**：有没有自发 spec-first、自发划 Phase、自发要求 E2E——这是"只给需求"测试的核心
2. **中途访谈一次**（像 TEST21 那样问"平台怎么样"）：对比修复前后 agent 的体感差异
3. **别主动干预**：除非卡死，让它自己跑完。我们测的就是自治能力
4. 跑完把 `.hiveweave/data.db` 路径告诉我，我做对照取证
