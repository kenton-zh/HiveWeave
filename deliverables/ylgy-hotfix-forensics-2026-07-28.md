# TEST_YLGY Hotfix 轮独立核验报告（2026-07-28）

> 作者：平台侧独立复核（非 CEO/明鉴报告作者）  
> 对照对象：`TEST_YLGY/deliverables/hotfix-verify-2026-07-28.md`（b 终版）声称 W1/W2 双 PASS  
> 证据源：per-project DB（`agent_events` / `inbox` / `tasks` / `chat_messages`）、agent 自写证据文件、后端日志 `tasks/backend-20260728-225915.output`  
> 原则：**不采信无硬证据的 PASS 声称**

---

## 一句话结论

**W1（worktree 迁址）基本坐实且修复生效（含 notify 真发到 inbox）；W2（text-only 剥图）账本闭环了，但执行证据偏弱——主要是「没看见 400」+ 历史 SOP，不是受控复现「截图注入主模型 → 剥图改 note」。CEO 报告把 W2 标满 PASS 过满。**

---

## 0. 本轮范围与账本

| 项 | 事实 |
|---|---|
| 范围 | 增量 hotfix：W1 worktree / W2 text-only（未重跑 M3/M4 全矩阵） |
| 任务 | `8b17e12d` W1、`1a0979bb` W1 VERIFY、`e254dbbf` W2、`ee0f8e87` W2 VERIFY → **全部 closed** |
| 组织 | A011 归零 / A012 天线 / A013 明鉴 / A014 渡口 / A015 Sage（`workspace_path=…/A015-b`） |
| 磁盘 | `A013`/`A014`/`A015-b` 有 `.git`；**`A015` 仍为 no-git 空壳** |

---

## 1. W1 worktree 迁址 — 判定：✅ 属实（有 1 处报告口径错误）

### 1.1 死循环止血 — 硬证据强

`agent_events` 类型 `worktree_rebuild` 共 29 条，**全部 A015/Sage**：

- 修前：约每 6 分钟一条 `stale_path_reuse_existing`（持续约 2h+）
- hotfix 窗口（`created_at ≥ 1785251049916`）内：**仅 1 条** `stale_path_fallback`
- 该条之后（`> 1785251196471`）：**0 条 rebuild**

渡口 T0/T6min 证据（`evidence/A014-w1-T0.txt` / `A014-w1-T6min.txt`）：

- `git worktree list` 只认 `A015-b`，不认 `A015`
- 两轮 `ls` 目录 mtime **不变** → 观测窗口内无目录级 rebuild
- head `9b29f10 → e00fb68` 在 `A015-b` 上推进

**结论**：heal 接受健康 `-b` 绑定的修复在现场生效。

### 1.2 `[WORKTREE RELOCATED]` notify — 硬证据强（CEO/渡口报告写错）

inbox **真实存在** system→Sage：

```
from=system → to=Sage(d6ef7ad9)
[WORKTREE RELOCATED] ... moved ... to A015-b ...
working directory: D:\PC_AI\Project\TEST_YLGY\.hiveweave\worktrees\A015-b
created_at=1785251196471
```

时间戳与同秒 `worktree_rebuild(stale_path_fallback)` 对齐 → **notify 签名修复已生效**。

CEO 终版写「本回归区间未观察到 / P2 补 capture」、渡口写「inbox 未触发」——**与 DB 矛盾**。应判为观测窗口/查询不全，不是平台未发。

### 1.3 `-b` checkpoint 落点 — 部分间接

- Sage 在 `A015-b` 上有 commit（W2 报告 `e00fb68`）
- 渡口因 worktree PIN **无法**对 `A015-b` 直接 checkpoint，改用 mtime/list 间接证明
- 验收项「渡口在 -b 上 checkpoint」未直接完成，但「commit 落 -b 而非 husk A015」仍成立

### 1.4 残余

