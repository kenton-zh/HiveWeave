# Spec: Agent 工作流标准化

> 来源: [Intent 文档](../intent/agent-workflow-standardization.md) | 状态: APPROVED
> 基于代码审查: 2025-06-25 | 用户确认: 2025-06-25

## Objective

将 agent-skills 六阶段生命周期（Define → Plan → Build → Verify → Review → Ship）融入 HiveWeave，让 CEO 遵循结构化工作流、专家 Agent 按需介入、用户拥有控制点和透明度。

**核心问题：** 当前 CEO 收到任务后立即派活给叶子，整个过程是黑箱——用户不知道进度、跑偏了也无感知、Agent 卡住了也没人知道。本质是缺乏结构化工作流和用户控制点。

**Who:** 项目管理者和最终决策者。
**Success looks like:** CEO 必须先完成 interview → refine → spec → plan 并获用户确认后才派活；专家在关键节点按需被调度；异常自动逐级上报；企业目标对齐所有 Agent。

## Tech Stack

| 层 | 现有 | 本轮改动 |
|---|---|---|
| Agent 编排 | `ai` SDK + Provider Factory | 改 system prompt，不换框架 |
| 后端 | Fastify + TypeScript | 扩展 `chat.ts` 上下文注入逻辑 |
| 前端 | React 19 + Vite + React Flow | GoalsPanel 已有，加 checkpoint 确认 UI |
| 数据库 | SQLite + Drizzle ORM | 已有 `goals_json` 字段，无需新表 |
| Agent Runtime | `AgentRuntime` (identity + context 分离) | 不改架构，只改 prompt 内容 |

## Commands

```
Build:   pnpm build
Test:    pnpm test
Lint:    pnpm lint
Dev:     pnpm dev
DB Push: pnpm db:push
```

## Agent 角色矩阵

### 组织架构（动态伸缩）

```
用户 (终极决策者)
 └── CEO                         → Define: interview → refine → spec → plan
      ├── 前端经理 (可选)         → Orchestrate: 拆解 → 调度专家 → 质量把关
      │   └── 叶子 × N           → Build: 写代码
      ├── 后端经理 (可选)         → Orchestrate
      │   └── 叶子 × N           → Build
      ├── Test Engineer (常驻)    → Verify: 按需调度
      ├── Code Reviewer (常驻)    → Review: 按需调度
      ├── Security Auditor (常驻) → Review: 按需调度
      └── Web Perf Auditor (常驻) → Review: 按需调度
```

- 小项目：CEO + 1 叶子即可。经理和专家常驻编制但不被调度就不消耗 token。
- 大项目：完整层级。专家由经理在关键节点调度。
- 组织框架由用户根据项目规模决定，不自动生成。

### 角色职责与权限

| 角色 | 职责 | 权限类型 | 核心工具 |
|---|---|---|---|
| CEO | Define + 最终仲裁 | coordinator | interview, refine, write_spec, plan_tasks, dispatch, approve |
| 经理 (Manager) | Orchestrate + 质量把控 | coordinator | read_logs, dispatch, review_code, schedule_expert, approve/reject |
| 叶子 (Module Dev) | Build: 写码 + 单测 + 日志 | executor | bash, grep, write_file, run_tests, write_work_log |
| Test Engineer | Verify: 测试验证 | executor | run_tests, analyze_coverage, write_test_report |
| Code Reviewer | Review: 代码质量审查 | executor | grep, read_file, write_review, flag_issues |
| Security Auditor | Review: 安全漏洞扫描 | executor | grep, read_file, run_security_scan, write_audit_report |
| Web Perf Auditor | Review: 性能审计 | executor | audit_perf, analyze_bundle, write_perf_report |

## 六阶段生命周期

### Phase 0: 企业目标（持续）

CEO 和用户共同在 GoalsPanel 设定目标。所有 Agent 的 context 自动注入 `formatGoalsForPrompt()` 输出。用户可随时更新，Agent 下次对话自动加载。

### Phase 1: Define（CEO 执行，用户确认后进入 Phase 2）

```
interview → refine → spec → plan
   │          │        │       │
   └─ 用户确认 ─┘        └─ 用户确认 ─┘
```

**Spec 必须包含"目标对齐"声明：**
```
## 目标对齐
本任务服务于企业目标:
- Objective: [引用企业目标]
- 关联 Key Result: [具体哪条 KR]
```

### Phase 2: Plan（CEO 执行）

将 spec 拆解为任务列表。每项任务指定负责的经理/叶子。

输出格式：
```markdown
- [ ] Task: [描述]
  - Assignee: [Agent 花名]
  - Acceptance: [验收条件]
  - Verify: [验证方式]
  - Depends on: [前置任务]
```

用户确认 plan 后，CEO 开始 dispatch。

### Phase 3: Build（叶子执行）

叶子 Agent 写代码 + 跑单测 + 写工作日志 + report_completion。

### Phase 4: Verify & Review（经理调度专家）

经理在三个关键节点调度专家：

| 节点 | 触发条件 | 调度对象 | 产出 |
|---|---|---|---|
| 模块完工 | 叶子 report_completion | Code Reviewer | review 报告（通过/打回+原因） |
| 跨模块集成前 | 多个叶子都完成 | Test Engineer | 集成测试结果 |
| 用户手动触发 | 用户觉得不放心 | 任意专家 | 对应报告 |

### Phase 5: Ship（暂本轮 out of scope，下一轮实现）

代码落地 + 最终验收 + 用户确认上线。

## 控制点与上报链路

### 用户关口

```
关口 1: Define 完成后（spec + plan）→ 用户审批 → 才能 dispatch
关口 2: 最终上线前 → 用户审批 → 才能 ship
```

### 异常递级上报

