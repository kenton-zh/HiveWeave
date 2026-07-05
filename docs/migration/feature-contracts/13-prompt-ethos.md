# 功能契约 13：ETHOS 提示词体系

> **本文件是功能契约（层 1），用 spec 语言描述模块的行为契约。**
> **任何 AI 工具实现 Python 版本时，必须满足此契约中的所有要求。**

## 元信息

| 项 | 值 |
|---|---|
| 模块编号 | 13 |
| 模块名称 | ETHOS 提示词体系 |
| Elixir 源码 | `llm/streamer.ex`：`build_messages` + `build_identity_prompt` + `build_context_prompt` + `build_involvement_block` + `build_coordinator_prompt`（CEO/HR/Generic 三分支）+ `build_executor_prompt`（分发器 → 6 子函数）+ `poll_and_inject_inbox` + `build_max_rounds_summary` |
| TS 参考源码 | `packages/core/src/streamer.ts`（部分实现） |
| OpenCode 参考源码 | `D:\PC_AI\Project\opencode\packages\opencode\src\session\system.ts`（语言规则部分） |
| 状态 | 草稿（v2 — 架构审查后大幅修订） |

## 功能概述

三层提示词架构：ETHOS 共享层 → 角色类型约束层 → 角色专属剧本层。`build_identity_prompt` 是静态的（同一 agent 跨 turn 不变，prefix cache 友好），`build_context_prompt` 是动态的（每轮从 memories/skills/goals/involvement 重建）。包含三原则、9 种角色专属剧本（CEO/HR/Generic Coordinator + 6 种 Executor 子类型）、CAVEMAN 沟通纪律、6 种组织范式完整定义、7 阶段开发生命周期、工具权限矩阵。

## 消息布局（DeepSeek 前缀缓存友好）

**关键设计**：context 放在 history **之后**，而非之前。这样 `[sys_identity + tools + stable_history]` 前缀可被 LLM API 缓存。如果动态 context 放在前面，每轮都会破坏 tools（~20KB）的缓存。

```
[System 1] identity prompt（常量，同一 agent 不变）→ prefix cache hit
[User/Assistant/Tool...] conversation history（过滤 system 消息）→ prefix cache hit（稳定部分）
[System 2] context prompt（动态，每轮可能变化）→ prefix cache miss
[User] 当前消息（前缀 "[来自: 用户] "）→ prefix cache miss
```

**compaction summary**：不是独立的 System 3。在 token 溢出时，由 `trim_context_if_needed` 作为 `[Earlier conversation summary]` 插入 history **中间**（head ++ [summary_msg] ++ to_keep），非每轮存在。

**context 为 nil 时**：跳过 System 2，直接 `[Sys1] ++ history ++ [user]`。

## 接口契约

### 输入

| 输入 | 来源 | 格式 | 说明 |
|---|---|---|---|
| agent | OrgService | `{id, name, role, permission_type, goal, backstory, bound_skills, ...}` | agent 元信息 |
| model | DB model registry | `{model_id, ...}` | 用于判断是否中文模型 |
| goals | Charter | `{objective, focus, keyResults, userInvolvement}` | 项目目标和用户参与度 |
| history | ConversationStore | `[{role, content, ...}]` | 过滤 system 消息后的历史 |
| inbox（动态） | InboxService | `[{message, priority, ...}]` | tool loop 每轮注入未读消息 |

### 输出

| 输出 | 目标 | 格式 | 说明 |
|---|---|---|---|
| identity prompt | 消息列表第 1 条 | `{role:"system", content:"..."}` | 静态，prefix cache 友好 |
| context prompt | 消息列表倒数第 2 条 | `{role:"system", content:"..."}` \| nil | 动态，每轮重建 |
| inbox 注入消息 | tool loop 中间 | `{role:"system", content:"## ⚡ 紧急中断..."}` | 按优先级注入 |

## 核心流程

### build_identity_prompt（静态）

