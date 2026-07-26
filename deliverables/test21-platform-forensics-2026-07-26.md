# TEST21 运行取证与 HIVE 平台问题系统分析

> 取证来源：`D:\PC_AI\Project\TEST21\.hiveweave\data.db`（只读）、git worktree/分支、后端日志文件、平台源码（`worktree_review.py` / `task_tools.py` / `agent.py`）。
> 运行窗口：2026-07-26 15:11:56 → 17:36:27（约 2 小时 25 分）。
> 分析方法：任务账本 + 对话记录 + agent_runs + agent_events + git 物证 + 源码交叉验证，非凭印象。
>
> **v2 修订（2026-07-26，经独立审计+源码二次核实）**：P1/P2/P6 三处机制归因重写——files_changed 是字节对比的"混列硬拒"而非"文件存在性"判定；"代交无门禁"不成立（B4 硬门已存在），真凶是 reassign 静默漂移导致证据链断裂；流川 stuck 的真凶是 P0-3 STALL BREAK 账本不赦免进展（本次 4 次 park 误伤率 4/4），不是 task-stall 停留超时。次要数值（207 runs、watchdog 6 次、P9 actor=NULL、取消率归因分布、P3 降级为增强现有 dedup）同步订正。

---

## 0. TL;DR

TEST21（CollabBoard 多人协作白板，CRDT + WebSocket，7 个 agent）**最终交付成功**：5 个 Phase 全部 closed，VERIFY 链完整，产物约 3000 行 TS（client+server+tests），E2E 15/15、压测 38/38 通过（VERIFY 证据）。平台的基本盘（账本、worktree 隔离、VERIFY 门禁、流式自愈、MERGE PROXY）经受住了考验。

但过程暴露出 **4 个根因、14 个平台问题**：35% 的任务是被取消而非关闭（7/20），4 次 9 分钟级超时死亡烧掉约 25% 墙钟时间，STALL BREAK 防呆机制 4 次 park 全部误伤（含一次"成功收工瞬间办丧事"），两次"绕过平台手工补锅"（files_changed 混列硬拒、reassign 后证据链断裂只能 cancel）。最危险的一条：**本次运行后端日志整体丢失（文件只有 2 字节）**——如果 DB 没有幸存，这次取证根本无法进行。

---

## 1. 运行概况（取证数据）

| 指标 | 数值 | 说明 |
|---|---|---|
| Agent 编制 | 7 | 归零(CEO/A032)、天线(HR/A033)、云岫(架构师/A034)、Echo(架构验证QA/A035)、流川(后端exec/A036)、星野(前端exec/A037)、墨菲(集成测试exec/A038) |
| 任务总量 | 20 | **closed 13 / cancelled 7（35%）**——归因：reassign 死锁 2 + files_changed 混列 2 + 重复派发 2 + 伞形管理任务收尾 1 |
| LLM 轮次 | **207 runs**（completed 203 + error 4） | error 全部死于"请求总超时"（≈540s 整） |
| 超时烧时 | ≥36 分钟 | 4 次 × 9 分钟，约占全程 25% |
| inbox | 242 条，全部已读 | 消息闭环良好 |
| 僵尸流式消息 | 0 | 自愈机制有效 |
| AGENT STUCK（STALL BREAK park） | **4 次，误伤率 4/4** | 星野 15:42 / Echo 15:53 / 流川 16:18:50 / 墨菲 16:46——park 后均很快恢复正常产出 |
| 系统误报 | SILENCE WATCHDOG 6 次 | 天线×4（16:00/16:30/17:00/17:30，精确 30 分钟复读，无冷却退避）+ 流川×2（17:04/17:34，任务已被 cancel 的合法 idle） |
| worktree 异常 | A034-b、A038-b 重建 + `hw/A037/resubmit2` 残留分支 | |
| 后端日志 | **2 字节（仅 `^C`）** | 2.5 小时运行零日志落盘 |

---

## 2. 先说公道话：平台做对了什么

避免"只看到问题就大修"的误判。以下机制本次有实证收益：

