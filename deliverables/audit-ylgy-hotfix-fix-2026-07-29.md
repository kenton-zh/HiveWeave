# TEST_YLGY Hotfix 审计修复报告（2026-07-29）

> 审计依据：`deliverables/ylgy-hotfix-forensics-2026-07-28.md`（前置独立核验） +  
> `deliverables/audit-ylgy-hotfix-forensics-2026-07-28.md`（三轮审计） +  
> `deliverables/test-ylgy-postmortem-2026-07-27.md`（P0 根因） +  
> `deliverables/audit-test-ylgy-report-2026-07-28.md`（增量1 审计）  
> 修复人：平台侧（一元 / WorkBuddy）  
> 纪律：全部落检测/机制层，禁止为此加提示词实例规则（符合提示词入账纪律）

---

## 一、修复前现状核查（fbcfbd4 已落部分）

逐行核对代码后确认，以下在 `fbcfbd4`（2026-07-28 23:05）已落地，**本次不重复改**：

| 项 | 状态 | 证据 |
|---|---|---|
| W1 effective path 公共化（5 个 git 操作改走 DB fallback） | ✅ 已落 | `git_worktree.py:1021` `_resolve_effective_worktree_path`，checkpoint/merge_by_branch/rollback/quarantine/delete/info 全部调用 |
| W1 notify 签名修复 | ✅ 已落 | `git_worktree.py:2883` `from_agent_id=, to_agent_id=, message=` |
| W1 heal race candidates（-b/-c/-d 后缀白名单） | ✅ 已落 | `git_worktree.py:2821` `_RELOCATION_SUFFIXES` |
| W2 剥图门三件套 | ✅ 已落 | `provider.py` `_IMAGES_OMITTED_NOTE` + `vision.py` 强制 `supports_images=True` + `test_text_only_model_image_gate.py` |
| close_task is-ancestor 配套硬门 | ✅ 已落 | `task.py:1466` `branch_tip_not_in_main` → rollback + raise |
| worktree_review 里程碑 uncovered hard reject | ✅ 已落 | `worktree_review.py:731` 分层执行（milestone=hard / other=warning） |
| P0-2 审查方证据硬闸（approve 前置 test_run） | ✅ 代码在 | `task_tools.py:1831` `find_reviewer_attestation` 硬拒 |

---

## 二、本次修复（4 项，全部机制层）

### 修复 1：reconcile `sqlite3.Row.get` 崩溃（P0-1 live bug）

**文件**：`services/git_worktree.py`（reconcile_worktrees ④ 段）

**根因**：`conn.execute_fetchall(...)` 返回 `aiosqlite.Row`（= `sqlite3.Row`），它**没有 `.get()` 方法**（只支持 `row["key"]` / `row.keys()`）。原代码 `row.get("evidence")` 抛 `AttributeError`，被外层 `except Exception as recon_err` 吞成 `"stranded task reconciliation failed: 'sqlite3.Row' object has no attribute 'get'"`。

**后果**：stranded closed-task 检测**全程失效**——启动对账永远跑不到 stranded 判定，P0-1 的"补救提交 stranded"这一类故障在启动时无法被发现。

**修复**：改用标准 cursor 模式 + `dict(r)` 转换（匹配代码库惯例 `dict(row)`），`.get()` 在 dict 上合法。

**同时扩展**：移除 `if not has_merge: continue` 提前跳过——无 merge fact 的 closed 任务（如 VERIFY 报告）也扫描其 branch tip 是否在 main。**有 merge fact 才重开 obligation**（避免给 docs-only/纯 VERIFY 任务造伪义务），无 merge fact 仅加入 `stranded_closed_tasks` 报告（可见性）。这覆盖审计窗口外发现的「Sage W1 VERIFY 报告 21d1697 stranded 在 hw/A015/work」第二例。

### 修复 2：`find_reviewer_attestation` 同源 `.get()` 崩溃（P0-2 P0 回归）

**文件**：`services/attestation.py:565` `find_reviewer_attestation`

