# Tasks: Agent 工作流标准化

> 来源: [Plan](../plans/agent-workflow-standardization.md) | 日期: 2025-06-25

---

## Task 1: 新增专家 Agent System Prompt 模板

**描述:** 在 `agent-runtime.ts` 的 `buildSystemPrompt()` 中为四个专家角色（Test Engineer、Code Reviewer、Security Auditor、Web Perf Auditor）编写专用 prompt 模板。每个模板定义：角色身份、介入时机、输出格式、行为约束。

**验收:**
- Test Engineer prompt 包含：测试触发条件、报告格式（pass/fail count + failures detail + recommendation）
- Code Reviewer prompt 包含：五轴审查标准（correctness/readability/architecture/security/performance）、严重级别标签（critical/warning/nit）、pass/reject 建议
- Security Auditor prompt 包含：OWASP Top 10 扫描清单、漏洞严重度分级、修复建议格式
- Web Perf Auditor prompt 包含：Core Web Vitals 指标、瓶颈分析、优化建议格式
- 所有专家 prompt 明确声明："不写代码，只做审查/测试/审计"

**验证:** 创建测试项目 → 获取专家的 system prompt → 确认包含上述内容

**文件:**
- `packages/agent-runtime/src/agent-runtime.ts`

---

## Task 2: 改造 CEO System Prompt 注入六阶段工作流

**描述:** 修改 CEO role 的 `buildSystemPrompt()` 模板。注入以下指令：
1. 收到新任务后，必须先走 Define 阶段：interview → refine → spec → plan
2. Spec 格式要求：Objective / Tech Stack / Project Structure / Success Criteria / 目标对齐段落（引用 GoalsPanel 中的 Objective 和具体 KR）
3. Plan 格式要求：任务列表，每项含 Assignee / Acceptance / Verify / Depends on
4. 完成 Define + Plan 后，必须调用 `hiveweave__request_approval` 发起审批，等待用户确认后才能 dispatch
5. 明确禁止：跳过 Define → Plan 直接 dispatch、在用户确认前开始写代码

**验收:**
- CEO prompt 包含六阶段指令
- CEO prompt 包含 Spec/Plan 格式要求
- CEO prompt 包含审批门禁规则
- CEO prompt 明确标注 MANDATORY 的禁止行为

**验证:** 创建测试项目 → 获取 CEO system prompt → 逐条检查指令 → 发送任务给 CEO → 观察是否先输出 spec 而非直接 dispatch

**文件:**
- `packages/agent-runtime/src/agent-runtime.ts`

---

## Task 3: 新项目自动创建专家 Agent

**描述:** 修改项目创建流程（`server/index.ts` 或 `project-service.ts`），新项目初始化时自动创建 6 个 Agent：
- CEO（coordinator，中文花名 + "CEO"）
- HR（coordinator，中文花名 + "人力资源总监"）
- Test Engineer（executor，中文花名 + "测试工程师"）
- Code Reviewer（executor，中文花名 + "代码审查员"）
- Security Auditor（executor，中文花名 + "安全审计员"）
- Web Perf Auditor（executor，中文花名 + "性能审计员"）

其中 4 个专家初始状态为 `inactive`（不被消息轮询唤起，不消耗 token），只有被结构化命令调度时才激活。

**验收:**
- 新项目创建后自动生成 6 个 Agent
- 所有 Agent 有中文花名（从 `names.ts` 随机分配，不重复）
- 所有 Agent 有中文岗位名称
- 专家初始 status 为 `inactive`

**验证:** 创建新项目 → 通过 API/UI 查看 agent 列表 → 确认 6 个 Agent 存在且名称/岗位正确 → 确认专家状态为 inactive

**文件:**
- `packages/core/src/project-service.ts`（或项目创建路由）
- `apps/server/src/index.ts`（如创建逻辑在此）
- `packages/shared/src/names.ts`（可能需要扩展花名池）

---

## Task 4: 经理结构化命令调度专家

**描述:** 在 `chat.ts` 或 `tool-executor.ts` 中识别经理发送的结构化命令：
- `/review <module>` → 唤醒 Code Reviewer，注入模块文件列表 context
- `/test <module>` → 唤醒 Test Engineer，注入测试目标 context
- `/audit <module>` → 唤醒 Security Auditor
- `/perf <module>` → 唤醒 Web Perf Auditor

调度流程：
1. 经理发送命令
2. 系统解析命令，找到对应的专家 Agent
3. 激活专家（status → active），注入模块上下文（文件列表、任务描述）
4. 专家执行审查/测试/审计，输出标准化报告
5. 报告返回给经理
6. 专家恢复 inactive

