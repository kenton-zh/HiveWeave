# TEST6_ylgy 实测尸检：HiveWeave 平台问题分析

- **测试项目**: `D:\PC_AI\Project\TEST6_ylgy`（羊了个羊 H5 补全，project_id `2a690e51`）
- **数据来源**: `.hiveweave/data.db`（只读）、后端日志 `tasks/backend-20260729-021008.output`、git 全分支、平台源码对照
- **分析时间**: 2026-07-29 03:30（测试于 02:11 启动，02:57:50 后全员静默，至分析时已停摆 ~35 分钟且无任何自愈迹象）

## 一、测试概况

组织：归零(CEO/A011) → 天线(HR/A012)、沧浪(前端技术负责人/A013) → 磐石(引擎/A014)、流光(UI动效/A015)、木卫(测试/A016)。

47 分钟活跃期内平台完成了一次完整的「探索 → 接口预定义 → 3 模块并行 → review → merge 冲突返工 → merge → VERIFY」循环，账本/义务/合并门禁主干是通的（5 条 merge obligation 全部 fulfilled）。**但收口阶段全线崩坏**：分析时点 main 停在 `d3c3cd5`，3 条分支有待合代码，2 个任务孤儿化，1 个 VERIFY 死锁，CEO 被一条僵尸消息拖进无限触发循环（已 59 次且仍在继续）。

## 二、当前卡死状态快照（分析时点仍在恶化）

| 对象 | 状态 | 卡住时长 |
|---|---|---|
| 木卫 VERIFY 任务 `fd5275e3` | claimed 70%，submit 被 gate 拒 → 等 user 消息（03:56 超时） | ~50 min |
| 归零 inbox 僵尸消息 `045ebfc9` | watcher 每 45s 触发一次，永远无效（trigger_fail_count=59+） | ~43 min |
| 任务 `26e71e29`（测试验收 BUILD） | created，assignee=NULL，从未入帐 | ~67 min |
| 任务 `1ea69725`（Post-merge 集成修复） | created，assignee=NULL，活已干完但没入帐 | ~42 min |
| 3 条 stranded 分支 | A013/A015/A016 各有未合并提交 | — |

## 三、P0 —— 验收与质量门失效

### P0-1 验收死锁三连环：gate 指路 → 工具断路 → agent 误入长眠

木卫完成了全部 E2E 验收（browse 截图、关卡选择→点击卡片→道具栏，131 条 browse_e2e attestation），但 **无法提交**：

1. `submit_task` 拒绝：`coreInteractionExecuted=true required`（task_tools.py，TEST21 M14 引入）。**该 gate 只读 submit 参数，不消费 attestation**——木卫 02:53:50 已拿到 `[core_interaction=1] js probe __HW_TEST__` attestation，gate 看不见它。
2. gate 的指引文案让 agent 去 "Run browse js/eval"，但 browse CLI 的 `eval <内联JS>` 把 JS 当文件路径报 `File not found`（02:55 两次失败）。**指引指向一条走不通的路**。
3. 唯一真正的解锁方式（submit 时传 `coreInteractionExecuted=true`）agent 没理解，转而 `defer_task_advance` + `commit_turn(blocked)` 等协调者——wait kind=user、03:56:14 才超时，**人为制造 1 小时死锁**。

教训：gate 必须「认 attestation 或明确说出参数名」，拒绝文案要给出**可执行的下一步**（`submit_task(..., coreInteractionExecuted=true)`），而不是工具名暗示。

### P0-2 质量门被 waiver 系统性架空（P0-2 硬闸名存实亡）

本次测试 **9/9 次代码类 approve 全部走 waive_attestation，0 次满足「审查方本人新鲜 test_run」硬闸**：