1. **VERIFY 独立验证链**：每个 Phase approve+merge 后自动 spawn 独立 QA 验证（排除实现者与 merger），13 个 closed 任务全部走完验证闭环。这是最终产物质量的直接保障。
2. **Worktree 隔离**：5 个写码 agent 并行 2.5 小时，仅发生一次 package.json 冲突（预期内），无代码互相踩踏。
3. **流式僵尸自愈**：`is_streaming` 残留为 0，无需人工 SQL 清僵尸。
4. **MERGE PROXY**：15:59 云岫长 turn 中无法合并，代理机制沿 parent 链找到兜底合并人，d71d18ce 未卡死。
5. **消息闭环**：242 条 inbox 全已读，`UNREPLIED_ASKS` 硬门只有 2 次触发且均收敛。
6. **Watchdog/stall 的"兜底唤醒"确实救过人**：流川 15:53 第三次超时死亡后，是 16:08 的 TASK STALL nudge 把他唤醒并恢复产出（16:08→16:17 完成 handlers.ts 24KB）。

**结论：方向不用动，要修的是执行层的四个根因。**

---

## 3. 根因与问题清单

### R1 证据链模型假设错误："任务↔实现者↔worktree 的绑定不堪一击"

平台审查/合并门禁的取证锚点是**当前 assignee 的 worktree**，而非**实现者**的 worktree；files_changed 的处置规则在"部分文件已与 main 一致"的混合情形下直接整单硬拒。本次三种现实场景全部打破该模型：

#### P1【P0】files_changed 门禁：混列"已与 main 一致"文件时整单硬拒（v2 重写）

- **机制**（源码核实 `worktree_review.py:227 compare_worktree_to_main`）：门禁对 files_changed 做 **worktree vs main 逐字节对比**（`:278 read_bytes() ==`），分三桶 `divergedFiles`/`identicalToMain`/`missingInWorktree`，处置规则：
  - 全部 identical → **放行**（BUG-9 补丁已修，`:295` `alreadyOnMain` 自动收口）
  - **部分 identical + 部分 diverged → 整单硬拒**（`:299`）← 本次命中的洞
  - 无 diverged → 硬拒（`:308`）
- **现象**：星野 Sub-task 3 申报 6 文件（3 个新组件 diverged + App.tsx/App.css/Canvas.tsx 与 main 一致），触发 `:299` 混合硬拒。云岫原话："review 因 files_changed 包含 App.tsx/App.css/Canvas.tsx（已在 main 上）被阻塞"。
- **证据**：11c57080、97843f6f 两任务因此取消；绕路残留 `hw/A037/resubmit2` 分支 + 手工 VERIFY a9e09c24；16:06→16:14 云岫/星野 6 轮消息手工补锅。
- **根因**：BUG-9 只修了"全部一致"的纯情形，没修"混合"情形。正确语义：**剥离 identical（视为"已在 main"的事实确认），只审 diverged；剥离后为空则走 alreadyOnMain 放行**。
- **同类复发**：15:32 星野 Sub-task 1 第一次 approved 后被系统打回 running（approved→running，15:32:49，actor=NULL，payload 为空），实际是 merge 冲突要求先合 main。
- ~~v1 误诊："按文件是否在 main 上存在判定"——错误，实际是字节对比，且纯一致情形已被 BUG-9 放行。~~

#### P2【P0】reassign 静默漂移 → 实现者/worktree 绑定断裂 → 审批死锁（v2 重写）

- ~~v1 误诊："平台允许非 assignee 提交（无硬门）"——错误。`submit_task` 自 7/22 有 **B4 硬门**（`task_tools.py:1071` "Only the assignee can submit"）。~~
- **真实链条**（DB 核实）：
  1. 流川超时死亡后，云岫将 a83d6f9a/2bf55d12 **reassign 给自己**——DB 铁证：两任务最终 assignee=ec607699（云岫），而 **task_events 全表 assign/reassign 类事件为 0**：reassign 在 running 状态只改字段、不写事件，**账本静默漂移**；
  2. 云岫（现为 assignee）B4 合规提交；
  3. 审查门按**当前 assignee**（云岫 A034）worktree 取证 → A034 无代码副本 → 审批阻塞；
  4. 代码明明已在 main 验证通过，却只能 cancel 平账（16:22:56）。
