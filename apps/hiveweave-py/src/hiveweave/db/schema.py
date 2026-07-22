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
        default_reasoning_effort TEXT,
        temperature REAL DEFAULT 1.0,
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
        status TEXT NOT NULL DEFAULT 'pending',
        result_hash TEXT,
        result_size INTEGER,
        error TEXT,
        started_at INTEGER NOT NULL,
        ended_at INTEGER,
        duration_ms INTEGER
    )
    """,
]

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
]
