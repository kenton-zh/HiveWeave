# HiveWeave Python 后端五轴对抗式审查报告

审查范围：DB 层（meta.py / project.py / schema.py）+ 核心服务层（permission / inbox / handoff / memory / game_time / approval / charter）+ 对话层（store / compaction）

审查日期：2026-07-05

---

## Critical（阻塞合并）

### C1. `charter.py` save_charter 事务失败后未 rollback，可导致章程被删除而不插入新章程

**文件**：`apps/hiveweave-py/src/hiveweave/services/charter.py`，行 56-73

```python
db = await meta_db.get_meta_db()
try:
    await db.execute("DELETE FROM agent_charters WHERE project_id = ?", [project_id])
    await db.execute("""INSERT INTO agent_charters ... VALUES (...)""", [...])
    await db.commit()
except Exception as e:
    logger.error("charter.save_failed", ...)
    raise  # ← 未调用 db.rollback()
```

**问题**：aiosqlite 默认 isolation_level="" (deferred)，DELETE 隐式开启事务。若 INSERT 失败（约束冲突 / 磁盘错误 / 连接中断），`except` 块仅 log + raise，**不调用 `db.rollback()`**。此时事务保持开启状态，DELETE 仍在连接上挂起。由于 Meta DB 是全局单连接（`meta.py` `_db`），下一次任何 `meta_db.execute()` 调用（来自任何服务）的 `db.commit()` 会**提交这个孤立的 DELETE**——旧章程被删除，新章程从未插入。数据丢失。

**修复**：在 `except` 块中加 `await db.rollback()`，或使用 `async with db.execute(...)` 上下文管理器，或使用 `db.executescript()` 原子执行。

---

### C2. `conversation/store.py` _persist_turn 存在 TOCTOU 竞态，可产生重复 / 交错 turn_index

**文件**：`apps/hiveweave-py/src/hiveweave/conversation/store.py`，行 322-342

```python
async def _persist_turn(self, agent_id: str, messages: list[dict]) -> None:
    ...
    row = await project_db.query_one(
        agent_id,
        "SELECT MAX(turn_index) AS max_idx FROM conversation_turns WHERE agent_id = ?",
        [agent_id])
    max_idx = row["max_idx"] if row and row["max_idx"] is not None else -1
    await project_db.execute(
        agent_id,
        "INSERT INTO conversation_turns ... VALUES (?, ?, ?, ?, ?, ?)",
        [turn_id, agent_id, max_idx + 1, raw, tokens, now])
```

**问题**：`append_turn`（行 83）通过 `asyncio.create_task(self._persist_turn(...))` fire-and-forget 调用此方法。两个并发的 `append_turn` 可产生两个并发的 `_persist_turn` task。虽然 aiosqlite 单连接序列化 SQL 执行，但**协程级**的 TOCTOU 仍然存在：

1. Task A 执行 `SELECT MAX(turn_index)` → 得到 N
2. Task B 执行 `SELECT MAX(turn_index)` → 仍得到 N（A 的 INSERT 尚未执行或尚未提交）
3. Task A 执行 INSERT turn_index=N+1
4. Task B 执行 INSERT turn_index=N+1 ← **重复**

schema.py 行 342 仅有普通索引 `idx_conversation_turns_agent_id ON conversation_turns(agent_id, turn_index)`，**非 UNIQUE 约束**，不会阻止重复插入。`_load_from_db` 的 `ORDER BY turn_index ASC` 会任意交错两个相同 turn_index 的行。

**修复**：为 `(agent_id, turn_index)` 添加 UNIQUE 约束；或用 `asyncio.Lock` per agent 序列化持久化；或改用 `INSERT INTO ... SELECT COALESCE(MAX(turn_index), -1) + 1 FROM conversation_turns WHERE agent_id = ?` 单语句原子计算。

---

### C3. `conversation/store.py` _do_compaction fire-and-forget 覆盖缓存，丢失压缩期间新增的消息

**文件**：`apps/hiveweave-py/src/hiveweave/conversation/store.py`，行 135-172

