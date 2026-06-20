## AI 工程组织 · MVP 技术蓝图

### 产品定位

不是"AI 编程工具"，而是一个会自我演化的 AI 工程组织。Agent 有职级、记忆可继承、离职有交接。用户可以像管理真实团队一样管理 AI 团队——设定组织架构、分配任务、调整编制，也可以随时跳过层级直接和任何成员对话。

---

### 已确认的设计决策

| 决策项 | 结论 | 备注 |
|---|---|---|
| 起步框架 | Claude Agent SDK | 原生 tool use + 结构化输出，代价是绑定 Anthropic 模型 |
| 分工原则 | 上级只协调，叶子才写代码 | 协调型 Agent 不接触代码，避免角色混乱 |
| 协调型权限 | 有验收权（读码 + 打回返工），不亲自写 | 经理只看代码质量和日志，不动手 |
| 集成测试 | MVP 先做「上级触发集成」 | 架构师/经理在子模块完成后触发集成测试 |
| 产品形态 | Web 界面 | 可视化组织架构 + 多面板对话 |
| 记忆隔离 | Agent 私有记忆互不可见 | 前端看不到后端的坑，反之亦然 |

#### ⚠️ 待决策项

**AI 扩张权设闸：** 经理级 Agent 想自己 spawn 新子模块，是否需要用户审批？

建议方案：MVP 阶段一律需要用户确认。后期可加白名单机制（例如"前端经理可在前端范围内自主创建子模块，但需事后报备"）。这个决策直接影响 MVP 是否需要做"审批流"组件。

**模型锁定 vs 多模型兼容：** Claude Agent SDK 锁定 Anthropic，但体验最好。如果后续需要接 DeepSeek/Qwen 等模型，需要在 Agent 编排层做一层抽象。建议 MVP 先用 Claude SDK，但在 memory/tool 接口上保持模型无关。

---

### 记忆三层模型

这是整个系统最核心的设计。Agent 是临时的，知识是持久的。

#### 第一层：项目宪法（宏观记忆）

全局只读，所有 Agent 启动时自动注入。内容包括：项目目标与规划、技术栈选型、架构决策、命名规范、整体进度。任何 Agent 都能读，但不能改——修改项目宪法需要通过架构师级 Agent 提案 + 用户确认。

#### 第二层：工作记忆（Agent 私有）

每个 Agent 在职期间积累的私有经验：写过什么代码、踩过什么坑、为什么选了 A 方案而不是 B、报错记录和解决方式。按 `agent_id` 严格隔离，前端 Agent 的工作记忆对后端完全不可见。工作记忆是 Agent 的"工位笔记本"，只在自身对话和上级查阅时可见。

#### 第三层：归档记忆（Archive）

Agent 被解散时，不是简单删除，而是：私有记忆经 LLM 总结后生成「交接文档」移交给接收方，原始记忆连同向量索引一起归档（scope 从 `agent` 翻转为 `archive`）。归档记忆不属于任何活跃 Agent，但可以被语义检索和未来 Agent 加载。

**复活路径：** 将来某个模块需要迭代时，新创建的 Agent 会自动检索该模块的归档记忆，作为初始工作记忆加载——相当于新人入职拿到前任的完整工作笔记。

#### 记忆检索

记忆多了以后关键词搜索不够用（例如"WebSocket 重连的坑"搜不到"长连接断开后的内存问题"）。MVP 阶段建议 SQLite + sqlite-vec 实现向量语义检索，后期可迁移到 PostgreSQL + pgvector。每条记忆记录同时存文本内容和 embedding 向量。

---

### 工具权限矩阵

两类 Agent，权限完全不同：

#### 协调型（架构师 / 经理）

- ✅ 读下级工作日志和代码
- ✅ 验收下级产出（读码审查 + 打回返工）
- ✅ 拆解需求、向下派活
- ✅ 增删下级 Agent
- ✅ 触发合并记忆
- ✅ 触发集成测试
- ❌ 不能写代码、不能跑代码
- ❌ 不能读非直属 Agent 的私有记忆

