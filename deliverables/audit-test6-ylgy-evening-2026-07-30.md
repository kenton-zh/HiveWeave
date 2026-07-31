# TEST6_ylgy 平台问题报告（晚场实证轮）

> 分析时间：2026-07-30 21:34–22:10
> 分析对象：`D:\PC_AI\Project\TEST6_ylgy`（《羊了个羊》H5，PRD 见项目 prd.txt）
> 运行窗口：2026-07-30 19:31 → 21:13（第四轮，组织：A001 归零 CEO / A002 天线 HR / A003 Arlo 技术负责人 / A004 潮汐 QA）
> 方法：per-project DB 只读取证 + git 拓扑对账 + 后端日志（backend-20260730-192408.output）+ 平台源码逐行核对
> 前置报告：`test6-ylgy-postmortem-2026-07-30.md`（早场 P1-P3 + F1-F5）、`audit-test6-ylgy-report-2026-07-30.md`（午场死锁 S1-S11）

---

## 〇、结论先行

**晚场 5/5 任务全部 closed，全链路无人工救援跑通——午场的 S1/S2/S11 修复与早场的 F1-F5 修复在生产实证中生效。** 但跑通的过程又暴露了 4 个新 P1 和若干 P2/P3。本轮最有价值的三个新发现：

1. **义务账本 fulfill 被 task_id 前缀静默击穿**（S11 自己的实现缺陷）——approve 传 8 位前缀时 fulfill 精确匹配落空，0 行更新、无任何报错，账本漏记。
2. **worktree 删除-重建空转**——GC/merge-teardown 刚删掉 worktree，懒创建与 heal tick 立刻重建，4 分钟内 5 建 4 删，GC 目标被自己的自愈对冲。
3. **VERIFY 在过期基线上取证，"main 验证"名不副实**——A003 的 VERIFY attestation commit 钉在 a4f3939（19:56 的旧 main），而真 main 已到 4937306；平台没有任何机制校验"验证目标 commit == 当前 main tip"。

另确认：**git merge 的 rebase-先行的拓扑是正确的**——A003 分支提交 d36ce65 经 rebase 重写为 159eee9 落 main，无内容丢失（详附录 B，避免后人误报 stranded）。

---

## 一、已验证生效的修复（好消息，先记账）

| 修复 | 实证 |
|------|------|
| S1 reviewer 硬闸按能力分层（CEO 走 consume 通道） | 20b3c1af 由 CEO approve，**无 waiver 记录**，消耗的是 A004 的同任务 test_run attestation（8747efef，exit 0）——consume 主路径打通 |
| S2 waived_by≠approver 小团队豁免 | ef5f972d / fc8b3aec approve 均落 `override=waive_self_approve_small_team` 审计标记，waiver 文案完整（94df22e0 / d5dcaf08） |
| S11 义务落账前移 | obligations 表 7 条：dispatch 落 review 义务、approve 落 merge 义务，类型/owner/时间链完整（对比午场 0 条） |
| F4 close-GC | 21:13:12 两条 `worktree_gc_on_close` 事件，A004 分支已合并正常删除、A003 分支未合并正确 preserve |
| P0-3 进程注册 | 21:10:20 `delete_processes_stopped`：A004 的 dev server（port 3202, pid 1488）先杀后删 worktree，WinError32 链路在第一现场被掐断 |
| merge rebase 拓扑 | A004 的 73cbabc 与 A003 的 d36ce65→159eee9 均完整落 main，FF 后 `git branch -d` 合法通过，无强删、无内容丢失 |
| husk 自愈 | A003-b/A004-b 空壳经 reconcile 每 6 分钟重试，21:44 全部 `reconcile_dir_removed`，最终清零 |

---

## 二、新发现问题

### P1-1：义务账本 fulfill 被 task_id 前缀静默击穿（S11 实现缺陷）

**现象**：任务 fc8b3aec（美术优化）已 closed，但它的 review 义务 `bd98faad` 至今 **pending**——7 条义务中唯一漏账。