- **三层洞**：
  - reassign 不写 task_events（谁、何时、从谁手里接的，无迹可查）；
  - 审查取证永远跟**当前 assignee** worktree，不跟**原实现者**——reassign 即证据链断裂；
  - 遗留 `_tool_submit_task`（`task_tools.py:279`）**无 B4 硬门**，是潜伏的绕路通道。
- ~~v1 中"995253d8 由云岫代交墨菲任务"的类比不成立~~：该任务创建时 assignee 即云岫（墨菲只是 running 执行者），与流川案例不同模式。
- **要害不变**：35% 取消率中 2 单来自此家族；reassign 是中层收尸的刚需操作，但其账本与证据链语义目前是残缺的。

#### P3【P1】重复派发：现有 dedup 存在但有覆盖盲区（v2 降级）

- ~~v1 误诊："无幂等机制"~~——不准确。`dispatch_task` 已有：同 assignee+相似标题 → 复用提示（`task_tools.py:542`）；跨 assignee 相似标题 → **仅 warning**（`:558`）。
- **本次两起重复均从盲区穿过**：Phase 4 重复单是**跨 assignee**（995253d8 派云岫 vs a447bc41 派墨菲）→ 只 warning 不拦截；Phase 5 重复单 bb339167 **无 assignee**（CEO 建单未指派）→ dedup 无法匹配。
- **修法**（增强而非新建）：无 assignee 任务纳入判重域；`expected_modules`/`parent_task_id` 等结构化字段作为判重键（不依赖标题文案，守语言无关铁律）；跨 assignee warning 升级为需显式 `force=true` 放行。

---

### R2 超时/停摆恢复链路各自为政：park、stall、watchdog、账本互相不看对方状态

#### P4【P0】流式"总超时"误杀活跃长 turn

- **现象**：4 次 error run（流川 3 次、星野 1 次）全部死于 `ValueError: 请求总超时`，且死亡时间精确落在启动后 ≈540s（544/548/545/543 秒）——即 `TOTAL_TIMEOUT_S=540`。
- **证据**：agent_runs 显示死亡时 **llm 调用 15~24 次、tool 调用 16~27 次**——agent 在持续产出，只是回合总时长超限。流川死亡时正在写 24KB 的 handlers.ts（他最终在 16:08 恢复后完成了它）。
- **要害**：540s 总超时与 600s safety_timeout 构成双重 turn 级死刑，把"停滞"与"活跃但漫长"混为一谈。代码型回合（20+ 次 LLM 调用 + 工具执行）在这个预算下必然大概率被斩。**这是本次运行最大的单一时间杀手（≥36 分钟，25%）**。

#### P5【P0】agent 超时/停泊后，任务账本不联动

- **现象**：流川 15:53 第三次超时后进入死寂，名下 3 个任务（1 个 running、2 个 claimed）原地悬挂 **15 分钟**（15:53→16:08），期间没有任何"任务需要分诊"的结构化事件发给上级；唤醒他的是任务侧 stall nudge 而非 agent 侧恢复流程。
- **要害**：`_park_after_stream_timeouts`（disposition=waiting + 升级上级）只管 agent 状态机，不管他名下的账本。停泊事件应携带任务清单触发"任务分诊"（继续持有/转交/释放 claim），而不是等任务各自 stall 超时。

#### P6【P0】STALL BREAK 账本不赦免进展：4 次 park 全部误伤（v2 重写）

