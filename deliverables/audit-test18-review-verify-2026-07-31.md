# TEST18（2026-07-30 晚轮）审查报告复核 + 深挖

审查日期：2026-07-31 ｜ 审查人：一元（WorkBuddy）
审查方法：对照 HiveWeave 源码（HEAD `d1256a3` + 未提交 TEST6 批次）+ TEST18 项目 DB（`D:\PC_AI\Project\TEST18\.hiveweave\data.db`）+ git reflog/objects + 后端日志（`tasks/backend-20260730-235634.output`）逐条实证。
对象：另一 AI 对 TEST18 晚轮（23:58 新建 → ~00:32 下班）的审查报告（下称"原报告"）。

---

## 〇、一句话总裁定

原报告 **5 条 P0 全部属实、4 条 P1 基本属实、修复方向 5 条全部正确**；但漏掉了一个 **正在工作区里的实码崩溃 bug（`UnboundLocalError: phase`，10 次崩溃波及 5 个 agent）**，以及若干机制层的根因细节。TEST18 晚轮暴露的问题中，有相当部分来自 **未提交的 TEST6 审计批次代码**（当晚生效但未入 HEAD），这批代码在 commit 前必须先修两个自带 bug。

---

## 一、原报告逐条裁定

### P0-1 Review 义务「出生即计时」→ 假逾期轰炸 —— **属实，机制确认**

**DB 实证**（obligations 表）：

| 任务 | 义务创建 | deadline | 任务提交时间 | 升级记录 |
|---|---|---|---|---|
| 渲染 (0132ed35) | 00:10:51（dispatch） | 00:25:51 | 00:24:12 | esc#1 00:26:18, esc#2 00:31:21 |
| 战斗 (2292eed8) | 00:10:51（dispatch） | 00:25:51 | **从未提交** | esc#1 00:26:18, esc#2 00:31:21 |
| UI (d0c74b9d) | 00:10:51（dispatch） | 00:25:51 | **从未提交** | esc#1 00:26:18, esc#2 00:31:21 |

- 代码确认：`dispatch.py:366-385`（未提交，标注 TEST6 S11）dispatch 时即 `ObligationLedger().create(..., "review", ...)`，deadline = 创建时刻 + `REVIEW_DEADLINE_MS`（`obligation.py:31`，15min）。
- `scan_overdue`（`obligation.py:282-337`）只查 `obligations.status='pending' AND deadline < now`，**不 JOIN tasks 查状态**——确认原报告"不看任务是否已 submitted"。
- 升级消息模板 `_notify_escalation` 硬编码 "review **the submitted** task"，对 running@20 的任务照样说"the submitted task" → 归零被误导，00:27:11 发信"三个MVP模块任务审批全部逾期（十方战斗 - submitted / 棱镜UI - submitted）"，把墨羽拉去审未提交的活。墨羽 00:28:07 正确回复"尚未提交，无法审查"。**纯噪声实锤。**
- **补充新细节①**：渲染任务 00:24:12 已提交（deadline 前 1m39s），潮汐 00:25:52 完成审查（rework 决策）——但 escalation #1 仍在 00:26:18 发出。**rework 不 fulfill review obligation**（只有 approve 才 fulfill），所以"审完了（打回）"照样被升级。
- **补充新细节②**：渲染 00:27:06 重新提交后，submit 路径（`submit.py:135-152`，未提交 TEST6 S11）试图激活 review 义务，但 `create()` 幂等命中 dispatch 时的旧行，**deadline 不重置**，escalation #2（00:31:21）在重提后仅 4 分钟就发出。
- **版本注意**："review obligation from birth" 是 **TEST6 S11 未提交批次新引入的行为**（7-30 白天写入工作区，当晚生效）。这不是老设计，是新回归。其意图（防漏审，配套 `audit_missing_review_obligations` backfill）合理，但 deadline 锚定创建时刻是缺陷。

**修复方向裁定**：原报告"dispatch 只登记、submit 才激活 deadline；escalate 前硬查 status ∈ {submitted, reviewing}"——**正确，且两条都应做**。另需补：(a) submit 时重置 deadline（当前幂等复用旧行）；(b) rework 决策应 fulfill 或重置该义务（否则审完仍被轰炸）。

