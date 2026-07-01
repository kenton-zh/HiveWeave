defmodule HiveWeave.Services.Dispatch do
  @moduledoc """
  Work log audit service — records what happened during agent dispatch and execution.

  All queries route to per-project DB via ProjectFactory.
  """

  alias HiveWeave.Repo.ProjectFactory

  require Logger

  @doc """
  Coordinator dispatches a task to a subordinate.
  Writes a work_log entry with type=discussion.
  """
  def dispatch_task(project_id, from_agent_id, to_agent_id, description, session_id) do
    log_id = Ecto.UUID.generate()
    now_ms = System.system_time(:millisecond)

    details = Jason.encode!(%{
      from_agent_id: from_agent_id,
      to_agent_id: to_agent_id,
      description: description
    })

    sql = "INSERT INTO work_logs (id, agent_id, project_id, session_id, type, summary, details, created_at) VALUES (?, ?, ?, ?, 'discussion', ?, ?, ?)"

    case ProjectFactory.query(project_id, sql, [log_id, from_agent_id, project_id, session_id, description, details, now_ms]) do
      {:ok, _} ->
        Logger.info("[Dispatch] Task dispatched: #{from_agent_id} → #{to_agent_id}: #{String.slice(description, 0, 80)}")
        {:ok, %{task_id: log_id, from_agent_id: from_agent_id, to_agent_id: to_agent_id, description: description}}

      {:error, reason} ->
        Logger.error("[Dispatch] Failed to dispatch task: #{inspect(reason)}")
        {:error, reason}
    end
  end

  @doc """
  Write a work log entry for an agent.
  """
  def write_work_log(project_id, agent_id, session_id, type, summary, details \\ %{}) do
    log_id = Ecto.UUID.generate()
    now_ms = System.system_time(:millisecond)
    details_json = if is_map(details), do: Jason.encode!(details), else: details || "{}"

    sql = "INSERT INTO work_logs (id, agent_id, project_id, session_id, type, summary, details, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"

    case ProjectFactory.query(project_id, sql, [log_id, agent_id, project_id, session_id, type || "discussion", summary, details_json, now_ms]) do
      {:ok, _} -> {:ok, log_id}
      {:error, reason} -> {:error, reason}
    end
  end

  @doc """
  Get subordinate's recent work logs (newest first).
  """
  def get_subordinate_logs(project_id, subordinate_agent_id, limit \\ 10) do
    sql = "SELECT id, agent_id, type, summary, details, created_at FROM work_logs WHERE agent_id = ? ORDER BY created_at DESC LIMIT ?"

    case ProjectFactory.query(project_id, sql, [subordinate_agent_id, limit]) do
      {:ok, r} ->
        Enum.map(r.rows, fn [id, agent_id, type, summary, details, created_at] ->
          %{
            id: id,
            agent_id: agent_id,
            type: type,
            summary: summary,
            details: parse_json(details),
            created_at: created_at
          }
        end)

      {:error, _} -> []
    end
  end

  @doc """
  Get agent's own work logs (newest first).
  """
  def get_agent_logs(project_id, agent_id, limit \\ 20) do
    get_subordinate_logs(project_id, agent_id, limit)
  end

  @doc """
  Get subordinate logs since a timestamp (oldest first, for incremental reads).
  """
  def get_subordinate_logs_since(project_id, subordinate_agent_id, since_timestamp) do
    sql = "SELECT id, agent_id, type, summary, details, created_at FROM work_logs WHERE agent_id = ? AND created_at > ? ORDER BY created_at ASC"

    case ProjectFactory.query(project_id, sql, [subordinate_agent_id, since_timestamp]) do
      {:ok, r} ->
        Enum.map(r.rows, fn [id, agent_id, type, summary, details, created_at] ->
          %{
            id: id,
            agent_id: agent_id,
            type: type,
            summary: summary,
            details: parse_json(details),
            created_at: created_at
          }
        end)

      {:error, _} -> []
    end
  end

  @doc """
  Coordinator approves subordinate's work.
  """
  def approve_work(project_id, coordinator_id, session_id, subordinate_id, review \\ nil) do
    summary = "Approved work from #{subordinate_id}"
    summary = if review, do: "#{summary}: #{review}", else: summary
    write_work_log(project_id, coordinator_id, session_id, "completion", summary, %{subordinate_id: subordinate_id, review: review})
  end

  @doc """
  Coordinator rejects subordinate's work.
  """
  def reject_work(project_id, coordinator_id, session_id, subordinate_id, feedback) do
    summary = "Rejected work from #{subordinate_id}: #{feedback}"
    write_work_log(project_id, coordinator_id, session_id, "error", summary, %{subordinate_id: subordinate_id, feedback: feedback})
  end

  defp parse_json(nil), do: %{}
  defp parse_json(str) when is_binary(str) do
    case Jason.decode(str) do
      {:ok, map} -> map
      _ -> %{}
    end
  end
end
