# HiveWeave E2E 第三次回归测试报告

**日期**：2026-07-06（第二次修复后）
**执行人**：TRAE（integrated_browser MCP + PowerShell API）
**对比基线**：[e2e-regression-2026-07-06.md](./e2e-regression-2026-07-06.md)
**测试范围**：A-H 同流程（Office 模块跳过）
**后端版本**：`0.2.0`（**与上次完全一致**，可能没 bump version）

---

## 结论速览

| 状态 | 数量 |
|------|------|
| ✅ 已修复 | 2（BUG-012、BUG-011） |
| 🟡 部分改善 | 3（BUG-007、BUG-008、BUG-006） |
| ❌ 仍存在 | 3（BUG-010、BUG-001、BUG-009 残留） |
| 🆕 新发现 | 1（BUG-014） |

**关键结论**：
- ✅ **BUG-012（org API 编码）已修** —— 但**只修一半**，communications API 仍 mojibake → **BUG-013**
- 🟡 **BUG-011 回归** —— CEO 这次不自动 charter（**回到第一次 E2E 的行为**）
- 🟡 **BUG-008 改善** —— CEO 这次只用了 1 个 tool call（上次 13 个），无试错
- 🚨 **BUG-010 仍未修复** —— T+60s A007 收件箱仍 `unreadCount=1, read=False`

---

## Bug 状态对比（三次回归总览）

| Bug | 第一次 E2E | 第二次回归 | **第三次回归** | 状态 |
|-----|-----------|-----------|---------------|------|
| BUG-001 getThemeColors | 🔴 存在 | 🔴 存在 | 🔴 存在 | ❌ |
| BUG-002 暂停按钮 | 🟢 证伪 | 🟢 通过 | 🟢 通过 | — |
| BUG-003 chat 残留 | 🟡 存在 | 🟡 存在 | 🟡 存在 | ❌ |
| BUG-005 polling 风暴 | 🟡 存在 | 🟡 存在 | 🟡 存在 | ❌ |
| BUG-006 ERR_ABORTED | 🟡 存在 | 🟡 存在 | 🟡 存在 | ❌ |
| BUG-007 Goals 空白 | 🔴 存在 | 🔴 存在 | 🟡 显示"重试"按钮 | 🟡 改善 |
| BUG-008 tool 试错 | 🟡 7次 | 🟡 13次 | 🟢 1次无试错 | ✅ 改善 |
| BUG-009 Mojibake（DB层） | 🟠 全局 | 🟡 仅API | 🟡 仅API | 🟡 |
| BUG-010 Executor 不干活 | 🚨 致命 | 🚨 致命 | 🚨 致命 | ❌ **未动** |
| BUG-011 CEO 自动 charter | — | 🟠 自动 | 🟢 不自动 | ✅ 改善 |
| BUG-012 org API mojibake | — | 🟡 存在 | 🟢 已修 | ✅ |
| **BUG-013** 🆕 comms API mojibake | — | — | 🟡 存在 | 🆕 |
| **BUG-014** 🆕 Goals "重试"按钮 | — | — | 🟡 新增 | 🆕 |

---

## 关键场景结果

### E. 新建项目 ✅
- API `POST /api/projects` → 200
- 项目 id `067b5727-...`，CEO id `b5feaeb2-...`
- 自动建好 CEO/HR/QA（3 个 agent）

### BUG-012 验证（org API 编码）✅
- 修复前：`name: "æé", "TypeScript å·¥ç¨å¸"`
- **修复后**：`name: "CEO", "HR", "QA"`, `role: "ceo", "hr", "qa"`（全部正常）
- ✅ 树结构 API 已修复

### F-1. CEO 分析代码库 ✅
- CEO 只用了 **1 次 tool call**（`list_files`）
- **提议"no additional engineers needed"** —— 提议用现有 solo 结构（CEO/HR/QA）干活
- **没自动 charter**（恢复第一次 E2E 行为）
- ✅ 主动问"Want me to charter and hire anyway, or stop here?"

### F-2. CEO 招人 + 派活 ⚠️
- 用户强制"招 1 个工程师" → CEO 派任务给 HR
- HR 招出 A007（`de3a50c0-...`），**前端显示 name = 沐风，role = TypeScript 工程师（正常中文）**
- CEO 派活给 A007，`expect_report=1`
- ✅ 通信 3 条（CEO→HR / HR→CEO / CEO→A007）
- ❌ **但 API 返回仍是 mojibake**：`name: æ²é£, role: TypeScriptå·¥ç¨å¸` → **BUG-013**

### F-3. 验证 BUG-010（executor 干活）❌
- T+0：A007 收件箱 `unreadCount=1, read=False`
- T+30s：仍 `unreadCount=1, read=False`, work-logs=`[]`, chat=0
- T+60s：仍 `unreadCount=1, read=False`
- **结论：BUG-010 完全未修复**

