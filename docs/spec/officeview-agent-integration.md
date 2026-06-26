# OfficeView Agent 集成方案

> 状态: Draft v1 | 作者: 榫卯 | 优先级: P1 (Iter 1.1)

## 1. 目标

将 OfficeView 从单 Agent 测试动画升级为**实时反映组织状态**的俯视办公室视图：
- 根据 `GET /api/org` 返回的 Agent 列表，动态分配工位（Workstation）
- 通过 SSE 实时驱动 Agent 动画状态（空闲→行走→工位→打字）
- click Agent Sprite → 与 OrgTree 共享 `selectedAgentId`，复用右侧面板

## 2. 数据流架构

```
┌─────────────────────────────────────────────────────────────┐
│                         App.tsx                              │
│  ┌─────────────────┐    ┌──────────────────────────────────┐│
│  │  SSE subscribe   │    │  store (Zustand)                 ││
│  │  (已有)           │───▶│  ┌ selectedAgentId ──────────┐  ││
│  │                  │    │  ├ processingAgents: string[]  │  ││
│  │  subscribeAgent  │    │  ├ activityFeed: ActivityEntry │  ││
│  │  Status()        │    │  └─────────────────────────────┘  ││
│  └─────────────────┘    └──────────┬───────────────────────┘ │
│                                    │                         │
│  ┌─────────────────┐               ▼                         │
│  │  activeView      │    ┌──────────────────┐               │
│  │  = "tree"|"office"│    │  OrgTree.tsx /    │               │
│  │  (已有, store)   │    │  OfficeView.tsx   │               │
│  └─────────────────┘    └──────────────────┘               │
│                                    │                        │
│  ┌─────────────────┐               ▼                        │
│  │  Right Panel     │    ┌──────────────────────┐          │
│  │  Chat/Agent/Logs │◀───│ selectedAgentId 驱动   │          │
│  └─────────────────┘    └──────────────────────┘          │
└─────────────────────────────────────────────────────────────┘
```

### 关键设计决策

| 决策 | 选项 | 结论 |
|------|------|------|
| 数据获取 | OfficeView 内部 `useEffect` 调用 `getOrgTree` | 不与 OrgTree 共享状态，各自独立 fetch（简单解耦） |
| 选中联动 | OfficeView click → `setSelectedAgent(id)` | 与 OrgTree 完全共享 `selectedAgentId` |
| 视图切换 | click → `setActiveView("tree")` | 复用现有 right panel，无需 OfficeView 内嵌面板 |
| 动画驱动 | PIXI ticker 从 store 读取 `processingAgents` | 不额外建 SSE 通道，复用已有状态 |

## 3. 接口定义

### 3.1 fetchAgents: 从 org route 取 Agent 列表

```typescript
// api.ts 已有
export async function getOrgTree(projectId?: string): Promise<OrgNode>

interface OrgNode {
  id: string;
  name: string;
  role: string;
  position: string;
  status: string;       // "active" | "paused" | "error"
  children?: OrgNode[];
}
```

OfficeView 只需 **flatten** 后的 Agent 列表：

```typescript
interface OfficeAgent {
  id: string;
  name: string;
  role: string;
  status: string;
}
```

### 3.2 从 store 读取的实时状态

```typescript
// store.ts 已有
processingAgents: string[];   // 当前正在处理的 agent id
setSelectedAgent: (id: string | null) => void;
selectedAgentId: string | null;
```

### 3.3 动画状态推导

```
Agent 的视觉状态 = f(processingAgents, 当前位置)
  - 在工位 + processing === true  → typing（打字动画）
  - 在工位 + processing === false → sitting（静坐）
  - 不在工位 + processing === true → walking（走向工位）
  - 不在工位 + processing === false → idle（在行走路径上暂停或处于等待状态）
```

## 4. 动态工位布局算法

### 4.1 配置式网格

```typescript
const OFFICE_CONFIG = {
  canvasWidth: 700,      // 与现有一致
  canvasHeight: 420,     // 与现有一致
  gridUnit: 40,          // 对齐单位
  wallThickness: 30,
  // 预定义工位插槽 (slot)，按网格对齐
  workstationSlots: [
    { id: "slot_1", gridX: 4, gridY: 2 },   // 像素位置 (160, 80)
    { id: "slot_2", gridX: 8, gridY: 2 },   // (320, 80)
    { id: "slot_3", gridX: 12, gridY: 2 },  // (480, 80)
    { id: "slot_4", gridX: 4, gridY: 7 },   // (160, 280)
    { id: "slot_5", gridX: 8, gridY: 7 },   // (320, 280)
  ] as const,
};
```