```
叶子 → 经理 → CEO → 用户

触发条件（硬编码）:
- 测试连续失败 ≥ 3 次
- Reviewer 打回 ≥ 3 次（同一 task）
- 安全扫描发现高危漏洞 (severity=critical)
- Agent 超时无响应 > 15 分钟
```

上报逻辑：叶子遇到异常 → message_superior → 经理尝试解决 → 解决不了 → message_superior → CEO 尝试解决 → 解决不了 → send_message(user) → 用户介入。

## 实现方案

### 策略: Prompt-First（先 A 后 B）

**Phase A（本轮）：** Prompt 约束
1. 修改 CEO 的 system prompt：注入六阶段工作流指令 + 目标对齐要求
2. 为四个专家角色编写专用 system prompt（含介入时机、产出格式、质量门禁）
3. 修改 `chat.ts` 的 `buildSystemPrompt` 动态段：加 checkpoint 状态注入
4. GoalsPanel 已有，`formatGoalsForPrompt` 已有，只加对齐校验指令

**Phase B（后续）：** 平台加固
1. Agent 状态机加阶段标记（`workflow_phase: defining | planning | building | verifying | reviewing`）
2. 前端加 checkpoint 审批 UI（替代聊天确认）
3. 自动上报：超时/失败计数 → 自动 notify 用户

### 改动文件

| 文件 | 改动 |
|---|---|
| `packages/agent-runtime/src/agent-runtime.ts` | 改 CEO role 模板：注入六阶段流程 + 目标对齐；新增四个专家的 role 模板 |
| `apps/server/src/routes/chat.ts` | 扩展 `buildGoalsBlock` 或新增 `buildCheckpointBlock`；添加上报触发逻辑 |
| `apps/web/src/components/GoalsPanel.tsx` | 已有，可能改样式/交互 |
| `packages/core/src/project-service.ts` | `formatGoalsForPrompt` 已有，可能微调格式 |

### 专家 Agent System Prompt 模板

#### Test Engineer
```
You are a Test Engineer in HiveWeave. You are called in at two points:
1. When a module is complete — write and run tests, report pass/fail
2. Before cross-module integration — run integration tests, flag conflicts

Output format: Test Report with pass/fail count, failures detail, and recommendation (pass/reject).
You do NOT write application code. You only verify.
```

#### Code Reviewer
```
You are a Code Reviewer in HiveWeave. Called when a leaf agent completes a module.
Review across: correctness, readability, architecture, security, performance.
Output: Review with severity labels (critical/warning/nit) and a pass/reject recommendation.
You do NOT write code. You only review.
```

#### Security Auditor
```
You are a Security Auditor in HiveWeave. Called before release or when user triggers.
Scan for: OWASP Top 10, secrets in code, injection vectors, unsafe dependencies.
Output: Audit Report with vulnerabilities by severity and fix recommendations.
You do NOT write code. You only audit.
```

#### Web Perf Auditor
```
You are a Web Performance Auditor in HiveWeave. Called before release or when user triggers.
Audit: Core Web Vitals, bundle size, rendering performance, network waterfall.
Output: Perf Audit with metrics, bottlenecks, and optimization recommendations.
You do NOT write code. You only audit.
```

## Testing Strategy

- **Unit tests:** `packages/core/src/project-service.test.ts` — `formatGoalsForPrompt` 输出验证
- **Integration:** 创建测试项目 → 添加 CEO + 叶子 + Reviewer → 验证 dispatch → review → report 链路
- **Manual:** 用户创建真实项目，走完整 Define → Plan → Build → Verify 流程，验证控制点生效

## Boundaries

- **Always:**
  - CEO 必须走 Define → Plan 才能 dispatch
  - spec 必须包含目标对齐声明
  - 专家 Agent 输出标准化报告格式
  - 异常超过阈值时自动升级

- **Ask first:**
  - 数据库 schema 变更
  - 新增 npm 依赖
  - Agent 角色权限变更
  - 平台层加固（Phase B）

- **Never:**
  - 专家 Agent 直接写代码
  - CEO 跳过 Plan 直接 dispatch
  - 叶子跨级读其他叶子的私有记忆
  - 自动触发专家（必须经理或用户调度）

## Success Criteria

1. CEO 新建项目后，走完 interview → refine → spec → plan 流程，输出 spec 文档和 task list，用户确认后才开始 dispatch
2. 叶子完成模块后，经理能在"模块完工"节点手动调度 Reviewer
3. 测试连续失败 ≥ 3 次时，异常自动上报到用户
4. 企业目标（GoalsPanel 内容）出现在所有 Agent 的 system prompt 中
5. CEO 的 spec 包含"目标对齐"段落，明确引用了企业目标中的具体 KR
6. 小项目（≤3 Agent）不强制要求完整层级
7. 专家 Agent 未被调度时不消耗任何 token

## 已决策事项

1. **Spec/Plan 存储：** 复用 `projects` 表 `charter_json` 字段，不新建表。存储在工作区 `.hiveweave` 内。
2. **用户确认 UI：** 复用现有 `ApprovalDialog` 模式——Agent 发起审批请求 → 前台弹窗 → 用户点击同意/拒绝。Phase A 不改 UI 组件，只新增审批请求类型（`workflow_checkpoint`）。
3. **专家 Agent 默认创建：** 每个新项目自动创建 CEO + HR + 4 个专家 Agent。中文岗位名称 + 花名（从 `names.ts` 花名池随机分配）。多语言支持后续迭代。
4. **调度触发：** 经理使用结构化命令调度专家。格式：`/review <module>` → Reviewer，`/test <module>` → Test，`/audit <module>` → Security，`/perf <module>` → Perf。
5. **向量语义检索：** 下一轮。
