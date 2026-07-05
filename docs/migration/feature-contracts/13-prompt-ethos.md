# 功能契约 13：ETHOS 提示词体系

> **本文件是功能契约（层 1），用 spec 语言描述模块的行为契约。**

## 元信息

| 项 | 值 |
|---|---|
| 模块编号 | 13 |
| 模块名称 | ETHOS 提示词体系 |
| Elixir 源码 | `llm/streamer.ex`：`build_identity_prompt` + `build_context_prompt` + `build_involvement_block` + `build_coordinator_prompt` + `build_executor_prompt` |
| TS 参考源码 | `packages/core/src/streamer.ts`（部分实现） |
| OpenCode 参考源码 | `D:\PC_AI\Project\opencode\packages\opencode\src\session\system.ts`（语言规则部分） |
| 状态 | 草稿 |

## 功能概述

三层提示词架构：ETHOS 共享层 → 角色类型约束层 → 角色专属剧本层。`build_identity_prompt` 是静态的（设计为 LLM API prefix cache 友好，同一 agent 跨 turn 不变），`build_context_prompt` 是动态的（每轮从 memories/skills/goals/involvement 重建）。包含三原则、角色纪律四件套、工具权限矩阵、组织范式流程节点。

## 接口契约

### 输入

| 输入 | 来源 | 格式 | 说明 |
|---|---|---|---|
| agent | OrgService | `{id, name, role, permission_type, goal, backstory, bound_skills, ...}` | agent 元信息 |
| model | DB model registry | `{model_id, ...}` | 用于判断是否中文模型 |
| goals | Charter | `{userInvolvement, ...}` | 项目目标和用户参与度 |

### 输出

| 输出 | 目标 | 格式 | 说明 |
|---|---|---|---|
| identity prompt | 消息列表第 1 条 | `{role:"system", content:"..."}` | 静态，prefix cache 友好 |
| context prompt | 消息列表第 2 条 | `{role:"system", content:"..."}` \| nil | 动态，每轮重建 |

## 核心流程

### 消息布局（DeepSeek 前缀缓存友好）

```
[System 1] identity prompt（常量，同一 agent 不变）→ prefix cache hit
[System 2] context prompt（动态，可能变化）→ prefix cache miss
[System 3] compaction summary（如有，压缩后变化）→ prefix cache miss
[User/Assistant/Tool...] conversation history
```

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
9. 角色专属 prompt（build_coordinator_prompt / build_executor_prompt）
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
   b. 默认 high
   c. 按级别格式化行为规则
2. build_goals_block_if_dirty(agent):
   a. 检查 goals_dirty（goals 自上次 agent 读取后是否更新）
   b. dirty → 注入完整 goals workbook + 标记已读
   c. not dirty → 返回 nil
3. build_memory_block(agent):
   a. MemoryService.build_agent_context(project_id, agent_id)
4. build_active_skills_section(bound_skills):
   a. SkillRegistry 构建技能摘要段
