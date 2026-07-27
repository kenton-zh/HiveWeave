# TEST22 复盘：HiveWeave 平台问题解剖与系统性解决方案

> 数据来源：TEST22 `.hiveweave/data.db` 只读解剖 + git 历史 + worktree 现场，2026-07-26 23:14。
> 原则：按 CLAUDE.md「治根不打地鼠」；方案落点按提示词入账纪律（检测层硬门 > 机制 > 提示词）。

---

## 一、事件时间线（全部证据来自 DB 时间戳）

| 时间 | 事件 |
|------|------|
| 21:57–22:09 | 项目创建（HiveBoard 实时协作看板），CEO 归零 + HR 天线自动上岗 |
| 22:09–22:12 | 招 2 名中层（云帆/前端架构师、潮汐/后端架构师）+ 7 名 executor，共 11 agent |
| 22:12–22:53 | 9 个模块任务并行推进，11 个任务 closed（含 VERIFY 链），账本流转总体健康 |
| **22:52** | **磐石(A048) 第一个撞上 HTTP 429 `AccountQuotaExceeded`（周配额耗尽）** |
| 22:53–22:57 | 磐石的任务被人工式转交（潮汐接手完成）——纠偏路径有效，但靠的是 agent 判断而非平台机制 |
| 22:59–23:06 | **青岚(A043) 撞 429 停机**。停机后仍被：① 派入 VERIFY 任务（claimed 卡死）② 连环 ask ×5（未读）③ 被云帆、CEO 分别 `commit_turn(waiting)` 等待 |
| 23:01–23:13 | 云帆(A041)、CEO 归零、潮汐(A042) 陆续独立撞 429 |
| **23:13** | **全项目停摆**。死锁链：CEO 等云帆 → 云帆等青岚 → 青岚等 quota reset |
| 23:14（解剖时） | 3 条 wait 未 cleared；6 条 inbox 未读；3 个任务卡死（见下） |

**配额真相**：429 响应体明确写着 `It will reset at 2026-07-27 00:00:00 +0800`——但青岚的 `quota_reset` wait 只设了 **15 分钟**（23:06→23:21 到期）。wait 到期唤醒 → 再撞 429 → 再 wait 15 分钟，每次唤醒都是一次 LLM 调用，**在本已耗尽的配额上空转放血**。

**项目停在哪**：main 分支 `App.tsx` 仍是骨架（"加载中..."），看板 UI 组件躺在青岚的 worktree 里没合并。11 个模块任务 closed，但最关键的三个卡在终点线前：

| 任务 | 状态 | 卡因 |
|------|------|------|
| BoardLayout 看板渲染 | `running:95` | merge 冲突 rework 打回时 assignee 青岚已 quota 停机 |
| RealtimeSync 实时协作 | `verifying:97` | 已 merge，VERIFY 无人执行 |
| VERIFY RealtimeSync | `claimed:10` | 派给了 quota 停机的青岚 |

---

## 二、暴露的平台问题（按严重度）

### P0-1：429 被吞进对话流，熔断器形同虚设

**证据**：`chat_messages` 里 12+ 条 `[ERROR] HTTP 429: {"error":{"code":"AccountQuotaExceeded",...,"Request id":...}}` 以 **assistant role** 持久化。

**根因**：provider 错误走了「对话内容」通道而非「异常」通道。后果三连：
1. `circuit_breaker` 根本没机会触发——它看到的是一轮"正常完成"的对话；
2. 错误文本（含 Request id 等噪声）进入对话历史，**下个 turn 消耗 token 并误导 agent**；
3. agent 把报错当"工作内容"继续推理，行为不可预测。

**定性**：这是 streamer 的架构 bug，不是 agent 笨。

### P0-2：无项目级配额熔断 + reset 时间没解析

**证据**：5 个 agent 在 12 分钟内**陆续独立**撞同一个 429；`agent_waits` 里 `quota_reset` 用固定时长（900s），而错误体里白纸黑字给了真实 reset 时刻。

**根因**：全项目共享一个 API key，`AccountQuotaExceeded` 是**全局事件**，平台却按 per-agent 错误处理。每个 agent 各自撞墙才知道墙存在；等待链上游（CEO→云帆→青岚）的 wait 各自独立到期、独立唤醒、独立空转。

### P0-3：quota 停机 agent 仍被派工/被等待 → 结构性死锁

**证据**：青岚 quota-waiting 期间——VERIFY 任务照常 claim 给他（23:05:59）；5 条 ask 未读；云帆与 CEO 的 wait 均未 cleared。