```
1. 基本信息：You are "<name>", a <role> in the HiveWeave engineering organization.
2. 角色目标：## Your Role\n<goal>（如有）
3. 角色背景：## Background\n<backstory>（如有）
4. ETHOS 工程准则（所有角色共享）：
   a. 原则 1: Boil the Lake — 完整实现，边界处理不能"以后再说"
      - 湖（可煮沸）：100% 测试覆盖、完整边界处理
      - 海洋（不可煮沸）：整体重写、跨季度迁移
      - 反模式列举
   b. 原则 2: Search Before Building — 先搜索成熟模式
      - Layer 1: 验证过的成熟模式 → 直接用
      - Layer 2: 新流行的实践 → 审视后用
      - Layer 3: 第一性原理推导 → 最有价值
   c. 原则 3: User Involvement（可调）— 让渡决策权，不让渡诚实义务
5. 通用验证文化：每个动作必须有证据支撑
6. 通用反合理化表（4 条）
7. .hiveweave 目录保护规则
8. 权限级别：coordinator / executor
9. 角色专属 prompt（build_coordinator_prompt / build_executor_prompt 分发）
10. 诚实与完整性规则（零容忍）
11. 决策规则（不自主做方向性决策）
12. 通信规则（花名称呼、统一消息格式、群发支持）
13. 行动纪律（说到做到、工具调用前写说明）
14. 语言规则（中文模型追加，参考 OpenCode system.ts）
```

### build_context_prompt（动态）

```
1. build_involvement_block(agent):
   a. 从 charter 读 userInvolvement（high/medium/low）
   b. 默认 high（streamer 兜底），但 charter.ex 写入 DB 时默认"宏观决策+技术选型"（medium）
   c. 按级别格式化行为规则
2. build_goals_block_if_dirty(agent):
   a. 检查 goals_dirty（goals 自上次 agent 读取后是否更新）
   b. dirty → 注入完整 goals workbook + 标记已读
   c. not dirty → 返回 nil
3. build_memory_block(agent):
   a. MemoryService.build_agent_context(project_id, agent_id)
4. build_active_skills_section(bound_skills):
   a. SkillRegistry 构建技能摘要段（只注入摘要，agent 运行时调 read_skill 加载完整指令）
5. 拼接非空部分为 system 消息
```

### format_goals_block 输出格式

```
## Enterprise Goals Workbook (updated)
**Objective:** <objective>
**Current Focus:** <focus>
**Key Results:**
  - [<status>] <text>
**User Involvement:** <involvement>
Route decisions matching the user-involvement scope to the user... For decisions outside this scope, ask your superior...
```

### 技能注入格式

```
## Active Skills
The following skills are bound to you. Each shows only a summary here.
When a task matches a skill, use `read_skill("<slug>")` to load its full instructions...
- **<slug>**: <description>
```

**关键设计**：只注入摘要，agent 运行时调 `read_skill` 加载完整指令（节省 context）。

### Inbox 动态注入（tool loop 每轮）

| 优先级 | 注入条件 | 格式 |
|---|---|---|
| urgent | tool loop 每轮检查 | `## ⚡ 紧急中断 — 需要切换任务` + 3 步指令（todowrite 保存→处理→恢复） |
| normal | tool loop 每轮检查 | `## 工作期间收到的新消息` + `[来自: 花名] [需要回复]: "内容"` |
| low | 不注入 | 留待下次 trigger |

### Max-rounds 提醒与摘要

- **Mid-round reminder**（80% 轮次时）：注入 system 消息 `⚠️ You have N tool calls remaining. Start wrapping up...`
- **Max-rounds summary**（轮次耗尽时）：发起无工具的独立 LLM 请求，强制生成摘要（what done / what pending / next steps）

## 用户参与度三级

| 级别 | 技术决策 | 产品/业务决策 | 重大方向变更 | 适用场景 |
|---|---|---|---|---|
| high | 必须问用户 | 必须问用户 | 必须问用户 | 用户有技术能力且想掌控方向 |
| medium | AI 自主 | 必须问用户 | 必须问用户 | 用户懂产品不懂技术 |
| low | AI 自主 | AI 自主 | 仅通知用户 | 用户完全信任 AI |

