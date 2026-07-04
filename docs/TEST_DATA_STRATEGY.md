# HiveWeave 测试数据方案
> 目标：让 ExUnit 与 Playwright 都能在独立、可重复、快速启动的环境中运行。

## 1. 后端 ExUnit 数据策略

### 1.1 数据库
- **Meta DB**: SQLite 内存库（`:memory:`），避免磁盘 IO 与并发冲突
- **Project DB**: SQLite 内存库，每个测试文件/describe 块独立创建
- **迁移**: 使用 `Ecto.Adapters.SQL.query!` 在 `test_helper.exs` 或 `setup` 块中建表，避免依赖 `mix ecto.migrate`

### 1.2 工厂模式
- 定义 `HiveWeave.TestFactory` 模块，提供快速构造函数：
  - `agent_factory/1` — 默认 Agent 属性，支持覆盖
  - `project_factory/1` — 默认 Project 属性
  - `model_factory/1` — 默认 LLM Model 属性
  - `template_factory/1` — 默认 Agent Template 属性
- 所有工厂函数返回 `Map`，不直接插入 DB，由测试决定是否插入

### 1.3 测试夹具（Test Fixtures）
- `test/support/fixtures.ex` 定义常用场景：
  - `setup_project/0` — 创建项目 + 元数据
  - `setup_agent/1` — 在指定项目下创建 Agent
  - `setup_tree/2` — 创建父子层级关系
  - `setup_permissions/2` — 创建权限规则

### 1.4 清理策略
- 每个测试使用 `setup` 块包装，确保事务回滚或显式删除
- 避免测试间依赖，禁止共享状态

## 2. 前端 Playwright 数据策略

### 2.1 Mock Server
- 使用 `msw` (Mock Service Worker) 拦截 `/api/*` 请求，返回预设 JSON
- 优点：不依赖真实后端，测试稳定、快速、可离线运行
- 缺点：不验证后端实现，需配合后端 ExUnit 覆盖真实逻辑

### 2.2 测试数据文件
- `test/fixtures/projects.json` — 预设项目数据
- `test/fixtures/agents.json` — 预设 Agent 数据
- `test/fixtures/chat.json` — 预设聊天历史
- Playwright 测试通过 `fs.readFileSync` 加载，注入到 MSW handler

### 2.3 真实后端模式（可选）
- 当需要端到端验证时，启动真实 Elixir 后端（端口 4000）
- 使用 `test/support/seed.exs` 预置数据
- Playwright 配置 `webServer` 自动启动/停止

## 3. 共享测试数据规范

### 3.1 ID 生成
- 使用 `Ecto.UUID.generate()` 生成前端测试中的 ID
- 确保前后端 ID 格式一致

### 3.2 时间戳
- 使用 `System.system_time(:millisecond)` 生成时间戳
- 前端测试使用固定时间戳，避免时间依赖

### 3.3 枚举值
- `status`: `"online" | "offline" | "busy"`
- `permission_type`: `"readonly" | "readwrite"`
- `role`: `"coordinator" | "executor" | "specialist"`

## 4. 实施优先级
1. **P0**: 后端 `TestFactory` + `test_helper.exs` 内存库配置
2. **P1**: 前端 MSW mock 基础配置
3. **P2**: 前后端共享 fixture 数据文件
4. **P3**: 真实后端 seed 脚本（用于 E2E）
