# TEST16 运行痕迹逆向分析：HiveWeave 平台 BUG 与设计问题报告

- 分析对象：D:\PC_AI\Project\TEST16（2026-07-25 15:50–16:45 真实运行，签到排行榜 Demo）
- 数据来源：`.hiveweave/data.db`（tasks/task_events/agents/inbox/chat_messages/work_logs/agent_waits）、git 分支与提交、后端日志 `tasks/backend-20260725-154945.output`
- 结论先行：项目 40 分钟闭环交付（主干流程 work），但运行中踩出 **2 个 P0、3 个 P1、5 个 P2 程序 BUG** 和 **4 个设计问题**。其中 P0-1 直接违反平台自己的「禁止用文案猜意图」HARD RULE。

---

## 一、运行全景还原

5 个 agent：归零(CEO/A001)、天线(HR/A002)、云岫(前端架构师/coordinator/A003)、潮汐(签到排行工程师/executor/A004)、阿波罗(测试工程师/executor/A005)。11 个任务全部 closed，CEO 报告 GO。

但痕迹显示过程并不干净：

| 时间 | 事件 | 异常 |
|------|------|------|
| 15:54 | A004 hire，worktree 创建失败 | `worktree_error="stale path locked and could not be cleared"`，但 workspace_path 仍写入该路径 |
| 15:56–16:04 | 潮汐在 A004 目录工作，`vite build` 失败 | bash 工具只返回首行输出，agent 盲试 6 次（`2>&1`、`--no-color`、`2>err.txt` 全试遍） |
| 16:03 | 阿波罗 E2E 任务 blocked | QA 的 cwd 被限制在 A005 worktree，无法跨目录启动 A004 的代码 |
| 16:35 | 云岫收到 [TASK APPROVED] | 消息让它"等你的 coordinator merge"——云岫自己就是 coordinator，陷入身份错乱死等 |
| 16:55–16:58 | 潮汐 3 任务 submitted → 2 个被 archive | 云岫报告：「验收标准中的中文含 `/` 被平台误解析为文件路径，导致无法 approve」→ 取消重建 |
| 16:44 | 全部任务 closed，CEO 报 GO | A005 的 E2E 证据分支（7 张截图）**从未合并到 main**；最后一条 merge wait `cleared_at=NULL` 永久悬挂 |
| 事后 | git 状态 | cbeaf79（潮汐的实现 checkpoint）是**孤儿 commit**，不属于任何分支；A004 worktree 只剩 node_modules 空壳 |

---

## 二、程序 BUG（按严重度分级）

### P0-1　验收标准里的中文「/」被误判为文件路径，approve 被平台硬拒

**证据**：task_events 两条 `task.archived`，reason =「验收标准中含 '/' 被平台误解析为文件路径，导致无法 approve。重建。」云岫被迫 cancel 两个已 submitted 的任务再重建。

**根因**（确定）：`services/worktree_review.py:441-496`

```python
_PATH_TOKEN_RE = re.compile(
    r"(?:(?:\.?/?[\w.-]+(?:/[\w.-]+)+)"
    r"|(?:[\w.-]+\.(?:" + "|".join(...) + r")))"
)
```

Python `re` 对 str 默认 Unicode，`\w` 匹配中文字符 → 「签到/排行榜」「流程/数据」这类验收标准文本被提取为"路径 token"，随后 `check_evidence_verifiable`（534-615 行）要求该路径存在于磁盘，不存在则在 `tools/task_tools.py:1469-1478` 的 approve 链中 **deny**。

**讽刺点**：文件 431-433 行注释自己写着 `Never scan free-text for intent keywords`，而这段正则恰恰在做平台 HARD RULE 明令禁止的事——用正则扫描自由文本推断结构。