| 时间 | 操作 | 间隔 |
|---|---|---|
| 02:36:51 归零 waive 4bcbb0e5+74855455 → 02:36:58 approve×2 | 7 秒 |
| 02:39:35 沧浪 waive 491beedb → 02:39:38 approve | 3 秒 |
| 02:39:44 沧浪 waive 4bcbb0e5 → 02:39:47 approve | 3 秒 |
| 02:47:05 归零 waive 541ea0ad → 02:47:11 approve | 6 秒 |
| 02:47:31 归零 waive 4a094569 → 02:47:39 approve | 8 秒 |

- **CEO 归零全程 0 条 test_run attestation**，豁免理由均为「我已 read_file 逐行审查」——读代码 ≠ 跑测试，恰恰是 P0-2 要防的行为。
- waive 只需自填 20 字理由，无第二方确认、无频次限制、无「豁免人≠批准人」隔离。**逃生舱比正门宽，硬闸形同虚设。**
- 流光被 gate 卡住时，沧浪的解法也是 waive（02:34:52），理由揭示另一个工具缺陷：**browse 工具不支持 taskId 参数，attestation 绑不上任务**。

### P0-3 VERIFY 带病通过 + 验收空头承诺

- 流光 VERIFY 引擎模块（4a094569）与冲突修复（541ea0ad）的结论均为通过，但证据写明 **"179 passed, 30 failed"**，30 个失败被合理化（"A016 的在研功能测试"）。**main 被判定健康时，同一测试命令实际是红的。**
- 两个 VERIFY 的 review_notes 都写「功能 E2E 由木卫统一验收」——**验收责任被转移给一个现已死锁的任务**。verification_cases 里这两条已 closed=passed，而承载最终承诺的 `3bd4958b` 永远停在 in_review。质量链的终点是断的，但链上每一环都显示绿色。
- 30 失败的结构性根源：**从项目根跑 vitest 会发现 `.hiveweave/worktrees/*/src/**/*.test.ts`**（流光证据里路径为 `.hiveweave/worktrees/A016/src/game/__tests__/props.test.ts`）。平台把 worktree 放在项目内部，任何 glob 型工具（vitest/tsc/eslint）都会被其他 agent 的在制品污染——02:21 的 VERIFY 已出现「3 个 worktree 副本各 19 tests」并被人肉正常化。**worktree 布局使"在 main 上跑测试"这个 VERIFY 基本动作失去意义。**
- 佐证不可靠：22 条 test_run attestation 全部 exit=0，与 "30 failed" 矛盾——失败的那次全量跑**没有留下 attestation**，attestation 只记录了切片成功的命令。证据模型有「报喜不报忧」的结构性偏差。

## 四、P1 —— 自愈与守护机制失能

### P1-1 inbox watcher 与 trigger 口径分裂 → CEO 无限触发循环（仍在进行）

消息 `045ebfc9`（`[TASK SUBMITTED] (4a094569)`，message_type=**task_event**，02:47:14）：

- **watcher**（agent.py）`get_pending_messages`：`read=0 AND wake=1` 全量计入，**不过滤 task_event** → 每 45s 发现 1 条 pending；
- **trigger_coordinator**（trigger.py 步骤 4）：明确过滤 `message_type=='task_event'`（注释：FYI-only，防 busy-wait）→ 过滤后为空 → `trigger_coordinator_no_messages` 返回，**不把任何消息标记已读**。

两个组件对同一条消息「算不算 pending」结论相反，消息永远 read=0。日志 `inbox_watcher_trigger_ineffective` 已 59 次（45s 退避），**平台检测到了循环（trigger_fail_count 递增）但没有任何熔断/清账出口**。修复：watcher 侧套用同一 task_event 过滤，或 trigger 判空后把扫描过的 FYI 消息标记已读；trigger_fail_count 超阈值应自愈（标读/归档）而非无限退避。

### P1-2 verify stale nudge 被幂等键永久去重（自我静默的闹钟）

