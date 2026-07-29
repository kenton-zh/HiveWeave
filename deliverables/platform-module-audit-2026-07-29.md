# HiveWeave 平台模块级审计报告

- **审计时间**: 2026-07-29
- **审计方式**: `code-review-and-quality` 五轴框架（正确性/可读性/架构/安全/性能），10 路并行只读审计 + mypy 验证轴 + pytest 全量回归 + TEST6 尸检修复交叉核对
- **审计范围**: `apps/hiveweave-py/src/hiveweave/` 全部后端模块（133 源文件）+ `apps/web/src/` 前端
- **参考基线**: CLAUDE.md 设计不变式、`deliverables/test6-ylgy-platform-analysis-2026-07-29.md` 已知问题清单
- **说明**: 所有发现均附文件:行号证据；标注「待人工确认」的项为代码逻辑链确定但外部行为无法静态证实者

---

## 〇、验证轴结果（实测，非静态推断）

### mypy `src/hiveweave/` — 21 errors / 10 files（exit 1）

值得单独点名（子代理未覆盖、我的py 独立捕获的**运行时 bug**）：

| 位置 | 错误 | 性质 |
|---|---|---|
| tools/misc_tools.py:826 | `InboxService.send_message` 被传入不存在的关键字 `project_id/sender_id/recipient_id/content` | 到达即 TypeError 的运行时 bug |
| tools/misc_tools.py:850 | 引用不存在的 `hiveweave.agents.agent.broadcast_agent_health` | 到达即 AttributeError |
| agents/agent.py:3128 | `await self._broadcast_agent_health(...)` 但该函数不返回值（非 coroutine） | 语义错误 |
| agents/agent.py:1864/1874 | `min()` 入参 `int|None`、list 赋值给 set | 类型边界不清 |
| services/merge_proxy.py:25/59 + game_time.py:1294 | dict key `Any|None` 传给 `str` | 潜在 None key |
| hooks/registry.py:82、tools/grep.py:265、tools/vision_tools.py:99、task_tools.py:4140、misc_tools.py:508、main.py:145 | 各类类型不匹配 | 类型卫生 |

### pytest — 910 passed / **6 failed**（45.7s）

6 个失败全部是同一根因：**P0-2 给 `WaiveAttestationParams` 加必填 `evidenceAttestationId` 时未同步更新测试**——
`test_dogfood_p2_fixes.py` 4 例（构造缺参即 ValidationError）+ `test_tool_schema_coverage.py` 2 例。
后果：waive 工具层（本审计 Required 高发区）**实际处于无测试保护状态**，且 CI 门面失守。

---

## 一、总览：模块健康度

| 模块 | 健康度 | Critical | 一句话总评 |
|---|---|---|---|
| agents/（生命周期） | 72/100 | 1 | 自愈体系完整有测试兜底；agent.py 膨胀到 4522 行，cancel 竞态+FYI 口径第三条裂缝 |
| llm/（流式） | 72/100 | 3 | OpenAI 主路径扎实；熔断 half-open 探针锁名存实亡、Gemini 多工具解析实锤 bug、测试在测平行实现 |
| tools/（工具系统） | 85/100 | 0 | 注册管线与注入面覆盖全面；fd 刻意泄漏、进程树杀死缺失、360 行不可达 legacy |
| services/任务审查链 | 76/100 | 2 | 状态机主干扎实；merge 成功路径「先删分支后扫 marker」数据丢失级顺序缺陷、自合并门可绕过 |
| services/组织权限 | 72/100 | 3 | 三条独立越权/竞态路径穿透「硬不变式」 |
| services/通信回合 | 72/100 | 1 | inbox 行映射漏字段致 reply-contract 确定性链路三处同时失效 |
| services/基础设施 | 78/100 | 1 | MCP stdio 把全量环境（含密钥）透传第三方子进程 |
| conversation+db+hooks | 72/100 | 1 | 压缩竞态丢消息可复现；conversation 层几乎零直接测试 |
| api/+realtime/ | 72/100 | 2 | HTTP 层与工具层防护不一致（.hiveweave 写穿透）、单模型端点泄 key、几乎无直接测试 |
| apps/web 前端 | 78/100 | 3 | phoenix API 误用 + 「停止」链路断裂 + hooks 顺序哑弹 |

**全平台 Critical 合计 17 项，Required 约 40 项。**

---

## 二、Critical 清单（17 项，按攻击面/失效模式聚类）

### A. 数据丢失/交付物损毁

