# TEST6_ylgy 平台问题审计报告

> 审计时间：2026-07-30 13:30（现场仍在运行，死锁未解）
> 测试项目：`D:\PC_AI\Project\TEST6_ylgy`（《羊了个羊》H5，PRD 见项目 prd.txt）
> 审计方法：项目 per-project DB 只读取证 + git 分支对账 + 平台后端日志（tasks/backend-20260730-124338.output）+ 平台源码门禁链走查
> 对照基线：TEST_YLGY 三轮复盘（2026-07-27/28/29）P0 缺陷清单

---

## 〇、现场概况

当前组织：A001 归零(CEO) / A002 天线(HR) / A003 潮汐(前端技术负责人，中层) / A004 云帆(引擎) / A005 Pixel(UI) / A006 山雀(测试)。

当前账本：4 个任务全部卡死 —— 0691fd15（优化总任务，submitted）+ 90ec331b / c44d7d94 / 60bd3786（子任务，running）。

**团队已停摆约 45 分钟，全部 agent 在等用户（小申）救援。** 后端无 error 级日志——平台没崩，是门禁逻辑把所有人锁死了。

---

## 一、P0-1：审批三闸门互咬 → 结构性死锁（本轮最严重问题）

### 现象

CEO 13:23 向 A003 承认：「已尝试审批全部 4 个任务，全部失败（第三方隔离：我豁免了 attestation 无法自审）」。用户指示「直接 approve」后重试，4 个审批再次全部失败。CEO 最终 `defer_task_advance`：「0691fd15 审批被第三方隔离阻塞，无其他 coordinator 可审批，等待用户试玩反馈」。

### 死锁的完整逻辑链（代码级）

任务 0691fd15：creator=CEO，assignee=A003，policy=`coordinator_review`（无 tags → 默认）。

1. `review.py:106`：禁自审，A003（assignee）不能 approve —— 合法。
2. `review.py:281-312`（P0-2 审查方硬闸）：reviewer 本人必须持有**本任务**的新鲜 `test_run` attestation。
3. CEO 权限模型（policy.py）：**无 bash / TEST_RUN capability** → CEO 永远跑不了测试 → 永远拿不到 test_run attestation → 硬闸 100% 拦死。
4. 逃生舱 `waive_attestation`：CEO waive 成功（tool_attestations 有 waiver 记录）。
5. `review.py:211-221`（TEST6 P0-2 新增）：**waived_by == approver 硬拒**——「waive→approve 需第三人」。
6. 组织内除 assignee 外只有 CEO 一个有 REVIEW 权的人 → **无人可审，死锁闭环**。

三条规则各自都"正确"，叠加后出现逻辑真空：CEO 不能自产证据（权限模型）→ 只能 waive → waive 后不能自审（第三方隔离）→ 没有第三方（小团队）→ 死锁。TEST13 已为同类场景开过口子（`review.py:147-163` 的 `ceo_merger_override`），但那个口子只覆盖 VERIFY 任务的 merged_by 场景，没覆盖普通代码任务的 reviewer-attestation 场景。

### 证据

- `agent_waits` 25+ 条死锁等待，reason 原文：「review approve blocked by attestation binding」「platform attestation绑定死锁，归零指示停止尝试approve」「帮忙approve 90ec331b+c44d7d94或告知attestation绑定方法」
- work_logs：CEO「用户指示直接approve……全部4个审批都因第三方隔离失败。这是因为我之前为所有任务豁免了attestation」
- 后端日志 13:23:42→13:26:58 完整过程

### 解决方案（机制层）

**S1：reviewer 硬闸按 reviewer 能力分层（首选）。** `review.py:281` 的 P0-2 硬闸增加前置判定：reviewer 无 TEST_RUN capability 时（CEO），不硬要求自持 test_run，转为**强制要求 consume 本任务链上的他人 test_run**（assignee/QA 的，绑到该任务或祖先任务的）。`consume_agent_ids` 通道已存在（review.py:284-293），把它从"软兜底"升级为 CEO 的"主路径"——CEO 审代码的合法姿势本来就该是复核下级证据，而不是亲自跑测试。

**S2：waived_by≠approver 增加小团队豁免（兜底）。** 当组织内除 assignee 外仅有 1 名 REVIEW 权持有者时，允许 waive 者自审，evidence 落 `override=waive_self_approve_small_team` 审计标记。与 `ceo_merger_override`（review.py:150-163）同构，把既有先例泛化。

**S3：死锁自检（检测层）。** approve 被拒时，平台应能识别"没有任何 agent 能合法 approve 此任务"的状态（reviewer 集合为空），此时错误消息直接说明死锁成因与出路，而不是让 agent 试完所有组合才崩溃上报。

---

## 二、P0-2：attestation 自动绑定在多任务并发审查时失灵（"绑定死锁"真凶）

### 现象