```python
async def _maybe_trigger_compaction(self, agent_id, project_id, key, messages) -> None:
    ...
    asyncio.create_task(
        self._do_compaction(agent_id, project_id, key, messages, budget)  # ← fire-and-forget
    )

async def _do_compaction(self, agent_id, project_id, key, messages, budget) -> None:
    ...
    compacted = await self._compaction.compact(messages, budget, callback)  # ← LLM 调用，耗时数秒
    ...
    self._cache[key] = history  # ← 覆盖缓存，丢失期间新增的消息
```

**问题**：`_do_compaction` 作为 fire-and-forget task 执行，内部 `compact()` 调用 LLM（行 93 `await llm_callback(prompt)`），耗时数秒。在此期间，`append_turn` 可能被再次调用，向 `self._cache[key]` 追加新消息。当 `_do_compaction` 完成后，`self._cache[key] = history` 用基于旧快照的压缩结果**覆盖整个缓存**，压缩期间新增的消息**全部丢失**。

**修复**：不要 fire-and-forget 压缩；或在写入缓存时 merge 而非覆盖（保留压缩期间新增的尾部消息）；或用 per-agent lock 序列化 append 与 compaction。

---

### C4. `game_time.py` _fire_alarm 失败时仍标记 fired=True，可永久丢失告警消息

**文件**：`apps/hiveweave-py/src/hiveweave/services/game_time.py`，行 130-138 + 186-197

```python
# tick() 行 130-138
for alarm in due:
    try:
        await self._fire_alarm(alarm)
    except Exception as e:
        log.error("alarm_fire_failed", ...)
    alarm["fired"] = True      # ← 无论成功失败都标记
state["alarms"] = [a for a in state["alarms"] if a not in due]  # ← 从内存移除

# _fire_alarm() 行 186-197
async def _fire_alarm(self, alarm: dict) -> None:
    await _execute(...)  # 1. UPDATE scheduled_alarms SET fired=1, status='fired'
    # 2. send inbox message
    await InboxService().send_message(...)  # ← 若此处失败，DB 已标记 fired=1
```

**问题**：两种失败模式：
- **UPDATE 成功 + inbox 发送失败**：DB 中 `fired=1, status='fired'`，但消息从未送达收件箱。重启后 `_load_state` 不会重新加载（`WHERE fired=0`）。**消息永久丢失。**
- **UPDATE 失败**：DB 仍 `fired=0, status='pending'`，但内存中 `alarm["fired"]=True` 且从 `state["alarms"]` 移除。重启前告警丢失，重启后恢复。临时丢失。

**修复**：先发送 inbox 消息，成功后再 UPDATE DB 标记 fired；或失败时不标记 `fired=True`、不移出内存列表，下个 tick 重试。

---

## Required（必须修复）

### R1. `db/meta.py` + `db/project.py` 懒初始化竞态，可导致连接泄漏

**文件**：`apps/hiveweave-py/src/hiveweave/db/meta.py` 行 100-106；`apps/hiveweave-py/src/hiveweave/db/project.py` 行 37-66

```python
# meta.py
async def get_meta_db() -> aiosqlite.Connection:
    global _db
    if _db is None:           # ← check
        await init_meta_db()  # ← await 期间其他协程也可进入
    assert _db is not None
    return _db

# project.py
async def ensure_project_db(workspace_path: str) -> aiosqlite.Connection:
    ws = str(Path(workspace_path).resolve())
    if ws in _cache:          # ← check
        return _cache[ws]
    ...
    conn = await aiosqlite.connect(db_path)  # ← await 期间其他协程可 miss 缓存
    ...
    _cache[ws] = conn         # ← set，覆盖前一个连接（泄漏）
    return conn
```

**问题**：`check-then-set` 模式在 asyncio 中不安全。`await aiosqlite.connect()` 会 yield 控制权。两个协程同时为同一 workspace 调用 `ensure_project_db` 时，都会 miss 缓存、各自创建连接，第二个覆盖 `_cache[ws]`，第一个连接**永远不会被关闭**——文件描述符泄漏。Meta DB 的 `get_meta_db` 同理（虽然通常启动时显式 init，但懒初始化路径仍有风险）。

**修复**：使用 `asyncio.Lock` 保护初始化；或用 "pending" sentinel（存 `asyncio.Future`），第二个协程 await 同一个 Future。