03:09:23 对 `fd5275e3` 的 stale nudge：`inbox_deduped`（命中 02:53:16 同款消息的 idempotency_key）→ 消息不落库 → `trigger_no_context` 空唤醒木卫 → 木卫 wait 契约（wake_on=user_message/task_transition/timeout）不匹配 → 继续睡。**inbox 去重无时间窗**（对比 team_chat 有 60s window_bucket），同一任务的第二次催办永远发不出去；而 `verify_stale_tasks_nudged count=1` 照记，形成「已催办」假象。催办类消息语义无幂等性，idempotency_key 应含时间桶或 nudge 序号。

### P1-3 沉默看门狗名存实亡

`health_supervisor` 02:10:09 启动后**零条 silent 事件**：全员静默 10-40 分钟无一人被标。源码两处致命伤：`_check_silent_agents` 命中阈值后只有 `log.warning` + `# TODO: wake agent + broadcast health error`（唤醒未实现）；外层 `except` 用 **debug 级**记 `check_project_failed`（若每轮抛异常则完全隐形）。CLAUDE.md 宣称的「10 分钟无产出→唤醒+红框」在此轮实测中不存在。

### P1-4 孤儿任务无守护者 + 账本外干活

- `26e71e29`：沧浪 02:21:29 **用 send_message 把任务"派"给了木卫**（type=task 消息），02:22:58 还向 CEO 汇报「已 dispatch，木卫 working」——但**从未调用 dispatch_task，账本 assignee=NULL**。木卫真干了活（f88afbc：3 个 TDD 测试文件），任务却始终 created。task_stall_nudge（created>5min）**只对已分配任务生效**，孤儿任务无任何看护。
- `1ea69725`：沧浪 02:47:15 建任务，随后**在自己 worktree 里把活干完了**（5ceff57 集成修复 + 5a24edd pointer-events 修复），但任务仍 created、assignee=NULL，提交 stranded。「建账→干活→销账」链条在 coordinator 自己身上断裂。
- 衍生乱象：02:52:45 沧浪通知流光「你的 332e4fc 不需要了，merge 以我的版本为准」——**两个 agent 对同一 bug 各自修了两次**（复活按钮），均 stranded。任务账本没有起到占位/锁的作用。

### P1-5 stranded commits：main 不是可发布状态

| 分支 | 提交 | 内容 | 影响 |
|---|---|---|---|
| hw/A013/work | 5ceff57, 5a24edd | generateLevel2 接入 + coverageThreshold + 复活按钮 + **pointer-events 修复（下层卡片点不动的真实玩法 bug）** | main 上第 2 关仍是硬编码布局、复活按钮调错函数、下层牌点不动 |
| hw/A015/work | 332e4fc | 复活按钮改调 useProp('revive') | 与 A013 重复修复 |
| hw/A016/work | f88afbc, 6457026 | 3 个 TDD 测试文件（coverage/level2/props）+ E2E 验收截图 | 全部测试资产未入库 |

若此刻发布 main，交付的是一个**带已知玩法 bug 的版本**——而平台账面上几乎所有任务都"closed/approved"。

### P1-6 worktree 层旧病复发

- 02:39:56 `stale_path_fallback`：A015 → A015-b 项目中途迁址（`executor_worktree_relocated`）。上次 TEST_YLGY 复盘的老问题，本项目仍发生。
- **heal tick 永不收敛**：02:59/03:05/03:11/03:17/03:23 每 ~6 分钟 `worktree_heal_tick recovered=1`——同一棵树被反复"修复"，治愈状态不持久（或判定条件每轮重估）。
- `reconcile_branch_preserved hw/A006/work legacy_unmerged` 每轮重复报告（历史测试残留分支，仓库复用时 reconcile 噪音）。
- 02:24:59 `vision.path_escape`：agent 造出 `D:\d\PC_AI\...` 双盘符路径——Windows/Git-bash 路径风格混用渗漏给 agent。

## 五、P2 —— 稳定性与数据卫生