#### 执行型（叶子 Agent）

- ✅ 读写代码
- ✅ 跑单元测试
- ✅ 写工作日志
- ✅ 读项目宪法 + 自身私有记忆
- ❌ 不能 spawn 子 Agent
- ❌ 不能读其他 Agent 的私有记忆
- ❌ 不能验收他人产出

---

### 组织架构与通信

#### 层级结构

典型的三层架构：

```
用户
 └── 总架构师
      ├── 前端经理
      │    ├── 模块A负责人（叶子）
      │    ├── 模块B负责人（叶子）
      │    └── 模块C负责人（叶子）
      └── 后端经理
           ├── API负责人（叶子）
           └── 数据库负责人（叶子）
```

组织架构是动态的：用户可以随时晋升叶子为经理、合并模块、解散 Agent、在经理下面新增子模块。

#### 通信路径

两条路径并存：

**层级传递（默认）：** 用户 → 架构师 → 经理 → 叶子，需求逐级拆解、日志逐级汇报。

**跨级直达：** 用户可以直接和任何层级的 Agent 对话。直接跟模块负责人说"把这个按钮颜色改一下"完全合法，不需要经过经理和架构师。系统记录这次对话到该 Agent 的工作日志中，上级下次读日志时会看到。

#### 上级对话前的日志读取协议

每次上级 Agent 和下级对话（或处理下级汇报）时，系统自动将该下级最近的工作日志摘要注入上级的 context，让上级了解"下级最近改了什么、遇到了什么问题"。这不需要人工触发，是通信协议的内置行为。

---

### 派活 → 验收闭环

完整的工作流如下：

```
架构师 读经理日志 → 派需求给经理
  → 经理 读模块日志 → 拆解需求派给叶子
    → 叶子 写代码 + 跑单测 + 写工作日志
    → 叶子 上报完成
  → 经理 读码验收
    → ❌ 打回：叶子返工
    → ✅ 通过：聚合产出上报架构师
→ 架构师 触发集成测试
  → ❌ 集成失败：定位到具体模块打回
  → ✅ 集成通过：闭环完成
```

每一级只做自己权限范围内的事。叶子写码，经理验收，架构师做集成。

---

### 演化事件

#### 事件一：晋升（叶子 → 经理）

触发条件：用户决定将某个叶子 Agent 升级为经理（例如前端负责人升级为前端经理）。

执行流程：
1. 切换工具集——从执行型权限变为协调型权限
2. 原工作日志归档（保留但不再追加）
3. 私有记忆下沉——原来的私有记忆作为"个人经验"保留，但不再作为工作记忆使用
4. 在该 Agent 下创建新的子模块 Agent，子模块继承原模块的归档记忆
5. 组织架构树更新，新经理可以开始接收和拆解需求

#### 事件二：解散（离职交接 / Handoff）

触发条件：用户删除某个 Agent（模块已完成、不再需要、或要合并）。

执行流程（6 步）：

1. **触发解散** — 用户选择删除目标 Agent
2. **冻结** — Agent status 变为 `dissolving`，停止接受新任务，但保留记忆和日志的读权限
3. **读取私有记忆** — 拉取该 Agent 全部的 scope=agent 记忆 + work_logs
4. **LLM 总结** — 调用 LLM 生成交接文档，包含：关键技术决策、踩过的坑及解决方式、遗留问题和风险、给后续维护者的建议
5. **移交接收方** — 总结以 `type=handoff_summary` 写入上级或同级 Agent 的工作记忆
6. **归档** — 原始记忆 scope 从 `agent` 翻转为 `archive`，向量索引保留，支持未来语义检索

不真删、可复活。

#### 事件三：合并（Merge）

触发条件：用户将两个 Agent 合并为一个（例如两个模块合并由一个 Agent 负责）。

执行流程（4 阶段）：

