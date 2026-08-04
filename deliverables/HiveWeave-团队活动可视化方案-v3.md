# HiveWeave 团队活动可视化方案（v3 · 体验/性能优化版）

> 基线：v2（子代理审计修订版，8 处事实修正已全部保留，本文不重复抄写，章节号沿用 v2）。
> v3 只回答两个问题：**前端怎么更便捷、更好看**（新增第四·A 章）+ **后端还有什么优化空间**（新增第三·B 章），并同步修订分期（五）、风险表（六）、文件落点（七）。
> v3 新增事实均已对代码核实：`realtime/event_bus.py` 已有完整 pub/sub（lobby/agent/project 三频道、非阻塞投递、慢消费者丢最旧）；前端 store 已有 `activityFeed` WS 管线（addActivity + RAF 节流）；`get_project_db_by_project_id` 返回的是**全局共享单 aiosqlite 连接**；前端已有 Tailwind 3.4 可直接用。

---

## 三·B、后端优化空间（v3 新增）

### B1. WS 失效信号替代 5s 轮询 —— 方向性挑战 v2 的「轮询足够」

v2 结论「不做 WebSocket 任务事件订阅（轮询足够）」在 v2 的事实基础上是对的（当时认为要新建订阅通道），但 v3 核实后发现**推送基础设施已全部现成**，轮询论不再成立：

- `event_bus.publish(channel, event, agent_id=?)` 现成，project 频道已存在；
- `TaskService._transition` 是唯一状态流转收口，加一行 `bus.publish(f"project:{pid}", {"kind":"task_event","task_id","to_status","ts"})` 即可全覆盖（含未来的盲区补写）；
- 前端 WS → `addActivity` → store 的管线现成，timeline 视图只需监听 `kind==='task_event'` 作**失效信号**，防抖 1s 重拉 activity 端点；正打开的 TaskTimelinePanel 若 task_id 匹配则重拉事件流。

**关键权衡（写死）：WS 事件只作失效信号，不作数据源。** 数据源永远是 REST 拉取 —— 这样丢事件、乱序、WS 断连全都无害（最坏退化为 v2 的轮询体验），轮询降级为 30s 兜底对时。
成本：后端 <30 行 + 前端 store 一个订阅分支。收益：延迟 5s → <1s，共享连接查询压力降一个数量级。

### B2. 事件盲区补写提前 —— 方向性挑战 v2 的「P3 再补」

v2 把 4 处盲区补写放 P3，理由是控制 P1 范围。反对意见：**P1 交付的是一个「第一天就带着 4 个已知谎言」的时间轴**，而末段校准兜底只能保证不张冠李戴、不能保证不瞬移 —— 用户第一次用就会看到瞬移并失去信任，这恰是本方案要解决的问题本身。

每处补写只是复用 `_transition` / `emit_task_event`，量级 <10 行/处。折中方案：
- **并入 P1**：`org.py` dismiss 批量改派（影响面最大，一次 dismiss 可批量 cancel 数十任务）、`obligation.py` 依赖唤醒（blocked→running 是泳道上最高频的瞬移源）；
- **留 P3**：`close.py` 回置 approved（低频）、`crud.py` REST PATCH 直接换 assignee（管理操作，可用「REST PATCH 强制走 reassign_task 路径」的方式消灭，连事件都免费得到）。

### B3. 共享单连接的读放大与快照一致性（v2 完全没覆盖）

`get_project_db_by_project_id` 返回全局共享单 aiosqlite 连接，**所有语句串行**。v2 只说「查询必须轻」，漏了两个更实质的问题：

1. **尾延迟传染**：5s 轮询的 4 表归并查询与 agent 高频写（work_logs/inbox/心跳）排同一条串行队列，timeline 打开期间任务写入尾延迟上升。对策：timeline 两个端点改用**独立只读连接**（WAL 模式天然支持并发读，`aiosqlite.connect(f"file:{path}?mode=ro", uri=True)`，连接池 1~2 条即可），与写路径物理隔离。
2. **撕裂快照**：四路查询若分四次 execute，写入进行中会拿到不一致状态（inbox 里催办已现、task_events 里对应段未到，时间轴出现「先有评论后有状态」的倒挂）。对策：单任务事件流的四路查询包在**同一事务**里（只读连接上 `BEGIN … COMMIT` 包住四条 SELECT），代价为零。

### B4. 游标分页与变更探测（防膨胀）