**根因**：与修复 1 同源的 `sqlite3.Row.get()` 崩溃。`db.execute_fetchall(...)` 返回 Row，`row.get("kind")` 抛 AttributeError → 被 `except Exception: pass` 吞 → **永远返回 False**。

**后果**：P0-2 审查方证据硬闸（`task_tools.py:1842` `if rev_needed and not waived:`）因 `find_reviewer_attestation` 永远返回 False → **拦死所有代码任务的 approve**。这是 fbcfbd4 引入 P0-2 硬闸代码后埋下的 P0 回归——硬闸在，但查询路径是坏的。任何带 `reviewer_required_kinds` 的 policy（`ui_browser_e2e`/`generic_tests`/`coordinator_review`）都会被无差别拒绝。

**修复**：改用 cursor + `row["kind"] if "kind" in row.keys() else ""`（不再依赖 `.get()`）。

### 修复 3：close_task merge obligation 安全网（P0-1 变体）

**文件**：`services/task.py` `_enforce_merge_on_close`

**根因**：审计窗口外 live 发现——8b17e12d 于 23:07:09 已 merge（78ec808 落 main）+ closed，但 merge obligation 81b43baa 仍于 23:17:40 / 23:22:43 两次升级。CEO 不得不 23:23:02 用 `cancel_task` 强行清账。**merge 在 git 层闭环、在 obligation 账本层不闭环**。

机制缺口：merge obligation 的 fulfill 只在 merge 工具包装层（`misc_tools.py:686`）调用。若 merge 经 merge_proxy / service 直走 / fulfill 静默失败，obligation 不会清。close_task 此前不兜底。

**修复**：在 `_enforce_merge_on_close` 的 `evidence_has_merge_fact` 分支 return 前补一刀 `ObligationLedger().fulfill(project_id, tid, "merge")` 作为安全网。merge 工具的 fulfill 仍是主路径；这是 backstop——只要任务真 close（merge fact + tip 在 main），obligation 必清，不再需要 cancel_task 强清。

### 修复 4：bash dev-server 自动注册（P0-3 增量2）

**文件**：`tools/bash.py`

**根因**：agent 自行 bash 起 dev server（`npm run dev` / `vite` / `bun dev` 等）未注册到 `process_registry`。`stop_processes_for_worktree` 只杀注册进程 → bash 起的 dev server 杀不到 → 锁 node_modules → WinError 32 → worktree 删不净 → husk → `-b` 级联。这是 P0-3 路径分裂的**根因第一环**（`start_dev_server` 注册了，bash 没有）。

**修复**：在 `execute_bash` 加 `_detect_dev_server_command(command)` 检测器——识别长驻 dev server 命令（`vite`/`npx vite`/`npm|pnpm|yarn run? dev|start`/`bun run? dev|start`/`next dev`/`nuxt dev`/`nodemon`），排除阻塞动词（`build`/`test`/`lint`/`install`）。命中后路由到 `_run_registered_dev_server`：经 `spawn_project_process` 非阻塞 spawn + `register(ProcessRecord)` 注册到 worktree cwd，立即返回「dev server started」而非阻塞到超时 orphan。

**保守边界**：`node server.js` 不在检测范围（`node` 太宽，无法区分长驻 vs 一次性脚本）——这种场景 agent 应显式用 `start_dev_server`。检测器对 19 个用例（11 正例 + 8 反例）全部通过。

---

## 三、W2 端到端硬证补证计划（审计第一优先欠账）

审计结论：W2 剥图门代码+单测在（fbcfbd4），但**端到端 dogfood 仍缺一刀硬证**——没人做过「强制 text-only 主模型 + 注入截图 → 抓 `_IMAGES_OMITTED_NOTE` 回执」的受控实验。渡口 ee0f8e87 测的是 `look_at_image` 的 vision 槽 fallback，与主模型剥图门（provider.py）是两条路径，不能互证。

### 受控实验步骤（小申终端执行）

**前置**：后端跑在 4000，有一个 activate 的项目。

