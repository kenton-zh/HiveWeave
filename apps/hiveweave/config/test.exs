import Config

# We don't run a server during test.
config :hiveweave, HiveWeaveWeb.Endpoint,
  http: [ip: {127, 0, 0, 1}, port: 4001],
  server: false

# Use a separate test database
config :hiveweave, :meta_db_path,
  Path.expand("../test/data/test_meta.db", __DIR__) |> String.replace("\\", "/")

# Print only warnings and errors during test
config :logger, level: :warning

config :hiveweave, :env, :test
