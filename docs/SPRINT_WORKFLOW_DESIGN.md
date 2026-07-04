# HiveWeave Sprint 工作流引擎架构设计文档

> 移植 gstack 管理模式到 HiveWeave 多 Agent 系统

## 一、设计目标与核心理念

### 1.1 现状分析

当前 HiveWeave 存在以下关键问题:

1. **生命周期仅存在于 prompt 文本中**:CEO prompt(`streamer.ex:1074-1113`)定义了 DEFINE→PLAN→BUILD→VERIFY→REVIEW→SHIP 六阶段,但没有任何持久化状态。
2. **设计文档无结构化存储**:DEFINE 阶段要求写 spec 到 memory,但 memory 是扁平键值列表,下游 agent 无法按阶段精确检索。
3. **缺乏阶段产出物追踪**:work_logs 是审计轨迹,没有结构化的阶段交付物。
4. **agent 按任务类型拉取历史的能力缺失**:context 注入靠 memory scope,没有基于任务类型的自动拉取。
5. **阶段流转无强制约束**:prompt 写了流转规则,但引擎层面不强制 exit 条件。

### 1.2 设计原则

- **Sprint 是 handoff 的上层编排,不替代 handoff 状态机**:通过观察者模式监听 handoff 状态变化驱动阶段流转。
- **设计文档驱动是核心**:DEFINE 阶段产出结构化设计文档,后续所有阶段通过 context_queries 自动拉取。
- **治本不补丁**:新增独立数据模型和服务模块,不篡改现有 handoff/memory 语义。
- **渐进式集成**:现有非 Sprint 的 handoff 继续工作,Sprint 是可选上层编排。

## 二、新增数据模型

### 2.1 sprints 表

```sql
CREATE TABLE IF NOT EXISTS sprints (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  title TEXT NOT NULL,
  goal TEXT DEFAULT '',
  status TEXT DEFAULT 'draft',             -- draft|active|completed|aborted
  current_phase TEXT DEFAULT 'define',     -- define|plan|build|verify|review|ship|reflect
  design_doc TEXT DEFAULT '',
  design_doc_status TEXT DEFAULT 'draft',   -- draft|review|approved
  created_by_agent_id TEXT,
  metadata TEXT DEFAULT '{}',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  started_at INTEGER,
  completed_at INTEGER
)
```

### 2.2 phase_artifacts 表

```sql
CREATE TABLE IF NOT EXISTS phase_artifacts (
  id TEXT PRIMARY KEY,
  sprint_id TEXT NOT NULL,
  phase TEXT NOT NULL,
  artifact_type TEXT NOT NULL,   -- design_doc|task_breakdown|build_log|test_report|review_report|ship_checklist|retrospective
  title TEXT DEFAULT '',
  content TEXT DEFAULT '',
  status TEXT DEFAULT 'draft',   -- draft|submitted|approved|rejected
  produced_by_agent_id TEXT,
  approved_by_agent_id TEXT,
  handoff_id TEXT,
  metadata TEXT DEFAULT '{}',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
)
```

### 2.3 sprint_tasks 表

```sql
CREATE TABLE IF NOT EXISTS sprint_tasks (
  id TEXT PRIMARY KEY,
  sprint_id TEXT NOT NULL,
  phase TEXT NOT NULL,
  handoff_id TEXT,
  title TEXT NOT NULL,
  description TEXT DEFAULT '',
  status TEXT DEFAULT 'pending',  -- pending|in_progress|completed|approved|rejected
  assigned_agent_id TEXT,
  artifact_id TEXT,
  context_query_profile TEXT DEFAULT '',
  order_index INTEGER DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
)
```

### 2.4 handoffs 表扩展

```sql
ALTER TABLE handoffs ADD COLUMN sprint_id TEXT;
ALTER TABLE handoffs ADD COLUMN sprint_phase TEXT;
```

## 三、七阶段定义

| 序号 | 阶段 | gstack 对应 | 核心产出物 | 必读 Skill | Exit 条件 |
|------|------|------------|-----------|-----------|-----------|
| 1 | DEFINE | Think | design_doc | spec-driven-development | design_doc_approved |
| 2 | PLAN | Plan | task_breakdown | planning-and-task-breakdown | task_breakdown_submitted |
| 3 | BUILD | Build | build_log | incremental-implementation | all_tasks_approved |
| 4 | VERIFY | Test | test_report | debugging-and-error-recovery | test_report_approved |
| 5 | REVIEW | Review | review_report | code-review-and-quality | review_report_approved |
| 6 | SHIP | Ship | ship_checklist | shipping-and-launch | ship_checklist_approved |
| 7 | REFLECT | Reflect | retrospective | documentation-and-adrs | retrospective_written |

## 四、WorkflowEngine 架构

```
workflow/
├── engine.ex              # 核心引擎:阶段流转、entry/exit 条件
├── phases.ex               # 7 阶段定义(纯数据)
├── context_query.ex        # context_queries 机制
├── artifact_service.ex     # phase_artifacts CRUD
└── sprint_service.ex       # sprints + sprint_tasks CRUD
```

### 与 handoff 状态机的集成

- **dispatch_task 时**:如果 CEO 有活跃 Sprint,创建 handoff 时附加 sprint_id
- **approve_work 时**:调用 `Engine.on_handoff_approved/2`,引擎检查是否推进阶段
- **report_completion 时**:如果 handoff 关联了 sprint_task,自动创建 build_log artifact

## 五、context_queries 机制

每个阶段声明它需要的历史上下文,系统自动拉取并注入到 agent 的 context prompt:

```elixir
context_queries: [
  {:artifact, phase: :define, type: :design_doc, status: :approved},
  {:artifacts, phase: :build, type: :build_log},
  {:memory, scope: :project, type: :decision}
]
```

## 六、专家角色模板(20 个)

### 管理层(4)
- CEO 首席执行官、CTO 技术总监、PM 产品经理、HR 人力资源

### 工程层(8)
- 前端、后端、全栈、DevOps、移动端、数据、AI 算法、UI/UX 设计师

### 质量层(4)
- 代码审查员、安全审计员、测试工程师、性能审计员

### 专项层(4)
- 技术文档工程师、数据库架构师、SRE、迁移工程师

## 七、前端可视化

新增 SprintPanel:7 阶段进度条 + 阶段详情 + 设计文档查看器 + 时间线

## 八、分步实施计划

1. 数据模型与建表(project_factory.ex + 3 个 schema)
2. Sprint CRUD 服务(sprint_service.ex + artifact_service.ex)
3. 阶段定义与 Engine 核心(phases.ex + engine.ex)
4. context_queries 机制(context_query.ex + streamer.ex 修改)
5. 工具集成(tool_executor.ex 新增 7 个工具)
6. Prompt 改造(streamer.ex CEO/executor prompt)
7. 专家角色模板(roles.ex 种子数据)
8. HTTP API(sprint_controller.ex + router.ex)
9. 前端可视化(SprintPanel + PhaseStepper 等)
10. 集成测试
