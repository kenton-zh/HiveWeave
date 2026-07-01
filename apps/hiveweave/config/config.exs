import Config

# General configuration
config :hiveweave,
  ecto_repos: [HiveWeave.Repo.Meta],
  generators: [binary_id: true, timestamp_type: :integer]

# Use binary_id for all schemas by default
config :hiveweave, HiveWeave.Repo.Meta,
  primary_key: [type: :binary_id, autogenerate: true],
  foreign_key_type: :binary_id

# Configures the meta DB path (WAL mode). Override via HIVEWEAVE_META_DB_PATH env.
meta_db_default =
  Path.join([__DIR__, "..", "..", "..", "packages", "db", "data", "hiveweave.db"])
  |> Path.expand()

config :hiveweave, :meta_db_path, System.get_env("HIVEWEAVE_META_DB_PATH") || meta_db_default

# Configures the meta DB
meta_db_path = System.get_env("HIVEWEAVE_META_DB_PATH") || meta_db_default

config :hiveweave, HiveWeave.Repo.Meta,
  database: meta_db_path,
  pool_size: 5,
  journal_mode: :wal

# Configures the per-project Repo defaults
config :hiveweave, :project_db_dir, ".hiveweave"
config :hiveweave, :project_db_filename, "data.db"

# Configures Phoenix
config :hiveweave, HiveWeaveWeb.Endpoint,
  url: [host: "localhost"],
  adapter: Bandit.PhoenixAdapter,
  render_errors: [
    formats: [json: HiveWeaveWeb.ErrorView],
    layout: false
  ],
  pubsub_server: HiveWeave.PubSub,
  presence: HiveWeaveWeb.Presence

# Configures the LLM provider
# Primary: DeepSeek V4 Flash Free (限流后自动切换到 Open Code 付费版)
config :hiveweave, :llm_providers,
  primary: %{
    base_url: System.get_env("OPENCODE_BASE_URL") || "https://opencode.ai/zen/v1",
    model: System.get_env("OPENCODE_MODEL") || "deepseek-v4-flash-free",
    api_key: System.get_env("OPENCODE_API_KEY") || "",
    provider: "openai-compatible",
    context_window: 200_000,
    max_output_tokens: 8_192,
    fallback: :fallback
  },
  fallback: %{
    base_url: System.get_env("OPENCODE_GO_BASE_URL") || "https://opencode.ai/zen/go/v1",
    model: "deepseek-v4-flash",
    api_key: System.get_env("OPENCODE_API_KEY") || "",
    provider: "openai-compatible",
    context_window: 1_000_000,
    max_output_tokens: 8_192
  }

# Import environment specific config
import_config "#{config_env()}.exs"
