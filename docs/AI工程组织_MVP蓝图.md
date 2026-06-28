# AI 工程组织 · MVP 技术蓝图（v2 合并版）

---

## 产品定位

不是"AI 编程工具"，而是一个**会自我演化的 AI 工程组织**——Agent 有职级、记忆可继承、离职有交接，并且**按岗位难度匹配不同价位的模型**，用尽量少的人类介入把一个复杂大工程从 0 推到 100。

核心追求三件事（按对外可感知度排序）：

1. **省钱**：越重要的岗位用越强越贵的模型，大量体力活交给便宜模型，只在全员卡死时请一次"最贵的专家"。
2. **少人类介入**：验收打回、集成、记忆传承都由组织内部消化。
3. **能扛大工程**：单 Agent 上下文装不下、hold 不住全局的复杂工程，靠分工 + 结构化记忆扛住长程一致性。

> **价值窗口认知：** 简单难题里单 Agent 永远赢（无协调损耗）。HiveWeave 的主场是"复杂到单 Agent 必然崩" + "成本敏感"的大工程。

---

## 0. 已确认决策

| 决策项 | 结论 | 备注 |
|---|---|---|
| 起步框架 | Claude Agent SDK | 实际以 ai SDK 的 `streamText` 落地 |
| 分工原则 | 上级只协调，叶子才写代码 | 协调型 Agent 不接触代码，避免角色混乱 |
| 协调型权限 | B 有验收权（读码 + 打回返工，不亲自写） | 经理只看代码质量和日志，不动手 |
| 集成测试 | MVP 先「上级触发集成」 | 架构师/经理在子模块完成后触发集成测试 |
| 产品形态 | Web 界面 | 可视化组织树 + 拖拽调层级 |
| 多模型兼容 | **核心特性**（见第 2 节） | 系统必须支持按岗位绑定不同 provider/model |

---

## 1. 记忆三层模型

这是整个系统最核心的设计。Agent 是临时的，知识是持久的。

### 第一层：项目宪法（宏观记忆）

全局只读，所有 Agent 启动时自动注入。内容包括：项目目标与规划、技术栈选型、架构决策、命名规范、整体进度。任何 Agent 都能读，但不能改——修改项目宪法需要通过架构师级 Agent 提案 + 用户确认。负责长程一致性——第 8 个模块仍记得第 1 个模块定的接口约定。

### 第二层：工作记忆（Agent 私有）

每个 Agent 在职期间积累的私有经验：写过什么代码、踩过什么坑、为什么选了 A 方案而不是 B、报错记录和解决方式。按 `agent_id` 严格隔离，前端 Agent 的工作记忆对后端完全不可见。工作记忆是 Agent 的"工位笔记本"，只在自身对话和上级查阅时可见。

### 第三层：交接 / 归档记忆

Agent 被解散时，不是简单删除，而是：私有记忆经 LLM 总结后生成「交接文档」移交给接收方，原始记忆连同向量索引一起归档（scope 从 `agent` 翻转为 `archive`）。归档记忆不属于任何活跃 Agent，但可以被语义检索和未来 Agent 加载。

> **复活路径：** 将来某个模块需要迭代时，新创建的 Agent 会自动检索该模块的归档记忆，作为初始工作记忆加载——相当于新人入职拿到前任的完整工作笔记。

### v2 升级：交接 / 传递记忆走"显式 artifact 引用" ⭐

- 每份交接产出带 `id` 的**结构化知识产物**（经历 / 经验 / 成果 / 遗留坑）。
- 继任者不是"读前任日记"，而是通过 `context_refs: [artifact_id]` 显式注入前任 artifact。
- 派活时用 `context_bootstrap`（path + reason）精确告诉下级"该看哪个产物、为什么"。
- **跨代复利**：前任 artifact 被引用 → 产出新 artifact → 再被下一代引用，让"组织传承"有可测量的载体。

> **设计原则：引用（reference）优于合并（merge）**——谁需要谁注入，干净、可追溯、不失真。

### 记忆检索

记忆多了以后关键词搜索不够用（例如"WebSocket 重连的坑"搜不到"长连接断开后的内存问题"）。MVP 阶段建议 SQLite + sqlite-vec 实现向量语义检索，后期可迁移到 PostgreSQL + pgvector。每条记忆记录同时存文本内容和 embedding 向量。