**根因**：`_find_independent_qa` / `dispatch_task` / `claim_task` / `send_message` 全部不检查目标 agent 的 disposition。平台有 parked/waiting 状态机，却不在派工入口使用它。

### P1-4：文件所有权无硬门 → 并行 merge 冲突高发

**证据**：dnd 模块 6 个文件**两轮**冲突——青岚（BoardLayout）越界改了磁铁（DragDrop）的 `src/dnd/*` 领地；waits 记录 4+ 轮中层手工冲突修复（"等待云帆修复合并冲突后重新提交"反复出现）；`merge_conflict_rework` 把任务打回 running 时 assignee 已停机。

**根因**：「负责文件」只写在任务文案的自然语言里，平台没有结构化所有权表；N 个 worktree 并行写同一路径无任何互斥。冲突解决 100% 靠 agent 手搓 git，中层成为人肉 merge 机器。

### P1-5：任务状态机缺「角色×转移」硬门

**证据**：云帆自述「你的任务 (2ef5ead4) 已被我**误改为 running 状态**（本应保持 submitted）」→ 需要 assignee 重新 submit，而 assignee 已停机 → 死结。

**根因**：`update_task_status` 对 reviewer 开放了反向转移（submitted→running）。policy.py 对工具能力有硬门，但对**同一工具内的状态转移矩阵**没有硬门。

### P2-6：worktree 对账盲区 + 杂项

**证据**：`.hiveweave/worktrees/A049/` 目录半残（只剩 `server/`），`git worktree list` 已无登记，`reconcile_worktrees` 未回收；实际使用的是重建的 `A049-b`。花名 "Evan"（A045）中英文混排，命名规范校验未拦。

---

## 三、系统性解决方案（分层落点）

> 纪律：能下沉到检测层的绝不放提示词。本轮 6 个问题**全部**应落在代码/机制层，提示词层零新增。

### 检测层（硬门，首选落点）

| # | 方案 | 落点 |
|---|------|------|
| S1 | **provider 错误与对话内容分流**：429/401/403/quota 类错误在 streamer 解析为结构化 error event → 写 `agent_events` + 触发熔断，**不落 `chat_messages`**（或落 `is_background=1` 的系统行，不进 LLM 历史） | `llm/streamer.py` + `conversation/store.py` |
| S2 | **派工排除停机 agent**：`dispatch_task`/`claim_task`/`_find_independent_qa`/`spawn_verify` 目标 disposition ∈ {quota_blocked, parked, blocked} → 硬拒并提示替代人选；quota 恢复后走现成的 `retry_qa_blocked_verify_tasks` 同款重挂 | `tools/task_tools.py` + `services/dispatch.py` |
| S3 | **状态转移矩阵硬门**：reviewer 仅允许 submitted→(approved\|rework)；assignee 仅允许 running→submitted；反向转移硬拒。`merge_conflict_rework` 平台自动转移豁免，但改走「通知」而非依赖 assignee 在线 rework | `services/task.py` + `services/policy.py` |
| S4 | **文件所有权校验**：任务账本增加 `owned_paths` 结构化字段；`submit_task` 校验 `evidence.files_changed ⊆ owned_paths`，越界 reject（与 `normalize_evidence_path` 同一入口） | `services/worktree_review.py` |

### 机制层

| # | 方案 | 落点 |
|---|------|------|
| S5 | **项目级配额熔断**：任一 agent 收到 `AccountQuotaExceeded` → 项目级 circuit 打开：全项目 agent 暂停 LLM 调用、置 `quota_blocked`、pending wake 挂起；**解析错误体 reset 时间**（正则/字段提取 `reset at <datetime>`）作为统一恢复点，解析失败才退固定 cooldown。杜绝「15 分钟一轮的空转放血」 | `llm/circuit_breaker.py`（升项目级）+ `agents/agent.py` |
| S6 | **等待链传递休眠**：`commit_turn(waiting, ref=X)` 时若 X 处于 quota_blocked → 该 wait 的唤醒事件降级为「等 X 恢复事件」，到期不发 LLM 调用。等待沿依赖链收敛到一个唤醒源 | `services/turn_session.py` + `agent_waits` |
| S7 | **merge 预检**：approve 前用 `git merge-tree` 干跑检测冲突；有冲突 → 先派「rebase 前置任务」给 assignee（在线时），而非 approve 后炸 `merge_conflict_rework` | `services/git_worktree.py` merge 门禁前 |
| S8 | **派工阶段路径互斥**（中期）：同一路径段不并行派给两个 executor，从源头降冲突率 | `services/org_invariants.validate_hire` / dispatch |

