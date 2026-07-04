defmodule HiveWeave.Services.Charter do
  require Logger

  @goals_sync_table :hiveweave_goals_sync

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

    # User involvement — defaults to "宏观决策+技术选型" if not set
    user_involvement = attrs[:user_involvement] || attrs["user_involvement"] || Map.get(existing, "userInvolvement", Map.get(existing, :userInvolvement, "宏观决策+技术选型"))

    goals_json = Jason.encode!(%{
      "objective" => merged_objective,
      "focus" => merged_focus,
      "keyResults" => merged_krs,
      "userInvolvement" => user_involvement
    })

    sql = "UPDATE projects SET charter_json = ? WHERE id = ?"

    case Ecto.Adapters.SQL.query(HiveWeave.Repo.Meta, sql, [goals_json, project_id]) do
      {:ok, _r} ->
        Logger.info("[Charter] Updated goals for project #{project_id}")
        touch_goals_version(project_id)
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

  # ── Goals sync (dirty-flag pattern) ──────────────────────────
  #
  # When the goals workbook is updated (even by one character), the project's
  # goals version is bumped. Each agent remembers the version it last read.
  # On the agent's next message, if its last-read version != current version,
  # the latest goals are injected into context and the agent's version is updated.
  # New agents start with version = nil, so they always read on first message.

  defp ensure_goals_sync_table do
    if :ets.whereis(@goals_sync_table) == :undefined do
      try do
        :ets.new(@goals_sync_table, [:set, :public, :named_table])
      rescue
        ArgumentError -> :ok  # already created by another process
      end
    end
    :ok
  end

  @doc "Bump the goals version for a project. Call this whenever goals change."
  def touch_goals_version(project_id) do
    ensure_goals_sync_table()
    now = System.monotonic_time(:nanosecond)
    :ets.insert(@goals_sync_table, {{:version, project_id}, now})
    :ok
  end

  @doc "Get the current goals version for a project (nil if never set)."
  def get_goals_version(project_id) do
    ensure_goals_sync_table()
    case :ets.lookup(@goals_sync_table, {:version, project_id}) do
      [{_, v}] -> v
      [] -> nil
    end
  end

  @doc "Get the version an agent last read (nil if never read)."
  def get_agent_goals_version(project_id, agent_id) do
    ensure_goals_sync_table()
    case :ets.lookup(@goals_sync_table, {{:read, project_id, agent_id}}) do
      [{_, v}] -> v
      [] -> nil
    end
  end

  @doc "Mark that an agent has read the goals at the given version."
  def set_agent_goals_version(project_id, agent_id, version) do
    ensure_goals_sync_table()
    :ets.insert(@goals_sync_table, {{:read, project_id, agent_id}, version})
    :ok
  end

  @doc "Check if an agent needs to re-read the goals (dirty flag).
  An agent is dirty if:
  - goals version exists AND agent hasn't read it (version mismatch or nil)
  - OR goals have never been versioned but the agent has never read (first time)"
  def goals_dirty?(project_id, agent_id) do
    case get_goals_version(project_id) do
      nil ->
        # Goals never explicitly versioned. Agent is dirty if it has never read.
        get_agent_goals_version(project_id, agent_id) == nil
      version ->
        version != get_agent_goals_version(project_id, agent_id)
    end
  end
end