- ~~v1 误诊："stuck 判定只看任务 running 停留时长"~~——错误。流川的 `[AGENT STUCK]` 原文（inbox 取证）是 **P0-3 跨轮 STALL BREAK 账本**（`agent.py:2087-2144`）："hit STALL BREAK 2 times in 30min — tool loop spinning without progress. Agent parked (disposition=blocked). Please reassign the task, provide guidance, or dismiss_agent + hire_agent."
- **本次 4 次 AGENT STUCK 全部由 STALL BREAK 触发，且全部误伤**：

  | 时间 | Agent | 被 park 后的实际表现 |
  |---|---|---|
  | 15:42:01 | 星野 | 15:50 正常提交 75379d23 |
  | 15:53:21 | Echo | 16:06 正常 claim 351abac6 并完成 |
  | **16:18:50** | 流川 | **park 发生在他成功 run（16:17→16:18:50 completed）的收工瞬间**；16:33 正常回答用户访谈 |
  | 16:46:39 | 墨菲 | 17:04 正常执行 ad1c5ab5 并提交 |
- **机制缺陷**（`agent.py:2089-2099`）：STALL BREAK 在同轮工具循环判 spinning 时记一笔，**但 turn 随后恢复并完成时不销账**——"曾经 spin 过但最终有产出"与"彻底空转"同罪，30 分钟凑满 2 次即 park+升级。流川的两笔账正记在两个 completed run 里。
- **次生灾害**：升级文案在误判前提下**直接开处方**（"reassign the task…or dismiss_agent + hire_agent"）——云岫正是照此执行 reassign，触发 P2 死锁链。**误诊与错误处方打包送达，组织照方抓药**。
- 时间线补充：云岫 16:17 向 CEO 申报"流川连续超时 stuck"依据的是 15:53 前的超时旧账；16:18:50 系统 STUCK 消息"确认"了这一叙事，16:20-16:22 重路由落地。平台信号与中层判断互相误证。

#### P7【P1】SILENCE WATCHDOG 不查义务账本，合法 idle 被 30 分钟周期复读举红

- **现象**（inbox 精确取证，共 6 次）：天线（HR）完成招聘后无待办，**16:00 / 16:30 / 17:00 / 17:30 四次**举红，沉默时长报数 31→62→92→122 分钟递增——**无冷却退避的精确 30 分钟复读机**，CEO 每次花一个 turn 处理"误报，忽略"。流川 17:04 / 17:34 再吃两次（其时其任务已被 cancel，合法 idle）。
- **要害**：watchdog 只看"有无产出"，不看"有无义务"（无未回 ask、无名下任务、无 wait contract）。合法沉默被当成异常反复上报。**一个 turn 也是成本**——本次 ≥6 个管理 turn 纯烧在误报上。

---

### R3 闸门只检查不解释：错误信息没有可行动的 ref

#### P8【P1】WAIT_WITHOUT_ASK / TURN EXIT BLOCKED 跨轮不追溯

- **现象**：CEO 归零的头号抱怨——"dispatch_task 或 ask_agent 明明已经发了消息，系统不认，反复要求本轮未向对方发过消息"；以及"上一轮已 ask 且对方未回复，本轮 waiting 同一人被拒"。
- **证据**：归零 17:12 完整反馈（chat_messages 原文）。预检逻辑是**回合本地**的（查本 turn 的送达证据），但义务是**跨轮**的（对方未回复的 ask 依然存在）。
- **要害**：闸门判定与义务账本脱节。若 waiting_on 对象存在未完结 ask（我方已发、对方未回），应视为合法 waiting，无需重复 ask。错误信息也应指明具体 ref（`waiting_on[0]=云岫`），而非笼统"未发消息"。

#### P9【P2】系统反向迁移无原因码

- **现象**：75379d23 出现 approved→running 的系统反向迁移（merge 冲突打回），**actor=NULL**（无操作主体）、payload 空、`blocked_reason` 未写。实现者只能从 git 状态自己猜。
- **要害**：凡系统发起的状态迁移（打回、取消、重挂），必须写机器可读 reason code + 人类可读说明，进 task_events.payload 与 inbox 通知。否则每个 agent 都要消耗 turn 做考古。

---

### R4 可观测性依赖人工与运气

#### P10【P0·运维】后端日志整体丢失

