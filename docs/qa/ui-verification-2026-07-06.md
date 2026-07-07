# HiveWeave UI 验证报告（之前漏的）

**日期**：2026-07-06
**执行人**：TRAE（integrated_browser MCP + API）
**目的**：补上前几次**只跑 API 没跑前端**的失误
**对比基线**：[e2e-postdiff-2026-07-06.md](./e2e-postdiff-2026-07-06.md)

---

## 测试方法（真前端）

1. 隔离 sandbox workspace
2. **API 建项目 + 走 F 流程**（让 ChatPanel 有对话可看）
3. **TRAE integrated_browser 打开** `http://localhost:5173`
4. 真实点 UI + 截图 + console 抓取
5. 验证 BUG-001/003/005/006/007/008/011 修复情况

---

## UI 验证结果

### ✅ 已验证修复

| BUG | 修复状态 | 证据 |
|-----|---------|------|
| **BUG-009 / 012 / 013** (mojibake) | ⚠️ **前端层 OK** | ChatPanel 显示完整中文（"已向 HR 发出招聘请求：1 名工程师，技能绑定 incremental-implementation 和 test-driven-development"）—— **前端读路径正确**；但 API JSON 仍 mojibake |
| **BUG-010** (executor 不干活) | ✅ **真修了** | 完整流程跑通：CEO 招人 + 派活 + Engineer 干活 + 回报 + CEO 确认 |

### ❌ 仍存在 / 未修

| BUG | 现象 | console 证据 |
|-----|------|------------|
| **BUG-001** (getThemeColors) | 每次 navigate 闪退"无项目" 1-2s | `[error] [getThemeColors] TypeError: Cannot destructure property 'exportedColors' of 'undefined'` |
| **BUG-003** (chat 残留警告) | ⏭️ 这次没出现（CEO chat 已成功，无断流残留） | — |
| **BUG-005** (polling 风暴) | 5 个组件独立轮询 | 5 个 `Poll failed: TypeError: Failed to fetch` 错误 |
| **BUG-006** (api.ts AbortError) | **❌ 未修复** | 仍然有 7 个 `net::ERR_ABORTED` 进 console error stream |
| **BUG-007** (GoalsPanel 空白) | ❌ **仍未修** | `Failed to load goals: Error: HTTP 405: Method Not Allowed` |
| **BUG-008** (tool 试错) | ⏭️ CEO 这次只用 1 个 tool call | 无试错，但 sample size 太小 |
| **BUG-011** (CEO 行为) | ⏭️ CEO 这次只招人不自动 charter | 行为合理（恢复期望值），bash.py 修复未实测 |

### 🆕 新发现

| BUG | 严重度 | 现象 |
|-----|--------|------|
| **BUG-016** | 🟠 中高 | `GET /api/projects/{id}/goals` 返回 **405 Method Not Allowed** —— **后端没有 GET goals 端点**（只有 POST 写）。**这就是 BUG-007 始终修不好的根因**——前端调 GET 端点不存在 |
| **BUG-017** | 🟡 中 | `Failed to fetch agent: Error: HTTP 404: Not Found` —— 前端缓存了过时的 agent id（来自已删除项目） |

### 🟡 截图存证
- `c:\Users\99744\AppData\Local\Temp\trae\screenshots\ui-orgtree-empty.png` — OrgTree 节点未渲染
- `c:\Users\99744\AppData\Local\Temp\trae\screenshots\ui-goals-405.png` — Goals tab 显示"重试"按钮（但 405 重试无意义）
- `c:\Users\99744\AppData\Local\Temp\trae\screenshots\ui-orgtree-after-switch.png` — 切回 OrgTree 后区域空白

---

## 关键发现：BUG-007 修不好的真因

**根因不在前端**，**在后端 API 缺失**：

1. 前端 `GoalsPanel.tsx:40` 调 `getProjectGoals(projectId)` 
2. `api.ts:181` 调 `fetchJSON(GET /api/projects/{id}/goals)`
3. 后端**只有 POST** `/api/projects/{id}/goals`（在 `api/projects.py:652`）
4. **没有 GET handler** → 405 Method Not Allowed
5. 你给 GoalsPanel 加的"重试"按钮**永远重试失败**（端点根本不存在）

**修复方向**（任选其一）：
- (A) 后端加 `GET /api/projects/{id}/goals` handler（推荐，符合 REST 习惯）
- (B) 前端从 `useAppStore` 读 goals（建项目时已存 store）
- (C) 前端用 `useProject(projectId).goals`（如果 zustand 已经有）

---

## BUG-006 修复未生效分析

你的修复（来自 git diff）：
```ts
if (e?.name === "AbortError") return null as T;
```
但 console **仍然有 7 个 ERR_ABORTED** —— 推测：
1. 你改了源码但**没保存**（IDE HMR 没触发）
2. Vite HMR 对 `.ts` 文件改动应该**自动 reload**，但 `fetchJSON` 的修改可能没生效
3. **前端是 Vite dev server**（`pnpm dev`），HMR 一般正常 —— 建议**硬刷新**（Ctrl+Shift+R）后再测

---

## OrgTree 节点未渲染

观察：snapshot 里左侧有 OrgTree tab 按钮（e3），点击后**没有看到 CEO/HR/QA/Engineer 节点**（应该有 React Flow 树状图）。

可能原因：
- 切项目时 `getOrg()` API 失败但没 retry
- React Flow 容器在切 tab 后**没重新挂载**
- 你对 `App.tsx` 的 +42 行 / `store.ts` 的 +12 行**改了渲染逻辑**，但有副作用

建议看 `App.tsx:256` 附近的 `runDeleteQueue` / `handleDeleteProject` —— console 错误里看到 `runDeleteQueue` 在用旧的 projectId 调 DELETE。

---

## PoE2LI 零污染 ✅

- 项目已删
- sandbox 文件已清

---

## 总修复状态（4 次回归总览）

| BUG | 状态 |
|-----|------|
| BUG-001 getThemeColors | ❌ 仍在（前端） |
| BUG-003 chat 残留 | ⚠️ 这次没出现（未复现） |
| BUG-005 polling 风暴 | ❌ 仍在 |
| BUG-006 api.ts | ❌ 修复**未生效** |
| BUG-007 Goals 空白 | ❌ 根因是 BUG-016 |
| BUG-008 tool 试错 | ⚠️ 改善但 sample 小 |
| BUG-009/012/013 mojibake | 🟡 前端 OK / API 仍乱码 |
| BUG-010 executor | ✅ **真修了** |
| BUG-011 bash.py | 🟡 未实测 |
| BUG-015 inbox 写入 | ✅ 被 BUG-010 修复附带解决 |
| **BUG-016** 🆕 goals GET 405 | ❌ 新发现，**根因** |
| **BUG-017** 🆕 stale agent id 404 | ❌ 新发现 |

---

## 下一步建议

### 🚨 最高优先
1. **修 BUG-016**：后端加 `GET /api/projects/{id}/goals` handler（5 行代码）
2. **修 BUG-006**：硬刷新浏览器再看一次；如果还不行，检查 `e?.name === 'AbortError'` 的语法（可能 `e.name` 不存在或大小写错）

### 🟠 中优先
3. **修 BUG-001**：getThemeColors race（虽然你说 Office 不测，但 OrgTree 也受影响）
4. **修 BUG-005**：polling 风暴

### 🟢 低优先
5. UI 验证 BUG-008/011/003（sample 不够）
