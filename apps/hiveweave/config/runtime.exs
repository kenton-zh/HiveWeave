import Config

# ── Agent cluster diagnostics ─────────────────────────────────
# Set HIVEWEAVE_DIAG=1 (or "true") to enable verbose Streamer/Agent logs
# for debugging multi-agent LLM issues. Off by default in production.
# runtime.exs is evaluated at application start — changing the env var
# only requires restarting the BEAM process, no recompilation needed.
diag_env = System.get_env("HIVEWEAVE_DIAG")
diag_enabled = diag_env != nil and String.downcase(diag_env) in ~w(1 true yes)

config :hiveweave, :diagnostics, enabled: diag_enabled
