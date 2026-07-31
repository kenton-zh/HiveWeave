# TEST6_ylgy 平台问题汇报（2026-07-30）

运行窗口：2026-07-29 22:23 → 23:55。项目最终闭环（8/8 task closed，E2E 15/16 + vitest 198 pass），但过程暴露 3 个平台层问题 + 1 个运维问题。本文档只列**本次运行仍有证据、且当前代码仍未根治**的问题；07-29 白天已修（e6b1607 / 286d0d4）的门禁问题不重复列入。

---

## P1 — main 被生成物污染 → merge 硬拒 → 跨 Agent 人工 stash 往返

**证据链**
- 23:21:34 → 23:22:45，`git_worktree_merge` 连续 3 次失败：`MAIN has uncommitted changes`。
- CEO 被迫 ask A003 到项目根 `git stash`，再重试 merge，全程 4 个回合约 90 秒。
- 运行结束至今（07-30），main 仍 dirty：` M tsconfig.tsbuildinfo`、`?? test_output_full.json`、`?? test_output_verify.json`；`stash@{0}: WIP on main: pre-merge-checkpoint` 残留未 pop。

**根因（两层）**
1. 生成物可进入 git 跟踪集：平台 `.gitignore` 模板（`service_create.py:ensure_git_repo`）不含 `*.tsbuildinfo` / `test_output*.json`；checkpoint 剥离清单 `GENERATED_FILES` 只含 lockfile。tsc/vite build/vitest 在 main 检出上跑（VERIFY 取证、E2E 起服务都会）必然弄脏 main。
2. merge 的 dirty 硬拒（`service_merge.py` → `_target_worktree_is_dirty`）没有自动 remediation：明明全是可再生文件（tsbuildinfo / 测试输出），也一律拒绝，把清理责任推给 Agent 协商。

**修复方案**
- A. `.gitignore` 模板补 `*.tsbuildinfo`、`test_output*.json`、`test-results/`、`playwright-report/`；`ensure_git_repo` 对已存在的 `.gitignore` 做**缺失行幂等追加**（老项目也能补齐）。
- B. 新增可再生文件清单 `REGENERABLE_PATTERNS`（tsbuildinfo / test_output*.json）：checkpoint 提交前剥离；已被跟踪的 `git rm --cached` 脱跟踪。
- C. merge dirty 检查分流：dirty 路径**全部**命中可再生清单 → 自动 `git checkout -- <paths>` 恢复后继续 merge；任一非可再生 → 维持硬拒。

## P2 — 任务 closed 后 worktree/分支不回收

**证据链**
- 8 个任务全部 closed 后，`git worktree list` 仍挂 A003-b / A004 / A005 / A006 / A007-b 五个 worktree，`hw/A004/work` 分支未合并（含 3 张 evidence PNG）。
- 另有孤儿分支 `hw/A013/work`、`hw/A016/work`（对应 Agent 在本项目 DB 中不存在，属上一代项目残留，且未合并）。
- 回收现状：merge 成功时 `_assignee_has_open_tasks` 为真则跳过清理（正确设计）；但**之后任务全部 closed 时没有任何钩子重新触发回收**。`reconcile_worktrees` 只在启动/supervisor heal 时跑。

**根因**
回收触发点缺失：close_task 是任务生命周期的终点，但不触发 worktree GC；merge 时的跳过是永久跳过。

**修复方案**
- `close_task` 末尾增加 best-effort GC 钩子（fail-open）：assignee 有写树资格、名下无 in-flight 任务（沿用 `_IN_FLIGHT_AFTER_MERGE_STATUSES` 口径）→ 调 `GitWorktreeService.delete()`。删除安全链已有保障：`git branch -d` 拒删未合并分支并透出 `preserved_branch`（A004 这种带未合并 evidence 的分支会被保留并报告，不丢证据）。

## P3 — 运行期后端代码陈旧，门禁修复不生效