**默认值冲突**：streamer 兜底为 `high`，但 charter.ex 写入 DB 时默认"宏观决策+技术选型"（medium）。Python 迁移以 charter.ex 的 medium 为准，streamer 兜底 high 仅在 charter 完全缺失时生效。

**不变部分**：无论哪个级别，AI 都不能伪造结果、隐藏风险、跳过验证。

### 语言规则（中文模型）

```
1. 检测模型是否中文训练：deepseek/kimi/qwen/glm/yi-/doubao/ernie/hunyuan
2. 中文模型 → 追加 "use the SAME language as the user"
3. 西方模型（Claude/GPT/Gemini）→ 不追加（信任模型自动镜像用户语言）
4. 参考 OpenCode system.ts:26-40
```

## 角色专属 prompt — Coordinator 层（3 分支）

### CEO 分支（`normalized == "ceo"`）

包含以下专属内容：

**1. Mission**：维护企业目标工作簿（read_goals/update_goals）+ 设计章程（read_charter/save_charter）+ 选择组织范式 + 委派招聘给 HR + 协调管理者 + 管理开发生命周期

**2. 组织范式库（6 种，每种 6 字段完整定义）**：

| 范式 | 规模 | 层级 | 协调层 | 适合 | 不适合 | 必经流程 |
|---|---|---|---|---|---|---|
| solo（单兵） | 1人 | 1层 | 无 | 目标明确单一、脚本/工具、MVP | 多领域专业知识、长周期 | DEFINE→BUILD→VERIFY→REVIEW（自审）→SHIP |
| flat_squad（扁平小组） | 2-5人 | 1层 | 无 | 小型项目、原型/POC、快速迭代 | 跨团队协调、严格质量门禁 | DEFINE（共商）→BUILD（并行）→REVIEW（交叉审）→SHIP |
| tech_lead | 3-8人 | 2层 | 有 | 纯技术项目、库/框架/SDK | 非技术管理、多业务线 | PLAN（Lead规划）→BUILD→VERIFY→REVIEW（Lead审）→SHIP |
| pm_architect | 5-15人 | 3层 | 有 | 中大型项目、多领域协作 | 小项目、纯技术探索 | DEFINE（PM）→DESIGN（架构师）→BUILD→VERIFY→REVIEW→SHIP |
| pod（小组制） | 8-20+人 | 3层 | 有 | 大型项目、多领域自治 | 小项目、单一领域 | Pod内flat_squad；Pod间PLAN→INTEGRATE→REVIEW→SHIP |
| pipeline（流水线） | 4-10人 | 2层 | 有 | 严格阶段依赖、合规、瀑布 | 快速迭代、弱依赖 | DEFINE→BUILD→VERIFY→REVIEW→SHIP，每阶段有门禁 |

**3. Org Design Rules**：三层默认（CEO→Manager→Engineer）、HR 永远无子节点、管理幅度 3-7 人、范式匹配项目规模

**4. Hiring Flow（MANDATORY）**：设计组织→list_subordinates 找 HR→send_message 发招聘需求→等待 HR 回报→send_message 分配工作。**CEO 永远不能直接 hire_agent**

**5. Development Lifecycle（7 阶段）**：

| 阶段 | 必读技能 | 产出 |
|---|---|---|
| EXPLORE | 无（list_files/read_file/grep/read_goals/read_charter/read_project_memory） | 项目状态评估 |
| DEFINE | spec-driven-development | 完整 spec（含边界、错误路径） |
| PLAN | planning-and-task-breakdown | 原子化任务（含验收标准） |
| BUILD | dispatch to executors（incremental-implementation + test-driven-development） | 代码（含边界处理） |
| VERIFY | debugging-and-error-recovery（如需） | 测试输出（附在报告中） |
| REVIEW | code-review-and-quality + security audit | 五轴审查完成 |
| SHIP | shipping-and-launch | 测试通过+无回归+文档更新 |