**证据链（smoking gun）**：
CEO 的 9 次 `review_task` 调用参数（chat_messages.tool_calls 原始记录）：

| 时间 | task_id 形式 | approve 结果 | 义务 fulfill |
|------|-------------|-------------|-------------|
| 19:41:50 | ef5f972d 全 UUID | ✅ | ✅ fulfilled |
| **19:56:06** | **fc8b3aec 8 位前缀** | ✅ | ❌ **静默漏账** |
| 19:59:55 | de317036 8 位前缀 | 被拒 | — |
| 20:01:06 | de317036 全 UUID | ✅ | ✅ |
| 21:10:00 | 20b3c1af 全 UUID | ✅ | ✅ |
| 21:13:05 | 7434cfd4 全 UUID | ✅ | ✅ |

**根因（代码级）**：
- `tools/tasks/review.py:517`：`ObligationLedger().fulfill(project_id, params.task_id, "review")` —— 直接传 agent 输入的原始值。
- `services/obligation.py:176`：`WHERE task_id = ?` 精确匹配，obligations 表存全 UUID → 前缀匹配 0 行 → `return 0`，**不抛错、无日志**（try/except 只在异常时 warning，0 行命中是"合法"返回）。
- 同文件 `:258/:363` 的 merge 义务创建走 `tid = task.get("id") or params.task_id`（已解析）——**同文件两种卫生标准**，fulfill 这条漏了。
- 同类隐患：`tools/misc_tools.py:707-709` merge fulfill 同样传原始 `params.task_id`（本轮 merge 义务碰巧由全 UUID 调用 fulfill 成功）。

**解决方案**：
1. **根治（首选）**：`ObligationLedger.fulfill/create` 边界内统一解析——入参 task_id 先经 tasks 表 `WHERE id = ? OR id LIKE ? || '%'` 归一到全 UUID（与 `close.py:37 require_task_id` 同语义），所有调用方不再各自记得解析。
2. **兜底对账**：close_task 末尾加一条义务对账（fail-open）：`status=closed` 的任务不应存在 pending obligation，发现即 fulfill 并 warning——午场 S11 后半句"对账任务定期校验"本轮证明仍未落地。
3. **可观测**：`fulfill` 返回 0 时记 debug 日志（带原始入参），下次漏账一眼可辨。

---

### P1-2：worktree 删除-重建空转（GC vs 懒创建/heal 目标互斥）

**现象**：21:10:21 → 21:14:04 四分钟内，A003/A004 的 worktree 被 **删 4 次、建 5 次**，最终全部任务 closed 后仍有 2 个 worktree 登记在册（A003/A004 @ 471900f）。

**证据链**（后端日志逐条）：
```
21:10:21.318  delete A004（merge teardown，分支已合并）
21:10:31.764  create A004 base=main        ← A004 被 [TASK APPROVED] 唤醒，懒创建
21:13:12.631  delete A003（close-GC，分支 d36ce65 未合并 → preserve）
21:13:12.891  delete A004（close-GC）
21:13:23.658  create A003 base=existing-branch  ← A003 被 [TASK APPROVED] 唤醒，懒创建
21:13:32.886  delete A003（merge teardown）
21:13:46.537  create A003 base=main        ← A003 被 [TASK CLOSED] 唤醒，懒创建
21:14:04.394  create A004 base=main        ← heal tick "recovered: 1"
```

**根因**：三条子系统各管一段、互不通信：
- merge teardown / close-GC 认为"任务没了，树该收"（F4 新逻辑，正确）；
- 懒创建（agent chat 入口："有写树资格 + 无有效 worktree → 重建并写回 DB"）只看资格**不看待办**；
- heal tick（`worktree_heal_tick`，每 ~6 分钟 recover 缺失 worktree）同样只按 `agent_gets_write_worktree` 判定。
- 而 [TASK APPROVED]/[TASK CLOSED] 这类系统通知必然唤醒 agent → 每次唤醒都触发重建。GC 省下的，懒创建/heal 加倍还回去。