- **现象**：本次 2.5 小时运行对应的后端日志 `tasks/backend-20260726-151111.output` **仅 2 字节（`^C`）**。stdout 重定向块缓冲 + 进程被杀，全部日志蒸发。本次取证只能靠 DB 幸存。
- **要害**：平台的自我诊断故事（CLAUDE.md "查看后端日志"一节）建立在一个随时可能全丢的假设上。**任何"出了事看日志"的机制都必须先保证日志必然落盘**：启动脚本加 `PYTHONUNBUFFERED=1` / uvicorn 文件 handler 直写 + 定期 flush，或 structlog FileHandler 不走 stdout 重定向。

#### P11【P2】worktree 重建痕迹与残留分支污染审查口径

- **现象**：云岫与墨菲的 worktree 目录变成 `A034-b`、`A038-b`（原目录被删后重建、挂回原分支），审查口径"读 `worktrees/<shortId>/`"出现歧义；星野的绕路分支 `hw/A037/resubmit2` 至今残留。
- **要害**：重建应记录事件（何时、为何、原目录去向），resubmit 类临时分支应有 TTL 或合并后清理钩子。

#### P12【P2】VERIFY spawn 不做能力匹配

- **现象**：Phase 4 VERIFY（浏览器 E2E）派给 Echo 后，Echo 以"无 dispatch 权限派给墨菲做浏览器 E2E"为由卡住，17:01→17:12 靠 CEO→云岫人工接力才转给墨菲。
- **分析**：一半是 agent 行为问题（Echo 选择委派而非自己动手），一半是平台问题——`_find_independent_qa` 只排除实现者/merger，不按验证类型匹配能力（BROWSE/E2E 环境）。VERIFY 任务应携带 `required_capabilities`，spawn 时按能力选人。

#### P13【P2】CEO 跨级只读可见性缺失

- **现象**：归零反馈"无法直接 check 非直属下级进度，每件事都绕道 manager"。本次 CEO 大量 turn 用于经云岫中转询问墨菲/Echo 状态。
- **分析**：这是组织设计权衡（防止 CEO 越级 micromanage），但**只读**进度视图不破组织纪律。建议给 CEO 加只读 `check_agent_progress`（不发消息、不唤醒）。

---

## 4. 系统性解决方案

按提示词入账纪律分流：**机制层（代码硬门/状态机）> 检测层（VERIFY/指标）> 提示词层（原则）**。能用机制解决的绝不用提示词。落点文件参照 CLAUDE.md 模块表。

### 4.1 R1 证据链重构（最高优先级）

**M1. files_changed 混列剥离（P1 的治根，v2 修订）**
- 落点：`services/worktree_review.py:compare_worktree_to_main:299`。
- 规则：混合情形（identical + diverged 同时非空）不再整单硬拒——剥离 `identicalToMain`（记入 meta `confirmedOnMain`，视为"已在 main"的事实确认），仅对 `divergedFiles` 执行审查取证；剥离后 diverged 为空则落入 BUG-9 的 `alreadyOnMain` 放行路径（补全 BUG-9 的另一半）。
- 辅助：claimed ⊆ 真实 diff 集（`git diff merge-base...HEAD`）时 auto-strip 未动文件，压低申报噪声；三点 diff 作校验辅助，不作主判定。
- 检测层：VERIFY 模板加回归用例——"申报清单混入已合并文件时，增量文件必须可审通过"。

**M2. reassign 记账 + 证据链绑实现者（P2 的治根，v2 修订）**
- 落点：`services/task.py:reassign_task` + `services/worktree_review.py` + `tools/task_tools.py`。
- 规则：
  - reassign **必写 task_events**（from_assignee / to_assignee / actor / 原因），running 状态也不例外——账本不允许静默漂移；
  - 任务在**首次 running 时锁定** `implementer_id` + `implementer_worktree`，reassign 不改写；审查取证按 **implementer** worktree 而非当前 assignee；
  - 补齐遗留 `_tool_submit_task`（`task_tools.py:279`）的 B4 硬门，堵绕路通道；
  - v1 的 `on_behalf_of` 结构化字段降级为可选增强——先把绑定断裂修掉，再谈代交显式化。