---

### R2. `db/project.py` 连接缓存与 agent 缓存无上限

**文件**：`apps/hiveweave-py/src/hiveweave/db/project.py` 行 23-26

```python
_cache: dict[str, aiosqlite.Connection] = {}       # 无上限
_agent_cache: dict[str, str] = {}                   # 无上限
```

**问题**：长期运行的服务器，随着项目增多，`_cache` 持有每个项目的 SQLite 连接（文件描述符），无 LRU 驱逐。`_agent_cache` 在 agent 被删除后也不会自动清理（只有显式调用 `evict_project_db` 才清理）。可能导致文件描述符耗尽。

**修复**：为 `_cache` 添加最大容量 + LRU 驱逐（驱逐时关闭连接）；或在 agent 删除时主动清理 `_agent_cache`。

---

### R3. `game_time.py` _fire_alarm 使用 _alarm_project 而非 alarm dict 中的 project_id

**文件**：`apps/hiveweave-py/src/hiveweave/services/game_time.py` 行 187

```python
async def _fire_alarm(self, alarm: dict) -> None:
    await _execute(_alarm_project.get(alarm["id"], ""),  # ← 可能返回 ""
        "UPDATE scheduled_alarms SET fired = 1, fired_at = ?, status = 'fired' "
        "WHERE id = ?", [int(time.time() * 1000), alarm["id"]])
```

**问题**：`alarm` dict 本身包含 `project_id` 字段（`_load_state` 行 169 和 `schedule_alarm` 行 93 都设置了）。但代码绕道使用 `_alarm_project` 映射表，fallback 为空字符串 `""`。若 `_alarm_project` 与 alarm dict 不同步（例如 `cancel_alarm` 已 pop 但 alarm 仍在 `state["alarms"]` 中），`_conn("")` → `get_project_workspace("")` → None → `ValueError`，告警触发失败。

**修复**：直接使用 `alarm["project_id"]`，移除对 `_alarm_project` 的依赖（或仅作为冗余校验）。

---

### R4. `approval.py` resolve_request 对不在 _pending 中的请求静默失败，DB 行永久卡在 pending

**文件**：`apps/hiveweave-py/src/hiveweave/services/approval.py` 行 107-134

```python
async def resolve_request(self, request_id, approved, remember=False, user_note=None):
    entry = self._pending.get(request_id)
    ...
    if entry is not None:
        await project_db.execute(...)  # 更新 DB
        entry.future.set_result(...)
        self._pending.pop(request_id, None)
    else:
        logger.warning("approval.not_in_pending", request_id=request_id)  # ← 仅 log，不更新 DB
```

**问题**：服务器重启后 `_pending` 内存清空。若用户在重启后尝试审批一个重启前创建的 pending 请求（`cleanup_orphaned_requests` 将其设为 timeout 之前的窗口期），`resolve_request` 找不到 entry，**仅打印 warning，不更新 DB**。DB 行永久卡在 `pending` 状态，用户无反馈。

**修复**：`else` 分支也应更新 DB（`UPDATE permission_requests SET status = ? WHERE id = ? AND status = 'pending'`），即使内存 future 已丢失；或返回错误给调用方。

---

### R5. `handoff.py` / `memory.py` / `game_time.py` 每次操作都查 Meta DB 解析 workspace_path（N+1 模式）

**文件**：
- `apps/hiveweave-py/src/hiveweave/services/handoff.py` 行 36-41
- `apps/hiveweave-py/src/hiveweave/services/memory.py` 行 64-70
- `apps/hiveweave-py/src/hiveweave/services/game_time.py` 行 29-33

```python
async def _conn(project_id: str):
    workspace = await meta_db.get_project_workspace(project_id)  # ← 每次都查 Meta DB
    if not workspace:
        raise ValueError(...)
    return await ensure_project_db(workspace)  # ← 连接有缓存，但 workspace 查询无缓存
```

**问题**：三个服务每次 `_query` / `_execute` 都先查 Meta DB 获取 `workspace_path`，再做实际查询。`ensure_project_db` 缓存了连接，但 `get_project_workspace` 没有。对于 game_time 的 5 秒 tick，每次 tick 至少 2 次 Meta DB 查询（persist_time + 可能的 alarm 操作）。高负载下 Meta DB 单连接成为瓶颈。

