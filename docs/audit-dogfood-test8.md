# Dogfooding 实测问题清单 — TEST8 项目

- **日期**：2026-07-21
- **背景**：在 TEST8 项目（workspace `D:\PC_AI\Project\TEST8`）对 HiveWeave 进行了一轮端到端 dogfooding 实测：CEO（归零）+ HR（天线）+ Test Lead（云岫）+ 多名测试/验证工程师（潮汐、北辰、青鸟、星野、流萤、墨羽、知更），执行 M1–M6 模块验证任务，覆盖任务派发、提交审查、worktree 合并、VERIFY 独立 QA、停滞催办、三层记忆等核心机制。
- **数据来源**：
  - 运行时对话与消息记录（agent 实测汇报）
  - per-project DB 只读核查：`D:\PC_AI\Project\TEST8\.hiveweave\data.db`（`tasks` / `memories` / `inbox` / `chat_messages` / `team_chat_dedupe` / `agent_waits` 表）
  - 源码核查：`apps/hiveweave-py/src/hiveweave/`

---

## 一、机制验证结果

| 机制 | 结果 | 说明 |
|------|------|------|
| CEO 只派直属中层 | ✅ | 派工链路 CEO→云岫（Test Lead）→执行工程师，层级干净 |
| 自交 → wake 上级 | ✅ | submit 后上级（云岫/CEO）均被正确唤醒 |
| 独立 QA 门禁（VERIFY 不落回实现者） | ✅ | VERIFY 统一派给非实现者（星野/流萤/墨羽/知更），找不到 QA 时正确 block + 通知 HR |
| task-advance 催办 | ✅ | [MERGE PENDING]、[VERIFY BLOCKED]、stale nudge 均按预期发出 |
| 审查质量 | ✅ | rework 与 approve 的反馈具体、可追溯（evidence.review_feedback） |
| 门禁判定准确性 | ❌ | CEO 审批 VERIFY 被误判为"实现者/合并人"拒绝（见 P1-b） |

---

## 二、问题清单

### P1-a 记忆链路断裂：write_memory 返回 "Memory saved" 但 read_memory 永远读不回

> **状态：已修复（commit 待提交）** ✅
> 修复摘要：服务层 `get_agent_memories` 增加 `module_id` 独立过滤参数（`services/memory.py:135-168`）；工具层 `read_memory_tool` 改为按 `scope='agent'` + `module_id` 列过滤（`tools/orchestration_tools.py:875-887`），渲染键名 `category`→`type`。写入侧 `add_entry`（scope='agent' + module_id 列）本即正确，读写已对称。回归测试：`tests/test_p1_p2_dogfood_fixes.py` 3 条（写读回环、moduleId 过滤、服务层对称）。

**现象**：19:46 由 agent 青鸟（e1534986，记忆与浏览器验证工程师）实测，`write_memory` 返回 `Memory saved.`，随后 `read_memory` 跨 moduleId / tags / 多轮对话均返回 `(no memories)`。

**证据**：
- TEST8 `memories` 表确认写入**确实落库**（5 条记录，agent_id=e1534986=青鸟，scope='agent'，module_id='M8' / 'M8-T1-test' / 'M8-test-write' / 'M8-persistence-test' / 'M8-notag'，含 tags 元数据）。即写入路径无故障，返回 "Memory saved" 是真实的。
- 因此断裂点在**读取路径**。

**根因分析**：

读取工具把 `moduleId` 当成了 **scope** 传给服务层：

- `apps/hiveweave-py/src/hiveweave/tools/orchestration_tools.py:877-879`：

  ```python
  entries = await mem.get_agent_memories(
      target_agent_id, project_id, params.module_id or "agent"
  )
  ```

  `get_agent_memories(agent_id, project_id, scope)` 的第三个参数是 **scope**（`memory.py:135-147`），SQL 为 `WHERE scope = ? AND agent_id = ?`。当 agent 按工具描述传入 `moduleId="M8"` 时，实际执行的是 `WHERE scope='M8'`，而 `write_memory` 写入时 scope 恒为 `'agent'`（`memory.py:181`，`add_entry` 硬编码 `scope = "agent"`）——**永远匹配不到任何行**。`memories.module_id` 列在读取路径上从未被查询。