1. **[git_worktree.py:1284→1312-1339] merge 成功后先删 worktree+分支，再扫冲突标记并 abort** — marker 命中时 `reset --hard ORIG_HEAD` 回滚 main，但分支已被 `branch -d` 删除，交付物只剩 reflog；且扫描范围是整个 main 工作区，预存 marker 样文本会误杀任意 merge。修复：扫描前移到 cleanup 前、范围收窄到本次 merge 文件、命中保留分支。
2. **[conversation/store.py:276-289] 压缩 C3 merge 启发式 `len(current_cache) > original_len` 在前次压缩改短 cache 后必然失效** — 连续两超阈 turn 双排压缩时，近期 turn 从 cache 静默丢失（cache/DB 分叉，重启才恢复）。修复：worker 执行时重读当前 cache 作输入，或校验尾消息同一性。
3. **[agents/agent.py:970-1043] `cancel()` 不持 `self._lock`，与 `chat()` setup 窗口竞态** — 窗口内 cancel 跳过取消、重置 IDLE，随后 chat 照跑 LLM（烧 token、状态错乱），watcher 被杀后需等下次入口才复活。修复：cancel 持锁或 chat 创建 task 前检查取消标志。

### B. 权限/安全边界穿透

4. **[services/org.py:153-164+207-230] 花名/岗位唯一性校验在 `_create_lock` 之外** — 并发 hire 读同一快照双双过检，重名/重复岗位/超编可入库，schema 无 UNIQUE 兜底。修复：校验+插入原子化，长期加唯一索引。
5. **[policy.py:220-230 + org_invariants.py + org_tools.py:196-201] role 字符串直接兑换 family** — HR 一次 hire 用 `role="ceo"`（或含「人力资源」）即可复制一个 CEO/HR，`validate_hire` 只保花名不保 role。修复：非 bootstrap 硬拒保留 role。
6. **[api/org.py:254-323] REST 变更端点（PATCH/PUT/dismiss/transfer）无 actor capability 门** — 可改 permissionType（executor→coordinator 即获 SOURCE_WRITE/BASH）；API key 未设时 agent 可 curl 本地 4000 自助提权。修复：全变更端点统一 MANAGE_ORG/STAFFING 硬门。
7. **[api/filesystem.py:120-134] `POST /api/filesystem/write` 可写穿 `.hiveweave/`（含 data.db）** — 工具层已有 .hiveweave 硬保护，HTTP 层缺失，可直接损毁 per-project DB。修复：write/read 拒绝首段为 .hiveweave 的路径。
8. **[api/models.py:739-745] `GET /api/llm-models/{id}` 返回完整 `api_key`** — list 端点脱敏、单查端点泄露。修复：HTTP 出口统一 mask。
9. **[services/mcp.py:177] MCP stdio 用 `{**os.environ, **self.env}` 把全量父进程环境（含 OPENCODE/ARK API key）透传第三方 MCP 子进程** — 凭证外泄通道，与同仓库闹钟脚本的白名单过滤（game_time.py:23-31）姿态自相矛盾。修复：复用 `_SAFE_ENV_KEYS` 白名单。
10. **[llm/streamer.py:1566-1570 + provider.py:548-555/977-980] CONTINUE_SENTINEL 在 Anthropic/Google 格式下制造连续同角色消息** — tool loop 第二轮起对原生端点必 400（生产 Ark 网关容忍，**待人工实测**）。修复：合并连续同角色消息。

### C. 门禁绕过（质量链失效）

11. **[tools/misc_tools.py:370-374] `_check_self_merge_gate` 显式 taskId 路径不校验任务归属** — 对自己未审分支调 `merge(taskId=<他人已 approved 任务>)` 即可借背书自合，「自写自审自合」核心控制被低成本绕过。修复：显式 taskId 同样要求 assignee==调用者或校验分支派生关系。
12. **[task_tools.py:1771,1780] waiver 存在时跳过全部身份门** — `if not verify_waived` 同时旁路禁自审门与 VERIFY 独立审门；QA 可等 CEO 豁免后自批自己的 VERIFY（第三人豁免+当事人自批=无人独立审查）。修复：`assignee==reviewer` 硬门不随 waiver 短路。

### D. LLM/协议层

13. **[llm/circuit_breaker.py:201-215] half_open 状态放行所有并发调用（探针锁失效）**；且 `_run_sync` 的 `httpx.Client` 构造在 try 之外、7 处 `call_soon_threadsafe` 无保护——线程静默死亡时主循环空等 195s 报一个丢失根因的超时。修复：half-open 只放一个探针；_run_sync 全包 try，兜底必入队。
14. **[llm/provider.py:1115] Google handler 对所有 functionCall 硬编码 `index:0`** — Gemini parallel function calling 的多个不同函数被 merge_tool_calls 拼成一个（name 粘连、arguments 非法 JSON），多工具调用必然损坏。修复：per-stream 单调递增 index。

### E. 前端高危