### P0-2 VERIFY「独立 QA」派给同组实现同学 —— **属实，代码实锤**

- 日志实证（backend output:4846）：`verify_task_spawned parent=bd8029d4(云帆平台模块) assignee=da39608f(Lucas) original=b6b85633(云帆)`，00:20:39。
- 代码实锤（`verify_spawn.py:_find_independent_qa`）：`required_caps=["test_run","source_read"]` 时 `matches_caps` 只查能力，executor/QA 全匹配；随后 `if original_parent: same = [a for a in qa_agents if a.parent_id == original_parent]`——云帆 parent=潮汐(c8d07f4b)，Lucas parent=潮汐，青鸟 parent=墨羽(f3584411) → **same[0]=Lucas，青鸟出局**。
- 青鸟（A013）确为 fam=qa（role="游戏测试工程师" 命中 `is_test_engineer_role` 的"测试工程师"），具备 TEST_RUN+SOURCE_READ，完全合格却被 same-parent 偏好挤掉。
- **"独立"只排除了原实现者+合并人**（exclude={云帆, 归零}），不排除同管理链。Lucas 与云帆同属潮汐门下。

**修复方向裁定**："有 caps 时仍应优先 fam=qa；同 parent 只作 QA 之间的 tie-break"——**正确**，改动点就在 `matches_caps` 之后加一个 fam=qa 优先层。注意当前工作区此逻辑 **未修**。

### P0-3 VERIFY attestation stale 死循环 —— **属实，且比原报告更硬**

**实证链**：

- VERIFY 任务 ff96e262 两次 rework（task_events）：00:30:25 "VERIFY attestation stale：测试在旧commit运行，main_tip是d8fd92b274c9"、00:31:39 "still stale. Lucas必须在main项目根目录…"。
- Lucas 对该任务的全部 test_run attestation（tool_attestations 表）：commit ∈ {2103fa9（00:21:51-00:22:48，main 旧 tip 落后 3）、dad4ac8（00:25:05，自己 worktree）、8803703（00:30:38-00:32:01，自己 worktree）}——**从不等于 main tip d8fd92b**。
- 硬门代码（`attestation.py:check_verify_baseline`，未提交 TEST6 evening P1-3 批次新增，当晚生效）：只允许 attestation commit == target/main_tip（含前缀）或 **是 main_tip 的祖先** 且 behind ≤ 0。
- 我在 TEST18 仓库实测：`git merge-base --is-ancestor 8803703 d8fd92b` → **否**（Lucas 的 worktree commit 是 main tip 的*后代*——他已 `merge main` 进自己分支，测试内容其实是 main 的*超集*），但门不认这个方向。`d8fd92b..8803703` 差 4 个 commit（渲染模块+修复）。
- **原报告没挖到的更深根因（NEW-3）**：`bash.py:_issue_test_run_attestation:1153-1172` 盖 commit 戳时用 `cwd=workspace`（= agent 绑定的 worktree）跑 `git rev-parse HEAD`。**即使 Lucas 按 CEO 指令 `cd D:\PC_AI\Project\TEST18 && npm test`，attestation 盖的仍是他 worktree A009 的 HEAD（8803703），永远≠d8fd92b**。对有 worktree 的 VERIFY 执行人，此门**结构性不可通过**——不是"难"，是"无路"。
- 归零三次 rework 指令（"到项目根跑测试"）因此全部无效，Lucas 00:30:48 work_log 还报告"main@d8fd92b 45测试全过"——他确实跑了（内容含 main），但戳不对。

**修复方向裁定**：三选项都可行，按根治度排序：
1. **VERIFY 工具的 attestation 强制按 main 工作区盖戳**（`workspaceSource=main` 或在 `_issue_test_run_attestation` 对 VERIFY 任务用 `project_main_workspace` 取 HEAD）——最直接，打断死循环；
2. **VERIFY 派给无 worktree 的 QA**（原报告选项 3；未提交批次里 `dispatch.py` 已加 "VERIFY skips write-worktree ensure"，方向一致）——但要注意：本项目中**青鸟恰好无 worktree**（见 NEW-6），若 VERIFY 给青鸟，她在项目根跑、戳即 main tip，两个 bug（选人+stale 门）一次解开；
3. 放宽等价判定（允许"main tip 是 attestation commit 的祖先"即测了超集）——有风险（超集里混入未合并改动会污染 VERIFY 结论），若做需限定 tree 等价而非 commit 祖先。

