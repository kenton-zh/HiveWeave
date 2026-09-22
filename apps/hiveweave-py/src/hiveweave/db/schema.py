"""Database schema definitions — SQL DDL for Meta DB and Per-project DB.

契约 11: 两层 SQLite
- Meta DB: 全局路由表（projects: id, name, workspace_path, created_at）+ 全局配置表
  不再存储任何 per-project 业务数据
- Per-project DB: 每项目一个 data.db（含 agents 表 + 业务数据表 + project_meta）
  agent_id → project_id 路由由 AgentRouter 内存映射完成
"""

# ── Meta DB 表 ──────────────────────────────────────────────
# Meta DB 只存全局路由和配置，不存任何 per-project 业务数据
# agent_index 已移除 — 路由由 AgentRouter 内存映射替代

# mcp_servers 的单一 DDL 定义 —— Meta 建表清单与 services/mcp.py 的幂等兜底
# 共用这一份，避免两处漂移（此前 mcp.py 自持一份 + 进程级标记）。
MCP_SERVERS_DDL = """
CREATE TABLE IF NOT EXISTS mcp_servers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    transport TEXT NOT NULL DEFAULT 'http',
    command TEXT DEFAULT '',
    args TEXT DEFAULT '[]',
    env TEXT DEFAULT '{}',
    url TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    created_at INTEGER
)
"""

