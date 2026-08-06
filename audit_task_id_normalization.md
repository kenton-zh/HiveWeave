# task_id / 引用 ID 双形态匹配审计报告

审计日期：2026-08-06
范围：`apps/hiveweave-py/src/hiveweave/`，与「attestation task_id 短前缀/横线不归一化」同类问题
已修复范围（不重复报告）：`services/attestation.py` 全部、`tools/tasks/query.py` waiver prefetch/显示、`tools/browse_tools.py` `_materialize_inline_js`

## 背景：平台存在三种 ID 形态

| 形态 | 例子 | 来源 |
|------|------|------|
| dashed 完整 UUID | `abcd1234-ef56-...` | `tasks.id = str(uuid.uuid4())`（crud.py:100），get_tasks 的 `id=` 展示 |
| dash-stripped 完整 UUID | `abcd1234ef56...` | `canonical_task_id()` 的 canonical 形态（attestation 域存储） |
| 8 位短前缀 | `abcd1234` | get_tasks 的 `short=` 展示（query.py:159），`require_task_id` 文档明确允许 agent 使用 |

关键事实：**tasks 表存 dashed，attestation 域存 dash-stripped，agent 两种都可能传**（还有短前缀）。任何跨域 `str == str` 精确比较都是雷。

Agent 可触发性总判：get_tasks 同时展示 `id=` 和 `short=`，`require_task_id` docstring 明说 "agents often pass the 8-char prefix"——agent 传短前缀是**常态行为**，不是边缘 case。

---

## P0 — 静默失败 / 门禁误判，agent 实际会踩

### P0-1 waive_attestation 证据绑定比较双向失效

**文件**：`tools/tasks/waive.py:166-172`

```python
ev_task = ev.get("task_id")
if not ev_task or str(ev_task) != str(params.task_id):
    return ToolResult.err(
        f"evidenceAttestationId must be bound to this task ..."
    )
```

**ID 形态分析**：`ev_task` 是修复后写入的 canonical dash-stripped 形态；`params.task_id` 是 agent 原始输入。短前缀 → 不匹配；**连从 get_tasks 复制的 dashed 完整 UUID 也不匹配**（canonical 剥了横线，`"abcd1234ef..." != "abcd1234-ef..."`）。两个方向都假阴性。

**可触发性**：agent 实际会踩。waive 是 review 死锁的官方逃生门，review.py 的拒绝提示里直接教 agent 调 `waive_attestation(taskId="{tid}")`。误拒会把 agent 推进重复重试循环。

**修复建议**：改用 `services/attestation.py:_task_ids_equal(project_id, ev_task, params.task_id)`（该函数已存在且双侧 canonical 化），或对双侧各跑 `canonical_task_id` 后比较。顺手把后续 `count_waivers/has_valid_waiver/create_waiver` 的入参统一成 `task["id"]`（服务层已 canonical 化，传 raw 也能工作，但统一更稳）。

---

### P0-2 update_progress 短前缀静默成功

**文件**：`services/tasks/progress.py:86-106`；工具入口 `tools/tasks/lifecycle.py:187`

```python
rows = await _query(
    project_id, "SELECT progress FROM tasks WHERE id = ?", [task_id]
)
current = int(rows[0]["progress"] or 0) if rows else 0
...
await _execute(project_id,
    "UPDATE tasks SET progress = ?, updated_at = ? WHERE id = ?",
    [new_val, now_ms, task_id])
```

**ID 形态分析**：未过 `require_task_id`。短前缀 → SELECT 0 行 → `current=0` 兜底 → UPDATE 匹配 0 行 → 无异常。

**可触发性**：agent 实际会踩。工具返回 `Task {id} progress set to N%` 成功回执，但 DB 没动——典型静默 mismatch，比报错更糟（agent 以为记账成功，stall 催办账本却看不到进展）。同包的 claim/submit/review/close/lifecycle 全部在第一行 `require_task_id`，唯独 progress.py 漏了。

**修复建议**：方法首行加 `task_id = await self.require_task_id(project_id, task_id)`（解析失败直接 ValueError，工具层已有 except 转错误回执）。

---

### P0-3 dispatch_task 复用现有任务时 assignee 静默不改

**文件**：`services/dispatch.py:161-178` + `services/tasks/crud.py:478-514`（`update_task`）

```python
if existing_task_id:
    task_id = existing_task_id
    existing = await self.task_service.get_task(project_id, task_id)  # 容忍前缀，OK
    ...
    await self.task_service.update_task(
        project_id, task_id, assignee_id=to_agent_id   # ← 裸 task_id
    )
```

