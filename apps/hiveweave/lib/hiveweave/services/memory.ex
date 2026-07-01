defmodule HiveWeave.Services.Memory do
  @moduledoc """
  Three-layer memory service: project / agent / archive.

  - project: Shared across all agents in a workspace (constitution)
  - agent: Private working memory for a single agent
  - archive: Frozen memories from dismissed agents, indexed by module_id

  All queries route to per-project DB via ProjectFactory.
  Uses ETS cache to avoid repeated DB hits on every LLM call.
  """

  alias HiveWeave.Repo.ProjectFactory

  require Logger

  @cache_table :agent_memory_cache
  @cache_ttl_ms 300_000       # 5 min TTL for agent-private memories
  @project_cache_ttl_ms 30_000  # 30s TTL for project constitution (changes frequently)

  # ── Cache helpers ──────────────────────────────────────

  defp ensure_cache_table do
    if :ets.whereis(@cache_table) == :undefined do
      :ets.new(@cache_table, [:set, :public, :named_table, read_concurrency: true])
    end
  rescue
    ArgumentError -> :ok
  end

  defp cache_get(project_id, kind, ttl_ms \\ @cache_ttl_ms) do
    ensure_cache_table()
    now = System.system_time(:millisecond)
    case :ets.lookup(@cache_table, {project_id, kind}) do
      [{^project_id, ^kind, data, expires}] when expires > now -> data
      _ -> nil
    end
  end

  defp cache_put(project_id, kind, data, ttl_ms \\ @cache_ttl_ms) do
    ensure_cache_table()
    expires = System.system_time(:millisecond) + ttl_ms
    :ets.insert(@cache_table, {project_id, kind, data, expires})
  end

  # Invalidate all cached memories for a project.
  # Also broadcasts via PubSub so other nodes/processes drop their cache.
  defp invalidate_cache(project_id) do
    ensure_cache_table()
    :ets.match_delete(@cache_table, {project_id, :_, :_, :_})
    Phoenix.PubSub.broadcast(HiveWeave.PubSub, "cache_inval:#{project_id}", {:memory_cache_inval, project_id})
  end

  # Subscribe to cache invalidation from other processes.
  # Called once per process that uses the memory cache.
  defp ensure_cache_subscription(project_id) do
    key = {:sub, project_id}
    unless :ets.whereis(@cache_table) != :undefined and :ets.lookup(@cache_table, key) != [] do
      ensure_cache_table()
      :ets.insert(@cache_table, {key, true})
      Phoenix.PubSub.subscribe(HiveWeave.PubSub, "cache_inval:#{project_id}")
    end
  rescue
    _ -> :ok
  end

  # Drain any pending cache invalidation messages from the mailbox.
  # Called at the top of build_agent_context to ensure fresh project data.
  defp drain_cache_inval do
    receive do
      {:memory_cache_inval, project_id} ->
        ensure_cache_table()
        :ets.match_delete(@cache_table, {project_id, :_, :_, :_})
        drain_cache_inval()
    after
      0 -> :ok
    end
  end

  @doc """
  Get all project-scope memories (shared constitution).
  Short TTL (30s) + PubSub invalidation on write.
  """
  def get_project_memories(project_id) do
    case cache_get(project_id, :project, @project_cache_ttl_ms) do
      nil ->
        sql = "SELECT id, agent_id, scope, module_id, type, content, source_agent_id, metadata, created_at, updated_at FROM memories WHERE scope = 'project' ORDER BY created_at ASC"
        result = case ProjectFactory.query(project_id, sql, []) do
          {:ok, r} -> Enum.map(r.rows, &row_to_memory/1)
          {:error, _} -> []
        end
        cache_put(project_id, :project, result, @project_cache_ttl_ms)
        result
      cached -> cached
    end
  end

  @doc """
  Get an agent's private memories.
  5 min TTL — only that agent writes to it, so changes are rare.
  """
  def get_agent_memories(project_id, agent_id) do
    case cache_get(project_id, {:agent, agent_id}, @cache_ttl_ms) do
      nil ->
        sql = "SELECT id, agent_id, scope, module_id, type, content, source_agent_id, metadata, created_at, updated_at FROM memories WHERE scope = 'agent' AND agent_id = ? ORDER BY created_at ASC"
        result = case ProjectFactory.query(project_id, sql, [agent_id]) do
          {:ok, r} -> Enum.map(r.rows, &row_to_memory/1)
          {:error, _} -> []
        end
        cache_put(project_id, {:agent, agent_id}, result, @cache_ttl_ms)
        result
      cached -> cached
    end
  end

  @doc """
  Get archived memories for a module (from predecessors).
  5 min TTL — archives are write-once.
  """
  def get_archived_memories(project_id, module_id) do
    case cache_get(project_id, {:archive, module_id}, @cache_ttl_ms) do
      nil ->
        sql = "SELECT id, agent_id, scope, module_id, type, content, source_agent_id, metadata, created_at, updated_at FROM memories WHERE scope = 'archive' AND module_id = ? ORDER BY created_at ASC"
        result = case ProjectFactory.query(project_id, sql, [module_id]) do
          {:ok, r} -> Enum.map(r.rows, &row_to_memory/1)
          {:error, _} -> []
        end
        cache_put(project_id, {:archive, module_id}, result, @cache_ttl_ms)
        result
      cached -> cached
    end
  end

  @doc """
  Write a new memory entry.
  Invalidates relevant cache on write.
  """
  def write_memory(project_id, opts) do
    id = Ecto.UUID.generate()
    now_ms = System.system_time(:millisecond)

    agent_id = Keyword.get(opts, :agent_id)
    scope = Keyword.get(opts, :scope, "agent")
    module_id = Keyword.get(opts, :module_id)
    type = Keyword.get(opts, :type, "fact")
    content = Keyword.get(opts, :content, "")
    source_agent_id = Keyword.get(opts, :source_agent_id)
    metadata = Keyword.get(opts, :metadata, %{}) |> ensure_json()

    sql = "INSERT INTO memories (id, agent_id, scope, module_id, type, content, source_agent_id, metadata, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"

    case ProjectFactory.query(project_id, sql, [id, agent_id, scope, module_id, type, content, source_agent_id, metadata, now_ms, now_ms]) do
      {:ok, _} ->
        Logger.info("[Memory] Saved: scope=#{scope}, type=#{type}, agent=#{agent_id || "none"}: #{String.slice(content, 0, 80)}")
        invalidate_cache(project_id)
        {:ok, id}

      {:error, reason} ->
        Logger.error("[Memory] Failed to save: #{inspect(reason)}")
        {:error, reason}
    end
  end

  @doc """
  Archive an agent's private memories (scope: agent → archive).
  Called when an agent is dismissed/deleted.
  """
  def archive_agent_memories(project_id, agent_id) do
    now_ms = System.system_time(:millisecond)
    sql = "UPDATE memories SET scope = 'archive', updated_at = ? WHERE agent_id = ? AND scope = 'agent'"

    case ProjectFactory.query(project_id, sql, [now_ms, agent_id]) do
      {:ok, r} ->
        count = r.num_rows || 0
        Logger.info("[Memory] Archived #{count} memories for agent #{agent_id}")
        invalidate_cache(project_id)
        count

      {:error, _} -> 0
    end
  end

  @doc """
  Build agent context string from memories.
  Injected into system prompt so agent has access to:
  1. Project constitution (shared)
  2. Private working memory
  3. Archived memories from predecessors (if module_id is set)
  """
  def build_agent_context(project_id, agent_id, module_id \\ nil) do
    # Drain any pending cache invalidation messages before reading
    drain_cache_inval()

    # Project memories (30s TTL + PubSub invalidation)
    project_mems = get_project_memories(project_id)
    project_block = if project_mems != [] do
      items = Enum.map(project_mems, fn m ->
        "- [#{m.type}] #{truncate(m.content, 200)}"
      end) |> Enum.join("\n")
      "## Project Constitution (Shared)\n#{items}"
    else
      nil
    end

    # Agent private memories
    agent_mems = get_agent_memories(project_id, agent_id)
    agent_block = if agent_mems != [] do
      items = Enum.map(agent_mems, fn m ->
        "- [#{m.type}] #{truncate(m.content, 200)}"
      end) |> Enum.join("\n")
      "## Your Private Working Memory\n#{items}"
    else
      nil
    end

    # Archived memories (only if module_id is set)
    archive_block = if module_id do
      archived = get_archived_memories(project_id, module_id)
      if archived != [] do
        items = Enum.map(archived, fn m ->
          "- [#{m.type}] #{truncate(m.content, 200)}"
        end) |> Enum.join("\n")
        "## Archived Memories (from predecessors on this module)\n#{items}"
      else
        nil
      end
    else
      nil
    end

    blocks = [project_block, agent_block, archive_block] |> Enum.reject(&is_nil/1)

    if blocks == [] do
      nil
    else
      Enum.join(blocks, "\n\n")
    end
  end

  # ── Helpers ─────────────────────────────────────────────────

  defp row_to_memory([id, agent_id, scope, module_id, type, content, source_agent_id, metadata, created_at, updated_at]) do
    %{
      id: id,
      agent_id: agent_id,
      scope: scope,
      module_id: module_id,
      type: type,
      content: content,
      source_agent_id: source_agent_id,
      metadata: parse_json(metadata),
      created_at: created_at,
      updated_at: updated_at
    }
  end

  defp truncate(str, len) when is_binary(str) do
    if String.length(str) > len do
      String.slice(str, 0, len) <> "..."
    else
      str
    end
  end
  defp truncate(_, _), do: ""

  defp ensure_json(map) when is_map(map), do: Jason.encode!(map)
  defp ensure_json(str) when is_binary(str), do: str
  defp ensure_json(_), do: "{}"

  defp parse_json(nil), do: %{}
  defp parse_json(str) when is_binary(str) do
    case Jason.decode(str) do
      {:ok, map} -> map
      _ -> %{}
    end
  end
end
