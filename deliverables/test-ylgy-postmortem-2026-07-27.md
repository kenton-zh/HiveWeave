# TEST_YLGY 复盘：HiveWeave 平台问题解剖与系统性方案

> 数据来源：`TEST_YLGY/.hiveweave/data.db`（8 agents / 20 tasks / 378 chat / 445 work_logs / 252 inbox）、  
> 后端日志 `tasks/backend-20260727-035052.output`、项目 git 历史。  
> 测试窗口：2026-07-27 03:53 → 05:34 SHIP closed（约 101 分钟），产出《羊了个羊》H5（5 模块、209 测试）。
>
> **修订记录（2026-07-27 二轮）**：经与 `git_worktree.py` / `worktree_review.py` 代码交叉核对后修正三处——  
> ① P0-1 机制改述为「ensure 子串误认 `-b` vs merge 死盯规范路径」（初版"merge 读陈旧 DB 路径"方向写反了）；  
> ② CHANGELOG 丢失的因果权重改判：**approve 时 uncovered soft-pass 是主因（归 P0-2），补救 merge 静默失败是次因**，`is-ancestor` 降为 P0-1 配套而非第一刀；  
> ③ P1-2 放弃"扫描任务描述抽路径"（擦边平台禁自由文本启发式纪律），改为结构化字段 + gitignore 白名单；P0-2 表述收紧为"审查方零增量证据"。  
> 落地顺序调整为 **P0-3 → P0-1 → P0-2 → P1-2**。

---

## 0. TL;DR

项目最终"SHIP 成功"，但过程审计发现 **3 个 P0、3 个 P1、4 个 P2** 平台问题。  
最严重的一个：**SHIP 宣布完成、VERIFY 通过，但 CHANGELOG.md 和 README.md 至今不在 main 上**——approve 时缺文件被 warning 级放行（主因），事后补救提交 `5510049` 又因 worktree 路径分裂撞 husk 静默失败、stranded 在 `hw/A003/work`（次因），全程无人知晓。  
好消息：组织骨架（招聘/账本/利益冲突硬门/冲突 rework 闭环/stall 检测）都正常工作。问题集中在 **worktree 进程生命周期、merge 收口完整性、审查侧证据缺失** 三处根因上。

---

## 1. 项目全景与量化指标

| 指标              | 值                                                                                     | 备注                      |
| --------------- | ------------------------------------------------------------------------------------- | ----------------------- |
| 组织              | CEO归零 → HR天线 → 潮汐(技术负责人) → 5 工程师                                                      | 04:01 批量招聘 5 人，4 分钟到岗   |
| 任务              | 20 个（含 7 个 VERIFY、1 个 cancelled）                                                      | 19 closed / 1 cancelled |
| LLM 轮次          | 160 assistant turns / 128 team 消息                                                     | 101 分钟                  |
| inbox 通知        | 177 条 task/task_event 类（≈9 条/任务）                                                      | 噪音来源见 P2-1              |
| merge 冲突 rework | 4 次（M1×2、M2、M3），全部 package-lock.json                                                  | 每次 5-15 分钟              |
| worktree 重建     | 6 次 stale_path_fallback/reuse，5 个孤儿目录残留                                               | WinError 32             |
| 审查时长            | submitted→approved：M1 **12s**、M3 **25s**、五轴 REVIEW **15s**、SHIP 46s、VERIFY 最低 **14s** | 仪式感审查，见 P0-2            |



---

## 2. P0 问题

### P0-1 SHIP 交付物丢失：路径分裂 + 静默失败 + 收口无祖先校验

**完整证据链**（后端日志 + git + 代码核对）：

| 时间                  | 事件                                                                                                             |
| ------------------- | -------------------------------------------------------------------------------------------------------------- |
| 05:32:34            | 潮汐 checkpoint `0a358f5`（RELEASE_REPORT.md），提交 SHIP——**此刻 CHANGELOG/README 尚不存在**                                  |
| 05:33:27            | CEO approve（46s），**`evidence.uncovered_criteria_warning` 仅告警不拦截**（`worktree_review.py:732`，注释明写"evidence sufficiency is the reviewer's judgment call, not a platform hard gate"） |
| 05:33:37            | 平台 `git_worktree.delete` 删 A003 worktree → merge `hw/A003/work`（ed5073c，**本身就不含这两文件**）→ VERIFY spawn → 05:34:14 task closed |
| 05:34:40            | A003 目录删不净（WinError 32，见 P0-3）→ 残留 **husk**（无 .git）；ensure 侧将 agent 重建至 **A003-b**                          |
| 05:35:23-40         | 潮汐发现缺失，在 A003-b 补写 CHANGELOG/README → commit `5510049`                                                        |
| 05:36:18 / 05:37:51 | 潮汐两次调 merge → **`merge_precondition_no_git`**：工具返回 `success:false` 给 agent，但平台侧仅 `log.warning`——义务不重开、不通知 creator、不进 agent_health。任务已 closed，潮汐放弃 |
| 现状                  | `main=ed5073c`；根目录**无 CHANGELOG.md、无 README.md**；`5510049` stranded 在 `hw/A003/work`（`merge-base --is-ancestor` 失败） |

