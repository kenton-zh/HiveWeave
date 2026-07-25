# TEST18 错误排查报告 · 独立审查

审查日期：2026-07-25 ｜ 审查人：一元（WorkBuddy）
审查方法：对照 HiveWeave 源码 + TEST18 项目 DB（`D:\PC_AI\Project\TEST18\.hiveweave\data.db`）+ 后端日志（`tasks/backend-20260725-*.output`）逐条实证。

---

## 总裁定

| 报告条目 | 裁定 | 一句话结论 |
|---|---|---|
| 问题 1 依赖链断裂 | **事实属实，结论错误** | 不会"永远卡死"：有三层兜底。但报告摸到了真问题的边缘 |
| 问题 2 blocked 任务上设 wait | **属实** | 时序与机制均验证；影响有限（30min TTL 兜底），且报告漏了同族两个漏洞 |
| 问题 3 A015-b fallback | **属实** | 日志实证（23:31:22），DB 路径已正确写为 A015-b |
| 问题 4 Robert vite 失败 | **属实，归因不精确** | D4 计数器只挂在 `bash` 工具，`start_dev_server` 不接入 |
| 修复方向一（解析 blocked_reason 提取 ID） | **不可采纳** | 直接违反 CLAUDE.md「禁止用文案猜意图」HARD RULE |
| 修复方向二（block_task 加结构化参数） | **已存在** | `depends_on_task_id` 服务层+工具层都有。真正缺口在提示词 |

---

## 问题 1：手动 block + 任务复制 → 依赖链断裂

### 报告事实链 —— 属实（DB 实证）

```
a19acb59  status=blocked  assignee=云岫(A013)  depends_on=NULL
          blocked_reason="dependency:Robert(e42acfc4)正在执行集成联调E2E测试，等待完成"
          wait_kind="dependency"（_infer_wait_kind 从前缀推断）
e42acfc4  status=running  assignee=Robert(A016)  creator=云岫  depends_on=NULL
7dde3e7e  Phase 5  status=created  assignee=NULL  depends_on=["a19acb59-b99e-..."]
```

`reconcile_blocked_tasks`（services/task.py:887）确实只看 `wait_kind+wake_at` 和 `depends_on` JSON，**不解析 blocked_reason 文本** ✓

### 报告结论 —— 两处错误

**错误 1："没有任何机制自动 unblock a19acb59"。**

`_wake_dependent_tasks`（task.py:790，在 `review_task` approve 和 `close_task` 两处触发）有一条刻意保留的弱匹配路径（task.py:833-835）：

```python
if completed_task_id not in deps and not (
    reason_l.startswith("dependency:") and mentions   # mentions = 完整id或前8位出现在 reason 里
):
    continue
```

云岫的 `blocked_reason` 以 `dependency:` 开头且含 `e42acfc4`（前 8 位），**恰好命中**。e42acfc4 一旦被 approve/close，a19acb59 会被自动 unblock + 给云岫发 `[DEPENDENCY MET]` + trigger。这是 TEST11 审计（#5-L2/H3）后刻意保留的折衷。

**错误 2："Phase 5 永远等不到依赖满足"。**

Phase 5 是 `created` 且无 assignee——它本来就没有"等待"机制（reconcile/wake 只扫 `blocked` 任务），靠 coordinator dispatch 推进。而且 `start_task` 对 `depends_on` **没有硬门**（只有 slice contract_json 的 ready gate），depends_on 对普通任务是软约束。另外 blocked 超 `BLOCKED_STALE_MS` 还有 game_time 的 stale nudge + 升级兜底。

### 报告摸到了但没挖到底的真问题

