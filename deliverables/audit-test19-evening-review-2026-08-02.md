# TEST19 晚轮报告三审：证据复核 + 遗漏挖掘 + 方案评估

审计人：一元（WorkBuddy）| 日期：2026-08-02
对象：《TEST19 修复验证 + TOKEN 浪费分析》（另一 AI 产出，针对 2026-08-02 晚轮演练库）
方法：TEST19 `.hiveweave/data.db` 全量复核 + HiveWeave 平台源码交叉验证 + tool_outputs 物证

---

## 一、总评

**数据库背景先声明**：晚轮库（4 任务 / 4 handoffs / 53 runs / 0 error）是二审行动清单落地后的**修复验证演练**（T1 标签污染对照、T2 VERIFY 对照、T3 归档推送），与早轮（10 任务 / 116 runs / 402 连环）是两个数据集。报告未声明这一点，"未发现死锁/卡死"容易被误读为对早轮 P0-2 审批死锁的否定——它只是本轮没复发。

**结论：问题没挖完，方案一半打偏。**
- ✅ 属实：T2 修复、数据快照（4 任务/4 handoffs）、②③④ 的大部分量化。
- ❌ 关键误判 1：把"截断保护生效、回传仅 ~2KB"列为"已做对的"——**实际截断被单行超长输出绕过，75KB 几乎全文回传，是本轮最大单点浪费**。
- ❌ 关键误判 2：①把"消息驱动唤醒"讲成"超时轮询"——29 次 wait 仅 1 次超时，药方治错了机制。
- ❌ 漏报 2 个可操作平台 bug：commit_turn 门禁×UUID 寻址死循环（撞满 600s safety_timeout）、files_changed 前导点被 `lstrip("./")` 吃掉（磐石已在汇总信里标注，报告漏看）。

---

## 二、逐项复核

### 1. 测试修复（T2 改诚实双断言，4 passed）——属实，定性需收紧

- 4 passed 属实（2 文件 × 2 断言；两文件的断言设计为不可能失败的诚实双断言）。
- **但这不是"修复"，是"承认缝隙"**：evidence 不随 merge 上 main 的 POST-MERGE VERIFY 证据落点问题（二审清单 ⑥，P2）仍然开着。报告把 T2 定性为"唯一真红已修"，会误导后续优先级。
- **T2 修复未提交**：`git status` 显示 `tests/test_c74fa450_evidence.py` 仍 modified（HEAD 是 T1 的 e2e07c3）。建议尽快 commit。

### 2. "DB 未发现死锁/卡死"——口径属实但有漏报

实测吻合：任务 4 条、handoffs 4、无孤儿 streaming、obligations 7 条全 fulfilled、无 402、无 agent_events 异常。
**漏报**：磐石 run `8adac99a` 以 `interrupted / safety_timeout` 撞满 600s 收场（见三-A）。不是"卡死"，但是一次实打实的门禁死循环——"运行时无异常"的结论不成立。

### 3. TOKEN 浪费四条——逐条核验

#### ① 等待-唤醒"超时轮询"（报告称最大头）——机制讲反了

| 报告表述 | 实测 | 判定 |
|---|---|---|
| "琥珀对磐石" | 等待者是 CEO **归零**（8766c772）；琥珀是早轮 agent，本轮不存在 | ❌ 张冠李戴 |
| "循环 22 次" | 0fa872e2 相关 wait **10 次**；22 = 归零全部 wait 数（含等天线×3、等 Vera×2）；17 = 归零等磐石数 | ❌ 三种口径混用 |
| "约 60s 一轮、每轮超时唤醒" | 29 次 wait 仅 **1 次**超时唤醒（磐石等 Vera，904s 到期）；28 次 `cleared_at < expires_at` 是**消息事件唤醒**——60s 超时几乎从未触发，消息总先到 | ❌ 机制说反 |
| "持续 33 分钟" | 0fa872e2 链条跨度 24.9 分钟 | ⚠️ 口径不明 |
| "每轮跑完整 LLM 回合、产出为零" | 每次唤醒确实跑完整回合（归零 26 个 completed runs），但窗口内磐石 9 条消息大多为实质内容（ask 正式回复、VERIFY 失败上报、最终汇总、a/b/c 裁定请求）——**不是零产出空转，是事件驱动的协作成本** | ⚠️ 半真 |