**修复方案**：
1. `_PATH_TOKEN_RE` 的字符类从 `[\w.-]` 改为 ASCII 限定 `[A-Za-z0-9_.-]`，中文文本不再命中；
2. 增加前缀白名单：仅当 token 以 `src/`、`docs/`、`apps/`、`tests/` 等已知根目录开头，或含已知扩展名时才视为路径引用；
3. 「路径不存在」从硬 deny 降级为 approve 回执中的 warning（证据充分性由 reviewer 判断，符合 CLAUDE.md「不做提交 attestation 硬闸」的设计原则）。

---

### P0-2　worktree 创建失败后的连环失效：残留路径、重试死循环、孤儿 commit

**证据链**：
- A004 `worktree_error="Failed to create worktree: stale path locked and could not be cleared"`，但 `workspace_path` 仍指向该路径；
- 日志中 `org.update_agent fields=["worktree_error"]` 在 15:56–16:03 反复出现 **10+ 次**（每轮 chat 懒创建重试都失败、重盖章）；
- `git worktree list` 无 A004，但潮汐在其中完成了全部开发并提交 cbeaf79；
- `git branch --contains cbeaf79` 为空 → **孤儿 commit**；
- A004 目录最终只剩 `node_modules` 空壳；`git_worktree.reconcile` 跑 3 次全部 `pruned:0, removed_dirs:0`，识别不了这个空壳。

**根因**（确定，四处叠加）：
1. `services/git_worktree.py:577-585`：stale 路径清理失败（`_force_clear_path`，109-135 行：rmtree + rename-aside 在 Windows 文件占用下双双失败）→ 报错返回；
2. 失败路径**只写 worktree_error、从不清空 workspace_path**（`tools/org_tools.py:456-458`、`main.py:269-274`、`git_worktree.py:2124-2128`）→ 无效路径永久残留；
3. `agents/agent.py:1352` 懒创建的有效性检查要求 `(ws/".git").exists()`，空壳不满足 → 每轮重新 create → 同一 locked 路径再次失败 → 死循环；
4. `worktree_review.py:56` `agent_worktree_path` 只要求目录非空（`any(p.iterdir())`，node_modules 残留即通过）→ 空壳被当作有效 worktree，approve/merge 流程继续引用它。

**修复方案**：
1. create 失败时**清空 workspace_path**（三条失败路径统一），让懒创建下轮走"无路径→重新分配新目录"分支，而不是反复撞同一 locked 路径；
2. stale 清理失败时换目录名（`A004` → `A004-b`）而不是直接失败——node_modules 文件锁是 Windows 常态，重试无意义；
3. `agent_worktree_path` 的有效性口径与 `agent.py:1352` 对齐：必须含 `.git` 才算有效 worktree；
4. `reconcile_worktrees` 增加空壳识别：目录存在但无 `.git` 且不在 `git worktree list` → 报告/回收；
5. `git_worktree_checkpoint` 在提交前校验当前目录是否为登记 worktree，防止产生孤儿 commit。

---

### P1-1　[TASK APPROVED] 通知模板不分角色，coordinator 收到后死等

**证据**：云岫 work_log：「[waiting] Phase 0.5 已 approve。等待 coordinator git_worktree_merge 合并 worktree A003 到 main。」——它自己就是 coordinator，在等一个不存在的"自己的 coordinator"。最终靠 CEO 兜底 merge 才解开。

**根因**（确定）：`tools/task_tools.py:1536-1555`，approve 通知文案对所有角色统一写死 `Wait for your coordinator to git_worktree_merge your worktree`，仅判断 `assignee_id != agent_id`，不读 assignee 的角色/family。

**修复方案**：按 assignee 的 role family 分支文案：
- executor → 「等待你的 coordinator merge」；
- coordinator → 「请自行 git_worktree_merge（或等待 CEO 行使 MERGE 兜底）」；
- CEO → 不需要 merge 指引。
同时在模板中附上 merge 责任人的具体名字（org parent），消除"以为别人会 merge"的模糊地带。

---

### P1-2　trigger 上下文同一毫秒重复落库 3 次

