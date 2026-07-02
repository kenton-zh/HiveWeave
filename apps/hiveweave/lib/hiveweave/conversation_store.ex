defmodule HiveWeave.ConversationStore do
  @moduledoc """
  Per-agent persistent conversation history with smart compaction.

  Design (mirrors the TS version):
  1. Token-budget trimming (not message count)
  2. Persistent sessions (survive server restarts) — stored in per-project DB
  3. Lazy loading from DB with in-memory cache
  4. Turn-level trimming (never splits assistant(tool_calls) / tool(result) pairs)
  5. Smart compaction via LLM — summarizes old turns, keeps recent intact
  6. System messages filtered from history (rebuilt by streamer each time)
  7. Orphaned tool results removed (no matching tool_call_id)

  All DB access routes through ProjectFactory.query_for_agent/3 to hit the
  correct per-project SQLite database.
  """

  use GenServer

  alias HiveWeave.Repo.ProjectFactory
  alias HiveWeave.TokenUtils

  require Logger

  @compaction_buffer 20_000
  @preserve_recent_min 10
  @preserve_recent_max 30
  @compaction_trigger_ratio 0.85  # Compact when total > 85% of budget
  @doom_loop_threshold 3

  defstruct [:cache]

  # ── Client API ──────────────────────────────────────────────

  def start_link(opts) do
    GenServer.start_link(__MODULE__, opts, name: __MODULE__)
  end

  def get_history(agent_id, project_id, token_budget \\ nil) do
    GenServer.call(__MODULE__, {:get_history, agent_id, project_id, token_budget})
  end

  def append_turn(agent_id, project_id, messages) do
    GenServer.call(__MODULE__, {:append_turn, agent_id, project_id, messages})
  end

  def clear(agent_id, project_id) do
    GenServer.call(__MODULE__, {:clear, agent_id, project_id})
  end

  def clear_all do
    GenServer.call(__MODULE__, :clear_all)
  end

  @doc """
  Check and trigger compaction when switching from an old model to a new one.
  Called before each agent turn. Returns :compacted or :ok.
  """
  def maybe_compact_on_model_switch(agent_id, project_id, opts) do
    old_ctx = opts[:old_context_window] || 128_000
    new_ctx = opts[:new_context_window] || 128_000
    current_tokens = opts[:current_tokens] || 0

    case HiveWeave.Compaction.Overflow.check_model_switch(old_ctx, new_ctx, current_tokens) do
      nil -> :ok
      budget ->
        Logger.info("[ConversationStore] Model switch: #{old_ctx} -> #{new_ctx} context, #{current_tokens} tokens -> compacting to #{budget}")
        GenServer.call(__MODULE__, {:compact_now, agent_id, project_id, budget})
    end
  end

  # ── GenServer callbacks ─────────────────────────────────────

  @impl true
  def init(_opts) do
    {:ok, %__MODULE__{cache: %{}}}
  end

  @impl true
  def handle_call({:get_history, agent_id, project_id, token_budget}, _from, state) do
    key = {project_id, agent_id}

    messages =
      case Map.get(state.cache, key) do
        nil ->
          loaded = load_from_db(agent_id) |> clean_messages()
          trimmed = trim_to_budget(loaded, token_budget)
          trimmed

        cached ->
          clean = clean_messages(cached)
          trim_to_budget(clean, token_budget)
      end

    {:reply, messages, %{state | cache: Map.put(state.cache, key, messages)}}
  end

  @impl true
  def handle_call({:append_turn, agent_id, project_id, messages}, _from, state)
      when is_list(messages) and length(messages) > 0 do
    key = {project_id, agent_id}

    existing =
      case Map.get(state.cache, key) do
        nil -> load_from_db(agent_id) |> clean_messages()
        cached -> clean_messages(cached)
      end

    # Filter out system messages before saving (they're rebuilt by streamer)
    filtered_new = Enum.reject(messages, fn m -> m["role"] == "system" end)
    combined = existing ++ filtered_new

    # Persist the new turn to per-project DB (async)
    Task.start(fn ->
      persist_turn(agent_id, filtered_new)
    end)

    # Update cache
    new_cache = Map.put(state.cache, key, combined)
    new_state = %{state | cache: new_cache}

    # Trigger async compaction if needed
    maybe_trigger_compaction(agent_id, project_id, key, combined)

    {:reply, :ok, new_state}
  end

  @impl true
  def handle_call({:append_turn, _agent_id, _project_id, _messages}, _from, state) do
    {:reply, :ok, state}
  end

  @impl true
  def handle_call({:compact_now, agent_id, project_id, budget}, _from, state) do
    key = {project_id, agent_id}
    existing = case Map.get(state.cache, key) do
      nil -> load_from_db(agent_id) |> clean_messages()
      cached -> clean_messages(cached)
    end
    trimmed = trim_to_budget(existing, budget)
    new_cache = Map.put(state.cache, key, trimmed)
    {:reply, :compacted, %{state | cache: new_cache}}
  end

  @impl true
  def handle_call({:clear, agent_id, project_id}, _from, state) do
    key = {project_id, agent_id}
    new_cache = Map.delete(state.cache, key)

    Task.start(fn ->
      ProjectFactory.query_for_agent(
        agent_id,
        "DELETE FROM conversation_turns WHERE agent_id = ?",
        [agent_id]
      )
    end)

    {:reply, :ok, %{state | cache: new_cache}}
  end

  @impl true
  def handle_call(:clear_all, _from, state) do
    {:reply, :ok, %{state | cache: %{}}}
  end

  @impl true
  def handle_info({:compaction_done, key, compacted_messages}, state) do
    Logger.info("[ConversationStore] Compaction done for #{inspect(key)}: #{length(compacted_messages)} messages")
    {:noreply, %{state | cache: Map.put(state.cache, key, compacted_messages)}}
  end

  @impl true
  def handle_info(_msg, state) do
    {:noreply, state}
  end

  # ── Compaction ──────────────────────────────────────────────

  defp maybe_trigger_compaction(agent_id, project_id, key, messages) do
    total = estimate_total_tokens(messages)

    # Get agent's model context window (default 128000), subtract compaction buffer
    model_ctx = get_agent_context_window(agent_id)
    budget = model_ctx - @compaction_buffer

    if total > budget * @compaction_trigger_ratio do
      Logger.info("[ConversationStore] Triggering compaction for agent #{agent_id} (#{total} tokens > #{trunc(budget * @compaction_trigger_ratio)})")

      Task.start(fn ->
        compacted = do_compaction(agent_id, project_id, messages, budget)
        send(__MODULE__, {:compaction_done, key, compacted})
      end)
    end
  end

  defp do_compaction(agent_id, _project_id, messages, budget) do
    # Determine split point — keep recent messages intact
    recent_count = min(@preserve_recent_max, max(@preserve_recent_min, div(length(messages), 3)))
    split_idx = max(0, length(messages) - recent_count)

    {old_messages, recent_messages} = Enum.split(messages, split_idx)

    if old_messages == [] do
      # Nothing to compact, just trim
      trim_to_budget(messages, budget)
    else
      # Try LLM summarization
      case call_compactor_llm(agent_id, old_messages) do
        {:ok, summary} ->
          Logger.info("[ConversationStore] Compaction complete for agent #{agent_id}: #{length(old_messages)} old messages summarized into #{String.length(summary)} chars")
          summary_msg = %{
            "role" => "system",
            "content" => "## Previous Conversation Summary\n\n#{summary}\n\n---\nBelow is the recent conversation:"
          }
          [summary_msg | recent_messages]

        {:error, reason} ->
          Logger.warning("[ConversationStore] LLM compaction failed (#{inspect(reason)}), falling back to trim")
          trim_to_budget(messages, budget)
      end
    end
  end

  defp get_agent_context_window(agent_id) do
    case Ecto.Adapters.SQL.query(HiveWeave.Repo.Meta,
      "SELECT a.model_id, m.context_window FROM agents a LEFT JOIN llm_models m ON a.model_id = m.id WHERE a.id = ? LIMIT 1",
      [agent_id]) do
      {:ok, %{rows: [[_, ctx] | _]}} when not is_nil(ctx) and ctx > 0 -> ctx
      _ -> 128_000
    end
  rescue
    _ -> 128_000
  end

  defp call_compactor_llm(agent_id, old_messages) do
    model = resolve_compactor_model(agent_id)

    if model == nil or model.base_url == "" do
      {:error, :no_model_available}
    else
      # Build conversation text for summarization
      conversation_text = format_messages_for_summary(old_messages)

      prompt = """
      Summarize the following conversation history concisely. Preserve:
      - Key decisions and their rationale
      - Important facts learned about the codebase or project
      - Task progress and current state
      - Any unresolved issues or open questions

      Conversation to summarize:
      #{conversation_text}

      Summary:
      """

      body = %{
        "model" => model.model_id,
        "messages" => [%{"role" => "user", "content" => prompt}],
        "temperature" => 0.3,
        "max_tokens" => 2000
      }

      url = build_url(model.base_url)

      headers = [
        {"content-type", "application/json"},
        {"authorization", "Bearer #{model.api_key}"}
      ]

      case Req.post(url,
             headers: headers,
             body: Jason.encode!(body),
             receive_timeout: 30_000
           ) do
        {:ok, %{status: 200, body: resp_body}} ->
          decoded = if is_binary(resp_body), do: Jason.decode!(resp_body), else: resp_body
          summary = extract_content(decoded)
          if summary && summary != "" do
            {:ok, summary}
          else
            {:error, :empty_response}
          end

        {:ok, %{status: status, body: resp_body}} ->
          {:error, {:http_error, status, resp_body}}

        {:error, reason} ->
          {:error, reason}
      end
    end
  rescue
    e -> {:error, e}
  end

  defp resolve_compactor_model(agent_id) do
    # Try to get the agent's model, fall back to first active model
    case ProjectFactory.resolve_project(agent_id) do
      {:ok, _project_id} ->
        # Get agent's model_id from meta DB
        {:ok, r} = Ecto.Adapters.SQL.query(
          HiveWeave.Repo.Meta,
          "SELECT a.model_id FROM agents a WHERE a.id = ? LIMIT 1",
          [agent_id]
        )

        model_id = case r.rows do
          [[id | _]] when not is_nil(id) -> id
          _ -> nil
        end

        resolved = if model_id do
          fetch_model_by_id(model_id)
        else
          nil
        end

        resolved || fetch_first_active_model()

      {:error, _} ->
        fetch_first_active_model()
    end
  rescue
    _ -> fetch_first_active_model()
  end

  defp fetch_model_by_id(id) do
    {:ok, r} = Ecto.Adapters.SQL.query(
      HiveWeave.Repo.Meta,
      "SELECT id, name, model_id, base_url, api_key, context_window FROM llm_models WHERE id = ? LIMIT 1",
      [id]
    )

    case r.rows do
      [row] -> row_to_model(row)
      _ -> nil
    end
  rescue
    _ -> nil
  end

  defp fetch_first_active_model do
    {:ok, r} = Ecto.Adapters.SQL.query(
      HiveWeave.Repo.Meta,
      "SELECT id, name, model_id, base_url, api_key, context_window FROM llm_models WHERE is_active = 1 ORDER BY created_at ASC LIMIT 1",
      []
    )

    case r.rows do
      [row] -> row_to_model(row)
      _ -> nil
    end
  rescue
    _ -> nil
  end

  defp row_to_model([_id, _name, model_id, base_url, api_key, _ctx]) do
    %{
      model_id: model_id,
      base_url: base_url || "",
      api_key: api_key || ""
    }
  end

  defp build_url(base_url) do
    base = String.trim_trailing(base_url, "/")
    "#{base}/chat/completions"
  end

  defp extract_content(%{"choices" => [%{"message" => %{"content" => content}} | _]}), do: content
  defp extract_content(_), do: nil

  defp format_messages_for_summary(messages) do
    Enum.map(messages, fn m ->
      role = m["role"] || "unknown"
      content = safe_content(m["content"])

      # Truncate very long content
      truncated = if String.length(content) > 500 do
        String.slice(content, 0, 500) <> "...[truncated]"
      else
        content
      end

      "[#{role}]: #{truncated}"
    end)
    |> Enum.join("\n\n")
  end

  # ── Message cleaning ────────────────────────────────────────

  # Normalize content to a binary string (multimodal content can be a list)
  defp safe_content(nil), do: ""
  defp safe_content(s) when is_binary(s), do: s
  defp safe_content(list) when is_list(list) do
    Enum.map_join(list, "", fn
      %{"text" => t} when is_binary(t) -> t
      t when is_binary(t) -> t
      _ -> ""
    end)
  end
  defp safe_content(other), do: to_string(other)

  defp clean_messages(messages) do
    messages
    |> Enum.reject(fn m -> m["role"] == "system" end)  # System msgs rebuilt by streamer
    |> remove_orphaned_tool_results()
  end

  defp remove_orphaned_tool_results(messages) do
    # Collect all tool_call_ids from assistant messages
    tool_call_ids =
      messages
      |> Enum.filter(&Map.has_key?(&1, "tool_calls"))
      |> Enum.flat_map(fn m ->
        (m["tool_calls"] || [])
        |> Enum.map(& &1["id"])
      end)
      |> Enum.filter(&(&1 != nil))
      |> MapSet.new()

    # Keep tool results only if their tool_call_id is in the set
    Enum.filter(messages, fn m ->
      if Map.has_key?(m, "tool_call_id") do
        MapSet.member?(tool_call_ids, m["tool_call_id"])
      else
        true
      end
    end)
  end

  # ── DB persistence ──────────────────────────────────────────

  defp load_from_db(agent_id) do
    case ProjectFactory.query_for_agent(
           agent_id,
           "SELECT id, agent_id, turn_index, raw_messages, approx_tokens, created_at FROM conversation_turns WHERE agent_id = ? ORDER BY turn_index ASC",
           [agent_id]
         ) do
      {:ok, r} ->
        r.rows
        |> Enum.flat_map(fn row ->
          [_id, _agent_id, _turn_index, raw_messages, _tokens, _created_at] = row

          case Jason.decode(raw_messages || "[]") do
            {:ok, msgs} when is_list(msgs) -> msgs
            _ -> []
          end
        end)

      {:error, reason} ->
        Logger.warning("ConversationStore.load_from_db failed for agent #{agent_id}: #{inspect(reason)}")
        []
    end
  rescue
    e ->
      Logger.warning("ConversationStore.load_from_db exception: #{inspect(e)}")
      []
  end

  defp persist_turn(agent_id, messages) do
    id = Ecto.UUID.generate()
    now = System.system_time(:millisecond)
    raw = Jason.encode!(messages)
    tokens = estimate_total_tokens(messages)

    turn_index =
      case ProjectFactory.query_for_agent(
             agent_id,
             "SELECT MAX(turn_index) FROM conversation_turns WHERE agent_id = ?",
             [agent_id]
           ) do
        {:ok, %{rows: [[nil] | _]}} -> 0
        {:ok, %{rows: [[max_idx | _] | _]}} -> (max_idx || 0) + 1
        _ -> 0
      end

    case ProjectFactory.query_for_agent(
           agent_id,
           "INSERT INTO conversation_turns (id, agent_id, role, turn_index, raw_messages, approx_tokens, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
           [id, agent_id, "turn", turn_index, raw, tokens, now]
         ) do
      {:ok, _} -> :ok
      {:error, reason} -> Logger.warning("Failed to persist turn: #{inspect(reason)}")
    end
  rescue
    e -> Logger.warning("persist_turn exception: #{inspect(e)}")
  end

  # ── Token budget trimming ───────────────────────────────────

  defp estimate_total_tokens(messages) do
    Enum.reduce(messages, 0, fn m, acc ->
      content_tokens = TokenUtils.estimate_tokens(m["content"] || "")
      tool_tokens = estimate_tool_tokens(m)
      acc + content_tokens + tool_tokens
    end)
  end

  defp estimate_tool_tokens(m) do
    cond do
      Map.has_key?(m, "tool_calls") ->
        Enum.reduce(m["tool_calls"] || [], 0, fn tc, acc ->
          acc + TokenUtils.estimate_tokens(tc["function"]["arguments"] || "")
        end)

      Map.has_key?(m, "tool_call_id") ->
        TokenUtils.estimate_tokens(m["content"] || "")

      true ->
        0
    end
  end

  defp trim_to_budget(messages, nil), do: messages

  defp trim_to_budget(messages, budget) when is_integer(budget) and budget > 0 do
    total = estimate_total_tokens(messages)

    if total <= budget do
      messages
    else
      do_trim(messages, budget, total)
    end
  end

  defp trim_to_budget(messages, _), do: messages

  defp do_trim([], _budget, _total), do: []

  defp do_trim(messages, budget, total) do
    [first | rest] = messages
    first_tokens = estimate_message_tokens(first)

    # Check if dropping this message would split a tool_calls/tool_result pair
    is_tool_call = Map.has_key?(first, "tool_calls")
    is_tool_result = Map.has_key?(first, "tool_call_id")

    {dropped_tokens, remaining} =
      cond do
        is_tool_call and length(rest) > 0 and Map.has_key?(hd(rest), "tool_call_id") ->
          # Drop tool call + tool result together
          second = hd(rest)
          {first_tokens + estimate_message_tokens(second), tl(rest)}

        is_tool_result and length(rest) > 0 and Map.has_key?(hd(rest), "tool_calls") ->
          # Edge case: tool result before tool call (shouldn't happen)
          second = hd(rest)
          {first_tokens + estimate_message_tokens(second), tl(rest)}

        true ->
          {first_tokens, rest}
      end

    new_total = total - dropped_tokens

    if new_total <= budget do
      remaining
    else
      do_trim(remaining, budget, new_total)
    end
  end

  defp estimate_message_tokens(m) do
    TokenUtils.estimate_tokens(m["content"] || "") + estimate_tool_tokens(m)
  end

  # ── Doom loop detection ──────────────────────────────────────

  @doc """
  Detect if the last N tool calls are identical (same tool + same args).
  Returns {:doom_loop, tool_name} if detected, otherwise :ok.
  """
  def detect_doom_loop(messages) when is_list(messages) do
    tool_calls =
      messages
      |> Enum.filter(fn m -> Map.has_key?(m, "tool_calls") end)
      |> Enum.flat_map(fn m ->
        (m["tool_calls"] || [])
        |> Enum.map(fn tc -> %{
          name: tc["function"]["name"],
          args: tc["function"]["arguments"]
        } end)
      end)
      |> Enum.take(-@doom_loop_threshold)

    if length(tool_calls) == @doom_loop_threshold do
      first = hd(tool_calls)
      all_same = Enum.all?(tool_calls, fn tc ->
        tc.name == first.name and tc.args == first.args
      end)
      if all_same, do: {:doom_loop, first.name}, else: :ok
    else
      :ok
    end
  end
end