**解决方案**：
1. 懒创建与 heal 增加统一前置条件：**名下有在途写任务**（复用 `reconcile._assignee_has_open_tasks` / `_IN_FLIGHT_AFTER_MERGE_STATUSES` 口径）才重建；无在途任务 → 不建，并在 debug 日志记 `worktree_recreate_skipped_no_open_tasks`。
2. 系统通知类唤醒（task_event）本身不该触发写树重建——唤醒入口处对 `message_type=task_event` 的 wake 跳过懒创建（agent 真要写代码时会经任务路径拿到树）。
3. 验收：项目全任务 closed 后 30 分钟，`git worktree list` 应只剩 main。

---

### P1-3：VERIFY 在过期基线上取证，"main 验证"名不副实

**现象**：VERIFY 任务 7434cfd4 的全部 attestation 的 `commit_hash` 钉在 **a4f3939**——那是 19:56 的旧 main 快照；而取证时刻真 main 已前进到 **4937306**（21:10:20 刚合入 A004 的 25 张证据 PNG + 737e4e9 的 vitest 修复）。A003 汇报"main分支验证完成"，实际是在自己分支（基于 a4f3939）上验的。

**证据**：tool_attestations 表 21:10:54–21:12:44 连续 4 条（2b5334e7 test_run / 33c2145c+5eb00e98 browse_e2e / 65cedc02 visual_check）`commit=a4f3939bf4`；git 拓扑确认 4937306 在 21:10:20 已上 main。

**为什么这是平台问题而不是 agent 撒谎**：
1. VERIFY 任务 spawn 时（`nudge_verify_tasks_after_merge`）**没有把"本次 merge 的目标 commit"写进任务契约**——QA/中层拿到任务时无从机械得知"该在哪个 commit 上验"。
2. attestation 记录了 `commit_hash`，但**没有任何闸门把它和当前 main tip 比对**——过期的验证也能过审。
3. agent 想验证 main 只能靠自觉 checkout——懒惰或疏忽就退化成"在自己分支上验"，且证据上完全看不出来（本轮靠人工对账才发现）。这与午场 S9（merge 事实不可见→CEO 幻觉）是同一族病：**平台事实对 agent 不可见，agent 凭印象申报**。

**解决方案**：
1. VERIFY 任务契约结构化写入 `target_merge_commit`（spawn 时的 main tip）；任务描述机械附带该值。
2. QA 侧 attestation 落库时校验：工作区 HEAD ∈ {target_merge_commit, 当前 main tip}，不符则工具回执警告并要求先同步——检测层，不阻断但留痕。
3. 审查方闸门（复用 P0-2 通道）：approve VERIFY 时比对 attestation.commit_hash 与 main tip 差距，过期 >N 个提交时提示「证据基线陈旧」。
4. 注意与 P1-2 联动：VERIFY 期间 worktree 被 GC/重建会导致基线漂移，应让 VERIFY assignee 的 worktree 在任务期内豁免 GC（`_assignee_has_open_tasks` 口径已天然覆盖—— VERIFY 任务在途即豁免，落实 P1-2 修复后此点自动成立）。

---

### P1-4：孤儿分支有检测、无处置通道（午场 S8 只落地一半）

**现象**：reconcile 每 6 分钟准点报 3 条 `reconcile_branch_preserved`（hw/A006、hw/A013、hw/A016，`orphan_agent_gone`，`priority=high`，`tip_is_ancestor_of_main=false`）——从 19:24 后端启动报到 21:50 仍未停，**将永远报下去**。

