# HiveWeave 幻觉评估报告 · 审计意见

审计日期: 2026-07-26
审计对象: `hiveweave-hallucination-assessment.md`（2026-07-26 版）
审计方法: prompt-engineering-expert 技能框架（幻觉分类 / 上下文管理 / 反模式识别）+ 对报告引用的全部关键代码路径逐条实证

---

## 一、总体结论

**报告的核心判断成立，项目确实存在该问题，但报告自身有 3 处事实错误、2 处重要遗漏、1 组无依据数字。**

- 核心论断"流程防护强、内容防护弱"——**属实**，证据链扎实。
- 核心论断"Compaction 摘要是最大单点风险"——**属实**，但风险机制描述不完整。
- 量化估算"15-25% 的 turn 含幻觉"——**无实测依据**，建议不采信、不用于决策。
- 修复方向（减少信息损失 + 检测补偿性编造）——**正确**，但 P0-3 与已有机制重叠，优先级应调整。

---

## 二、逐条验证结果

### 2.1 验证属实（15 项，附代码证据）

| 报告论断 | 验证结果 | 代码证据 |
|----------|----------|----------|
| 摘要 temperature=0.3, max_tokens=2000 | ✅ 精确到行号 | `compaction.py:28-29` |
| 摘要前工具输出截断至 2000 字符 | ✅ | `compaction.py:185` + `token_utils.py:27` `TOOL_OUTPUT_MAX_CHARS=2000` |
| prune_persisted 不可逆替换为无元数据占位符 | ✅ 写入 cache+DB，后续请求永不可见 | `store.py:456` `"[Old tool result content cleared]"` |
| 工具输出 2000行/50KB → head20+tail5 | ✅ | `executor.py:40-41, 1251-1253` |
| 20K buffer 部分补偿 token 高估 | ✅（命名小错：实际叫 `COMPACTION_BUFFER`，非 SAFETY_BUFFER_TOKENS） | `token_utils.py:21` |
| HONESTY_BLOCK 软约束 | ✅ | `prompts/identity.py:137, 269` |
| Attestation gate 机器验证不可绕过 | ✅ 含 verify + waiver 机制 | `services/attestation.py` |
| Epistemic status 三态（verified/claimed/unknown） | ✅ 源自 Magentic-One | `tools/org_tools.py:969` |
| Turn exit gates + WAIT_WITHOUT_ASK | ✅ | `agents/agent.py:1519-1707` |
| 无工具输出注入消毒 | ✅ 全库 grep 无 sanitize/injection 处理逻辑 | — |
| poll cache 30s TTL | ✅ | `streamer.py:341` `_POLL_CACHE_TTL_S=30.0`（仅缓存 check_agent_status/get_tasks） |
| Org directory dirty-check + org_version | ✅ | `agents/agent.py:1156-1164` |
| 三通道（send_message/message_peer/message_subordinate）并存 | ✅ | `tools/executor.py:555,572` |
| 摘要无时间戳/turn 编号 | ✅ 摘要模板 7 个 section 无时间字段 | `compaction.py:156-176` |
| 无实体存在性/数值/一致性运行时校验 | ✅ | — |

### 2.2 事实错误（3 项）

**错误 1：Doom loop "16次阈值，failure-retry豁免" — 数字与机制均错。**
实际是**按工具分级**的体系（`streamer.py:94-188`）：
- 写类工具默认阈值 = **3**（同工具+同参数**连续** 3 次，`DOOM_LOOP_DEFAULT_LIMIT`）
- 只读工具豁免计数，只受 **15 次保险丝**约束（`DOOM_LOOP_READONLY_FUSE`）
- 且首次触发仅注入警告给 LLM 纠正机会，**第二次才真中断**（`streamer.py:871-873`）
未发现 "failure-retry 豁免"——报告作者疑将"只读豁免"误记。该系统的 doom 防护比报告描述的精细得多，此错误**低估**了现有防护。

**错误 2：token 10-15% 高估被列为"风险点" — 归因错误。**
`token_utils.py:43` 注释明写"保守高估 ~10-15%，**确保不超模型硬限制**"。这是故意的安全方向设计（高估→更早触发压缩），不是缺陷，更不会诱发幻觉。报告把设计决策误读为风险。

**错误 3：量化估算表（触发率 3%-18%、综合 15-25%）— 无依据。**
报告自称"基于 TEST3/TEST16 联调数据"，但通篇无样本量、无统计方法、无原始记录，末尾又承认"非实测结果"。这类精确到百分位的估算最容易被引用为事实，建议从报告中删除或降级为定性判断。

### 2.3 重要遗漏（2 项）

