import Config

config :hiveweave, HiveWeaveWeb.Endpoint,
  cache_static_manifest: "priv/static/cache_manifest.json",
  http: [ip: {127, 0, 0, 1}, port: 4000],
  check_origin: false

config :hiveweave, :env, :prod