**现状盘点**：
- 检测 ✅ 已落地（`orphan_agent_branches` 进 reconcile result，含 tip/ancestry/priority，这正是午场 S8 要求的）。
- 处置 ❌ 无通道：无 inbox 通知 CEO/用户、无 quarantine、无 UI 出口、无 TTL。warning 噪音淹没真告警。
- 分支内容：A016 含 4 个提交（TDD 测试文件 coverage/level2/props + E2E 截图）与 A006 含 1 个提交（27 张 E2E 截图 + 9 attestations）是**真实测试资产**；A013 的 2 个提交内容已被重做，可弃。

**解决方案**：
1. **首报即通知**：orphan 分支首次检出时，向 CEO（或用户，若 CEO 也不在）发一条 inbox（带 tip、文件清单、是否 ancestor），之后静默——通知一次，不每 tick 重复。
2. **处置工具**：给 reconcile 加 `quarantine_orphan_branches` 动作（`git update-ref refs/quarantine/hw/<sid>/<ts> <tip>` + 删原 ref）——不丢对象、可恢复，ref 命名空间隔离后 `git branch` 列表恢复干净。
3. **TTL**：`orphan_agent_gone` 且 ancestor-of-main 的分支（如将来 A013 这类已被重做的）N 天未认领 → 自动 quarantine；非 ancestor 的永仅报告（删除安全链原则不变）。
4. **UI 出口**：Settings/Debug 页列孤儿分支 + 一键 quarantine（可选，最低优先级）。

---

### P2-5：长 E2E 任务的调度空窗 → 假 blocked 警报（~20 分钟协调浪费）

**现象**：A004 的游戏验证任务（20b3c1af，browse 连击型长任务）20:24:14 交卷后 **idle 17 分钟**（20:24→20:41），直到 TASK STALL 催办才醒；20:52 CEO/A003 的 30 分钟 wait 超时，误判"blocked"，CEO 派 Arlo 专项诊断（agent_waits 原文：「等待Arlo诊断潮汐blocked原因并解除」）。实际 A004 一直在正常推进（60%→80%→提交），全程无真实阻塞。

**根因（机制链）**：
1. 合法 Idle 政策下 `phase=in_progress` 最多再续 1 个 slice；slice 用完后 agent 停机，**重唤醒只能靠外部事件**。
2. 而 running 任务的 stall 阈值是 20 分钟——一个正在干活的 agent，slice 结束到被唤醒之间最长饿 20 分钟。
3. CEO/A003 的 `agent_waits`（30 分钟 timeout）比 stall 阈值后触发，于是"明明在推进"被读作"blocked"，触发无效诊断协调。

**解决方案**：
1. **slice 结束即重排**（首选）：`commit_turn(in_progress)` 且本轮有实质工具活动（tool_calls>0）时，turn exit 直接武装一个短延迟 wake（如 30s），不等 stall 阈值——把"续跑预算"从"最多 1 个 slice"改为"slice 后自动再排队"，预算仍由 no-progress 熔断兜底。
2. **心跳也算产出**：沉默看门狗/TASK STALL 的"产出"判定把 assistant 的 tool_calls 行纳入（当前只看 assistant 文本行 + work_logs，`game_time.py:1994-2007`）——长 browse 会话期间不应记为沉默。
3. **wait 与 stall 阈值对齐**：`agent_waits` 对 running 任务的 timeout 建议 ≥ stall 阈值 + 余量，避免"催办未到、警报先响"。

---

### P2-6：main 检出上的注册进程无回收路径

**现象**：port 3000 的 dev server（pid 33640）至今 LISTENING。日志确认它是 **CEO 20:04:42 经 `start_dev_server`（preferredPort 3000）在 main 检出上启动**的注册进程。

**根因**：进程回收只挂在 worktree 生命周期上（`stop_processes_for_worktree`，P0-3）；main 检出不属于任何 worktree → 项目收尾无人杀它。注册表在内存（两库均无 process 表），后端一重启连账都没了。

**解决方案**：
1. 项目 deactivate / 全任务 closed 时，清扫该项目 workspace 下全部注册进程（不限 worktree 绑定）。
2. 进程注册表落盘（per-project DB 一张 `processes` 表），后端重启后对账：PID 已死清账、PID 活着且 cmdline 含 workspace 路径的重新认领。