**药方评估**：超时唤醒→LLM 回合的机制在代码里真实存在（`game_time.py:535` clear_expired → `[WAIT_TIMEOUT]` urgent + `_watchdog_trigger`），"超时无新信息→静默 re-arm / 指数退避"作为加固方向成立，但**对本轮观测到的浪费几乎无效**（超时只发生 1 次）。真正的对应修复是三-A（重复回执治理）+ 低信息量消息唤醒合并（wake coalescing）。**降级为 P3。**

#### ② 同 run 重复工具调用——方向对，规模夸大，最强证据误列

| 报告表述 | 实测 | 判定 |
|---|---|---|
| read_file 同文件 6/5/3 次 | 同 args_hash 6/5/3 次没错，但 **distinct run = 6/5/3——全是跨 run 各读一次**，不是同 run 重复。跨 run 重读是新上下文的合理行为 | ❌ 证据误列 |
| get_tasks 空参 19 次 | **23 次跨 19 runs**（平均 1.2 次/run，非同 run 重复） | ⚠️ 数字与定性都偏 |
| get_platform_state 14 次 | ✅ 14 次跨 7 runs；同 run 同参最多 ×3（3 个 run），真实同 run 冗余 | ✅ |
| 同 run 连续 3 次 list_available_skills | ✅ 实锤：天线 run `874af4b6` step1/2/3 连续 3 次，各 ~7s | ✅ |

同 run 同参真实冗余合计 ≈ 10 次 / 347 总调用 ≈ **3%**。
**药方评估**：tool_args_hash 缓存可行（`run_steps` 已记录 `tool_args_hash`/`result_hash`，基建现成），但必须：(a) 只限只读白名单工具；(b) 同 run 内任何写操作后缓存失效（自己 create_task 后 get_tasks 必须见新数据）；(c) 注意 `doom_loop.py:7-42` 的只读豁免集合是**有意设计**——注释明写"agent 没有订阅机制，轮询是它获取状态的唯一手段"，报告未意识到这层张力。边际收益 ~3%，**P3**。

#### ③ 大 turn 放大——属实但口径可议

磐石 turn 8 = 47 条 / 224,352B / ≈8,726 tokens ✅。但**按 token 最大是天线 turn 1：20,189 tokens**（102KB，11 条）——CJK 占比高时字节≠token，报告按字节排序漏掉了 2.3 倍大的 token 峰值。而天线这个 turn 正是被下一条的 75KB 预览喂大的。

#### ④ thinking 1MB——结论对，机制说得不对，且漏了一个硬化点

- content 95,874B / thinking 1,013,576B ✅；0/55 turns 含 `"thinking"` 键 ✅ 不注入。
- **但真相更微妙**：1MB thinking 以 `reasoning_content` 键存进了 `conversation_turns.raw_messages`（235 条消息，总量 1,013,576 chars 与 thinking 分毫不差）。OpenAI 格式 `build_body`（provider.py:195）只规范化 images，**不剥 reasoning_content**——它会被序列化进请求体。省 token 靠的是 OpenAI 兼容服务端忽略未知字段，**不是"store 层剥离"**；Anthropic 格式则真的作为 thinking blocks 发送（provider.py:641-643，有意保持思维链）。
- 且 `estimate_tokens_for_messages`（token_utils.py:54）**不统计 reasoning_content**——预算裁剪对它失明。换一家把 reasoning_content 计入的 provider，账单立炸。
- **建议 P2 硬化**：OpenAI 格式出站剥 reasoning_content（仅保留同 run tool loop 续链窗口）；Anthropic 维持。