---

## 2. 成本-能力梯度引擎（v2 核心新增 ⭐）

整个组织是一台"按岗位难度匹配模型价格"的引擎：

| 岗位 | 职责 | 模型档位 | 成本逻辑 |
|---|---|---|---|
| 执行层（叶子） | 写码、跑测、体力活 | 便宜模型 | 量大，必须省 |
| 验收 / 经理层 | 读码验收、拆派、打回 | 中端模型（不能太便宜 ⚠️） | 质量闸门 |
| CEO / 架构师 | 全局决策、触发集成 | 高端模型 | 数量少、权重高 |
| 专家会诊 | 全员卡死时请教 | 最强最贵模型 | 只在最高杠杆瞬间出现 |

### ① 验收闸是成本结构的命门

便宜模型在底层埋的坑会顺层级往上冒。若验收层模型太弱、看不出隐患 → 坏代码一路绿灯到 CEO → 最后还得请最贵专家擦屁股，反而更贵。

> **结论：省钱的关键不在执行层多便宜，而在验收层"刚好够聪明、能拦住坑"。** 调梯度时优先精算验收层下限。

### ② 专家会诊机制（把贵模型用在刀刃上）

- **触发**：组织内各层都解决不了时，由 CEO 把问题提炼成一个干净、具体的问题再交给最贵专家。
- **省且强**：贵模型不全程跟随、只点一下；问题被提纯过，解决率更高。
- **防滥用**：设触发门槛（如组织内重试 N 次 / 验收连续打回 M 次才允许上报），否则退化成"凡事问专家"，成本失控。

> 因为这条，"多模型兼容"从待决策项**转正为核心特性**：系统必须支持按岗位绑定不同 provider/model。

---

## 3. 工具权限矩阵

两类 Agent，权限完全不同：

### 协调型（架构师 / 经理）

- ✅ 读下级工作日志、读代码（验收用）
- ✅ 拆任务、派活、打回返工
- ✅ 增删下级、引用 / 继承 artifact、触发集成测试
- ✅ 全员卡死时向专家会诊（受门槛约束）
- ❌ 写代码、跑代码
- ❌ 读非直属 Agent 的私有记忆

### 执行型（叶子 Agent = 模块负责人）

- ✅ 读写代码、跑单测、写工作日志、产出 artifact
- ✅ 读宏观记忆 + 自己私有记忆 + 被注入的 `context_refs`
- ❌ spawn 子 Agent
- ❌ 看别人私有记忆
- ❌ 验收他人产出

---

## 4. 通信机制

两条路径并存：

**层级传递（默认）：** 用户 → 架构师 → 经理 → 叶子，需求逐级拆解、日志逐级汇报。

**跨级直达通信：** 允许在必要时跨层直接对话（如 CEO 直接问某叶子），避免信息逐层失真与延迟。系统记录跨级对话到该 Agent 的工作日志中，上级下次读日志时会看到。

### 上级对话前的日志读取协议

每次上级 Agent 和下级对话（或处理下级汇报）时，系统自动将该下级最近的工作日志摘要注入上级的 context，保证"带着上下文开口"，而非空对空。这不需要人工触发，是通信协议的内置行为。

---

## 5. 派活 → 验收闭环

完整的工作流如下：

```
架构师 读经理日志 → 派需求给经理（带 context_bootstrap）
  → 经理 读各模块日志 → 拆解需求派给叶子
    → 叶子 写代码 + 跑单测 + 写工作日志 + 产出 artifact
    → 叶子 上报完成
  → 经理 读码验收
    → ❌ 打回：叶子返工
    → ✅ 通过：聚合产出上报架构师
→ 架构师 收聚合 → 触发集成测试
  → ❌ 集成失败：定位到具体模块打回
  → ✅ 集成通过：闭环完成
    （任意层卡死且达门槛 → CEO 提炼具体问题 → 专家会诊）
```

每一级只做自己权限范围内的事。叶子写码，经理验收，架构师做集成。

---

## 6. 关键演化事件

### 事件一：晋升（叶子 → 经理）

触发条件：用户决定将某个叶子 Agent 升级为经理。

执行流程：