**根因（二轮核对修正后）**：

1. **路径分裂（直接机制）**：ensure 侧（`ensure_executor_worktree`，`git_worktree.py:2432`）用子串 `/worktrees/{short_id}` 匹配 DB 路径——`A003-b` 误命中 `A003` 被判"worktree already bound"，agent 实际在 `-b` 工作；而 merge/checkpoint/`_validate_merge_preconditions`（同文件 :926）**从不读 `agents.workspace_path`**，一律 `_worktree_path(short_id)` 死盯规范路径 `worktrees/A003` → 撞 husk 判死。同一 agent 两个"合法"位置：**写落 `-b`，合盯原址**。
2. **husk 比目录消失更糟**：前置校验注释写明"目录不存在时可从 main 仓合分支"；husk 存在但无 .git → 直接 `precondition_failed`，报错文案让 agent"先修 worktree"——但任务已关，无人执行。
3. **静默失败**：merge 前置失败仅 warning。CREATOR_MUST_MERGE 义务在"merge 事件发生"时清除，而非"tip 已含入"时。
4. **收口无祖先校验（配套项，非本案主因）**：close 只要求"发生过 merge 事件"，不校验 `git merge-base --is-ancestor <branch-tip> main`。**权重说明：CHANGELOG 在 approve 时刻就不存在，即使当时有 is-ancestor 也拦不住首次丢失（tip 已在 main）；它防的是"有 merge fact 但事后提交未合入"这一类——本次补救提交 `5510049` 正是此类，故值得做，但不是第一刀。**

**系统性修复**：

- worktree 路径单一事实源：merge/checkpoint/审查/统计与 ensure 走**同一解析函数**（认 `-b` 或认规范路径，二选一全局统一；按 P0-3 的"目录恒等绑定"落地后即为规范路径）。
- merge 失败 fail-closed：任何 merge 前置失败 → obligation 保持/重开 + inbox 通知 creator + agent_health 黄框，禁止 warning-only。
- `close_task` 硬门（配套）：close 前跑 `merge-base --is-ancestor`，tip 不在 main → 拒绝 close 并重开 MERGE 义务。
- 启动对账增加一条：closed 任务但 branch tip ∉ main → 重开义务（`reconcile_worktrees` 扩展一个查询）。

### P0-2 审查/VERIFY 全面仪式化：12 秒审完核心引擎（本案 CHANGELOG 丢失的第一根因）

实测 submitted→approved 时长：M1 引擎 12s、M3 25s、五轴代码审查 15s、VERIFY SHIP 18s、VERIFY M1 14s。  
这些时长连一次 `run_tests` 都不够——审批者没有执行任何验证动作，直接 approve。  
**表述收紧（二轮核对）**：approve 并非"完全无 attestation"——闸门会复验 evidence 里**提交方**的 attestation / `tests_passed` 标记；问题在于**审查方/VERIFY 方零增量证据**：复用对方证据 + 多数 policy soft，自己一行命令不跑。  
连带后果（本案主因链）：SHIP 在 approve 时刻就缺 CHANGELOG/README，`uncovered_criteria` 被降级为 warning（`worktree_review.py:730` 注释明写这是 **TEST16 P0-1 的故意产品决策**："evidence sufficiency is the reviewer's judgment call"）→ 46 秒放行 → 合入 main 的 `ed5073c` 先天缺文件。P0-1 的补救失败只是把这个先天缺口锁死。

**系统性修复**（镜像提交侧闸门，复用现有 attestation 基建）：

