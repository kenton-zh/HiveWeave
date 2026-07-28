# 三轮审计：ylgy-hotfix-forensics-2026-07-28.md 核验意见（2026-07-28 深夜）

> 审计人：平台侧（WorkBuddy/一元）
> 对象：`deliverables/ylgy-hotfix-forensics-2026-07-28.md`（TEST_YLGY hotfix 轮独立核验报告）
> 方法：逐条对照 TEST_YLGY per-project DB（agent_events / inbox / tasks / chat_messages）、main 仓 git 记录、后端日志 `tasks/backend-20260728-225915.output`、agent 证据文件

## 总裁定

**报告可采信。** 核验的约 15 条具体声称全部属实，无一条夸大或虚构；W1「修复生效」与 W2「PARTIAL（闭环≠机制实证）」两个核心定级均成立。有 4 处可补强（不推翻结论），另有报告窗口外的 live 事件显著改变残留优先级。

## 一、逐条核验（全部通过）

| 报告声称 | 核验结果 |
|---|---|
| 29 条 worktree_rebuild 全部 A015/Sage | ✅ DB 证实（27 reuse_existing + 2 fallback） |
| 修前约 6 分钟一条、持续 2h+ | ✅ 实测间隔 362-363s，跨度 2.66h（20:14→22:54） |
| hotfix 窗口（≥1785251049916）仅 1 条 fallback | ✅ 精确吻合（23:06:36.120） |
| 该条之后 0 条 rebuild | ✅ 就 Sage 成立（截至 23:28 未再发） |
| inbox 存在 system→Sage [WORKTREE RELOCATED] | ✅ id=d271cab3，23:06:36.471，delivered=1 read=1，内容与引用逐字一致，与 rebuild 事件同秒（差 351ms） |
| CEO 终版写「未观察到/P2 补 capture」、渡口写「未触发」 | ✅ 两份原文核对无误，均与 DB 矛盾 |
| 渡口 checkpoint 因 PIN 间接证明 | ✅ 任务 evidence 原文自述 |
| 四任务 closed、22 closed/2 cancelled | ✅ tasks 表证实 |
| 审查链大量 waive（明鉴/CEO） | ✅ 四任务全部 waived：fdd4dd06（8b17e12d）、f40b4e07（1a0979bb）、3c48efb8（e254dbbf）、CEO 终验 waiver（ee0f8e87）；Sage 提交 attestation_ids=[] 空 |
| W2 无新 400（仅提示词复述） | ✅ chat 窗口内 4 处提及全是验收标准复述；后端日志 1 处是 create_task 参数引用 |
| Sage 报告依赖修前 fc68c4ef、缺受控剥图证据 | ✅ 逐行核对：测1「本回合」证据仅为「回合正常运行」，无 screenshot 注入；无 _IMAGES_OMITTED_NOTE 回执 |
| 代码+单测在 | ✅ fbcfbd4 已落 provider.py 剥图门（_IMAGES_OMITTED_NOTE）+ vision.py 强制 supports_images=True（防误伤 look_at_image）+ test_text_only_model_image_gate.py 215 行 |
| 磁盘 A013/A014/A015-b 有 .git、A015 空壳 | ✅ 成文时属实（A014 在报告窗口后也成空壳，见第三节） |

## 二、报告可补强处（不推翻结论）

1. **未抓到最尖锐的矛盾**：W1 VERIFY（1a0979bb） Sage 证据原文「观察 1 次迁址(d271cab3 A015→A015-b) + inbox 通知到达」，CEO 终审却在同任务 review_feedback 里照抄「inbox 未触发，P2 补 capture」。错误叙述覆盖了同一条任务记录里的相反证据——这比「观测窗口不全」更深层，是审查方不复核提交证据的实证，恰好印证 P0-2（审查方执行证据硬闸）的动机。
2. **§1.4 自己留的追问可已解答**：hotfix 窗口那次 fallback 的触发条件 = 23:06:34 明鉴 merge(branchName='A015') 走 resolve_effective_path_db 正确解析 → merge 流程 23:06:35.22 delete A015 worktree → 0.9s 后 Sage ensure 发现无有效树、canonical 为空壳 → 一次性 fallback 重挂 A015-b（head=a8cf868）+ 发 notify。是「merge 删除 + ensure 重挂」的设计内一次性路径，非死循环复燃；但也暴露 ensure 仍先探 canonical 再回退 DB 绑定，而非优先认 DB 绑定的 -b。
3. **W2 VERIFY 测错了代码路径**：渡口 ee0f8e87 的 3 个样例全部走 look_at_image 的 vision 槽 text_only fallback，与主模型剥图门（provider.py）是两条不同路径，不能互证。且「8.8KB 修前小图 → text-only 详细分析(体积证据+canvas 空白)」本身有幻觉嫌疑（text-only 模型对看不见的图产出「详细分析」被判 PASS）——剥图门要防的正是这种输出。
4. **T6min 归属**：CEO 报告自承 T6min 为「明鉴自补」，渡口任务证据也声称 T0+T6min 双跑（文件真实存在、23:06:02 采样）。两线并行但报告未点破 provenance 重叠；渡口窗口结束于 23:06:02，RELOCATED 23:06:36 发出——险错过 34 秒，非漏查。

## 三、报告窗口外 live 发现（23:17–23:28，改变残留优先级）

1. **P1→P0：merge obligation 账本不清（P0-1 变体复发）**。8b17e12d 于 23:07:09 已 merge（78ec808 落 main）+ closed，obligation 81b43baa 仍于 23:17:40 / 23:22:43 两次升级。CEO 23:18:39 重跑 git_worktree_merge（删除 A014 worktree → 空壳 → 23:21:42 A014-b 迁址 cascade），23:23:02 用 cancel_task 强行清账。merge 在 git 层闭环、在 obligation 账本层不闭环。
2. **P0-1 第二例**：Sage 的 W1 VERIFY 交付物 reports/A015-w1-relocate-verify.md（commit 21d1697）**stranded 在 hw/A015/work 未合 main**，任务却已 closed。讽刺的是，正是这条未合入的 commit 含有推翻 CEO「未触发」的正确证据。
3. **WinError 32 未根治**：A014 空壳 rename→.stale-* 失败 ×2；reconcile 对 A014/A015 的 orphan+husk 删除全部失败。dev server 进程（port 3456 pid 7484）已被 process registry 正确击杀——句柄另有所属（shell cwd / 杀软 / 沙箱待查）。
4. **live 代码 bug**：reconcile 报 `stranded task reconciliation failed: 'sqlite3.Row' object has no attribute 'get'`。
5. **反向加分**：A014 迁址全链路 live 演示成功（fallback → create -b → DB 重绑 → notify → agent 确认收到）——无意中为 W1 修复提供第二例端到端实证，报告的 W1 结论因此更稳。

## 四、结论

- 报告事实层零错误，判断层 W1 ✅ / W2 ⚠️ PARTIAL 均成立，可全文采信。
- 其 P1（补 W2 端到端硬证：强制 text-only 主模型 + 注入截图 → 抓 _IMAGES_OMITTED_NOTE）仍是第一优先；注意必须测主模型路径，look_at_image 槽不算。
- 本审计新增的 merge-obligation 不清账（含 CEO cancel_task 清账的错误姿势）与 VERIFY 交付物 stranded 两例，应并入 P0-1 修复范围一并处理。