**验收:**
- `/review <module>` 能唤醒 Reviewer 并输出审查报告
- `/test <module>` 能唤醒 Test Engineer 并输出测试报告
- 报告完成后专家恢复 inactive
- 无效模块名返回友好错误

**验证:** 经理发送 `/review frontend` → 观察 Reviewer Agent 是否被唤醒 → 确认报告内容 → 确认完成后 Reviewer 恢复 inactive

**文件:**
- `apps/server/src/routes/chat.ts`
- `packages/agent-runtime/src/agent-runtime.ts`（可能需要新增调度方法）

---

## Task 5: 新增 Workflow Checkpoint 审批类型

**描述:** 扩展现有 `ApprovalService`，新增 `workflow_checkpoint` 审批请求类型。CEO 完成 Define 后调用 `hiveweave__request_approval` 发起审批，参数包含 spec/plan 摘要。前端 `ApprovalDialog` 已支持任意审批类型，只需确保新类型能正常展示。

**审批请求格式:**
```json
{
  "type": "workflow_checkpoint",
  "phase": "define_complete",
  "summary": "Spec + Plan 摘要...",
  "details": "完整 spec/plan 链接或内容"
}
```

**验收:**
- CEO 发起 `workflow_checkpoint` 审批后，用户看到弹窗
- 弹窗显示 Spec/Plan 摘要
- 用户同意 → CEO 继续 dispatch
- 用户拒绝 → CEO 收到反馈修改
- `remember` 选项可用（同一项目后续 checkpoint 自动通过）

**验证:** CEO 完成 Define → 用户看到审批弹窗 → 点击同意 → CEO 开始 dispatch → 点击拒绝 → CEO 修改 spec

**文件:**
- `apps/server/src/routes/chat.ts`（新增审批请求处理）
- `apps/server/src/routes/permissions.ts`（可能需要扩展）
- `apps/web/src/components/ApprovalDialog.tsx`（可能需要小改）
- `packages/agent-runtime/src/agent-runtime.ts`（prompt 中要求 CEO 调用审批）

---

## Task 6: 异常自动上报

**描述:** 在消息轮询/Agent 调度逻辑中新增异常检测和上报：
1. **测试连败检测：** Test Engineer 连续报告失败 ≥ 3 次（同一 task） → 自动 notify 上级
2. **打回循环检测：** Reviewer 对同一 task 打回 ≥ 3 次 → 自动 notify 上级
3. **安全高危检测：** Security Auditor 报告 severity=critical → 自动 notify 用户
4. **超时检测：** Agent 距离上次消息超过 15 分钟且有待完成任务 → 自动 notify 上级

上报链路：`notify_superior` → 上级尝试解决 → 解决不了 → 继续上报 → 直到用户。

**验收:**
- 连续测试失败 ≥ 3 次触发自动上报
- 安全高危漏洞触发上报直达用户
- 超时 15 分钟触发上报
- 上报历史可追溯

**验证:** 模拟测试连败 → 确认上报消息生成 → 模拟安全高危 → 确认用户收到通知 → 模拟超时 → 确认上报

**文件:**
- `apps/server/src/game-time-scheduler.ts`（新增超时扫描）
- `apps/server/src/routes/chat.ts`（新增失败计数 + 上报逻辑）
- `packages/core/src/inbox-service.ts`（可能需要新增通知方法）

---

## Task 7: 企业目标对齐校验

**描述:** 增强 CEO 的 system prompt，要求 spec 必须包含"目标对齐"段落。同时增强 `formatGoalsForPrompt` 输出，在 Agent context 中明确标注：
1. 当前项目 Objective
2. Key Results（含进度）
3. **对齐指令：** "你的 spec 必须声明本任务服务于哪条 KR。如果没有任何 KR 相关，请向用户确认方向是否正确。"

**验收:**
- CEO 的 spec 输出包含 `## 目标对齐` 段落
- spec 明确引用了具体 KR
- 如果任务与目标不匹配，CEO 主动提醒用户

**验证:** 设定企业目标 → 给 CEO 发任务 → 检查 spec 输出是否包含目标对齐 → 发一个跟目标无关的任务 → CEO 是否提醒用户

**文件:**
- `packages/agent-runtime/src/agent-runtime.ts`（改 prompt）
- `packages/core/src/project-service.ts`（`formatGoalsForPrompt` 微调）