- `review_task(approve)` 前置：代码类任务需审查方**本人**在本任务上的新鲜 `run_tests` attestation（对 merged 结果或 worktree 执行），缺失则拒绝 approve，引导先跑测试。
- VERIFY 任务 submit 前置：`kind=verify` 的 attestation（验证命令执行记录），把"独立验证"从声明变成证据。
- `evidence.uncovered_criteria_warning` 对 SHIP/里程碑级任务升级为 hard reject（可由 CEO 显式 waive，留 waiver 记录）。
- **文档口径同步（必要前置）**：`CLAUDE.md` 现行条款"**不做**提交 attestation 硬闸（证据由领导 review 判定）"与本修复方向存在产品张力，落地前需先改文档与 `worktree_review.py:730` 的设计注释，明确"审查方执行证据"与"提交方证据硬闸"是两条不同的线——前者加，后者维持不做。
- 反方意见：这会把每次 approve 拉长几分钟、烧 token，且"跑了测试"不等于"读懂了代码"。接受这个代价——attestation 保证的是**执行下限**（至少跑过），判断深度仍靠模型；下限能堵住 P0-1 这一类纯流程失守。

### P0-3 worktree 清理 vs 长驻进程：WinError 32 → `-b` 级联 + 路径漂移

日志铁证：5 次 `git_worktree.force_clear_failed [WinError 32]`（A003/A004/A005/A006/A008 全部中招）。  
机理：agent 在 worktree 里用 `start_dev_server` 起 dev server（A005:3000、A003:3001/3002）→ node 进程锁死 node_modules → merge 后平台删 worktree 删不掉 → `_force_clear_path` 失败 → 落到 `-b` 目录继续。后果链：

- 5 个孤儿目录残留磁盘（含仍在运行的 dev server，端口被占），规范路径留 husk；
- **路径分裂由此而起**：删不净 → ensure 侧经子串匹配（`git_worktree.py:2432`，`/worktrees/A003` 误命中 `A003-b`）把 `-b` 认作合法绑定，而 merge/checkpoint 死盯规范路径 → P0-1 的 `merge_precondition_no_git` 直接由此引发；
- 墨羽出现 `.hiveweave/worktrees/A008/.hiveweave/worktrees/A008/...` 嵌套路径写入——agent 对"我在哪个目录"的认知被路径漂移搞乱。

**系统性修复**（落地顺序提到最前，理由见 §7）：

- worktree teardown 前置杀进程：`start_dev_server` 登记表已记录 pid/port → 删 worktree 前先停该 worktree 下所有注册进程，停不掉则**阻塞 teardown 并报错**，而不是绕道 `-b`。
- 消除路径漂移：worktree 目录名与 short_id 恒等绑定（A003 永远是 `worktrees/A003`）；stale 时的正确动作是 `git worktree repair` + 原址重建，不是换名。`-b` 退避只在磁盘真损坏时使用，且必须 inbox 告知 agent"你的工作区已搬迁"。同步修掉 ensure 侧的子串误匹配（:2432），让"认 `-b`"这件事从机制上消失。
- `reconcile_worktrees` 增加：扫描并回收 `.stale-*`、husk（无 .git）与无登记的孤儿目录（进程死后）。

---

## 3. P1 问题

### P1-1 package-lock.json：可预测的结构性冲突，每次 merge 都撞

M1（2 次）、M2、M3 全部因 lockfile 冲突触发 `merge_conflict_rework` urgent 回炉；04:23 还发生一次 untracked 文件隔离（`merge-quarantine/20260727-042327`：package-lock.json + M5-checklist）。  
每个 executor 在自己 worktree 里 `npm install` 都会重生成 lockfile——**这是 100% 可预测的冲突**，却让 4 个 agent 各自花一轮 rework 重新发明"accept main + npm install"。

**系统性修复**（预防层，非提示词）：

- 平台脚手架在项目初始化时写 `.gitattributes`：`package-lock.json merge=union`（或 pnpm/yarn 对应策略），union 后由 merger 侧跑一次 `npm install` 修正。
- 建立**生成物清单**（GENERATED_FILES：各生态 lockfile、dist/、*.min.*）：checkpoint 时若 executor diff 触碰清单内文件 → 自动 `git checkout main -- <file>` 剥离；merge 时对清单内文件自动 theirs+regenerate。
- 提示词层只留一句原则："生成物不随提交走"，不写实例级规则（符合提示词入账纪律）。

### P1-2 跨 agent 共享产物无一等通道 → 直接酿成集成失败

因果链（全部有日志）：