**Phase 0 EXPLORE 子步骤**：
- Step 0.0 Search Before Building：先搜索常见组织模式
- Step 0.1 评估项目状态：list_files/read_file/read_goals/read_charter
- Step 0.2 分支决策：空项目→直接问用户；有基础→深入探索后只问方向
- **IRON RULE**：工作区已回答的问题不得问用户

**Boil the Lake 完整性检查（每阶段退出标准）**：DEFINE spec 完整 / PLAN 任务原子化 / BUILD 含边界 / VERIFY 附测试输出 / REVIEW 五轴完成 / SHIP 测试+无回归+文档

### HR 分支（`normalized == "hr"`）

包含以下专属内容：

**1. 招聘流程**：Search Before Building 招聘前必做（先搜索模板库）→ list_agent_templates → hire_agent（传 templateId 预填）

**2. Recruitment Skill Standards 表**（按角色绑定技能）：

| 角色 | 技能 slug |
|---|---|
| CEO | spec-driven-development, planning-and-task-breakdown |
| HR | skill-creator |
| Tech Lead | source-driven-development, code-review-and-quality |
| Developer | test-driven-development, incremental-implementation |
| Reviewer | code-review-and-quality, security-and-hardening |

**3. Naming & Position Rules**：花名规则（两字诗意昵称）、中文职位名、name=花名/role=职位

**4. Backstory 要求**：2-4 句个人叙事，非项目相关，含过往经验/性格/爱好

**5. 招聘质量门**：hire 后必须验证 role/skills/goal/backstory，不合格则 dismiss 重招

**6. Name Reporting Rule**：报告招聘结果必须用工具返回的精确花名，禁止自创

**7. IRON RULE — HR NEVER has children**：HR 是服务角色，不是组织管理者

### Generic Coordinator 分支（默认）

包含：Review & Quality Gate（关键模块派 Reviewer）+ 反合理化表（3 条：能跑就approve/任务小/口头确认）+ 验证清单（3 项）

## 角色专属 prompt — Executor 层（6 子函数）

`build_executor_prompt` 是分发器，按 role 路由到 6 个子函数：

| role 匹配 | 子函数 | 角色定位 |
|---|---|---|
| test_engineer | build_test_engineer_prompt | 测试工程师（不写应用代码） |
| code_reviewer | build_code_reviewer_prompt | 代码审查员（五轴评审） |
| security_auditor | build_security_auditor_prompt | 安全审计员（OWASP/STRIDE） |
| web_perf_auditor | build_web_perf_auditor_prompt | Web 性能审计员（Core Web Vitals） |
| reviewer/inspector/审查员/qa/qa_engineer/测试专员 | build_inspector_prompt | 通用审查员（质量门禁） |
| 其他（默认） | build_generic_executor_prompt | 通用执行者 |

### 各角色纪律差异

| 角色 | 铁律 | 输出格式 | 反合理化表 | 验证清单 |
|---|---|---|---|---|
| Test Engineer | 不写应用代码/Beyoncé Rule/测试金字塔80-15-5/DAMP | Summary/Failures/Regressions/Recommendation | 3条 | 3项 |
| Code Reviewer | 不写代码/3次拒绝升级/100行拆分/staff标准 | Verdict(APPROVE/CHANGES/REJECT)/Critical/Warnings/Nitpicks | 3条 | 3项 |
| Security Auditor | 8/10置信度/exploit场景/Critical立即升级/17项误报排除 | Verdict+CWE+CVSS+exploit+修复 | 2条 | 3项 |
| Web Perf Auditor | 指标诚实/Core Web Vitals目标/Quick+Deep双模式 | Verdict+CWV表格+瓶颈分析 | 2条 | 3项 |
| Inspector | Audit Memory（write_memory） | path:line: severity: problem | — | 1项 |
| Generic Executor | 先调查后修复/完整实现/测试先行/DAMP | — | 3条 | 3项 |

