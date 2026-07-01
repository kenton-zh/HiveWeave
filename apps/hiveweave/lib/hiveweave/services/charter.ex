defmodule HiveWeave.Services.Charter do
  require Logger

  # Ensure the agent_charters table exists in Meta DB
  defp ensure_table do
    Ecto.Adapters.SQL.query(HiveWeave.Repo.Meta, """
      CREATE TABLE IF NOT EXISTS agent_charters (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        agent_id TEXT,
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        status TEXT DEFAULT 'draft',
        created_at INTEGER,
        updated_at INTEGER
      )
    """, [])
    :ok
  rescue
    _ -> :ok
  end

  @doc "Save or update the project charter (CEO writes)"
  def save_charter(project_id, agent_id, attrs) do
    ensure_table()
    id = Ecto.UUID.generate()
    now_ms = System.system_time(:millisecond)
    title = attrs[:title] || ""
    content = attrs[:content] || ""
    status = attrs[:status] || "active"

    delete_sql = "DELETE FROM agent_charters WHERE project_id = ?"

    case Ecto.Adapters.SQL.query(HiveWeave.Repo.Meta, delete_sql, [project_id]) do
      {:ok, _} ->
        insert_sql = """
        INSERT INTO agent_charters (id, project_id, agent_id, title, content, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """

        case Ecto.Adapters.SQL.query(HiveWeave.Repo.Meta, insert_sql, [
               id, project_id, agent_id, title, content, status, now_ms, now_ms
             ]) do
          {:ok, _} ->
            Logger.info("[Charter] Saved charter for project #{project_id}: #{String.slice(title, 0, 80)}")
            Phoenix.PubSub.broadcast(HiveWeave.PubSub, "cache_inval:#{project_id}", {:memory_cache_inval, project_id})

            {:ok,
             %{
               id: id,
               project_id: project_id,
               agent_id: agent_id,
               title: title,
               content: content,
               status: status,
               created_at: now_ms,
               updated_at: now_ms
             }}

          {:error, reason} ->
            Logger.error("[Charter] Failed to insert charter: #{inspect(reason)}")
            {:error, reason}
        end

      {:error, reason} ->
        Logger.error("[Charter] Failed to delete existing charter: #{inspect(reason)}")
        {:error, reason}
    end
  end

  @doc "Read the current project charter"
  def read_charter(project_id) do
    ensure_table()
    sql = """
    SELECT id, project_id, agent_id, title, content, status, created_at, updated_at
    FROM agent_charters WHERE project_id = ? ORDER BY created_at DESC LIMIT 1
    """

    case Ecto.Adapters.SQL.query(HiveWeave.Repo.Meta, sql, [project_id]) do
      {:ok, r} ->
        case r.rows do
          [[id, proj_id, agent_id, title, content, status, created_at, updated_at]] ->
            time_str = format_time(created_at)
            formatted = "## Project Charter: #{title}\n\n#{content}\n\n_Status: #{status}, Created: #{time_str}_"

            {:ok,
             %{
               id: id,
               project_id: proj_id,
               agent_id: agent_id,
               title: title,
               content: content,
               status: status,
               created_at: created_at,
               updated_at: updated_at,
               formatted: formatted
             }}

          _ ->
            nil
        end

      {:error, _} -> nil
    end
  end

  @doc "Read enterprise goals from projects table (Meta DB). Returns a map or nil."
  def read_goals(project_id) do
    sql = "SELECT charter_json FROM projects WHERE id = ?"

    case Ecto.Adapters.SQL.query(HiveWeave.Repo.Meta, sql, [project_id]) do
      {:ok, r} ->
        case r.rows do
          [[charter_json]] when not is_nil(charter_json) ->
            # Try to parse as JSON (front-end format); fall back to treating as raw text
            case Jason.decode(charter_json) do
              {:ok, decoded} when is_map(decoded) ->
                {:ok, decoded}
              _ ->
                # charter_json is raw text (old format), wrap it
                {:ok, %{objective: charter_json, focus: nil, keyResults: []}}
            end

          _ ->
            {:ok, nil}
        end

      {:error, reason} ->
        Logger.error("[Charter] Failed to read goals: #{inspect(reason)}")
        {:error, reason}
    end
  end

  @doc "Update enterprise goals. Writes JSON format compatible with front-end."
  def update_goals(project_id, attrs) do
    objective = attrs[:objective] || attrs["objective"] || ""
    focus = attrs[:focus] || attrs["focus"] || ""
    key_results_raw = attrs[:key_results] || attrs["key_results"] || []

    # Normalize key_results to array of {text, status, owner} objects (front-end format)
    # Accept both string arrays and object arrays as input
    key_results = Enum.map(key_results_raw, fn kr ->
      cond do
        is_binary(kr) ->
          %{"text" => kr, "status" => "doing", "owner" => nil}
        is_map(kr) ->
          %{
            "text" => kr["text"] || kr[:text] || "",
            "status" => kr["status"] || kr[:status] || "doing",
            "owner" => kr["owner"] || kr[:owner]
          }
        true ->
          %{"text" => to_string(kr), "status" => "doing", "owner" => nil}
      end
    end)

    # Read existing goals to merge (preserve existing key results if not provided)
    existing = case read_goals(project_id) do
      {:ok, map} when is_map(map) -> map
      _ -> %{}
    end

    merged_objective = if objective != "", do: objective, else: Map.get(existing, "objective", Map.get(existing, :objective, ""))
    merged_focus = if focus != "", do: focus, else: Map.get(existing, "focus", Map.get(existing, :focus, ""))
    merged_krs = if key_results != [], do: key_results, else: Map.get(existing, "keyResults", Map.get(existing, :keyResults, []))

    goals_json = Jason.encode!(%{
      "objective" => merged_objective,
      "focus" => merged_focus,
      "keyResults" => merged_krs
    })

    sql = "UPDATE projects SET charter_json = ? WHERE id = ?"

    case Ecto.Adapters.SQL.query(HiveWeave.Repo.Meta, sql, [goals_json, project_id]) do
      {:ok, _r} ->
        Logger.info("[Charter] Updated goals for project #{project_id}")
        {:ok, "Goals updated successfully"}

      {:error, reason} ->
        Logger.error("[Charter] Failed to update goals: #{inspect(reason)}")
        {:error, reason}
    end
  end

  defp format_time(nil), do: "unknown"
  defp format_time(ms) when is_integer(ms) do
    DateTime.from_unix!(div(ms, 1000))
    |> Calendar.strftime("%Y-%m-%d %H:%M:%S")
  end
end
