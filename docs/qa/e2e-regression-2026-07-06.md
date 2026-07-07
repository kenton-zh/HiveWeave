# HiveWeave E2E 回归测试报告

**日期**：2026-07-06（修复后）
**执行人**：TRAE（integrated_browser MCP + PowerShell API）
**对比基线**：[e2e-2026-07-06.md](./e2e-2026-07-06.md)
**测试范围**：A-H 同流程回归
**Office 模块**：**跳过**（用户声明未正式开发）

---

## 结论速览

| 状态 | 数量 |
|------|------|
| ✅ 已修复 | 0 |
| 🟡 部分改善 | 2（BUG-009、BUG-006）|
| ❌ 仍存在 | 3（BUG-010、BUG-007、BUG-001） |
| 🆕 新发现 | 2（BUG-011、BUG-012） |
| 行为变化（设计性） | 1（CEO 主动 charter） |

**关键结论**：
- 🚨 **BUG-010（executor 不干活）未修复** — 严重阻塞性问题仍存在
- 🟡 **BUG-009（mojibake）只在 API JSON 层** — 前端 ChatPanel 渲染中文是正常的
- 🆕 **BUG-011、012 是新发现**（见下文）

---

## Bug 状态对比表

| Bug | 上次状态 | 这次状态 | 备注 |
|-----|----------|----------|------|
| BUG-001 `getThemeColors` | 🔴 存在 | 🔴 **仍在** | 每次 navigate 后闪退 1-2s |
| BUG-002 暂停按钮 | 🟢 证伪 | 🟢 通过 | 正常 |
| BUG-003 残留警告 | 🟡 存在 | 🟡 **仍在** | 截图存证 |
| BUG-005 Polling 风暴 | 🟡 存在 | 🟡 **仍在** | network 抓包可证 |
| BUG-006 ERR_ABORTED | 🟡 存在 | 🟡 **未完全修** | 仍见 ERR_ABORTED |
| BUG-007 Goals 空白 | 🔴 存在 | 🔴 **仍在** | UI 实测空白 |
| BUG-008 tool 试错 | 🟡 存在 | 🟡 **仍在** | CEO 仍用 path + filePath 试错 |
| BUG-009 Mojibake | 🟠 API 层 | 🟠 **仅 API 层** | 前端 ChatPanel 显示正常 |
| **BUG-010** Executor 不干活 | 🚨 **致命** | 🚨 **仍未修复** | inbox 写入了 35s+ 仍 unread=1 |
| **BUG-011** 🆕 | - | 🟠 **新发现** | CEO 自动 charter 不等确认 |
| **BUG-012** 🆕 | - | 🟡 **新发现** | Engineer name `星野` 是中文好，但 A007 id 跟上次的 A008 一样 |

---

## 关键场景结果

### E. 新建项目 ✅
- API `POST /api/projects` → 200
- 项目 id `59adae4a-...`，CEO id `165ee0ec-...`
- 4 个子 agent 自动建好（HR/QA/Engineer A008）

### F-1. CEO 分析代码库 ⚠️
- ✅ 调 13 次 tool 读完项目（`list_files` / `read_file` / `read_charter` / `read_goals` / `save_charter` / `list_subordinates` / `send_message`）
- 🆕 **BUG-011**：CEO 这次**直接执行了 charter + 招人 + 派活**，没问"charter this?" 确认。
  - 上次 F-1 行为：问"Want me to charter this and dispatch?"
  - 这次 F-1 行为：直接 `save_charter` + `send_message(HR)` 派活
  - **影响**：行为变化，可能是 prompt 改了，也可能是 executor 沉默导致 CEO 不得不自己做完所有事
- 🔴 **BUG-008 仍在**：同一次响应里 `read_file` 用了 `path` 和 `filePath` 两种参数（试错 2 次后才用对）
- 🆕 **BUG-012**：HR 招的工程师 name 是 `星野`（中文，**正确**），role 是 `TypeScript 工程师`（中文，**正确**）
  - **但 API 返回仍是 mojibake** `æé` / `TypeScript å·¥ç¨å¸`
  - 结论：**API JSON 编码有问题，前端 DB 存储是正常的**

### F-2 / F-3. CEO charter + 招人 + 派活 ❌
- ✅ CEO 派任务到 A008 inbox：`unreadCount=1, read=False`
- ❌ **A008 35s+ 仍未消费消息**
- ❌ `work-logs: []`
- ❌ `chat history: 0`
- CEO 二次回复："未回报。`read_work_logs` 无记录，星野尚未提交结果"
- **结论：BUG-010 未修复**

### G. 团队沟通
- ✅ 3 条 comm：CEO→HR、HR→CEO、CEO→Engineer
- ✅ `expect_report=true` 正确
- ❌ 下游 executor 沉默

### H. 清理 ✅
- 项目列表：e2e-sandbox 已删，只剩 PoE2LI
- Sandbox workspace 目录已清空
- **PoE2LI 零污染**

---

## 新发现 Bug

### BUG-011 🟠 中高 — CEO 自动执行不需用户确认（行为变化）
- **现象**：上次 CEO 在 F-1 末尾问"Want me to charter this and dispatch?" 等待用户确认；这次直接 `save_charter` + `send_message` 派活
- **影响**：
  - 用户失去对 charter 内容的 review 机会
  - charter 内容是 LLM 一次生成，未经人工 review 即落地
  - 如果 charter 错了，已经招了人派了活，回滚成本高
- **建议**：保留"preview → confirm → execute"两步流程，加 `--dry-run` 选项

### BUG-012 🟡 中 — API JSON 编码层 mojibake（与 BUG-009 同源）
- **现象**：
  - DB 存储的 engineer name = `星野`（正确）
  - DB 存储的 role = `TypeScript 工程师`（正确）
  - **但 `GET /api/org` 返回的 JSON** 里 name = `æé`、role = `TypeScript å·¥ç¨å¸`
- **推测**：
  - 读取路径上有 `str.encode('latin-1').decode('utf-8')` 双重编/解码
  - **只发生在读取时**，写入时编码正确
- **位置**：`apps/hiveweave-py/src/hiveweave/services/org.py`（`list_org` 或 `get_agent_by_id`）
- **影响**：
  - 任何用 API 读 org 的工具/脚本都会拿到乱码
  - 但前端 ChatPanel 显示正常（说明 ChatPanel 走的是另一条 read 路径，或者前端二次解码）

---

## PoE2LI 数据完整性

- ✅ 项目元数据：未变
- ✅ PoE2LI workspace 文件：未触碰
- ✅ 任何 agent 都没读 / 写 PoE2LI workspace
- ✅ 测试前后 `GET /api/projects` 列表对比：只有 PoE2LI

---

## LLM Token 消耗

- 2 轮 chat 约 1.5-2k tokens
- 主要由 CEO 的 13 次 tool_call 序列消耗

---

## 下一步建议

1. **🚨 优先修 BUG-010** —— 不修，多 Agent 协作永远瘫痪
2. **🔴 修 BUG-007**（Goals 空白）—— UI 高频功能
3. **🟠 修 BUG-011** —— 给 CEO 加 `--dry-run` 或"charter preview 确认"开关
4. **🟡 修 BUG-012** —— `services/org.py` 读取路径的编码
5. **🟡 修 BUG-008** —— 工具 schema 强制参数名
6. **🟡 修 BUG-006 / BUG-005** —— 性能 + console 干净