> **为什么用 grid 坐标？** 未来可读配置或从 API 下发 slot 定义，grid 坐标比像素坐标更语义化。

### 4.2 Agent → Slot 分配策略

策略: **按角色分层分配**，同一层级的 agent 优先分到同一行。

```
排序规则:
1. 按层级 depth 分组 (CEO depth=0, coordinator depth=1, executor depth=2)
2. 同级内按名称字母序
3. 依次填充 slot_1..slot_N
4. 如果 Agent 数 > slot 数 → 在末尾显示 "+N more" 溢出提示
```

```typescript
function assignWorkstations(agents: OfficeAgent[], slots: Slot[]): Map<string, Slot> {
  const sorted = agents.sort((a, b) => {
    // 按 depth 升序（CEO 先排），同 depth 按 name
    return depthOf(a) - depthOf(b) || a.name.localeCompare(b.name);
  });
  const assignment = new Map<string, Slot>();
  sorted.forEach((agent, i) => {
    if (i < slots.length) {
      assignment.set(agent.id, slots[i]);
    }
    // 溢出: agent 不入 map，显示溢出计数
  });
  return assignment;
}
```

### 4.3 溢出提示

当 `agents.length > workstationSlots.length` 时，在场景右下角（或右上角）显示:

```
+3 more agents beyond office capacity
```

这是一个纯文本 PIXI.Text 对象，点击不动。超出容量的 agent 不显示 sprite。

### 4.4 家具渲染不变

现有家具（书架、饮水机、植物、会议桌）保留固定位置不变。新增 Agent 后家具**可能被遮挡**，通过 z-order 控制:
- 地板 → 家具 → Agent Sprites → 溢出提示

## 5. Agent 动画系统改造

### 5.1 从单 AgentSprite 到多 AgentSprite Map

```typescript
class OfficeScene {
  agents: Map<string, AgentSprite> = new Map();  // agentId → sprite
  ticker: (delta: number) => void;

  syncAgents(agentList: OfficeAgent[], processingSet: Set<string>) {
    // add/remove/spawn/recycle sprites
  }
}

class AgentSprite {
  agentId: string;
  // ... 现有 animation state machine
  bind(agent: OfficeAgent) {
    this.agentId = agent.id;
    this.nameLabel.text = agent.name;
    // 根据 role 调整 tint 或 frame
  }
}
```

### 5.2 动画状态机（更新版）

```
         ┌─────────────────────────────────────┐
         │          IDLE (待命)                  │
         │ 不在工位, 不处理                       │
         └─────────┬───────────────────────────┘
                   │ processing === true
                   ▼
         ┌─────────────────────────────────────┐
         │          WALKING (走向工位)            │
         │  沿直线以 WALK_SPEED 像素/tick 移动    │
         └─────────┬───────────────────────────┘
                   │ dist < WALK_SPEED (到达)
                   ▼
         ┌─────────────────────────────────────┐
         │          SITTING (入座)               │
         │  站在 chair 位置 + 坐下动画            │
         └─────────┬───────────────────────────┘
                   │ processing === true
                   ▼
         ┌─────────────────────────────────────┐
         │          TYPING (工作)                │
         │  播放 walk frames 作为打字动画         │
         └─────────┬───────────────────────────┘
                   │ processing === false
                   ▼
         ┌─────────────────────────────────────┐
         │   STAND UP → IDLE (原地待命)          │
         │  保持工位位置, 进入 idle 帧            │
         └─────────────────────────────────────┘
```

> **简化**: 当前不实现 Agent 之间的路径避让。如果两个 Agent 走到同一 slot，后者等待前者完成。单文件/单 slot 场景下碰撞概率接近零。

### 5.3 Name Label

每个 AgentSprite 头顶显示 agent name（PIXI.Text），字体 white 11px，带细阴影确保可读。

```
  ┌──────┐
  │ 像素  │  ← PIXI.Text
  │ ◌ 行走 │  ← Sprite
  └──────┘
```

## 6. 交互设计

### 6.1 Click → Select Agent