**M3. dedup 盲区补全（P3，v2 降级为增强）**
- 落点：`tools/task_tools.py:542-560`（现有 dedup 段）。
- 规则：无 assignee 任务纳入判重域；`(parent_task_id, expected_modules 哈希)` 等结构化字段作判重键（不依赖标题文案，守语言无关铁律）；跨 assignee 命中时由 warning 升级为需显式 `force=true` 放行。

### 4.2 R2 恢复链路一体化

**M4. 超时语义拆分：idle 超时 + 可续期总预算（P4 的治根）**
- 落点：`llm/streamer.py`。
- 规则：
  - **idle 超时**（无 delta/无工具结果 N 秒，建议 120s）——这才是"停滞"，杀。
  - **turn 总预算**改为软预算：每次工具执行/每个完整 delta 块刷新总预算（活动即续命），硬上限拉到 safety_timeout 内沿（如 570s），并保证 `commit_turn` 有机会收口。
  - 死亡前兜底：总预算耗尽时，若本 turn 已有工具产出，注入系统提示强制 agent 以 `commit_turn(phase=in_progress)` 收尾，保住进展与账本一致性，而不是裸 ValueError。
- 检测层：`/api/debug/metrics` 增加 `stream_idle_timeout` vs `stream_total_timeout` 分列计数（现在只有后者，无法区分误杀）。

**M5. 停泊事件联动任务分诊（P5 的治根）**
- 落点：`agents/agent.py:_park_after_stream_timeouts` + `services/task.py`。
- 规则：agent 停泊/连续错误升级时，事件 payload 必带其名下非终态任务清单；上级 inbox 收到结构化 `[PARKED WITH TASKS]`（含任务 id 与建议动作：继续持有/转交/释放 claim）；平台同时给这些任务打 `owner_parked` 标记，stall 阈值对它们暂停计时（避免双重催办噪声）。

**M6. STALL BREAK 进展赦免 + 升级文案去处方化（P6 的治根，v2 重写）**
- 落点：`agents/agent.py:2087-2148`（P0-3 跨轮账本段）。
- 规则：
  - **进展赦免**：turn 以 completed 收口且本 turn 有实质产出（工具写操作/账本推进/消息送达）时，该 turn 记的 STALL BREAK 不进入跨轮 ledger（或收口时销账）——"spin 过但最终有产出"不与"彻底空转"同罪；
  - **park 前复核**：凑满阈值时先查最近成功 run 时间，近 5 分钟内有成功 run 的降级为观察（记 metric，不 park 不升级）；
  - **升级文案去处方化**：改为陈述事实（"X 分钟内 N 次 stall break，最近成功 run 时间 T，名下任务清单"），把 "reassign / dismiss_agent + hire_agent" 等处置指令从系统消息中拿掉——处置决策属于上级判断，不该由一条可能误报的消息指挥；
  - 指标：`/api/debug/metrics` 增加 `stall_break_parked` / `stall_break_forgiven` 分列计数。
- 提示词层（少量）：coordinator 剧本加一句原则——"收到下级 stuck 信号后先查其最近 run 活性再处置；信号可能是旧账，下级可能已恢复"。（泛化原则，不收实例。）

**M7. watchdog 义务感知（P7 的治根）**
- 落点：`_check_silent_agents`。
- 规则：举红前查义务账本——无未回 ask、无非终态任务、无未履约 wait 的 agent 不举红；同一 agent 的沉默举红加指数冷却（10min→30min→2h），上级"确认误报"可显式标记 `idle_acknowledged`（一次点击，非消息）。
- 这比归零建议的 `idle_reason` 注册更优：不依赖 agent 自觉申报，平台自己算得出来。

### 4.3 R3 闸门可行动化

**M8. WAIT_WITHOUT_ASK 跨轮追溯（P8 的治根）**
- 落点：`turn_exit.py` / reply_contract。
- 规则：waiting_on 对象存在"我方已发且未回"的 open ask 时，直接合法；闸门报错统一格式 `GATE=<名> REF=<agent/任务> MISSING=<具体动作>`，禁止笼统文案。
- 检测层：debug metrics 记录每次 gate 拦截的 GATE+REF，可统计哪类闸门在误伤。