**修复**：在服务层缓存 `project_id → workspace_path` 映射（类似 `project.py` 的 `_agent_cache`），或在 `ensure_project_db` 中增加 `project_id` 参数直接路由。

---

### R6. `memory.py` invalidate 过度失效——写一个 agent 的记忆会清空同项目所有 agent 的缓存

**文件**：`apps/hiveweave-py/src/hiveweave/services/memory.py` 行 54-60

```python
@classmethod
def invalidate(cls, project_id: str) -> None:
    """Clear all cached memories for a project (契约 05: write 后失效)."""
    to_remove = [k for k in _cache if k[0] == project_id]  # ← 清除该项目所有缓存
    for k in to_remove:
        _cache.pop(k, None)
```

**问题**：`save_memory` 行 143 调用 `self.invalidate(project_id)`，无论 scope 是 `project` / `agent` / `archive`，都清除该项目的**所有**缓存条目。这意味着 agent A 写一条私有记忆（scope=agent），会失效 agent B 的私有记忆缓存、archive 缓存、project 缓存。过度失效导致大量不必要的 DB 重查。

**修复**：按 scope 精细失效。scope=project 时只清 `(project_id, "project")`；scope=agent 时只清 `(project_id, "agent", agent_id, scope)`；scope=archive 时只清对应的 `(project_id, "archive", module_id)`。

---

### R7. `conversation/store.py` _load_from_db 无 LIMIT，加载全量历史到内存

**文件**：`apps/hiveweave-py/src/hiveweave/conversation/store.py` 行 300-317

```python
rows = await project_db.query(
    agent_id,
    "SELECT raw_messages FROM conversation_turns "
    "WHERE agent_id = ? ORDER BY turn_index ASC",  # ← 无 LIMIT
    [agent_id])
```

**问题**：长期运行的 agent 可能积累数千个 turn，每个 turn 的 `raw_messages` 可能很大（含 tool 输出）。全量加载到内存可能导致 OOM 或高延迟。

**修复**：加 `LIMIT`（如最近 500 轮），或分页加载；或在 turn_index 上用范围查询只加载近期轮次。

---

### R8. `inbox.py` / `handoff.py` _ensure_schema 的 _migrated 集合在 DB 重建后不会重新检查

**文件**：
- `apps/hiveweave-py/src/hiveweave/services/inbox.py` 行 23, 26-36
- `apps/hiveweave-py/src/hiveweave/services/handoff.py` 行 33, 58-68

```python
_migrated: set[str] = set()  # 进程级，永不清除

async def _ensure_schema(agent_id: str) -> None:
    if agent_id in _migrated:   # ← 一旦标记，永不重查
        return
    try:
        await project_db.execute(agent_id, "ALTER TABLE inbox ADD COLUMN priority ...")
    except Exception:
        pass
    _migrated.add(agent_id)
```

**问题**：若 per-project DB 被 `evict_project_db` 驱逐后重建（如 workspace 被删除重建），`_migrated` 仍记有该 agent/project，`_ensure_schema` 直接跳过。新 DB 的 `inbox` / `handoffs` 表缺少 `priority` / `context_delivered` 等列，后续查询 `SELECT ... priority ...` 会报 `no such column` 错误。

此外 `except Exception: pass`（inbox.py 行 34 / handoff.py 行 67）过于宽泛——不仅吞掉 "duplicate column"（预期），还吞掉 "database is locked"（应重试）、磁盘错误（应上抛）等。

**修复**：`_migrated` 应在 `evict_project_db` 时清除对应条目；`except` 应只捕获 `aiosqlite.OperationalError` 并检查 "duplicate column" 消息，其他错误上抛。更好的方案是将缺失列直接加入 `schema.py` 的 DDL。

---

### R9. `schema.py` inbox / handoffs 表定义与运行时 schema 不一致

**文件**：`apps/hiveweave-py/src/hiveweave/db/schema.py` 行 131-141（inbox）、行 187-195（handoffs）

