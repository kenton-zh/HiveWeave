defmodule HiveWeave.Services.Settings do
  @moduledoc """
  SettingsService — global key-value settings store.
  Uses the global_settings table in Meta DB.
  """

  require Logger

  @doc """
  Get a setting value by key.
  Returns the value string or nil.
  """
  def get(key) do
    case Ecto.Adapters.SQL.query(
      HiveWeave.Repo.Meta,
      "SELECT value FROM global_settings WHERE key = ?",
      [key]
    ) do
      {:ok, r} ->
        case r.rows do
          [[value] | _] -> value
          _ -> nil
        end
      {:error, _} -> nil
    end
  rescue
    _ -> nil
  end

  @doc """
  Set a setting value (upsert).
  """
  def set(key, value) do
    {:ok, _} = Ecto.Adapters.SQL.query(
      HiveWeave.Repo.Meta,
      "DELETE FROM global_settings WHERE key = ?",
      [key]
    )

    case Ecto.Adapters.SQL.query(
      HiveWeave.Repo.Meta,
      "INSERT INTO global_settings (key, value, updated_at) VALUES (?, ?, ?)",
      [key, to_string(value), System.system_time(:millisecond)]
    ) do
      {:ok, _} ->
        Logger.info("[Settings] Set #{key} = #{String.slice(to_string(value), 0, 80)}")
        {:ok, value}
      {:error, reason} ->
        Logger.error("[Settings] Failed to set #{key}: #{inspect(reason)}")
        {:error, reason}
    end
  rescue
    e -> {:error, inspect(e)}
  end

  @doc """
  Get all settings as a map.
  """
  def all do
    case Ecto.Adapters.SQL.query(
      HiveWeave.Repo.Meta,
      "SELECT key, value FROM global_settings ORDER BY key"
    ) do
      {:ok, r} ->
        r.rows
        |> Enum.map(fn [k, v] -> {k, v} end)
        |> Enum.into(%{})
      {:error, _} -> %{}
    end
  rescue
    _ -> %{}
  end

  @doc """
  Delete a setting.
  """
  def delete(key) do
    {:ok, _} = Ecto.Adapters.SQL.query(
      HiveWeave.Repo.Meta,
      "DELETE FROM global_settings WHERE key = ?",
      [key]
    )
    :ok
  rescue
    _ -> :ok
  end
end