- activity 端点加 `cursor_ts` 游标参数替代纯 since/until 硬窗，前端「加载更早」向后翻页；
- 响应带 `max_event_ts`；轮询/重拉带 `if_changed_since=<max_event_ts>`，无变化返回 `{changed:false}`（等价 304），轮询成本 O(1)；
- 返回超 limit 时显式带 `truncated: true`，前端提示用户缩小时间窗，不静默截断（呼应 AGENTS.md「截断触发=上游已漏」）。

### B5. 明确不做的（防镀金）

- 不为 json_extract 兜底建表达式索引/生成列 —— 兜底查询占比小，schema 复杂化不值；
- 不做服务端事件聚合缓存表 —— 等真实性能 profile 出现再说；
- 不把 handoffs 改成历史表 —— v2 已裁定，维持。

---

## 四·A、前端体验与视觉升级（v3 新增）

### A1. 方向性裁决：自绘 vs 现成库（先给反对意见）

「更便捷、更好看」最直白的答案是上现成甘特库（vis-timeline / Frappe Gantt / echarts）。**v3 的裁决是维持自绘，但把理由和止损线摆上台面：**

- 反对自绘的最强理由：pan/zoom/刷选/tooltip/mini-map 自己做，400 行预算大概率失守，且手感（惯性、边界吸附）永远拼不过成熟库；
- 反对上库的最强理由：Electron 打包体积 + 主题体系（项目自己的 CSS 变量/Tailwind 配色）与库自带样式打架，定制成本经常高于自绘；且本场景交互面**确实窄** —— 无拖拽改期、无依赖连线、无行编辑，只有 pan/zoom/click/hover/brush；
- **止损线（写进验收）**：P2 实现时若 TeamTimeline 系文件合计超 800 行仍手感粗糙，触发降级方案 —— 砍平滑缩放，改 CSS Grid 静态泳道 + 浏览器原生滚动 + 离散时间档位（1h/6h/1Day），保信息密度放弃炫技。

### A2. 视觉升级清单（Tailwind 现成，零新依赖）

| 项 | 做法 |
|---|---|
| 语义色 token | `utils.ts` 集中一张表，走 Tailwind 色板：running=emerald-500、waiting=amber-400、blocked=orange-500、reviewing=violet-500、approved/done=sky-500、**cancelled=slate-400+斜纹**、待认领空段=灰底虚线边框。**禁止散落 hex**；与现有面板共用 CSS 变量体系 |
| 布局骨架 | 左列 agent 栏 `sticky left-0`（头像色点+名字+角色徽章+实时健康点，直接复用 store 的 agentHealth），顶部时间轴 `sticky top-0`，交叉处留白角标 |
| 当前时刻 | 红色 1px 竖线 + 顶端脉冲圆点（CSS animation，不写 JS 动画） |
| 游戏时间 | Day 边界竖带（bg-slate-100 交替）+ 顶部副刻度「Day N」 |
| 任务块 | 高 22px、圆角、块内空间够时直接渲染「等评审 3h12m」相对时长；hover 浮 tooltip 卡片（标题/状态/起止/负责人/评审人） |
| 空段纹理 | 待认领段用 `repeating-linear-gradient` 斜纹，与「无任务空白」明确区分 |
| Mini-map | 底部 40px 总览条（全窗口密度图）+ 可拖视口框；这是「好看」里性价比最高的一项 |
| 动效 | 块出现 fade-in 150ms、视口变化 transform transition，全部 CSS，不上 JS rAF 动画 |
| 三态 | 加载骨架屏 / 空态（「该窗口无任务活动」+ 快捷扩窗按钮）/ 错误态（带重试），照抄现有面板先例 |

### A3. 便捷性清单（每条都对应一个真实使用场景）

1. **深链 URL**：`view/taskId/since/until` 同步进 URL hash —— 调试时把链接贴给同事或贴给 AI 助手，对方打开即同视角（TEST18 式复盘的核心痛点）；
2. **一键复制任务链路为 Markdown**：TaskTimelinePanel 顶部按钮，把事件流渲染成紧凑 Markdown 表格 —— 直接贴进对话框让 AI 分析「这任务卡哪了」，这是本功能最高频的下游动作；
3. **键盘**：`j/k` 上下切任务、`←/→` 平移、`+/-` 缩放、`0` 回到现在、`Esc` 清选中；
4. **时间刷选 brush** + 预设档位（最近 1h / 最近 1 游戏日 / 全部）；
5. **筛选条**：agent 多选 chip / 状态多选 / 标题关键字，全部客户端过滤（数据已全量在前端，零后端成本）；
6. **「跳到现在」浮动按钮**：视口不含当前时刻时出现；
7. **异常视觉锚点**：stall 催办 nudge、review_rework 打回节点加红色左边框 + 图标，一眼定位「哪里卡过」；
8. **事件按游戏日分组**：TaskTimelinePanel 事件流按 Day N 分组折叠，长任务不刷屏。

