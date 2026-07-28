# TEST_YLGY 第三方回归报告审计(2026-07-28)

审计对象:另一 AI 对 TEST_YLGY 回归(18 closed / 2 cancelled)的报告。  
审计方式:HiveWeave 平台代码 + TEST_YLGY 项目库(`.hiveweave/data.db`,只读)逐条对账,不采信任何无硬证据的声称。

---

## 一句话结论

**报告质量高:三个"真实平台问题"两个完全坐实(含精确到 6 分钟的死循环现场),一个定性偏差(不是平台 bug)。但报告的修复方案不充分——按它的两刀修完,会撞上它没发现的第三层问题(checkpoint 等五个 git 操作不认 -b 迁址),造成二茬事故。**

---

## 一、三个"平台问题"逐条审计

### 问题 1:text-only 模型被注入截图 → 400 死亡螺旋 —— ✅ 属实,修复真实存在且接线完整

| 报告声称         | 审计证据                                                                                                                                             | 判定   |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------ | ---- |
| 截图无条件注入主对话模型 | 修复前 `_normalize_messages_with_images(messages)` 无 supports_images 参数,无条件注入                                                                       | 属实   |
| 工作区已有未提交修复   | `git status`:`M provider.py` + 未跟踪 `test_text_only_model_image_gate.py`(15 个测试:OpenAI/Anthropic/Google × tool/user 注入 × factory NULL→False 保守映射) | 属实   |
| (报告未审)修复是否接线 | `provider.py:1383` 从模型行读 `supports_images`(默认 False,fail-safe"宁剥图不错 400");`:1299` 传入 handler;三 handler 全部实现剥图改 `_IMAGES_OMITTED_NOTE` 文字指引       | 接线完整 |

**报告漏掉的**:未提交修复实际是**一组三个文件**,不是它说的一个:

- `provider.py` — 剥图门本体(报告说了)
- `vision.py` — **vision 槽位强制 `supports_images=True`**。注释明写:防止 vision 模型行出厂默认 `supports_images=0` 被自家剥图门误伤,`look_at_image` 只发文字提示词让模型瞎编。若只提交 provider.py 落下这个,剥图门和视觉验证互相打架。
- `browse_tools.py` — `GSTACK_TERMINAL_AGENT=0` + `BROWSE_IDLE_TIMEOUT=8h`,治 **bun.exe 黑框狂弹/看门狗重生风暴**——这是报告通篇没提的**第四起独立事故**的修复。

另:`uv.lock` 有 494+/494- 纯格式 churn,无包变化,提交前还原。

**测试状态**:未找到该测试跑过的证据。需小申终端执行(勿在沙箱跑 HiveWeave pytest):

```
cd apps/hiveweave-py && timeout 120 uv run pytest tests/test_text_only_model_image_gate.py -q
```

### 问题 2:worktree heal 死循环 + 通知 API 签名漂移 —— ✅ 属实,数据比报告更精确

**签名漂移铁证**(`git_worktree.py:2808-2822`):

```python
await InboxService().send_message(
    project_id=project_id, sender_id="system",
    recipient_id=agent_id, content=..., message_type="system")
```

真实签名(`inbox.py:130`):`send_message(from_agent_id, to_agent_id, message, ...)` —— **四个参数名全错**,TypeError 被 except 吞成 `executor_worktree_relocation_notify_failed`。项目库实证:`inbox` / `chat_messages` 中 `[WORKTREE RELOCATED]` **0 条**,agent 确实从未收到通知。

**死循环铁证**(`agent_events` 表,24 条 `worktree_rebuild`,全部落在 A015/Sage):

- `20:14:50` 首次 `stale_path_fallback`(真正迁址时刻)
- 之后 `stale_path_reuse_existing` **每 6 分 03 秒一次**,从 20:17 到 22:30 持续 2h16m,**审计当下仍在循环**
- `agents` 表:A015 当前绑定 `worktrees\A015-b`,`worktree_error=None`(**git 健康却每轮被判 mismatch**)
- 目录现场:`A015` 与 `A015-b` 并存

机制定性准确:`:2719` heal 只认 `dir_basename == short_id`(P0-3 防路径分裂的精确匹配修复)vs `:673-708` create 的 `-b/-c/-d` 迁址兜底——**上一次修复与迁址策略直接打架**。报告"'只认 canonical basename'与 -b 迁址策略打架"的表述精确。

### 问题 3:回归组长单轮被硬掐死 —— ⚠️ 机制存在,但定性偏差:这不是平台 bug

- 机制属实:`streamer.py:922-941`,`HARD_TOTAL_TIMEOUT_S` 硬墙,耗尽时注入明确指引 `"call commit_turn(phase='in_progress') and continue in the next wake"`。
- **这是显式的切片续作协议,不是缺陷**。明鉴 83 rounds 一轮扫 M1–M5 没续作,是 agent 编排/playbook 问题,不是平台门禁问题。把它列进"真实平台问题"篮子会稀释前两个真 P0 的紧迫性。报告建议标"(可选)",定级尚算克制。
- 修法不在平台:回归 playbook 改为按模块分 wake(知识层/skill,按三层分流纪律不进提示词原则层)。

---

## 二、报告漏掉的问题(本次审计增量)

### 增量 1(最重要):checkpoint / rollback / quarantine / delete / info 五个 git 操作不认 -b 迁址