1. 交出写码权限 → 换上协调工具集
2. 原工作日志归档（保留但不再追加）
3. 私有记忆沉淀为 artifact，下沉给新建子模块
4. 在该 Agent 下创建新的子模块 Agent，子模块继承归档记忆
5. 组织架构树更新，新经理可以开始接收和拆解需求

### 事件二：解散（离职交接 / Handoff）

触发条件：用户删除某个 Agent（模块已完成、不再需要、或要合并）。

执行流程（6 步）：

1. **触发解散** — 用户选择删除目标 Agent
2. **冻结** — Agent status 变为 `dissolving`，停止接受新任务，但保留记忆和日志的读权限
3. **读取私有记忆** — 拉取该 Agent 全部的 scope=agent 记忆 + work_logs
4. **LLM 总结** — 调用 LLM 生成结构化交接 artifact（带 id），包含：关键技术决策、踩过的坑及解决方式、遗留问题和风险、给后续维护者的建议
5. **移交接收方** — 交接 artifact 挂入上级/同级可引用池，通过 `context_refs` 被引用
6. **归档** — 原始记忆 scope 从 `agent` 翻转为 `archive`，向量索引保留，支持未来语义检索

不真删、可复活、可被未来 Agent `context_refs` 引用。

### 事件三：合并（Merge）

触发条件：用户将两个 Agent 合并为一个（例如两个模块合并由一个 Agent 负责）。

执行流程（4 阶段）：

1. **各自总结** — Agent A 和 Agent B 各对自己的私有记忆做一次 LLM 总结，输出 summary_A 和 summary_B
2. **冲突检测** — 将两份总结一起喂给 LLM，识别矛盾点（技术选型冲突、接口约定不一致、命名风格差异等）
3. **冲突解决** — 三种策略可选：
   - `auto`：无冲突自动并集
   - `manual`：冲突项交人工/上级裁决
   - `hybrid`：能自动的自动、剩余冲突上报（推荐）
4. **合成新记忆** — 合并后的记忆注入新 Agent 作为初始工作记忆，冲突解决记录归档

公式：`merged_memory = A ∪ B − conflicts + resolutions`

原始 Agent 的私有记忆各自归档（scope → archive）。merges 表记录完整的来源、冲突和解决方案。

---

## 7. 状态机与 Agent Factory

### 7 状态 · 10 转换

```
IDLE ──→ PROCESSING ──→ REVIEWING
  ↑            │              │
  │            ├─→ WAITING ──→┘（验收中）
  │            ├─→ BLOCKED ──→ ERROR
  │            └─→ ERROR
  │
  └── ARCHIVED（终态，可复活）
```

| 状态 | 含义 |
|---|---|
| IDLE | 空闲，等待派活或用户对话 |
| PROCESSING | 正在工作（写码 / 跑测 / 协调） |
| WAITING | 等待下级汇报 / 等待审批 |
| REVIEWING | 验收中（读码审查） |
| BLOCKED | 卡死，待会诊或上报 |
| ERROR | 异常状态（流卡死 / 执行失败） |
| ARCHIVED | 终态。原始记忆归档，可被未来 Agent 检索但不可修改 |

10 种转换覆盖：派活、开工、产出、验收通过/打回、卡死上报、专家返回、超时翻转、归档、复活等。

### Agent 创建流程（Agent Factory）

新 Agent 创建时的自动初始化：

1. 分配岗位与模型档位
2. 注入项目宪法（全局共享记忆）
3. 如果接手已有模块 → 通过 `context_bootstrap` 检索该模块的归档 artifact 作为初始上下文
4. 如果是全新模块 → 初始化空工作记忆
5. 挂载工具集（按协调型/执行型权限矩阵）
6. 注册到组织架构树，设置 parent_id
7. 置 IDLE

---

## 8. 运行时稳定性（v2 新增 ⚠️）

### 问题定位

**故障：** Agent 显示 PROCESSING（绿点）但 Live Activity 全空白 ≥15 分钟，手动重启才恢复。

**根因：** `streamText` 流静默挂起（half-open / provider hang）。现有防护覆盖请求级 / error 级 / turn 级，唯独缺"流 idle 级"看门狗；且 provider 裸配无 fetch 超时。

### 三道防线（P0 → P1）