15. **[apps/web api.ts:320,450] 对 joining 状态 channel 重复 `channel.join()`** — phoenix.js `joinedOnce` 守卫下二次调用同步抛异常，开局发消息等真实场景消息未发出且无 error 事件（基于 1.7.x 已知实现，**待人工确认**）。修复：复用 JoinPush/缓存 join promise。
16. **[ChatPanel.tsx:1219,1470-1471] 「停止」按钮无法取消后端流** — streamChat 返回的 `{abort}`（唯一 push "cancel" 的句柄）被丢弃，AbortController 的 signal 从未传出；点停止仅复位本地 UI，agent 照跑。修复：保存返回值并在 handleStop/卸载清理中调用。
17. **[ChatPanel.tsx:1483→1503] 条件 return 之后调用 Hook** — 当前父级三元保证不挂载属哑弹，父级改始终挂载即 React 崩溃。修复：hook 移到提前 return 前。

---

## 三、TEST6 尸检修复交叉核对（AI_MEMORY 声称今日已修）

| 修复项 | 结论 | 证据/缺陷 |
|---|---|---|
| P1-1 watcher/trigger 共用 filter_actionable_pending | **落地但有缺陷** | trigger.py:216-226/253、agent.py:441-444/493-494 已统一；但 `_maybe_self_retrigger`（agent.py:4051）是第三条未过滤的 pending 消费路径，仅剩 FYI 时 executor 仍被唤醒烧 turn |
| P1-1 仅 FYI 时 ACK 标读 | 已落地 | agent.py:451-469/500-507、trigger.py:231-248 |
| P1-1 trigger_fail_count≥5 熔断 | 已落地（语义修正） | agent.py:402/511-546：熔断=举红+升级上级+清零+30s 退避；「不标读 actionable」是有意设计（测试固化），比旧建议的「标读清账」更安全 |
| P0-1 submit 门消费 core_interaction attestation | 已落地且正确 | task_tools.py:1372-1407 + attestation.py:550-594；拒绝文案含可执行样例 |
| P0-1 browse 内联 JS 写 tempfile | 已落地且正确 | browse_tools.py:226-259，双分支测试覆盖 |
| P0-2 waive 必填 evidenceAttestationId | **已落地但测试全挂** | 实现 task_tools.py:2561-2576 等齐全；6 个测试未同步（见验证轴），waive 链路无测试保护 |
| P0-2 每任务最多 2 次 / 豁免人≠批准人 / 失败 test_run 落账 | 已落地 | attestation.py:542/516-537、task_tools.py:1873-1884、bash.py:1125-1127 |
| P0-3 VERIFY failuresAcknowledged 结构化门 | **落地但有缺陷** | 门在 task_tools.py:1409-1438；但 `count_reported_test_failures`（attestation.py:615-625）首个正则匹配取胜，聚合输出 "Suites: 0 failed…Tests: 3 failed" 时误判 0 → 门整体跳过。修复：取 max |
| P0-3 spawn 自动 exclude .hiveweave/ | 已落地且正确 | process_registry.py:290-316 三正则+幂等守卫，bash 与 dev_server 双路径消费 |
| P1-2 verify stale nudge 时间桶 idempotency | 已落地 | task_tools.py:3682-3685 |
| P1-3 health_supervisor wake+红框+上级通知 | 已落地 | health_supervisor.py:305-427；残余瑕疵：无 health:ok 恢复广播 |
| P1-4 孤儿任务（created+assignee=NULL）催 creator | 已落地 | game_time.py:1091-1146 [ORPHAN TASK] |

**结论：12 项修复全部真实落地，其中 3 项带需跟进的缺陷（FYI 第三路径、waive 测试失效、失败计数解析）。**

---

## 四、跨模块根因模式（比单点更重要的系统性问题）

1. **共享 aiosqlite 连接上的事务泄漏（同一模式 3 处）**：dismiss 任务过户（org.py:554-789）、`replace_waits`（wait_contract.py:197-260）——裸写循环+末尾单次 commit，中途异常不回滚，半成品被后续无关写（project.py 每次 execute 都 commit）一并落库。治根：封装「共享连接事务上下文」统一 BEGIN/rollback。
2. **asyncio 裸 `create_task` 无引用（7 处）**：agent.py:2710/4005/4327/4340、game_time.py:835、event_audit.py:44、telemetry.py:204-209——CPython 只持弱引用，task 可能 mid-run 被 GC，retrigger/审计/wait 超时静默丢失。治根：全局 `_pending: set` + `add_done_callback(discard)` 的 canonical helper。
3. **「硬门很硬，但旁边有绕行小道」（同一模式 5 处）**：waiver 短路身份门、自合并门跨任务背书、move_file/apply_patch 绕 write-kind 分类（当前不可利用但缺纵深防御）、role 字符串兑换 family、REST 端点无 actor 门。治根：每个硬门配一条「绕行面」审计断言测试。
4. **测试失效/测错目标**：waive 6 测试全挂；`sse_to_chunks` 生产零调用却是 SSE 测试唯一入口（生产路径 `parse_stream_chunk` 无直接测试）；conversation/api/realtime 几乎零直接测试；Critical 的 `_row_to_msg` 漏字段正是被「手工构造 dict」的测试掩盖。
5. **文档漂移（CLAUDE.md 需同步）**：74→**84** 个注册工具；`supervisor.restart_agent` 死代码**已被删除**（文档说法过时）；DOOM_LOOP_READONLY_TOOLS 17→**20**；CJK token 比率代码 /1.0 文档 ~1.5；main.py version 0.1.0 vs health.py 0.2.0；`_SLICE_BUDGET_MAX=2` vs 文档「最多再 1 个 slice」（**待人工确认哪边为准**）。
6. **「readonly」语义名存实亡**：`READONLY_TOOLS`（permission.py:100-113）含 bash/write_file/apply_patch；`infer_role_family` 默认返回 "executor" 使「未知 family 兜底 READONLY」永不可达（policy.py:230 死兜底）——任何未识别 role 拿满 executor 权限，与 CLAUDE.md 直接冲突。

