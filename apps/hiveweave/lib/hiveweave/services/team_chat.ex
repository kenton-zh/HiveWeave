defmodule HiveWeave.Services.TeamChat do
  @moduledoc """
  TeamChatService — multi-agent group chat persistence and deduplication.

  Wraps ChatMessageService to record inter-agent team communications
  as 'team' role messages visible in the UI team comms panel.
  """

  alias HiveWeave.Repo.ProjectFactory

  require Logger

  @doc """
  Record a team chat message between agents.
  Deduplicates by (from, to, dedupe_key) within a time window.
  """
  def record_message(agent_id, from_agent_id, to_agent_id, content, _opts \\ %{}) do
    dedupe_key = build_dedupe_key(from_agent_id, to_agent_id, content)

    if is_duplicate?(agent_id, dedupe_key) do
      :duplicate
    else
      now_ms = System.system_time(:millisecond)
      id = Ecto.UUID.generate()

      attrs = %{
        id: id,
        agent_id: agent_id,
        role: "team",
        content: content,
        is_background: false,
        is_read: false,
        is_streaming: false,
        created_at: now_ms,
        team_from_agent_id: from_agent_id,
        team_to_agent_id: to_agent_id
      }

      case save_team_message(attrs) do
        {:ok, _} ->
          # Save dedupe key
          save_dedupe_key(agent_id, dedupe_key, now_ms)
          :ok

        {:error, reason} ->
          Logger.warning("[TeamChat] Failed to record message: #{inspect(reason)}")
          {:error, reason}
      end
    end
  end

  @doc """
  Get team chat history for a project/agent.
  """
  def get_history(agent_id, limit \\ 50) do
    sql = """
    SELECT id, agent_id, content, team_from_agent_id, team_to_agent_id, created_at
    FROM chat_messages
    WHERE role = 'team' AND agent_id = ?
    ORDER BY created_at DESC
    LIMIT ?
    """

    case ProjectFactory.query_for_agent(agent_id, sql, [agent_id, limit]) do
      {:ok, r} ->
        r.rows
        |> Enum.reverse()
        |> Enum.map(fn [id, aid, content, from_id, to_id, created] ->
          %{
            id: id, agent_id: aid, content: content,
            from_agent_id: from_id, to_agent_id: to_id,
            created_at: created
          }
        end)

      {:error, _} -> []
    end
  end

  # ── Save team message with team columns ────────────────────────

  defp save_team_message(attrs) do
    id = Map.get(attrs, :id)
    agent_id = Map.get(attrs, :agent_id)
    role = Map.get(attrs, :role, "team")
    content = Map.get(attrs, :content, "")
    is_background = if Map.get(attrs, :is_background, false), do: 1, else: 0
    is_read = if Map.get(attrs, :is_read, true), do: 1, else: 0
    is_streaming = if Map.get(attrs, :is_streaming, false), do: 1, else: 0
    created_at = Map.get(attrs, :created_at, System.system_time(:millisecond))
    team_from = Map.get(attrs, :team_from_agent_id, "")
    team_to = Map.get(attrs, :team_to_agent_id, "")

    sql = """
    INSERT INTO chat_messages (id, agent_id, role, content, tool_calls, is_background, is_read, is_streaming, team_from_agent_id, team_to_agent_id, created_at)
    VALUES (?, ?, ?, ?, '[]', ?, ?, ?, ?, ?, ?)
    """

    case ProjectFactory.query_for_agent(agent_id, sql, [
           id, agent_id, role, content, is_background, is_read, is_streaming, team_from, team_to, created_at
         ]) do
      {:ok, _} ->
        {:ok, %{id: id, role: role, content: content, created_at: created_at}}

      {:error, reason} ->
        {:error, reason}
    end
  rescue
    e -> {:error, e}
  end

  # ── Deduplication ──────────────────────────────────────

  @dedupe_window_ms 60_000  # 1 minute dedup window

  defp build_dedupe_key(from_id, to_id, content) do
    hash = :crypto.hash(:md5, "#{from_id}:#{to_id}:#{content}")
    Base.encode16(hash, case: :lower)
  end

  defp is_duplicate?(agent_id, key) do
    cutoff = System.system_time(:millisecond) - @dedupe_window_ms

    case ProjectFactory.query_for_agent(agent_id,
           "SELECT id FROM team_chat_dedupe WHERE agent_id = ? AND dedupe_key = ? AND created_at > ?",
           [agent_id, key, cutoff]) do
      {:ok, r} -> r.rows != []
      _ -> false
    end
  rescue
    _ -> false
  end

  defp save_dedupe_key(agent_id, key, now_ms) do
    id = Ecto.UUID.generate()
    ProjectFactory.query_for_agent(agent_id,
      "INSERT INTO team_chat_dedupe (id, agent_id, dedupe_key, created_at) VALUES (?, ?, ?, ?)",
      [id, agent_id, key, now_ms])
  rescue
    _ -> :ok
  end
end