### P0-4 审查方 attestation 未绑 taskId → waive/approve 全拒 —— **属实，并补充机制盲区**

- 实证：墨羽 00:28:37/00:28:47/00:29:09 三次 test_run（commit=d8fd92b274，**main tip——她在项目根跑的，内容完全合格**），task_id 全为 NULL。她 00:30:31 向归零回报："0132ed35我未成功waive_attestation（test_run attestation未绑定taskId，review_task和waive都被系统拒绝）"。
- 拒绝机制：waive 侧（`waive.py:150-156`）要求 `evidenceAttestationId` 必须绑定本任务；approve 侧 reviewer 硬门要求审查方持本任务新鲜 test_run。绑定失败则两路全拒。
- **原报告没挖到的绑定盲区（NEW-4）**：`bash.py:_resolve_test_attestation_task_id:1070-1126` 的 reviewer 路径是「`creator==self` 且唯一 submitted/reviewing」，assignee 路径是「`assignee==self` 且唯一 running/claimed」。墨羽是归零临时拉来帮审的——她**既不是 creator（潮汐）、不是 assignee（Lucas）、也不是 pinned reviewer（潮汐）**，两条路径全部落空；且落空分支 `return None, ""` **静默无提示**（只有 >1 reviewing 时才给候选清单 S5）。她从头到尾不知道要显式传 `taskId`。

**修复方向裁定**："缺 taskId 时工具回执写清补救步骤"——**正确**，且应把候选提示从 >1 reviewing 扩展到 0 匹配但有 REVIEW 能力的 agent；"可选自动回填 open reviewing task"——谨慎，帮审场景无正式 reviewer 身份，自动回填容易绑错，建议只补提示不做自动绑。

### P0-5 429 后执行者停摆（十方） —— **属实，但根因要改写**

**实证时间线（agent_events + backend log）**：

- 十方：00:15:26 第一次 429（streak=1, cooldown 120s）→ **00:18:03 resume 成功触发**（trigger_firing 日志在）→ 该回合又撞 429（chat 里 00:18:03 的 [ERROR] HTTP 429）→ 00:20:34 streak=2, cooldown 600s → 应 ~00:30:34 resume → **00:31:58 项目下班**，未见复活。
- 棱镜同款：00:15:39（streak=1）→ 00:20:23（streak=2, 600s）→ 下班。
- **原报告没写的关键事实（NEW-5）**：429 是 `AccountRateLimitExceeded`——**账号级限流**。同一窗口内 5 个 agent 全撞：十方 00:15:26、棱镜 00:15:39、潮汐 00:15:53（management tier！）、墨羽 00:20:49、Lucas 也密集调用。per-agent 冷却退避治不了账号级天花板；`RATE_LIMIT_BACKOFF_STEPS_S=(120,600,1800)` 的阶梯只是把死亡时间后移。且 backup 槽位"同 api_key 跳过"（`_resolve_failover_backup`），单 key 下无逃生通道。
- resume 机制本身**生效了一次**（00:18:03），不能说"复活机制失效"；是账号在 120s 后仍被打满（其他 agent 没停），复活回合立刻再 429。

**修复方向裁定**：原报告"确认 flash 模型 429 后 _park / cooldown resume 是否对 executor 生效"——偏弱，只是"去确认"。我的实证答案：机制生效但救不了账号级限流。真正该做的：(a) `AccountRateLimitExceeded` 识别为**全局信号**，一个 agent 撞了，全项目 agent 进入协调降速（而非各自独立退避后错峰继续打）；(b) executor tier 多 key 池或真实 backup（不同 key）；(c) resume 点火前查 circuit breaker 状态，避免往仍熔断的账号上撞。

### P1-A merge 后 delete worktree（墨羽 A008） —— **属实，根因是双重 merge owner**

**完整时序**（task_events + inbox + reflog）：

