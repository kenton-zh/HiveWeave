# HiveWeave

AI 工程组织 — 多 Agent 层级协作编程助手。

Agent 有职级、记忆可继承、离职有交接。用户可以像管理真实团队一样管理 AI 团队。

## 核心特性

- 动态层级组织架构（架构师 → 经理 → 执行者）
- 三层记忆系统（项目共享 / Agent 私有 / 归档）
- Handoff 交接机制（解散时总结 → 移交 → 归档 → 可复活）
- Merge 合并机制（冲突检测 → 仲裁 → 合成新记忆）
- 协调型 / 执行型权限矩阵
- 跨级直达通信

## 技术栈

- Backend: Node.js / TypeScript + Fastify
- Frontend: React + Vite + React Flow
- Memory: SQLite + sqlite-vec
- Agent: Claude Agent SDK
- Sandbox: Docker

## 文档

- [MVP 技术蓝图](./docs/AI工程组织_MVP蓝图.md)