### A4. 组件预算修订（v2 表基础上）

新增三个小文件守住单文件纪律：`MiniMap.tsx`（~80）、`TimeAxis.tsx`（~80）、`TimelineTooltip.tsx`（~50）。`TeamTimeline.tsx` 预算 400 不变（只留容器+联动），`usePanZoom.ts` 120 不变，系列合计 ~900 行但单文件全部 ≤400。

---

## 五、分期交付（v3 修订）

### P1：任务全链路回放 + 数据可信度
v2 原范围 **+ B2 的两处盲区补写（org.py / obligation.py）+ B3 的只读连接与事务一致性 + B1 的 WS 失效信号（后端部分）**。
验收追加：dismiss 一批 agent 后时间轴无瞬移；crud.py REST PATCH 换 assignee 已强制走 reassign 路径。

### P2：团队泳道总览（好看版）
v2 原范围 **+ A2 视觉清单 + A3 便捷清单 1/2/4/5/6 + MiniMap/TimeAxis/Tooltip 三组件 + B1 前端订阅分支**。
验收追加：A1 止损线检查（合计行数与手感）；深链可分享；30s 兜底轮询 + WS 失效双通道实测。

### P3：打磨与扩展
v2 原 P3 剩余项（close.py/crud.py 盲区收尾、沉默历史持久化、chat_messages.task_id 评估）**+ B4 游标分页（按实测数据量决定是否提前）+ A3 剩余项（3/7/8）**。

---

## 六、风险与对策（v3 增量行）

| 风险 | 对策 |
|------|------|
| WS 事件丢失/乱序导致视图不更新 | WS 只作失效信号不作数据源（B1 写死）；30s 兜底轮询对时 |
| 只读连接路径漂移（db 文件被删/驱逐） | 复用 ensure_project_db 的路径解析 + evict 检查；只读连接打开失败降级回共享连接 |
| 自绘甘特行数/手感失控 | A1 止损线：超 800 行触发降级方案（静态泳道+离散档位） |
| 盲区补写引入新事件形态 | 补写只复用 `_transition`/`emit_task_event` 既有入口，不发明新 event_type；回归测试跟在每个补写点后 |
| Mini-map/brush 交互复杂度蔓延 | 列入 P2 验收行数检查；超预算先砍 brush 保留预设档位 |

---

## 七、文件落点汇总（v3 增量）

**后端新增/修改（增量）**
- `services/tasks/timeline.py` — 四路查询同事务化 + 只读连接获取（B3）
- `db/project.py` — 新增 `get_project_db_readonly(project_id)`（B3，~20 行）
- `services/org.py` / `services/obligation.py` — 盲区补写各 <10 行（B2）
- `services/tasks/crud.py` — REST PATCH 换 assignee 强制走 reassign 路径（B2）
- `services/tasks/events.py` 或 `_transition` 收口处 — WS publish 一行（B1）

**前端新增（增量）**
- `components/timeline/{MiniMap,TimeAxis,TimelineTooltip}.tsx`（A4）
- `components/timeline/useDeepLink.ts` — URL hash 同步（A3.1，~60 行）
- `store.ts` — task_event WS 订阅分支（B1 前端，~20 行）

其余落点同 v2，不重复。

---

## 八、v2 → v3 修订清单（留档）

1. 推翻「轮询足够」：基于 event_bus 现成事实，改 WS 失效信号 + 30s 兜底（B1）；
2. 盲区补写从 P3 提前两处到 P1，crud.py 改走 reassign 路径（B2）；
3. 新增共享单连接尾延迟与撕裂快照分析，引入只读连接 + 同事务四路查询（B3）；
4. 新增游标分页 / if_changed_since / truncated 标志（B4）；
5. 前端新增视觉清单（语义色/sticky/mini-map/纹理/三态）与便捷清单（深链/复制 Markdown/键盘/brush/筛选/跳现在）（A2/A3）；
6. 自绘 vs 库的争论写成带止损线的明确裁决（A1）；
7. 组件预算 +3 文件，单文件纪律不变（A4）。