1. **各自总结** — Agent A 和 Agent B 各对自己的私有记忆做一次 LLM 总结，输出 summary_A 和 summary_B
2. **冲突检测** — 将两份总结一起喂给 LLM，识别矛盾点（技术选型冲突、接口约定不一致、命名风格差异等）
3. **冲突解决** — 三种策略可选：`auto`（LLM 自动仲裁）、`manual`（用户逐一决定）、`hybrid`（LLM 给建议 + 用户确认，推荐）
4. **合成新记忆** — 合并后的记忆注入新 Agent 作为初始工作记忆，冲突解决记录归档

公式：`merged_memory = summary_A ∪ summary_B − conflicts + resolutions`

原始 Agent 的私有记忆各自归档（scope → archive）。merges 表记录完整的来源、冲突和解决方案。

---

### Agent 生命周期（状态机）

7 个状态，10 种转换：

```
Created ──→ Active ──→ Promoted
                │           │
                ├─→ Receiving ──→ Active
                ├─→ Merging ──→ Archived
                └─→ Dissolving ──→ Archived
              Promoted ──→ Dissolving ──→ Archived
```

| 状态 | 含义 |
|---|---|
| Created | 刚创建，加载项目宪法 + 归档记忆（如有），初始化空工作记忆 |
| Active | 正常工作，持续写入 work_logs 和私有记忆 |
| Promoted | 晋升为协调型，获得读下属日志和验收的权限 |
| Receiving | 正在接收 Handoff（从解散的同级/下级 Agent 获得交接总结） |
| Merging | 正在参与合并（与其他 Agent 做记忆合并） |
| Dissolving | 正在解散（执行 Handoff 6 步流程） |
| Archived | 终态。原始记忆归档，可被未来 Agent 检索但不可修改 |

#### Agent 创建流程（Agent Factory）

新 Agent 创建时的自动初始化：
1. 注入项目宪法（全局共享记忆）
2. 如果接手已有模块 → 检索该模块的归档记忆，作为初始工作记忆
3. 如果是全新模块 → 初始化空工作记忆
4. 分配工具权限（协调型 or 执行型）
5. 注册到组织架构树，设置 parent_id

---

### 数据模型（6 张核心表）

```sql
-- 1. Agent 注册表
agents (
  id              uuid PRIMARY KEY,
  name            varchar,
  role            enum,              -- 'architect' | 'manager' | 'module_dev'
  parent_id       uuid REFERENCES agents(id),  -- 层级关系
  module_id       uuid REFERENCES modules(id), -- 负责哪个模块
  status          enum,              -- 'created' | 'active' | 'promoted' | 'receiving' | 'merging' | 'dissolving' | 'archived'
  goal            text,
  backstory       text,
  skills          jsonb,             -- 专属技能列表
  permission_type enum,              -- 'coordinator' | 'executor'
  created_at      timestamp,
  updated_at      timestamp
)

-- 2. 模块表（支持嵌套）
modules (
  id                  uuid PRIMARY KEY,
  name                varchar,
  parent_module_id    uuid REFERENCES modules(id), -- 模块树
  status              enum,            -- 'active' | 'completed' | 'archived'
  current_agent_id    uuid REFERENCES agents(id),  -- 当前负责人
  created_at          timestamp,
  updated_at          timestamp
)

-- 3. 记忆表（三层 scope + 向量索引）
memories (
  id                uuid PRIMARY KEY,
  agent_id          uuid REFERENCES agents(id),  -- null = 项目级
  scope             enum,              -- 'project' | 'agent' | 'archive'
  module_id         uuid REFERENCES modules(id), -- 关联模块（归档时用）
  type              enum,              -- 'knowledge' | 'decision' | 'lesson' | 'error' | 'log' | 'handoff_summary' | 'merge_summary'
  content           text,              -- 记忆正文
  embedding         vector(1536),      -- 语义向量（用于检索）
  source_agent_id   uuid,              -- 原始作者（handoff/merge 时保留溯源）
  metadata          jsonb,             -- 灵活扩展字段
  created_at        timestamp,
  updated_at        timestamp
)

-- 4. 工作日志表
work_logs (
  id          uuid PRIMARY KEY,
  agent_id    uuid REFERENCES agents(id),
  session_id  uuid,                    -- 对话会话 ID
  type        enum,                    -- 'code_change' | 'bug_fix' | 'feature' | 'refactor' | 'discussion'
  summary     text,                    -- 做了什么
  details     jsonb,                   -- 文件变更、命令执行、测试结果等
  created_at  timestamp
)

-- 5. 交接记录表
handoffs (
  id                  uuid PRIMARY KEY,
  from_agent_id       uuid REFERENCES agents(id),  -- 被解散的 Agent
  to_agent_id         uuid REFERENCES agents(id),  -- 接收方（上级或同级）
  module_id           uuid REFERENCES modules(id),
  summary             text,              -- LLM 生成的交接总结
  memory_snapshot_id  uuid,              -- 关联归档记忆
  status              enum,              -- 'pending' | 'completed' | 'failed'
  created_at          timestamp
)

-- 6. 合并记录表
merges (
  id                  uuid PRIMARY KEY,
  source_agent_ids    uuid[],            -- 被合并的 Agent 列表
  target_agent_id     uuid REFERENCES agents(id), -- 合并后的新 Agent
  summary             text,              -- LLM 合并总结
  conflicts           jsonb,             -- 检测到的冲突详情
  resolution          jsonb,             -- 解决方案记录
  created_at          timestamp
)
```

