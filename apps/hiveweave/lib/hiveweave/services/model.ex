defmodule HiveWeave.Services.Model do
  @moduledoc """
  ModelService — LLM model registry CRUD in Meta DB.
  """

  require Logger

  @doc "List all LLM models"
  def list_models do
    {:ok, r} = Ecto.Adapters.SQL.query(HiveWeave.Repo.Meta,
      "SELECT id, name, model_id, base_url, api_key, context_window, max_output_tokens, supports_thinking, is_active FROM llm_models ORDER BY created_at ASC", [])
    r.rows |> Enum.map(fn [id, name, mid, base, key, ctx, max_out, thinking, active] ->
      %{id: id, name: name, model_id: mid, base_url: base, api_key: key && String.slice(key, 0, 8) <> "...", context_window: ctx, max_output_tokens: max_out, supports_thinking: thinking == 1, is_active: active == 1}
    end)
  rescue
    e ->
      Logger.warning("[Model] list_models failed: #{inspect(e)}")
      []
  end

  @doc "Get a single model by ID"
  def get_model(id) do
    {:ok, r} = Ecto.Adapters.SQL.query(HiveWeave.Repo.Meta,
      "SELECT id, name, model_id, base_url, api_key, context_window, max_output_tokens, supports_thinking, is_active FROM llm_models WHERE id = ? LIMIT 1", [id])
    case r.rows do
      [[id, name, mid, base, key, ctx, max_out, thinking, active]] ->
        {:ok, %{id: id, name: name, model_id: mid, base_url: base, api_key: key, context_window: ctx, max_output_tokens: max_out, supports_thinking: thinking == 1, is_active: active == 1}}
      _ -> {:error, :not_found}
    end
  rescue
    e -> {:error, inspect(e)}
  end

  @doc "Create a new model"
  def create_model(attrs) do
    id = attrs[:id] || Ecto.UUID.generate()
    name = attrs[:name] || ""
    model_id = attrs[:model_id] || ""
    base_url = attrs[:base_url] || ""
    api_key = attrs[:api_key] || ""
    context_window = attrs[:context_window] || 128_000
    max_output = attrs[:max_output_tokens] || 8_192
    is_active = if attrs[:is_active] != false, do: 1, else: 0
    now = System.system_time(:millisecond)

    case Ecto.Adapters.SQL.query(HiveWeave.Repo.Meta,
      "INSERT INTO llm_models (id, name, model_id, base_url, api_key, context_window, max_output_tokens, is_active, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
      [id, name, model_id, base_url, api_key, context_window, max_output, is_active, now, now]) do
      {:ok, _} -> {:ok, %{id: id, name: name, model_id: model_id}}
      {:error, reason} -> {:error, inspect(reason)}
    end
  rescue
    e -> {:error, inspect(e)}
  end

  @doc "Update a model"
  def update_model(id, attrs) do
    now = System.system_time(:millisecond)
    updates =
      []
      |> maybe_add_update(attrs[:name], "name")
      |> maybe_add_update(attrs[:model_id], "model_id")
      |> maybe_add_update(attrs[:base_url], "base_url")
      |> maybe_add_update(attrs[:api_key], "api_key")
      |> maybe_add_update(attrs[:context_window], "context_window")
      |> maybe_add_update(attrs[:max_output_tokens], "max_output_tokens")
      |> maybe_add_bool_update(attrs, :is_active)
      |> Kernel.++([{"updated_at", now}])

    if updates == [{"updated_at", now}] do
      {:error, "No fields to update"}
    else
      sets = Enum.map(updates, fn {k, _} -> "#{k} = ?" end) |> Enum.join(", ")
      vals = Enum.map(updates, fn {_, v} -> v end) ++ [id]
      case Ecto.Adapters.SQL.query(HiveWeave.Repo.Meta,
        "UPDATE llm_models SET #{sets} WHERE id = ?", vals) do
        {:ok, _} -> {:ok, id}
        {:error, reason} -> {:error, inspect(reason)}
      end
    end
  rescue
    e -> {:error, inspect(e)}
  end

  defp maybe_add_update(acc, nil, _col), do: acc
  defp maybe_add_update(acc, val, col), do: [{col, val} | acc]

  defp maybe_add_bool_update(acc, attrs, col) do
    if Map.has_key?(attrs, col) do
      [{Atom.to_string(col), if(attrs[col], do: 1, else: 0)} | acc]
    else
      acc
    end
  end

  @doc "Delete a model"
  def delete_model(id) do
    case Ecto.Adapters.SQL.query(HiveWeave.Repo.Meta, "DELETE FROM llm_models WHERE id = ?", [id]) do
      {:ok, _} -> :ok
      {:error, reason} -> {:error, inspect(reason)}
    end
  rescue
    e -> {:error, inspect(e)}
  end

  @doc "Get active model IDs for an agent"
  def get_active_models do
    {:ok, r} = Ecto.Adapters.SQL.query(HiveWeave.Repo.Meta,
      "SELECT id, name, model_id FROM llm_models WHERE is_active = 1 ORDER BY created_at ASC", [])
    r.rows |> Enum.map(fn [id, name, mid] -> %{id: id, name: name, model_id: mid} end)
  rescue
    _ -> []
  end
end