A003（中层）要 approve 子任务 90ec331b / c44d7d94。它跑了 vitest，但 attestation 全部绑到**父任务** 0691fd15（tool_attestations 表：`615119a3 | test_run | 0691fd15`）。`find_reviewer_attestation` 按 `WHERE task_id = ?` 精确匹配子任务 → 查无记录 → 硬拒。A003 反复尝试无果，发消息问 CEO「attestation 绑定方法」；CEO 的指示也是错的（「绑定 0691fd15 生成 test_run attestation ID」）。

### 根因（bash.py:1070-1097 `_resolve_test_attestation_task_id`）

绑定优先级：显式 taskId → **assignee path**（名下唯一 running/claimed 任务）→ **reviewer path**（名下唯一 submitted/reviewing 且 creator=自己）。

两个设计缺陷叠加：

1. **assignee path 抢占 reviewer path**：A003 名下 running 任务恰有 1 个（父任务 0691fd15）→ 自动绑父任务。它作为 reviewer 跑测试的真实意图（给子任务审批攒证据）被自己的 assignee 身份截胡。
2. **reviewer path 单任务假设**：`if len(reviewing) == 1` 才绑定。A003 同时审 3 个子任务 → len=3 → 返回 None。多任务并发审查是常态，单任务假设不成立。

结果：A003 无论跑多少次测试，attestation 永远落在父任务上；子任务的闸永远解不开。agent 无法从工具行为中理解绑定规则——这是**机制可发现性缺陷**，不是 agent 的错。

### 解决方案（机制层）

**S4：绑定优先级反转。** reviewer path（creator 的 submitted/reviewing 任务）优先于 assignee path。审查意图比自身执行意图更可能是跑测试的原因——尤其当工作区是被审分支时。

**S5：多任务时拒绝静默乱绑。** reviewing >1 且无显式 taskId 时：attestation 落 `task_id=None` 并在工具回执中列出候选任务（「本次 test_run 未绑定任务；如需用于审批，请带 taskId 重跑或从以下任务选择」），而不是绑到概率最高的那个。静默绑错比不绑更糟——它制造出"证据存在但永远匹配不上"的假象。

**S6：审批拒绝消息带出可用证据清单（检测层）。** `find_reviewer_attestation` 失败时，查询 reviewer 名下所有新鲜 attestation（不限 task_id），在错误消息中列出「你持有 attestation X（绑在任务 Y），与目标任务不匹配」，让 agent 一次看懂差距，而不是靠猜。

---

## 三、P1-3：cancel_task 逃生舱被焊死，agent 被逼出荒诞行为

### 现象

13:24:04 A003 连续 cancel_task ×3（理由：「代码已 merge 到 main 并验证通过……取消以清除 gate deadlock」）→ 全部被拒（"execution evidence 仍在"）。随后 A003 只能 **rework 自己确认"代码完全正确"的任务**——「rework 仅为清除审批死锁」——并反复向下级广播「不要重新提交」。任务状态机空转（submitted→reviewing→running），账本被污染，问题没有任何实质推进。

### 根因

`unblock_soft.py:21-71 review_deadlock_blocks_cancel`：review pipe（submitted/reviewing）+ 有 waiver/evidence → 拒 cancel。设计意图是防"cancel 清场绕过审查"，方向正确；但它假设审批路径本身健康——当审批死锁（问题一/二）存在时，这个闸门把最后一个逃生舱也焊死了。**防护机制之间没有死锁检测，互相当对方不存在。**

### 解决方案（机制层）

**S7：cancel 拦截增加死锁豁免判定。** `review_deadlock_blocks_cancel` 拦截前先做 S3 的"是否存在合法 approve 路径"检测：若无合法审批人（如本例），放行 cancel 并要求 reason ≥20 字（已有），cancel 落账 `cancelled_in_deadlock` 供审计。防护机制的组合必须死锁自由（deadlock-free），这是与单条规则正确性同等重要的验收标准。

---

## 四、P1-4：stranded 分支大面积复发 + CEO 交付幻觉

### 现象（git 对账结果）

| 分支 | 未合 main 提交数 | 内容 | 损失评估 |
|------|-----------------|------|----------|
| `hw/A013/work` | 2 | pointer-events 修复 + 集成修复 | 内容后被 1086526 重做，侥幸无损 |
| `hw/A016/work` | 4 | TDD 测试文件（coverage/level2/props）+ E2E 截图 | **测试资产 stranded** |
| `hw/A003/work` | 8 | 集成 merge + UI-1 修复（tip 314c2bd） | 集成提交 stranded |
| `hw/A006/work` | 1 | E2E 验证产物（27 截图 + 9 attestations） | VERIFY 产物 stranded（TEST_YLGY P0-1 变体复发） |

更严重的是**幻觉**：CEO 在 work_log 中声称「代码已交付 main commit 314c2bd」——314c2bd 实际 stranded 在 `hw/A003/work`，从未进 main。merge 收口状态对 agent（包括 CEO）不可见，agent 凭印象汇报。

### 根因

1. A013/A016 的 agent 已从 agents 表消失（org_dismiss_log / personnel_records 均空，DB 只覆盖最近 43 分钟——疑似测试流程中重置过 DB），但**分支残留无人认领**：dismiss/重置路径没有"stranded tip 强制对账"闸。TEST_YLGY 复盘要求 reconcile 扫 stranded tip（"merge→obligation 清账→VERIFY 产物落 main 当一条链验"），本轮证明该链仍不闭环。
2. agent 查询「我的提交是否已进 main」没有趁手工具，CEO 只能靠猜——幻觉是信息缺失的必然产物。

