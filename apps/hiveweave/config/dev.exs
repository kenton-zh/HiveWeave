import Config

config :hiveweave, HiveWeaveWeb.Endpoint,
  http: [ip: {127, 0, 0, 1}, port: 4000],
  check_origin: ["//localhost", "//127.0.0.1"],
  debug_errors: true,
  code_reloader: true,
  watchers: []

config :hiveweave, :env, :dev

# Reduce Ecto SQL query logging to silence (debug is too noisy)
config :hiveweave, HiveWeave.Repo.Meta, log: false
config :hiveweave, HiveWeave.Repo.ProjectFactory, log: false
