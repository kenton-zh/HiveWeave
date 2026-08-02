# TEST19 分析报告二审：证据复核 + 遗漏挖掘 + 方案评估

审计人：一元（WorkBuddy）| 日期：2026-08-02
对象：《TEST19 平台问题分析报告》（另一 AI 产出）
方法：TEST19 工作区数据库（.hiveweave/data.db）全量复核 + 平台源码交叉验证 + git 提交核验

---

## 一、总评

**原报告 8 个问题全部属实，证据链完整，定性准确，没有发现编造。** 但有一个贯穿三个问题的共同根因被完全漏掉，导致两条建议（尤其建议 4）开错了药方——**建议 4 若直接采纳会拆坏 TEST6/TEST13 反复加固的 VERIFY 三方隔离机制，必须拦下**。

数据快照存在多处口径偏差（runs 差 28 个、失败调用低估 3 倍），不影响定性，但"样本中"的表述掩盖了问题的真实规模。

---

## 二、数据快照复核（原报告 vs 全量实测）

| 项 | 原报告 | 实测 | 判定 |
|---|---|---|---|
| agents | 7 | 7 | ✅ |
| 任务 | 10（7 closed / 3 归档） | 10（7 closed / 3 cancelled） | ✅ |
| runs | 88（78 完成 / 10 error） | **116（106 completed / 10 error）** | ❌ 差 28 个 completed |
| turns | 118 | 118 | ✅ |
| waits | 55 | 57 | ⚠️ 小偏差 |
| attestations | 57 | 58 | ⚠️ 小偏差 |
| handoffs | 9 | 9 | ✅ |
| 失败工具调用 | "样本中 40+" | **全量 135**（commit_turn 36 / bash 36 / review_task 18 / submit 9 / run_command 9 / 其他 27） | ❌ 低估 3 倍 |
| commit_turn 门禁 | ×12 | **36 次 REJECTED** | ❌ 低估 |
| review_task 拒绝 | ×9 | **18 次**（TEST_RUN 11 + VERIFY 隔离 5 + 归档/状态 2） | ❌ 低估 |
| .hiveweave 拦截 | ×7 | **3 次**（run_steps 全量） | ❌ 高估 |
| reassigned | "9906288f 4 次" | 实际 2 任务 ×2 次（9906288f、4a8b189e 各 →CEO→退回） | ⚠️ 表述易误读 |

结论：原报告量化是采样估算而非全量统计。问题规模（尤其 commit_turn 门禁 36 次）比报告呈现的大得多。

---

## 三、重大遗漏：tags=verify 污染是 P0-1 / P0-2 / P1-3 的共同根因

**这是本轮二审最重要的发现，原报告完全没看到。**

### 机制链

磐石创建 4 个模块验证任务时，给它们全部打了 `tags: ["verify", ...]`（DB 实证）。而平台 `_is_verify_task()`（verify.py:80）的判定是**双通道**：

```python
title.startswith("VERIFY:")  → True
"verify" in tags             → True   # ← 事故入口
```

于是 4 个普通实施任务被平台**全程当 VERIFY 系统任务对待**，14+ 处 VERIFY 特殊逻辑全部误伤：

| 原报告问题 | tags 污染的作用 |
|---|---|
| **P0-1 自动归档** | 模块A/B close 时被误判为"VERIFY 收口"→ `_close_sibling_verify_tasks` 把同 parent（27397136）下所有 tags=verify 的非 closed 任务当重复清扫。三任务标题与触发者完全不同仍能命中，靠的就是 tags 通道（时间线实证：approve 后 222ms/301ms 归档） |
| **P0-2 磐石被硬拒** | review.py:118 的 VERIFY 隔离门（父任务实现者/合并人不可审）**只对 _is_verify_task 为真的任务生效**。普通任务只有禁自审门。磐石是父任务 27397136 的 assignee → forbidden 集合命中 → 4 个子任务全拒 |
| **P1-3 强制 main 跑测试** | dispatch.py:294 对误判任务注入 [VERIFY GUIDANCE]（"去项目根 main 验证，别用个人 worktree"）；bash.py:1339 强制 attestation 盖 main 戳。知微被引导到 main 跑测试 → main 没有平台源码 → 只能复制 7.3MB 进 A019-verify/ → merge 触发两批 quarantine（目录实证 20260801-215626/215640） |
| **unbound attestation** | 6 条 unbound 全部是探针（4）+ 知微（2），全部 workspace=项目根，集中在 100 秒窗口。VERIFY 绑定推断（bash.py:1140 `_VERIFY_OPEN={created,claimed,running}`）在任务 submitted 后失效——agent 提交后继续补跑测试就绑不上 |