---

## 五、分模块详情

### 5.1 agents/（agent.py 4522 行 + supervisor.py + trigger.py）— 72/100

**Critical**: cancel 不持锁竞态（清单 #3）。

**Required**:
- agent.py:1010-1024 cancel 语义自相矛盾：注释称「普通 cancel 会 ACK pending」，主流路径实际保留未读（`_handle_cancel` 注释相反），ACK 块仅在竞态窗口可达——确认意图后删不可达块。
- agent.py:4051 `_maybe_self_retrigger` 未过 filter_actionable_pending（TEST6 P1-1 第三条裂缝）。
- agent.py:2710/4005/4327/4340 fire-and-forget create_task 无引用（GC 风险，retrigger 被回收=静默停摆）。
- agent.py:2166-2183 mid-turn 到达的消息成功退出时被 ACK 但 LLM 从未见过（非 expect_report 类永久丢失；作者注释显示知情，**待人工确认**）。
- agent.py 单文件 4522 行：`_handle_completion` 单方法 ~950 行混 5 种职责；「上级升级+trigger」模式 6 处近复制，应提 canonical helper。

**Optional 精选**: trigger_source 永为空串（905-909）；回复门禁 N+1 查询（1831）；30min 并集把 ask 到达前的发送算作已回复（1870-1874，待确认）；`_drain_message_queue` 仅成功路径调用（2328）；admit_wake 恒真 stub 周边 ~80 行仪式代码（trigger.py:100-114）；每 text_delta 一次 DB 写（4404-4412）。
**死代码**: `Agent.trigger()`、`_build_open_task_hint`、`CHAT_CALL_TIMEOUT_MS`、`SELF_RETRIGGER_DELAY_MS`/`TRIGGER_DELAY_MS` 重复定义、cancel ACK 分支、supervisor.restart_agent（已删，文档待更新）。
**测试**: watcher 复活/中断计数/stall 账本/single-flight/僵尸自愈均有覆盖；缺 cancel 竞态、self-retrigger FYI 过滤用例。

### 5.2 llm/（streamer.py + provider.py + retry.py + circuit_breaker.py）— 72/100

**Critical**: half-open 探针锁失效+线程静默死亡（#13）；Gemini 多 functionCall index 硬编码（#14）；CONTINUE_SENTINEL 连续同角色（#10）。

**Required**:
- provider.py:847-876 + streamer.py:1773-1775 Anthropic 流结束 `usage.input` 被 message_delta 覆盖为 0，token 计量失真（改「非零才覆盖」）。
- streamer.py:1598+1686-1708 HTTP 重试用同一 delta_id 重推整条流，前端拼出「旧残段+新全文」错乱文本（仅 connect error/非 200 才应走 with_retry）。
- streamer.py:1755-1765 500/502 走 PermanentError 不上报 error_status → agent.py:1182 的 500/502 failover 分支是不可达死逻辑（契约断裂，二选一收敛）。
- streamer.py:139-172 `DOOM_LOOP_TOOL_LIMITS` 与注册表漂移：4 个死条目（save_memory/save_goals/execute_code/mark_read），连带 write_memory 只享受默认 limit=3 而非设计的 8。
- provider.py:960-967 Gemini functionResponse name 恒 "unknown"，tool loop 第二轮起请求非法。
- provider.py:582-584/625-637 Anthropic thinking：temperature 未强制 1；回传无 signature 的 thinking block（与 790-792 丢弃 signature 自相矛盾）。
- 测试有效性：`sse_to_chunks` 是生产零调用的平行实现却是 SSE 测试唯一入口；breaker 状态机/线程桥接无测试。

