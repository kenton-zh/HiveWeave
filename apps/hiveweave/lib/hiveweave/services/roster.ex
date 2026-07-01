defmodule HiveWeave.Services.Roster do
  @moduledoc """
  Personnel roster service — position, department, responsibilities per agent.
  Uses the personnel_records table in per-project DB.
  """

  alias HiveWeave.Repo.ProjectFactory

  require Logger

  @doc """
  Update an agent's personnel record (upsert).
  """
  def update_roster(project_id, agent_id, attrs) do
    now_ms = System.system_time(:millisecond)
    position = attrs[:position] || attrs["position"] || ""
    department = attrs[:department] || attrs["department"] || ""
    responsibilities = attrs[:responsibilities] || attrs["responsibilities"] || ""

    id = Ecto.UUID.generate()

    # Upsert: delete existing, then insert
    delete_sql = "DELETE FROM personnel_records WHERE project_id = ? AND agent_id = ?"

    case ProjectFactory.query(project_id, delete_sql, [project_id, agent_id]) do
      {:ok, _} ->
        insert_sql = """
        INSERT INTO personnel_records
        (id, project_id, agent_id, position, department, responsibilities, status, updated_by, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)
        """

        case ProjectFactory.query(project_id, insert_sql, [
               id, project_id, agent_id, position, department, responsibilities,
               agent_id, now_ms
             ]) do
          {:ok, _} ->
            Logger.info("[Roster] Updated roster for agent #{agent_id}")
            {:ok, "Roster updated"}

          {:error, reason} ->
            Logger.error("[Roster] Failed to insert roster: #{inspect(reason)}")
            {:error, reason}
        end

      {:error, reason} ->
        {:error, reason}
    end
  end

  @doc """
  Read the full personnel roster for a project.
  Returns formatted text.
  """
  def get_roster(project_id) do
    sql = """
    SELECT r.agent_id, r.position, r.department, r.responsibilities, r.status,
           a.name, a.role, a.short_id
    FROM personnel_records r
    LEFT JOIN agents a ON r.agent_id = a.id
    WHERE r.project_id = ?
    ORDER BY r.department, r.position
    """

    case ProjectFactory.query(project_id, sql, [project_id]) do
      {:ok, r} when r.rows != [] ->
        entries =
          r.rows
          |> Enum.map(fn [_agent_id, position, department, responsibilities, status,
                          name, role, short_id] ->
            "#{name || "?"} (#{short_id || "?"}) — #{role || "?"}\n" <>
            "  Position: #{position}\n" <>
            "  Department: #{department}\n" <>
            "  Responsibilities: #{responsibilities}\n" <>
            "  Status: #{status}"
          end)
          |> Enum.join("\n---\n")

        {:ok, "## Personnel Roster\n\n#{entries}"}

      {:ok, _} ->
        {:ok, "Roster is empty. No personnel records found."}

      {:error, reason} ->
        Logger.error("[Roster] Failed to read roster: #{inspect(reason)}")
        {:error, reason}
    end
  rescue
    e ->
      Logger.warning("[Roster] get_roster failed: #{inspect(e)}")
      {:error, "Failed to read roster"}
  end
end