**问题**：
- `inbox` 表 DDL 缺少 `priority` 列，由 `inbox.py` 运行时 ALTER 补齐
- `handoffs` 表 DDL 缺少 `module_id` / `expect_report` / `reported_up` / `updated_at` / `context_delivered` 五列，由 `handoff.py` 运行时 ALTER 补齐

这导致：
1. 全新 DB 创建后，在首次 inbox/handoff 操作前，直接查询这些列会失败
2. schema 文档与实际结构不符，维护困难
3. 运行时 ALTER 是反模式——应将列定义在 schema DDL 中

**修复**：将 `priority` 等列直接加入 `schema.py` 的 `PROJECT_DB_TABLES` DDL，移除运行时 ALTER。

---

### R10. `schema.py` permission_requests 表无索引

**文件**：`apps/hiveweave-py/src/hiveweave/db/schema.py` 行 260-273（表定义）、行 339-348（索引列表）

**问题**：`permission_requests` 表无任何索引。`approval.py` 有两个高频查询：
- 行 139-147：`WHERE agent_id = ? AND status = 'pending'`
- 行 159-166：`WHERE project_id = ? AND status = 'pending'`

全表扫描。permission_requests 积累后（从不清理历史记录），查询性能退化。

**修复**：在 `PROJECT_DB_INDEXES` 中添加：
```sql
CREATE INDEX IF NOT EXISTS idx_permission_requests_agent_status ON permission_requests(agent_id, status)
```

---

## Optional（建议改进）

### O1. `permission.py` _extract_args_string 对非 bash 工具拼接所有参数值，模式匹配脆弱

**文件**：`apps/hiveweave-py/src/hiveweave/services/permission.py` 行 160-169

```python
def _extract_args_string(self, tool_name: str, tool_args: dict | None) -> str:
    if tool_name == "bash":
        return str(tool_args.get("command", tool_args.get("cmd", "")))
    parts = []
    for v in tool_args.values():
        parts.append(v if isinstance(v, str) else str(v))
    return " ".join(parts)  # ← 所有值空格拼接
```

**问题**：非 bash 工具的参数被无序拼接（dict 迭代顺序在 Python 3.7+ 是插入顺序，但调用方传参顺序不保证）。参数级模式 `read_file(*secret*)` 会匹配 `"path/to/secret_file other_arg"`，但无法精确定位是哪个参数。可能导致意外 allow/deny。

**建议**：为每个工具定义显式的参数提取规则（如 `read_file` → `path`，`write_file` → `path`），或改为按参数名匹配。

---

### O2. `handoff.py` get_pending_handoffs / get_accepted_handoffs 无 LIMIT

**文件**：`apps/hiveweave-py/src/hiveweave/services/handoff.py` 行 159-177

**问题**：若 agent 积累大量 pending/accepted handoff（如未及时 accept），查询返回全量。建议加 `LIMIT 100`。

---

### O3. `charter.py` update_goals 无法用空列表清空 key_results

**文件**：`apps/hiveweave-py/src/hiveweave/services/charter.py` 行 125-129

```python
kr_raw = goals.get("key_results") or goals.get("keyResults") or []
if kr_raw:                    # ← 空列表为 falsy
    key_results = [self._normalize_kr(kr) for kr in kr_raw]
else:
    key_results = existing.get("keyResults", [])  # ← 回退到旧值
```

**问题**：传入 `key_results=[]` 试图清空目标，`kr_raw=[]` 为 falsy，回退到 existing 值，无法清空。`objective` 和 `focus` 也有同样问题（`or` 链无法区分 "未传" 和 "传了空字符串"）。

**建议**：用 `goals.get("key_results", existing.get("keyResults", []))` 区分未传与空列表。

---

### O4. `compaction.py` resolve_compactor_callback 每次压缩都查 Meta DB

**文件**：`apps/hiveweave-py/src/hiveweave/conversation/compaction.py` 行 203-244

**问题**：每次 `_do_compaction` 都调用 `resolve_compactor_callback(agent_id)`，执行 1-2 次 Meta DB 查询（agent → model → base_url/api_key）。无缓存。

**建议**：缓存 `(agent_id, model_id) → callback` 映射，model 变更时失效。

---

