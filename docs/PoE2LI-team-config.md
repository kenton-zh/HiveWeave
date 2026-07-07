# PoE2LI Team Configuration

> 由 CEO 在创建项目时参考。纪律套装说明见 CLAUDE.md 或 ask HR via `list_available_skills("discipline")`.

## Project Profile

- **项目**: 流放漓 (PoE2LI) — PoE2 智能工具站
- **类型**: 全栈 Web + 爬虫 + AI Agent 后台
- **规模**: 中大型，预计 6-10 人
- **推荐组织范式**: Tech Lead 制，3 层 (CEO → 领域经理 → Executor)

## Wave 1 — CEO Direct Reports

| 角色 | 花名 | 纪律套装 | 关键工具技能 |
|------|------|----------|-------------|
| CEO | 漓总 | spec-driven-development, planning-and-task-breakdown, context-engineering | documentation-and-adrs |
| 后端经理 | (HR 分配) | planning-and-task-breakdown, code-review-and-quality, shipping-and-launch | api-and-interface-design, ci-cd-and-automation |
| 前端经理 | (HR 分配) | planning-and-task-breakdown, code-review-and-quality, design-consultation | frontend-ui-engineering, design-review |

## Wave 2 — Team Leads' Direct Reports

### 后端团队

| 角色 | 纪律套装 | 关键工具技能 |
|------|----------|-------------|
| 后端开发 (FastAPI+SQL) | self-review, incremental-implementation, test-driven-development | api-and-interface-design |
| 爬虫开发 (poe2db/poe.ninja) | self-review, incremental-implementation | source-driven-development, websearch |
| QA | code-review-and-quality, security-and-hardening, debugging-and-error-recovery | browser-testing-with-devtools |

### 前端团队

| 角色 | 纪律套装 | 关键工具技能 |
|------|----------|-------------|
| 前端开发 (Next.js) | self-review, incremental-implementation, test-driven-development | frontend-ui-engineering, browser-testing-with-devtools |
| 设计 (可选, P0 后加) | design-consultation, design-review | design-html, design-shotgun |

## Project Rules — 注入所有 Agent 的 context prompt

```markdown
## 项目背景 — 流放漓 (PoE2LI)
你正在开发一个面向中文 PoE2 玩家的智能工具站。核心闭环: PoB Code 解码 → AI 生成中文作业本 → 前端展示。

## 合规红线（全员必读）
### A 级红线（永不触碰）
- 禁止抓取/逆向官方游戏客户端资源文件
- 官方 API 调用严格遵守 X-Rate-Limit-* 头，命中 429 必须指数退避
- 所有官方 API 数据必须走缓存层，禁止"每次用户请求触发一次官方调用"
- OAuth 申请需真人撰写，去除 LLM 痕迹

### B 级合规（爬虫专属）
- 第三方采集（poe.ninja 等）隔离在 collectors/grey/ 目录
- 低频 + 拟人化访问 + 尊重 robots.txt
- 预留一键切换合规源开关
- AGPL 代码只参考不照搬（注意 License 传染）

## 技术栈
- 前端: Next.js (React) + TypeScript + TailwindCSS
- 后端: Python FastAPI + Celery + PostgreSQL + pgvector
- AI: DeepSeek V4 Flash 或 mimo-v2.5（便宜优先）
- 部署: Docker + docker-compose

## "代码 vs AI" 铁律
凡是代码能精确做的，绝不交给 AI:
- 确定性操作（base64/zlib 解码、XML 解析）→ 代码
- 模糊推理（归纳 Build 思路、生成中文作业本）→ AI
- AI 产出必须是结构化数据，支持重试 + 人工抽检 + 规则兜底

## 当前状态
P0 核心闭环部分完成。需要:
1. 文档解析入库（Word/PDF → 知识条目）
2. 前端完善 + 上线
3. P1 模块（信息库、问答 RAG）
```

## CEO 启动指令（示例）

```
CEO 漓总，这是流放漓项目的设计文档和团队配置。

第一步: 读项目文档，了解 PoE2LI 的技术栈、合规要求和当前进度。
第二步: 按 Tech Lead 制搭建组织。第一波招后端经理和前端经理。
第三波: 各经理拆任务后自行招人。

项目规则已注入 charter.project_rules，所有 Agent 都会读到。
用户参与度: medium — 每个肉眼可见的节点验收，不参与开发过程。
```