- 次要 bug（同一函数）：`orchestration_tools.py:883` 用 `e.get('category', '?')` 渲染条目，但 `_row_to_memory`（`memory.py:292-296`）返回的行字典键是 `type`，没有 `category`，即使读到也会全部显示为 `[?]`。

**结论**：写路径把记忆写到 `scope='agent'` + `module_id=<用户传入值>`；读路径把 `moduleId` 塞进 `scope` 过滤条件。两侧对 `moduleId` 的语义理解不一致，导致 100% 读不回。不是 DB 上下文错误，也不是缓存问题（`save_memory` 后已定向失效缓存，`memory.py:236-237`）。

**修复建议**：
1. 服务层增加按 module 过滤的读取（如 `get_agent_memories(..., module_id=...)` → `WHERE scope='agent' AND agent_id=? AND module_id=?`），或工具层读取后按 `module_id` 过滤；`moduleId` 缺省时返回该 agent 全部 agent-scope 记忆。
2. `orchestration_tools.py:883` 的 `e.get('category')` 改为 `e.get('type')`。
3. 补一条 write→read 回环集成测试，覆盖带/不带 moduleId 两种调用。

---

### P1-b VERIFY 审批门禁误判：未参与实现的 CEO 被判为 implementer/merger 拒绝

> **状态：已修复（commit 待提交）** ✅
> 修复摘要：
> 1. 审门 forbidden 移除无差别 `creator_id`，仅当 creator 本人即实现者/合并人时才禁止（`tools/task_tools.py:1197-1232`）——CEO 可正常审批 VERIFY，实现者与合并人仍被拒。
> 2. `merged_by` 持久化：`submit_task` 覆盖 evidence 时自动保留既有 `merged_by`（`services/task.py:573-600`），审门在 submit 后仍能有效排除合并人。
> 回归测试：`tests/test_p1_p2_dogfood_fixes.py` 5 条（CEO approve 不被误判、CEO rework 端到端通过、实现者仍被拒、合并人仍被拒、merged_by 持久化）。

**现象**：20:27，CEO（归零，015120a5）对 VERIFY 任务执行 `review_task(approve)`，被系统拒绝，报错大意为"VERIFY approval must come from the CEO or an independent reviewer — the implementer / merger of the parent task cannot approve its verification"。CEO 本人既未实现也未合并该任务。

**证据**：
- TEST8 `tasks` 表：所有系统 spawn 的 VERIFY 任务（如 `33b9a9b0` VERIFY M5-T1、`3dc488bc` VERIFY M4-T1、`e420c366` VERIFY M4-T2）的 `creator_id` 均为 `015120a5-…` = **归零（CEO）**。
- 已关闭 VERIFY 的 `evidence.reviewed_by` 全部是 `1368663b`（云岫），没有任何一条由 CEO 审批成功——与误判吻合。

**根因分析**（新逻辑自相矛盾）：

1. 今天新提交的 VERIFY spawn 逻辑把 **VERIFY 的 creator 落到 CEO**：
   `apps/hiveweave-py/src/hiveweave/tools/task_tools.py:1820-1828`——注释明确写着"VERIFY 的 creator 落到 CEO（审权不落回 merger=中层）；submit 时 [TASK SUBMITTED] 因此直达 CEO 做里程碑验收"，`creator_id = ceo["id"]`（`task_tools.py:1828`，经 `create_task(..., creator_id=creator_id, ...)` 落库，`task_tools.py:1857`）。

2. 同一天新加的 VERIFY 独立审门却把 **creator_id 无差别加入 forbidden**：
   `task_tools.py:1197-1216`——forbidden 集合 = `evidence.merged_by`（1207-1209）+ `task.creator_id`（1210-1211）+ 父任务 assignee（1212-1216）；`if str(agent_id) in forbidden: 拒绝`（1217-1222）。

两条规则叠加：VERIFY 的 creator 恒为 CEO → CEO 永远在 forbidden 里 → **CEO 永远无法审批任何系统 spawn 的 VERIFY**。门禁的本意是排除"实现者/合并人"，`creator_id` 被当作了实现者的代理变量，但新逻辑已把 creator 改成了 CEO，代理假设失效。DB 中 VERIFY 的 `evidence` 实际没有 `merged_by` 字段（提交时 evidence 被 submit 覆盖），所以真正命中拒绝的是 1210-1211 的 creator 分支。