---

### 技术栈

| 层 | 选型 | 理由 |
|---|---|---|
| Agent 编排 | Claude Agent SDK | 原生 tool use + 结构化输出，体验最好 |
| 后端 | Node.js / TypeScript | 和 Claude SDK 生态契合 |
| 前端 | React + Vite | 轻量快速 |
| 组织架构可视化 | React Flow | 拖拽式组织树，交互丰富 |
| 记忆存储 | SQLite + sqlite-vec（MVP）| 每 Agent 一个命名空间，轻量够用；后期可迁 PG+pgvector |
| 沙箱执行 | Docker | 叶子 Agent 的代码在隔离容器里跑，不污染宿主机 |

---

### MVP 三步走

**Phase 1：跑通层级派活**
搭建基础组织架构（架构师 → 经理 → 叶子），实现需求逐级拆解、日志逐级汇报、上级读码验收、叶子写码跑测。此时记忆系统只做最简版（项目宪法 + Agent 工作记忆），不做归档和交接。

**Phase 2：接记忆隔离 + 归档**
实现三层记忆模型，Agent 私有记忆严格隔离，解散时走完整 Handoff 流程（总结 → 移交 → 归档）。加入向量语义检索。新 Agent 创建时自动加载相关归档记忆。

**Phase 3：补演化事件**
实现晋升（叶子 → 经理，切换权限集）、合并（两 Agent 记忆合并 + 冲突解决）、AI 扩张权设闸（审批流 or 白名单）。组织架构可视化交互完善。

---

### 附录：和其他方案的差异点

| 维度 | 本产品 | OpenHands | MetaGPT | CrewAI |
|---|---|---|---|---|
| 层级关系 | 动态嵌套，随时调整 | 扁平多 Agent | 固定 SOP 流程 | 扁平 Crew |
| 记忆隔离 | 三层（共享/私有/归档）| 全局共享 | 全局共享 | 简单 memory |
| 交接机制 | LLM 总结 + 归档 + 可复活 | 无 | 无 | 无 |
| 合并机制 | 冲突检测 + 仲裁 + 合成 | 无 | 无 | 无 |
| 跨级通信 | 支持用户直达任意 Agent | 不支持 | 不支持 | 不支持 |
| 权限矩阵 | 协调型/执行型严格区分 | 统一权限 | 按角色硬编码 | 无 |
| 沙箱执行 | Docker 隔离 | Docker | 无 | 无 |