`update_task` 内部（crud.py:512-514）：

```python
params.append(task_id)
await _execute(project_id,
    f"UPDATE tasks SET {', '.join(set_clauses)} WHERE id = ?", params)
```

**ID 形态分析**：`dispatch_task` 工具描述明确让 agent 传 `taskId` 复用现有任务（dispatch.py:126-131），agent 给的自然是 get_tasks 里的形态。`get_task` 容忍前缀（归档检查能过），但 `update_task` 裸 `WHERE id = ?` → 短前缀 0 行更新 → **assignee 没换**。后续 `ensure_assignee_claimed` 走 get_task 容忍路径不报错，inbox 唤醒消息照常发出——任务实际仍挂在旧 assignee 名下。

**可触发性**：agent 实际会踩。这是 TEST21 M3 dedup 提示主动引导的路径（"请复用 taskId=..."）。

**修复建议**：`dispatch.py` 在 `if existing_task_id:` 分支首行 `existing_task_id = await self.task_service.require_task_id(project_id, existing_task_id)`；根治则让 `update_task` 内部 resolve（它是 generic PATCH，所有调用方受益）。

---

### P0-4 depends_on 依赖自动唤醒静默失效

**写入侧** `services/tasks/crud.py:184`（create_task）：`depends_on` 原样入库，agent 传的短前缀/带横线形态直接落 DB。
**读取侧 1** `services/obligation.py:559`：

```python
if fulfilled_task_id not in deps:   # 精确成员判定
    continue
```

**读取侧 2** `services/obligation.py:625-633`（`_all_deps_met`）：`WHERE id IN ({placeholders})` 精确匹配。

**ID 形态分析**：`fulfilled_task_id` 是内部全量 dashed UUID；deps 里躺着 agent 写的 `abcd1234` → `not in` 命中 → 依赖任务永远不自动 unblock。`block_task` 路径（lifecycle.py:121）对 `depends_on_task_id` 有 `require_task_id`，唯独 create_task 的 `depends_on` 列表没有。

**可触发性**：agent 实际会踩（create_task 工具参数 `dependsOn` 无形态约束）。后果是被依赖任务完成后被阻塞任务不醒，靠 task stall 催办兜底才发现——正是本审计要消灭的"静默"类。

**修复建议**：`create_task` 入库前对 `depends_on` 逐项 `resolve_task_id`（解析不了保留原值，与 canonical_task_id 的 fail-open 语义一致）；或读取侧 `_wake_dependent_tasks` 改前缀容忍比较（仿 `turn_exit._task_ref_matches`）。两边都做最稳。

---

### P0-5 reply_to 合约闭合：发送侧容忍前缀，查询侧精确匹配

**文件**：`services/inbox.py`

- 发送侧软警告（422-426）：`cid == rt or cid.startswith(rt) or rt.startswith(cid[:12])` —— **前缀容忍**
- turn exit 兜底（`services/turn_exit.py:187-194`）：同款前缀容忍
- **查询侧闭合判定**（906-919 / 966-979）：

```python
f"SELECT DISTINCT reply_to FROM inbox WHERE reply_to IN ({placeholders})"
```

精确 `IN` 匹配。`reply_to` 原样入库（501 行附近）。

**ID 形态分析**：reply_contract_id 是平台生成的 UUID；gate 提示只展示 `contract=<前12位>`，turn_exit 注释明说 "LLM 用前缀传 replyTo 也能闭合"。agent 传 12 位前缀 → 发送侧判定合法（不警告）→ 原样落库 → `get_outstanding_ask_senders`（被 `turn_exit.py:761` 预检调用）精确匹配找不到闭合 → **假 UNREPLIED_ASKS** → commit_turn 预检 REJECT → 修复循环。同一合约在三个代码路径有两种匹配语义，是典型的"自己跟自己打架"。

连带小坑：auto-close 的 `closed_set`（360-366）也是精确成员判定，前缀 reply_to 会让已回复合约被重复 auto-associate。

**可触发性**：agent 实际会踩（gate 提示本身就只给前缀）。

**修复建议**：治本——send_message 落库前把 `reply_to` 前缀解析成完整 contract id（known_set 里唯一前缀命中则替换），一处归一化，所有读取侧受益；`get_outstanding_ask_*` 保持精确即可。

---

## P1 — 罕见路径或影响有限

### P1-1 cancel_task deadlock 逃逸证据戳静默丢失

**文件**：`tools/tasks/admin.py:135-139`