**证据**：潮汐的 chat_messages 中 3 条完全相同的 user 消息（Goals Workbook + Pending Tasks），created_at 完全相同（1784966181856）。

**根因**（确定机制、竞态触发）：三层去重全部是 check-then-act，无原子保证：
1. `agents/trigger.py:71,442-445` `_last_goals_msg_version` 模块级 dict 先读后写无锁；
2. `services/team_chat.py:87-117` `check_and_mark` 先 SELECT 再 INSERT，无唯一约束（docstring 自述修"P2 三连发"，但只防住顺序重试防不住并发）；
3. `services/charter.py:249-263` goals dirty 读版本与写版本之间存在竞态，并发的 build_trigger_context 都判定 dirty 都注入。
而 300ms 合并窗（`agents/agent.py:3395-3434`）只覆盖 busy 入队路径，非 busy 直发路径完全暴露。

**修复方案**：
1. `team_chat_dedupe` 表加 `UNIQUE(agent_id, from_agent_id, content_hash, window_bucket)`，改 `INSERT OR IGNORE`，以数据库约束兜底并发；
2. goals 注入版本号用 DB 原子 bump（`UPDATE ... SET goals_msg_version=? WHERE agent_id=? AND goals_msg_version<?`）替代内存 dict；
3. 直发路径也过一遍 dedupe 再落库。

---

### P1-3　项目闭环闸门不校验 merge 落地：任务全 closed，证据分支未合并

**证据**：
- `git log main..hw/A005/work` → 96a91c1「E2E验收证据：7张截图」**未合并**；
- main 上 `evidence/` 只有 2 张图（潮汐自己截的），阿波罗的 7 张正式验收证据不在 main；
- agent_waits 最后一条（潮汐等 merge）`cleared_at=NULL`，项目结束后仍悬挂；
- CEO 最终报告却写「E2E验证通过，证据截图已保存」。

**根因**（设计缺口）：CLAUDE.md 定义「approved=95 仍须 merge+VERIFY，100 仅属于 closed」，但 closed 转移**没有硬门校验 assignee 分支已合入 main**（ahead=0 或 evidence 文件在 main 存在）。VERIFY 任务 approve 后直接 closed，merge 全靠 agent 自觉。

**修复方案**：
1. 任务 closed 前校验：该任务关联分支相对 main `ahead>0` 且含未合并文件 → 拒绝 closed，自动 spawn merge 义务给 MERGE capability 持有者；
2. VERIFY 任务 closed 时校验 evidence 路径在 main 上存在（而不只是 worktree）；
3. 项目级「全部 closed」报告前跑一遍 `git log main..{各分支} --oneline`，非空则禁止 GO 结论。

---

### P2-1　bash 工具输出截断：构建错误只剩首行，agent 盲试

**证据**：潮汐原话：「vite build 报 exit code 1，错误信息被截断（只显示 `vite v6.4.3 building for production...`），尝试了重定向到文件、--no-color 参数等方式均无法捕获完整错误信息」。16:01:40–16:04:10 同一命令变体重试 6 次，浪费约 2.5 分钟 + 大量 token。doom loop 熔断未触发（参数不完全相同即绕过）。

**修复方案**：
1. bash 工具非零退出时返回 stdout+stderr 各自 tail 4KB（而不是截断到首行）；Windows 下注意 vite 等工具的 ANSI 控制字符剥离；
2. doom loop 对 bash 类命令按"归一化命令前缀"（去重定向/管道/flags）判重，防止改参数绕过；
3. 工具描述里注明当前 shell 类型（cmd / git bash），避免 agent 混用 `rm`/`del`（本次也踩了：删除临时文件失败）。

### P2-2　parent_task_id 格式不一致
前 5 个任务存 8 位短前缀（`bb041dd2`），后续 VERIFY 任务存完整 UUID。树遍历/统计查询按等值匹配会漏。**修复**：写入处统一归一为完整 UUID，存量数据一次性迁移。