**Optional 精选**: event_q 无 maxsize 无背压（1654）；每轮新建 httpx.Client（1660，tool loop 每轮一次 TLS 握手）；parse_sse 每 chunk 全量扫描 O(n²)（1686-1690）；熔断 fallback 递归无环检测（807-815）；熔断错误契约不统一（CircuitBreakerOpenError 被当 crash 计）；熔断器按 model name 共享状态跨项目牵连（762）；`_fire_delta` 在消费循环内，on_delta 异常杀死 LLM 调用且计入熔断（1780-1789）；`_LLM_SEMAPHORE` 单例跨 loop 绑定问题（测试环境）；stall break 后的 summary 调用不走 semaphore/熔断/重试（1340-1342）。
**死代码**: `_iter_sse_with_timeout`、`_read_error_body`、`sse_to_chunks`、`_api_format_to_provider_type`、`create_from_name`、`ProviderType`、provider.py TOTAL_TIMEOUT_S=300 副本、DOOM_LOOP 死条目 4 个。
**Nit**: 模块 docstring 过期（300s/100 轮 vs 实际 540s/1000000）；CircuitBreakerOpenError 文案 "not implemented" 过期。

### 5.3 tools/（23 文件，84 注册工具 + 5 legacy 评审套件）— 85/100

**Critical**: 无（全场最高分模块）。

**Required**:
- dev_server_tools.py:117/195-197 刻意 fd 泄漏（"child owns the fd via inheritance" 是误解）——每次成功起 dev server 永久泄漏一个 fd，Windows 阻止日志删除。修复：spawn 成功后父进程关闭。
- bash.py:688-699 超时只杀壳进程不杀进程树（cmd/npm 派生的孙进程变孤儿占端口）；dev_server_tools.py:180-184 同类（杀 npm 包装层留 vite node，重试撞 strictPort）。修复：Windows taskkill /T /F 或 Job Object。
- policy.py:396-397 `classify_write_kind` 不覆盖 move_file 的 destination 与 apply_patch（当前因工具表巧合不可利用，缺纵深防御）。

**Optional**: browse `core_interaction` 语义错位（js 算交互、click 反而不算，browse_tools.py:379）；browse tempfile 永不清理；game_run_case probe 标 core_interaction=True 削弱 P0-1 门。
**死代码**: task_tools.py:40-427 `TaskToolsMixin` 全部 7 个 `_tool_*` 方法（~360 行）+ `_TaskToolHost` 不可达，且 legacy 实现缺 attestation 门，留存误导；executor.py:1275 `"review"` 键被管线遮蔽。
**测试缺口**: P0-1 submit 门端到端拒绝路径、P0-2 taskId 解析三分支均无单测。
**mypy 补充**: misc_tools.py:826（错误 kwargs 调用 send_message）与 850（引用不存在属性）为到达即炸的运行时 bug，归本模块必修。

### 5.4 services/任务审查链（task/dispatch/approval/attestation/worktree_review/merge_proxy/obligation/run_ledger/git_worktree）— 76/100

**Critical**: merge 先删分支后扫 marker（#1）；自合并门跨任务 taskId 绕过（#11）。

**Required**:
- waiver 短路全部身份门（#12）。
- obligation.py:315-320 依赖唤醒野生 SQL blocked→running，绕过 _transition/task_events/契约清理。
- task.py:1450-1451 `_task_skips_merge_gate` 在客观 commits-ahead 检测前 return——`no_code_change=true`（提交方自控字段）可放行有未合并提交的 close。修复：先跑客观检测再采纳声明。
- attestation.py:615-625 失败计数首匹配取胜（P0-3 门被 "Suites: 0 failed" 绕过，取 max）。
- git_worktree.py:1199+ merge() 无并发锁（create 有），并发 merge 裸撞 index.lock 或重复 spawn VERIFY。
- waive 工具层测试全挂（验证轴 6 failed）。

**Optional**: reassign reviewing→claimed 野生 SQL（task.py:2194）；`_rollback_close_to_approved` 几乎不可达的回退（1718-1724）；task_event_relay 失败事件也 mark_delivered（at-most-once 丢安全网通知）；`update_task` 允许 PATCH tags 可改 docs_only 标绕 merge 门；task_contract 三类校验一律 deferred=True 造成「已执行」错觉。
**死代码**: `_auto_checkpoint_dirty_target`、`_porcelain_non_hiveweave_dirty`、`_stamp_merge_status_on_close`。
**不变式核对**: progress 语义/assign=claim/CREATOR_MUST_MERGE/VERIFY 独立审/分支命名/删除安全链/对账/evidence 规范化——全部落地。

### 5.5 services/组织权限（org/org_invariants/org_guardrails/org_span/policy/permission/staffing/roster/template/names/agent_router/skill_registry/model/settings）— 72/100

**Critical**: hire 校验在锁外（#4）；role 兑换 family（#5）；REST 变更端点无 actor 门（#6）。