**遗漏 1：截断≠信息丢失——完整输出始终存盘可找回，报告整条"信息损失链路"论证打了折扣。**
- `executor.py:1247-1258`：截断时完整输出保存到 `.hiveweave/tool_outputs/<agent>_<ts>_<tool>.txt`，marker 中**附带文件路径**；
- `token_utils.py:105-113` 同样存系统临时目录并附路径。
即 agent 随时可 `read_file` 找回全量内容。真实风险是"**agent 不知道/不主动去读**"，而非"信息被销毁"。这把 P0-3 的性质从"补信息"变成了"补指引"——修复成本低一个数量级（见方案）。

**遗漏 2：compaction 摘要 prompt 已有精确保留规则，"内容防护近乎空白"表述过绝对。**
`compaction.py:172` 摘要模板含 `Preserve exact file paths, commands, error strings`。提示词层防护存在，缺的是**运行时校验**——报告的"零拦截"仅对运行时成立。

---

## 三、解决方案（修正版，按 ROI 排序)

### P0 — 本周可做，成本低收益高

**1. Prune 占位符结构化**（报告 P0-2，完全采纳）
改 `store.py:456`，占位符携带元数据，消除"脑补空间"：
```python
messages[i] = {**messages[i], "content":
    f"[Tool '{tool_name}' output pruned at turn {turn_no}. "
    f"Original: {n_lines} lines / {n_bytes}B. Status: {ok_or_fail}. "
    f"First line: {first_line[:120]}]"}
```
注意保留精确前缀匹配 `"[Old tool result content cleared]"` 的向后兼容（`store.py:421` 依赖该串判停），新格式需同步更新该判断。

**2. 摘要加时间锚点 + 降温**（报告 P0-1，采纳并具体化）
- `compaction.py:28` `SUMMARY_TEMPERATURE = 0.3 → 0.1`（摘要不需要创造性，与 prompt-engineering-expert 技能一致）；
- `compaction.py:149` 摘要消息头改为：`[Earlier conversation summary — covers turns 1..N, generated at game-day T. States described may be stale; re-verify files/tasks before relying on them.]`。

**3. 截断 marker 改为主动指引**（修正报告 P0-3，利用已遗漏的存盘机制）
不改截断策略，只改 `executor.py:1255-1258` marker 文案：
```
... [output truncated: {lines} lines, {bytes} bytes.
Full output saved to {file_path} — read_file it if you need exact values (test counts, error lines) NOT visible in this preview] ...
```
一句话告诉 agent"何时该去读全量"，比报告原方案（重构截断保留策略）便宜得多，且不动摇已验证的契约 02。

### P1 — 1-2 周

**4. commit_turn 时做文件路径存在性校验**（采纳报告 P1-4，但收窄到最高 ROI 的一项）
只做"agent 声明中提到的文件路径 → `os.path.exists`"单项检查，失败注入提醒而非阻断。数值对比、因果校验暂缓——假阳性成本会吃掉收益。这与项目既有的"检测层优先于提示词层"入账纪律一致。

**5. 报告原 P1-5（LLM-as-Judge 跨turn一致性）与 P1-6（摘要回验）降级为 P2。**
两者都是额外 LLM 调用，成本与误报率未定。先用 P0-2 的 turn 编号锚点把"定位矛盾"变得可行，再考虑自动检测。

### P2 — 中期

**6. 工具输出注入检测**（采纳报告 P2-9）
对 read_file/bash 输出做轻量模式匹配（`[SYSTEM]`、`Ignore (all )?previous instructions` 等），命中加隔离标记。当前无对抗输入场景，不急，但它是唯一"零防护"项，值得排进 backlog。

**7. 置信度标记**（采纳报告 P2-7，但先试点）
仅在 coordinator 的 review_task 结论中试点 `[confidence: high/med/low]`，验证前端展示与 agent 依从性后再推广——直接全量要求标注大概率沦为形式主义。

### 不建议做

- ❌ 修 token 高估——是特性不是 bug（见错误 2）。
- ❌ 立即上 RAG / 语义缓存——报告 P2-8 与 attestation 机制职责重叠，先把 P0 做完看数据再说。

---

## 四、给报告作者的修订建议

1. 删除或定性化第三节"量化估算"表；
2. 修正 doom loop 描述为"分级阈值 + 首警告后中断"；
3. 信息损失链路图补充各层的"存盘找回"出口，重新评估"数值幻觉 65 分"——有找回通道后建议下调至 50-55；
4. "内容防护近乎空白"改为"运行时内容校验空白"（提示词层已有 HONESTY_BLOCK + 摘要保留规则）。

---

## 附：审计使用的技能

- **prompt-engineering-expert**（本地市场安装至 `~/.workbuddy/skills/`）：幻觉分类法（Issue 2: 来源引用 / 置信度 / 上下文接地）、上下文管理反模式、Testing Checklist。SkillHub 远程检索无更垂直的"agent 幻觉治理"技能（检索词 prompt hallucination / agent stability / context engineering 均无相关结果），该技能为当前最优可得框架。
