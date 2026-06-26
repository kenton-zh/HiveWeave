# Intent: Agent 工作流标准化

> 来源: interview-me 采访确认 | 日期: 2025-06-25
> 下游: spec-driven-development

## 确认的意图

- **Outcome:** 将 agent-skills 六阶段生命周期融入 HiveWeave。组织架构按项目规模动态伸缩——小项目 2-3 个 Agent，大项目完整层级。CEO 做 Define，中层经理做 Orchestrate（拆解 + 调度专家 + 质量把关），叶子只做 Build。四个专家 Agent（Test / Reviewer / Security / Perf）常驻编制但按需调度。企业目标面板是所有 Agent 的北极星，启动注入 context + 阶段切换时对齐校验。

- **User:** 在方案审批和最终上线两个关口介入；异常递级上报到用户为止；组织框架由用户根据项目规模决定。

- **Why now:** CEO 无脑跑进度 → 黑箱 → 缺乏控制点和透明度。需要结构化工作流和用户知情权。

- **Success:**
  1. CEO 完成 interview → refine → spec → plan 获用户确认后才派活
  2. 中级经理在模块完工 / 集成前 / 用户手动触发三个节点调度专家
  3. 异常自动逐级上报（叶子→经理→CEO→用户）：测试连败 3 次 / Reviewer 多次打回 / 安全高危 / 超时卡死
  4. 企业目标注入 Agent context，spec 必须对齐企业目标
  5. 组织架构随项目规模动态伸缩
  6. Token 经济：专家常驻但不调用不消耗

- **Control mode:** 方案关口（用户审批 spec） + 异常上报（递级升级，解决不了才到用户）

- **Constraint:** Prompt 约束先行（改 CEO + 专家 system prompt），不改底层 agent-runtime 架构。组织框架由用户决定，不自动生成。

- **Priority ordering:**
  - 本轮：CEO 六阶段 prompt、专家调度 prompt、控制点/上报链路、企业目标对齐
  - 下一轮：向量语义检索、Ship 阶段（依赖本轮 Build/Verify/Review 先跑通）
  - 持续做：权限系统细化（不改架构，在现有基础上加规则）

- **Out of scope (本轮):** 像素办公室动画、向量语义检索、实际部署（Ship）、权限系统重构