1. **自愈路径靠"文案恰好合规"，设计脆弱**。云岫之所以能被救，是因为 executor 提示词（prompts/executor.py:377）教了 `blockedReason="dependency:…"` 格式。换个写法（"等 Robert 的新 Phase 4 完成"）就不自愈。把系统正确性押在 LLM 文案习惯上，不是稳健设计。
2. **真正的卡死路径报告没找到**：如果云岫按 dedup 提示"正确"处置——`cancel_task(a19acb59)`——`archive_task`（task.py:1490）**不处理 reverse dependents**。Phase 5 的 `depends_on` 将永远指向一个 cancelled 任务，而 `completed` 集合只含 `approved/closed`。这才是没有任何兜底的悬空。
3. **上游缺口：跨 assignee 重复任务检测盲区**。`create_task`（task_tools.py:723）和 `dispatch_task`（task_tools.py:542）的 dedup 都按 `assignee_id` 过滤。云岫自建 Phase 4 → 转派 Robert 建 Phase 4，assignee 不同，dedup 必然查不出。"任务复制"就是这么漏进来的。

---

## 问题 2：在已 blocked 任务上设 wait contract —— 属实

时序实证（task_events + agent_waits）：

```
23:33:28  CEO 设 wait on a19acb59（任务 claimed）→ 23:37:30 被清
          ✓ 云岫 start_task 触发 claimed→running，_clear_task_wait_contracts 生效（TEST17 修复工作正常）
23:46:20  云岫 block a19acb59（running→blocked）
23:46:43  CEO 又设 wait on a19acb59 —— 任务已 blocked ✓ 报告属实
23:46:51  CEO replace_waits 重设（当前 ACTIVE，expires 00:16:51）
```

机制确认：`replace_waits`（wait_contract.py:179）**不校验 ref 任务状态**；`wait_ttl_task_ms = 30min`（config.py:57）✓ "park 到 30 分钟超时"数字准确。

**影响比报告暗示的小**：e42acfc4 approve → `_wake_dependent_tasks` unblock a19acb59 → `_transition` → `_clear_task_wait_contracts` → CEO 被唤醒。链路正常推进时等不满 30 分钟；最坏情况也有 TTL 兜底醒。本质上是"最多 30 分钟的无谓停泊"，不是死锁。

**报告漏掉的同族漏洞（TEST17 修复盲区）**：
- `_transition_multi`（task.py:499，review_task **rework 打回路径**唯一使用者，task.py:1233）**没有调用** `_clear_task_wait_contracts`。等待审查结果的 waiter 在任务被打回 rework 时不会被唤醒，park 到 TTL。
- `archive_task`（cancel_task 通道，task.py:1535 直接 UPDATE，不走状态机）同样**不清** wait contracts。

---

## 问题 3：A015-b fallback —— 属实

日志实证（backend-20260725-225428.output）：

```
23:31:22  git_worktree.force_clear_failed  WinError 32（A015 目录被占用，rename-aside 也失败）
23:31:22  git_worktree.stale_path_fallback  A015 → A015-b
```

DB 里 A015（墨羽）的 `workspace_path` 已正确写为 `...\worktrees\A015-b`，`worktree_error=NULL` ✓ P0-2 修复在真实 Windows 环境工作正常。**注意这不是孤例**：TEST16 的 A004 同日 20:29:47 发生同样 fallback（vite/node 进程锁 node_modules 是 Windows 复发问题）。

**报告漏掉的 fallback 自身缺陷**：后缀循环（git_worktree.py:581-590）只检查 `not Path(alt).exists()`，不检查 alt 是否已是该 agent 的有效 worktree。若 A015 仍锁 + A015-b 有效时某调用方绕过 `ensure_executor_worktree` 直接 `create()`，会**增生 A015-c、A015-d**（agent 未提交文件 stranded 在 -b）。当前主路径（ensure 的 `/worktrees/{short_id}` 子串匹配）能覆盖 -b，严重度低，但循环里加一句 `_has_git(alt)` 复用即可幂等。

---

## 问题 4：Robert vite 启动失败 —— 属实，归因不精确

