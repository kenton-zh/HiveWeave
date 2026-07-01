ExUnit.start()

# Ensure the test Meta DB has the tables needed by service tests.
# The Meta Repo is already started by the application supervisor when
# `mix test` boots the OTP app (server: false in test config).
try do
  Ecto.Adapters.SQL.query!(HiveWeave.Repo.Meta, """
    CREATE TABLE IF NOT EXISTS global_settings (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL DEFAULT '',
      updated_at INTEGER NOT NULL DEFAULT 0
    )
  """)

  Ecto.Adapters.SQL.query!(HiveWeave.Repo.Meta, """
    CREATE TABLE IF NOT EXISTS permission_requests (
      id TEXT PRIMARY KEY,
      agent_id TEXT NOT NULL,
      project_id TEXT NOT NULL,
      tool_name TEXT NOT NULL,
      tool_arguments TEXT DEFAULT '{}',
      description TEXT DEFAULT '',
      status TEXT NOT NULL DEFAULT 'pending',
      user_note TEXT,
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL
    )
  """)

  Ecto.Adapters.SQL.query!(HiveWeave.Repo.Meta, """
    CREATE TABLE IF NOT EXISTS permission_rules (
      id TEXT PRIMARY KEY,
      agent_id TEXT NOT NULL,
      project_id TEXT NOT NULL,
      tool_pattern TEXT NOT NULL,
      action TEXT NOT NULL DEFAULT 'allow',
      created_at INTEGER NOT NULL
    )
  """)

  Ecto.Adapters.SQL.query!(HiveWeave.Repo.Meta, """
    CREATE TABLE IF NOT EXISTS mcp_servers (
      id TEXT PRIMARY KEY,
      name TEXT NOT NULL UNIQUE,
      transport TEXT NOT NULL DEFAULT 'http',
      command TEXT DEFAULT '',
      url TEXT DEFAULT '',
      created_at INTEGER
    )
  """)

  Ecto.Adapters.SQL.query!(HiveWeave.Repo.Meta, """
    CREATE TABLE IF NOT EXISTS llm_models (
      id TEXT PRIMARY KEY,
      name TEXT NOT NULL DEFAULT '',
      model_id TEXT NOT NULL DEFAULT '',
      base_url TEXT NOT NULL DEFAULT '',
      api_key TEXT NOT NULL DEFAULT '',
      context_window INTEGER NOT NULL DEFAULT 128000,
      max_output_tokens INTEGER NOT NULL DEFAULT 8192,
      supports_thinking INTEGER NOT NULL DEFAULT 0,
      is_active INTEGER NOT NULL DEFAULT 1,
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL
    )
  """)

  Ecto.Adapters.SQL.query!(HiveWeave.Repo.Meta, """
    CREATE TABLE IF NOT EXISTS agent_templates (
      id TEXT PRIMARY KEY,
      source TEXT NOT NULL DEFAULT '',
      division TEXT NOT NULL DEFAULT '',
      name TEXT NOT NULL,
      role TEXT NOT NULL DEFAULT 'specialist',
      color TEXT NOT NULL DEFAULT '',
      emoji TEXT NOT NULL DEFAULT '',
      vibe TEXT NOT NULL DEFAULT '',
      description TEXT NOT NULL DEFAULT '',
      prompt_body TEXT NOT NULL DEFAULT '',
      original_file TEXT NOT NULL DEFAULT '',
      created_at INTEGER NOT NULL
    )
  """)
rescue
  _ -> :ok
end