### 解决方案（机制层）

**S8：孤儿分支对账（reconcile 补强）。** `reconcile_worktrees` 增加扫描：git 分支 `hw/<sid>/*` 存在但 agents 表无对应 short_id（dismiss/DB 重置后）→ 标记孤儿 + tip is-ancestor 检查 + 报告上级。非 ancestor 的孤儿分支是最高优先回收对象。

**S9：merge 事实自查工具/门禁。** `git_worktree_status`（或 submit/报告路径）输出中机械附带「分支 tip 是否 ancestor of main」的布尔事实，让任何 agent 汇报交付状态前先拿到事实，从机制上消除"凭印象声称已交付"。

**S10：dismiss/项目重置硬闸。** dismiss_agent 闭合生命周期清单中已有"清 worktree"，需补"分支 tip 未合并则强制 quarantine + 通知上级"——禁止无声丢弃。

---

## 五、P2-5：obligations 账本空转

### 现象

`obligations` 表 **0 条记录**；同期 `agent_waits` 25+ 条、tasks 4 条（含 claimed/running/submitted 全生命周期）。CREATOR_MUST_MERGE、merge 义务、审查义务全部没有落账。

### 根因

`ObligationLedger.create` 落账点稀少：review.py:663（approve 后）、close.py:481、misc_tools.py:828、reconcile.py:658。本轮任务全部死在 approve 之前 → 落账点全部未触发。dispatch/claim/submit/reviewing 这些前置状态迁移没有 obligation 落账，账本对"审批中"的责任链完全失明。

### 解决方案（机制层）

**S11：义务落账前移。** dispatch/指派时落 `review` 义务（owner=creator/上级），submit 时激活，approve/close 时 fulfill——责任链从任务出生就入账，而不是审批通过才补记。对账任务定期校验：status≠closed 的任务应有对应 open obligation，缺失告警。

---

## 六、方案总表（全部落机制/检测层，零提示词补丁）

| # | 方案 | 治的问题 | 落点 | 优先级 |
|---|------|---------|------|--------|
| S1 | reviewer 硬闸按能力分层，CEO 走 consume 通道 | P0-1 死锁 | review.py + attestation.py | **P0 第一刀** |
| S2 | waived_by≠approver 小团队豁免（留 override 审计） | P0-1 死锁 | review.py:211 | P0 同 PR |
| S3 | approve 拒绝时死锁成因自检 | P0-1 可诊断性 | review.py 错误路径 | P0 同 PR |
| S4 | attestation 绑定优先级：reviewer path 先于 assignee path | P0-2 绑定死锁 | bash.py:1070 | **P0 同 PR** |
| S5 | 多任务并发时拒绝静默绑定 + 回执列候选 | P0-2 可发现性 | bash.py | P0 同 PR |
| S6 | approve 拒绝消息附带 reviewer 现有证据清单 | P0-2 可诊断性 | review.py:295 | P1 |
| S7 | cancel 拦截前做死锁豁免判定 | P1-3 逃生舱 | unblock_soft.py:21 | P1 |
| S8 | reconcile 扫描孤儿分支（agent 已消失的分支） | P1-4 stranded | reconcile.py | P1 |
| S9 | merge 事实（is-ancestor）进 status/汇报通道 | P1-4 幻觉 | git_worktree status | P1 |
| S10 | dismiss/重置时未合并 tip 强制 quarantine | P1-4  stranded | org dismiss 生命周期 | P1 |
| S11 | 义务落账前移到 dispatch/submit | P2-5 账本空转 | tasks 状态机 | P2 |

**验收标准新增一条元规则：任何新增硬闸（hard gate）必须证明与既有闸门组合后死锁自由——给出"所有合法路径被堵死时的逃生舱"论证。本轮 P0-1 死锁正是三条各自正确的闸门叠加后无人做组合验证的产物。**

## 七、建议验证计划

1. **死锁复现用例（单测）**：构造 creator=CEO / assignee=中层 / 无第三方 reviewer 的任务，CEO waive 后 approve → 断言 S2 豁免生效且 evidence 落 override 标记；S1 生效后断言 CEO 可用 assignee 的同任务 test_run 通过硬闸。
2. **绑定优先级用例**：agent 同时持有 1 个 running（assignee=自己）+ 3 个 reviewing（creator=自己）任务，跑测试 → 断言 S4/S5 行为（绑 reviewer path 或拒绝静默绑定并列候选）。
3. **逃生舱用例**：review pipe + evidence 存在 + 无合法审批人 → cancel 放行且落 `cancelled_in_deadlock`。
4. **端到端**：TEST6 场景重跑（小团队 + CEO 终审），观察全链路无人工救援闭环。

> 测试请在用户终端跑 pytest，勿在 WorkBuddy 沙箱跑（环境安全约定）。