**P0 · chunk 间 idle 看门狗：** `withIdleTimeout` 包住 `streamText` fullStream，两 chunk 间隔超阈值（建议 60s）抛 `STREAM_IDLE_TIMEOUT`，复用现有重试链。

**P0 · 全局 turn 超时 + 状态翻转：** turn 总超时（建议 300s），超时强制 PROCESSING → ERROR 并发 error 事件，杜绝"僵尸绿点"。

**P1 · provider 注入带 `AbortSignal.timeout` 的 fetch：** 补请求级兜底。

> 这条对"少人类介入 / 无人值守跑通宵"是**生死线**——卡死必须能自愈。

---

## 9. 数据模型（SQL Schema 概览）

6 张核心表：

```sql
-- 1. Agent 注册表
agents (
  id              uuid PRIMARY KEY,
  name            varchar,
  role            enum,              -- 'architect' | 'manager' | 'module_dev'
  parent_id       uuid REFERENCES agents(id),  -- 层级关系
  module_id       uuid REFERENCES modules(id), -- 负责哪个模块
  model_tier      enum,              -- 'cheap' | 'mid' | 'high' | 'expert'（v2 新增）
  status          enum,              -- 'idle' | 'processing' | 'waiting' | 'reviewing' | 'blocked' | 'error' | 'archived'
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

-- 4. Artifacts 表（v2 新增 · 结构化知识产物）
artifacts (
  id                uuid PRIMARY KEY,
  agent_id          uuid REFERENCES agents(id),   -- 产出者
  module_id         uuid REFERENCES modules(id),  -- 关联模块
  type              enum,              -- 'experience' | 'deliverable' | 'lesson' | 'legacy_issue'
  title             varchar,
  content           text,              -- 产物正文
  context_refs      uuid[],            -- 引用的其他 artifact id（显式引用链）
  metadata          jsonb,
  created_at        timestamp,
  updated_at        timestamp
)

-- 5. 交接记录表
handoffs (
  id                  uuid PRIMARY KEY,
  from_agent_id       uuid REFERENCES agents(id),  -- 被解散的 Agent
  to_agent_id         uuid REFERENCES agents(id),  -- 接收方（上级或同级）
  module_id           uuid REFERENCES modules(id),
  artifact_id         uuid REFERENCES artifacts(id), -- v2: 关联交接 artifact
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

-- 7. 事件日志表（v2 新增）
events (
  id                  uuid PRIMARY KEY,
  type                enum,              -- 'dispatch' | 'approve' | 'reject' | 'promote' | 'dissolve' | 'merge' | 'consult_expert'
  agent_id            uuid REFERENCES agents(id),
  payload             jsonb,             -- 事件详情
  created_at          timestamp
)
```

> **v2 相对原 schema 的增量：** `agents` 增加 `model_tier` 字段；新增 `artifacts` 表作为可引用资产的中心（被 `context_refs` 指向）；新增 `events` 表记录演化事件；`handoffs` 增加 `artifact_id` 关联。

---

## 10. 技术栈

| 层 | 选型 | 理由 |
|---|---|---|
| Agent 编排 | Claude Agent SDK（ai SDK `streamText`） | 原生 tool use + 结构化输出 |
| 多 provider | ai SDK 多 provider 抽象 | 支持按岗位绑定不同模型（OpenAI / Anthropic / DeepSeek 等） |
| 后端 | Node.js / TypeScript | 和 ai SDK 生态契合 |
| 前端 | React + Vite | 轻量快速 |
| 组织架构可视化 | React Flow | 拖拽式组织树，交互丰富 |
| 记忆存储 | SQLite + sqlite-vec（MVP） | 每 Agent 一个命名空间，轻量够用；后期可迁 PG+pgvector |
| 沙箱执行 | Docker | 叶子 Agent 的代码在隔离容器里跑，不污染宿主机 |

---

## 11. 前端愿景：🎮「游戏发展国」式像素办公室

把组织树做成一间**像素风办公室**——灵感来自开罗游戏的《游戏发展国》（Game Dev Story）。用户打开 HiveWeave，看到的是一间正在运转的 AI 游戏工作室。

### 核心意象

每个 Agent 是一个像素小人，工位按层级排布（CEO 在顶层办公室，叶子在工位区）。

### 协作可视化