**修复建议**：
- 从 forbidden 集合中移除 `task.creator_id`，或仅在 `creator_id == 父任务 assignee / merged_by` 时才加入（creator 不再携带实现者语义）。CEO 身份本身应由"审权不落回 merger"逻辑显式豁免。
- 同时注意 `evidence.merged_by` 在 submit 后会被覆盖丢失（spawn 时写入 `task_tools.py:1868`，submit 的 evidence 不含该键），若仍要排除合并人，应把 `merged_by` 持久化到独立列或在 submit 合并 evidence 时保留。
- 补测试：creator=CEO 的 VERIFY，CEO approve 应通过；父任务 assignee approve 应拒绝。

---

### P2 doom loop ×4：commit_turn 同参数连调 8+ 次熔断

> **状态：已修复（低成本缓解，commit 待提交）** ✅
> 修复摘要：根因之一是 `commit_turn` 对同参数重复调用返回逐字相同的 "TurnResult accepted"，模型在相同上下文中做出相同决策形成正反馈。修为：同一 turn 内同参数 commit 已被接受时，返回差异化提示（明确告知"已提交，勿再同参调用，输出收尾文本等出口闸门"），打破重复循环（`tools/turn_tools.py:91-107`）。熔断-警告机制本身按设计工作，保留不动。深层诱因（P1-a 卡住验证、LLM 超时重试）随 P1-a 修复与 Ark 模型池覆盖缓解。

**现象**：云岫 20:10 / 20:18 / 20:53、潮汐 21:09，共 4 次触发 doom loop 熔断（`commit_turn` 同参数连调 8+ 次）。

**证据**：TEST8 `chat_messages` 表 `[ERROR] Doom loop detected: tool 'commit_turn' called 8+ times with same args` 共 5 条记录，时间窗 20:07:46–21:10:30。

**根因分析**：`commit_turn` 的工具结果不改变 agent 可见的任何状态（无新消息、无任务状态变化），LLM 在同一上下文里反复做出同一工具调用决策；熔断器（doom-loop 检测）按"同工具同参数 ≥8 次"切断。触发聚集在 20:07–21:10，与 LLM 请求总超时（下条）同一时段——超时重试导致上下文停滞，放大了重复调用。

**修复建议**：`commit_turn` 返回结果中注入可区分的状态（如 turn 序号/剩余义务摘要），或在 prompt 层提示"commit_turn 已提交、等待外部事件"；熔断后应将 agent 置为等待态而非立即重试同一上下文。

### P2 消息三连发轰炸

> **状态：已修复（commit 待提交）** ✅
> 修复摘要：`TeamChatService` 新增 `check_and_mark`（检查+登记原子化去重，与 `record_message` 同 MD5 规则、同 60s 窗口、fail-open，`services/team_chat.py:87-125`）；trigger digest 写库前先过该方法（`agents/trigger.py:433-464`）——窗口内重复只跳过落库（`digest_msg_id=None`），agent 仍正常被唤醒 chat，超时重试语义不变。回归测试：`tests/test_p1_p2_dogfood_fixes.py` 2 条（窗口内去重/不同内容不受影响、record_message 原语义不回归）。

**现象**：同一内容向同一接收人 3 连发："## Goals Workbook (updated)" 20:19:38 向流萤×3、20:54:41 向墨羽×3；"## Pending Tasks" 块 20:05:44 / 20:47:09 向北辰×3、20:56:30 向潮汐×3、21:10:29 向青鸟×3；submit 通知同类连发。`team_chat_dedupe` 表存在（52 行）但没拦住。

**证据**：TEST8 `chat_messages` 表精确重复（from, to, content 完全相同、created_at 同一秒）：`1368663b→19fe78c4` Goals Workbook ×3（20:19:38）、`1368663b→8cb058f1` ×3（20:54:41）、`1368663b→f195ee22` Pending Tasks ×3（20:05:44、20:47:09）等。

