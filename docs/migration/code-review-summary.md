# Python 后端五轴代码审查汇总

审查日期：2026-07-05
审查范围：78 文件，16,042 行
三路并行审查：DB+服务层 / LLM+工具层 / Agent+API+Realtime 层

## 统计

| 严重程度 | 数量 | 说明 |
|---|---|---|
| Critical | 11 | 阻塞合并，必须修复 |
| Required | 35 | 必须修复 |
| Optional | 25 | 建议改进 |
| Nit | 15 | 小问题 |

## Critical 问题（11 项，按优先级排序）

### 数据丢失类（4 项）

**C1. charter.py 事务失败未 rollback**
- 文件：`services/charter.py:56-73`
- DELETE+INSERT 无 rollback，失败后孤立 DELETE 会被后续 commit 提交，旧章程被删新章程未插入
- 修复：except 块加 `await db.rollback()`

**C2. conversation/store.py turn_index TOCTOU 竞态**
- 文件：`conversation/store.py:322-342`
- fire-and-forget 的 _persist_turn 并发执行 SELECT MAX+INSERT，产生重复 turn_index
- 修复：`(agent_id, turn_index)` 加 UNIQUE 约束，或用单语句 `INSERT ... SELECT COALESCE(MAX(...),-1)+1`

**C3. conversation/store.py 压缩覆盖缓存丢失消息**
- 文件：`conversation/store.py:135-172`
- _do_compaction 耗时数秒，完成后覆盖整个缓存，压缩期间 append_turn 新增的消息全部丢失
- 修复：写入缓存时 merge 而非覆盖，或用 per-agent lock 序列化

**C4. game_time.py 告警失败仍标记 fired**
- 文件：`services/game_time.py:130-138,186-197`
- _fire_alarm 先 UPDATE DB 标记 fired=1 再发 inbox，inbox 失败则告警永久丢失
- 修复：先发 inbox 成功后再标记 fired，或失败时不标记

### 安全漏洞类（4 项）

**C5. bash.py 泄露全部环境变量**
- 文件：`tools/bash.py:90-94`
- `**os.environ` 把 OPENAI_API_KEY/OPENCODE_API_KEY 等所有密钥传给子进程，readonly agent 可 `env > leak.txt` 窃取
- 修复：白名单传递环境变量，只传 PATH 等必要变量

**C6. 敏感文件保护不统一（系统性问题）**
- 文件：`tools/patch.py`, `tools/grep.py`, `tools/review.py`
- file.py 有 SENSITIVE_PATTERNS 检查，但 patch/grep/review 全部没有。agent 可通过 patch 修改 .env、grep 搜索密钥行、review 将密钥发送给 LLM
- 修复：提取为共享 `security.py` 模块，所有文件操作工具统一调用

**C7. API 认证中间件可被绕过**
- 文件：`api/auth.py`
- /api/health 放行过宽，且 WebSocket 端点无认证检查
- 修复：WebSocket 端点加 api_key 检查，health 放行范围精确化

**C8. 路径参数注入**
- 文件：`api/org.py`, `api/chat.py`
- agent_id 含特殊字符（如 `../`）未校验，可能导致路径遍历
- 修复：路径参数加 UUID 格式校验

### 功能破坏类（3 项）

**C9. 熔断器 fallback 是死代码**
- 文件：`llm/streamer.py:398-409`
- cb_result.fallback 为真时只打日志不 return 也不切换 provider，继续用被熔断的 provider 发请求
- 修复：fallback 时切换到备用 provider 或抛出异常

**C10. 熔断器永远感知不到 HTTP 错误**
- 文件：`llm/streamer.py:427-445`
- RetryableError/PermanentError 被捕获为 error 字典返回，stream 方法无条件调用 report_success，熔断器几乎不会打开
- 修复：错误时调用 report_failure

**C11. agent.py finally 块竞态**
- 文件：`agents/agent.py`
- finally 块清理状态时未检查 _llm_task 是否仍为当前 task，可导致多 LLM task 并发操作同一 Agent
- 修复：finally 块检查 `if self._llm_task is current_task` 再清理

## Required 问题（35 项，按类别分组）

### 正确性（15 项）
- meta.py/project.py 懒初始化竞态导致连接泄漏
- 连接缓存无上限（内存泄漏风险）
- SSE 解析不处理 `\r\n\r\n` 分隔符
- Doom loop 检测器累加总数不跟踪连续性
- 上下文裁剪只检查相邻 2 条消息，多 tool_result 场景产生孤儿
- approval.py resolve_request 重启后静默失败
- 三服务每次操作查 Meta DB 解析 workspace（N+1）
- memory invalidate 过度失效
- _load_from_db 无 LIMIT
- _migrated 集合在 DB 重建后不重查
- schema 与运行时 ALTER 不一致
- permission_requests 无索引
- main.py shutdown 传 Agent 对象（应为 agent_id）
- supervisor.py 崩溃频率限制失效
- SSE/WebSocket 事件总线割裂

### 安全（5 项）
- WebSocket 无认证
- 路径参数注入（agent_id/project_id）
- CORS 配置过宽
- 项目创建 workspace_path 注入
- 工具输出临时文件无清理

### 架构（8 项）
- Agent 与 Supervisor 循环依赖（延迟导入是临时方案）
- API 路由双路径兼容增加维护负担
- EventBus 订阅者无上限
- 熔断器 per-provider 还是全局不明确
- 工具返回格式不一致（部分工具缺 error 字段）
- prompts 模块返回 str 而非 dict（与契约不一致）
- main.py lifespan 某步骤失败阻塞后续
- compaction.py SUMMARY_MARKER 格式与 Elixir 不一致

### 性能（7 项）
- Agent._build_messages 每次调用重复构建 identity prompt
- WebSocket 每连接双 task 内存开销
- API 端点缺分页
- grep 无结果上限
- 大文件读取不分块
- tool loop 25 轮上限可能不够
- EventBus 最近活动缓冲无上限

## 修复优先级建议

### P0 — 合并前必须修复（11 项 Critical）
1. C1 charter rollback
2. C2 turn_index UNIQUE 约束
3. C3 压缩缓存 merge
4. C4 告警失败不标记
5. C5 bash 环境变量白名单
6. C6 敏感文件保护统一
7. C9 熔断器 fallback
8. C10 熔断器错误上报
9. C11 agent finally 竞态
10. C7 WebSocket 认证
11. C8 路径参数校验

### P1 — 端到端测试前必须修复
- SSE 解析 `\r\n\r\n`
- 上下文裁剪孤儿 tool_result
- main.py shutdown 修复
- supervisor 崩溃频率限制

### P2 — 可延后
- 性能优化类
- 架构改进类
- Optional/Nit