---

### P3-7：pre-merge-checkpoint 空提交成为 main tip

每次 merge 在 worktree 内 `commit -m "pre-merge-checkpoint" --allow-empty`（`service_merge.py:448`），rebase+FF 后该空提交落在 main 上；当前 main tip 471900f 就是一个空 checkpoint。历史里 8 个 `pre-merge-checkpoint` 污染 log。**方案**：checkpoint 后若 tree==parent（真空），rebase 时 drop 该提交（`GIT_SEQUENCE_EDITOR` 或 rebase 后 `reset --soft HEAD~1` 判空再定）；或改为 stash 式快照不进历史。

### P3-8：browse 产物 `.gstack/*` 每次 checkpoint 都警告

21:09:21 / 21:12:54 两条 `checkpoint_ignored_files`（.gstack/browse-audit.jsonl 等 4 个）。browse 工具在工作区写审计日志是常态，**方案**：`.gstack/` 加入 `GITIGNORE_GENERATED_ENTRIES`（constants.py），与 test_output 同待遇。

### P3-9：`no_text_hint_exhausted`

21:10:21 A004 round=22 hint_count=6——单轮 22 轮工具循环无文本输出。未造成后果，记录待观察；若复发应查 tool-loop 的文本提示注入逻辑。

---

## 三、历史遗留清账（本轮盘点，需人工处置）

| 项 | 状态 | 建议 |
|----|------|------|
| `hw/A006/work`（b8757d9：27 截图 + 9 attestations） | 内容 stranded，无 agent 认领 | 人工决定：cherry-pick 取证价值或 quarantine |
| `hw/A016/work`（4 提交：TDD 测试文件 + 截图） | **测试资产 stranded** | 建议 cherry-pick 测试文件回 main 后 quarantine |
| `hw/A013/work`（2 提交） | 内容已被重做（1086526），可弃 | quarantine |
| `stash@{0}: WIP on main: 692523f pre-merge-checkpoint` | 早场 merge 冲突残留，main 已前进 20+ 提交 | `git stash drop`（确认无独存内容后） |
| main 工作区 `.trae/` untracked | 无害 | 忽略或入 .gitignore |
| d36ce65（dangling） | **内容无丢失**（rebase 孪生 159eee9 在 main） | 无需处置，gc 自然回收 |

---

## 四、修复清单总表（全部落机制/检测层，零提示词补丁）

| # | 问题 | 落点 | 方案要点 | 优先级 |
|---|------|------|---------|--------|
| E1 | P1-1 前缀漏账 | `obligation.py` fulfill/create 边界 + `close.py` 对账 | task_id 统一归一（`id = ? OR id LIKE ?||'%'`）；closed 任务义务兜底 fulfill；0 命中记日志 | **P0 同 PR** |
| E2 | P1-2 删建空转 | 懒创建（agent chat 入口）+ heal tick | 统一加"有在途写任务"前置；task_event 唤醒跳过懒创建 | **P0 同 PR** |
| E3 | P1-3 VERIFY 基线 | VERIFY spawn + attestation 落库 + approve 闸门 | 契约写 target_merge_commit；attest 校验 HEAD；approve 比对基线 | P1 |
| E4 | P1-4 孤儿处置 | `reconcile.py` + 新 quarantine 动作 | 首报 inbox 通知一次；quarantine ref 隔离；ancestor 分支 TTL 自动收 | P1 |
| E5 | P2-5 调度空窗 | turn exit / `game_time.py` | in_progress+有工具活动 → 短延迟自动重排；tool_calls 计入产出；wait 阈值对齐 | P1 |
| E6 | P2-6 main 进程回收 | process_registry + deactivate/close-all 钩子 | 全任务 closed 清扫 main 注册进程；注册表落盘 | P2 |
| E7 | P3-7 空提交 | `service_merge.py:447-449` | 空 checkpoint 不进历史 | P3 |
| E8 | P3-8 .gstack | `constants.py` GITIGNORE_GENERATED_ENTRIES | 加 `.gstack/` | P3 |
| E9 | P3-9 no_text | tool_loop 观察项 | 记录复发条件 | P3 |