- 潮汐把 5 份模块规格写进**自己 worktree 的** `.hiveweave/shared/`（该目录被 .gitignore，其他 agent 不可见）；
- 任务书里却写着"规格文档：`.hiveweave/shared/specs/M1-engine.md`（必读）"——**assignee 视角下路径不存在**；
- 04:08:42 墨羽报告"两个文件均不存在"；04:11:51 潮汐才意识到，复制到 `docs/` 合入 main（84ea441，约 04:23 落地）；
- **M2 UI 在规格不可见的窗口期（04:08→04:22）完成开发**：自带 `types/game.ts` + mock 数据；
- 05:14 E2E 才发现模块未集成（类型冲突、App.tsx 假数据、9 个 tsc 错误）→ 追加"集成修复"任务（约 30 分钟）+ REVIEW 轮。
- 同类：M5 最初把 e2e 文件写到 git 不可追踪路径（commit 3fc66b6 返工）；墨羽第一次提交 M5 时 worktree 里**根本没有产物文件**（提交无产物约束）。

**系统性修复**（二轮修正：初版"扫描任务描述抽路径"擦边平台"禁对自由文本做启发式"硬纪律，放弃；改为结构化路线）：

- **结构化引用字段**：任务书增加 `artifact_refs[]` / `required_paths[]` 字段，dispatch/claim 时只校验结构字段在 assignee worktree 中的存在性——不碰自由文本，守住"语言无关、禁文案猜意图"纪律。creator 侧在 `dispatch_task` 时同步校验（写错路径当场报错，比 assignee 摸黑更早）。
- **最低成本先行项**：禁止"必读规格"落入 gitignore 的 `.hiveweave/shared/`——脚手架初始化时提供 git 可追踪的 `docs/shared/` 约定目录 + `.gitignore` 白名单；`save`/checkpoint 时对 `.hiveweave/` 下的"文档类"新增文件给 creator 即时警告（该目录天然跨 worktree 不可见）。
- **submit 侧对称闸门**：approve 路径已有 `missing_claimed` 硬拒（`worktree_review.py:739`，claimed 文件不在盘上 → deny）——**不缺存在性检查本身**，缺的是 (a) submit 时刻的对称闸门（别等 approve 才发现）、(b) 空 `files_changed` + criteria 路径仅 warning 的组合逃逸。补法：submit 时跑同一套存在性校验 + 里程碑任务 uncovered → hard reject（与 P0-2 第三条同一处改动）。
- 平台级共享产物存储（中期）：`artifacts` 表 + `share_artifact`/`read_artifact` 工具（DB 承载，天然跨 worktree 可见），规格/契约类文档走 DB 不走文件系统。

### P1-3 模型配置零校验：CEO 首轮对话直接 400

03:53:08，CEO 第一个 LLM 调用崩溃：`max_tokens ... expected <= 128000, but got 384000`。  
种子模型配置的 max_tokens 超出该模型上限，平台在 seed/保存时不校验，第一回合才以 HTTP 400 炸出来。

**修复**：`llm_models` 表增加 `max_output_tokens` 元数据；模型保存/seed 时 clamp 或拒绝超限配置；启动时校验现有配置并告警。（检测层，几行代码）

---

## 4. P2 问题（修不修看投入产出）

| #    | 问题                                                                                           | 证据                                                                                                              | 建议                                                                                                 |
| ---- | -------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| P2-1 | 通知噪音：177 条 task/task_event，agent 反复烧 turn 对账 stale 消息                                        | 归零 47 秒内 3 个空转 turn；潮汐多次"多条消息混杂（部分stale）"                                                                       | 把 `supersede_watchdog_messages` 的 upsert 语义推广到 task_event 类：任务状态前进时旧通知自动 ACK；trigger 注入当前态摘要而非历史流水 |
| P2-2 | merge gate 报错误导：approve+VERIFY spawn 后 status=verifying，再 merge 报"状态为 verifying 而非 approved" | 04:06 潮汐误报"merge 失败"，CEO 花一个 turn 安抚                                                                            | `_check_self_merge_gate` 把 verifying 视为 post-approve 合法态，或报错文案区分"未批准"与"验证进行中"                      |
| P2-3 | browse 工具点不动 React 合成事件，通关页 E2E 被迫降级为代码审查                                                    | 墨羽 05:51 仍在重试，"Element not found or not interactable" / JS `.click()` 不触发 React                                 | browse click 改用 CDP `Input.dispatchMouseEvent`（真实输入事件）；无法交互时明确报错而非假成功                              |
| P2-4 | attestation 绑定 submitter 个人，委托包装任务卡死                                                         | CEO 派的 E2E 包装任务 3410baa3 两次 blocked 后被 cancel（执行者墨羽≠提交者潮汐，拿不出 browse attestation）；同期 waiver 记录 task_id 被截断为 8 位 | attestation 按 task_id 归池（任务内任何 agent 的执行证据都算数）；修 waiver 写库的 ID 截断 bug                              |

