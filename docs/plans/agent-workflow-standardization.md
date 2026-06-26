# Plan: Agent 工作流标准化

> 来源: [Spec](../spec/agent-workflow-standardization.md) | 日期: 2025-06-25

## 实现顺序

```
Task 1 (专家 Prompt)  Task 2 (CEO Prompt)  Task 3 (项目模板)
       │                      │                   │
       └──────┬───────────────┘                   │
              ▼                                   │
       Task 4 (结构化命令调度) ←────────────────────┘
              │
              ▼
       Task 5 (审批 Workflow Checkpoint)
              │
              ▼
       Task 6 (异常上报触发)
              │
              ▼
       Task 7 (目标对齐校验)
```

**并行:** Task 1 + Task 2 + Task 3 可同时开发（互不依赖）
**顺序:** Task 4 依赖 Task 1（专家必须先存在才能被调度）和 Task 3（专家 Agent 必须已创建）
**顺序:** Task 5 → Task 6 → Task 7 依赖 Task 2（CEO 流程必须先跑通）

## 风险与缓解

| 风险 | 概率 | 缓解 |
|---|---|---|
| CEO 忽略 prompt 指令直接 dispatch | 中 | prompt 中加 MANDATORY 标记 + 用户审批关口硬拦截 |
| 专家 Agent context 不足导致审查无效 | 中 | 调度命令附带模块文件列表，专家只读指定文件 |
| 结构化命令解析与现有 hiveweave__ 工具命名冲突 | 低 | `/review` 使用不同前缀，不与 `hiveweave__` 冲突 |
| 新项目自动创建太多 Agent 导致 token 浪费 | 低 | 专家 inactive 状态不参与消息轮询，不消耗 token |

## 验证检查点

1. **Task 1-2 完成后：** 创建测试项目 → 查看 CEO system prompt 是否包含六阶段指令
2. **Task 3 完成后：** 新建项目 → 验证 6 个 Agent（CEO + HR + 4 专家）自动创建
3. **Task 4 完成后：** 经理发送 `/review frontend` → 验证 Reviewer 被唤醒并输出报告
4. **Task 5 完成后：** CEO 完成 spec → 验证前台弹窗审批 → 用户同意后 CEO 继续 dispatch
5. **Task 6 完成后：** 模拟测试连败 3 次 → 验证用户收到通知
6. **Task 7 完成后：** CEO 输出 spec → 验证包含"目标对齐"段落并引用 KR