#### "已做对的：75KB skills 被截断保护，回传仅 ~2KB"——说反了，这是本轮最大单点浪费

实锤链：
1. 天线 list_available_skills（search="git worktree"）结果 74,976B（`run_steps.result_size`，该字段记录的是**截断后**回传长度，streaming.py:245）。
2. 截断门**确实触发**：75,396B 全文保存到 `tool_outputs/b10721a7_..._list_available_skills.txt` ✅（这部分报告说对了）。
3. **但预览≈原文**：该输出只有 24 行，其中一行 **73,353 字符**（skills.sh 市场列表单行 JSON dump）。预览 = head 20 行 + tail 5 行 = **75,490 chars ≈ 100% 回传**。
4. 天线 turn 1 的 tool 消息实测 74,976 chars、内含截断标记原文——铁证。这个 turn 因此成了全库最大 token turn（20,189）。

根因：`_maybe_save_large_output`（executor.py:1374）与 `token_utils.truncate_tool_output`（:120）的预览都**按行不按字节**——单行超长输出（JSON dump/大列表序列化）使截断形同虚设。**两处同病**。
修法（P1）：预览加字节双封顶——每行截 N 字节 + 总预览 ≤ ~4KB； marker 保留。附带：`list_available_skills` 输出格式本身病态（search 过滤后仍 75KB、单行 73KB），可按行结构化（P3）。

---

## 三、报告漏掉的问题

### A. commit_turn 门禁 × UUID 寻址死循环（本轮 P1，运行时最值得修）

磐石 run `8adac99a`（27 步，600s safety_timeout interrupted）逐步实证：
- `commit_turn REJECTED [UNREPLIED_ASKS]` ×5 —— 全轮 16 次同类 REJECTED 的极端样本；
- 期间 `send_message`/`ask_agent` 各 1 次失败：`No active recipients found. Unknown: ['8766c772-1ccc-4dcb-...']` —— **agent 照抄系统消息里的 CEO UUID 当收件人，工具不认**。

根因链：
1. 收件人解析只认 short_id/name/role（orchestration_tools.py:150-201），**无 UUID 通道**；而 `[TASK SUBMITTED]`、ask 契约等平台消息里全是 UUID。
2. 回执送不达 → ask 永远"未回复" → UNREPLIED_ASKS 门禁持续拒 commit → 反复补发 → 撞满 600s 熔断，整轮 600s 燃烧归零。
3. 外溢效应：磐石向归零重发 ~3 次"最终汇总已提交"重复回执——**这正是报告①看到的"等待方反复被唤醒"的另一端**。报告盯着等待方开药，病根在发送方。

值得肯定：REJECTED 文案已带"动作: ask_agent or send_message to sender REF"指引（行动清单 ④ 生效了）——但被寻址 bug 架空。
修法：收件人解析加 UUID 匹配分支（一行）；门禁 REJECTED 提示中识别"已尝试回复但寻址失败"并点名 UUID 问题。

### B. files_changed 前导点被 `lstrip("./")` 吃掉（本轮 P1）

`submit.py:467`：`fc_clean = str(fc).strip().lstrip("./")` —— `.hiveweave/reports/c74fa450/evidence.md` 被剥成 `hiveweave/reports/...`（前导点没了）。连锁后果：
1. `:470` 的 `if ".hiveweave/" in fc_clean` **永假**（前导点已被吃）——设计好的".hiveweave/ invisible file"识别失效；
2. 存在性校验拿错误路径 → 本轮 4 次 `submit_task rejected: ... do not exist on disk: hiveweave/reports/...`，报错把 agent 往"文件不存在"误导，而非"证据放错了地方"；
3. 这是 T2 evidence 提交受挫、agent 群体转而怀疑"evidence 落点"的直接推手之一。