**根因分析**：`team_chat_dedupe` 去重表只服务于 `TeamChatService.record_message`（`apps/hiveweave-py/src/hiveweave/services/team_chat.py:33-85`，60s 窗口内 (from,to,content) MD5 去重）。而三连发的消息全部是 **trigger digest**——由 `agents/trigger.py:436-447` 经 `ChatMessageService.save_message` 直接写入 `chat_messages`，**完全不经过 `record_message`，不查也不写 `team_chat_dedupe`**。trigger 路径唯一的防重是 goals 版本号剥离（`trigger.py:424-431`），但它只剥 Goals 块且整条上下文仍落库；当 inbox 消息因超时/失败未 ACK（`trigger.py:409-412` 注释：timeout/error 不标已读以便重试）时，同一 agent 被连续触发 3 次，同一 digest 就落 3 次。

**修复建议**：trigger digest 写库前接入同一去重（或对 digest 内容做 (agent_id, hash(context)) 短窗口去重）；ACK/重试链路应避免对同一 inbox 批次重复构建并保存 digest。

### P2 LLM 请求总超时 ×7

> **状态：不修（本轮）** ⏭️
> 原因：已被 Ark 模型池覆盖（模型池/超时预算在另一线处理），按指示不在本次改动范围内。

**现象**：实测期间 LLM 请求整体超时 7 次以上，多个 agent 对话中断。

**证据**：TEST8 `chat_messages` 表 `[ERROR] 请求总超时` 共 10 条（19:39:04–21:09:54），`[对话被中断]` 4 条（21:13:58–21:19:47）。超时与 doom loop、三连发集中在同一时段，相互放大。

**根因分析**：`llm/streamer.py` 总超时预算在长工具循环（多轮 tool call + 大上下文 digest）下被打满；超时后 inbox 不 ACK → 重试 → 上下文更长 → 更易超时，形成正反馈。

**修复建议**：区分"单次请求超时"与"总预算超时"，总预算接近耗尽时提前收尾并持久化中间态；重试时裁剪 digest（去掉已处理的 Goals/Pending 块）。

---

### P3 派工错配：知更闲置、M1 VERIFY 先派青鸟改派星野致孤儿任务

> **状态：不修（本轮）** ⏭️
> 原因：属编排流程/提示词层问题（HR 派工节奏、Test Lead 手工改派未走系统闭环），非明确代码缺陷；不强行改码。后续方向：手工 VERIFY 纳入 `_is_verify_task` 生命周期、改派时自动关闭原任务——建议单独立项。

**现象**：HR 招聘知更（2e4efa89，M4-M5 验证工程师）后长期闲置（21:08 入职，21:20 才被派工）；M1 VERIFY 最初派给青鸟，云岫随后手工改派星野，青鸟侧留下孤儿任务 `cc1ca4ec`（"VERIFY: M1-T1/T2/T3 QA 验证"，creator=云岫、assignee=青鸟、status=submitted、parent_task_id=None，非系统 spawn 流程产物，无父任务闭环）。

**证据**：TEST8 `tasks` 表 `cc1ca4ec-…` 行；inbox 21:08:34 天线通知"已招聘知更…请将三个VERIFY任务派给…"，21:20:02 才派工。

**修复建议**：VERIFY 改派时应关闭/转移原任务；系统 spawn 之外的"手工 VERIFY"应被 `_is_verify_task` 识别并纳入 VERIFY 生命周期（approve 自动关 parent 逻辑依赖 parent_task_id，`services/task.py:650-651`，孤儿任务 parent 为 None 无法闭环）。

### P3 等待打旋：青鸟 13 条 blocked waiting、WAIT_CYCLE×2、WAIT_TIMEOUT×2

> **状态：不修（本轮，随 P1-a 缓解）** ⏭️
> 原因：已定位为 P1-a 记忆链路断裂的下游症状（青鸟无法交付只能反复请示），P1-a 修复后应复测验证；wait 唤醒条件覆盖"上级任意新消息"属行为调优，建议随复测结果单独立项。

**现象**：青鸟在 M8 记忆验证期间反复进入 blocked/waiting（累计 13 条 blocked waiting），出现 WAIT_CYCLE×2、WAIT_TIMEOUT×2。