1. **配置一个 text-only 模型**：Settings 里加一个模型，`supports_images=false`（或不勾选支持图片），设为某 executor 的 primary。
2. **触发主模型收到截图**：让该 executor agent 跑一个会调 browse screenshot 的任务（或直接 chat 让它 browse 一个页面截图）。
3. **抓证**：
   - 后端日志 `tasks/<最新>.output` grep `_IMAGES_OMITTED_NOTE` 或 `supports_images`；
   - 或在 `provider.py` 的剥图分支临时加一行 `log.info("image_strip_e2e_proof", dropped=n)` 后看日志。
4. **断言**：主模型请求体里**无 images 字段**，user/tool content 里出现 `_IMAGES_OMITTED_NOTE` 文案（"已省略 N 张截图…模型配置 supports_images=false"）。
5. **对照**：同一个截图发给 `supports_images=true` 的模型 → 请求体保留 images。两条路径对比即证剥图门生效。

**关键**：必须测**主模型路径**（provider.py 的 `_normalize_messages_with_images` / handler 的 `_user_content_blocks`），`look_at_image` 的 vision 槽不算（那是 vision.py 强制 `supports_images=True` 的另一条线）。

---

## 四、测试清单（小申终端跑，勿在沙箱跑）

⚠️ **禁止在 WorkBuddy 沙箱跑 HiveWeave pytest**（safe-delete fail-closed 与 git 对象操作交互异常，曾致 .git pack 全丢）。

```bash
cd apps/hiveweave-py

# 1. 本次修复回归测试（新增）
timeout 120 uv run pytest tests/test_audit_ylgy_hotfix_fixes.py -q

# 2. fbcfbd4 已落的剥图门单测
timeout 120 uv run pytest tests/test_text_only_model_image_gate.py -q

# 3. fbcfbd4 已落的 -b 绑定稳定化单测
timeout 120 uv run pytest tests/test_worktree_relocate_binding.py -q

# 4. 全量（注意：worktree/merge 类在 Windows 本机有 ~36 项预存在失败，
#    invalid reference，与 Linux 云环境结论不同——只看本次新增测试是否绿）
timeout 120 uv run pytest tests/ -q
```

**新增测试文件**：`tests/test_audit_ylgy_hotfix_fixes.py`（7 个用例）覆盖：
- reconcile Row.get 崩溃修复 + VERIFY stranded 可见性扩展
- find_reviewer_attestation 返回 True/False 正确性（不再永远 False）
- close_task merge obligation 安全网 fulfill 调用
- dev-server 检测器正例/反例/端口提取/`&` 剥离

---

## 五、变更文件清单

| 文件 | 改动 |
|---|---|
| `services/git_worktree.py` | reconcile ④ 段：`dict(r)` 转换修复 Row.get 崩溃；移除 has_merge 提前跳过，VERIFY stranded 可见性 |
| `services/attestation.py` | `find_reviewer_attestation`：cursor + `row["kind"]` 修复 Row.get 崩溃（P0-2 回归） |
| `services/task.py` | `_enforce_merge_on_close`：merge fact 分支 return 前补 fulfill 安全网 |
| `tools/bash.py` | `_detect_dev_server_command` + `_run_registered_dev_server` + execute_bash 路由 |
| `tests/test_audit_ylgy_hotfix_fixes.py` | 新增 7 个回归用例 |

---

## 六、未处理项与建议

1. **WinError 32 句柄另有所属**：修复 4 让 bash 起的 dev server 可注册可杀，但审计指出「空壳目录 rename/删除仍被占——句柄另有所属（shell cwd / 杀软 / 沙箱待查）」。这超出代码层，需运维侧排查（shell cwd 锁定目录、杀软扫描锁）。建议：reconcile 对 rename 失败的 husk 记 `agent_health` 黄框而非静默。
2. **W2 端到端硬证**：见第三节计划，需小申终端执行受控实验。
3. **提示词零增量**：本次修复全部落机制层，未往 coordinator/executor 提示词加任何实例规则（符合提示词入账纪律三层分流）。