`_worktree_path()` canonical 直取(无 DB fallback)的分布:

| 行号        | 函数                                       | 走 DB fallback?                       |
| --------- | ---------------------------------------- | ------------------------------------ |
| :992-1006 | `_resolve_agent_branch()`(merge 前置校验用)   | ✅ 有                                  |
| :857      | `checkpoint()`                           | ❌ 直取 canonical                       |
| :1359     | `merge_by_branch()` pre-merge checkpoint | ❌ 直取(与 :1033 校验走 effective **自相矛盾**) |
| :1649     | `rollback()`                             | ❌                                    |
| :1694     | `quarantine_for_review()`                | ❌                                    |
| :1768     | `delete()`                               | ❌                                    |
| :1938     | `info()`                                 | ❌                                    |

**含义**:报告的两刀(修 notify + heal 接受健康 -b)只能止血死循环。一旦 heal 接受 -b 成为长期绑定,上述五个操作会在 canonical(锁死残留/husk)上执行——失败是最好情况,最坏是**在错误的树上提交**。且该隐患**当下已经在发生**:Sage 在 -b 干活期间,任何对 A015 的 checkpoint 请求都会撞 canonical,只是本轮回归没走到这步没暴露。按报告方案修完,二茬事故几乎必然。

### 增量 2:根因链第一环没人问——`stop_processes_for_worktree` 为什么没拦住锁

`:655-663` create 前已执行 P0-3 的"先杀注册进程",A015 仍然锁了 → **锁目录的进程没进 process_registry**(疑似 agent 自行 bash 起的 dev server 未注册)。根因修复方向:bash 工具层对 node/npm/vite/bun 等长驻特征进程强制注册到所属 worktree。

### 增量 3:第四起事故的修复在野(browse_tools.py)

见问题 1 审计。报告盘点工作区时只对了它已知的事故,没做全量 diff——审计方法上的教训:**"未提交修复"必须以 `git status` + 全量 diff 为准,不能以事故记忆为准**。

### 增量 4:PASS 项与次要问题核对

- PASS 项抽查:DOC_WRITE kind=source 门禁(`policy.py:159/168/340-350`)、tool_attestations 表、obligation 去重等机制代码均在;`git log` 最近十提交与报告对照表吻合。PASS 结论机制层面可信(逐条实测频率无法独立复核,不逐条背书)。
- 次要问题定性全部同意:前端 `/api/org/agents/用户` 400(角色名当 agent id 轮询,噪声)、占位 assigneeId 不校验(产品缺口)、`turn_exit_gate_exhausted`(gate 在逼修,非静默漏过)。

---

## 三、整体解决方案

### 本 PR(四件套一起提,不拆)

1. **剥图门三件套**:`provider.py` + `vision.py` + `test_text_only_model_image_gate.py`。vision.py 不能落——落了 vision 槽位被自家门误伤。
2. **修 notify 调用**(`git_worktree.py:2808-2822`):改 `from_agent_id="system", to_agent_id=agent_id, message=...`;`InboxService` 无参可实例化、`project_db` 按 agent_id 路由,`project_id` 参数直接删。
3. **heal 接受健康迁址绑定**(`:2713-2741` 增加分支):`cur` 的 basename == short_id(原逻辑)**或** == `short_id + -b/-c/-d` 中其一且 `_has_git(cur)` 且 DB 已绑定该路径 → 视为合法绑定,清 error 返回,不再 mismatch 重挂。精确后缀白名单,防止复活 A003-b 冒充 A003 的旧路径分裂 bug。
4. **effective path 公共化(增量 1,必须与 3 同 PR)**:把 `:992-1006` 的 DB fallback 提成公共 helper,`checkpoint / merge_by_branch / rollback / quarantine_for_review / delete / info` 全部改走。3 与 4 拆开就是二茬事故。

### 后续(另开 issue,不阻塞)

1. process_registry 强制化:bash 工具 spawn 的长驻进程自动注册(治增量 2 根因)。
2. canonical 解锁后自动迁回 + 清理 -b(可选,有 4 之后不紧急)。
3. `browse_tools.py` gstack 修复单独一个 commit(与剥图门无关,别混)。
4. `uv.lock` churn 还原。
5. 回归 playbook 切片化(明鉴按模块分 wake)——知识层,不改平台。

### PR 前验证清单(小申终端执行)

- [ ] `timeout 120 uv run pytest tests/test_text_only_model_image_gate.py -q` 全绿
- [ ] 修 heal 后构造:A015 绑定 -b、canonical 锁 → ensure 一次 → 返回 success 且 `agent_events` 无新 `worktree_rebuild`
- [ ] 修 notify 后触发一次迁址 → inbox 出现 `[WORKTREE RELOCATED]`
- [ ] 对绑定 -b 的 agent 跑 checkpoint → 操作落在 -b 而非 canonical

---

## 四、对报告的总评

找问题的能力:强(三中二,且死循环机制描述与代码路径逐行吻合)。  
方案能力:中(两刀方向都对,但没看穿 -b 迁址的契约面远比 heal 一处大;问题 3 定性偏)。  
审计完整性:中(漏盘两组未提交改动;未验证测试是否跑过)。  
采信建议:**结论可采信,方案按本文第三节整体替换**。