**证据**：TEST8 `agent_waits` 表青鸟（e1534986）`phase='blocked'`、note='awaiting reply' 等记录（如 19:57 建立、20:38 清除，挂起 ~40 分钟）。

**根因分析**：记忆链路断裂（P1-a）使青鸟无法完成验证交付，只能向上级反复请示；等待唤醒依赖 `message_from_ref` / timeout，上级回复未命中唤醒条件时只能靠 timeout 兜底，形成打旋。属 P1-a 的下游症状。

**修复建议**：修 P1-a 后复测；wait 唤醒条件应覆盖"上级任意新消息"。

### P3 测试质量：硬编码 PASS，返工后仍妥协通过

> **状态：不修（本轮）** ⏭️
> 原因：VERIFY 空 attestation_ids 放行属审查策略宽严问题（提示词/策略调优），非明确代码缺陷；强制非空或记录豁免原因建议与 attestation 策略整体评估后单独立项。

**现象**：个别验证报告先以硬编码 PASS 提交被 rework，返工后仍以"妥协通过"收尾（测试脚本未真正跑全路径）。

**证据**：VERIFY evidence 中 `attestation_ids` 普遍为空数组（`"attestation_ids": []`），tests_passed=true 仅靠文字声明；M1 VERIFY 报告注明"M1 为 CLI 操作无 UI 可 browse"。

**修复建议**：approve 门禁已要求 attestation_ids（`task_tools.py:1162-1163`），但对 VERIFY 类任务实际放行空数组，应强制非空或记录豁免原因。

---

## 三、遗留状态（实测结束时）

- **merge pending ×3**：M4-T1（9e669027）、M4-T2（07fea708）、M5-T1（b9c35fe1）已 approved 待 `git_worktree_merge`（inbox [MERGE PENDING] 21:06:20–21:06:21 发给云岫）。
- **VERIFY blocked ×3**：33b9a9b0 / 3dc488bc / e420c366 因无独立 QA 被 block（inbox [VERIFY BLOCKED] 21:07:24），后由知更认领，截至结束 3 条 VERIFY 已 submitted 待审批——但受 P1-b 影响 CEO 无法 approve。
- **孤儿任务**：cc1ca4ec（青鸟名下 submitted 的手工 VERIFY，无 parent，无法闭环）。
- **知更闲置**：21:08 入职 → 21:20 才首次派工，空转约 12 分钟。

---

## 四、修复优先级与状态（2026-07-21 已实施）

| 优先级 | 项 | 状态 |
|--------|-----|------|
| 1 | P1-a 记忆读路径 moduleId/scope 错位 | ✅ 已修复（memory.py / orchestration_tools.py，回归测试 3 条） |
| 2 | P1-b VERIFY 门禁 creator_id 误判 + merged_by 持久化 | ✅ 已修复（task_tools.py / services/task.py，回归测试 5 条） |
| 3 | P2 三连发（trigger digest 接入去重） | ✅ 已修复（team_chat.py / trigger.py，回归测试 2 条） |
| 4 | P2 doom loop commit_turn 同参正反馈 | ✅ 已修复（低成本缓解：重复提交返回差异化提示，turn_tools.py） |
| 5 | P2 LLM 总超时 ×7 | ⏭️ 不修（Ark 模型池覆盖） |
| 6 | P3 派工错配 / 等待打旋 / 测试质量 | ⏭️ 不修（流程/提示词/策略层问题，随 P1 修复复测，建议单独立项） |

**测试**：新增 `apps/hiveweave-py/tests/test_p1_p2_dogfood_fixes.py` 10 条全绿；后端测试套件除以下**既有失败**（干净 HEAD 上同样失败，与本次改动无关，已逐一核实）外全绿：`test_agent_interruption_counting`、`test_attestation_auto_attach::test_submit_auto_attaches_when_ids_omitted`、`test_doom_loop::test_commit_turn_trips_at_6`、`test_doom_readonly_exemption::test_write_tools_keep_table_limits`、`test_loop_hardening::test_submit_tool_requires_attestation`、`test_model_self_test::test_openrouter_api_query`、`test_open_task_reminder`×3、`test_worktree_stable_branch_callsites`×2。