### O5. `compaction.py` _format_for_summary 可构建无界 prompt 字符串

**文件**：`apps/hiveweave-py/src/hiveweave/conversation/compaction.py` 行 145-169

**问题**：遍历所有 old_messages 构建 transcript 字符串，仅对单条消息的 content 截断到 `TOOL_OUTPUT_MAX_CHARS`（2000），但不限制总长度。若 old_messages 有数百条，transcript 可能超过 compactor LLM 的 context window，导致 API 调用失败。

**建议**：限制 transcript 总字符数（如 50K），超限时从最旧开始截断。

---

### O6. `store.py` _clean_messages 不处理 assistant 消息有 tool_calls 但无后续 tool result 的情况

**文件**：`apps/hiveweave-py/src/hiveweave/conversation/store.py` 行 190-208

**问题**：`_clean_messages` 移除孤立 tool 消息（tool result 无匹配 tool_call_id），但**不移除**有 tool_calls 但无匹配 tool result 的 assistant 消息。裁剪后若 assistant(tool_calls) 被保留但其 tool result 被裁掉，LLM API 会拒绝（OpenAI 要求 tool_calls 必须有对应 tool result）。

**建议**：在清理逻辑中双向匹配——也移除无匹配 tool result 的 assistant(tool_calls) 消息。

---

## Nit（小问题）

### N1. `meta.py` _migrate_meta_schema 使用 f-string 拼 SQL

**文件**：`apps/hiveweave-py/src/hiveweave/db/meta.py` 行 57-59

```python
await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")
```

值来自硬编码的 `_META_MIGRATIONS` 列表，无注入风险。但 f-string 拼 DDL 是代码异味，未来若有人误改成动态来源可引入风险。

---

### N2. `approval.py` 使用已弃用的 asyncio.get_event_loop()

**文件**：`apps/hiveweave-py/src/hiveweave/services/approval.py` 行 67

```python
loop = asyncio.get_event_loop()  # ← Python 3.10+ 已弃用
```

应改为 `asyncio.get_running_loop()`（在 async 函数中安全获取当前运行中的 loop）。

---

### N3. `permission.py` READONLY_TOOLS 包含 write_memory / write_work_log

**文件**：`apps/hiveweave-py/src/hiveweave/services/permission.py` 行 23-30

`readonly` 预设包含 `write_memory` 和 `write_work_log`，名称语义与 "readonly" 矛盾。若是设计意图（agent 总能写自己的记忆/日志），建议加注释说明。

---

### N4. `store.py` get_history 每次都对已清洗的缓存重复 _clean_messages

**文件**：`apps/hiveweave-py/src/hiveweave/conversation/store.py` 行 50-59

```python
if key not in self._cache:
    loaded = await self._load_from_db(agent_id)
    self._cache[key] = self._clean_messages(loaded)  # ← 入缓存时清洗
cleaned = self._clean_messages(self._cache[key])      # ← 每次读取再清洗一次
```

幂等但浪费 CPU。缓存中已是清洗后的消息，读取时无需再清洗。

---

## 无发现

### `conversation/compaction.py`（除 O4/O5 外）

核心压缩逻辑（`check_overflow` / `compact` / `_trim_to_budget`）正确，LLM 失败回退到硬截断的设计合理，SUMMARY_MARKER 机制与 store.py 的提取逻辑一致。`_safe_content` 正确处理多模态 content。

---

## 汇总

| 严重程度 | 数量 | 关键项 |
|---|---|---|
| Critical | 4 | charter 事务无 rollback / turn_index 竞态 / 压缩覆盖缓存 / 告警永久丢失 |
| Required | 10 | 懒初始化竞态 / 缓存无上限 / N+1 查询 / 过度失效 / schema 不一致 / 无索引 |
| Optional | 6 | 参数匹配脆弱 / 无 LIMIT / 无法清空 key_results / 无界 prompt |
| Nit | 4 | f-string DDL / 弃用 API / 命名 / 冗余清洗 |

最高优先级修复：**C1-C4 四个 Critical 项均为数据丢失类问题**，必须在合并前修复。其中 C2（turn_index 竞态）和 C3（压缩覆盖缓存）在并发场景下极易触发。