**证据链**
- 23:24:21 CEO approve VERIFY 任务 d89a564a 被 reviewer 门禁拒绝（"Review需要test_run"）——但 assignee A004 持有 23:23 新鲜 `test_run` attestation（749c674a，exit 0），而 286d0d4（07-29 17:40）已提交 assignee 消耗路径（`find_reviewer_attestation(consume_agent_ids=...)`）。
- 23:24:55 CEO **自己 waive 又自己 approve** 成功——286d0d4 的 waived_by≠approver 硬门也未生效。
- 结论：运行后端加载的是 17:40 之前的旧代码。

**根因**
E2E 纪律（每次测试重启后端）未被遵守；平台自身无代码版本可见性，无法从日志确认运行的是哪个 commit。

**修复方案**
- 运维层：重申并执行 E2E 协议（测试前必重启后端/前端）。
- 平台层（轻量）：后端启动日志输出当前 git short hash（读 `.git/HEAD` 解析，失败静默），让"跑的是旧代码"一眼可辨。

---

## 修复清单（本次执行）

| # | 问题 | 改动点 |
|---|------|--------|
| F1 | P1-A | `service_create.py`：gitignore 模板补生成物 + 已存在文件幂等追加缺失行 |
| F2 | P1-B | `constants.py` 新增 `REGENERABLE_PATTERNS`；checkpoint 剥离 + 脱跟踪 |
| F3 | P1-C | `service_merge.py`：dirty 全是可再生 → 自动恢复后 merge；否则维持硬拒 |
| F4 | P2 | `tasks/close.py`：close 后 best-effort worktree GC |
| F5 | P3 | `main.py` lifespan：启动日志带 git short hash |

## 不修复（记录在案）

- `hw/A013/A016` 孤儿未合并分支：删除安全链原则不变——未合并分支只报告不强删，由人决定。
- evidence PNG 只存在于 Agent 分支（A004）：证据归档到 `.hiveweave/` 共享存储是独立改进项，本次不做。
- A003→A003-b / A007→A007-b 重定位：自愈已生效（目录锁定时挂 `-b` 后缀），dev-server 进程注册 + `stop_processes_for_worktree` 已在位。

---

## 审计轮（2026-07-30，子代理对抗性审计）

实证脚本 `tmp_audit_test6_adversarial.py`（41 项检查全 PASS）。结论：F1/F4/F5 实证通过；F3 还原语义是合理取舍（丢弃的仅是可再生内容）。发现 1 个阻塞项 + 2 个风险，**均已修复**：

| # | 审计发现 | 处置 |
|---|---------|------|
| A1（阻塞） | F3 只落在 `merge()`；`merge_by_branch()`（`git_worktree_merge` 工具 5 条路径中 4 条的主路径）仍硬拒，TEST6 失败场景可复发 | 分流块抽成共享 helper `merge_support.restore_regenerable_dirt_or_reject()`，两条合并路径统一走它 |
| A2（风险） | staged-new（`git add` 未提交）的可再生文件不在 HEAD，`checkout HEAD --` 必然失败，merge 仍被卡 | helper 内用 `ls-tree HEAD` 分流：在 HEAD 的走 `checkout HEAD --`；不在 HEAD 的走 `rm --cached` 脱暂存（文件留盘，info/exclude 保证不再进 status） |
| A3（风险） | checkpoint 对被剥的再生文件复用 ignored-files 警告通道，文案建议 `git add -f`——照做下个 checkpoint 再被剥，死循环建议 | 再生文件从通用 ignored 警告中剔除，单独发 `regen_note`：说明「永不随提交走」，证据场景指引「改名加 short_id 前缀后重新提交」 |

验证：`tmp_validate_test6_fixes.py` 增补 F2msg/F3c/F3d 场景（含 `merge_by_branch` 主路径），全部 PASS；`pytest tests/` 942 passed；mypy 无新增错误（存量 21 条均为包拆分重构的 mixin attr-defined 模式噪声）。