- 00:12:17 潮汐 approve 墨羽 Phase0.5（1deb2a40）→ 系统给**墨羽**发 `[TASK APPROVED] "You are the merge owner — please run git_worktree_merge"`（`review.py:589-597`，coordinator-family assignee 分支）；
- 00:12:18 系统给**潮汐**发 `[MERGE PENDING] "YOU (coordinator) must merge branchName='A008'"`（`review.py:_inject_merge_pending_wake` + merge obligation owner=潮汐）；
- **同一分支两个 merge owner**。00:12:32 墨羽自己 merge（reflog: `78bad49 main@{00:12:32}: merge hw/A008/work: Fast-forward`），按契约 09（`service_merge.py:332-346`）merge 成功自动拆 worktree（墨羽无其他 open task）；
- 00:12:45 潮汐执行他的 merge → "No worktree branch found for agent A008" + conflict 报错，向归零求救；
- 归零 00:12:32 把任务归档（"先批后档"），merge obligation 记为 cancelled——**但 merge 实际已完成**（78bad49 在 main 上），账本记 cancelled 与事实不符（P1-D 的佐证）。

**裁定**：原报告描述准确。补充：auto-delete 本身是契约 09 设计（没问题），**bug 是 approve 流对 coordinator-assignee 双重指派 merge owner**（executor-assignee 不会——`review.py:598-608` 走 "Wait for your coordinator" 单 owner）。修法：coordinator-assignee 时只保留一个 merge 指令来源。

### P1-B rework 走 `_transition_multi`，waiter 可能不清 —— **本轮弱证据，不定罪**

- 下班时 open waits 共 4 条：墨羽×3（0132ed35/2292eed8/d0c74b9d 的 task_transition，00:30:55 武装，01:00:55 到期）、归零×1（ask_reply 潮汐，00:31:45 武装）。
- 这 4 条都是**合法等待**：墨羽等三个模块任务的下次流转（其中两个还在 running@20），归零等潮汐回信——项目 00:31:58 下班时它们都还没到触发条件。不构成"waiter 未清"的实锤。
- 渲染的 rework（00:25:52）→ Lucas 重提（00:27:06）期间，未见明显卡死的 waiter。
- 旧 TEST18（7/25）报告的同类问题不能自动外推；本轮证据不足，**存疑保留**。

### P1-C 假逾期 + VERIFY 死局 + ask 风暴，CEO 26 次 wait —— **属实（实测 24 条）**

- agent_waits 表实测归零 24 条 wait 记录（原报告称 26，量级一致）：绝大部分是 ask_reply 等潮汐（13min 级 TTL），典型的"问一句等一刻钟"循环。
- 归因链成立：6 条假 escalation（P0-1）+ VERIFY 死循环（P0-3）+ attestation 绑定死局（P0-4）把 CEO 精力拖进平台噪声。归零还因此误判"潮汐 waiting_agent 等待自己是死锁"（00:27:11）——实际潮汐的 3 条 task_transition wait 是合法等 executor 提交，不是死锁。**平台噪声直接转化为 CEO 的错误管理动作**。

### P1-D merge_waived / 文档任务 cancel，账本语义乱 —— **属实**

- 墨羽 Phase0.5（1deb2a40）：00:07:36 归零 waive attestation → 归零不能自批（第三方规则）→ 00:12:17 潮汐 approve → 00:12:32 墨羽 merge（78bad49 上 main）→ 00:12:32 归零 archive（reason "测试策略文档已…"）。终态 `cancelled@0`，merge obligation 记 cancelled——**活干完了、文档在 main、任务却是 cancelled**。批过又 cancel，实锤。
- waive→approve 第三方规则造成两次"击鼓传花"（归零waive→潮汐批；墨羽waive→归零批），流程能走但全靠人肉绕。

### 原报告「哪些不是平台 bug」的裁定 —— **全部同意**

- 渲染 tsc/webgl-mock 错误 = 项目代码质量（Lucas 自己测试文件的问题，rework 合理）✓
- 棱镜 worktree 未 sync main = 执行纪律（他的 A012 停在初始 commit 751a03d，潮汐已催办）✓——但见 NEW-8，他的 test_run attestation 全盖在 751a03d 上被照收，平台层有证据质量缺口。
- 旧报告 `deliverables/test18-bug-report-review-2026-07-25.md` 是 7/25 另一轮 ✓（文件在，彼时 DB 与本轮无关；本轮项目 7-30 23:58 才建）。