META_DB_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS projects (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        workspace_path TEXT,
        created_at INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_templates (
        id TEXT PRIMARY KEY,
        source TEXT DEFAULT 'builtin',
        division TEXT,
        name TEXT NOT NULL,
        role TEXT NOT NULL,
        color TEXT,
        emoji TEXT,
        vibe TEXT,
        description TEXT,
        prompt_body TEXT,
        discipline_suite TEXT DEFAULT '',
        created_at INTEGER,
        updated_at INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS llm_models (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        model_id TEXT NOT NULL,
        base_url TEXT,
        api_key TEXT,
        provider_type TEXT DEFAULT '',
        context_window INTEGER DEFAULT 128000,
        max_output_tokens INTEGER DEFAULT 4096,
        supports_thinking INTEGER DEFAULT 0,
        thinking_format TEXT DEFAULT '',
        default_reasoning_effort TEXT,
        temperature REAL DEFAULT 1.0,
        supports_vision INTEGER DEFAULT 0,
        top_p REAL,
        top_k INTEGER,
        tool_call_rounds INTEGER,
        model_family TEXT DEFAULT '',
        thinking_mode TEXT DEFAULT '',
        is_active INTEGER DEFAULT 1,
        created_at INTEGER,
        updated_at INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS global_settings (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS meta_index (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at INTEGER
    )
    """,
    # mcp_servers 曾由 services/mcp.py 的懒创建 + 进程级布尔 `_schema_ready`
    # 维护。那个标记是「按槽位记忆、不随载体换代失效」的又一例（L18 同族）：
    # close_meta_db 会重置 meta 的迁移标记，却不会重置它 → Meta DB 整代重建后
    # 本表被跳过 → 下游 `no such table: mcp_servers`；而 tests/test_mcp_api.py
    # 必须手工把该布尔复位才能过，正是这个陷阱存在的证据。
    # 归位到统一建表路径后标记语义消失 —— 状态集中在权威源（DSH packages/
    # AGENTS.md:15「Publish state only at its commit point」同旨）。
    MCP_SERVERS_DDL,
]

# ── Per-project DB 表 ──────────────────────────────────────
# 契约 11: 文件名 data.db（非 project.db），DELETE journal mode，busy_timeout 5000
# agents 表在 per-project DB 中 — 完整 agent 数据按项目物理隔离

PROJECT_DB_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS facts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        subject TEXT NOT NULL,
        payload TEXT,
        source TEXT,
        project_id TEXT,
        verified_at INTEGER NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_facts_kind_time
    ON facts (kind, verified_at)
    """,

    """
    CREATE TABLE IF NOT EXISTS agents (
        id TEXT PRIMARY KEY,
        short_id TEXT,
        project_id TEXT NOT NULL,
        name TEXT NOT NULL,
        role TEXT NOT NULL,
        parent_id TEXT,
        module_id TEXT,
        status TEXT DEFAULT 'active',
        goal TEXT,
        backstory TEXT,
        skills TEXT DEFAULT '[]',
        model_id TEXT,
        permission_type TEXT DEFAULT 'executor',
        permission_mode TEXT DEFAULT 'readonly',
        allowed_tools TEXT DEFAULT '[]',
        denied_tools TEXT DEFAULT '[]',
        ask_tools TEXT DEFAULT '[]',
        mcp_servers TEXT DEFAULT '[]',
        bound_skills TEXT DEFAULT '[]',
        reasoning_effort TEXT,
        workspace_path TEXT,
        language TEXT DEFAULT 'en',
        compacted_prefix TEXT,
        created_at INTEGER,
        updated_at INTEGER,
        last_active_at INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS project_meta (
        project_id TEXT PRIMARY KEY,
        description TEXT DEFAULT '',
        org_paradigm TEXT DEFAULT 'solo',
        charter_json TEXT DEFAULT '{}',
        goals_json TEXT DEFAULT '[]',
        language TEXT DEFAULT 'en',
        game_time_accumulated_seconds INTEGER DEFAULT 0,
        -- fixplan §6 #12 交付物平面：web|native-desktop|game-engine|cli|library
        -- （权威定义见 services/delivery_plane.py::DELIVERY_PLANES）。
        -- 视觉门（ui_browser_e2e / code_audit_visual）遇非 web 平面降档。
        delivery_plane TEXT DEFAULT '',
        -- fixplan #8 交付状态位（权威写者 = tools/misc_tools.py::
        -- mark_delivery_complete_tool，唯一；message_user 只读）。
        -- ⚠ **刻意不带 DEFAULT**：语义是「NULL = 未标记（未知）」，
        -- 带 DEFAULT 会让 SQLite 把升级前的存量行回填成那个值，让"未知"
        -- 伪装成"已判定"（09-12 run_steps.started 的实测教训）。
        -- delivery_state: NULL | 'complete' | 'blocked'
        --   （'blocked' 由平台在核验失败时写入，代表"账本仍有未收口项"）
        -- delivery_snapshot: 标记时刻的核验快照 JSON（closed 计数/未读/政策码）
        delivery_state TEXT,
        delivered_at TEXT,
        delivery_snapshot TEXT,
        updated_at INTEGER
    )
    """,
    # fixplan §6 #12：存量库补 delivery_plane（懒迁移；正典已含该列，新库不跑）。
    """ALTER TABLE project_meta ADD COLUMN delivery_plane TEXT DEFAULT ''""",
    # fixplan #8：存量库补交付状态位三列。⚠ 与 delivery_plane 不同，
    # **绝不带 DEFAULT**（NULL = 未标记，见上方建表注释）。
    """ALTER TABLE project_meta ADD COLUMN delivery_state TEXT""",
    """ALTER TABLE project_meta ADD COLUMN delivered_at TEXT""",
    """ALTER TABLE project_meta ADD COLUMN delivery_snapshot TEXT""",
    """
    CREATE TABLE IF NOT EXISTS inbox (
        id TEXT PRIMARY KEY,
        from_agent_id TEXT NOT NULL,
        to_agent_id TEXT NOT NULL,
        message TEXT,
        read INTEGER DEFAULT 0,
        created_at INTEGER,
        message_type TEXT,
        expect_report INTEGER DEFAULT 0,
        priority TEXT DEFAULT 'normal',
        task_id TEXT,
        -- 2026-09-11 正典化（同 tasks，见其注释）：这 9 列此前只靠
        -- `services/inbox._ensure_schema` 的 ALTER 补，于是**每个新建库**都要
        -- 跑 9 条 ALTER（在 Windows 上首次 schema 变更 ~1.5s + 每条 ~11ms）。
        -- 保留 `_MISSING_COLUMNS` 给存量老库；新库建表即完整。
        wake INTEGER DEFAULT 1,
        idempotency_key TEXT,
        delivered INTEGER DEFAULT 1,
        parked INTEGER DEFAULT 0,
        triage_batch_id TEXT,
        wake_category TEXT,
        delivery_state TEXT DEFAULT 'delivered',
        reply_contract_id TEXT,
        reply_to TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chat_messages (
        id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT,
        thinking TEXT,
        tool_calls TEXT,
        tool_call_id TEXT,
        is_streaming INTEGER DEFAULT 0,
        is_background INTEGER DEFAULT 0,
        is_read INTEGER DEFAULT 1,
        is_context INTEGER DEFAULT 0,
        team_from_agent_id TEXT,
        team_to_agent_id TEXT,
        images TEXT,
        metadata TEXT,
        created_at INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS conversation_turns (
        id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        turn_index INTEGER NOT NULL DEFAULT 0,
        raw_messages TEXT NOT NULL DEFAULT '[]',
        approx_tokens INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memories (
        id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        scope TEXT DEFAULT 'agent',
        module_id TEXT,
        type TEXT DEFAULT 'fact',
        content TEXT,
        source_agent_id TEXT,
        metadata TEXT,
        created_at INTEGER,
        updated_at INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS handoffs (
        id TEXT PRIMARY KEY,
        from_agent_id TEXT,
        to_agent_id TEXT,
        module_id TEXT,
        summary TEXT,
        status TEXT,
        expect_report INTEGER DEFAULT 0,
        reported_up INTEGER DEFAULT 0,
        context_delivered INTEGER DEFAULT 0,
        artifact_path TEXT,
        context_refs TEXT,
        created_at INTEGER,
        updated_at INTEGER,
        task_id TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS work_logs (
        id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        project_id TEXT,
        session_id TEXT,
        task_id TEXT,
        action TEXT,
        type TEXT,
        summary TEXT,
        content TEXT,
        details TEXT DEFAULT '{}',
        metadata TEXT DEFAULT '{}',
        created_at INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        title TEXT NOT NULL,
        description TEXT,
        assignee_id TEXT,
        creator_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'created',
        priority INTEGER DEFAULT 2,
        progress INTEGER DEFAULT 0,
        tags TEXT,
        parent_task_id TEXT,
        depends_on TEXT,
        acceptance_criteria TEXT,
        evidence TEXT,
        expected_modules TEXT,
        contract_json TEXT,
        blocked_reason TEXT,
        source TEXT DEFAULT 'agent',
        retry_count INTEGER DEFAULT 0,
        created_at INTEGER NOT NULL,
        claimed_at INTEGER,
        submitted_at INTEGER,
        closed_at INTEGER,
        updated_at INTEGER NOT NULL,
        is_archived INTEGER DEFAULT 0,
        -- 2026-09-11 正典化：这 11 列此前**只**靠
        -- `services/tasks/db._ensure_schema` 的 ALTER 补（正典 DDL 落后于
        -- 实际 schema）。后果不只是"老库要迁移" —— **每个新建库**都要跑 11 条
        -- ALTER，而第一条 schema 变更在 Windows 上要 ~1.5s（首次写页 + 实时
        -- 扫描）；全量测试因此从 8min 涨到 16min（实测 `ALTER due_at`
        -- = 1456ms，其余每条 ~11ms）。
        -- 保留 `_MISSING_COLUMNS` 的 ALTER 路径给**存量老库**，但新库不再需要它
        -- —— 这才是「状态集中在权威源」（DSH packages/AGENTS.md:15）。
        -- 守卫见 tests/test_schema_ddl_migration_sync.py。
        due_at INTEGER,
        wait_kind TEXT,
        wake_at INTEGER,
        policy_id TEXT,
        archived_by TEXT,
        archived_reason TEXT,
        archived_at INTEGER,
        reviewer_id TEXT,
        implementer_id TEXT,
        implementer_worktree TEXT,
        -- 2026-09-14（#11）：任务**种类**——闭合枚举，非成员 ⇒ None（**不猜**，
        -- 对齐 `services/delivery_plane.py::normalize_delivery_plane` 的范式）。
        -- NULL = 普通任务（含存量未回填）；'verify' = 系统 spawn 的 VERIFY 任务。
        -- ⚠ **不带 DEFAULT**：本列语义是「未知 = NULL」，给 DEFAULT 会让"未知"
        -- 伪装成"已判定"，而升级前的存量行会被 SQLite 回填成那个 DEFAULT
        -- （本仓纪律：新列的迁移形态本身就是判据）。
        -- 它取代了此前用**任务标题**判 VERIFY 的文本判据（标题只作展示）——
        -- 标题判据的病灶：改标题即可翻转验收门与串行锁（#11）。
        kind TEXT,
        owner_parked INTEGER DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_events (
        id TEXT PRIMARY KEY,
        agent_id TEXT,
        event_type TEXT,
        payload TEXT,
        created_at INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scheduled_alarms (
        id TEXT PRIMARY KEY,
        project_id TEXT,
        from_agent_id TEXT,
        to_agent_id TEXT,
        purpose TEXT,
        fire_at_game_seconds INTEGER,
        repeat_interval_seconds INTEGER,
        script_command TEXT,
        status TEXT DEFAULT 'pending',
        fired INTEGER DEFAULT 0,
        fired_at INTEGER,
        last_fired_at INTEGER,
        run_count INTEGER DEFAULT 0,
        created_at INTEGER
    )
    """,
    # BUG-036 migration: add recurring + script columns to existing DBs
    """ALTER TABLE scheduled_alarms ADD COLUMN repeat_interval_seconds INTEGER""",
    """ALTER TABLE scheduled_alarms ADD COLUMN script_command TEXT""",
    """ALTER TABLE scheduled_alarms ADD COLUMN last_fired_at INTEGER""",
    """ALTER TABLE scheduled_alarms ADD COLUMN run_count INTEGER DEFAULT 0""",
    """
    CREATE TABLE IF NOT EXISTS questions (
        id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        project_id TEXT,
        question TEXT NOT NULL,
        options TEXT,
        answer TEXT,
        status TEXT DEFAULT 'pending',
        created_at INTEGER,
        answered_at INTEGER
    )
    """,
    """ALTER TABLE questions ADD COLUMN options TEXT""",
    # BUG-A migration: persist worktree creation errors for observability
    """ALTER TABLE agents ADD COLUMN worktree_error TEXT""",
    # D6: activity timestamp — stall/UI must not treat lifecycle status as busy
    """ALTER TABLE agents ADD COLUMN last_active_at INTEGER""",
    # 修 #4: activated_at — agent 首个 turn 完成时写入；NULL 表示从未激活
    """ALTER TABLE agents ADD COLUMN activated_at INTEGER""",
    """
    CREATE TABLE IF NOT EXISTS todos (
        id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        project_id TEXT,
        content TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        priority TEXT DEFAULT 'medium',
        created_at INTEGER,
        updated_at INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS permission_requests (
        id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        project_id TEXT,
        tool_name TEXT NOT NULL,
        tool_arguments TEXT DEFAULT '{}',
        description TEXT DEFAULT '',
        status TEXT DEFAULT 'pending',
        remember INTEGER DEFAULT 0,
        user_note TEXT,
        created_at INTEGER,
        updated_at INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_waits (
        id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        ref TEXT NOT NULL,
        wake_on TEXT NOT NULL DEFAULT '[]',
        expires_at INTEGER,
        obligation_version TEXT,
        phase TEXT,
        note TEXT,
        created_at INTEGER NOT NULL,
        cleared_at INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS team_chat_dedupe (
        id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        dedupe_key TEXT NOT NULL,
        created_at INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS personnel_records (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        position TEXT,
        department TEXT,
        responsibilities TEXT,
        notes TEXT,
        status TEXT DEFAULT 'active',
        hire_date TEXT,
        updated_by TEXT,
        updated_at INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_charters (
        id TEXT PRIMARY KEY,
        project_id TEXT,
        agent_id TEXT NOT NULL,
        title TEXT,
        content TEXT,
        project_rules TEXT DEFAULT '',
        status TEXT DEFAULT 'active',
        version TEXT DEFAULT '1.0',
        created_at INTEGER,
        updated_at INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS game_time_state (
        id TEXT PRIMARY KEY,
        project_id TEXT,
        game_seconds INTEGER DEFAULT 0,
        updated_at INTEGER
    )
    """,
    # 模块表（支持嵌套）—— 形状按 docs/AI工程组织_MVP蓝图.md:283-287。
    # 批次 7 交付面：`parent_module_id` 自引用建模块树；`current_agent_id`
    # 记录当前负责人；`memories.module_id` 在归档时回指本表（见蓝图 :299）。
    # 注：本表**当前无写入方**（批次 7 接管写侧），
    # tests/test_every_project_db_table_has_writer.py 会因此合法变红 —— 预期。
    """
    CREATE TABLE IF NOT EXISTS modules (
        id TEXT PRIMARY KEY,
        project_id TEXT,
        name TEXT NOT NULL,
        -- 注：`path`（模块拥有的代码路径）**刻意保持可空**。旧 DDL 曾写
        -- `path TEXT NOT NULL`，但那张表**全仓零写入方**（批次 5 实测），
        -- 该约束从未被执行过、也从未被验证过。恢复时按**实际已落地的写侧**
        -- 对齐：`services/modules.py::create_module(path=None)` 默认不传，
        -- `api/org.py:433` 的 Pydantic 契约也是 `path: str | None = None`。
        -- 若此处写 NOT NULL，会让「按蓝图建模块树」的合法调用（只给 name）
        -- 直接 IntegrityError —— 那是把一条**没人用过**的旧约束凌驾于现行契约。
        -- 审计意见（恢复丢了 NOT NULL）已收到；此处是**显式取舍**不是遗漏，
        -- 取舍依据 = 已落地的写侧契约 + 蓝图（:283-287）本就不含 path 列。
        path TEXT,
        description TEXT,
        parent_module_id TEXT,
        status TEXT DEFAULT 'active',
        current_agent_id TEXT,
        created_at INTEGER,
        updated_at INTEGER
    )
    """,
    # 存量库补批次 7 需要的三列（懒迁移；正典已含，新库不跑）。
    """ALTER TABLE modules ADD COLUMN parent_module_id TEXT""",
    """ALTER TABLE modules ADD COLUMN status TEXT DEFAULT 'active'""",
    """ALTER TABLE modules ADD COLUMN current_agent_id TEXT""",
    """
    CREATE TABLE IF NOT EXISTS tool_attestations (
        id TEXT PRIMARY KEY,
        tool_call_id TEXT,
        task_id TEXT,
        agent_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        command_or_url TEXT,
        exit_code INTEGER,
        workspace TEXT,
        commit_hash TEXT,
        stdout_hash TEXT,
        artifact_hashes TEXT,
        console_errors INTEGER,
        created_at INTEGER NOT NULL,
        expires_at INTEGER,
        project_id TEXT NOT NULL,
        waiver_kind TEXT
    )
    """,
    """ALTER TABLE tasks ADD COLUMN policy_id TEXT""",
    """ALTER TABLE tasks ADD COLUMN contract_json TEXT""",
    # ── Durable Run Ledger ──────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS agent_activations (
        id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        run_id TEXT,
        trigger_type TEXT,
        trigger_source TEXT,
        trigger_detail TEXT,
        inbox_msg_ids TEXT DEFAULT '[]',
        interrupted_run_id TEXT,
        checkpoint_summary TEXT,
        consumed_at INTEGER,
        created_at INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_runs (
        id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        activation_id TEXT,
        status TEXT NOT NULL DEFAULT 'running',
        lease_expires_at INTEGER,
        budget_llm_calls INTEGER DEFAULT 50,
        budget_tool_calls INTEGER DEFAULT 100,
        budget_elapsed_ms INTEGER DEFAULT 600000,
        actual_llm_calls INTEGER DEFAULT 0,
        actual_tool_calls INTEGER DEFAULT 0,
        started_at INTEGER NOT NULL,
        ended_at INTEGER,
        result_summary TEXT,
        error_reason TEXT,
        checkpoint_data TEXT,
        -- ── 事实位列（2026-09-11 批次 1）───────────────────────────
        -- 存在的理由：回归清单的两条判据**只能靠日志猜**，因为平台没把
        -- 可机检的事实落库 —— 于是口径只能放宽（错）或漏报（也错）。
        --
        -- empty_stream：该 run 是否收到过**明确的空流**（request started but
        --   zero chunks arrived）。有它 ⇒ usage=0 是**正确记账**，不是丢账
        --   （0 chunk 无 token 可记）。此前只有 recovery.py 的一行日志，
        --   回归脚本读不到 ⇒ R11 只能把这类"秒杀型"标为"未排除"。
        -- cache_verdict：首请求的前缀指纹分类（hit_ok / near_zero_hit /
        --   cold_start / cache_window_expired / drift_zero_hit / unknown_usage），
        --   由 llm/streamer/probe.py 计算、此前只落日志。
        --   有它 ⇒ R3 能只在 `drift_zero_hit`（漂移实锤、平台侧可修）判 FAIL，
        --   而不是把三者混成一个命中率数字（那会把"provider 缓存窗口过期"
        --   和"平台自己把前缀改写了"当成同一件事）。
        --   ⚠ Q2（TEST_DSH_66，2026-09-22）：`near_zero_hit` 是**新档**——
        --   此前 `hit_ok` 用存在性判据（cache_read > 0）⇒ 实测 77 条
        --   cache_read≈113 / input≈4.9万（命中率 0.2%~1%）全判绿，
        --   使这一列在"命中率是否达标"这个问题上**不可用于判 FAIL**。
        --   该列**无 CHECK 约束**（TEXT），新增档位不需要迁移。
        empty_stream INTEGER DEFAULT 0,
        cache_verdict TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS run_steps (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        step_index INTEGER NOT NULL,
        step_type TEXT NOT NULL,
        tool_name TEXT,
        tool_call_id TEXT,
        tool_args_hash TEXT,
        tool_args_excerpt TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        result_hash TEXT,
        result_size INTEGER,
        result_excerpt TEXT,
        error TEXT,
        started_at INTEGER NOT NULL,
        ended_at INTEGER,
        duration_ms INTEGER,
        runner_failed INTEGER DEFAULT 0,
        command_failed INTEGER DEFAULT 0,
        injection_applied INTEGER DEFAULT 0,
        timeout_kind TEXT,
        timeout_ms INTEGER,
        outcome_unknown INTEGER DEFAULT 0,
        not_started INTEGER DEFAULT 0,
        started INTEGER DEFAULT 0,
        enforcement TEXT,
        git_hardened INTEGER,
        -- P0-3：沙箱/ACL 拒绝的成因细分（闭合枚举，见 tools/result.py::DeniedBy）。
        -- ⚠ **无 DEFAULT**：NULL 表示「当时未记录」（老行），与任何枚举值都不同形，
        --   回扫判据 `denied_by IS NULL` 才能区分「新增行漏填」与「老行没这列」。
        denied_by TEXT,
        -- 「拒绝来自环境（ACL/封条）而非 runner 没跑起来」—— 与 denied_by 同批观测。
        blocked_by_environment INTEGER,
        -- 由**封条函数**赋值（它唯一知道封了什么）：`acl_lockdown:<path>` 形态。
        sealed_by TEXT,
        executed INTEGER
    )
    """,
    # F5（2026-09-17）：`executed` 执行面事实 —— 「命令**到底有没有启动**」。
    #
    # 为什么必须独立成列（审计 B2 的核心质疑，成立）：只有 `enforcement` 一列
    # 时，三种**语义完全不同**的情况在数据里**同形**（都是 NULL）：
    #   ① `executed=False` 判定成立但进程没起来（pwsh 缺失 / 受限路径抛错）
    #   ② 非 spawn 工具（write_file 等）—— 这个问题**不适用**
    #   ③ `executed=None` 未判定
    # ⇒ 想回答「哪些调用宣告了沙箱却根本没跑」，在数据上**不可判**。
    # 这正是 F5 本身的病：**两个正交事实被塞进一个字段**。工具层把它们并排
    # 存在了内存里，落库面若只有一个 `enforcement`，又坍缩回一根列。
    #
    # 形态纪律与 `enforcement` 完全一致：**无 DEFAULT**（未知 = NULL，
    # 不是 0/否），新行由 INSERT 显式写值，写入方只在能确定时传。
    """ALTER TABLE run_steps ADD COLUMN executed INTEGER""",
    # P0-3（2026-09-21）：拒绝成因细分落库。`denied_by` 是**闭合枚举**
    # （outside_boundary / sealed_git / no_write_sid / unknown_acl），由
    # `tools/fact_positions.py::classify_denied_by` 判定；`blocked_by_environment`
    # 区分「环境拒绝」与「runner 没起来」；`sealed_by` 由封条函数赋值。
    # ⚠ 三列都**无 DEFAULT**（NULL = 老行未记录，与 False/0 不同形）。
    """ALTER TABLE run_steps ADD COLUMN denied_by TEXT""",
    """ALTER TABLE run_steps ADD COLUMN blocked_by_environment INTEGER""",
    """ALTER TABLE run_steps ADD COLUMN sealed_by TEXT""",
    # TEST10: 既有库迁移 — run_steps 增加结果摘录列（观测性，截断 2KB）
    """ALTER TABLE run_steps ADD COLUMN result_excerpt TEXT""",
    # P2-1: 既有库迁移 — run_steps 增加工具参数原文摘录列（观测性，
    # 截断 200 字符；120s 超时命令此前只留 hash 事后不可考）
    """ALTER TABLE run_steps ADD COLUMN tool_args_excerpt TEXT""",
    # F4（平台修复计划 2026-08-30）：三组正交事实位 — 退出码非零本身永远
    # 不足以区分「runner 失败（命令没跑起来）」与「command 失败（跑了但没过）」。
    # 错误文案按事实位合成而非按退出码推断（对齐 DSH RunnerFailureRule）：
    #   runner_failed    — 命令未执行（参数注入破坏 / 方言不支持 / 权限 / 审批 /
    #                       runner 自身故障，如 [No tool executor]）
    #   command_failed   — 命令执行了但失败（测试未过 / 断言失败 / 业务错误）
    #   injection_applied— 平台是否改写/尝试改写这条命令（改写内容记录在
    #                       result_excerpt，回显给 Agent 看得见这双手）
    """ALTER TABLE run_steps ADD COLUMN runner_failed INTEGER DEFAULT 0""",
    """ALTER TABLE run_steps ADD COLUMN command_failed INTEGER DEFAULT 0""",
    """ALTER TABLE run_steps ADD COLUMN injection_applied INTEGER DEFAULT 0""",
    # F7（平台修复计划 2026-08-30）：超时统一分类 + timeout_ms，与 F4 事实位正交可组合。
    # 取值域（2026-09-10 补 `turn`）：
    #   command — 工具**自身声明**的超时（tool_exec.py 的 `Command timed out
    #             after Ns`，帽来自 DECLARED_TIMEOUT_MS / MAX_TIMEOUT_S）
    #   runner  — runner 层超时（进程没起来即超时）〔**预留**：全仓无写入点，
    #             TEST_DSH_50/51 审计实测 `grep 'timeout_kind.*runner'` 为空；
    #             当前该语义由 runner_failed 事实位承担。要么接线要么删，
    #             由 tests/test_prompt_fact_sync.py 的绊线盯着〕
    #   wait    — 等待类（question/spawn 的等待窗口）
    #   turn    — **整轮兜底**超时（core.py 的 asyncio.wait_for(HARD+30) 掐断），
    #             由 run_ledger 的孤儿步骤清扫补写（TEST_DSH_50/51 实测：该出口
    #             此前完全未接线，4 个 600s 硬杀 run 的 timeout_kind 全 NULL）
    """ALTER TABLE run_steps ADD COLUMN timeout_kind TEXT""",
    """ALTER TABLE run_steps ADD COLUMN timeout_ms INTEGER""",
    # L5（2026-09-11）：第三种事实位 —— 悬挂步骤的「结果未知」。
    # 既有两个位回答的都是「命令跑了没 / 跑过没过」，而孤儿步骤（run 已死、
    # 步骤仍 running）**既不是没跑、也不是跑了没过**，是第三种语义：调用已
    # 发出但完成结果未持久化。此前只能靠自由文本 `orphan step swept: ...`
    # 表达 → 下游只能 grep 文案，口径一变就断。
    #
    # 对齐 DSH `packages/core/session/src/repair.ts:14-18` 的两个具名恢复码，
    # 分野 = **有没有 `tool/call` 事件**（即命令是否真的发出过）：
    #   outcome_unknown — 调用已记录但结果未持久化（DSH TOOL_OUTCOME_UNKNOWN）
    #   not_started     — 调用**从未开始**就被重启掐断（DSH TOOL_NOT_STARTED），
    #                     由 startup_sweep 造成，语义更接近「重试即可，无副作用」
    # 两位**分开**而非合成一位：前者要 agent 先核实外部状态，后者可直接重试，
    # 混成一位会把「别盲目重试」的警告浪费在安全的重试上。
    """ALTER TABLE run_steps ADD COLUMN outcome_unknown INTEGER DEFAULT 0""",
    """ALTER TABLE run_steps ADD COLUMN not_started INTEGER DEFAULT 0""",
    # report TEST_DSH_54 #2/#9（2026-09-12）：`outcome_unknown` 与 `not_started`
    # 此前**无法在行级区分** —— `agents/streaming.py` 的 record_step_start
    # （INSERT, status='running'）发生在 execute() **之前**，所以一行 running
    # 既可能是"已派发、执行中被整轮超时掐死"，也可能是"从未派发"。
    # 少了这个输入，v1 提议的"无执行证据 ⇒ not_started ⇒ 可安全重试"会把
    # 前者误标成后者，对 submit_task/dispatch_task 这类副作用工具就是**双发**。
    # 本列是补上的那个输入：execute() 前一刻置 started=1。
    #   行级判定：started=1 → outcome_unknown（可能已有副作用，先核外部状态）
    #             started=0 → not_started（从未执行 ⇒ 必然无副作用 ⇒ 可直接重试）
    # 存量行必须是 **NULL**（"无法判定"）⇒ 一律按 outcome_unknown 保守处理。
    #
    # ⚠ 这里**故意不写 DEFAULT 0**：SQLite 的 `ALTER TABLE ADD COLUMN … DEFAULT 0`
    # 会给**存量行回填 0**（实测：legacy running 行读出 started=0，`IS NULL` 命中 0 行），
    # 于是升级前遗留的 running 行会落进 started=0 → 被判"从未执行、可直接重试" ——
    # 正是本修复要避免的那次**副作用双发邀请**。不写 DEFAULT ⇒ 存量行为 NULL。
    # 新行由 `record_step_start` 的 INSERT **显式**写 started=0（不依赖列默认值）。
    """ALTER TABLE run_steps ADD COLUMN started INTEGER""",
    # #1 治本（2026-09-14）：**执行面观测字段** —— 这条命令实际走的哪条路
    # （`confined` / `native`），由 `acl_sandbox.entry.spawn_agent_command`
    # 无条件盖戳（含原生分支），工具结果透传。
    # 为什么需要它：改造前沙箱路由是**每个工具自己的约定**，漏接不产生任何
    # 信号（实证：`start_dev_server` 从未 import 过 sandbox 而照样以平台身份
    # 执行任意 command）。有了这一列，「某次调用到底有没有沙箱」才可查。
    # ⚠ **不写 DEFAULT**：非 spawn 类工具与升级前遗留行必须是 NULL
    # （= 不适用/未判定）；回填成 'native' 会把"没这条信息"说成"确认无沙箱"。
    """ALTER TABLE run_steps ADD COLUMN enforcement TEXT""",
    # 0-3（2026-09-16）：**让 `HIVEWEAVE_GIT_HARDENED` 有消费者**。
    # 该标记由 `util/win_subprocess.apply_git_hardening` 写进 spawn 的 env，
    # 但此前除"注入器自证幂等"外**零下游** ⇒ git 自毁时无法归因
    # （实证 #23：22 次 `external diff died` 全发生在 agent 自己的 shell 里，
    #  而事后无法回答"那次 git 到底有没有跑在加固环境里"）。
    # 本列 = 该次 spawn 的 env **实际**带没带加固（由 env 构造点读标记得出，
    # 不是对代码路径的推断）。1/0 是确定值，NULL = 不适用/未判定（非 spawn 类
    # 工具、spawn 失败未执行、升级前遗留行）。
    # ⚠ **不写 DEFAULT**（同 `started` / `enforcement` 的理由）：SQLite 的
    # `ADD COLUMN … DEFAULT 0` 会给**存量行回填 0**，把"没这条信息"说成
    # "确认未加固" —— 那是假事实。新行由写入点显式给值。
    """ALTER TABLE run_steps ADD COLUMN git_hardened INTEGER""",
    # F11（平台修复计划 2026-08-30）：缓存治理 — 冷启动标记的 ALTER 已移至
    # CREATE TABLE llm_usage 之后（见列表末尾）。迁移顺序铁律：任何
    # ALTER TABLE <表> ADD COLUMN 必须排在该表的 CREATE TABLE 之后 ——
    # 本列表按序执行，建表循环对 ALTER 异常静默吞（project.py），排错位
    # 的 ALTER 在新库上报 "no such table" 被吞 → 列永远缺失 → 记账全断
    # （TEST_DSH_37 六轮审计 P0-①：274 次调用零记账）。
    # ── Task Transactional Outbox ───────────────────────────
    # 每次 task 状态转换原子写入事件；relay 读取未投递事件并通知相关方
    """
    CREATE TABLE IF NOT EXISTS task_events (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        from_status TEXT,
        to_status TEXT,
        actor_id TEXT,
        payload TEXT DEFAULT '{}',
        created_at INTEGER NOT NULL,
        delivered INTEGER DEFAULT 0,
        delivered_at INTEGER
    )
    """,
    # ── Verification Case ───────────────────────────────────
    # 单一权威实体，关联 original_task → verify_task → merger → QA
    """
    CREATE TABLE IF NOT EXISTS verification_cases (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        original_task_id TEXT NOT NULL,
        verify_task_id TEXT,
        merger_agent_id TEXT,
        qa_agent_id TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        merge_commit_hash TEXT,
        review_notes TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        closed_at INTEGER
    )
    """,
    # ── Demand-driven Staffing ─────────────────────────────
    # 结构化用人需求：VERIFY blocked → 需 QA；新模块 → 需 executor
    """
    CREATE TABLE IF NOT EXISTS staffing_demands (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        role_needed TEXT NOT NULL,
        reason TEXT,
        task_id TEXT,
        priority TEXT DEFAULT 'normal',
        status TEXT DEFAULT 'open',
        fulfilled_by TEXT,
        created_at INTEGER NOT NULL,
        fulfilled_at INTEGER
    )
    """,
    # DESIGN-3: dismiss quota + same-role rehire cooldown audit log
    """
    CREATE TABLE IF NOT EXISTS org_dismiss_log (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        role TEXT,
        role_key TEXT,
        short_id TEXT,
        name TEXT,
        game_day INTEGER NOT NULL,
        dismissed_by TEXT,
        dismissed_at INTEGER NOT NULL
    )
    """,
    # TEST16 P1-2: atomic dedupe — window_bucket enables UNIQUE constraint
    # so INSERT OR IGNORE replaces check-then-act (TOCTOU race).
    """ALTER TABLE team_chat_dedupe ADD COLUMN window_bucket INTEGER""",
    # TEST16 D2: Obligation Ledger — structured obligations with deadlines
    # and escalation. Replaces pure message-driven "hope they read inbox"
    # coordination for merge/review/verify duties.
    """
    CREATE TABLE IF NOT EXISTS obligations (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        owner_agent_id TEXT NOT NULL,
        obligation_type TEXT NOT NULL,
        task_id TEXT,
        context_json TEXT DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'pending',
        created_at INTEGER NOT NULL,
        deadline INTEGER NOT NULL,
        fulfilled_at INTEGER,
        escalated_to TEXT,
        escalated_at INTEGER,
        escalation_count INTEGER DEFAULT 0
    )
    """,
    # ── LLM Token Metering ───────────────────────────────────
    # 每行 = 一次 LLM 请求。归属 agent/run/task/project 四级，
    # 覆盖主对话 / 压缩 / 子代理三条调用路径。best-effort 写入。
    # F11 cold_start 直接进建表语句（新库原生带列）；旧库由紧随其后的
    # ALTER 补列（旧库表已存在，ALTER 不会触发 "no such table"）。
    """
    CREATE TABLE IF NOT EXISTS llm_usage (
        id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        project_id TEXT,
        run_id TEXT,
        task_id TEXT,
        model_id TEXT,
        request_type TEXT DEFAULT 'main',   -- main | compaction_dialog | compaction_memory | subagent
        provider TEXT,
        input_tokens INTEGER DEFAULT 0,
        output_tokens INTEGER DEFAULT 0,
        cache_read_tokens INTEGER DEFAULT 0,
        cache_creation_tokens INTEGER DEFAULT 0,
        total_tokens INTEGER DEFAULT 0,
        duration_ms INTEGER DEFAULT 0,
        cold_start INTEGER DEFAULT 0,
        creation_unreported INTEGER DEFAULT 0,
        created_at INTEGER NOT NULL
    )
    """,
    # F11（平台修复计划 2026-08-30）：缓存治理 — 冷启动标记。run 首请求
    # cache_read=0 且 cache_creation=0 时打 cold_start=1，让「前缀重建成本」
    # 可见可统计（r4：20 run 首请求零命中，合计 ~1.7M tokens 前缀重建无账）。
    # 旧库迁移（新库由上方 CREATE 直接带列；ALTER 必须排在 CREATE 之后）。
    """ALTER TABLE llm_usage ADD COLUMN cold_start INTEGER DEFAULT 0""",
    # 42 轮 P2-9：cache_creation=0 分母缺分量打标 —— 1 = provider 本轮
    # 未回传 cache 写入（usage 缺字段，或 provider 族根本不上报），此时
    # cache_creation_tokens 的 0 是「无数据」；0 = 上游真回传（含真 0）。
    # 量程位由 llm/util.normalize_usage 的 cache_creation_reported 单一判据
    # 取反落库，新旧库都由 ALTER 补列（与 cold_start 同模式）。
    """ALTER TABLE llm_usage ADD COLUMN creation_unreported INTEGER DEFAULT 0""",
    # ── 团队开会（docs/spec/team-meeting.md）─────────────────
    # 平台侧会务记录（人观察 / debug / export），不是 agent 记忆。
    # 每次状态迁移写行；同项目只允许一场进行中（partials unique index，
    # DB 唯一约束而非 check-then-insert）。
    """
    CREATE TABLE IF NOT EXISTS meetings (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        chair_id TEXT NOT NULL,
        title TEXT,
        topics_json TEXT NOT NULL DEFAULT '[]',
        participant_ids_json TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'assembling',
        topic_index INTEGER NOT NULL DEFAULT 0,
        round_index INTEGER NOT NULL DEFAULT 0,
        topic_results_json TEXT NOT NULL DEFAULT '[]',
        delivery_state TEXT NOT NULL DEFAULT 'none',
        hold_started_at INTEGER,
        created_at INTEGER,
        concluded_at INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS meeting_utterances (
        id TEXT PRIMARY KEY,
        meeting_id TEXT NOT NULL,
        project_id TEXT NOT NULL DEFAULT '',
        topic_index INTEGER NOT NULL DEFAULT 0,
        round_index INTEGER NOT NULL DEFAULT 0,
        agent_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT,
        created_at INTEGER,
        -- 结构化弃权原因（2026-09-18）：区分「主动弃权」与「被掐断而未完成」。
        -- 空串 = 非 abstain 或无此信息（旧行）。
        abstain_reason TEXT NOT NULL DEFAULT ''
    )
    """,
    # 迁移：旧库补列（ALTER 失败 = 列已存在，被 ensure_project_db 吞掉）
    """ALTER TABLE meeting_utterances ADD COLUMN abstain_reason TEXT NOT NULL DEFAULT ''""",
]

# ── Per-project DB 建表自检（迁移顺序缺陷防护）────────────────
# ensure_project_db 建表后逐项核验：关键列缺失 = 迁移断裂 = 记账/事实位
# 静默丢失，必须 fail-loud（启动即崩比静默断账好——TEST_DSH_37 P0-①
# 教训：F11 的 ALTER 曾排在 CREATE 前，新库缺列导致 274 次调用零记账、
# R3 命中率 0% 伪影、Token 页面只显示 2 行压缩数据）。
# DSH 对照：deepseek-harness invariant 框架的启动自检同构
# （packages/llm/token-meter/src/invariant.ts）。
PROJECT_DB_COLUMN_CHECKS: dict[str, set[str]] = {
    "llm_usage": {"cold_start", "creation_unreported"},
    "run_steps": {
        "runner_failed", "command_failed", "injection_applied",
        "timeout_kind", "timeout_ms", "outcome_unknown", "not_started",
        "started", "enforcement", "git_hardened",
        # F5（2026-09-17）：执行面事实。登记进启动自检 ⇒ 迁移断裂（ALTER 排到
        # CREATE 前被吞、或旧库没跑 ALTER）会在启动时 fail-loud，而不是让
        # 「宣告了沙箱却没跑」再次静默退化成 NULL（本仓 TEST_DSH_37 P0-1 形态）。
        "executed",
        # P0-3（2026-09-21）：拒绝成因细分三列。登记进启动自检 ⇒ 迁移断裂
        #（ALTER 排到 CREATE 前被吞 / 旧库没跑 ALTER）会在启动时 fail-loud，
        # 而不是让「成因永远 NULL」静默退化成本条要治的那种假归因。
        "denied_by", "blocked_by_environment", "sealed_by",
    },
    # meeting_utterances.abstain_reason（2026-09-18）：区分「主动弃权」与
    # 「被轮次预算掐断/超时/异常」。登记进自检 ⇒ 迁移断裂会在启动时 fail-loud，
    # 而不是让主持人把「没来得及说话」静默读成「没有意见」。
    "meeting_utterances": {"abstain_reason"},
}

# ── Meta DB 索引 ────────────────────────────────────────────

META_DB_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_llm_models_is_active ON llm_models(is_active)",
]

# ── Per-project DB 索引 ────────────────────────────────────

PROJECT_DB_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_agents_project_id ON agents(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_agents_short_id ON agents(short_id)",
    "CREATE INDEX IF NOT EXISTS idx_agents_parent_id ON agents(parent_id)",
    "CREATE INDEX IF NOT EXISTS idx_inbox_to_agent ON inbox(to_agent_id, read)",
    "CREATE INDEX IF NOT EXISTS idx_inbox_created_at ON inbox(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_chat_messages_agent_id ON chat_messages(agent_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_conversation_turns_agent_id ON conversation_turns(agent_id, turn_index)",
    "CREATE INDEX IF NOT EXISTS idx_memories_agent_id ON memories(agent_id, scope)",
    "CREATE INDEX IF NOT EXISTS idx_handoffs_to_agent ON handoffs(to_agent_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_work_logs_agent_id ON work_logs(agent_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_project_status ON tasks(project_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee_id)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_task_id)",
    "CREATE INDEX IF NOT EXISTS idx_tool_attestations_project ON tool_attestations(project_id, kind)",
    "CREATE INDEX IF NOT EXISTS idx_agent_events_agent_id ON agent_events(agent_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_scheduled_alarms_project_id ON scheduled_alarms(project_id, fired)",
    "CREATE INDEX IF NOT EXISTS idx_permission_requests_agent ON permission_requests(agent_id)",
    "CREATE INDEX IF NOT EXISTS idx_personnel_records_agent_id ON personnel_records(agent_id)",
    "CREATE INDEX IF NOT EXISTS idx_agent_charters_project_id ON agent_charters(project_id)",
    # ── Durable Run Ledger indexes ──────────────────────────
    "CREATE INDEX IF NOT EXISTS idx_agent_activations_agent ON agent_activations(agent_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_agent_runs_agent ON agent_runs(agent_id, started_at)",
    "CREATE INDEX IF NOT EXISTS idx_agent_runs_status ON agent_runs(status)",
    "CREATE INDEX IF NOT EXISTS idx_run_steps_run ON run_steps(run_id, step_index)",
    # ── Task Outbox indexes ─────────────────────────────────
    "CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_task_events_undelivered ON task_events(project_id, delivered) WHERE delivered = 0",
    "CREATE INDEX IF NOT EXISTS idx_verification_cases_original ON verification_cases(original_task_id)",
    "CREATE INDEX IF NOT EXISTS idx_verification_cases_verify ON verification_cases(verify_task_id)",
    "CREATE INDEX IF NOT EXISTS idx_staffing_demands_open ON staffing_demands(project_id, status) WHERE status = 'open'",
    "CREATE INDEX IF NOT EXISTS idx_org_dismiss_log_project_day ON org_dismiss_log(project_id, game_day)",
    "CREATE INDEX IF NOT EXISTS idx_org_dismiss_log_role ON org_dismiss_log(project_id, role_key, game_day)",
    # TEST16 P1-2: atomic dedupe — UNIQUE constraint enables INSERT OR IGNORE
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_team_chat_dedupe_atomic ON team_chat_dedupe(agent_id, dedupe_key, window_bucket)",
    # TEST16 D2: Obligation Ledger indexes
    "CREATE INDEX IF NOT EXISTS idx_obligations_pending ON obligations(project_id, status, deadline) WHERE status = 'pending'",
    "CREATE INDEX IF NOT EXISTS idx_obligations_owner ON obligations(owner_agent_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_obligations_task ON obligations(task_id, obligation_type)",
    # ── Timeline v4 §4.7: 单任务聚合 + 时间窗聚合 ─────────────
    "CREATE INDEX IF NOT EXISTS idx_work_logs_task ON work_logs(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_inbox_task ON inbox(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_handoffs_task ON handoffs(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_task_events_created ON task_events(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_handoffs_created ON handoffs(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_work_logs_created ON work_logs(created_at)",
    # ── LLM Token Metering indexes ───────────────────────────
    "CREATE INDEX IF NOT EXISTS idx_llm_usage_agent ON llm_usage(agent_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_llm_usage_project ON llm_usage(project_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_llm_usage_run ON llm_usage(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_llm_usage_task ON llm_usage(task_id)",
    # ── 团队开会索引 ─────────────────────────────────────────
    # 同项目进行中一场：partial UNIQUE 是 DB 级唯一约束（非 check-then-insert）
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_meetings_active_per_project ON meetings(project_id) WHERE status IN ('assembling','collecting','facilitating')",
    "CREATE INDEX IF NOT EXISTS idx_meetings_project_created ON meetings(project_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_meeting_utterances_meeting ON meeting_utterances(meeting_id, topic_index, round_index)",
    "CREATE INDEX IF NOT EXISTS idx_meeting_utterances_agent ON meeting_utterances(meeting_id, agent_id)",
]
