# HiveWeave 项目健康度检查报告

> **检查时间**: 第 1 天 19:34:35  
> **检查范围**: 代码质量、运行状态、依赖完整性、安全、测试、性能  
> **检查人**: 半夏 (QA Engineer)

---

## 1. 总体结论

| 维度 | 状态 | 说明 |
|------|------|------|
| 构建完整性 | ✅ 通过 | server/web/db 均能构建 |
| 类型安全 | ✅ 通过 | 各包 typecheck 无报错 |
| 代码质量 | ⚠️ 风险 | 存在 6 个严重/主要问题 |
| 安全性 | ❌ 风险 | 存在明文密钥、未授权访问等 4 个问题 |
| 测试覆盖 | ❌ 缺失 | 零测试文件，核心逻辑无覆盖 |
| 性能 | ⚠️ 风险 | 存在 O(n) 扫描、大包警告 |

**综合评级**: 🟡 **有条件通过** — 构建可用，但存在安全和正确性风险，建议在上线前修复 Critical/Major 问题。

---

## 2. 构建与依赖状态

### 2.1 构建结果
| 包 | 命令 | 结果 | 备注 |
|----|------|------|------|
| `@hiveweave/server` | `pnpm -C apps/server typecheck` | ✅ 通过 | TypeScript 无类型错误 |
| `@hiveweave/web` | `pnpm -C apps/web build` | ✅ 通过 | Vite 构建成功 |
| `@hiveweave/db` | `pnpm -C packages/db typecheck` | ✅ 通过 | Drizzle schema 正常 |

### 2.2 依赖完整性
- 根 `package.json` 包含 `turbo`、`typescript`、`effect`、`@modelcontextprotocol/sdk` 等基础依赖
- 工作区依赖通过 `workspace:*` 协议正确链接
- 构建时出现 `better-sqlite3` / `esbuild` 构建脚本警告，需在 CI 中 `pnpm approve-builds`

### 2.3 前端性能警告
- Web 构建产生 547.57 kB 的 `index.js` 主包，Vite 已发出 chunk size 警告
- **建议**: 对重型库（`pixi.js`、`@xyflow/react`）启用 `manualChunks` 拆分

---

## 3. 代码质量审查

### 3.1 Critical 问题

#### 🔴 P1: seed.ts 向全局元数据库写入项目范围数据
- **文件**: `packages/db/src/seed.ts`
- **问题**: 向全局 meta DB 插入 `agents`、`modules`、`memories`，但这些表属于 per-project 数据库
- **影响**: 全局注册表被污染，项目初始化可能因 schema 不匹配失败
- **修复方向**: seed 应接受 projectId / projectDb，写入对应项目数据库

#### 🔴 P2: org 路由缺少认证与授权
- **文件**: `apps/server/src/routes/org.ts`
- **问题**: 创建/修改/删除 agent、获取组织树等接口均未校验调用者身份与权限
- **影响**: 任何客户端均可接管组织管理，存在完整权限绕过风险
- **修复方向**: 在 Fastify 路由层增加 auth middleware，按 `PermissionType` 限制 HR/PM 专属操作

### 3.2 Major 问题

#### 🟠 P3: UUID 前缀解析存在歧义
- **文件**: `packages/core/src/org-service.ts:189`
- **问题**: `resolveAgent` 在 UUID 前缀匹配到多条时返回 `matches[0]`，可能导致操作打到错误 agent
- **修复方向**: 多匹配时返回 `null` 或抛出歧义异常，要求提供完整 ID

#### 🟠 P4: API Key 明文存储
- **文件**: `packages/db/src/client.ts`（`llm_models.api_key`）
- **问题**: 密钥以明文 TEXT 存入 SQLite
- **影响**: 文件系统可读即密钥泄露
- **修复方向**: 使用 OS keychain / 环境变量注入，或至少做 AES 加密存储

#### 🟠 P5: chat.ts 通过 O(n) 扫描解析 agent→project 映射
- **文件**: `apps/server/src/routes/chat.ts:160`
- **问题**: `getProjectServices` 遍历所有项目并逐一开 DB 连接
- **影响**: 项目增多后延迟线性增长，连接数可能打满
- **修复方向**: 建立 `agentId → projectId` 全局索引

#### 🟠 P6: generateNextShortId 竞态条件
- **文件**: `packages/core/src/org-service.ts:42`
- **问题**: 读取所有 agent 计算 max shortId，并发创建时可能重复
- **修复方向**: 使用事务 + `SELECT ... FOR UPDATE` 或独立计数器表原子递增

### 3.3 Minor 问题

| 问题 | 文件 | 说明 |
|------|------|------|
| 未使用的 `assertRole` | `packages/core/src/tool-executor.ts:85` | HR 工具无实际角色门控 |
| `initProjectDbTables` 未调用 | `packages/db/src/client.ts:180` | 项目库可能缺少必需表 |
| `any` 类型滥用 | `packages/core/src/org-service.ts` | buildTree/getOrgTree 使用 any |
| 重复 `initProject` | `apps/server/src/routes/chat.ts:230` | 每次请求都执行初始化 |

---

## 4. 安全审计

> 注：自动安全审计工具因输出解析失败未能完成，以下基于代码审查结果。

### 4.1 发现的安全风险

| 风险 | 级别 | 位置 | 说明 |
|------|------|------|------|
| 敏感数据暴露 | 🔴 High | `llm_models.api_key` | 明文密钥 |
| 未授权访问 | 🔴 High | `apps/server/src/routes/org.ts` | 无 auth middleware |
| 权限绕过 | 🟠 Medium | `packages/core/src/tool-executor.ts` | HR_ONLY_TOOLS 未实际执行角色检查 |
| 日志信息泄露 | 🟡 Low | 全局 | 未见敏感请求体脱敏处理 |

---

## 5. 测试覆盖

- **测试文件**: 0 个
- **覆盖范围**: 核心逻辑（DB factory、agent 解析、tree 构建、SSE 流、fallback 链）均无单测
- **风险**: 修复上述问题时缺乏回归保障

---

## 6. 性能评估

- **前端**: 主包过大（547KB），需 code splitting
- **后端**: O(n) 项目扫描、未索引短 ID 生成、连接复用不足
- **数据库**: 未见慢查询，但缺乏索引评估证据

---

## 7. 修复建议优先级

### 立即处理 (P0)
1. **为 org 路由增加认证中间件**
2. **修复 seed.ts 数据污染问题**

### 近期处理 (P1)
3. **API Key 加密/外部化存储**
4. **UUID 前缀歧义修正**
5. **建立 agentId→projectId 索引**

### 后续处理 (P2)
6. **shortId 生成改为事务/计数器**
7. **前端 chunk 拆分**
8. **补充核心模块单元测试**

---

## 8. 结论

项目当前处于 **"可运行但需加固"** 状态：
- ✅ 构建链路完整、TypeScript 类型通过
- ❌ 缺少测试、存在安全与正确性风险
- 🟡 建议在完成组织架构上线前，至少修复 P0 问题