**Required**:
- org.py:554-789 dismiss 过户批处理非原子（事务泄漏模式 #1）。
- agent_router.py:64-67 rebuild 只装 active——重启后 archived agent 从路由消失，历史消息 sender/reviewer 解析不到（重启前后行为不一致）。
- model.py:373-382 `model_resolve_emergency_pool` 分支跨级取模型，违反「禁跨级」（**待人工确认**是否故意；无测试）。
- permission.py:100-113/234-237「readonly」名存实亡 + readonly→readwrite 静默升级（根因模式 #6）。
- policy.py:230 + permission.py:267-268 未知 family 兜底永不可达，未识别 role 默认 executor 全权限。
- org_invariants.py:123-135 validate_hire 对解析不到的 parent_id 静默放行（REST 可造孤儿 agent，树内不可见）。
- model.py:198-202 不变量校验被 falsy 绕过（显式传 0 落库 context_window=0）。
- org_invariants.py:111-121 executor 岗位唯一性大小写敏感（"API工程师" vs "api 工程师" 可绕）。

**Optional**: dismiss 配额 TOCTOU（org_guardrails.py:60-141）；UUID 前缀歧义静默取首个（org.py:322-325）；bind_skill 读-改-写无锁；skills.sh 一次搜索最坏 ~24s；model.get 双渠道同 model_id 时 LIMIT 1 行序未定义可能拿错 api_key；start_dev_server 在 CEO_TOOLS 与「CEO 无 bash」有张力（待确认）。
**死代码**: unknown-family 兜底分支、role_family explicit 分支、CLAWHUB_* 常量、EXECUTOR_ONLY_TOOLS 空集等 6 项。
**Nit**: staffing.py:101 `fulfilled_by[:12]` 传 None TypeError 被吞；roster.py ALTER 失败仍标 migrated；names.py 1000 次穷尽后返回重名。

### 5.6 services/通信回合（inbox/team_chat/chat_message/reply_policy/wait_contract/wake_policy/turn_*/handoff）— 72/100

**Critical**: inbox.py:1134-1152 `_row_to_msg` 丢弃 `reply_contract_id`——连锁三处失效：① trigger 的 how_to_reply 提示永不出现；② turn_exit 合约关闭判定退化为启发式；③ escape valve 的 waive_items 恒空 → 阀门触发后 commit_turn 仍被 UNREPLIED_ASKS 硬拒，且消息已读、agent 被告知有未回复 ask 却看不到对象。

**Required**:
- turn_exit.py:149-175 `collect_unreplied_asks` 仍把工具**参数**里的收件人当已回复证据（与 278-289 已移除参数兜底的口径直接冲突，失败 send 两门证据标准矛盾）。
- turn_exit.py:755-806 预检三组任务门不看 `waiting_on` 覆盖——按提示操作的 agent 被 soft-warn，同 turn 重 commit 即硬 REJECT。
- wait_contract.py:197-260 replace_waits 事务泄漏（根因模式 #1）。
- handoff.py:86-102 create_handoff SELECT-then-INSERT 无唯一约束（并发重复 handoff 义务双计）。
- handoff.py:248-250 mark_reported 的 `(task_id = ? OR task_id IS NULL)` 与 docstring 矛盾，submit 任意任务顺带清掉他人的无 task_id 义务（待确认）。
- orchestration_tools.py:388-391 expect_report schema 文案仍宣称「自动从文案推断」（HARD RULE 已禁），诱导 LLM 永不显式传 → 回复义务系统性漏建。
- inbox.py:264-279 自动幂等键无时间窗——合法同内容重发被永久去重且跳过 auto-close。
- inbox.py:426-442 UNIQUE 竞态分支返回未落库的幻影 msg_id。

**Optional**: team_chat 60s 整点分桶跨边界不去重；team_chat_dedupe 只插不删无界增长；archived 拒投 fail-open（210-222，待确认）；break_wait_cycles 清掉环成员全部 wait 含无关契约；chat_message.py:362 同毫秒时间戳误判已回复；done_slice 预检每次跑 git status 子进程。
**死代码**: inbox_triage 几乎全文件（build_platform_digest 等 5 函数，trigger 已不走）、wake_policy.classify_message（已掏空）、reply_policy.message_requests_reply（恒 False）、team_chat._is_duplicate、wait_contract 3 个唤醒匹配函数。
**测试缺口**: 无 `_row_to_msg` 透出 contract_id 断言（Critical 漏网之根）、无 handoff 并发去重、无 waiting-on-自己-任务的预检用例。

### 5.7 services/基础设施（game_time/health_supervisor/process_registry/mcp/memory/work_log/off_duty/platform_state/system_state/project_lifecycle/telemetry/event_audit/vision/charter）— 78/100

**Critical**: MCP stdio 透传全量环境密钥（#9）。