```python
await task_module._execute(
    project_id,
    "UPDATE tasks SET evidence = ?, updated_at = ? WHERE id = ?",
    [_json.dumps(ev), int(time.time() * 1000), params.task_id],  # ← 裸
)
```

archive 本体走 `archive_task`（close.py:706 有 `require_task_id`）能成功，但这个证据戳 UPDATE 用 raw 输入 → 短前缀 0 行 → `cancelled_in_deadlock` 审计字段丢失，仅 `log.warning`。窄路径（仅 deadlock_escape），但静默。修复：改用 `task_row["id"]`（get_task 已解析）。

### P1-2 waive_merge 证据戳同样裸写，且连锁导致 close 被拒

**文件**：`tools/tasks/waive.py:482-486`，同款裸 UPDATE。后果比 P1-1 重：`ev["merge_waived"]` 写不进去 → 紧随的 `close_task`（519 行，内部已归一化）读 merge gate 时发现没豁免 → close 失败，agent 面对"waive 成功但 close 被拒"的矛盾回执。修复：用 `task["id"]`。

### P1-3 unclaim_task 未归一化（响亮报错，非静默）

**文件**：`services/tasks/claim.py:219-238`。短前缀 → `ValueError("Task not found")`。agent 能得到反馈重试全量 ID，危害低，但与同文件 claim_task/reassign_task（都有 require_task_id）不一致。修复：首行加 `require_task_id`。

### P1-4 agent_waits 任务引用精确匹配，waiter 醒不来

**写入**：`services/wait_contract.py:212/235`（`replace_waits` 存 agent 原始 ref）
**清除/唤醒**：`services/tasks/close.py:570-575`

```python
"SELECT id, agent_id FROM agent_waits "
"WHERE kind = 'task' AND ref = ? AND cleared_at IS NULL",
```

agent `commit_turn(waiting, waiting_on=[{kind:"task", ref:"abcd1234"}])` → ref 短前缀落库 → 任务状态转换时全量 UUID 精确匹配不上 → waiter 不被唤醒，睡满 TTL（task 类默认 TTL 较长）。turn_exit 校验侧 `_task_ref_matches`（48-55）是前缀容忍的，DB 侧不是——又一处同链双语义。修复：`replace_waits` 对 kind='task' 的 ref 先 `resolve_task_id` 再入库。

### P1-5 verification_cases 的 verify_task_id 双形态

**文件**：`tools/tasks/waive.py:234-236`

```python
parent_id = task.get("parent_task_id") or params.task_id
await vcs.ensure_case(
    project_id, original_task_id=parent_id, verify_task_id=params.task_id,  # ← 裸
)
```

`verify.py` 全部查询是精确匹配（`verify_task_id = ?` / `original_task_id = ?` / `LEFT JOIN tasks t ON t.id = vc.verify_task_id` 521 行）。系统内部流程（close.py:792）写的是解析后的全量 UUID；waive 路径写 agent 原始输入 → 同一任务可能出现两条 case 或 reconcile_orphans JOIN 不上（tstatus 为 NULL 的孤儿 case）。修复：waive_attestation_tool 在 get_task 之后统一改用 `task["id"]`。

### P1-6 parent_task_id 原样入库 vs 精确比较

**写入**：`tools/tasks/create.py:58` → `crud.py` create_task 原样存。
**读取**：`crud.py:447`（`find_structured_open_dup` 的 `parent_task_id = ?`）、`verify.py:333`（`WHERE parent_task_id = ?` VERIFY 查找）。

agent 用短前缀建子任务 → 结构化查重 miss（重复建任务）+ VERIFY 父子关联 miss。系统自建的 VERIFY 任务 parent 是全量 UUID，不受影响；仅 agent 手动建子任务路径踩。修复：create_task 入库前 resolve parent_task_id。

---

## P2 — 理论问题 / 现状安全但脆弱

### P2-1 handoff task_id 清除

`services/handoff.py:272` `mark_reported` 的 `(task_id = ? OR task_id IS NULL)` 精确匹配。当前唯一带 task_id 的调用方 submit.py:569 传的是已归一化 ID，安全。但 `tools/misc_tools.py:837/867`（merge precondition 失败建 handoff）用裸 `params.task_id` 入库——短前缀 handoff 残留风险，目前无清除方按 task_id 精确查它，所以只是"脏数据"而非"功能 bug"。

### P2-2 已确认安全的对照项（排除项）

