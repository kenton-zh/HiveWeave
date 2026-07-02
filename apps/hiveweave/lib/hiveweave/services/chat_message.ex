defmodule HiveWeave.Services.ChatMessage do
  @moduledoc """
  Chat message persistence service.

  Uses per-project SQLite databases (not the Meta DB).
  All queries are routed through ProjectFactory.query_for_agent/3,
  which resolves the agent's project_id and uses the correct
  per-project Ecto Repo instance.
  """
  alias HiveWeave.Repo.ProjectFactory

  require Logger

  @doc """
  Save a chat message.

  `attrs` is a map. Supported keys: id, agent_id, role, content,
  tool_calls, is_background, is_read, created_at, is_streaming, images,
  team_from_agent_id, team_to_agent_id. Unknown keys are ignored; missing
  keys fall back to defaults.
  """
  def save_message(attrs) do
    id = attrs[:id] || attrs["id"] || Ecto.UUID.generate()
    agent_id = attrs[:agent_id] || attrs["agent_id"]
    role = attrs[:role] || attrs["role"] || "assistant"
    content = attrs[:content] || attrs["content"] || ""
    tool_calls = attrs[:tool_calls] || attrs["tool_calls"] || "[]"
    is_background = to_int(attrs[:is_background] || attrs["is_background"], false)
    is_read = to_int(attrs[:is_read] || attrs["is_read"], true)
    created_at = attrs[:created_at] || attrs["created_at"] || System.system_time(:millisecond)
    is_streaming = to_int(attrs[:is_streaming] || attrs["is_streaming"], false)
    is_context = to_int(attrs[:is_context] || attrs["is_context"], false)

    sql = """
    INSERT INTO chat_messages (id, agent_id, role, content, tool_calls, is_background, is_read, is_streaming, is_context, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    case ProjectFactory.query_for_agent(agent_id, sql, [
           id, agent_id, role, content, tool_calls, is_background, is_read, is_streaming, is_context, created_at
         ]) do
      {:ok, _} ->
        {:ok, %{id: id, role: role, content: content, created_at: created_at}}

      {:error, reason} ->
        {:error, reason}
    end
  rescue
    e -> {:error, e}
  end

  defp to_int(v, _) when is_integer(v), do: v
  defp to_int(v, _) when is_boolean(v), do: if(v, do: 1, else: 0)
  defp to_int(nil, default), do: if(default, do: 1, else: 0)
  defp to_int(_, default), do: if(default, do: 1, else: 0)

  @doc """
  Update an existing message.
  """
  def update_message(agent_id, id, attrs) do
    updates = []

    updates =
      if Map.has_key?(attrs, :content) or Map.has_key?(attrs, "content") do
        updates ++ [{"content", attrs[:content] || attrs["content"]}]
      else
        updates
      end

    updates =
      if Map.has_key?(attrs, :is_read) or Map.has_key?(attrs, "is_read") do
        updates ++ [{"is_read", to_int(attrs[:is_read] || attrs["is_read"], true)}]
      else
        updates
      end

    updates =
      if Map.has_key?(attrs, :is_streaming) or Map.has_key?(attrs, "is_streaming") do
        updates ++ [{"is_streaming", to_int(attrs[:is_streaming] || attrs["is_streaming"], false)}]
      else
        updates
      end

    updates =
      if Map.has_key?(attrs, :tool_calls) or Map.has_key?(attrs, "tool_calls") do
        updates ++ [{"tool_calls", attrs[:tool_calls] || attrs["tool_calls"]}]
      else
        updates
      end

    case updates do
      [] ->
        {:ok, %{id: id}}

      _ ->
        set_clauses = updates |> Enum.map(fn {col, _} -> "#{col} = ?" end) |> Enum.join(", ")
        values = Enum.map(updates, fn {_, v} -> v end) ++ [id]

        case ProjectFactory.query_for_agent(
               agent_id,
               "UPDATE chat_messages SET #{set_clauses} WHERE id = ?",
               values
             ) do
          {:ok, _} -> {:ok, %{id: id}}
          {:error, reason} -> {:error, reason}
        end
    end
  rescue
    e -> {:error, e}
  end

  @doc """
  Get recent messages for an agent.
  Returns a list of maps. Uses DESC+reverse to get the newest N messages
  in chronological order (same fix as the TS version).
  """
  def get_messages(agent_id, limit \\ 200) do
    case ProjectFactory.query_for_agent(
           agent_id,
            "SELECT id, agent_id, role, content, tool_calls, is_background, is_read, is_streaming, is_context, created_at FROM chat_messages WHERE agent_id = ? ORDER BY created_at DESC LIMIT ?",
           [agent_id, limit]
         ) do
      {:ok, r} ->
        r.rows
        |> Enum.map(fn row -> Enum.zip(r.columns, row) |> Enum.into(%{}) end)
        |> Enum.reverse()

      {:error, _} ->
        []
    end
  rescue
    _ -> []
  end

  @doc """
  Mark messages as read.
  """
  def mark_as_read(agent_id, ids) when is_list(ids) do
    if length(ids) > 0 do
      placeholders = Enum.map_join(1..length(ids), ",", fn _ -> "?" end)

      ProjectFactory.query_for_agent(
        agent_id,
        "UPDATE chat_messages SET is_read = 1 WHERE id IN (#{placeholders})",
        ids
      )

      length(ids)
    else
      0
    end
  rescue
    _ -> 0
  end

  @doc """
  Get unread background messages.
  """
  def get_unread_background(agent_id) do
    case ProjectFactory.query_for_agent(
           agent_id,
            "SELECT id, agent_id, role, content, tool_calls, is_background, is_read, is_streaming, is_context, created_at FROM chat_messages WHERE agent_id = ? AND is_background = 1 AND is_read = 0 ORDER BY created_at ASC",
           [agent_id]
         ) do
      {:ok, r} ->
        r.rows |> Enum.map(fn row -> Enum.zip(r.columns, row) |> Enum.into(%{}) end)

      {:error, _} ->
        []
    end
  rescue
    _ -> []
  end

  @doc """
  Clear stuck streaming flags (called on startup).
  Iterates all projects and clears is_streaming=1.
  """
  def clear_stuck_streaming do
    import Ecto.Query
    alias HiveWeave.Repo.Meta
    alias HiveWeave.Schema.Project

    projects = Meta.all(from p in Project, select: p.id)

    Enum.each(projects, fn project_id ->
      case ProjectFactory.query(
             project_id,
             "UPDATE chat_messages SET is_streaming = 0 WHERE is_streaming = 1",
             []
           ) do
        {:ok, _} -> :ok
        {:error, e} -> Logger.warning("clear_stuck_streaming for project #{project_id}: #{inspect(e)}")
      end
    end)

    :ok
  rescue
    e ->
      Logger.warning("clear_stuck_streaming failed: #{inspect(e)}")
      :ok
  end

  @doc """
  Check if there are unanswered user messages for an agent.

  An "unanswered user message" is a foreground (is_background=0) user message
  that has no subsequent assistant message. This is used to detect the case
  where a user sent a message while the agent was busy — the message was
  saved to DB but never processed.
  """
  def has_unanswered_user_messages?(agent_id) do
    sql = """
    SELECT EXISTS(
      SELECT 1 FROM chat_messages m1
      WHERE m1.agent_id = ?
        AND m1.role = 'user'
        AND m1.is_background = 0
        AND NOT EXISTS(
          SELECT 1 FROM chat_messages m2
          WHERE m2.agent_id = m1.agent_id
            AND m2.role = 'assistant'
            AND m2.is_background = 0
            AND m2.created_at > m1.created_at
        )
    )
    """

    case ProjectFactory.query_for_agent(agent_id, sql, [agent_id]) do
      {:ok, %{rows: [[1]]}} -> true
      {:ok, %{rows: [[0]]}} -> false
      {:ok, %{rows: [["true"]]}} -> true
      {:ok, %{rows: [["false"]]}} -> false
      _ -> false
    end
  rescue
    e ->
      Logger.warning("has_unanswered_user_messages? failed: #{inspect(e)}")
      false
  end
end