### 为什么这改变了问题的定性

原报告把三件事当成三个独立缺陷分别开方。实际是**一个根因（平台保留 tag 与 agent 自由 tag 无隔离）+ 一个放大器（_is_verify_task 双通道判定）**。02f4cbf 只修掉了 14+ 处影响面中的一处（sibling 清理），其余 13 处（review 隔离门、dispatch 指引、bash 盖戳、claim 行为、waive 限制、obligations、close 级联、verify_spawn、verify_merge、game_time、reconcile、ensure、submit reviewer 指派）至今仍在对 tags=verify 的普通任务生效——**下一个项目里任何 agent 再打 verify tag，P0-2/P1-3 会原样复发**。

### 正确的第一刀（原报告没有）

在 `create_task` 入口把平台保留 tag（verify 等）从 agent 提交中剥离或拒绝（除非任务确为系统 spawn），并把 `_is_verify_task` 收敛为只认 VERIFY: 前缀（tags 通道是 c71f03a/df2a9f0 时代的兼容路径，如今已全是前缀任务）。一处修复，14 处影响面同时消失。

---

## 四、其他遗漏与偏差

### 4.1 P1-4 归因错误：A015 计划从未要求放 .hiveweave/reports/

复核 A015-verification-plan.md 全文 + 全部任务描述：**没有任何"证据放 .hiveweave/reports/"的要求**。计划只写"自动化证据=测试运行输出；实地证据=日志/diff/截图/commit hash"。

真相是 **agent 群体自发选择了 .hiveweave/reports/ 路径习惯**（训练数据惯例或互相模仿），撞上沙箱拦截。真问题仍在——证据落点无官方约定 + 拦截报错无可行动指引（02f4cbf 的 submit hint 已部分对症）——但"计划与沙箱自相矛盾"的表述不实，按它修会修错对象。

### 4.2 状态机异常横跳（原报告未挖）

9906288f 事件流：`submitted(1785591919) → running(1785592112) → submitted(1785592124)`，**3 秒内无 rework 事件的状态回退**。叠加 review_task 失败样本里的 "Task must be 'submitted' or 'reviewing' to approve, but is 'approved'" 和 "Task 9906288f is archived and cannot be reviewed"——说明 agent 侧任务状态视图滞后于平台真实状态（报告提了 6b93f7df 现象但未量化、未归因）。

### 4.3 402 处理：原报告归类"外部因素"放过了一半

实证：10 个 error run 全部 HTTP 402 Insufficient Balance（知微×4、磐石×4、CEO×2，4 分钟连环爆发）；agent_events 全项目只有这 10 条 llm_error；2 条 escalation（知微→磐石→CEO）因收件人同样在撞 402 而永久未读。

402（余额耗尽）与 429（限流）是**完全不同语义**：429 可重试，402 重试必败。平台 retry.py 对 402 没有快速失败 + 全局熔断路径，导致每个 agent 各自撞满 4 次才 give up，且 watchdog 的 escalation 投递给同样已死的收件人。原报告建议 5 方向对，但必须细化为"4xx 分类处理"：402 → 立即全局熔断停止唤醒所有 agent；429 → 全局协调降速。

### 4.4 原报告漏掉的正面证据（影响"修复是否有效"的判断）

- **obligations 账本 12 条全部 fulfilled/cancelled，0 残留、0 escalation**——TEST_YLGY 的"merge 收口不清账"未复发；3 个被系统归档任务的 review obligation 也正确转 cancelled。
- **worktree_error 全干净**，5 个 worktree 路径全规范（A015–A019），无 -b 迁址、无 husk——TEST_YLGY hotfix（W1）实地生效。
- 沉默看门狗未触发、无 orphan streaming。
- **ff80297（TEST18 死锁修复）在 TEST19 得到实地回归验证**：40c57e67 演练任务 submit→waiver→approve 全链路跑通并收口，0c19ee6c（真 VERIFY: 前缀任务）正常走完 VERIFY 流程——原报告提了演练存在，但没有点明这是对 TEST18 修复的实地验证成功。

---

## 五、修复方案靠谱度评估

### 5.1 已落地修复

