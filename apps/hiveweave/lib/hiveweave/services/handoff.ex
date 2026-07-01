defmodule HiveWeave.Services.Handoff do
  @moduledoc """
  Task handoff lifecycle management — tracks tasks from dispatch to approval.

  State machine:
    pending → accepted → completed → approved (terminal)
                         completed → accepted (reopen/rework)
  """

  alias HiveWeave.Repo.ProjectFactory

  require Logger

  @doc """
  Create a new handoff (task assignment).
  Includes deduplication: if an active (pending/accepted) handoff with the same
  from→to and summary already exists, returns the existing one instead of creating a duplicate.
  """
  def create_handoff(project_id, from_agent_id, to_agent_id, summary, opts \\ []) do
    now_ms = System.system_time(:millisecond)
    module_id = Keyword.get(opts, :module_id)
    expect_report = if Keyword.get(opts, :expect_report, false), do: 1, else: 0

    # Deduplication: check if an active handoff with same from→to+summary already exists
    dedup_sql = "SELECT id FROM handoffs WHERE from_agent_id = ? AND to_agent_id = ? AND summary = ? AND status IN ('pending', 'accepted') LIMIT 1"

    case ProjectFactory.query(project_id, dedup_sql, [from_agent_id, to_agent_id, summary]) do
      {:ok, r} when r.rows != [] ->
        [existing_id | _] = hd(r.rows)
        Logger.info("[Handoff] Dedup: existing active handoff found (#{existing_id}), skipping create: #{String.slice(summary, 0, 60)}")
        {:ok, existing_id}

      _ ->
        handoff_id = Ecto.UUID.generate()
        sql = "INSERT INTO handoffs (id, from_agent_id, to_agent_id, module_id, summary, status, expect_report, reported_up, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'pending', ?, 0, ?, ?)"

        case ProjectFactory.query(project_id, sql, [handoff_id, from_agent_id, to_agent_id, module_id, summary, expect_report, now_ms, now_ms]) do
          {:ok, _} ->
            Logger.info("[Handoff] Created: #{from_agent_id} → #{to_agent_id}: #{String.slice(summary, 0, 60)}")
            {:ok, handoff_id}

          {:error, reason} ->
            Logger.error("[Handoff] Failed to create: #{inspect(reason)}")
            {:error, reason}
        end
    end
  end

  @doc """
  Get pending handoffs for an agent (not yet accepted, not yet delivered as context).
  """
  def get_pending_handoffs(project_id, to_agent_id) do
    sql = "SELECT id, from_agent_id, to_agent_id, module_id, summary, status, expect_report, reported_up, created_at, updated_at FROM handoffs WHERE to_agent_id = ? AND status = 'pending' AND context_delivered = 0 ORDER BY created_at ASC"

    case ProjectFactory.query(project_id, sql, [to_agent_id]) do
      {:ok, r} -> Enum.map(r.rows, &row_to_handoff/1)
      {:error, _} -> []
    end
  end

  @doc """
  Get accepted handoffs for an agent (only those not yet delivered as context).
  """
  def get_accepted_handoffs(project_id, to_agent_id) do
    sql = "SELECT id, from_agent_id, to_agent_id, module_id, summary, status, expect_report, reported_up, created_at, updated_at FROM handoffs WHERE to_agent_id = ? AND status = 'accepted' AND context_delivered = 0 ORDER BY created_at ASC"

    case ProjectFactory.query(project_id, sql, [to_agent_id]) do
      {:ok, r} -> Enum.map(r.rows, &row_to_handoff/1)
      {:error, _} -> []
    end
  end

  @doc """
  Get ALL accepted handoffs for an agent (including already delivered ones).
  Used for coordinator self-check and status queries.
  """
  def get_all_accepted_handoffs(project_id, to_agent_id) do
    sql = "SELECT id, from_agent_id, to_agent_id, module_id, summary, status, expect_report, reported_up, created_at, updated_at FROM handoffs WHERE to_agent_id = ? AND status = 'accepted' ORDER BY created_at ASC"

    case ProjectFactory.query(project_id, sql, [to_agent_id]) do
      {:ok, r} -> Enum.map(r.rows, &row_to_handoff/1)
      {:error, _} -> []
    end
  end

  @doc """
  Mark handoffs as context_delivered=1 (they've been included in a trigger context).
  Prevents re-injection on subsequent triggers.
  """
  def mark_delivered(project_id, handoff_ids) when is_list(handoff_ids) and handoff_ids != [] do
    now_ms = System.system_time(:millisecond)
    placeholders = Enum.map(handoff_ids, fn _ -> "?" end) |> Enum.join(",")
    sql = "UPDATE handoffs SET context_delivered = 1, updated_at = ? WHERE id IN (#{placeholders})"
    case ProjectFactory.query(project_id, sql, [now_ms | handoff_ids]) do
      {:ok, _} -> :ok
      {:error, _} -> :error
    end
  end
  def mark_delivered(_, _), do: :ok

  @doc """
  Accept all pending handoffs for an agent (pending → accepted).
  Returns count of accepted handoffs.
  """
  def accept_pending_handoffs(project_id, to_agent_id) do
    now_ms = System.system_time(:millisecond)
    sql = "UPDATE handoffs SET status = 'accepted', updated_at = ? WHERE to_agent_id = ? AND status = 'pending'"

    case ProjectFactory.query(project_id, sql, [now_ms, to_agent_id]) do
      {:ok, r} -> r.num_rows || 0
      {:error, _} -> 0
    end
  end

  @doc """
  Complete a handoff (accepted → completed).
  If handoff_id is nil, completes the most recent accepted handoff.
  """
  def complete_handoff(project_id, to_agent_id, handoff_id \\ nil) do
    now_ms = System.system_time(:millisecond)

    case handoff_id do
      nil ->
        sql = "UPDATE handoffs SET status = 'completed', updated_at = ? WHERE to_agent_id = ? AND status = 'accepted' ORDER BY created_at DESC LIMIT 1"
        case ProjectFactory.query(project_id, sql, [now_ms, to_agent_id]) do
          {:ok, r} ->
            count = r.num_rows || 0
            {:ok, %{completed: count > 0}}
          {:error, _} -> {:ok, %{completed: false}}
        end

      id ->
        sql = "UPDATE handoffs SET status = 'completed', updated_at = ? WHERE id = ? AND to_agent_id = ? AND status = 'accepted'"
        case ProjectFactory.query(project_id, sql, [now_ms, id, to_agent_id]) do
          {:ok, r} ->
            count = r.num_rows || 0
            {:ok, %{completed: count > 0, handoff_id: id}}
          {:error, _} -> {:ok, %{completed: false}}
        end
    end
  end

  @doc """
  Get completed handoffs from a subordinate (for coordinator review).
  """
  def get_completed_from_subordinate(project_id, from_agent_id, to_agent_id, limit \\ 5) do
    sql = "SELECT id, from_agent_id, to_agent_id, module_id, summary, status, expect_report, reported_up, created_at, updated_at FROM handoffs WHERE from_agent_id = ? AND to_agent_id = ? AND status = 'completed' ORDER BY updated_at DESC LIMIT ?"

    case ProjectFactory.query(project_id, sql, [from_agent_id, to_agent_id, limit]) do
      {:ok, r} -> Enum.map(r.rows, &row_to_handoff/1)
      {:error, _} -> []
    end
  end

  @doc """
  Get all handoffs for an agent (both directions).
  """
  def get_handoffs_for_agent(project_id, agent_id, limit \\ 10) do
    sql = "SELECT id, from_agent_id, to_agent_id, module_id, summary, status, expect_report, reported_up, created_at, updated_at FROM handoffs WHERE from_agent_id = ? OR to_agent_id = ? ORDER BY created_at DESC LIMIT ?"

    case ProjectFactory.query(project_id, sql, [agent_id, agent_id, limit]) do
      {:ok, r} -> Enum.map(r.rows, &row_to_handoff/1)
      {:error, _} -> []
    end
  end

  @doc """
  Find accepted handoffs with expect_report=true and reported_up=false.
  Used by coordinators for self-check (should report up to superior).
  """
  def get_unreported_accepted_handoffs(project_id, to_agent_id) do
    sql = "SELECT id, from_agent_id, to_agent_id, module_id, summary, status, expect_report, reported_up, created_at, updated_at FROM handoffs WHERE to_agent_id = ? AND status = 'accepted' AND expect_report = 1 AND reported_up = 0 ORDER BY created_at ASC"

    case ProjectFactory.query(project_id, sql, [to_agent_id]) do
      {:ok, r} -> Enum.map(r.rows, &row_to_handoff/1)
      {:error, _} -> []
    end
  end

  @doc """
  Mark handoffs as reported up (reported_up = true).
  Called after agent uses message_superior.
  """
  def mark_reported_up(project_id, to_agent_id) do
    now_ms = System.system_time(:millisecond)
    sql = "UPDATE handoffs SET reported_up = 1, updated_at = ? WHERE to_agent_id = ? AND expect_report = 1 AND reported_up = 0"

    case ProjectFactory.query(project_id, sql, [now_ms, to_agent_id]) do
      {:ok, r} -> r.num_rows || 0
      {:error, _} -> 0
    end
  end

  @doc """
  Get latest completed handoff from a subordinate.
  """
  def get_latest_completed(project_id, from_agent_id, to_agent_id) do
    sql = "SELECT id, from_agent_id, to_agent_id, module_id, summary, status, expect_report, reported_up, created_at, updated_at FROM handoffs WHERE from_agent_id = ? AND to_agent_id = ? AND status = 'completed' ORDER BY updated_at DESC LIMIT 1"

    case ProjectFactory.query(project_id, sql, [from_agent_id, to_agent_id]) do
      {:ok, r} ->
        case r.rows do
          [row | _] -> row_to_handoff(row)
          [] -> nil
        end
      {:error, _} -> nil
    end
  end

  @doc """
  Approve a handoff (completed → approved, terminal state).
  """
  def approve_handoff(project_id, from_agent_id, to_agent_id) do
    now_ms = System.system_time(:millisecond)
    sql = "UPDATE handoffs SET status = 'approved', updated_at = ? WHERE from_agent_id = ? AND to_agent_id = ? AND status = 'completed' ORDER BY updated_at DESC LIMIT 1"

    case ProjectFactory.query(project_id, sql, [now_ms, from_agent_id, to_agent_id]) do
      {:ok, r} ->
        count = r.num_rows || 0
        {:ok, %{approved: count > 0}}
      {:error, _} -> {:ok, %{approved: false}}
    end
  end

  @doc """
  Reopen a handoff (completed → accepted, for rework).
  Resets context_delivered so the rework task gets re-injected into the agent's context.
  """
  def reopen_handoff(project_id, from_agent_id, to_agent_id) do
    now_ms = System.system_time(:millisecond)
    sql = "UPDATE handoffs SET status = 'accepted', context_delivered = 0, updated_at = ? WHERE from_agent_id = ? AND to_agent_id = ? AND status = 'completed' ORDER BY updated_at DESC LIMIT 1"

    case ProjectFactory.query(project_id, sql, [now_ms, from_agent_id, to_agent_id]) do
      {:ok, r} ->
        count = r.num_rows || 0
        {:ok, %{reopened: count > 0}}
      {:error, _} -> {:ok, %{reopened: false}}
    end
  end

  # ── Helpers ─────────────────────────────────────────────────

  defp row_to_handoff([id, from_agent_id, to_agent_id, module_id, summary, status, expect_report, reported_up, created_at, updated_at]) do
    %{
      id: id,
      from_agent_id: from_agent_id,
      to_agent_id: to_agent_id,
      module_id: module_id,
      summary: summary,
      status: status,
      expect_report: expect_report == 1,
      reported_up: reported_up == 1,
      created_at: created_at,
      updated_at: updated_at
    }
  end
end