Agent 之间的通信是办公室里可观测的事件：

- **派活（dispatch）**：上级站起来走到下级的工位旁边，递出一张任务卡
- **汇报（report）**：下级走到上级工位旁递交报告；`expectReport=true` 时递交后上级点头
- **验收打回**：经理在电脑前看代码，红叉动画 + 原因
- **验收通过**：对勾动画
- **同级沟通**：两个 Agent 站在走廊聊天，有对话气泡飘出关键词

### 状态可视化

状态用小人姿态体现：

- **PROCESSING**：打字动画
- **WAITING**：喝咖啡
- **BLOCKED**：头顶问号
- **ERROR**：冒黑烟
- **专家会诊**：最贵专家以"外援顾问"形象空降，解决后离场

### 交互方式

像素办公室是主视图，但不替代现有功能：

- **概览层**：俯视整个办公室，一眼看到所有 Agent 的状态和活动
- **详情层**：点击 Agent 工位，右侧滑出 ChatPanel + WorkLogPanel，可直接对话
- **视图切换**：办公室视图 ↔ 纯组织架构树（React Flow）——用户可以选择用哪种方式查看

### 技术实现路线

- **MVP 阶段**：用 CSS 网格 + 简单图标实现"桌子 + 人 + 状态指示器"的极简版本
- **进阶阶段**：引入 PixiJS + Canvas 渲染像素精灵，Agent 有行走动画和协作可视化
- **长期愿景**：办公室随团队规模扩展——从一间小房间到一层楼，再到一栋大楼

> 让"AI 公司在运转"这件事**肉眼可见、可玩、可传播**。

---

## 12. 待决策项

| 决策项 | 状态 | 说明 |
|---|---|---|
| 多模型兼容 vs 单模型锁定 | ✅ 已决策 | 多模型兼容，核心特性（见第 2 节） |
| AI 自主扩张权是否需要审批闸 | ⏳ 待定 | 经理想 spawn 新模块要不要人点头，防一夜多出 30 个叶子烧 token |
| 专家会诊触发门槛的具体数值 | ⏳ 待定 | 重试 N 次 / 打回 M 次 |
| 验收层模型下限的精算 | ⏳ 待定 | 成本 vs 拦截能力的平衡点 |
| idle / turn 超时阈值在 high-effort 长思考下的误杀调参 | ⏳ 待定 | 需实测调优 |

---

## 13. MVP 三步走

**Phase 1：跑通层级派活 + 分级模型路由**

搭建基础组织架构（架构师 → 经理 → 叶子），实现需求逐级拆解、日志逐级汇报、上级读码验收、叶子写码跑测。关键新增：按岗位绑定不同模型档位（叶子用便宜模型、验收层用中端模型、架构师用高端模型）。此时记忆系统只做最简版（项目宪法 + Agent 工作记忆），不做归档和交接。前端：保持现有 React Flow 组织架构树 + ChatPanel，作为开发调试主界面。

**Phase 2：接记忆隔离 + artifact 引用机制（context_refs / bootstrap）**

实现三层记忆模型，Agent 私有记忆严格隔离，解散时走完整 Handoff 流程（总结 → 产出交接 artifact → 归档）。交接记忆走显式 artifact 引用而非散文合并——新 Agent 创建时通过 `context_bootstrap` 精确注入前任 artifact。加入向量语义检索。前端：在现有架构树旁新增"极简办公室视图"——CSS 网格布局的工位矩阵，交互逻辑跑通。

**Phase 3：补演化事件 + 专家会诊触发器 + 流卡死三道防线 + 像素办公室**

实现晋升（叶子 → 经理，切换权限集）、合并（两 Agent 记忆合并 + 冲突解决）、专家会诊触发器（组织内重试 N 次 / 验收连续打回 M 次 → CEO 提炼问题 → 请教最贵专家）。落地运行时稳定性三道防线：chunk 间 idle 看门狗 + 全局 turn 超时 + provider fetch 超时。前端：引入像素精灵，用 Canvas/PixiJS 渲染真正的像素办公室。

---

## 附录：实施进度（截至 2026-06-23）

> 基于代码仓库实际审查。✅ = 已完成 / 🔄 = 部分完成 / ⬜ = 未开始

### Phase 1 — 跑通层级派活 ✅ 完成