---

## 二、原报告漏掉的平台问题（新发现）

### NEW-1（P0，最紧急）`UnboundLocalError: phase` —— 未提交代码里的回合崩溃，10 次崩溃波及 5 个 agent

**后端日志实锤**（`backend-20260730-235634.output`，行号与当前工作区完全一致）：

```
File "agents/completion.py", line 974, in handle_completion
    phase == "in_progress"
UnboundLocalError: cannot access local variable 'phase' where it is not associated with a value
```

10 次 `llm_task_crashed`：归零 ×3（00:08:13 / 00:29:05 / 00:30:47）、青鸟 ×1（00:09:17）、潮汐 ×1（00:11:58）、墨羽 ×2（00:11:59 / 00:29:45）、Lucas ×3（00:23:45 / 00:24:23 / 00:25:31）。**Lucas 三次崩溃全发生在他做 VERIFY 的关键窗口，归零第三次崩溃（00:30:47）正好打断了 attestation 死局的处理回合**（work_log: "turn interrupted; inbox left unread for resume"）。

**机制**：`completion.py:handle_completion` 中 `phase` 只在 `exit_decision.ok==True` 的 else 分支（`completion.py:584`）赋值；而未提交批次新增的 elif（`completion.py:973-978`，标注 TEST6 evening P2-5 `_arm_productive_continue`）位于 turn-exit 闸门的**公共尾部**。当闸门判 park（`turn_exit_parked`）或 repair 次数耗尽（`turn_exit_gate_exhausted`）时，gate_retrigger_hint=None、continue_slice=False、budget_exhausted=False，执行流落到 `elif (phase == ...)` → 崩溃。即：**每次 turn-exit park/gate-exhaust 都崩**。

**影响**：本应"干净停泊"的回合变成 error 事件 → 计入连续错误 → 健康红框广播 → inbox 留 unread 等 resume。闸门语义被污染，还放大了当晚的噪声总量。

**版本注意**：此 elif 在 HEAD（d1256a3）中不存在，是未提交批次（7-30 白天 TEST6 修复）引入的回归，**当前工作区仍未修**。修 commit 前必须先修它——一行即可：在 `gate_retrigger_hint = None`（`completion.py:413`）处同步初始化 `phase: str | None = None`。

### NEW-2（P1）双重 merge owner 竞态 —— P1-A 的根因深化

见 P1-A 裁定。`[TASK APPROVED]`（给 coordinator-assignee："You are the merge owner"）与 `[MERGE PENDING]`（给 reviewer："YOU must merge"）同时指派两人，先 merge 者触发契约 09 自动拆树，后 merge 者报 "No worktree branch found"。executor-assignee 无此问题（approve 通知是"等 coordinator 合"）。**潮汐报错的 "No worktree branch found for agent A008" 不是树异常丢失，是墨羽已合完按约拆树**——错误消息对 agent 完全无法自解释，潮汐只能升级求救，白费两个回合。

### NEW-3（P0-3 根因深化）attestation 的 commit 戳钉死在绑定 workspace，与实际运行目录无关

`bash.py:1153-1172`：`git rev-parse HEAD` 用 `cwd=workspace`（agent 绑定的 worktree），不看命令实际在哪跑。CEO 让 Lucas"到项目根跑测试"在工具层无解。这是 P0-3"无可执行路径"的最深一层。

### NEW-4（P0-4 根因深化）审查人绑定逻辑只认 creator/assignee，帮审者静默出局

`bash.py:1079-1085` reviewer 路径 `creator_id == agent_id`。归零临时拉墨羽帮审时，她三无一不是（非 creator/非 assignee/非 pinned reviewer）→ 静默 unbound、零提示。waive 与 approve 的报错也都不含"带 taskId 重跑"的指引。

### NEW-5（P0-5 根因改写）429 是账号级限流，management tier 同样中招

同一窗口十方/棱镜/潮汐/墨羽全撞 `AccountRateLimitExceeded`。per-agent 退避阶梯无法解决共享账号天花板；backup 同 key 被跳过。需要全局协调降速或多 key。

### NEW-6（P1）青鸟（QA executor）无 worktree 且无 worktree_error —— 契约沉默违反