- **429 风暴**：02:37–02:47 merge/VERIFY 高峰期 8 条 llm_error（AccountRateLimitExceeded），6 次 retry_exhausted + 6 次 rate_limit_deferred，波及全部 4 个干活 agent；另有 1 次 HTTP 400 InvalidParameter（磐石 02:22:44，inbox_left_unread）。
- `get_messages_failed`×15：agent `b04b459d`（非本项目，疑似已删项目残留）"not registered in Meta DB"——跨项目路由/前端轮询噪音。
- attestation 与 evidence 矛盾（exit=0 vs 30 failed，见 P0-3）；`merge_commit_hash` 格式不一（`d3c3cd5` 短哈希 vs `584f8a3c...` 全哈希）。
- team 消息按收件人逐份存储，日志视角同一内容出现 4 次（沧浪/流光 各一份），审计噪音。
- `no_text_hint_exhausted`×4、`turn_exit_gate_exhausted`×11、`tool_loop_stall`×12（均 forgiven）——模型空文本/绕路倾向仍高，靠平台兜底拉回。

## 六、值得肯定（机制主干是通的）

1. **任务账本主干闭环**：assign=claim、CREATOR_MUST_MERGE、merge obligation 5/5 fulfilled；merge 冲突 → rework → MERGE CONFLICT FIX 任务 → 复审 → 合入的链条完整走通。
2. **VERIFY 独立性裁决正确**：`_find_independent_qa` 把引擎 VERIFY 派给流光（非实现者/非合并者）、UI VERIFY 派给木卫（非实现者流光）， reviewer==assignee 门禁生效。
3. **接口预定义 → 并行解耦**是本轮最顺畅的范式：沧浪先探代码并把 P0 接口落到 main，3 个 executor 零阻塞并行，17 分钟完成三模块 BUILD。
4. stall break + forgive（substantial_progress）12 次判定无一误杀；TurnResult 出口闸门把「干完不说」的回合都逼成了 commit_turn。

## 七、修复建议（全部落机制层，按出手顺序）

1. **P1-1 立即修**：统一 watcher/trigger 的 pending 口径（watcher 同样过滤 task_event，或 trigger 判空后标记已读）+ trigger_fail_count 超阈值自动清账（标读+告警），先止血归零的 45s 死循环。
2. **P0-1**：`coreInteractionExecuted` gate 二选一——(a) 消费同任务 browse core_interaction=1 attestation 自动放行；(b) 拒绝文案直接给可执行调用样例。同步修 browse CLI `eval` 内联 JS（或在工具描述里写死 `js probe` 用法）。
3. **P0-2 补洞**：waiver 增加硬约束——豁免人不得与后续批准人相同（waive→approve 需第三人）；单任务豁免次数上限；豁免理由禁止「read_file 审查」类无执行证据措辞（最小校验：必须引用至少一条执行类 attestation id）。browse attestation 支持 taskId 绑定。
4. **P0-3**：VERIFY 任务 submit 时若 `test_output` 含失败计数>0，平台强制 status=rework 或要求显式 `failures_acknowledged` 字段+逐条归因（结构化，非自由文本）；**worktree 移出项目目录**（或平台在 spawn 测试命令时自动注入 exclude `.hiveweave/`），根治测试污染；attestation 记录失败运行（exit≠0 也落账）。
5. **P1-2/P1-3**：催办 idempotency_key 加时间桶；实现 health_supervisor 的 wake 动作并把 except 提为 warning。
6. **P1-4**：task_stall 覆盖「created 且 assignee IS NULL」（nudge creator）；send_message 类工具检测到文案含 task_id 时硬提示「请用 dispatch_task 入帐」（结构化校验，非文案扫描）；coordinator 在自己 worktree 提交代码时强制关联任务（checkpoint 校验 task 绑定）。

---

*附：人工解锁当前现场的最小动作——SQL 标读归零的 `045ebfc9`；给木卫发任意 user 消息（或等 03:56 超时）；把 `26e71e29`/`1ea69725` 指派并入帐；合并 A013/A015/A016 三条分支。*