### 提示词层

**零新增。** 这是本次复盘最重要的方法论结论：6 个问题没有一个是「agent 不够聪明/不够守纪律」导致的，全部是平台机制缺位。往提示词里加「遇到 429 要XXX」属于实例级补丁，违反入账纪律，且对 quota 雪崩毫无用处。

---

## 四、机制有效性验证（正面证据，勿误伤）

复盘不能只记失败，以下机制在 TEST22 中验证有效，方案设计时**不要动**：

- **僵尸 streaming = 0**：自愈链（finalize/sweep/startup 清理）工作正常；
- **义务账本 + 升级链**：merge obligation escalation_count=3 正常升级到 CEO；
- **磐石停机后的任务转交**：纠偏路径（虽然靠 agent 判断）走通了，说明任务账本本身够健壮；
- **VERIFY spawn + 独立 QA 排除原实现者/merger**：选派逻辑正确，错只错在没查 disposition（S2 修）；
- **分支命名 P0 稳定化**：`hw/<sid>/work` 全程无分支增生（旧 slug 病未复发）。

---

## 五、回归对照（TEST22 是 TEST21 修复后的回归测试）

按小申观察清单逐项核对，并与 TEST19/20/21 既往问题对照复发情况：

| 观察项 | 结果 | 证据 |
|--------|------|------|
| 取消率 <10% | **未达标 17.6%**（3/17） | 但构成不同：2 个「后端领域精确规范」重复单 + 1 个磐石配额转交旧单，非 TEST21 式证据链误杀，属合理取消，可豁免 |
| 超时烧时 <5% | **达标** | 无 TOTAL_TIMEOUT 致死、无 STALL BREAK 事件（TEST21 的 4 次×9min 烧时未复发） |
| watchdog 误报 | **达标** | 未见 HR 合法 idle 误报类记录 |
| reassign 有事件 | **未达标/复发** | 磐石转交仍是 **cancel 旧单 + create 新单**（e3052747 cancelled → ad2b748e），不是 `dispatch_task(taskId=)` 转派复用——TEST19 P1-4「机制不可发现」**复发**，工具描述补强/软引导未落地 |
| STALL BREAK 误 park | **达标** | 0 次触发（TEST21 误伤率 4/4 未复发） |
| VERIFY 拖拽真实执行 | **存疑** | DragDrop/BoardLayout 两任务**没有 spawn VERIFY 任务**（其余 6 个实现任务都有）。DragDrop 直接 closed:100。VERIFY spawn 的选择性漏发需查 `_spawn_verify_task` 触发条件 |

### 复发项（既往方案积压未实施，非新 bug）

1. **TEST20 P0-B「配额型 429 空转」完全复发**：当时修法明确写了「解析 reset 头/park 到 reset/key 级配额熔断」，TEST22 实证一条都没落地——quota wait 仍固定时长、无项目级熔断、agent 排队撞墙。**这是本次雪崩的直接原因：不是没诊断过，是诊断了没修。**
2. **TEST19/Echo reconcile 盲区变种复发**：A024-b 之后这次是 A049-b——worktree 重建产生半残旧目录（只剩 `server/`），三方核对仍漏「磁盘存在但未登记」象限。

### 新增实锤（既往未记录的）

1. **429 以 assistant role 落 `chat_messages`**（TEST20 时是 120s 冷却 resume 循环，错误至少走异常通道；本次错误进对话内容通道，熔断器被旁路——可能因 Ark channel 错误返回方式不同而走岔，需对 streamer 全 provider 做错误路径审计）；
2. **等待链跨 agent 无传递休眠**（TEST20 只有单 agent 撞墙，本次三层等待链把空转放大成死锁）；
3. **reviewer 可反向转移任务状态**（submitted→running，云帆自述"误改"）。

---

## 六、边界声明（账要算清）

- **周配额耗尽本身是外部事件**（账号额度），不是平台 bug。平台要背的是「应对方式」：错误分流、熔断、派工排除、reset 解析——四道防线全部缺席，导致一个可预知、可休眠等待的外部事件演变成全项目雪崩 + 死锁。
- TEST22 总投入约 1 小时 10 分钟有效推进（22:06–23:13），11 任务 closed，工程产出质量不差（store/sync/dnd/server 四模块 + 测试齐全）。平台底座是扎实的，本次暴露的是**异常路径工程**（failure-mode engineering）的系统性欠账——正常路径越顺，异常路径的洞越显眼。