### CAVEMAN 沟通风格（所有角色共有）

每个角色 prompt 末尾都有"Communication Style — STRICT DISCIPLINE"段：

**对上级（send_message）**：CAVEMAN 风格。无客套、无赞美、无流程叙述。
- **BANNED 短语**："干得漂亮" "很好" "辛苦了" "让我" "看起来" "I will now" "let me" "great work"
- **只说**：做了什么、发现什么、下一步

**对用户**：完整句子，仅报结论，无逐步叙述，最多 2-3 句。

**Reply Routing Rule**：
- team_chat 回复只给该 agent
- 问用户必须用 `question` 工具
- 禁止混 channel（不能在 team_chat 回复中夹带给用户的问题）

### Identity Relationships（Generic Executor 专属）

三个身份必须区分：
- **"user"** = 人类操作员（非 CEO、非上级），是项目最终决策者
- **"superior"** = 派发任务的 agent
- **"self"** = name (role)，禁止第三人称自称

消息中"user"永远指人类操作员，不指 CEO 或其他 agent。

## 角色纪律四件套（每个角色必备，但内容因角色而异）

| 组件 | 说明 |
|---|---|
| 何时不做 / 铁律 | 明确列出不该做的事（如 executor 不 spawn agent，test engineer 不写应用代码） |
| 输出格式 | 规定输出格式和规范（每个角色不同） |
| 验证清单 | 每阶段必须通过的检查项（每个角色不同） |
| 反合理化表 | 常见借口 + 反驳（每个角色 2-3 条，内容不同） |

## Time-Context 注入（三方不一致）

| 实现 | 状态 |
|---|---|
| TS 参考实现 | 有 `buildTimeContextBlock` / `prefixTriggerMessage` / `prefixInterAgentMessage` |
| Elixir 实现 | **完全未实现**，游戏时间不注入任何 prompt |
| 契约 13（原版） | 未提及 |
| AGENTS.md | 列为 service |

**Python 迁移决策**：待定。需用户确认是否在 Python 版中实现 time-context 注入。

## 常量引用

| 常量 | 值 | 所在章节 |
|---|---|---|
| 默认 userInvolvement（streamer 兜底） | `high` | ETHOS 提示词 |
| 默认 userInvolvement（charter 写入） | "宏观决策+技术选型"（medium） | Charter 服务 |
| 中文模型列表 | deepseek/kimi/qwen/glm/yi-/doubao/ernie/hunyuan | 同上 |
| goals dirty 检查 | 每轮检查 goals 版本 | 同上 |
| join 历史消息数 | `50` 条 | 实时通信 |
| max-rounds 提醒阈值 | 80% 轮次 | 同上 |
| inbox urgent 注入 | 每轮 | 同上 |
| inbox normal 注入 | 每轮 | 同上 |
| inbox low 注入 | 不注入 | 同上 |

## 已知问题

| 问题编号 | 说明 | Python 迁移处理 |
|---|---|---|
| — | identity prompt 是静态的，设计为 prefix cache 友好 | 确保 Python 侧同一 agent 的 identity prompt 跨 turn 不变 |
| — | context prompt 放在 history 之后（非之前）以保护 prefix cache | 确保 Python 侧消息布局为 [Sys1][history][Sys2][user] |
| — | compaction summary 非独立 System 3，插入 history 中间 | 确保 Python 侧压缩摘要插入位置正确 |
| — | goals dirty 机制用 ETS 版本号缓存 | Python 用内存字典或 Redis |
| — | 中文模型检测用 model_id 字符串匹配 | 保留此逻辑 |
| — | userInvolvement 默认值冲突（streamer high vs charter medium） | 以 charter medium 为准，streamer high 仅兜底 |
| — | time-context 三方不一致（TS有/Elixir无/契约未提） | 待用户确认是否实现 |
| — | get_project_language 是 dead code（不被任何 prompt 调用） | Python 不实现 |
| — | build_system_prompt 是废弃函数（向后兼容保留） | Python 不实现 |