| 功能点 | 状态 | 实现细节 |
|---|---|---|
| 组织架构树 (React Flow) | ✅ | `OrgTree.tsx` 递归布局，自定义 `AgentNode`，MiniMap + Controls |
| 层级创建 Agent | ✅ | `AddAgentDialog` 支持手动创建 + 模板创建，指定 parent/role/permission_type |
| CEO+HR 自动初始化 | ✅ | `server/index.ts` 项目启动时自动创建 or 修复，HR 挂 CEO 下 |
| Chat with Agent (SSE 流式) | ✅ | `chat.ts` 完整 SSE 流：text/tool_use/tool_result/approval_request/retry/done/error |
| 跨级直达对话 | ✅ | 用户可直接跟任意层级 Agent 聊天 |
| 日志逐级上报 | ✅ | `dispatch_task` → `report_completion` → `approve_work` / `reject_work` |
| 工作日志 | ✅ | `WorkLogPanel` + `WorkLogService`，DB 持久化 |
| 审批流 | ✅ | `ApprovalDialog` + `PermissionService` + `ApprovalService`，支持 remember |
| 实时活动流 | ✅ | SSE `status` 事件 → `activityFeed`，含 text/tool_use/tool_result/thinking/done/error |
| 通信可视化 | ✅ | React Flow 动画边，按 dispatch/message/trigger/peer 着色，3s 轮询 |
| Agent 状态机 | ✅ | 7 种状态 (created/active/promoted/receiving/merging/dissolving/archived) |
| 上班/下班 | ✅ | Pause/Resume 系统，SSE 实时同步 paused 状态 |
| 停止生成 | ✅ | ChatPanel "停止"按钮 abort SSE stream |
| 项目管理 | ✅ | 多项目创建/切换/删除，文件夹选择 workspace |
| 模型配置 | ✅ | `ModelSettings` 全 CRUD，支持测试连接 |
| 人员编制 (Roster) | ✅ | `RosterService` + API，记录部门/职位/职责 |
| Agent 模板 | ✅ | `TemplateService`，按 source/division/role 筛选 |
| 文件系统浏览 | ✅ | `FolderPicker` → `fs.ts` 路由，列出目录/盘符 |
| 图片输入 | ✅ | 粘贴/选择图片，base64 传输，存储到 chat_messages |
| 消息排队 | ✅ | Agent 繁忙时自动排队，完成后自动发送 |

Phase 1 已远超额完成。除了原始蓝图"层级派活"的目标外，还实现了审批流、实时活动、消息排队、图片输入、项目管理、模型配置等大量超出范围的功能。

### Phase 2 — 记忆隔离 + 归档 + 极简办公室 🔄 大部分完成

#### 记忆系统

| 功能点 | 状态 | 实现细节 |
|---|---|---|
| 三层记忆模型 | ✅ | `memories` 表 `scope` 字段：`project` / `agent` / `archive` |
| 项目宪法 (scope=project) | ✅ | `MemoryService.getProjectMemories()`，所有 Agent 启动时注入 |
| Agent 私有记忆 (scope=agent) | ✅ | `MemoryService.getAgentMemories(agentId)`，按 agent_id 严格隔离 |
| 归档记忆 (scope=archive) | ✅ | `MemoryService.getArchivedMemories(moduleId)`，按模块检索 |
| 记忆 CRUD | ✅ | `MemoryService` 完整实现：save/getByType/getByAgent/listRecent |
| Agent 解散时自动归档 | ✅ | `DELETE /agents/:id` 调用 `memoryService.archiveAgentMemories(id)` |
| 文字记忆注入 context | ✅ | `chat.ts` 内 `buildSystemPrompt()` 聚合 project + agent + archive 记忆 |
| LLM 总结生成交接文档 | 🔄 | `HandoffService` 实现了 dispatch→accept→complete 流程，但解散时的 LLM 总结生成未见明确调用 |
| 向量语义检索 | ⚠️ | `memories` 表 DB schema 中**没有 embedding 列**（设计文档有但实际表无），未见 sqlite-vec 集成 |

#### 交接 & 任务管理