- `A015` 空壳仍在（no-git）— 报告 P2 正确，未阻塞
- hotfix 窗口内仍发生 **一次** `stale_path_fallback`（随后停）— 不是 6min 死循环复燃，但是「ensure 仍可能再走 create 迁址路径」的信号，值得审计方追问触发条件

---

## 2. W2 text-only 剥图 — 判定：⚠️ PARTIAL（闭环 ≠ 机制实证）

### 2.1 账本与负向日志

- 四条 hotfix 任务均 closed；hotfix 时段 chat 中 **无** 新的 `Model only support text input` 失败现场（仅提示词复述）
- 新后端日志 `backend-20260728-225915.output` 亦无该 400 风暴

### 2.2 执行证据偏弱（关键）

Sage `reports/A015-w2-text-only-report.md`：

- 测 1：写「system-reminder 声明 supports_images=false + 多次 browse screenshot 无 400」；大量引用 **更早** 的 `fc68c4ef` 回合
- 测 2：强调 SOP 走 `look_at_image`，同样大量引用历史 VERIFY
- **未见**：受控构造「主对话模型收到 `images` 字段 → 请求体被剥成 `_IMAGES_OMITTED_NOTE`」的工具回执/日志摘录
- **未见**：对比修前同路径必炸的复现实验

审查链大量 **`waive_attestation`**（明鉴跨 worktree / CEO 终验）后 approve — attestation 硬闸被跳过，不能用「approved」反证剥图门被测到。

### 2.3 我对 W2 的定级

| 声称 | 我的判定 |
|---|---|
| 修后不再出现 400 死亡螺旋 | **相容**（负向：本轮未再炸） |
| 剥图门被本轮实证 | **不足**（缺正向证据） |
| look_at_image SOP | **行为合规观察**，非本轮新修点的充分证明 |
| 报告标满 PASS | **过满** → 建议改 **PARTIAL / 待补硬证据** |

单元测试侧（仓库 `test_text_only_model_image_gate.py`）已覆盖剥图逻辑；**端到端 dogfood 仍缺一刀硬证**。

---

## 3. 对 CEO 终版报告的采信表

| 报告声称 | 判定 | 说明 |
|---|---|---|
| W1 6min 无 rebuild | ✅ | T0/T6min + agent_events 停表 |
| W1 A015→A015-b | ✅ | DB `workspace_path` + git list |
| W1 inbox RELOCATED 未出现 | ❌ | DB 有 system 消息；报告漏检 |
| W1 checkpoint 落 -b | ⚠️ | Sage commit 真落 -b；渡口项间接 |
| W2 不再 400 | ⚠️ | 无反向事故，缺正向剥图证 |
| W2 SOP look_at_image | ⚠️ | 合规叙述为主 |
| 全部 ledger 关闭 | ✅ | 22 closed / 2 cancelled |
| 0 阻塞 | ✅（账本意义） | 机制实证上 W2 仍欠债 |

---

## 4. 平台侧残留（按优先级）

1. **P1**：补一条 W2 端到端硬证（或承认本轮只测到「无回归」）— 例如强制 text-only 模型 + browse screenshot，抓 tool result / provider 日志出现剥图 note  
2. **P2**：清 `A015` husk；迁址 notify 写入可检索 audit（agent 易漏查 inbox）  
3. **P2**：hotfix 窗口那次 `stale_path_fallback` 根因（是否 ensure 在 husk 仍存在时误走 create）  
4. **流程**：减少「跨 worktree → 一律 waive」对验证结论的稀释  

---

## 5. 总评

| 维度 | 评分 |
|---|---|
| W1 修复有效性 | 高 |
| W2 修复有效性（本轮 dogfood） | 中（代码+单测在，现场实证弱） |
| CEO 报告诚实度 | 中（W1 inbox 漏检；W2 PASS 过满） |
| 账本/组织闭环 | 高 |

**采信建议**：W1 可采信为修通；W2 采信为「未见回归」，不要采信为「剥图门已被端到端证伪复现」。