## 验收标准

### 消息布局
- [ ] 消息布局为 `[Sys1 identity] → [history] → [Sys2 context] → [user]`（context 在 history 之后）
- [ ] context 为 nil 时跳过 System 2
- [ ] compaction summary 插入 history 中间，非独立 System 3
- [ ] user 消息前缀 "[来自: 用户] "

### Identity Prompt
- [ ] 包含三原则（Boil the Lake / Search Before Building / User Involvement）
- [ ] 包含通用验证文化 + 反合理化表
- [ ] 包含 .hiveweave 目录保护规则
- [ ] 包含诚实与完整性规则（零容忍）
- [ ] 包含决策规则（不自主做方向性决策）
- [ ] 包含通信规则（花名称呼、群发支持）
- [ ] 包含行动纪律（说到做到、工具前写说明）
- [ ] 同一 agent 跨 turn 不变（prefix cache 友好）

### Coordinator Prompt
- [ ] CEO 分支包含组织范式库（6 种 × 6 字段）
- [ ] CEO 分支包含 Development Lifecycle（7 阶段 + Phase 0 EXPLORE）
- [ ] CEO 分支包含 Hiring Flow + IRON RULE（不直接 hire）
- [ ] CEO 分支包含 Boil the Lake 完整性检查
- [ ] HR 分支包含 Recruitment Skill Standards 表
- [ ] HR 分支包含 Naming Rules + 招聘质量门 + HR NEVER has children
- [ ] Generic Coordinator 分支包含 Review & Quality Gate

### Executor Prompt
- [ ] build_executor_prompt 是分发器，按 role 路由到 6 子函数
- [ ] test_engineer：Beyoncé Rule + 测试金字塔 + DAMP + 不写应用代码
- [ ] code_reviewer：五轴评审 + staff 标准 + 100 行拆分
- [ ] security_auditor：8/10 置信度 + exploit 场景 + 17 项误报排除
- [ ] web_perf_auditor：Core Web Vitals 目标 + Quick/Deep 双模式
- [ ] inspector：Audit Memory + one-line-per-finding 格式
- [ ] generic_executor：Identity Relationships + 先调查后修复 + 技能自主添加

### CAVEMAN 沟通纪律
- [ ] 所有角色包含 CAVEMAN 风格段
- [ ] BANNED 短语清单完整
- [ ] 对上级 vs 对用户双轨风格
- [ ] Reply Routing Rule（team_chat 回复隔离 + question 工具专用）

### Context Prompt
- [ ] 每轮重建，包含 involvement + goals + memory + skills
- [ ] goals dirty 时注入完整 workbook 并标记已读
- [ ] format_goals_block 含路由指令（决策路由到 user 还是 superior）
- [ ] 技能注入只含摘要，运行时调 read_skill 加载完整指令

### 动态注入
- [ ] inbox urgent 优先级注入 3 步指令
- [ ] inbox normal 优先级注入消息列表
- [ ] inbox low 优先级不注入
- [ ] 80% 轮次时注入 mid-round 提醒
- [ ] 轮次耗尽时发起 max-rounds summary

### 语言规则
- [ ] 中文模型追加语言规则
- [ ] 西方模型不追加语言规则

## Python 实现建议

- `def build_identity_prompt(agent, model) -> dict` 返回 `{"role": "system", "content": ...}`
- `def build_context_prompt(agent) -> dict | None` 返回 `{"role": "system", "content": ...}` 或 None
- `def build_messages(agent, message, opts, history, model) -> list` 按布局 `[Sys1] + history + [Sys2] + [user]` 拼接
- 提示词模板用 Python 多行字符串（f-string 或 template）
- 9 种角色 prompt 各一个函数，build_coordinator_prompt/build_executor_prompt 做分发
- goals dirty 检查用内存字典（对应 Elixir ETS）
- 中文模型检测用字符串匹配
- inbox 动态注入在 tool loop 中实现，不在 build_messages 中
- 参考 OpenCode `system.ts` 的语言规则实现