| 功能点 | 状态 | 实现细节 |
|---|---|---|
| Handoff 记录表 | ✅ | `handoffs` 表：from_agent/to_agent/summary/status |
| 派活 → 接受 → 完成 | ✅ | `HandoffService.createHandoff()` / `acceptPendingHandoffs()` / `completeHandoff()` |
| 验收通过/打回 | ✅ | `approveHandoff()` / `reopenHandoff()` |
| 子任务完成通知上级 | ✅ | `report_completion` tool + `inboxService` 自动通知 |
| 解散交接 (Handoff 6 步) | 🔄 | 冻结/读记忆/归档路径已通，但"LLM 总结 → 写入接收方记忆"未见完整链 |

#### 极简办公室

| 功能点 | 状态 | 实现细节 |
|---|---|---|
| PixiJS 渲染引擎 | ✅ | `OfficeView.tsx` 使用 PixiJS 8.x，700×420 canvas |
| 地板/墙壁/网格 | ✅ | 木地板 + 网格线 + 墙壁，函数 `createFloor()` |
| 办公家具 | ✅ | 桌子/椅子/显示器/书架/绿植/饮水机/会议桌 |
| 5 个固定工位 | ✅ | `WORKSTATIONS` 数组硬编码，3+2 布局 |
| 角色精灵加载 | ✅ | `AgentSprite` 类，从 sprite sheet 切 7 帧，去背景色处理 |
| 精灵动画状态机 | ✅ | idle/walking/sitting/typing 四个状态 |
| 测试序列 | ✅ | `runTestSequence()`：走进来→坐下→打字→换工位→再坐下→打字 |
| 接入真实 Agent 数据 | ❌ | OfficeView 完全未对接 agent 数据，工位硬编码 |
| 点击工位展开对话 | ❌ | 没有交互响应，没有事件处理 |
| 空桌子创建 Agent | ❌ | 没有实现 |
| 屏幕颜色反映状态 | ❌ | 显示器只是静态蓝色辉光 |

Phase 2 评估：记忆系统核心骨架完整（三层 scope + CRUD + 归档），但缺少向量检索和完整的 LLM 交接总结链。办公室画了很漂亮的场景但完全是独立测试模式——精灵和数据没接上。

### Phase 3 — 演化事件 + 像素办公室 🔄 部分完成

#### 演化事件

| 功能点 | 状态 | 实现细节 |
|---|---|---|
| 晋升状态 (Promoted) | 🔄 | 状态枚举存在，`AgentNode` 有颜色定义，但完整 5 步晋升流程未确认 |
| 合并记忆 | ⚠️ | `merges` 表存在，但代码中未见完整的 conflict-detect → resolve → synthesize 流程 |
| 冲突检测与仲裁 | ⚠️ | 高级合并功能未实现 |
| AI 扩张权设闸 | 🔄 | 审批流基础能力已完备，但"Agent 自主 spawn 需审批"的具体逻辑未确认 |

#### 像素办公室高级特性

| 功能点 | 状态 | 实现细节 |
|---|---|---|
| 行走动画 | ✅ | `walkTo()` 方法 + 帧动画 + 方向翻转，测试序列验证通过 |
| 站位动画 | ✅ | `sitDown()` / `standUp()` |
| 打字动画 | ✅ | `startTyping()` 帧循环 |
| 协作可视化 (派活走过场) | ❌ | 未实现 |
| 汇报递交动画 | ❌ | 未实现 |
| 同级聊天气泡 | ❌ | 未实现 |
| 办公室随团队扩展 | ❌ | 工位固定 5 个 |
| 组织树缩略小地图 | ❌ | 未实现 |
| 时间线回放 | ❌ | 未实现 |
| 里程碑庆祝动画 | ❌ | 未实现 |

Phase 3 评估：Agent Runtime 的部分能力已经非常深入（tool executor 支持完整的 dispatch/report/review 工具链），但"演化事件"的精髓还未完整实现。像素办公室做了很好的技术验证，但离蓝图里的"游戏发展国式实况办公室"还差很多。

### 超出蓝图的已实现功能

这些功能原始 MVP 蓝图未规划但已实现：消息排队系统、LLM 重试机制（最多 3 次指数退避）、API Key 安全掩码、Agent 重命名、权限规则配置、Agent 模板系统、Orphaned message 警告、Orphaned approval 清理、后台消息/未读标记、Agent 详情面板（6 分区）。
