defmodule HiveWeave.Services.Inbox do
  @moduledoc """
  Inbox service - message delivery between agents.

  Uses per-project SQLite databases (not the Meta DB).
  All queries route through ProjectFactory.

  Supports priority (low/normal/urgent), expect_report flag,
  and message_type (superior/peer/alarm) for inter-agent communication.
  """

  alias HiveWeave.Repo.ProjectFactory

  require Logger

  @doc """
  Send a message to an agent's inbox.

  Options:
    - priority: "low" | "normal" | "urgent" (default: "normal")
    - expect_report: boolean (default: false)
    - message_type: "superior" | "peer" | "alarm" (default: "superior")
  """
  def send_message(from_agent_id, to_agent_id, message_type, content, opts \\ %{}) do
    id = Ecto.UUID.generate()
    now = System.system_time(:millisecond)
    mtype = to_string(message_type)
    expect_report = if opts[:expect_report], do: 1, else: 0
    priority = opts[:priority] || "normal"

    sql = """
    INSERT INTO inbox (id, from_agent_id, to_agent_id, message, read, created_at, message_type, expect_report, priority)
    VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)
    """

    case ProjectFactory.query_for_agent(to_agent_id, sql, [
           id, from_agent_id, to_agent_id, to_string(content), now, mtype, expect_report, priority
         ]) do
      {:ok, _} ->
        broadcast_inbox_update(to_agent_id, %{
          id: id,
          from_agent_id: from_agent_id,
          to_agent_id: to_agent_id,
          message: content,
          type: mtype,
          priority: priority,
          expect_report: expect_report == 1,
          created_at: now
        })

        Logger.info("[Inbox] #{from_agent_id} → #{to_agent_id} (type=#{mtype}, priority=#{priority}): #{String.slice(to_string(content), 0, 80)}")

        {:ok, %{id: id, from_agent_id: from_agent_id, to_agent_id: to_agent_id, type: mtype, content: content}}

      error ->
        Logger.error("[Inbox] Failed to send: #{inspect(error)}")
        error
    end
  rescue
    e ->
      Logger.error("[Inbox] send_message exception: #{inspect(e)}")
      {:error, e}
  end

  @doc """
  Get pending (unread) messages for an agent.
  Optionally filter by message_type.
  """
  def get_pending_messages(agent_id, opts \\ %{}) do
    limit = opts[:limit] || 50
    message_type = opts[:message_type]

    {sql, params} =
      case message_type do
        nil ->
          {"SELECT id, from_agent_id, to_agent_id, message, read, created_at, message_type, expect_report, priority FROM inbox WHERE to_agent_id = ? AND read = 0 ORDER BY created_at ASC LIMIT ?",
           [agent_id, limit]}

        mt ->
          {"SELECT id, from_agent_id, to_agent_id, message, read, created_at, message_type, expect_report, priority FROM inbox WHERE to_agent_id = ? AND read = 0 AND message_type = ? ORDER BY created_at ASC LIMIT ?",
           [agent_id, to_string(mt), limit]}
      end

    case ProjectFactory.query_for_agent(agent_id, sql, params) do
      {:ok, r} -> Enum.map(r.rows, &row_to_message/1)
      {:error, _} -> []
    end
  rescue
    _ -> []
  end

  @doc """
  Get all inbox messages for an agent (read and unread).
  """
  def get_inbox(agent_id, opts \\ %{}) do
    limit = opts[:limit] || 50

    case ProjectFactory.query_for_agent(
           agent_id,
           "SELECT id, from_agent_id, to_agent_id, message, read, created_at, message_type, expect_report, priority FROM inbox WHERE to_agent_id = ? ORDER BY created_at DESC LIMIT ?",
           [agent_id, limit]
         ) do
      {:ok, r} -> Enum.map(r.rows, &row_to_message/1)
      {:error, _} -> []
    end
  rescue
    _ -> []
  end

  @doc """
  Get unread count for an agent.
  """
  def get_unread_count(agent_id) do
    case ProjectFactory.query_for_agent(
           agent_id,
           "SELECT COUNT(*) FROM inbox WHERE to_agent_id = ? AND read = 0",
           [agent_id]
         ) do
      {:ok, r} ->
        case r.rows do
          [[count]] -> count
          _ -> 0
        end

      {:error, _} -> 0
    end
  rescue
    _ -> 0
  end

  @doc """
  Mark a specific message as read.
  """
  def mark_as_read(agent_id, message_id) do
    ProjectFactory.query_for_agent(
      agent_id,
      "UPDATE inbox SET read = 1 WHERE id = ?",
      [message_id]
    )
    :ok
  rescue
    _ -> :ok
  end

  @doc """
  Mark all unread messages as read for an agent.
  Optionally filter by message_type.
  """
  def mark_all_read(agent_id, message_type \\ nil) do
    {sql, params} =
      case message_type do
        nil -> {"UPDATE inbox SET read = 1 WHERE to_agent_id = ? AND read = 0", [agent_id]}
        mt -> {"UPDATE inbox SET read = 1 WHERE to_agent_id = ? AND read = 0 AND message_type = ?", [agent_id, to_string(mt)]}
      end

    ProjectFactory.query_for_agent(agent_id, sql, params)
    :ok
  rescue
    _ -> :ok
  end

  defp row_to_message([id, from_agent_id, to_agent_id, message, read, created_at, message_type, expect_report, priority]) do
    %{
      id: id,
      from_agent_id: from_agent_id,
      to_agent_id: to_agent_id,
      message: message,
      read: read == 1,
      created_at: created_at,
      message_type: message_type,
      expect_report: expect_report == 1,
      priority: priority || "normal"
    }
  end

  defp broadcast_inbox_update(agent_id, message) do
    Phoenix.PubSub.broadcast(
      HiveWeave.PubSub,
      "agent:#{agent_id}",
      {:inbox_update, message}
    )
  end
end
