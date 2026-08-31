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
]

# ── Per-project DB 表 ──────────────────────────────────────
# 契约 11: 文件名 data.db（非 project.db），DELETE journal mode，busy_timeout 5000
# agents 表在 per-project DB 中 — 完整 agent 数据按项目物理隔离

PROJECT_DB_TABLES = [
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
        updated_at INTEGER
    )
    """,
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
        task_id TEXT
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
        is_archived INTEGER DEFAULT 0
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
    """
    CREATE TABLE IF NOT EXISTS modules (
        id TEXT PRIMARY KEY,
        project_id TEXT,
        name TEXT NOT NULL,
        path TEXT NOT NULL,
        description TEXT,
        created_at INTEGER,
        updated_at INTEGER
    )
    """,
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
        project_id TEXT NOT NULL
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
        checkpoint_data TEXT
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
        duration_ms INTEGER
    )
    """,
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
    # F7（平台修复计划 2026-08-30）：超时统一分类 — timeout_kind ∈
    # （runner / command / wait）+ timeout_ms，与 F4 事实位正交可组合。
    """ALTER TABLE run_steps ADD COLUMN timeout_kind TEXT""",
    """ALTER TABLE run_steps ADD COLUMN timeout_ms INTEGER""",
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
        created_at INTEGER NOT NULL
    )
    """,
    # F11（平台修复计划 2026-08-30）：缓存治理 — 冷启动标记。run 首请求
    # cache_read=0 且 cache_creation=0 时打 cold_start=1，让「前缀重建成本」
    # 可见可统计（r4：20 run 首请求零命中，合计 ~1.7M tokens 前缀重建无账）。
    # 旧库迁移（新库由上方 CREATE 直接带列；ALTER 必须排在 CREATE 之后）。
    """ALTER TABLE llm_usage ADD COLUMN cold_start INTEGER DEFAULT 0""",
]

# ── Per-project DB 建表自检（迁移顺序缺陷防护）────────────────
# ensure_project_db 建表后逐项核验：关键列缺失 = 迁移断裂 = 记账/事实位
# 静默丢失，必须 fail-loud（启动即崩比静默断账好——TEST_DSH_37 P0-①
# 教训：F11 的 ALTER 曾排在 CREATE 前，新库缺列导致 274 次调用零记账、
# R3 命中率 0% 伪影、Token 页面只显示 2 行压缩数据）。
# DSH 对照：deepseek-harness invariant 框架的启动自检同构
# （packages/llm/token-meter/src/invariant.ts）。
PROJECT_DB_COLUMN_CHECKS: dict[str, set[str]] = {
    "llm_usage": {"cold_start"},
    "run_steps": {"runner_failed", "command_failed", "injection_applied", "timeout_kind", "timeout_ms"},
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
]