5. 拼接非空部分为 system 消息
```

### 用户参与度三级

| 级别 | 技术决策 | 产品/业务决策 | 重大方向变更 | 适用场景 |
|---|---|---|---|---|
| high | 必须问用户 | 必须问用户 | 必须问用户 | 用户有技术能力且想掌控方向 |
| medium | AI 自主 | 必须问用户 | 必须问用户 | 用户懂产品不懂技术 |
| low | AI 自主 | AI 自主 | 仅通知用户 | 用户完全信任 AI |

**不变部分**：无论哪个级别，AI 都不能伪造结果、隐藏风险、跳过验证。

### 语言规则（中文模型）

```
1. 检测模型是否中文训练：deepseek/kimi/qwen/glm/yi-/doubao/ernie/hunyuan
2. 中文模型 → 追加 "use the SAME language as the user"
3. 西方模型（Claude/GPT/Gemini）→ 不追加（信任模型自动镜像用户语言）
4. 参考 OpenCode system.ts:26-40
```

### 角色专属 prompt

**coordinator prompt** 包含：
- 管理工具说明（review_code/approve_work/reject_work 等）
- worktree 管理说明
- 招聘流程（HR 专属：Search Before Building 招聘前必做）
- 组织范式流程节点（6 种范式）
- Boil the Lake 完整性检查

**executor prompt** 包含：
- 文件操作工具说明
- 工作日志要求
- 完整实现要求
- 边界处理要求
- 反合理化表（执行者专属）

## 角色纪律四件套（每个角色必备）

| 组件 | 说明 |
|---|---|
| 何时不做 | 明确列出不该做的事（如 executor 不 spawn agent） |
| 输出格式 | 规定输出格式和规范 |
| 验证清单 | 每阶段必须通过的检查项 |
| 反合理化表 | 常见借口 + 反驳 |

## 组织范式流程节点（6 种）

| 范式 | 必经流程 |
|---|---|
| solo | 必须自审 |
| flat_squad | 交叉审查 |
| tech_lead | tech_lead 审查所有 |
| pm_architect | 双线汇报 |
| pod | 队内自审 + 跨队审查 |
| pipeline | 阶段门禁 |

## 常量引用

| 常量 | 值 | 所在章节 |
|---|---|---|
| 默认 userInvolvement | `high` | ETHOS 提示词 |
| 中文模型列表 | deepseek/kimi/qwen/glm/yi-/doubao/ernie/hunyuan | 同上 |
| goals dirty 检查 | 每轮检查 goals 版本 | 同上 |
| join 历史消息数 | `50` 条 | 实时通信 |

## 已知问题

| 问题编号 | 说明 | Python 迁移处理 |
|---|---|---|
| — | identity prompt 是静态的，设计为 prefix cache 友好 | 确保 Python 侧同一 agent 的 identity prompt 跨 turn 不变 |
| — | context prompt 每轮重建 | 确保动态部分在 System 2，不混入 System 1 |
| — | goals dirty 机制用 process dictionary 缓存 | Python 用 turn 级变量缓存 |
| — | 中文模型检测用 model_id 字符串匹配 | 保留此逻辑 |

## 验收标准

- [ ] identity prompt 包含三原则（Boil the Lake / Search Before Building / User Involvement）
- [ ] identity prompt 包含通用验证文化 + 反合理化表
- [ ] identity prompt 包含 .hiveweave 目录保护规则
- [ ] identity prompt 包含诚实与完整性规则（零容忍）
- [ ] identity prompt 包含决策规则（不自主做方向性决策）
- [ ] identity prompt 包含通信规则（花名称呼、群发支持）
- [ ] identity prompt 包含行动纪律（说到做到、工具前写说明）
- [ ] coordinator prompt 包含管理工具 + worktree + 组织范式
- [ ] executor prompt 包含文件操作 + 工作日志 + 完整实现
- [ ] context prompt 每轮重建，包含 involvement + goals + memory + skills
- [ ] 用户参与度三级（high/medium/low）正确格式化
- [ ] 中文模型追加语言规则
- [ ] 西方模型不追加语言规则
- [ ] goals dirty 时注入完整 workbook 并标记已读
- [ ] identity prompt 同一 agent 跨 turn 不变（prefix cache 友好）
- [ ] context prompt 为 nil 时不插入 System 2

## Python 实现建议

- `def build_identity_prompt(agent, model) -> dict` 返回 `{"role": "system", "content": ...}`
- `def build_context_prompt(agent) -> dict | None` 返回 `{"role": "system", "content": ...}` 或 None
- 提示词模板用 Python 多行字符串（f-string 或 template）
- goals dirty 检查用 turn 级变量缓存（对应 Elixir process dictionary）
- 中文模型检测用字符串匹配
- 参考 OpenCode `system.ts` 的语言规则实现