**M9. 系统迁移必带原因码（P9）**
- 落点：所有系统发起的状态迁移（merge 冲突打回、VERIFY 重挂、cancel）。
- 规则：task_events.payload 必含 `{reason_code, detail}`，并同步一条结构化 inbox 给 assignee。

### 4.4 R4 可观测性保底

**M10. 日志必然落盘（P10 的治根，立刻做）**
- 落点：`start-backend.bat/sh` + `main.py`。
- 规则：`PYTHONUNBUFFERED=1` 进启动脚本；或 uvicorn `--log-config` 配置 FileHandler 直写 `tasks/backend-<ts>.log`（不经 stdout 重定向）；structlog 增加每 N 秒 flush。配套：lifespan 启动时写一行 `log_vital_sign`，发现日志文件不可写直接拒绝启动（fail-closed，宁可起不来不要裸奔）。

**M11. worktree 生命周期记账（P11）**
- 落点：`services/git_worktree.py` + `reconcile_worktrees`。
- 规则：目录重建写 `agent_events`（原因、原 HEAD）；临时分支（resubmit/hotfix 类）合并后自动删或 7 天 TTL 清理；审查口径文档统一为"以 agents.workspace_path 为准"。

**M12. VERIFY 能力匹配（P12）**
- 落点：VERIFY spawn（`_find_independent_qa`）。
- 规则：VERIFY 任务生成时按验收类型推导 `required_capabilities`（BROWSE/TEST_RUN/SOURCE_READ），选人时过滤；无匹配人选时直接 blocked 并说明缺什么能力（现在只报"缺 QA"）。

**M13. CEO 只读跨级视图（P13，低优先）**
- 落点：工具表 `CEO_TOOLS` + `services/org.py`。
- 规则：`check_agent_progress(agent_id)` 只读返回 disposition/在办任务/最近产出时间，不发送消息不唤醒；写路径仍走直属链。

### 4.5 不建议做的事（防止过度修复）

1. **不要为此放松审查/合并门禁**。本次 VERIFY 链是产物质量的直接保障，问题在取证语义粗糙，不在门禁严格。修 M1/M2 后门禁应**更准**，不是更松。
2. **不要把 watchdog/stall 一关了之**。P7 的解法是义务感知，不是禁用——16:08 正是 stall nudge 救活了流川。
3. **不要在提示词里为本次每个事故开新条款**（遵守提示词入账纪律）。本次只有 M6 的一条 stuck 判定原则值得进剧本；其余全部走机制层。
4. **不要为"代交"加文案检测**。语言无关铁律约束下，一切意图判定走结构化字段（on_behalf_of、幂等键、required_capabilities）。

---

## 5. 落地优先级建议

| 批次 | 项 | 理由 |
|---|---|---|
| 立刻（本周） | M10 日志保底、M4 超时语义、M1 混列剥离、M6 STALL BREAK 赦免 | 诊断根基 + 本次最大时间杀手 + 两个误伤率最高的账本噪音源 |
| 下一迭代 | M2 reassign 记账与绑定、M5 停泊分诊、M7 watchdog 义务感知 | 恢复链路一体化，消灭"绕过平台手工补锅" |
| 随后 | M8 闸门追溯、M3 dedup 补盲、M9 原因码、M12 能力匹配 | 摩擦打磨，agent 体验直通 |
| 可排期 | M11 worktree 记账、M13 CEO 只读视图、M14 browse evaluate+VERIFY 证据规范 | 卫生、组织体验与测试基建 |

**验证方式建议**：修完第一批后，用同一 CollabBoard 题目（或同级别）再跑一次 TEST22，对照本次基线——取消率 35%→目标 <10%，超时烧时 25%→目标 <5%，watchdog 误报 6 次→0，reassign 无事件→100% 记账，**STALL BREAK park 后 5 分钟内有成功 run 的误 park 次数→0**。指标进 `/api/debug/metrics`，不看感觉。

---

## 6. 附录：用户运行中访谈 Agent 的建议清单及处置（2026-07-26 补充）