### P2-3　cancelled 任务 progress 残留 90
取消的任务 progress 停在 submitted 的 90，UI 展示困惑。**修复**：cancel 转移时 progress 置 0 或引入负值语义。

### P2-4　task_events.actor_id 大面积 NULL
running/submitted/reviewing/approved 事件 actor 全为 None，审计无法追溯"谁批的"。**修复**：状态转移写事件时透传调用者 agent_id。

### P2-5　list_files maxdepth≤3 限制不透明
云岫 `pipeline.args_invalid` 报错才知道限制。工具描述应写清取值范围（小事，但每个 agent 都会踩一次）。

---

## 三、设计问题

### D1　E2E 验收的 worktree 隔离悖论
QA（A005）无法跨 worktree 启动 executor（A004）的代码做验收，只能 blocked 空转等 merge 到 main（本项目空转约 40 分钟）。而 E2E 任务却与开发任务**同时派发**。
**建议**：E2E/验收类任务声明 `depends_on=merge(<task_id>)`，由平台在 merge 落地事件后再唤醒 QA；或者给 QA 一个共享 staging worktree（main 的只读 checkout + 可跑 dev server）。

### D2　merge 责任"三重等待"
executor 等 coordinator merge、coordinator 等"自己的 coordinator"（受误导文案影响）、CEO 有 MERGE 兜底但不主动。本项目 3 次 merge 实际都由 CEO 完成。
**建议**：merge 义务结构化——approve 落账时直接在 reviewer（或其 MERGE 祖先）的义务账本中写入 merge obligation，靠 wait contract 唤醒，而非文案提示。

### D3　husk worktree 上的 merge 绕过分支语义
A004 无登记分支，其内容最终以文件拷贝方式进 main（801836c ≈ cbeaf79 + 2 个 md 文件），cbeaf79 成孤儿。merge 的"分支已 approved、已 checkpoint"前置校验在无分支场景下整体失效。
**建议**：merge 前置校验「源必须是登记 worktree + 有分支 + ahead>0」，不满足则拒绝并触发 worktree 修复流程（与 P0-2 联动）。

### D4　doom loop 防护对 bash 无效
默认熔断按"同工具+同参数"判重，bash 命令改个 flag 就绕过。本次 vite build 盲试 6 次未触发任何防护。
**建议**：见 P2-1 修复 2。

---

## 四、修复优先级与顺序建议

| 顺序 | 项 | 理由 |
|------|----|------|
| 1 | P0-1 路径误判（worktree_review.py） | 每个中文项目都会踩，approve 主链路被硬拒，改动小（正则字符类 + 降级 warning） |
| 2 | P0-2 worktree 失败连环（4 处） | 数据腐蚀级：孤儿 commit + 无效路径残留 + 每轮重试烧 token |
| 3 | P1-3 closed 不校验 merge | 交付完整性问题：报告 GO 但证据不在 main，信任级缺陷 |
| 4 | P1-1 approve 通知分角色 | 文案修复，半小时工作量，消除 coordinator 死等 |
| 5 | P1-2 dedupe 原子化 | DB 约束兜底，一次迁移 + 两处改写 |
| 6 | P2-1 bash 输出完整返回 | 直接省 token、省时间，agent 体验改善最大 |
| 7 | P2-2 ~ P2-5 | 顺手修 |
| 8 | D1 ~ D3 | 涉及流程变更，建议单独评审后落地 |

---

## 附：本次验证过的平台健壮点（不是无脑黑）

- 无 streaming 僵尸消息（自愈机制 work）；
- inbox 134 条全部已读，无积压；
- TurnResult 出口闸门、UNREPLIED_ASKS 硬门按设计工作（潮汐确实被拦过并最终合规退出）；
- wait contract 的 wake/clear 闭环除最后一条外全部正常；
- VERIFY spawn → QA 独立验证 → CEO 终审链路完整走通。