**02f4cbf（sibling 清理重写）——靠谱，但只是一处止血。**
三层硬门（触发者必须 VERIFY: 前缀 / 目标必须 VERIFY: 前缀 + 标题归一化匹配 / in-flight 全跳过）全部对症，7 个新测试，注释准确记录 TEST19 教训。问题：14+ 处 tags 污染影响面只修了这 1 处（见第三节）。

**ff80297（TEST18 waiver 可见性 + rework 失效 + consume_ids 扩展）——靠谱且已被实地验证**（见 4.4）。

### 5.2 原报告 5 条建议逐条评级

| # | 建议 | 评级 | 理由 |
|---|---|---|---|
| 1 | 归档时向 assignee+creator 推送恢复指引 | ✅ 靠谱 | 机制层、成本小；36 次 commit_turn REJECTED 和 3 次人肉重建都支持 |
| 2 | commit_turn 门禁带步骤清单 + 幂等化 | ✅ 靠谱 | 36 次 REJECTED（占全部失败 27%）实证充分；注意 soft-pass 提示已存在，问题是不可行动 |
| 3 | 平台源码引用通道 + 禁止复制源码 | ⚠️ 半靠谱 | 方向对但**没触到根因**：若普通验证任务不被 tags 污染强制 main，复制需求本就不会被平台制造。先做第三节的第一刀，再评估通道是否仍必要（TEST19 验证对象是平台自身，属特殊情形，不宜为它过度设计通用机制） |
| 4 | 审批门对"协调者创建、非本人实现"放宽 | ❌ **危险，拦下** | 磐石被拦是 tags 污染触发 VERIFY 隔离门，不是门本身过严。该门是 TEST6 P0-2/TEST13 P0-1/BUG-P1b 三轮事故加固的三方隔离（waiver→approve 需第三人、实现者不可审 VERIFY）。放宽它 = 为治误诊拆安全机制。正确修法是消除误判，门本身不动 |
| 5 | LLM 4xx 降级提示 | ⚠️ 靠谱但需细化 | 必须区分 402（不可重试→全局熔断）与 429（账号级→全局降速），统一"降级提示"治不了连环 give up + escalation 投死人 |

---

## 六、修正后的行动清单（按优先级）

1. **P0：create_task 剥离/拒绝平台保留 tag + `_is_verify_task` 收敛为只认 VERIFY: 前缀**（一处修，14 处影响面同愈；须配存量任务的 tags 审计——TEST19 这种已归档库可不动，运行中库需一次性迁移脚本）
2. **P0：402 全局熔断**（不可重试 4xx 立即停唤醒 + 通知用户，不再各 agent 连环 give up）
3. **P1：归档/清扫推送恢复指引**（原建议 1）
4. **P1：commit_turn 门禁可行动化 + 幂等**（原建议 2，36 次实证）
5. **P1：任务状态变更主动推送**（治 4.2 状态滞后横跳）
6. **P2：证据落点官方约定**（.hiveweave 沙箱放行一个白名单子目录，或明确 tool_outputs/ 为标准落点并写进 dispatch 指引）
7. **不做**：放宽 VERIFY 审批隔离门（原建议 4）
8. **缓做**：平台源码引用通道（原建议 3），待第 1 刀落地后复评

---

## 附：二审证据索引

- 归档事件流：task_events（2796c26c claimed→archived、5129a62f running→archived、9906288f submitted→archived，均 system/"sibling VERIFY closed"）
- 触发时间线：2d22ba02+c71c2c44 closed 后 222ms 归档第一批；4a8b189e closed 后 301ms 归档第二批
- tags 实证：tasks 表 6 个模块任务全含 "verify" tag；唯一真 VERIFY 任务 0c19ee6c 是前缀+tags 双命中
- 代码：verify.py:80（_is_verify_task 双通道）、review.py:118（VERIFY 隔离门）、dispatch.py:294（VERIFY GUIDANCE）、bash.py:1140/1339（绑定推断+强制 main 戳）、crud.py:107（VERIFY 不 auto-claim）
- quarantine：.hiveweave/merge-quarantine/20260801-215626、20260801-215640
- unbound：tool_attestations 6 条 task_id=NULL，探针 4 + 知微 2，窗口 1785591974–1785592074
- 402：agent_runs 10 条 error_reason=HTTP 402（1785594266–1785594495）；inbox 2 条未读 escalation
- obligations：12 条全 fulfilled/cancelled