agents 表：青鸟 `permission_type=executor`、`workspace_path=None`、`worktree_error=None`；磁盘无 `worktrees/A013`、git 无 `hw/A013/*` 分支。`agent_gets_write_worktree`（`ensure.py:28-29`）对 perm=executor 恒 True，hire 时应建树；CLAUDE.md 要求软失败必须写 `worktree_error`。**树没建、错没记**——违反"软失败必须写 error"的契约。讽刺的是：这个"事故"恰好让青鸟成为 VERIFY 的完美人选（项目根执行、戳=main tip）；而 `_find_independent_qa` 偏偏选了有树的 Lucas。后续她的 merge obligation（branchName='A013'）还对着不存在的分支走了一遍 MERGE PENDING→fulfilled（00:25:01）→orphan_approved_migrate 关闭的流程，属于纯知识任务也跑完整 merge 性命周期的流程噪声。

### NEW-7（P0-1 深化）rework 不 fulfill review obligation

渲染 00:24:12 提交、潮汐 00:25:52 完成审查（rework 决策），obligation 仍 pending 并升级（00:26:18）。审查义务只认 approve；rework（=审查已完成、打回修改）不清账。resubmit 也不重置 deadline（幂等复用 dispatch 时的旧行）。这放大了 P0-1：即使 reviewer 按时审了，只要决策是 rework，升级照发。

### NEW-8（P2）两个证据质量/遥测缺口

- **blocked 事件无 actor 无原因**：渲染 00:11:39 `task.blocked`（actor_id=None, payload={}）。真实原因是 Lucas 自己 `update_task_status(blocked, reason="dependency: waiting for 潮汐 to merge scaffolding")`（tool.execute 日志有），但 task_events 丢了 actor 与 reason（`task_transition` 日志 `reason_code=null`）。原报告审这个问题时只能猜。
- **过期基线 attestation 照收**：棱镜 worktree 停在初始 commit 751a03d（无脚手架代码），他 00:19:38-00:20:19 的三次 test_run 全盖在 751a03d 上——**在空壳上跑的"测试通过"被 attestation 系统照收**。attestation 只验"跑没跑"，不验"在什么代码上跑才有意义"（非 VERIFY 任务无 stale 门）。

### NEW-9（P1，流程语义）waive→approve+cancel 双堵 → agent 用"假 rework"当逃生门

墨羽 waive 青鸟准备任务后：不能自批（第三方规则）、cancel 被拒（`unblock_soft.review_deadlock_blocks_cancel`：有 waiver 且存在合法批准人归零）。她的实际逃生路径是 00:23:06 发一条 **"你的工作完全合格、无需修改" 的假 rework**（原文："rework原因是审批流程死锁：我waive了attestation gate后第三方隔离规则禁止我审批，cancel也被拒。请直接resub"），把任务弹回青鸟重提，再请归零批。**平台规则组合把 agent 逼到滥用 rework 语义**。同时 review obligation（owner=墨羽）在 00:24:17 对她升级——她明明做完了能做的全部（waive+尝试批+尝试 cancel+假 rework 解锁），义务账仍记她逾期。小团队豁免（`is_small_team_sole_reviewer`，当晚已在工作区）此处不适用：墨羽不是唯一 REVIEW 持有者（归零/潮汐也在），但她的 waiver 又只锁她自己——规则组合在"多 REVIEW 但 waiver 已发"的场景留下体验很差的窄缝。

---

## 三、代码版本说明（重要背景）

TEST18 晚轮运行代码 = HEAD `d1256a3`（7-30 11:54 commit）**+ 未提交 TEST6 审计批次**（7-30 白天写入工作区，当晚随 23:56 启动的后端生效）。多个当晚暴露的问题直接来自这批未提交改动：

| 未提交改动（TEST6 批次） | 当晚暴露的问题 |
|---|---|
| `dispatch.py` review obligation from birth（S11） | P0-1 假逾期轰炸 |
| `submit.py` submit 激活 review obligation（S11） | 重提不重置 deadline（P0-1 加剧） |
| `attestation.py check_verify_baseline`（P1-3 新增） | P0-3 stale 死循环 |
| `completion.py` `_arm_productive_continue` elif（P2-5） | **NEW-1 崩溃 bug** |
| `bash.py` attestation 绑定 reviewer 路径（S4/S5） | P0-4 帮审者静默 unbound |
| `review.py` 审查方 test_run 硬门 + 第三方规则 | P0-4 / NEW-9 |