---

## 5. 根治方案总表（映射到代码）

| 簇                 | 落点模块                                                                      | 关键改动                                                                      | 层     |
| ----------------- | ------------------------------------------------------------------------- | ------------------------------------------------------------------------- | ----- |
| ① worktree 进程生命周期 | `services/git_worktree.py` / dev-server registry / `reconcile_worktrees`  | teardown 先杀注册进程；目录名恒等绑定（含修 :2432 子串误匹配）；禁静默搬迁；husk/孤儿回收 | 机制    |
| ② merge 收口完整性     | `services/git_worktree.py` / `services/task.py` / `tools/task_tools.py`   | 路径解析单一源；merge 失败 fail-closed（义务重开+通知）；close 前 `is-ancestor`（配套）+ 对账重开 | 检测+机制 |
| ③ 审查侧证据          | `services/attestation.py` / `tools/task_tools.py` / `worktree_review.py`  | approve/verify 需审查方本人执行证据；里程碑任务 uncovered → hard reject；同步改 CLAUDE.md 口径 | 检测    |
| ④ 生成物冲突免疫         | 脚手架模板 / `git_worktree.checkpoint` / merge                                 | `.gitattributes` union 策略 + GENERATED_FILES 清单剥离+regenerate               | 检测+预防 |
| ⑤ 共享产物通道          | `tools/task_tools.py`（结构字段校验）/ 脚手架 gitignore 白名单 / 新 `services/artifacts.py` | `artifact_refs[]` 结构化校验、禁必读文件入 `.hiveweave/`、submit 对称存在性门          | 检测    |
| ⑥ 杂项包             | config/seed 校验、merge gate 文案、browse CDP 点击、attestation 任务级归池、通知 supersede | 见 P1-3 / P2 表                                                             | 检测+机制 |

**明确的非方案**（按提示词入账纪律拒绝）：往 coordinator/executor 提示词里加"merge 后要确认合入""lockfile 冲突怎么处理""规格要写进 docs/"这类实例级规则——全部属于可检测行为，应下沉到上述硬门，提示词一寸不加。

---

## 6. 反方意见（对自己结论的质疑）

1. **"101 分钟交付一个 209 测试的游戏，还要求什么？"** —— 过程指标确实不错。但本次暴露的 P0-1 是**交付物完整性**问题：用户拿到的 main 与报告宣称的不一致。这类问题在小项目里只是少两个 md 文件，在大项目里就是丢代码。性质比效率问题严重。
2. **审查 attestation 会不会只是把仪式感换成跑测试的形式主义？** —— 会部分如此。它保证下限（执行过），不保证深度（读懂了）。深度只能靠更强的模型/更好的审查提示词，平台机制到此为止。建议搭配低成本信号：approve 时记录审查时长+证据，事后可审计，让"12 秒审批"至少在数据里现形。
3. **worktree 目录恒等绑定会不会在磁盘真损坏时卡死？** —— `-b` 退避保留为最后手段，但触发即通知 agent + 记 agent_health，不再静默。
4. **lockfile union merge 有风险吗？** —— union 可能产生语义无效的 lockfile，所以必须配"merger 侧 regenerate"步骤；只 union 不 regenerate 是半个方案。

## 7. 建议落地顺序（二轮调整）

1. **P0-3 teardown 杀进程 + 目录恒等绑定（含子串误匹配修复）**——它是 husk/`-b`/路径分裂的产源；不修它，Windows 上每个项目都会继续产 husk，下游所有修复都会被绕过。
2. **P0-1 路径单一源 + merge fail-closed**（可与 1 同 PR）；`is-ancestor` 作为配套一并落地，但认知上明确它防的是"补救提交 stranded"这一类，不是本案 CHANGELOG 先天缺失。
3. **P0-2 审查侧 attestation + 里程碑 uncovered hard reject**（先改 CLAUDE.md"不做提交 attestation 硬闸"的文档口径，再动代码）——本案 CHANGELOG 丢失的第一根因在这里。
4. **P1-2 结构化 `artifact_refs[]` + gitignore 白名单 + submit 对称门**。
5. **P1-1 生成物清单**、**P1-3 配置校验**。
6. P2 打包按需。

**避坑提醒**：勿按初版先修 close 的 `is-ancestor`——那会治好"有 merge fact 但 tip∉main"一类故障，却放过硬核原因（husk/`-b` 路径分裂与 uncovered soft-pass），给"修好了"的错觉。