### G. 团队沟通 ⚠️
- 3 条 comms（结构正确）
- ❌ **内容 mojibake**（communications API 读取路径未修）→ BUG-013
- ✅ expect_report=1 正确

### UI 验证（GoalsPanel）🟡
- 切到 Goals tab：显示"重试"按钮（**新增**）
- 仍**没显示 GoalsPanel 内容**（仅按钮可见）
- 部分改善：现在有重试机制，但核心功能仍不可用

### H. 清理 ✅
- 项目列表：e2e-sandbox 已删，只剩 PoE2LI
- Sandbox workspace 目录已空
- **PoE2LI 零污染**

---

## 新发现 Bug

### BUG-013 🟠 中高 — Communications API 仍 mojibake（BUG-012 残留）
- **现象**：`GET /api/communications` 返回的 `message` 字段仍是 mojibake
  - 真实内容："Hired TypeScript engineer under you: 沐风 (A007)"
  - API 返回：`Hired TypeScript engineer under you: æ²é£ (A007), role=TypeScriptå·¥ç¨å¸`
- **与 BUG-012 对比**：
  - BUG-012（org 树）：**已修**
  - BUG-013（communications）：**未修**
- **推测**：`services/comm.py` 或 `communications` 表的读取路径上有独立的编码 bug
- **位置**：`apps/hiveweave-py/src/hiveweave/services/comm.py` 或 `db/`（推测）
- **影响**：
  - 前端 ChatPanel 显示正常（**走另一条读路径**）
  - 但任何用 `/api/communications` 的脚本/工具/UI 都会拿到乱码
  - 影响**所有依赖 comms 数据的 UI**（如通信历史、Agent 上下文）

### BUG-014 🟡 中 — GoalsPanel 显示"重试"按钮但内容仍不渲染
- **现象**：
  - 第一次 E2E：Goals tab 完全空白
  - 第二次回归：Goals tab 完全空白
  - 第三次回归：Goals tab 显示一个"重试"按钮（**新增**），但仍**没有 GoalsPanel 内容**
- **推测**：GoalsPanel 数据加载失败 → 显示重试按钮 → 但点重试仍无内容（推测无 Goals 数据或 load 逻辑空）
- **位置**：`apps/web/src/components/GoalsPanel.tsx`
- **改善**：现在有错误恢复机制（重试按钮），但核心问题未解决

---

## 总体趋势

### ✅ 改善
- BUG-008（tool 试错）：7 → 13 → **1**（LLM 工具调用效率大幅提升）
- BUG-011（CEO 自动 charter）：自动 → 不自动（恢复合理默认）
- BUG-012（org API 编码）：mojibake → 正常
- BUG-007（Goals 空白）：空白 → 显示重试按钮（有错误恢复机制）

### ❌ 未动
- **BUG-010（executor 不干活）** —— 致命 bug，**3 次回归 0 修复**
- BUG-001（getThemeColors race）—— 前端 UI 问题（用户说不测 Office 排除，但其他 UI 仍受影响）
- BUG-009（mojibake）—— 部分改善，但 communications 仍乱码

---

## PoE2LI 数据完整性

- ✅ PoE2LI 项目元数据：未变
- ✅ PoE2LI workspace 文件：未触碰
- ✅ 任何 agent 都没读 / 写 PoE2LI workspace
- ✅ 测试前后 `GET /api/projects` 列表对比：只剩 PoE2LI

---

## LLM Token 消耗

- 2 轮 chat 约 0.5-1k tokens
- CEO 这次只用了 1 次 tool call（**比上次减少 92%**），主要靠 prompt 改进

---

## 下一步建议

1. **🚨 优先修 BUG-010** —— 3 次回归都未动，**不修等于产品不可用**
2. **🟠 修 BUG-013** —— BUG-012 同源 bug，应该一起修
3. **🟠 修 BUG-007** —— 核心 UI 功能，加载逻辑排查
4. **🟡 修 BUG-001**（如果你打算继续用 Office）—— 否则不动
5. **🟡 修 BUG-014** —— 重试按钮背后加 error log，方便诊断
6. **🟡 修 BUG-006 / BUG-005** —— 性能 + console 干净

---

## 测试效率趋势

| 指标 | 第一次 E2E | 第二次回归 | 第三次回归 |
|------|-----------|-----------|-----------|
| CEO tool calls | 7+ | 13 | **1** |
| LLM tokens | 2-3k | 1.5-2k | **0.5-1k** |
| 总测试时间 | ~15min | ~10min | **~8min** |
| 发现新 bug | 7 | 2 | 1 |

**趋势向好**，但 BUG-010 是块硬骨头。