**含义**：这批改动本意是修 TEST6/TEST_YLGY 的老问题，但自己在真实项目里引入了 P0-1 与 NEW-1 两个新 P0。commit 前必须先修 NEW-1（一行初始化）并决定 P0-1 的 deadline 锚定策略。`_find_independent_qa` 的 same-parent 偏好与 `scan_overdue` 不查任务状态则是 HEAD 里已提交的老问题。

---

## 四、综合修复优先级（融合原报告建议 + 新发现，全部落机制层）

1. **NEW-1 崩溃修复**（一行：`completion.py` 初始化 `phase = None`）——正在工作区里的活 bug，每次 gate-park 都崩，commit 前必修。顺手补一条 gate-park 路径的单测。
2. **P0-1 义务时钟**：(a) `scan_overdue` JOIN tasks 硬查 `status ∈ {submitted, reviewing}` 才升级；(b) submit 时重置 review deadline（当前幂等复用 dispatch 旧行）；(c) rework 决策 fulfill/重置该义务。升级消息模板带上任务真实状态，不许再说 "the submitted task"。
3. **P0-3 VERIFY×main**：VERIFY 任务的 attestation 按 main 工作区盖戳（`_issue_test_run_attestation` 对 VERIFY 用 `project_main_workspace`），配合未提交批次的 "VERIFY skips write-worktree ensure"。**不要**只放宽祖先判定（超集污染风险）。
4. **P0-2 VERIFY 选人**：`_find_independent_qa` 在 caps 匹配集内先筛 fam=qa，same-parent 仅作 QA 间 tie-break。
5. **P0-4 绑定引导**：attestation unbound 时对 REVIEW 能力者输出"带 taskId 重跑"的明确指引（含候选任务）；reviewer 路径补 `reviewer_id==self`。
6. **NEW-2 双重 merge owner**：coordinator-assignee 的 approve 流只保留一个 merge 指令（建议保留 reviewer 侧 MERGE PENDING，assignee 侧文案改为"等待 reviewer 合并"或按实际 owner 对齐）；"No worktree branch found" 报错文案补充"可能已被另一方合并"的解释。
7. **P0-5 账号级限流**：`AccountRateLimitExceeded` 全局广播降速（而不是 per-agent 各自退避）；executor tier 配置真 backup（不同 key）。
8. **P1 语义/遥测**：task_events 迁移记录 actor+reason；非 VERIFY 任务的 attestation 基线质量提示；NEW-9 的 waive→approve 体验缝（waive 时预警"你将不能自批"）。
9. **NEW-6**：hire 时 worktree 创建失败必须写 `worktree_error`（青鸟无树无错误，违反契约）；纯知识/docs 任务避免走完整 merge 义务生命周期。

---

## 五、对原报告的总评

事实核查：**10/10 条指控全部有 DB/日志/代码实证**（P1-B 证据弱但标了"可能"，属诚实表述）。机制分析：P0-1/P0-2/P0-3 的归因准确；P0-5 的归因（resume 失效）不够准——实为账号级限流。修复建议：5 条方向全部正确，P0-3 的三选项中建议按本报告第 3 条排序落地。

主要遗漏：NEW-1 崩溃 bug（工作区实码回归，当晚 10 次 crash，含 CEO 3 次，比报告里任何一条都更"P0"）；双重 merge owner（P1-A 的根）；attestation commit 戳绑定 workspace（P0-3 的最深根）；帮审者绑定盲区（P0-4 的机制细节）；429 的账号级本质（P0-5 的正确归因）；青鸟无树（NEW-6）。

**方法注**：原报告提到的对账路径（per-project DB `.hiveweave/data.db` 的 agents/tasks/obligations/inbox/task_events/tool_attestations/agent_waits 表 + `tasks/backend-*.output` 后端日志 + 项目 git reflog/objects）已全部复核有效；CLAUDE.md/CLAUDE.local.md 的查询方法同样适用于本轮。