**Required**:
- game_time.py:835 + event_audit.py:44 + telemetry.py:204-209 裸 create_task/ensure_future 无引用（根因模式 #2）。
- mcp.py:213-223 stdio JSON-RPC 从不校验响应 id——超时后迟到响应被下一次 call 错配，错误结果直接喂 LLM。
- mcp.py:418 `disconnect_all()` 无调用方——后端退出后 stdio MCP 子进程残留孤儿。
- game_time.py:1849-1887 `[DEAD AGENT]` 升级无上限，对永久死 agent 每 15min 无限刷屏（对比沉默看门狗已有退避）。
- process_registry.py:364-367 裸 `npm/pnpm dev`（无 vite 子串）改写为 `PORT=xx cmd` POSIX 语法，Windows shell=True 下整条命令必炸。

**Optional**: game_time start() 重启丢 6 个 tracker 计数（升级链被打断）；`_cooled` 在 send 之前盖章（失败也耗冷却）；`_check_silent_agents` 无时间下界全表聚合+N+1（2000-2069）；health_supervisor 无 health:ok 恢复广播；telemetry `tool_loop_stall` 计数器不进 snapshot（/api/debug/metrics 看不到）；mcp `_get_connection` 无锁并发首连泄漏 transport；event_audit 表无保留策略；charter save_charter 的 project_id 形参被忽略；vision strip_images 假设 content 为 str；work_log get_since 无 LIMIT。
**死代码**: `_check_stalled`/`_nudge_awaiting_replies` no-op 仍被 tick 空调用、`_break_peer_review_deadlocks`、3 个零引用常量、system_state `_cleanup_orphaned_approvals` 空占位（连带 _hourly_sweep 每小时空转）。
**Nit**: health_supervisor 红框 message 英文 vs game_time 中文口径不一。

### 5.8 conversation/ + db/ + hooks/ + main.py — 72/100

**Critical**: 压缩竞态丢消息（#2）。

**Required**:
- compaction.py:205-220 trim 回退「成对裁剪」只 drop assistant + 第 1 条 tool result——≥2 个 tool_calls 时剩余 result 变孤儿，违反不拆对不变式。
- main.py:542-552 + api/auth.py:73-100 中间件顺序错误：设 `HIVEWEAVE_API_KEY` 后 CORS preflight（OPTIONS 不带凭据）被 401，前端跨域全挂。修复：放行 OPTIONS 或 CORS 最外。
- db/project.py:99-105 ensure_project_db 对 ALTER `except Exception` 全吞零日志——迁移半成品无任何观测，运行期以 "no such column" 在远处爆炸（meta.py:118-122 已有正确范式）。
- store.py:232-233 每次 append_turn 全历史 token 扫描 + 两次 DB 查询（context window 不随模型变）——O(历史) 重复扫描。
- store.py:82-86 `_cleared_agents` 守卫只拦 `_persist_turn`，队列中的 `_persist_compaction/_persist_pruned` 可绕过使 clear() 失效（当前无生产调用方，潜伏陷阱）。

**Optional**: compacted_prefix UPDATE 失败静默（重启后摘要无声丢失）；token_utils `_save_tool_output` 写盘失败仍返回路径（谎称已保存）；工具输出含潜在密钥写共享临时目录默认权限；hooks wait_for 超时取消 handler 留半完成写（fail=open 应在副本上跑）；`output["hint"]` 单槽位互相覆盖；main.py 启动对每项目串行 git 子进程（>50 项目打满连接 LRU）；shutdown 不 drain conversation 写队列。
**死代码**: store.py `clear()`/`clear_all()`/`maybe_compact_on_model_switch()`/`_PRUNE_PROTECTED_TOOLS`/`_trim_to_budget` 全家（生产调用方均不传 token_budget）、compaction `should_compact()`、token_utils 3 函数、main.py:421-432 恒空循环。
**测试**: conversation store/compaction **零直接测试**（Critical 竞态无护栏）；hooks 有专项测试。

### 5.9 api/（16 文件）+ realtime/（4 文件）— 72/100

**Critical**: filesystem write 写穿 .hiveweave（#7）；单模型端点泄 api_key（#8）。

**Required**:
- phoenix_adapter.py:220 WS 消息无尺寸上限（GB 级消息打爆内存）；chat payload 无长度校验即落库进 prompt。
- phoenix_adapter.py:299-381 channel join 无 per-topic 授权——任意客户端可 join `agent:<任意id>` 拉历史并 chat/cancel（单租户设计可接受但应写明）。
- event_bus.py:285/354/400 `project:{id}` 频道只发布无订阅端点——status/goals/question 的 project 事件全部空投（补端点或删发布）。
- filesystem.py:217-293 `/api/fs/browse` 无需 projectId 可遍历整盘（泄露目录结构）。
- system.py:83-143 restart-backend/frontend 在 key 未设时任何人可杀服务。
- models.py:676-806 SSRF 原语：create/test 向用户提供的任意 baseUrl 发 POST 并回显响应前 200 字符（内网探测）。