```typescript
// OfficeScene.ts
onSpriteClick(agentId: string) {
  useAppStore.getState().setSelectedAgent(agentId);
  useAppStore.getState().setActiveView("tree");  // 切到 tree 视图
}
```

- 点击 agent sprite → `setSelectedAgent(agentId)` → 右侧面板显示 Chat/Agent/Logs
- 同时 `setActiveView("tree")` → 用户看到 OrgTree 中该 agent 高亮（已有逻辑）
- **不**在 OfficeView 内嵌右侧面板，复用已有布局

### 6.2 高亮状态

- 当 `selectedAgentId === agent.id` 时，该 agent sprite 增加 **glow 边框**（黄色外发光 2px）
- 用 PIXI.Filter 或简单的附加 Graphics 实现

### 6.3 Hover 提示

- mouseenter sprite → 显示 agent 的 role/position 信息（PIXI.Text tooltip）
- mouseleave → 隐藏

## 7. 开发阶段拆分

### Phase 1: Data Binding (无动画, 纯映射)

**改动范围**: OfficeView.tsx，约 +150 行

1. 新增 `fetchAgents()` 内部逻辑 → flatten `getOrgTree` 结果
2. 新增 `assignWorkstations()` 函数 → Agent → Slot 映射
3. 维护 `Map<string, AgentSprite>`，根据 agent 列表增删 sprite
4. 每个 sprite 显示 name + role tint（placeholder 方块）
5. 点击 sprite → `setSelectedAgent(id)` + `setActiveView("tree")`
6. 溢出提示 "+N more"

**验收标准**:
- [ ] 切换到 Office 视图后 3s 内渲染所有 Agent
- [ ] 每个 Agent 头顶显示名称
- [ ] 点击 Agent → 右侧面板打开 Chat/Agent/Logs
- [ ] Agent 超过 5 个时末尾显示溢出计数
- [ ] 不破坏现有 "Run Test" 按钮（可以保留或删除）

### Phase 2: Real-time Animation

**改动范围**: OfficeView.tsx + AgentSprite class，约 +200 行

1. 从 store 读取 `processingAgents`，驱动 AgentSprite 动画状态
2. idle → walking → sitting → typing 状态转换
3. SSE 收到 `processing=true` → Agent 走向工位 → 坐下 → 打字
4. SSE 收到 `processing=false` → 停止打字 → 原地 idle
5. 选中高亮（glow border）
6. Hover tooltip

**验收标准**:
- [ ] 处理中的 Agent 走到工位坐下打字
- [ ] 停止处理后保持工位位置，停止动画
- [ ] 选中 Agent 有视觉高亮
- [ ] 悬停显示 role/position

### Phase 3: Polish (可选, P2)

1. 多走帧 sprite sheet 切换不同方向
2. 随机空闲动画（伸懒腰、看手机）
3. 场景过渡动画（切换项目时淡入淡出）
4. 按 role 区分的 avatar 色彩/图标

## 8. 不变项（不在此迭代实现）

| 功能 | 原因 |
|------|------|
| 路径避让算法 | MVP 不需要，碰撞概率低 |
| 缩放 / 平移 / 拖拽 | 700×420 固定 canvas |
| 分页模式（多页 Office） | 溢出用文字提示即可 |
| OfficeView 内嵌对话 | 复用右侧 panel |
| Agent 之间通信可视化 | 属于 Iter 1.3 功能范围 |

## 9. 风险与缓解

| 风险 | 概率 | 缓解 |
|------|------|------|
| PixiJS 初始化延迟导致闪白 | 中 | App mount 时预创建 PIXI.Application |
| 大量 Agent（>20）性能 | 低 | PixiJS 处理 50 个 sprite 无压力，溢出不渲染 |
| SSE 瞬时高频更新导致动画闪烁 | 低 | processing 状态变化时只触发一次状态转换，不做过渡打断 |
| getOrgTree 返回结构变化 | 低 | 只依赖 `id`/`name`/`role` 字段，已有 schema |

## 10. 工作量估算

| Phase | 估算（人·时） | 依赖 |
|-------|--------------|------|
| Phase 1: Data Binding | 4h | 无，纯前端 |
| Phase 2: Real-time Anim | 6h | 无，复用 Phase 1 |
| Phase 3: Polish | 4h (P2) | 无需安排 |
| **总计** | **10h (MVP)** | 像素独立完成 |

---

*文档版本: v1 | 最后更新: 项目第 2 天*