**磐石的最终汇总信（inbox 实测）已把"files_changed 前导点"列为平台复查点——报告作者复核时漏看了这封就在库里的信。**
修法：`lstrip("./")` → `removeprefix("./")`，并把 `.hiveweave/` 判定移到规范化之前。

### C. 次要点名

- `Task must be 'submitted'... but is 'running'` ×1 + `Illegal transition: blocked → submitted` ×1：状态视图滞后（二审 4.2）在行动清单 ⑤ 落地后仍有残留案例，建议按时间戳定位复现路径。
- `cancel_task refused for task c74fa450: review pipe still has execution evidence` ×1：保护性拒绝且文案有指引，属正常工作（TEST18 双堵防护生效）。
- 跨级沟通拒绝 ×2（Vera 直打 CEO 被拦）：组织纪律门正常工作。

---

## 四、修正后的行动清单（按优先级）

1. **P1：截断预览字节双封顶**（executor.py `_maybe_save_large_output` + token_utils `truncate_tool_output` 两处同修；每行截断 + 总预览 ≤ ~4KB）——本轮最大单点浪费，一行 JSON 即可击穿现有防线。
2. **P1：收件人解析支持 UUID**（orchestration_tools.py）+ 门禁识别"寻址失败型未回复"——治 600s 死循环与重复回执。
3. **P1：files_changed 规范化修 `lstrip("./")`**（submit.py:467）——恢复 `.hiveweave/` invisible 识别，消除误导性报错。
4. **P2：OpenAI 格式出站剥 reasoning_content**（保留同 run 续链；Anthropic 不动）。
5. **P3：超时唤醒静默 re-arm / 指数退避**（报告原建议 ①，机制存在但本轮非主因）。
6. **P3：只读工具同 run args_hash 缓存**（报告原建议 ②，加白名单+写失效，收益 ~3%）。
7. **P3：list_available_skills 输出按行结构化**（配合第 1 条）。
8. **遗留未动**：POST-MERGE VERIFY 证据落点（二审 ⑥）——T2 的"诚实绿"是承认缝隙，缝隙本身仍开。
9. **立即做**：commit T2 测试修复（tests/test_c74fa450_evidence.py 未提交）。

---

## 附：三审证据索引

- 等待唤醒：agent_waits 29 行全量（28 行 `cleared_at < expires_at`；唯一超时 9389e343，磐石等 Vera 904s）；0fa872e2 链 1785674568425→1785676061754。
- 死循环：run_steps（run 8adac99a，27 步；commit_turn REJECTED ×5；send/ask UUID 失败 ×2）；agent_runs `interrupted/safety_timeout`。
- 截断绕过：tool_outputs/b10721a7_1785673203069_list_available_skills.txt（75,396B / 24 行 / 最长行 73,353 chars）；天线 turn 1 tool 消息 74,976 chars 含截断标记；conversation_turns approx_tokens 20,189。
- reasoning_content：raw_messages 235 条含该键、总 1,013,576 chars（= chat_messages.thinking 总量）；token_utils.py:54 未统计。
- 前导点：submit.py:467/470/473-474；run_steps 4 次 `do not exist on disk: hiveweave/reports/...`（无前导点）。
- 重复回执：inbox 磐石→归零（1785675303563 / 1785675457086 / 1785675505406 三条"已提交"同义回执）。
- 重复调用：run 874af4b6（天线）step1/2/3 list_available_skills 各 ~7s；get_platform_state 同参 ×3（runs 20226eee/2270edf3/d1cdddff）。
- 代码：executor.py:1374-1413（预览无字节帽）、token_utils.py:120-147（同病）、orchestration_tools.py:150-201（无 UUID 匹配）、provider.py:195/641、wait_contract.py + game_time.py:528-671（超时唤醒机制）、doom_loop.py:7-49（只读豁免设计）。