- `CWD_FAILURE_STREAK_THRESHOLD = 5`（bash.py:40）✓ 数字正确
- 机制是**纯 advisory**：达到阈值只在输出尾部追加提示文案，不阻断执行（bash.py:338 "Never blocks execution"）
- **归因精度**：`_update_cwd_failure_streak` 只挂在 `bash` 工具（bash.py:901/984）。Robert 若用 `start_dev_server` 工具起 vite，失败**不会**触发 D4 计数器
- 定性正确：这是 TEST18 项目开发问题，非平台 bug

---

## 修复方案评估

### 方向一：从 blocked_reason 自动提取任务 ID 写入 depends_on —— ❌ 不可采纳

直接违反 CLAUDE.md「语言无关：禁止用文案猜意图（HARD RULE）」——禁止用正则/关键词扫描自由文本推断意图。`_wake_dependent_tasks` 的 `dependency:` 前缀匹配已是 TEST11 审计后刻意收窄的折衷（H3：agent 名弱匹配曾因误 unblock CEO/HR 被移除），再加强文本解析等于回滚审计结论。

### 方向二：block_task 增加 blocked_by_task_id 参数 —— ⚠️ 已实现，缺口在别处

**该参数早已存在**：
- 服务层：`block_task(..., depends_on_task_id=...)`（task.py:699），merge 进 depends_on（task.py:731-755）
- 工具层：`depends_on_task_id` 参数 + 别名 `dependsOnTaskId/dependsOn`（task_tools.py:827-835）

云岫没用它的真正原因：**executor 提示词（prompts/executor.py:377-378）只教了 `blockedReason="dependency:…"` 文案格式，从没教 `dependsOnTaskId`**。coordinator 提示词同样没有。

**正解（一行提示词修复）**：把 executor.py:377 的教学改为优先 `dependsOnTaskId`（结构化），blockedReason 仅作人类可读说明。这比任何平台改动都治本。

---

## 报告漏掉的问题汇总（按严重度）

| # | 问题 | 位置 | 严重度 |
|---|---|---|---|
| L1 | cancel/archive 任务不处理 reverse dependents，depends_on 悬空（cancelled ∉ completed），返回文案无警告 | task.py:1490 `archive_task`；task_tools.py:1814 | **高**（真·无兜底卡死） |
| L2 | `_transition_multi`（rework 打回路径）不调 `_clear_task_wait_contracts`，waiter park 到 30min TTL | task.py:499 / 1233 | 中 |
| L3 | `archive_task` 不调 `_clear_task_wait_contracts` | task.py:1535 | 中 |
| L4 | 跨 assignee 重复任务检测盲区（dedup 按 assignee 过滤）→ 任务复制漏进 | task_tools.py:542 / 723 | 中 |
| L5 | stale_path_fallback 后缀循环不幂等（-b 有效时增生 -c/-d） | git_worktree.py:581-590 | 低 |
| L6 | `_wake_dependent_tasks` 的 `mentions` 用原始大小写匹配 + id 前 8 位理论碰撞 | task.py:827 | 低 |
| L7 | 提示词教文案格式、不教结构化参数 `dependsOnTaskId`（问题 1 的真正根因） | prompts/executor.py:377 | **高**（修复成本一行） |

## 建议行动（按投入产出比排序）

1. **改 prompts/executor.py:377**：block 教学从 `blockedReason="dependency:…"` 改为 `dependsOnTaskId="..."`（结构化优先）。一行，根治问题 1 的复发。
2. **`archive_task` 增加 reverse-dependents 检测**：查出 `depends_on` 含该任务的未完成任务，返回文案中警告（"以下任务依赖它，将悬空：…请 retarget"）；同时挂 `_clear_task_wait_contracts`。
3. **`_transition_multi` 尾部挂 `_clear_task_wait_contracts`**（与 `_transition` 对齐，TEST17 修复补全）。
4. dedup 检测增加"跨 assignee 同标题"警告（不硬拒，提示即可）。
5. fallback 循环加 `_has_git(alt)` 复用分支（一行幂等）。