用户在运行中向 6 个 agent 提了 6 次"给平台提意见"（Echo×2、归零×2、云岫、星野、流川、墨菲，共 8 篇长文反馈）。归零/云岫/星野/流川的建议与 DB 物证高度吻合，已并入正文 P1-P13 与 M1-M13。Echo 与墨菲的反馈包含**工具能力层缺口**——这是 DB 取证看不到的盲区，单列如下。

### 6.1 墨菲（集成测试）——browse 工具能力缺口，经其自身证据核实

| 建议 | 取证核实 | 处置 |
|---|---|---|
| **browse 无法 canvas 交互**（无 mousedown/mousemove/mouseup 模拟、无 JS evaluate 注入），canvas 类应用 E2E"基本废了一半" | **属实，且后果比自述更严重**：其证据截图 `phase4-rectangle-tool.png`/`phase4-initial-canvas.png` 中画布为空、`元素: 0`——"E2E 15/15 通过"实际只覆盖工具栏/连接/用户列表，**白板核心功能（绘制）从未被真实交互验证过**。VERIFY 通过的含金量需打折 | **新增 P14【P1】**：browse 增加 `evaluate`（JS 注入）能力，QA 可 dispatch MouseEvent 序列模拟绘制；M14 落点 `tools/` browse 实现。同时 VERIFY 证据规范要求声明"核心交互路径是否真实执行"，防止"能截屏即算过" |
| assistant text 与 send_message 分离导致"写 500 字分析没人看到" | 属实（机制如此，意图是防废话） | 提示词层已有约定；可考虑 turn 收尾时提示"本 turn 有 N 段未发送的长文本"，低成本 |
| `lookup_dev_server` 找不到已启动进程（PID 35596），靠 tasklist 手动找 | 与 P11 同类：进程注册不牢靠 | 并入 M11，dev server 注册/发现机制统一记账 |
| `browse inspect/html` 看完整 DOM | 合理，低优先 | 工具增强 backlog |

### 6.2 Echo（架构验证 QA）——7 条建议的独立研判

| 建议 | 研判 | 处置 |
|---|---|---|
| waiting_on 无 TTL/超时升级 | **部分不准确**：平台已有 `agent_waits.expires_at` + 重启 `recover_wait_timeouts`（DB 中有 15 条带 expires_at 的 wait 记录）。但 agent 体感"没有"，说明到期后的升级路径不可感知或失效 | 不建新机制；检测层验证 wait 到期后是否真的有升级动作，把结果暴露给等待方 |
| 前瞻任务（lookahead）防空转 | 痛点真实（Echo 在 Phase 2/3 闲置，云岫也指出其角色模糊），但"平台自动分配前瞻任务"与派工纪律（CEO/中层定分工）有张力 | 不改机制；归到组织规划——HR 招聘时应按 Phase 规划角色利用率，或 QA 在间隙转代码审查 |
| skills 版本管理/changelog | 合理 | 知识层 backlog（skill_registry 加 version 元数据） |
| work log 支持附件（commit hash、env、原始输出） | 合理且与 VERIFY 证据规范互补 | 并入 M14 的 VERIFY 证据规范：`commitHash`/`envSnapshot` 作为结构化字段 |
| browse 截图基线对比 | 真实缺口，优先级一般 | 工具增强 backlog |
| 压力测试工具 | Phase 5 实际用 jest 脚本完成（38/38 claimed），属"外部拼凑但跑通" | 低优先；skill 层沉淀压测方法论即可 |
| 结构化 bug report 工具 | 与现有结构化消息字段重叠 | 不建新工具；用 `message_type=bug` + 结构化 payload（遵守语言无关铁律） |

### 6.3 访谈反馈对问题清单的增量

- **新增 P14【P1】**：browse 缺 JS 注入/canvas 交互能力 → 直接削弱 VERIFY 对 UI 核心交互的验证效力（墨菲自述 + 截图物证双重确认）。方案 M14 见 6.1。
- **VERIFY 证据规范升级**（并入 M14）：证据必须含"核心交互路径真实执行"声明 + `commitHash`/`envSnapshot` 结构化字段；评审人可对"仅有静态截图"的 UI 验证打回。