**Optional**: 5 个读端点 `except Exception → return []` 吞错（tasks.py:311-312 ensure_executor_worktree 连日志都没有）；event_bus 队列满丢最旧事件（丢 done/error 前端卡 PROCESSING）；SSE `_subscribe` 无连接数上限（WS 有 50）；`_agent_buffers` 归档后不清；legacy `/ws/agent/{id}` init 含 inbox 内部消息与 phoenix 版暴露面不一致；chat.py:474-477 正常路径只推 user message_id 不推 assistant 占位（与契约不符，**待确认前端是否已改由 stream start 建占位**）；settings upsert 无 key 白名单/value 无界。
**死代码**: `verify_api_key`（导出但零 Depends）、RedisBackend（NotImplementedError 占位）、set_game_time_speed 占位端点、COMPAT 兼容路由群（债）。
**测试**: api/realtime **几乎零直接测试**，鉴权中间件/join 流程/event_bus 规则均盲区。

### 5.10 apps/web 前端 — 78/100

**Critical**: phoenix 重复 join 抛异常（#15）；停止按钮断链（#16）；hooks 顺序哑弹（#17）。

**Required**:
- ChatPanel.tsx:1257-1267 兜底路径丢弃首个 text chunk（且使 1281-1292 主初始化分支不可达）。
- api.ts:308-310 chat push 无 error/timeout 处理——WS 断开时消息静默丢失，用户已 optimistic 上屏。
- api.ts:303/501/504-506 `_agentHandlers` 单槽覆盖 + cleanup 无 ownership 校验（旧清理删新 handler）。
- api.ts:75 API key 明文 localStorage（无 dangerouslySetInnerHTML，现实风险有限，但密钥不应落 localStorage）。
- QuestionDialog.tsx:36-47 无项目仍 5s 轮询 + 原生 alert（与自建 ConfirmDialog 方向相悖）。

**Optional**: OrgTree 未消费 agentDispositions（「主文案跟 disposition」只在 ChatPanel 生效）；OfficeView/OrgTree 多路 3-5s 轮询替代 WS 事件；GoalsPanel 渲染期写 ref + fetchGoals 无竞态防护；ChatPanel 1761 行/api.ts 1247 行/OrgTree 1093 行超大文件；AgentDetailPanel useEffect 依赖不全 + "Elixir backend" 过期注释；MonitorPanel turn↔event 靠 ±5s 时间窗启发式（后端带 turn_id 可根治）。
**死代码**: ChatPanel 1281-1292 不可达分支、AbortController 空壳、AddAgentDialog 空 setTimeout+无效模板搜索、MonitorPanel idx prop。
**Nit**: index key 16 处（GoalsPanel:258 接近 Required）；`[SSE] console.log` 调试残留；globalThis 挂 `__hw_*` + 大量 as any。
**符合性**: agent_health→红框链路完整、canvas 层无模拟逻辑、无 dangerouslySetInnerHTML、OfficeScene destroy 防护齐全——设计约束达标。

---

## 六、修复优先级建议（按出手顺序）

**第一梯队（数据丢失/安全，立即修）**
1. git_worktree merge 顺序缺陷 + marker 扫描范围（#1）
2. conversation 压缩竞态（#2）
3. MCP 环境透传密钥（#9）
4. api_key 单查泄露 + filesystem 写穿 .hiveweave + REST org 变更无门（#6/#7/#8）
5. 前端「停止」断链 + phoenix 重复 join（#15/#16）
6. cancel 不持锁竞态（#3）

**第二梯队（门禁收口，本周修）**
7. 自合并门 taskId 归属校验 + waiver 不短路禁自审门（#11/#12）
8. hire 校验入锁 + 非 bootstrap 硬拒保留 role（#4/#5）
9. `_row_to_msg` 透出 reply_contract_id + escape valve 回归测试（5.6 Critical）
10. 失败计数取 max（P0-3 门收口）
11. inbox 幂等键加时间桶 + expect_report schema 文案改正

**第三梯队（契约/卫生）**
12. 修 6 个 waive 测试 + 补 sse/breaker/conversation/api 直接测试（验证轴门面）
13. 500/502 failover 契约收敛（二选一）+ Anthropic usage 合并
14. 事务泄漏 3 处统一封装 + 裸 create_task 7 处统一 helper
15. 清理死代码（全平台约 40 处，清单见各模块节）+ CLAUDE.md 文档漂移同步

---

*附：本报告为只读审计，未修改任何代码。Critical 中标注「待人工确认/待实测」5 项（#10、#15 及 5.1 mid-turn ACK、model emergency pool、chat 占位事件），建议优先人工裁决。*