**元规则重申（午场已立，本轮再次验证其必要性）：任何新硬闸/新生命周期钩子上线前，必须验证与既有机制组合后的死锁自由与目标一致——S11 的 fulfill 被前缀击穿、F4 的 GC 被懒创建对冲，都是"单条正确、组合打架"。**

---

## 五、验证计划

1. **E1 单测**：approve 传 8 位前缀 → obligations 对应行 fulfilled；closed 任务存在 pending 义务 → close 兜底 fulfill + warning。
2. **E2 单测**：任务全 closed 后模拟 agent 被 task_event 唤醒 → 无 worktree 重建；heal tick 在无在途任务时 recover=0。
3. **E3 单测**：VERIFY 任务证据含 target_merge_commit；attestation HEAD 不符时工具回执警告。
4. **E4 单测**：orphan 分支首报 → CEO inbox 恰好 1 条；quarantine 后 `git branch` 无该 ref、`refs/quarantine/*` 可达、tip 可恢复。
5. **端到端**：TEST6 场景第五轮重跑，验收——全任务 closed 30 分钟后 `git worktree list` 只剩 main；obligations 无 pending 泄漏；main log 无空 checkpoint。

> 测试请在用户终端跑 pytest，勿在 WorkBuddy 沙箱跑（环境安全约定）。

---

## 附录 A：晚场运行时间线（DB 实证）

```
19:31:20  组织创建（A001 CEO / A002 HR）；19:33:11 A003 Arlo 到岗
19:33:35 → 19:42:14  ef5f972d 图片生成工具验证 ✅ closed（S2 豁免首用）
19:44:45 → 20:02:13  fc8b3aec 美术优化（16 张 AI PNG 替换 SVG）✅ closed
                      ├─ 19:56:06 approve（8 位前缀 → 义务漏账 bd98faad）
                      └─ de317036 VERIFY（A004）20:02:13 closed
19:57:07  A004 潮汐（QA executor）到岗
20:04:42  CEO start_dev_server port 3000（至今存活 → P2-6）
20:20:18 → 21:13:12  20b3c1af 完整游戏体验验证 ✅ closed
                      ├─ 20:24→20:41 空窗 17 分钟（P2-5）
                      ├─ 20:52 假 blocked 诊断（P2-5）
                      ├─ 21:09:04 A004 test_run exit 0；21:10:00 approve（S1 consume 首用）
                      ├─ 21:10:20 merge（rebase→FF，73cbabc 落 main；3202 进程先杀后删）
                      └─ 7434cfd4 VERIFY（A003）21:13:12 closed
                         └─ attestation commit=a4f3939（过期基线 → P1-3）
21:10:21 → 21:14:04  worktree 删 4 建 5（P1-2）
21:44:16  A003-b/A004-b husk 经 reconcile 重试后清除
```

## 附录 B：git 拓扑最终态（避免误报 stranded）

```
main: … a4f3939 → 737e4e9 → 73cbabc → 4937306 → 159eee9 → 471900f (tip)
                      A004 的工作    pre-merge   A003 VERIFY    空 checkpoint
                      (FF 落 main)   checkpoint  (d36ce65 的    (P3-7)
                                            rebase 孪生)
d36ce65 (dangling): parent=a4f3939 —— 内容 = 159eee9 的子集，无丢失
```

merge 流程 = worktree 内空 checkpoint → `git rebase main` → `git merge --no-edit`（FF）→ teardown。分支经 rebase 重写后恒为"已合并"，`git branch -d` 合法通过，未合并分支 preserve 的安全链语义不被破坏。
