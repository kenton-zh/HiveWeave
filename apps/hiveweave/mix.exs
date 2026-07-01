defmodule HiveWeave.MixProject do
  use Mix.Project

  def project do
    [
      app: :hiveweave,
      version: "0.2.0",
      elixir: "~> 1.17",
      elixirc_paths: elixirc_paths(Mix.env()),
      start_permanent: Mix.env() == :prod,
      aliases: aliases(),
      deps: deps()
    ]
  end

  def application do
    [
      extra_applications: [:logger, :runtime_tools, :inets, :ssl, :xmerl],
      mod: {HiveWeave.Application, []}
    ]
  end

  defp elixirc_paths(:test), do: ["lib", "test/support"]
  defp elixirc_paths(_), do: ["lib"]

  defp deps do
    [
      # Web framework
      {:plug, "~> 1.16"},
      {:plug_cowboy, "~> 2.7"},
      {:bandit, "~> 1.6"},
      {:phoenix, "~> 1.7.14"},
      {:phoenix_pubsub, "~> 2.1"},
      {:cors_plug, "~> 3.0"},

      # Data layer
      {:ecto, "~> 3.12"},
      {:ecto_sql, "~> 3.12"},
      {:ecto_sqlite3, "~> 0.15"},

      # JSON
      {:jason, "~> 1.4"},

      # HTTP client (for LLM streaming)
      {:req, "~> 0.5"},
      {:finch, "~> 0.18"},

      # Telemetry & metrics
      {:telemetry, "~> 1.2"},
      {:telemetry_metrics, "~> 0.6"},

      # File watching / hot code reload (dev)
      {:file_system, "~> 1.0", only: :dev},

      # Testing - Phoenix ships ConnTest for free
    ]
  end

  defp aliases do
    [
      "ecto.setup": ["ecto.create", "ecto.migrate", "run priv/repo/seeds.exs"],
      test: ["test"]
    ]
  end
end