- `services/org.py:325 resolve_agent`：short_id 精确 / UUID 精确 / UUID 前缀三级解析——agent ID 域无此问题
- `services/turn_exit.py:_task_ref_matches/_ref_in_set`、`wait_contract.py:_ref_matches_sender`：前缀容忍
- `services/merge_proxy.py`：全程用 `task["id"]`（DB 全量）
- `services/attestation.py:931-950`（reviewer 证据门）：Python 侧双 canonical，兼容存量短前缀行
- legacy 工具 `approve_work/reject_work/report_completion`（misc_tools.py:1230-1307）：走 HandoffService 按 subordinate agent 解析，不做 task_id 匹配
- `get_task`（crud.py:327-335）：内部 resolve_task_id，容忍前缀——所有"先 get_task 再操作"的路径读侧安全

---

## 存量数据兼容评估（第 4 问）

**结论：需要一次性迁移，或查询侧双形态兼容。**

| 表 | 存量风险 | 现状 |
|----|---------|------|
| `tool_attestations.task_id` | 修复前 agent 传入的短前缀 / dashed UUID 行 | `get_valid_waiver`（590-599）、`count_waivers`（618-623）、`invalidate_valid_waivers`（651-658）用 canonical key 单形态 `task_id = ?` → **存量 waiver 行隐形**：任务表现为"从未被豁免"（需重新 waive），且 lifetime cap（MAX_WAIVERS_PER_TASK=2）对存量行计数不到、可绕过。reviewer 证据门（931-950）已 Python 侧兼容，无需迁移 |
| `tasks.depends_on` | agent 建的短前缀依赖 | 无兼容（见 P0-4） |
| `agent_waits.ref`（kind='task'） | 短前缀 | 无兼容（见 P1-4），TTL 到期自愈 |
| `verification_cases.verify_task_id` | waive 路径短前缀 | 无兼容（见 P1-5） |
| `inbox.reply_to` | 前缀 | 见 P0-5 |

**迁移脚本建议**（一次性，随启动 migration 或手动跑）：

```sql
-- 伪代码：需 Python 侧配合 resolve 短前缀
UPDATE tool_attestations
SET task_id = <canonical dash-stripped full UUID>
WHERE task_id IS NOT NULL
  AND (task_id LIKE '%-%' OR length(task_id) < 32);
```

注意 canonical 形态是 dash-stripped，而 `tasks.id` 是 dashed——迁移时短前缀行要经 `resolve_task_id` 解析再 dash-strip，不能只做字符串处理。替代方案：`get_valid_waiver/count_waivers/invalidate_valid_waivers` 三处 SQL 改 `replace(lower(task_id), '-', '') = ?`（stored 侧归一化），与 `require_task_id` 的 LIKE 双形态查询同思路，可免于迁移但每查询全表扫。

---

## 汇总表

| # | 位置 | 失败模式 | 可触发 | 风险 |
|---|------|---------|--------|------|
| P0-1 | tools/tasks/waive.py:167 | 精确比较，双形态都假阴性 | 是 | 高 |
| P0-2 | services/tasks/progress.py:86 | 未归一化，静默成功 | 是 | 高 |
| P0-3 | services/dispatch.py:176 + crud.py:512 | update_task 裸写，assignee 不换 | 是 | 高 |
| P0-4 | crud.py:184 + obligation.py:559/625 | depends_on 写入不归一、读取精确 | 是 | 高 |
| P0-5 | inbox.py:911/972 | reply_to 发送容忍/查询精确 | 是 | 高 |
| P1-1 | tools/tasks/admin.py:138 | 证据戳裸写 | 窄路径 | 中 |
| P1-2 | tools/tasks/waive.py:484 | 证据戳裸写 → close 连锁被拒 | 窄路径 | 中 |
| P1-3 | services/tasks/claim.py:219 | 未归一化，响亮报错 | 是 | 低 |
| P1-4 | wait_contract.py:235 + close.py:573 | wait ref 精确匹配 | 是 | 中 |
| P1-5 | tools/tasks/waive.py:236 + verify.py | verify_task_id 双形态 | 窄路径 | 中 |
| P1-6 | create.py:58 + crud.py:447/verify.py:333 | parent_task_id 双形态 | 窄路径 | 中 |
| P2-1 | misc_tools.py:837/867 | handoff 脏数据 | 理论 | 低 |

**系统性建议**：在 `TaskService` 层定一条硬规矩——"任何 `WHERE id = ?` / `task_id = ?` 的 SQL 之前必须经过 `require_task_id`/`resolve_task_id`"，并把 `update_task`、`update_progress`、`unclaim_task` 三个漏网方法补上首行归一化（一行改动消灭一类 bug）。发送/入库侧归一化（reply_to、depends_on、waits ref、parent_task_id）优于读取侧逐点容忍。
