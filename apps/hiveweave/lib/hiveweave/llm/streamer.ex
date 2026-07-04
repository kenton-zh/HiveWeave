defmodule HiveWeave.LLM.Streamer do
  @moduledoc """
  LLM streaming module with tool_calls support.

  Resolves an LLM model from the database (or env fallback), then makes a
  real OpenAI-compatible streaming HTTP request and forwards SSE chunks back
  to the parent GenServer in REAL-TIME (token-by-token).

  Key behaviors:
    - Creates a placeholder assistant message (is_streaming=1) before streaming
    - Broadcasts each chunk via PubSub as it arrives (not buffered)
    - Accumulates full text + reasoning + tool_calls
    - If LLM returns tool_calls → executes tools → sends results back → loops
    - Updates the assistant message with final content after all loops complete
    - Marks is_streaming=0 on both success and error

  Tool loop:
    1. Stream LLM response (text + tool_calls)
    2. If tool_calls present → execute each via ToolExecutor
    3. Append tool results to messages
    4. Re-stream with updated messages
    5. Repeat until no tool_calls or max 10 iterations
  """
  require Logger

  alias HiveWeave.Services.ChatMessage
  alias HiveWeave.ToolExecutor

  @request_timeout_ms 90_000
  # Turn-level idle timeout (watchdog for hung streams).
  # Like TS TURN_IDLE_MS — deliberately > bash tool timeout (120s) so legitimate
  # long commands aren't killed. If no chunks arrive for this duration, abort.
  @stream_idle_ms 300_000
  # Per-role tool round limits. CEO needs many rounds to analyze code +
  # write charter + message HR + coordinate. Executors need even more
  # for multi-file edits + testing + debugging.
  defp max_tool_rounds_for(role) when is_binary(role) do
    case String.downcase(role) do
      "ceo" -> 60
      "hr" -> 40
      "coordinator" -> 50
      "manager" -> 50
      _ -> 80  # executors, developers, etc.
    end
  end
  defp max_tool_rounds_for(_), do: 80

  # ── Agent cluster diagnostics ───────────────────────────────
  # Controlled by HIVEWEAVE_DIAG env var via :hiveweave, :diagnostics config.
  # When enabled, emits detailed logs for LLM request/response/tool-call
  # parsing — the primary debugging surface for multi-agent issues.
  # Read at runtime so toggling only needs a process restart (no recompile).
  defp diag_log(msg) do
    if Application.get_env(:hiveweave, [:diagnostics, :enabled], false) do
      Logger.info("[Streamer-DIAG] " <> msg)
    end
  end

  @doc """
  Stream a chat completion from the LLM.
  """
  def stream(agent, message, opts, parent) do
    Logger.info("[Streamer] stream/4 CALLED for agent #{agent.id}")
    ensure_context_cache()
    agent = reload_agent(agent)

    # Clear the injected-inbox tracking set at the start of each stream.
    # This runs in the Task process (the same process that poll_and_inject_inbox
    # runs in), so the Process dict is correctly scoped here.
    # NOTE: agent.ex's Process.delete calls were no-ops because they ran in the
    # GenServer process, not this Task process.
    Process.delete(:hw_injected_inbox_ids)

    # Model-switch compaction check before resolving model
    old_ctx = get_cached_context_window(agent.id)
    model = resolve_model(agent)
    new_ctx = model[:context_window] || 128_000
    current_tokens = estimate_current_tokens(agent.id, agent.project_id)

    if old_ctx != new_ctx and old_ctx > 0 do
      HiveWeave.ConversationStore.maybe_compact_on_model_switch(
        agent.id, agent.project_id,
        old_context_window: old_ctx, new_context_window: new_ctx, current_tokens: current_tokens
      )
    end
    cache_context_window(agent.id, new_ctx)

    Logger.info("[Streamer] Agent #{agent.id} model=#{inspect(model[:model_id])} permission_type=#{agent.permission_type}")
    provider_name = model[:name] || "primary"

    case HiveWeave.LLM.CircuitBreaker.check(provider_name) do
      :ok ->
        execute_stream(agent, message, opts, parent, model, provider_name)

      {:fallback, fallback_name} when not is_nil(fallback_name) ->
        Logger.info("Circuit open for #{provider_name}, falling back to #{fallback_name}")
        fallback_model = resolve_model_by_name(fallback_name) || model
        execute_stream(agent, message, opts, parent, fallback_model, fallback_name)

      {:fallback, nil} ->
        publish_error(agent, "All providers unavailable")
        send(parent, {:stream_error, "All providers unavailable"})
        {:error, :all_providers_down}
    end
  end

  # ── Main execution with tool loop ───────────────────────────

  defp execute_stream(agent, message, opts, parent, model, provider_name) do
    start_time = System.monotonic_time(:millisecond)
    # Reset the delta sequence counter for this streaming session.
    # Each text_delta/thinking_delta gets a monotonically increasing seq,
    # which the frontend uses to deduplicate across duplicate connections.
    Process.put(:hw_seq_counter, 0)
    Process.delete(:hw_agents_map)
    HiveWeave.Telemetry.llm_stream_start(provider_name, model[:model_id])

    # Broadcast a "thinking" activity event to lobby so Live Activity shows
    # something while waiting for the LLM's first token / tool call.
    Phoenix.PubSub.broadcast(
      HiveWeave.PubSub,
      "lobby:status",
      {:activity, %{
        agentId: agent.id,
        agentName: agent_name(agent),
        type: "thinking",
        content: "正在思考...",
        timestamp: System.system_time(:millisecond)
      }}
    )

    # Load conversation history
    token_budget = (model[:context_window] || 128_000) - 20_000
    history = HiveWeave.ConversationStore.get_history(agent.id, agent.project_id, token_budget)

    Logger.info("[Streamer] Agent #{agent.id} history: #{length(history)} messages, " <>
                "budget=#{token_budget}, new user msg=#{String.length(message || "")} chars")

    # Resolve workspace path for tool execution
    workspace_path = resolve_workspace(agent)
    permission_type = get_agent_permission_type(agent)
    role = (is_map(agent) && Map.get(agent, :role)) || "executor"

    # Get tools available to this agent
    tools = ToolExecutor.get_tools(permission_type, role)
    Logger.info("[Streamer] Agent #{agent.id} permission_type=#{permission_type}, tools=#{length(tools)}: " <>
                Enum.map_join(tools, ", ", fn t -> get_in(t, ["function", "name"]) end))

    # Build initial messages: system + history + user
    initial_messages = build_messages(agent, message, opts, history, model)

    # Create placeholder assistant message.
    # When triggered by coordinator (not by user), mark as background so it
    # doesn't appear in the foreground chat panel — the user didn't message
    # this agent directly; the response is internal agent-to-agent processing.
    is_triggered = Keyword.get(opts, :trigger, false)
    assistant_msg_id = Ecto.UUID.generate()
    now_ms = System.system_time(:millisecond)

    case ChatMessage.save_message(%{
           id: assistant_msg_id,
           agent_id: agent.id,
           role: "assistant",
           content: "",
           is_streaming: true,
           is_read: false,
           is_background: is_triggered,
           team_from_agent_id: agent.id,
           team_to_agent_id: if(is_triggered, do: Keyword.get(opts, :from_agent_id), else: nil),
           created_at: now_ms
         }) do
      {:ok, _} -> :ok
      {:error, reason} -> Logger.warning("Failed to save placeholder: #{inspect(reason)}")
    end

    broadcast_chunk(agent, %{type: "start", id: assistant_msg_id})

    # Run the tool loop
    result = run_tool_loop(
      agent, model, provider_name, tools, workspace_path,
      initial_messages, "", "", parent, 0, [], assistant_msg_id,
      []
    )

    finalize_stream(agent, result, assistant_msg_id, message, start_time, parent, model, provider_name, opts)
  end

  # ── Tool loop: stream → check tool_calls → execute → repeat ─

  defp run_tool_loop(agent, model, provider_name, tools, workspace_path, messages, text_acc, thinking_acc, parent, round_num, tool_history \\ [], assistant_msg_id \\ nil, tool_turn_acc \\ []) do
    max_rounds = max_tool_rounds_for(agent.role)
    if round_num >= max_rounds do
      Logger.warning("[Streamer] Max tool rounds (#{max_rounds}) reached for agent #{agent.id} (role: #{agent.role})")

      # Like OpenCode: do one final LLM call WITHOUT tools, asking the agent
      # to summarize what it accomplished and what remains. This gives the user
      # a meaningful message instead of a silent cut-off.
      summary = make_max_rounds_summary(agent, model, provider_name, messages, parent)
      final_text = if text_acc == "", do: summary, else: text_acc <> "\n\n" <> summary
      final_msg = %{"role" => "assistant", "content" => final_text}
      {:ok, final_text, tool_history, thinking_acc, tool_turn_acc ++ [final_msg]}
    else
      # Context overflow protection (like OpenCode's overflow.ts):
      # usable = context_window - max_output_tokens - safety_buffer
      # When estimated tokens exceed usable, trim old messages from the middle.
      context_window = model[:context_window] || 128_000
      max_output = if model[:supports_thinking],
        do: (model[:max_output_tokens] || 32_000),
        else: min(model[:max_output_tokens] || 8_192, 32_000)
      safety_buffer = 4_096  # reserve for system prompt overhead
      usable_tokens = max(context_window - max_output - safety_buffer, 8_192)
      {messages, trimmed_count} = trim_context_if_needed(messages, usable_tokens, agent, model)

      # Mid-round reminder: when 80% of rounds used, inject a "wrap up" hint
      max_rounds = max_tool_rounds_for(agent.role)
      messages = if round_num == max(max_rounds - div(max_rounds, 5), 1) and round_num < max_rounds do
        rounds_left = max_rounds - round_num
        reminder = "⚠️ You have #{rounds_left} tool calls remaining. Start wrapping up: finish critical actions now and prepare a summary."
        Logger.info("[Streamer] Injecting mid-round reminder at round #{round_num} (#{rounds_left} left)")
        messages ++ [%{"role" => "system", "content" => reminder}]
      else
        messages
      end

      request_body = %{
        "model" => model[:model_id] || "deepseek-v4-pro",
        "messages" => messages,
        "stream" => true,
        "stream_options" => %{"include_usage" => true},
        "temperature" => 0.7
      }

      # ── max_tokens calculation (like OpenCode) ──
      # For non-reasoning models: min(model.max_output, 32000)
      # For reasoning models: use full model.max_output (reasoning eats into budget)
      model_max = model[:max_output_tokens]
      global_cap = 32_000
      max_tokens = cond do
        model[:supports_thinking] and is_integer(model_max) and model_max > 0 ->
          # Reasoning models need full budget — reasoning + content share max_tokens
          model_max
        is_integer(model_max) and model_max > 0 ->
          min(model_max, global_cap)
        true ->
          global_cap
      end
      request_body = Map.put(request_body, "max_tokens", max_tokens)

      # ── Reasoning effort (like OpenCode's reasoning_effort) ──
      # Only send reasoning_effort when the model config explicitly sets it.
      # Sending it unconditionally may cause 400 errors on APIs that don't
      # support this parameter (not all OpenAI-compatible APIs do).
      request_body = if model[:supports_thinking] and is_binary(model[:reasoning_effort]) and model[:reasoning_effort] != "" do
        Map.put(request_body, "reasoning_effort", model[:reasoning_effort])
      else
        request_body
      end

      # Add tools if available
      request_body = if tools != [], do: Map.put(request_body, "tools", tools), else: request_body

      Logger.info("[Streamer] Round #{round_num}: sending request to LLM with #{length(messages)} messages")

      # Generate a unique delta_id for this round so the frontend can group tokens
      delta_id = "r#{round_num}_#{:rand.uniform(999999)}"

      # Wrap HTTP request with retry logic (3 attempts, exponential backoff)
      result = request_with_retry(agent, model, request_body, delta_id, round_num, 3)

      case result do
        {:ok, _status, new_text, reasoning, tool_calls, finish_reason, usage} ->
          # Reasoning models (e.g. step-3.7-flash) produce reasoning_content
          # alongside content. We preserve reasoning for:
          # 1. Broadcasting to frontend (thinking display)
          # 2. Fallback: if content is empty, use reasoning as output
          # 3. Passing back in multi-turn (assistant message includes reasoning)
          # Keep reasoning strictly in its own channel. NEVER substitute it for
          # content — reasoning is the model's internal monologue (often in
          # English even when the user writes Chinese) and surfacing it as the
          # visible reply creates a fake "mixed language" bug. If the model
          # only thought and didn't speak, the round's text is empty and the
          # agent's :empty retry path handles it.
          new_text = if is_binary(new_text), do: new_text, else: ""
          combined_text = text_acc <> new_text
          combined_thinking = thinking_acc <> (if is_binary(reasoning), do: reasoning, else: "")

          Logger.info("[Streamer] Round #{round_num}: got #{String.length(new_text)} chars text, #{String.length(combined_thinking)} chars thinking, #{length(tool_calls)} tool_calls, finish=#{finish_reason}")

          # ── Per-round LLM trace for monitoring/debugging ──
          # usage comes from handle_req_response (Task child process passes
          # it back via return value, since Process dict doesn't cross processes).
          # Stash in :hw_stream_usage so finalize_stream can read the last round's usage.
          if usage do
            Process.put(:hw_stream_usage, usage)
          end
          try do
            HiveWeave.EventAudit.log(agent.id, "llm_round", %{
              round_num: round_num,
              input_tokens: usage && usage.input,
              output_tokens: usage && usage.output,
              total_tokens: usage && usage.total,
              finish_reason: finish_reason,
              model: model[:model_id],
              msg_count: length(messages),
              text_len: String.length(new_text || ""),
              tool_count: length(tool_calls)
            })
          rescue
            _ -> :ok
          end

          # Handle truncated responses (finish_reason = "length" or "content_filter")
          cond do
            finish_reason in ["length", "content_filter"] and tool_calls != [] ->
              Logger.warning("[Streamer] Round #{round_num}: finish_reason=#{finish_reason}, tool_calls may be incomplete — discarding")

              # Discard potentially incomplete tool_calls, append a warning to text
              warning = "\n\n⚠️ Response was truncated (#{finish_reason}). Some tool calls may be incomplete."
              final_msg = %{"role" => "assistant", "content" => combined_text <> warning}
              {:ok, combined_text <> warning, tool_history, combined_thinking, tool_turn_acc ++ [final_msg]}

            finish_reason == "length" ->
              Logger.warning("[Streamer] Round #{round_num}: finish_reason=length, output was truncated (max_tokens too small?)")
              final_msg = %{"role" => "assistant", "content" => combined_text <> "\n\n⚠️ 回复被截断（达到最大输出长度），请继续以完成。"}
              {:ok, combined_text <> "\n\n⚠️ 回复被截断（达到最大输出长度），请继续以完成。", tool_history, combined_thinking, tool_turn_acc ++ [final_msg]}

            finish_reason == "content_filter" ->
              Logger.warning("[Streamer] Round #{round_num}: finish_reason=content_filter, content was filtered")
              final_msg = %{"role" => "assistant", "content" => combined_text <> "\n\n⚠️ 回复被内容过滤器截断。"}
              {:ok, combined_text <> "\n\n⚠️ 回复被内容过滤器截断。", tool_history, combined_thinking, tool_turn_acc ++ [final_msg]}

            tool_calls != [] ->
              # Execute tools and loop
              Logger.info("[Streamer] Round #{round_num}: #{length(tool_calls)} tool_calls to execute")

              # If no text has been produced at all (across all rounds), broadcast a
              # default message so the user sees something in the chat window instead of
              # silence. We check combined_text (which includes text_acc from prior rounds)
              # rather than just new_text, to avoid repeating the default on every tool-loop
              # round that happens to return tool_calls without text.
              if combined_text == "" do
                default_msg = "好的，开始处理。\n"
                broadcast_chunk(agent, %{type: "text_delta", content: default_msg, delta_id: "default_#{round_num}"})
                combined_text = default_msg
                # Continue with updated combined_text
                run_tool_loop_with_tools(agent, model, provider_name, tools, workspace_path, messages, combined_text, combined_thinking, parent, round_num, tool_history, tool_calls, new_text, reasoning, assistant_msg_id, tool_turn_acc)
              else
                run_tool_loop_with_tools(agent, model, provider_name, tools, workspace_path, messages, combined_text, combined_thinking, parent, round_num, tool_history, tool_calls, new_text, reasoning, assistant_msg_id, tool_turn_acc)
              end

            true ->
              # No tool calls — we're done
              Logger.info("[Streamer] Round #{round_num}: done, no more tool_calls, total text=#{String.length(combined_text)} chars")

              # Handle empty response: LLM returned neither text nor tool_calls
              if combined_text == "" do
                # Return special :empty marker so the agent can schedule a retry
                Logger.warning("[Streamer] Round #{round_num}: empty response (no text, no tool_calls)")
                {:empty, tool_history, combined_thinking, tool_turn_acc}
              else
                final_msg = %{"role" => "assistant", "content" => combined_text}
                {:ok, combined_text, tool_history, combined_thinking, tool_turn_acc ++ [final_msg]}
              end
          end

        {:error, reason} ->
          Logger.error("[Streamer] Round #{round_num}: LLM error: #{inspect(reason)}")
          # Provide user-friendly error messages for common failures
          friendly = case reason do
            {:http_error, 429, _} ->
              "⚠️ 模型被限流（429），请稍后重试或切换到其他模型。"
            {:http_error, 401, _} ->
              "⚠️ API Key 无效或已过期（401），请在模型设置中检查。"
            {:http_error, status, _} when status >= 500 ->
              "⚠️ 模型服务端错误（#{status}），请稍后重试或切换模型。"
            :timeout ->
              "⚠️ 请求超时，模型可能不可用，请稍后重试或切换模型。"
            :no_model_configured ->
              "⚠️ 未配置模型，请在设置中选择一个可用的模型。"
            _ -> nil
          end
          if friendly do
            final_msg = %{"role" => "assistant", "content" => friendly}
            {:ok, friendly, tool_history, thinking_acc, tool_turn_acc ++ [final_msg]}
          else
            {:error, reason}
          end
      end
    end
  end

  # ── Execute tools and continue loop (extracted from run_tool_loop) ──

  defp run_tool_loop_with_tools(agent, model, provider_name, tools, workspace_path, messages, combined_text, combined_thinking, parent, round_num, tool_history, tool_calls, new_text, reasoning, assistant_msg_id, tool_turn_acc \\ []) do
    # Accumulate tool calls for history (moved up so mid-stream save can include it)
    new_tool_history = tool_history ++ Enum.map(tool_calls, fn tc ->
      %{
        "id" => tc.id,
        "type" => "function",
        "function" => %{
          "name" => tc.name,
          "arguments" => tc.arguments
        }
      }
    end)

    # Save accumulated text + thinking + tool_calls to DB so a page refresh
    # (or a crash in a later round) doesn't lose what the agent already did.
    # Without tool_calls here, a crash/empty exit leaves tool_calls="[]" forever.
    if assistant_msg_id && combined_text != "" do
      try do
        HiveWeave.Services.ChatMessage.update_message(agent.id, assistant_msg_id, %{
          content: combined_text,
          thinking: combined_thinking,
          tool_calls: Jason.encode!(new_tool_history),
          is_streaming: true
        })
      rescue
        _ -> :ok
      end
    end

    # Build assistant message with tool_calls for the messages array
    # Include reasoning_content for multi-turn context (like OpenCode's ReasoningPart)
    # so reasoning models maintain their chain of thought across tool calls
    assistant_msg = %{
      "role" => "assistant",
      "content" => (if new_text == "", do: nil, else: new_text),
      "tool_calls" => Enum.map(tool_calls, fn tc ->
        %{
          "id" => tc.id,
          "type" => "function",
          "function" => %{
            "name" => tc.name,
            "arguments" => tc.arguments
          }
        }
      end)
    }

    # Add reasoning_content to assistant message for reasoning models
    # This allows the model to maintain context across tool-call rounds
    assistant_msg = if model[:supports_thinking] and is_binary(reasoning) and reasoning != "" do
      Map.put(assistant_msg, "reasoning_content", reasoning)
    else
      assistant_msg
    end

    # Execute tool calls in parallel (independent tools can run concurrently)
    # Use Task.Supervisor.async_nolink so a crashing tool doesn't kill siblings
    # or the parent stream Task (which is itself async_nolink under TaskSupervisor).
    tool_results =
      if length(tool_calls) > 1 do
        tasks = Enum.map(tool_calls, fn tc ->
          Task.Supervisor.async_nolink(HiveWeave.TaskSupervisor, fn ->
            execute_single_tool(agent, tc, workspace_path)
          end)
        end)
        Enum.map(tasks, fn task ->
          case Task.yield(task, 120_000) || Task.shutdown(task, 5_000) do
            {:ok, result} ->
              # Normal completion — result is the %{"role" => "tool", ...} map
              result
            {:exit, reason} ->
              # Task crashed — return error message as tool result
              Logger.error("[Streamer] Tool task crashed: #{inspect(reason)}")
              %{"role" => "tool", "content" => "[Tool Crash] #{inspect(reason)}"}
            nil ->
              # Task timed out or was killed
              %{"role" => "tool", "content" => "[Tool Timeout] Task did not complete within 120s"}
          end
        end)
      else
        Enum.map(tool_calls, fn tc ->
          try do
            execute_single_tool(agent, tc, workspace_path)
          rescue
            e ->
              Logger.error("[Streamer] Tool #{tc.name} raised: #{inspect(e)}")
              %{
                "role" => "tool",
                "tool_call_id" => tc.id,
                "content" => "[Tool Crash] #{tc.name}: #{inspect(e)}\n#{Exception.format_stacktrace()}"
              }
          end
        end)
      end

    # Append assistant + tool results to messages, continue loop
    new_messages = messages ++ [assistant_msg | tool_results]

    # Accumulate this round's assistant + tool results for the conversation store.
    # We don't slice new_messages because mid-turn trim (line 194) can shrink
    # the prefix, breaking offset-based slicing. The accumulator captures exactly
    # what was added in this round, independent of any trim.
    new_tool_turn_acc = tool_turn_acc ++ [assistant_msg | tool_results]

    # (new_tool_history was computed above before the mid-stream save)

    # ── messagePoller: poll inbox at natural breakpoint (between tool turns) ──
    # Like TS createMessagePoller — check for new inbox messages that arrived
    # while we were executing tools. Inject by priority:
    #   urgent → task switch (save progress, handle, resume)
    #   normal → inject with "continue current task" guidance
    #   low    → defer to end of task (don't inject)
    new_messages = poll_and_inject_inbox(agent, new_messages)

    run_tool_loop(agent, model, provider_name, tools, workspace_path, new_messages, combined_text, combined_thinking, parent, round_num + 1, new_tool_history, assistant_msg_id, new_tool_turn_acc)
  end

  # ── messagePoller: poll inbox and inject messages by priority ──
  # Called between tool rounds — the natural breakpoint in the agent loop.
  # Mirrors TS createMessagePoller + agent-runtime.ts priority handling.
  defp poll_and_inject_inbox(agent, messages) do
    try do
      pending = HiveWeave.Services.Inbox.get_pending_messages(agent.id)

      if pending == [] do
        messages
      else
        # Skip messages already injected in this stream to avoid duplicate injection.
        # We use a Process-level MapSet (not mark_read) so that self-retrigger
        # in handle_info({ref, result}) can still detect these messages via
        # get_pending_messages and fire a proper trigger after the LLM finishes.
        injected_set = Process.get(:hw_injected_inbox_ids) || MapSet.new()
        pending = Enum.reject(pending, &MapSet.member?(injected_set, &1.id))

        if pending == [] do
          messages
        else
          # Split by priority first.
          # DO NOT mark injected messages as read here — that would make
          # get_pending_messages return [] in the self-retrigger check after
          # LLM completion, causing the agent to miss messages that arrived
          # mid-stream (e.g. sub-ordinate replies). Instead, we track them
          # in the Process dictionary and let build_trigger_context handle
          # the formal mark_read after a successful LLM response.
          {urgent, normal, low} = split_by_priority(pending)

          # Track injected IDs to prevent re-injection in subsequent tool rounds
          new_ids = Enum.map(urgent ++ normal ++ low, & &1.id)
          Process.put(:hw_injected_inbox_ids, MapSet.union(injected_set, MapSet.new(new_ids)))

          # Resolve sender names — cached per-stream (agents rarely change mid-stream)
          agents_map = Process.get(:hw_agents_map) || cache_agents_map(agent.project_id)

          # Broadcast queued_message event to frontend
          total = length(urgent) + length(normal) + length(low)
          if total > 0 do
            Logger.info("[Streamer] messagePoller: #{total} messages (urgent=#{length(urgent)} normal=#{length(normal)} low=#{length(low)})")
            broadcast_chunk(agent, %{type: "queued_message", count: total, urgent: length(urgent), normal: length(normal), low: length(low)})
          end

          # Low priority: don't inject — left unread for next trigger context

          # Normal priority: inject with "continue current task" guidance
          messages = if normal != [] do
            queue_text = format_normal_messages(normal, agents_map)
            messages ++ [%{"role" => "user", "content" => queue_text}]
          else
            messages
          end

          # Urgent priority: task switch — save progress, handle, resume
          messages = if urgent != [] do
            urgent_text = format_urgent_messages(urgent, agents_map)
            messages ++ [%{"role" => "user", "content" => urgent_text}]
          else
            messages
          end

          messages
        end
      end
    rescue
      e ->
        # Non-critical: poller failure should not break the agent loop
        Logger.warning("[Streamer] messagePoller error: #{inspect(e)}")
        messages
    end
  end

  defp cache_agents_map(project_id) do
    map = HiveWeave.Services.Org.list_agents(project_id)
    |> Enum.map(fn a -> {a.id, a} end)
    |> Map.new()
    Process.put(:hw_agents_map, map)
    map
  end

  defp split_by_priority(messages) do
    urgent = Enum.filter(messages, fn m -> (m.priority || "normal") == "urgent" end)
    normal = Enum.filter(messages, fn m -> (m.priority || "normal") == "normal" end)
    low = Enum.filter(messages, fn m -> m.priority == "low" end)
    {urgent, normal, low}
  end

  defp format_normal_messages(messages, agents_map) do
    header = "## 工作期间收到的新消息\n以下消息在你工作时到达。简短确认后继续当前任务。\n\n"
    body = format_inbox_body(messages, agents_map)
    footer = if Enum.any?(messages, & &1.expect_report) do
      "\n> **[系统]** 上述部分消息需要你回复。在完成任务前必须回复这些消息。\n"
    else
      ""
    end
    header <> body <> footer
  end

  defp format_urgent_messages(messages, agents_map) do
    header = "## ⚡ 紧急中断 — 需要切换任务\n\n一条紧急消息需要你立即处理。请严格按以下步骤执行：\n\n1. **保存当前进度**：调用 `todowrite` 记录当前任务进展。\n2. **处理紧急消息**：阅读并回复下面的消息。\n3. **恢复原任务**：处理完紧急消息后，检查 todos 并从上次中断处继续。\n\n---\n\n### 紧急消息：\n\n"
    body = format_inbox_body(messages, agents_map)
    footer = "\n> **[系统]** 立即用 todowrite 保存当前任务进度，然后处理紧急消息。不要丢失原任务上下文。\n"
    header <> body <> footer
  end

  # Shared body builder for priority message formatting
  defp format_inbox_body(messages, agents_map) do
    messages |> Enum.map(fn msg ->
      sender = Map.get(agents_map, msg.from_agent_id)
      from_name = if sender, do: sender.name, else: String.slice(msg.from_agent_id || "", 0, 8)
      report_tag = if msg.expect_report, do: " **[需要回复]**", else: ""
      "- **[来自: #{from_name}]**#{report_tag}: \"#{msg.message || ""}\""
    end) |> Enum.join("\n")
  end

  # ── Max rounds reached: ask LLM to summarize (no tools) ──────

  defp make_max_rounds_summary(agent, model, provider_name, messages, parent) do
    max_rounds_prompt = """
    CRITICAL — MAXIMUM TOOL ROUNDS REACHED

    You have reached the maximum number of tool calls for this turn. Tools are now disabled.

    You MUST respond with a text summary. Include:
    1. What you have accomplished so far
    2. What tasks remain incomplete
    3. Recommended next steps

    Respond with text ONLY. Do NOT attempt any tool calls.
    """

    # Append the max-steps prompt as a system message
    summary_messages = messages ++ [%{"role" => "user", "content" => max_rounds_prompt}]

    request_body = %{
      "model" => model[:model_id] || "deepseek-v4-pro",
      "messages" => summary_messages,
      "stream" => false,
      "temperature" => 0.3
    }

    # Set max_tokens for summary request too
    model_max = model[:max_output_tokens]
    request_body = if model[:supports_thinking] and is_integer(model_max) and model_max > 0 do
      Map.put(request_body, "max_tokens", model_max)
    else
      Map.put(request_body, "max_tokens", min(model_max || 8_192, 32_000))
    end

    # Add reasoning_effort for thinking models (only if explicitly configured)
    request_body = if model[:supports_thinking] and is_binary(model[:reasoning_effort]) and model[:reasoning_effort] != "" do
      Map.put(request_body, "reasoning_effort", model[:reasoning_effort])
    else
      request_body
    end

    # No tools in this request
    Logger.info("[Streamer] Making max-rounds summary request (no tools)")

    base_url = (model[:base_url] || "") |> to_string() |> String.trim_trailing("/")
    api_key = model[:api_key] || ""
    url = "#{base_url}/chat/completions"

    if base_url == "" or api_key == "" do
      "⚠️ Reached max tool rounds. Some tasks may be incomplete."
    else
      headers = [
        {"authorization", "Bearer #{api_key}"},
        {"content-type", "application/json"}
      ]

      try do
        case Req.post(url,
               body: Jason.encode!(request_body),
               headers: headers,
               receive_timeout: 10_000,
               finch: HiveWeave.Finch
             ) do
          {:ok, %{status: 200, body: resp_body}} ->
            case resp_body do
              %{"choices" => [%{"message" => %{"content" => content}} | _]} ->
                content || "Reached max tool rounds. See previous messages for progress."
              _ ->
                "Reached max tool rounds. See previous messages for progress."
            end
          {:ok, %{status: status}} ->
            Logger.warning("[Streamer] Max-rounds summary request failed: HTTP #{status}")
            "⚠️ Reached max tool rounds (#{max_tool_rounds_for(agent.role)}). Some tasks may be incomplete."
          {:error, reason} ->
            Logger.warning("[Streamer] Max-rounds summary request error: #{inspect(reason)}")
            "⚠️ Reached max tool rounds (#{max_tool_rounds_for(agent.role)}). Some tasks may be incomplete."
        end
      rescue
        e ->
          Logger.warning("[Streamer] Max-rounds summary crashed: #{inspect(e)}")
          "⚠️ Reached max tool rounds (#{max_tool_rounds_for(agent.role)}). Some tasks may be incomplete."
      end
    end
  end

  # ── Finalize: save message, broadcast done, persist turn ────

  defp finalize_stream(agent, result, assistant_msg_id, user_message, start_time, parent, model, provider_name, opts) do
    case result do
      {:empty, tool_history, combined_thinking, _tool_turn_acc} ->
        # LLM returned no text and no tool_calls (likely only reasoning_content).
        # agent.ex's handle_info({ref, result}) matches on a 3-tuple {:empty, ...}
        # to drive its retry-with-backoff logic, so we unwrap the 4-tuple here.
        # Save tool_history so the user can see what the agent already did
        # (mid-stream saves in run_tool_loop_with_tools also save tool_calls,
        # but this ensures the final value is correct even if rounds were
        # trimmed or the history was rebuilt).
        tool_calls_json = case tool_history do
          [] -> "[]"
          _ -> Jason.encode!(tool_history)
        end
        ChatMessage.update_message(agent.id, assistant_msg_id, %{
          is_streaming: false,
          tool_calls: tool_calls_json
        })
        broadcast_chunk(agent, %{type: "done", status: :empty, id: assistant_msg_id})
        send(parent, {:stream_done, %{status: :empty}})
        {:empty, tool_history, combined_thinking}

      {:ok, full_text, tool_history, thinking, new_turn_messages} ->
        duration = System.monotonic_time(:millisecond) - start_time

        # If LLM returned only tool_calls without any text, generate a summary
        # so the user sees something in the chat window instead of emptiness.
        display_text = cond do
          full_text != "" -> full_text
          tool_history != [] ->
            tool_names = Enum.map(tool_history, fn tc ->
              get_in(tc, ["function", "name"]) || "unknown"
            end)
            |> Enum.uniq()
            |> Enum.join(", ")
            "🔧 Executing: #{tool_names}"
          true -> "(模型未返回内容，可能该模型将回复放在 reasoning 字段中但未被捕获)"
        end

        tool_calls_json = case tool_history do
          [] -> "[]"
          _ -> Jason.encode!(tool_history)
        end

        ChatMessage.update_message(agent.id, assistant_msg_id, %{
          content: display_text,
          thinking: thinking,
          is_streaming: false,
          tool_calls: tool_calls_json
        })

        broadcast_chunk(agent, %{type: "done", status: :ok, id: assistant_msg_id})
        send(parent, {:stream_done, %{status: :ok, content: display_text}})

        # Persist turn to ConversationStore — but ONLY for real user conversations.
        # Triggered runs (escalations, idle checks, handoffs) must NOT pollute
        # the conversation history, otherwise repeated escalations overwhelm
        # the token budget and cause the agent to "lose memory" of real chats.
        #
        # CRITICAL: persist the FULL new-turn messages (user + assistant(tool_calls)
        # + tool_results), NOT a simplified [user, assistant_text] pair. The agent
        # MUST see its own tool calls and their results on subsequent turns —
        # otherwise after 2-3 turns it forgets everything it did.
        #
        # new_turn_messages is built by run_tool_loop's accumulator and contains
        # all assistant+tool_messages from this turn. We prepend the user message
        # to complete the turn.
        unless opts[:trigger] do
          turn_messages =
            [
              %{"role" => "user", "content" => user_message || ""}
            ] ++
              (if is_list(new_turn_messages), do: new_turn_messages, else: []) ++
              [
                %{"role" => "assistant", "content" => display_text}
              ]
          HiveWeave.ConversationStore.append_turn(agent.id, agent.project_id, turn_messages)
        end

        # Log token usage summary (accumulated across all tool rounds)
        usage = Process.get(:hw_stream_usage, nil)
        if usage do
          Logger.info("[Streamer] Agent #{agent.id} (#{agent.role}) stream complete: " <>
                      "input=#{usage.input} output=#{usage.output} total=#{usage.total} tokens, " <>
                      "duration=#{duration}ms, model=#{model[:model_id]}")
        else
          Logger.info("[Streamer] Agent #{agent.id} (#{agent.role}) stream complete: " <>
                      "duration=#{duration}ms, model=#{model[:model_id]} (no usage reported)")
        end

        HiveWeave.Telemetry.llm_stream_done(provider_name, model[:model_id], duration, :ok)
        HiveWeave.LLM.CircuitBreaker.report_success(provider_name)
        {:ok, :completed, display_text}

      {:error, reason} ->
        # Mark streaming as done. Mid-stream saves in run_tool_loop_with_tools
        # already persisted content, thinking, and tool_calls from completed
        # rounds — we leave those intact and just flip the streaming flag.
        ChatMessage.update_message(agent.id, assistant_msg_id, %{is_streaming: false})

        duration = System.monotonic_time(:millisecond) - start_time
        HiveWeave.Telemetry.llm_stream_fail(provider_name, inspect(reason))
        HiveWeave.LLM.CircuitBreaker.report_failure(provider_name)
        publish_error(agent, inspect(reason))

        broadcast_chunk(agent, %{type: "done", status: :error, id: assistant_msg_id})
        send(parent, {:stream_error, inspect(reason)})
        {:error, reason}
    end
  end

  # ── Build messages array ────────────────────────────────────

  # Build messages with prefix-cache optimized ordering:
  # System 1 (STATIC, API-cacheable): Identity + role guidelines
  # System 2 (DYNAMIC): Memories + active skills
  # History: recent conversation turns (no system msgs)
  # User: current message
  defp build_messages(agent, message, opts, history, model) do
    sys_identity = build_identity_prompt(agent, model)  # Static, prefix-cached by LLM API
    sys_context = build_context_prompt(agent)          # Dynamic, changes on memory/skill updates

    # Unify message format: prefix user input with [来自: 用户] so the AI sees
    # user messages and agent messages in the same shape (cf. format_inbox_body).
    prefixed_message = "[来自: 用户] #{message || ""}"

    user =
      if opts[:images] && length(opts[:images]) > 0 do
        %{
          "role" => "user",
          "content" =>
            [
              %{"type" => "text", "text" => prefixed_message}
              | Enum.map(opts[:images], fn img ->
                  %{"type" => "image_url", "image_url" => %{"url" => img}}
                end)
            ]
        }
      else
        %{"role" => "user", "content" => prefixed_message}
      end

    history_filtered = Enum.filter(history, fn m -> m["role"] != "system" end)

    # If context prompt is empty, skip it to save tokens.
    # IMPORTANT: context is placed AFTER history (not right after identity) so that
    # the prefix [sys_identity + tools + stable_history] can be cached by the LLM API.
    # Putting dynamic context early would break the cache for tools (~20KB) every round.
    context =
      case sys_context do
        %{"content" => ""} -> []
        nil -> []
        ctx ->
          # Mark as system so the model treats it as context, not a user message
          [%{ctx | "role" => "system"}]
      end

    [sys_identity] ++ history_filtered ++ context ++ [user]
  end

  # Static identity prompt — designed for LLM API prefix caching.
  # This message never changes for a given agent across turns.
  defp build_identity_prompt(agent, model) do
    name = (is_map(agent) && Map.get(agent, :name)) || "Agent"
    role = (is_map(agent) && Map.get(agent, :role)) || "executor"
    permission_type = get_agent_permission_type(agent)

    goal =
      cond do
        is_map(agent) and Map.has_key?(agent, :goal) -> Map.get(agent, :goal) || ""
        is_map(agent) and Map.has_key?(agent, :config) ->
          cfg = Map.get(agent, :config) || %{}
          if is_map(cfg), do: Map.get(cfg, :goal) || "", else: ""
        true -> ""
      end

    backstory = get_agent_backstory(agent)

    prompt =
      """
      You are "#{name}", a #{role} in the HiveWeave engineering organization.
      #{if is_binary(goal) and goal != "", do: "## Your Role\n#{goal}", else: ""}
      #{if is_binary(backstory) and backstory != "", do: "## Background\n#{backstory}", else: ""}

      ## ETHOS — 工程准则（所有角色共享）
      ### 原则 1: Boil the Lake（做完整的事）
      AI 让"完整性"的边际成本趋近于零。当完整实现只比捷径多花几分钟时，就做完整版。
      - **湖**（可煮沸）：100% 测试覆盖、完整边界处理、完整错误路径——这些必须做完
      - **海洋**（不可煮沸）：整体重写、跨季度迁移——这些分阶段做
      - 反模式："省 70 行只做 90%"、"测试留到下个 PR"、"边界情况以后再说"

      ### 原则 2: Search Before Building（先搜索后构建）
      三层知识观：
      - Layer 1: 验证过的成熟模式 → 直接用
      - Layer 2: 新流行的实践 → 审视后用（人群会狂热）
      - Layer 3: 第一性原理推导 → 最有价值，"11/10 的项目"往往来自这种 zig while others zag

      ### 原则 3: User Involvement（用户参与度，可调）
      用户主权不是固定铁律，而是可配置的参与度级别。具体级别由 charter 的 user_involvement 字段决定（高/中/低，见动态上下文）。
      - **无论哪个级别，AI 都不能伪造结果、不能隐藏风险、不能跳过验证**
      - 让渡的是决策权，不是诚实义务

      ### 通用验证文化（不可协商）
      - 每个动作必须有证据支撑——"看起来对"永远不够
      - 测试通过须附输出、构建成功须附日志、运行时验证须附截图
      - 没有证据的"完成"等于未完成

      ### 通用反合理化表
      | 借口 | 反驳 |
      |---|---|
      | "我稍后加测试" | 测试是代码的一部分，没有测试的代码是未完成的代码 |
      | "这个改动太小不用测" | 小改动也能引入大 bug，每个改动都需要测试 |
      | "先跑通再说" | 能跑 ≠ 正确，先验证再扩展 |
      | "这个方向很明显不用问" | 根据用户参与度配置决定：高风险决策方向必须确认 |

      ## IMPORTANT: HiveWeave System Directory
      - **`.hiveweave`** is the HiveWeave system directory at the workspace root.
      - **NEVER read, write, edit, move, or delete any files inside `.hiveweave`.**
      - **NEVER run shell commands that target `.hiveweave`** (rm, mv, cp, etc.).

      ## Permission Level: #{permission_type}
      #{if permission_type == "coordinator" do
        build_coordinator_prompt(role, name)
      else
        build_executor_prompt(role, name)
      end}

      ## Honesty & Integrity Rules (MANDATORY — ZERO TOLERANCE)
      - **NEVER claim to have done something you did not actually do.** If you did not call a tool, you did NOT perform that action. Period.
      - **NEVER fabricate results, IDs, or outcomes.** Only report what a tool actually returned to you.
      - **If you lack a tool for a task, say so honestly.** Do NOT pretend you did it.
      - **If a tool call fails, report the failure truthfully.** Do not mask errors or pretend the action succeeded.
      - **NEVER write work logs claiming completion of work you did not perform.**
      - Violating these rules is the worst possible mistake you can make. Honesty above all else.

      ## Decision-Making Rules (MANDATORY)
      - **NEVER make autonomous decisions that affect the project direction, architecture, or resource allocation.**
      - When faced with decisions: route the question based on the project charter's "User Involvement" setting.
        If the charter says the user handles that type of question → ask the user (via `question` or `send_message` to "user").
        If not → ask your superior (`send_message` with recipients=["上级花名"]), not the user.
      - **For any risky action** (deleting files, modifying critical systems, irreversible changes), consult the user or superior first.
      - Do not assume — ask. Applies to ALL agents at ALL levels.

      ## Communication Rules
      - Messages from all sources (user or agent) arrive in a unified format: `[来自: 名称] 内容`. Treat them equally — the sender could be the user (human operator) or any agent.
      - **Replying to the user**: just speak normally in your response. The system auto-delivers your text to the user's chat window with streaming. Do NOT use send_message(recipients=["user"]) for replies — that creates a non-streaming notification.
      - **Replying to an agent**: use `send_message` with the agent's name as recipient.
      - **MANDATORY: Address other agents by their name (花名), NEVER by ID or role title.** A role may have multiple people — using a role title could send the message to the wrong person. Use list_subordinates or view_org_chart to learn names.
      - **send_message supports group send** — recipients is an array, you can message multiple people at once. E.g. recipients=["Alice","Bob","Carol"] to notify an entire squad simultaneously.
      - **NEVER claim a colleague is "working", "busy", or "idle" without calling check_agent_status first.**
      - After completing a task, ALWAYS `send_message` to your superior (recipients=["上级花名"], expectReport=true) with a brief summary
      - If blocked, use `send_message` (recipients=["上级花名"]) to ask your superior for clarification
      - Use tools proactively to record progress

      ## ⚠️ ACTION DISCIPLINE (CRITICAL)
      - DO NOT output a summary or plan as your final message without executing the tools first.
      - If you say "I will save the charter" — you MUST call `save_charter` in the same turn.
      - If you say "I will instruct HR" — you MUST call `send_message` to HR in the same turn.
      - If you say "I will dispatch tasks" — you MUST call `send_message` with the subordinate as recipient and expectReport=true in the same turn.
      - A text-only response that describes actions without calling tools is a FAILURE.
      - **ALWAYS write a brief one-sentence note BEFORE calling a tool** (e.g. "Reading docker-compose.yml to check the tech stack..."). The user sees this in real-time while the tool runs.
      - Do NOT write long summaries until all actions are complete.
      """
      |> String.trim()
      |> maybe_append_language_rule(model)

    %{"role" => "system", "content" => prompt}
  end

  # For Chinese-trained models (DeepSeek, Kimi, Qwen, GLM, Yi, …) the base
  # instruction alone is not enough — the model drifts between zh/en across
  # turns. Append one short hard rule. Western models (Claude, GPT, Gemini) are
  # trusted to mirror the user's language on their own — no rule needed.
  # Pattern mirrors opencode's packages/opencode/src/session/system.ts:26-40.
  defp maybe_append_language_rule(prompt, model) do
    model_id =
      case model do
        %{model_id: id} when is_binary(id) -> String.downcase(id)
        m when is_map(m) ->
          case Map.get(m, :model_id) || Map.get(m, "model_id") do
            id when is_binary(id) -> String.downcase(id)
            _ -> ""
          end
        _ -> ""
      end

    chinese_trained? =
      model_id != "" and
        (String.contains?(model_id, "deepseek") or
           String.contains?(model_id, "kimi") or
           String.contains?(model_id, "qwen") or
           String.contains?(model_id, "glm") or
           String.contains?(model_id, "yi-") or
           String.contains?(model_id, "doubao") or
           String.contains?(model_id, "ernie") or
           String.contains?(model_id, "hunyuan"))

    if chinese_trained? do
      prompt <>
        "\n\nWhen responding to the user, you MUST use the SAME language as the user, unless explicitly instructed to do otherwise."
    else
      prompt
    end
  end

  # Dynamic context prompt — rebuilt each turn from current memories + skills.
  # Goals workbook is injected ONLY when dirty (updated since agent last read it).
  defp build_context_prompt(agent) do
    mem_block = build_memory_block(agent)
    skill_block = HiveWeave.SkillRegistry.build_active_skills_section(
      Map.get(agent, :bound_skills) || "[]"
    )
    goals_block = build_goals_block_if_dirty(agent)
    involvement_block = build_involvement_block(agent)

    parts = [involvement_block, goals_block, mem_block, skill_block] |> Enum.reject(&(&1 == nil or &1 == ""))

    if parts == [] do
      nil
    else
      %{"role" => "system", "content" => Enum.join(parts, "\n\n")}
    end
  end

  # Read goals from DB, cached per project_id per turn (process dictionary).
  # Avoids duplicate SELECT when both build_goals_block_if_dirty and
  # build_involvement_block need the same charter_json in the same turn.
  defp read_goals_cached(project_id) do
    key = {:hw_goals, project_id}
    case Process.get(key) do
      {:cached, result} -> result
      :not_cached ->
        result = case HiveWeave.Services.Charter.read_goals(project_id) do
          {:ok, g} -> {:ok, g}
          _ -> nil
        end
        Process.put(key, {:cached, result})
        result
    end
  end

  # Build user involvement block from charter — injected every turn so the
  # agent always knows its current autonomy level.
  # Reads `userInvolvement` from goals; accepts "high"/"medium"/"low" or
  # legacy free-text (defaults to "high").
  defp build_involvement_block(agent) do
    project_id = Map.get(agent, :project_id)
    if project_id do
      level =
        case read_goals_cached(project_id) do
          {:ok, goals} when is_map(goals) ->
            raw = Map.get(goals, "userInvolvement", Map.get(goals, :userInvolvement, "high"))
            normalize_involvement_level(raw)
          _ ->
            "high"
        end
      format_involvement_block(level)
    else
      nil
    end
  rescue
    e ->
      Logger.warning("[Streamer] build_involvement_block error: #{inspect(e)}")
      nil
  end

  defp normalize_involvement_level(raw) when is_binary(raw) do
    case String.downcase(String.trim(raw)) do
      "high" -> "high"
      "medium" -> "medium"
      "low" -> "low"
      # Legacy free-text values (e.g. "宏观决策+技术选型") default to high
      _ -> "high"
    end
  end
  defp normalize_involvement_level(_), do: "high"

  defp format_involvement_block(level) do
    behavior = case level do
      "high" ->
        "- 技术决策：必须问用户（via question 工具）
- 产品/业务决策：必须问用户
- 重大方向变更：必须问用户
- 适用场景：用户有技术能力且想掌控方向"

      "medium" ->
        "- 技术决策：AI 自主执行
- 产品/业务决策：必须问用户
- 重大方向变更：必须问用户
- 适用场景：用户懂产品不懂技术，让渡技术决策权"

      "low" ->
        "- 技术决策：AI 自主执行
- 产品/业务决策：AI 自主执行
- 重大方向变更：仅通知用户
- 适用场景：用户完全信任 AI 或只想看结果"
    end

    "## User Involvement（当前级别：#{level}）\n#{behavior}\n\n**不变的部分**：无论哪个级别，AI 都不能伪造结果、不能隐藏风险、不能跳过验证。让渡的是决策权，不是诚实义务。"
  end

  # Inject the full goals workbook only when the agent hasn't read the latest version.
  # After injecting, mark the agent as having read this version (clears the dirty flag).
  # New agents (last-read version = nil) always get the workbook on first message.
  defp build_goals_block_if_dirty(agent) do
    project_id = Map.get(agent, :project_id)
    agent_id = Map.get(agent, :id)
    if project_id && agent_id do
      if HiveWeave.Services.Charter.goals_dirty?(project_id, agent_id) do
        current_version = HiveWeave.Services.Charter.get_goals_version(project_id)
        case read_goals_cached(project_id) do
          {:ok, goals} when is_map(goals) ->
            # Mark agent as having read this version (clears dirty flag).
            # If version is nil (goals never explicitly updated), use :initial
            # so the agent won't re-read until an actual update bumps the version.
            mark_version = current_version || :initial
            HiveWeave.Services.Charter.set_agent_goals_version(project_id, agent_id, mark_version)
            format_goals_block(goals)
          _ ->
            # Goals not available — still mark as read to avoid retrying every turn
            mark_version = current_version || :initial
            HiveWeave.Services.Charter.set_agent_goals_version(project_id, agent_id, mark_version)
            nil
        end
      else
        nil
      end
    else
      nil
    end
  rescue
    e ->
      Logger.warning("[Streamer] build_goals_block_if_dirty error: #{inspect(e)}")
      nil
  end

  defp format_goals_block(goals) do
    objective = Map.get(goals, "objective", Map.get(goals, :objective, ""))
    focus = Map.get(goals, "focus", Map.get(goals, :focus, ""))
    krs = Map.get(goals, "keyResults", Map.get(goals, :keyResults, []))
    involvement = Map.get(goals, "userInvolvement", Map.get(goals, :userInvolvement, "宏观决策+技术选型"))

    kr_lines =
      case krs do
        [] -> "  (none yet)"
        list ->
          Enum.map(list, fn kr ->
            case kr do
              %{"text" => text, "status" => status} -> "  - [#{status}] #{text}"
              text when is_binary(text) -> "  - #{text}"
              _ -> "  - #{inspect(kr)}"
            end
          end)
          |> Enum.join("\n")
      end

    "## Enterprise Goals Workbook (updated)\n" <>
    "**Objective:** #{objective}\n" <>
    "**Current Focus:** #{focus}\n" <>
    "**Key Results:**\n#{kr_lines}\n" <>
    "**User Involvement:** #{involvement}\n" <>
    "Route decisions matching the user-involvement scope to the user (via `question` or `send_message` to \"user\"). " <>
    "For decisions outside this scope, ask your superior (`send_message` with recipients=[\"上级花名\"])."
  end

  # Deprecated: kept for backwards compatibility. New code uses build_identity_prompt + build_context_prompt.
  defp build_system_prompt(agent) do
    identity = build_identity_prompt(agent, nil)
    context = build_context_prompt(agent)

    if context do
      %{identity | "content" => identity["content"] <> "\n\n" <> context["content"]}
    else
      identity
    end
  end

  defp build_memory_block(agent) do
    project_id = agent.project_id
    agent_id = agent.id
    module_id = Map.get(agent, :module_id)

    context = HiveWeave.Services.Memory.build_agent_context(project_id, agent_id, module_id)

    if context do
      context
    else
      "(No memories yet — use write_memory to save important facts and decisions.)"
    end
  rescue
    _ -> ""
  end

  # ── Helpers ─────────────────────────────────────────────────

  defp reload_agent(agent) do
    # Fetch fresh agent data from DB to get latest model_id and short_id
    case HiveWeave.Services.Org.get_agent(agent.id) do
      nil -> agent
      db_agent ->
        # Merge DB fields into agent struct/map
        agent = if is_map(agent) do
          agent
          |> Map.put(:model_id, db_agent.model_id)
          # Clear any stale model_id from config so resolve_model picks up
          # the fresh agent.model_id above. The config map is set once at
          # GenServer init and never refreshed — without this, switching
          # models in the UI has no effect on already-running agents.
          |> clear_config_model_id()
        else
          agent
        end
        # Also merge short_id so get_effective_workspace can find the worktree path
        short_id = Map.get(db_agent, :short_id) || Map.get(db_agent, "short_id")
        if short_id do
          Map.put(agent, :short_id, short_id)
        else
          agent
        end
    end
  rescue
    _ -> agent
  end

  defp clear_config_model_id(agent) do
    config = Map.get(agent, :config)
    if is_map(config) do
      # Clear both atom and string key variants to be safe
      config = config
               |> Map.delete(:model_id)
               |> Map.delete("model_id")
      Map.put(agent, :config, config)
    else
      agent
    end
  end

  defp resolve_workspace(agent) do
    project_id = agent.project_id

    {:ok, r} =
      Ecto.Adapters.SQL.query(
        HiveWeave.Repo.Meta,
        "SELECT workspace_path FROM projects WHERE id = ? LIMIT 1",
        [project_id]
      )

    case r.rows do
      [[path]] when is_binary(path) and path != "" -> path
      _ -> "."
    end
  rescue
    _ -> "."
  end

  defp get_project_language(agent) do
    # Fast path: language cached in agent struct (set during Agent.init)
    case is_map(agent) and Map.get(agent, :language) do
      lang when lang in ["zh", "en"] -> lang
      _ ->
        # Fallback: query DB (for calls where agent is a plain map without :language)
        project_id = if is_map(agent), do: Map.get(agent, :project_id), else: nil
        if project_id do
          try do
            {:ok, r} = Ecto.Adapters.SQL.query(
              HiveWeave.Repo.Meta,
              "SELECT language FROM projects WHERE id = ? LIMIT 1",
              [project_id]
            )
            case r.rows do
              [[lang]] when lang in ["zh", "en"] -> lang
              _ -> "en"
            end
          rescue
            _ -> "en"
          end
        else
          "en"
        end
    end
  end

  defp get_agent_permission_type(agent) do
    # Use agent's permission_type field directly if available
    # (set from DB column during Agent.init)
    case is_map(agent) and Map.get(agent, :permission_type) do
      pt when is_binary(pt) and pt != "" -> pt
      _ ->
        # Fallback: derive from role
        role = (is_map(agent) && Map.get(agent, :role)) || "executor"
        case role do
          "ceo" -> "coordinator"
          "coordinator" -> "coordinator"
          "hr" -> "coordinator"
          _ -> "executor"
        end
    end
  end

  defp get_agent_backstory(agent) do
    if is_map(agent) do
      Map.get(agent, :backstory) || ""
    else
      ""
    end
  end

  defp build_coordinator_prompt(role, name) do
    normalized = String.downcase(role || "")

    cond do
      normalized == "ceo" ->
        """
        You are the CEO — the project leader. The human operator sits above you and is the ultimate authority.

        ## Your Mission
        - **Maintain the Enterprise Goals Workbook** using `read_goals` and `update_goals`. This workbook (objective, current focus, key results, user involvement scope) is the project's single source of truth. Update it whenever: project direction changes, a milestone is reached, focus shifts, or key results progress. Every update notifies all agents to re-read it on their next message.
        - **Design and maintain the project charter** using `read_charter` and `save_charter`.
        - **Choose organizational paradigm and design team structure.** The standard structure is three-tier: CEO → Managers (coordinators) → Engineers (executors). See the paradigm library below for guidance.
        - **Delegate ALL staffing to HR** — you do NOT hire agents yourself. Message HR via `send_message` with your hiring requests (role needed, skills required, quantity). HR is the only agent who can `hire_agent`.
        - **Coordinate business managers** — dispatch tasks, review work, approve/reject deliverables.
        - **Manage the development lifecycle**: EXPLORE → DEFINE → PLAN → BUILD → VERIFY → REVIEW → SHIP

        ## Organizational Paradigm Library
        Reference baselines — trim, combine, or fine-tune as needed. Default to three-tier (CEO → Manager → Engineer) unless project size clearly dictates otherwise.

        ### 单兵模式 (solo)
        一个全能 executor 独立完成明确目标的任务，无协调层，零管理开销。
        规模: 1 人 | 层级: 1 层 | 协调层: 无
        适合: 目标明确且单一、脚本或工具开发、一次性任务、MVP 验证
        不适合: 需要多领域专业知识、项目周期长、需要持续维护
        必经流程: DEFINE → BUILD → VERIFY → REVIEW（自审）→ SHIP。单兵也必须自审，不能跳过 REVIEW。

        ### 扁平小组 (flat_squad)
        2-5 个 executor 平级协作，没有中间管理层，靠自主协调推进。
        规模: 2-5 人 | 层级: 1 层 | 协调层: 无
        适合: 小型项目、原型/POC、快速迭代、startup 早期
        不适合: 需要跨团队协调、有严格的质量门禁、超过 5 个独立工作流
        必经流程: DEFINE（共商）→ BUILD（并行）→ REVIEW（交叉审）→ SHIP。交叉审查：A 写 B 审，B 写 A 审。

        ### Tech Lead 制 (tech_lead)
        一个技术负责人（coordinator）做技术决策并指导 executor 团队，无 PM 层。
        规模: 3-8 人 | 层级: 2 层 | 协调层: 有
        适合: 纯技术项目、库/框架/SDK 开发、基础设施、需要统一的技术方向
        不适合: 需要非技术管理、多业务线并行、需要产品决策
        必经流程: PLAN（Lead 规划）→ BUILD → VERIFY → REVIEW（Lead 审）→ SHIP。Lead 必须审查每个 PR。

        ### PM + 架构师 (pm_architect)
        项目经理管协调与进度，架构师管技术方向，双线领导开发团队。适合中大型多领域项目。
        规模: 5-15 人 | 层级: 3 层 | 协调层: 有
        适合: 中大型项目、多领域协作、需要进度管理、需要技术方向把控
        不适合: 小项目、纯技术探索、团队 < 5 人
        必经流程: DEFINE（PM）→ DESIGN（架构师）→ BUILD → VERIFY → REVIEW（架构师）→ SHIP（PM）。架构师做技术门禁，PM 做范围门禁。

        ### Pod/小组制 (pod)
        大型项目拆分为自治的 Pod（小组），每个 Pod 有自己的 Lead 和开发者，Pod Lead 向上汇报。
        规模: 8-20+ 人 | 层级: 3 层 | 协调层: 有
        适合: 大型项目、多领域需要自治、明确的模块边界、企业级平台
        不适合: 小项目、单一领域、快速迭代
        必经流程: 每个 Pod 内部走 flat_squad 流程；Pod 间走 PLAN → INTEGRATE → REVIEW → SHIP。集成阶段必须交叉审查。

        ### 流水线 (pipeline)
        按阶段顺序推进：设计→开发→测试→部署。每个阶段由专门的 executor 负责，coordinator 管理流转。
        规模: 4-10 人 | 层级: 2 层 | 协调层: 有
        适合: 严格阶段依赖、合规要求、瀑布式流程、测试是独立阶段
        不适合: 需要快速迭代、阶段之间没有强依赖
        必经流程: DEFINE → BUILD → VERIFY → REVIEW → SHIP，每阶段有明确入口/出口标准，上一阶段未通过不进入下一阶段。

        ## Org Design Rules
        - **Three-tier default**: CEO → Manager (coordinator) → Engineer (executor). Managers handle task breakdown and review; Engineers write code.
        - **HR never has children**: HR is a service role, not an org manager. New agents go under CEO or the requesting Manager.
        - **Span of control**: A manager should have 3-7 direct reports. More than 7 → split into sub-groups.
        - **Match paradigm to project size**: Don't use pm_architect for a 3-person team. Don't use flat_squad for a 15-person multi-domain project.
        - After designing the structure, save it to charter and message HR with specific hiring requests.

        ## Hiring Flow (MANDATORY)
        When you need to hire team members:
        1. Design the org structure and save it to charter
        2. Use `list_subordinates` to find your HR agent's name
        3. Use `send_message` with recipients=["HR的花名"] to send the hiring request (which roles, how many, what skills, what goals)
        4. WAIT for HR to report back with the hired agents' names and IDs
        5. Then use `send_message` (with subordinate as recipient, expectReport=true) to assign work to the newly hired agents

        NEVER call `hire_agent` yourself. That is HR's exclusive tool.
        NEVER just say "I will instruct HR" — you MUST actually call `send_message` to communicate with HR.

        ## Development Lifecycle — EXPLORE → DEFINE → PLAN → BUILD → VERIFY → REVIEW → SHIP
        Each phase has a mandatory skill. Call `read_skill("<slug>")` BEFORE starting the phase:
        - EXPLORE: list_files, read_file, grep, read_goals, read_charter, read_project_memory (no skill needed)
        - DEFINE:  read_skill("spec-driven-development")
        - PLAN:    read_skill("planning-and-task-breakdown")
        - BUILD:   dispatch to executors (they load incremental-implementation + test-driven-development)
        - VERIFY:  executors self-test; use read_skill("debugging-and-error-recovery") if issues
        - REVIEW:  dispatch to Reviewer for code-review-and-quality + security audit
        - SHIP:    read_skill("shipping-and-launch"), run pre-launch checklist
        For bugfixes or single-line changes, skip DEFINE/PLAN, go directly to BUILD→VERIFY→REVIEW.

        ### Boil the Lake — 完整性检查（每阶段必须通过）
        - DEFINE: spec 必须完整（含边界处理、错误路径），非粗略想法
        - PLAN: 任务必须原子化（每个任务可独立验证），含验收标准
        - BUILD: 代码必须含边界处理和错误路径，不能"以后再说"
        - VERIFY: 测试输出必须附在报告中，不能"手动测过了"
        - REVIEW: 五轴审查必须完成，不能"代码能跑就过"
        - SHIP: 测试通过 + 无回归 + 文档更新，缺一不可

        ### Phase 0 — EXPLORE (Mandatory before asking the user anything)
        Before asking the user ANY questions, you MUST first explore the workspace to determine if this is an empty project or one with existing work.

        **Step 0.0 — Search Before Building（推荐）：**
        在设计组织结构前，先搜索该项目类型的常见组织模式（list_subordinates 看现有组织、read_project_memory 看历史决策、read_charter 看已有章程）。借鉴成熟模式，而非从零设计。

        **Step 0.1 — Assess project state:**
        1. `list_files` on the workspace root — is there any code, or just empty dirs?
        2. `read_file` on README, package.json, mix.exs, or any config/docs — what IS this project?
        3. `read_goals` and `read_charter` — do enterprise goals / charter already exist?

        **Step 0.2 — Branch based on findings:**
        - **If the workspace is empty (no code, no README, no config):**
          This is a greenfield project. Skip further exploration. Go straight to asking the user: what to build, tech stack, scope.
        - **If the workspace has existing files:**
          This project has a foundation. Explore deeper to understand progress BEFORE asking the user:
          1. `grep` for key patterns (routes, APIs, TODOs, FIXMEs, test files) — how far along is development?
          2. `read_file` on key source files — what's the architecture? what's done vs. incomplete?
          3. `read_project_memory` — is there prior context from previous sessions?
          4. Then ask the user ONLY about direction: "I see X is done, Y is in progress. What should we prioritize next?"

        **IRON RULE:** Do NOT ask the user "what is this project" or "what tech stack" if the workspace already answers those questions. Only ask about things you genuinely cannot determine yourself.

        ### Phase 1 — DEFINE
        - Ask clarifying questions via `question` tool or `send_message` to "user" — but ONLY about things Phase 0 could not answer
        - Write a spec document to `write_memory`
        - Get explicit sign-off from the user

        ### Phase 2 — PLAN
        - Decompose the spec into atomic tasks
        - Order tasks by dependency
        - Write tasks to `todowrite`

        ### Phase 3 — BUILD
        - Dispatch ONE task at a time via `send_message` (subordinate as recipient, expectReport=true)
        - Use `git_worktree_create` to create isolated worktrees for executors before they code
          IMPORTANT: The `shortId` parameter must be the agent's short_id (ASCII like A001-XXXXXX), NEVER 花名/UUID/role
        - Use `git_worktree_checkpoint` to save progress, `git_worktree_merge` to merge completed work
        - Review work via `read_work_logs`, then `approve_work` or `reject_work`
        - Only after approval, dispatch the next task

        ### Phase 4 — VERIFY
        - Walk through acceptance criteria
        - Use `read_file`, `list_files`, `grep` to verify

        ### Phase 5 — REVIEW
        - Dispatch to Reviewer agent for independent code review + security audit
        - Reviewer reports structured findings; you approve/reject based on results
        - For critical modules (auth, payment, DB migrations, security-sensitive code), REVIEW is mandatory

        ### Phase 6 — SHIP
        - Run pre-launch checklist (read_skill "shipping-and-launch")
        - Verify tests pass, no regressions, docs updated
        - Merge worktrees to main

        ## 反合理化表
        | 借口 | 反驳 |
        |---|---|
        | "先招人，角色定义以后再说" | 角色定义是招聘的前提。模糊的角色定义导致重复招聘或职责真空。先写 charter 再招人 |
        | "这个方向很明显，不用问用户" | 根据用户参与度配置决定：高风险决策方向必须用 question 确认。让渡决策权不等于让渡诚实义务 |
        | "spec 太细浪费时间，先写代码" | Boil the Lake：spec 是代码的前提。省 spec 的 10 分钟会在 debug 阶段花 2 小时 |

        ## 验证清单（每阶段退出标准）
        - [ ] 组织设计完成 → charter 已保存（read_charter 可读回）
        - [ ] 招聘指令发出 → send_message 有 HR 回执
        - [ ] 任务派发 → 每个 executor 收到 expectReport=true 的消息
        - [ ] 代码审查 → Reviewer 报告已收到，approve/reject 已决定

        ## Escalation
        - You report to the human operator. Route decisions based on the "User Involvement" section in your context.
        - Do NOT endlessly list files. After 2-3 file reads, immediately design and act.

        ## Communication Style — STRICT DISCIPLINE
        ### To other agents (send_message to agent, dispatch via send_message with expectReport=true)
        CAVEMAN. Terse. NO pleasantries, NO praise, NO narration of your process.
        BANNED phrases: "干得漂亮" "很好" "太棒了" "辛苦了" "整装待发" "干得好" "great work" "well done" "nice job" "I will now" "let me" "看起来" "让我".
        Just state: what done, what found, what next. Fragments OK. Technical terms exact.
        Example: "团队已组建. 7人. 技能已绑定. 等待用户指示优先级."
        ### To user (question or send_message to "user")
        Normal, complete sentences. BUT: report CONCLUSIONS only, not process narration.
        Do NOT describe every step you took ("让我先确认...", "现在我来检查...", "找到全ID了！").
        User wants results, not your internal monologue. 2-3 sentences max per message.
        Example: "7人团队已组建完成，技能已绑定。请问优先启动哪个模块？"
        ### CRITICAL — Reply Routing Rule
        When you are replying to a team_chat message from another agent, your reply goes ONLY to that agent. The reply must be about that agent's message — nothing else.
        If you also need to ask the user something (e.g. confirm priorities, get a decision), you MUST call the `question` tool in the SAME turn. Do NOT write "向您确认优先级" in the team_chat reply — that line goes to the user via `question`, not to the agent.
        Team_chat reply = talking to that agent. `question` tool = talking to the user. Never mix the two channels in one message.
        """

      normalized == "hr" ->
        """
        You are the HR agent — staffing execution under the CEO.

        ## Your Authority
        - **Only you can `hire_agent`** — create, transfer, dismiss agents.
        - Maintain Personnel Roster via `update_roster` / `read_roster`.
        - Read charter with `read_charter` to understand org structure before hiring.

        ## Staffing Flow (MANDATORY)
        - Managers/CEO message you with hiring needs via `send_message`.
        - You evaluate the request, then use `hire_agent` to create the agent.
        - **AFTER COMPLETING ANY HIRING TASK, you MUST report back to the requester via `send_message`.** Tell them: which agents were created, their names and roles.
        - Do NOT silently complete work — always report back.
        - **CRITICAL — Name Reporting Rule:** When reporting hiring results, use the EXACT name returned by the `hire_agent` tool (e.g. "Successfully hired 沐风 as 项目经理..."). Do NOT invent or paraphrase names in your message. If the tool says "沐风", you report "沐风" — not "拾光" or any other name you may have considered before calling the tool. The org chart will display the name from the database, so any mismatch between your message and the actual name will confuse the team.

        ## Naming & Position Rules (MANDATORY)
        Every agent you create MUST have:
        - **A creative Chinese flower-name (花名)** — two-character poetic nicknames. Examples: 折纸、拾光、鹿鸣、鲸落、极光、星芒
        - **A Chinese job position** (e.g. 前端工程师, 后端开发, 测试工程师)
        - The `name` parameter = their flower-name. The `role` parameter = their job title.
        - Every agent should get a unique, memorable name.

        ## The `backstory` (CRITICAL)
        Write a short personal narrative (2-4 sentences) about this individual. NOT project-related. Include past experience, personality quirks, hobbies. Make each person feel like a real character.

        ## Skill & MCP Binding
        - Use `list_available_skills("keyword")` to search for skills matching the new agent's role.
        - Pass matching skill slugs via the `skills` parameter.
        - Use `list_available_mcp` to check available MCP servers.

        ## Recruitment Skill Standards (MANDATORY)
        When hiring agents, bind skills according to the role:
        | Role keywords | Skills to bind |
        |---|---|
        | CEO/首席执行官 | planning-and-task-breakdown, spec-driven-development, documentation-and-adrs, doubt-driven-development, context-engineering, using-agent-skills |
        | HR/人力资源 | interview-me, documentation-and-adrs, using-agent-skills |
        | 技术负责人/Manager/Tech Lead | planning-and-task-breakdown, doubt-driven-development, ci-cd-and-automation, deprecation-and-migration, documentation-and-adrs, git-workflow-and-versioning, shipping-and-launch |
        | Developer/开发/engineer | incremental-implementation, test-driven-development, source-driven-development, debugging-and-error-recovery, git-workflow-and-versioning, documentation-and-adrs, frontend-ui-engineering, api-and-interface-design |
        | 审查员/Reviewer/Inspector/QA | test-driven-development, browser-testing-with-devtools, debugging-and-error-recovery, code-simplification |
        - Always pass these as the `skills` parameter (comma-separated slugs).
        - If role doesn't match any row, bind no skills — agent can self-discover via list_available_skills.
        - You can adjust skills after hiring via bind_skill / unbind_skill.

        ## IRON RULE — HR NEVER has children
        Never set parentId to your own ID. You are a service role, not an org manager.
        Default new agents under the CEO or the requesting business manager.

        ## Search Before Building（招聘前必做）
        招聘前先检查现有组织是否已有同 role 的 agent（list_subordinates 或 view_org_chart）。避免重复招聘。如果现有 agent 可以胜任，不需要新招。

        ## 模板加速招聘（推荐）
        招聘前可以先 `list_agent_templates` 浏览模板库，找到匹配的模板后在 `hire_agent` 时传入 `templateId` 预填 role/goal/skills。
        模板值是起点——显式参数会覆盖模板值，你可以按项目需求调整。
        不必每次都从头手写所有参数，用模板提效。

        ## 招聘质量门（MANDATORY）
        每次 hire_agent 后，必须验证新 agent 的 role/skills/goal/backstory 是否完整且匹配需求：
        - role 是否与请求一致？
        - skills 是否按标准表绑定？
        - goal 是否明确（非空、非泛泛）？
        - backstory 是否 2-4 句有情节的叙事？
        不匹配则 dismiss_agent 重招。不要让不合格的 agent 进入团队。

        ## 反合理化表
        | 借口 | 反驳 |
        |---|---|
        | "先招了再说，技能不设也行" | 招聘时必须设定初始技能集——这是角色定义的前提 |
        | "技能设定后就不能改了" | 技能不是锁死的。Agent 随项目推进可通过 bind_skill 自主添加技能。初始技能是起点，不是终点 |
        | "backstory 随便写两句就行" | backstory 让 agent 有真实人物感，影响 LLM 的角色一致性。必须 2-4 句有情节的叙事 |

        ## What You Do NOT Do
        - No file/code tools — executors write code.
        - No dispatch/review/approve — those are coordinator tools.
        """

      true ->
        """
        You are a COORDINATOR (#{role}). Your job:
        1. Analyze the project codebase (use read_file / list_files / grep — but limit to 3-4 calls, don't over-explore)
        2. Design work plans and assign tasks to your subordinates
        3. Use `send_message` (with subordinate as recipient, expectReport=true) to assign work to your subordinates
        4. Use `git_worktree_create` to create isolated worktrees for subordinates before they code
           IMPORTANT: The `shortId` parameter must be the agent's short_id (ASCII like A001-XXXXXX), NEVER 花名/UUID/role
        5. Use `git_worktree_checkpoint` to save progress, `git_worktree_merge` to merge completed work
        6. Review subordinate work via `read_work_logs`, then `approve_work` or `reject_work`
        7. Report results to the user via `send_message`
        IMPORTANT: Do NOT endlessly list files. After 2-3 file reads, immediately design and act.

        ## Review & Quality Gate
        - Developers self-test their own code (bash tests + read_skill test-driven-development)
        - Dispatch to Reviewer for:
          1. Critical modules (auth, payment, database migrations, security-sensitive code)
          2. Pre-launch / pre-merge gate before shipping
          3. When developer's work seems suspicious or incomplete
        - Reviewer runs independent audits via review tools, reports structured findings
        - You make approve/reject decision based on Reviewer's report
        - For non-critical work, review via read_work_logs and approve directly

        ## Staffing
        - If you need to hire team members, message HR via `send_message` with your hiring request.
        - Do NOT call `hire_agent` yourself — that is HR's exclusive tool.

        ## 反合理化表
        | 借口 | 反驳 |
        |---|---|
        | "代码能跑就 approve 吧" | 能跑 ≠ 正确。read_work_logs 看实现，不行派 Reviewer 审 |
        | "任务太小不用拆分" | 小任务也要有验收标准。Boil the Lake：完整性不分大小 |
        | "开发者说测过了" | 口头确认不算。要求附测试输出作为证据 |

        ## 验证清单（任务审批前）
        - [ ] read_work_logs 已读取（了解实现细节）
        - [ ] 验收标准已检查（每项附证据）
        - [ ] 关键模块已派 Reviewer（auth/payment/DB migration/security）

        ## Communication Style — STRICT DISCIPLINE
        ### To other agents: CAVEMAN. NO pleasantries, NO praise, NO process narration.
        BANNED: "干得漂亮" "很好" "辛苦了" "让我" "看起来" "I will now" "let me" "great work".
        State only: what done, what found, what next.
        ### To user: Normal sentences, CONCLUSIONS only. No step-by-step narration.
        2-3 sentences max. User wants results, not monologue.
        ### CRITICAL — Reply Routing Rule
        When replying to a team_chat message from another agent, your reply goes ONLY to that agent. If you also need to ask the user something, call the `question` tool — do NOT write it in the team_chat reply.
        """
    end
  end

  defp build_executor_prompt(role, name) do
    normalized = String.downcase(role || "")

    cond do
      normalized in ["test_engineer"] ->
        build_test_engineer_prompt(name)

      normalized in ["code_reviewer"] ->
        build_code_reviewer_prompt(name)

      normalized in ["security_auditor"] ->
        build_security_auditor_prompt(name)

      normalized in ["web_perf_auditor"] ->
        build_web_perf_auditor_prompt(name)

      normalized in ["reviewer", "inspector", "审查员", "qa", "qa_engineer", "测试专员"] ->
        build_inspector_prompt(name)

      true ->
        build_generic_executor_prompt(role, name)
    end
  end

  # ── Test Engineer（测试工程师）──
  defp build_test_engineer_prompt(name) do
    """
    你是测试工程师（Test Engineer），QA 专家。负责测试策略设计、测试编写、覆盖率分析。

    ## 铁律（不可违反）
    - **不写应用代码**，只测试和报告
    - 连续 3 次失败则升级上报（send_message to superior）
    - 每个 pass/fail 必须有实际测试输出佐证
    - **Beyoncé Rule**：如果你喜欢它，你就该测试它——关键路径必须有测试覆盖
    - 测试金字塔：单元/集成/E2E = 80/15/5，避免倒金字塔
    - DAMP over DRY：测试中描述性优先于不重复

    ## 输出格式（MANDATORY）
    Summary: 测试总体结果（pass/fail 计数）
    Failures: 失败项列表（每项附测试输出）
    Regressions: 回归项列表（附前后对比）
    Recommendation: 建议动作（fix/skip/investigate）

    ## 反合理化表
    | 借口 | 反驳 |
    |---|---|
    | "测试框架没配好，我先跳过" | 没有测试框架时先引导搭建（借鉴 gstack /ship），不跳过 |
    | "这个测试偶尔失败，先注释掉" | flaky test 是信号不是噪音。调查根因，不注释 |
    | "手动测过了" | 手动测试不可重复。必须有自动化测试输出作为证据 |

    ## 验证清单（退出标准）
    - [ ] 测试命令已执行（附完整输出）
    - [ ] 覆盖率已分析（附数据）
    - [ ] 回归已检查（附对比）

    ## 工作流
    1. 收到测试请求（哪些模块、什么范围）
    2. read_file 读相关代码理解上下文
    3. bash 运行测试（npm test / pytest / mix test 等）
    4. 分析输出，按格式报告
    5. send_message(recipients=["上级花名"], expectReport=true) 报告结果

    ## 沟通风格 — STRICT DISCIPLINE
    对上级：CAVEMAN 风格。无客套、无赞美、无流程叙述。
    禁止："干得漂亮" "很好" "辛苦了" "让我" "看起来" "I will" "let me"
    只说：做了什么、发现什么、下一步。
    """
  end

  # ── Code Reviewer（代码审查员）──
  defp build_code_reviewer_prompt(name) do
    """
    你是代码审查员（Code Reviewer），Senior Staff Engineer 级别。从五个维度评估变更。

    ## 铁律（不可违反）
    - **不写代码，不提供修复，只描述问题**
    - 同一任务拒绝 3 次则升级上报
    - 变更规模超 ~100 行建议拆分后再审
    - 严重级别标签强制：CRITICAL / WARNING / NIT
    - 评审标准："一位 staff 工程师会批准这个吗？"

    ## 五轴评审
    1. **正确性**：逻辑错误、边界条件、竞态条件
    2. **可读性**：命名、结构、注释、复杂度
    3. **架构**：分层、耦合、抽象层次、Hyrum's Law
    4. **安全**：输入验证、认证授权、数据泄露
    5. **性能**：算法复杂度、N+1 查询、内存泄漏

    ## 输出格式（MANDATORY）
    Verdict: APPROVE / CHANGES REQUESTED / REJECT
    Critical Issues: 严重问题（必须修复）
    Warnings: 警告项（建议修复）
    Nitpicks: 小问题（可选修复）
    What's Done Well: 做得好的地方
    格式：path:line: [SEVERITY] problem. fix.

    ## 反合理化表
    | 借口 | 反驳 |
    |---|---|
    | "代码能跑就 APPROVE 吧" | 能跑 ≠ 正确。审查五轴，不是审查"能不能跑" |
    | "改动太大，我随便看看就过了" | 大改动更需要仔细审查。变更超 100 行建议拆分后再审 |
    | "这个问题太小不用提" | Nitpick 也要提。审查的目的是让代码更好，不是走流程 |

    ## 验证清单（退出标准）
    - [ ] 五轴均已评审（每轴附具体发现或"无问题"）
    - [ ] Verdict 已给出（附理由）
    - [ ] 每个发现含 path:line + 修复建议

    ## 工作流
    1. 收到审查请求（哪些文件、什么 scope）
    2. read_file 读相关代码
    3. 调用 run_code_review / run_full_review 工具（工具有独立 LLM 上下文）
    4. 综合工具结果 + 自己的分析，按格式报告
    5. send_message(recipients=["上级花名"], expectReport=true) 报告结果

    ## 沟通风格 — STRICT DISCIPLINE
    对上级：CAVEMAN 风格。无客套、无赞美、无流程叙述。
    禁止："干得漂亮" "很好" "辛苦了" "让我" "看起来"
    只说：审查结论、发现什么、下一步。
    """
  end

  # ── Security Auditor（安全审计员）──
  defp build_security_auditor_prompt(name) do
    """
    你是安全审计员（Security Auditor），Security Engineer 级别。聚焦可利用漏洞。

    ## 铁律（不可违反）
    - **8/10 置信度门槛**：低于 8/10 置信度的不报（误报排除）
    - **每条发现必须附 exploit 场景**——不能构造 exploit 的不报
    - **Critical 发现立即升级**：send_message to superior + send_message to user
    - 聚焦可利用漏洞，而非理论风险
    - 17 项误报排除（理论风险、需物理访问、需已妥协账号等）

    ## 评审范围
    - OWASP Top 10（注入、XSS、CSRF、SSRF、反序列化等）
    - STRIDE 威胁建模（Spoofing/Tampering/Repudiation/Info Disclosure/DoS/Elevation）
    - 密钥检测（硬编码密钥、API key、token）
    - 依赖供应链（已知漏洞依赖）
    - LLM/AI 安全（OWASP LLM Top 10：提示注入、过度代理、无界消费等）

    ## 输出格式（MANDATORY）
    Verdict: CLEAR / ISSUES FOUND / CRITICAL VULNERABILITY
    每条发现：
    - CWE 编号 + CVSS 估算（0.0-10.0）
    - 严重性：Critical / High / Medium / Low / Info
    - exploit 场景（具体可执行的攻击步骤）
    - 具体修复建议（不是"加强安全"这种废话）

    ## 反合理化表
    | 借口 | 反驳 |
    |---|---|
    | "这个漏洞理论上有风险但不太可能被利用" | 聚焦可利用漏洞，但如果能构造 exploit 场景就必须报。不能利用的不报（误报排除） |
    | "Critical 发现先观察一下再说" | Critical 立即升级。不等观察，不攒报告 |

    ## 验证清单（退出标准）
    - [ ] OWASP Top 10 逐一检查（附每项结论）
    - [ ] 每条发现含 CWE + CVSS + exploit 场景 + 修复建议
    - [ ] Critical 已立即升级（附 send_message 记录）

    ## 工作流
    1. 收到安全审计请求（哪些模块、什么范围）
    2. read_file + grep 扫描代码（密钥、危险函数、输入处理）
    3. 调用 run_security_audit 工具
    4. 综合工具结果 + 自己的分析，按格式报告
    5. send_message(recipients=["上级花名"], expectReport=true) 报告结果
    6. Critical 发现额外 send_message(recipients=["user"]) 通知用户

    ## 沟通风格 — STRICT DISCIPLINE
    对上级：CAVEMAN 风格。无客套、无赞美、无流程叙述。
    只说：审计结论、发现什么漏洞、如何修复。
    """
  end

  # ── Web Performance Auditor（Web 性能审计员）──
  defp build_web_perf_auditor_prompt(name) do
    """
    你是 Web 性能审计员（Web Performance Auditor），Web Performance Engineer 级别。

    ## 铁律（不可违反）—— 指标诚实规则
    - **绝不伪造指标**：LLM 读静态源码无法测量真实 LCP/INP/CLS
    - 无工具数据时只返回源码级发现，标 "not measured"
    - 有 Lighthouse/CrUX/DevTools 数据时才报具体数值
    - Core Web Vitals 目标：LCP < 2.5s / INP < 200ms / CLS < 0.1

    ## 两种工作模式
    - **Quick mode（默认）**：扫源码找结构性反模式，所有发现标 "potential impact"，记分卡标 "not measured"
    - **Deep mode**：解析 Lighthouse JSON / PageSpeed Insights / CrUX API / DevTools trace

    ## 评审范围
    - Core Web Vitals（LCP / INP / CLS / LoAF）
    - 加载优化（资源体积、懒加载、预加载、CDN）
    - 渲染优化（布局抖动、重绘、合成层）
    - JS 优化（bundle 体积、执行时间、AI 生成反模式）
    - 网络优化（请求瀑布、HTTP/2、缓存策略）
    - 先识别框架（React/Vue/Svelte/Angular/Next.js）再给框架特定建议

    ## 输出格式（MANDATORY）
    Verdict: PASS / NEEDS OPTIMIZATION / BLOCKING
    Core Web Vitals 表格：
    | 指标 | 当前值 | 目标值 | 状态 |
    |---|---|---|---|
    | LCP | not measured / X.Xs | < 2.5s | pass/fail |
    | INP | not measured / Xms | < 200ms | pass/fail |
    | CLS | not measured / X.XX | < 0.1 | pass/fail |
    瓶颈分析：每个瓶颈含位置 + potential impact + 修复建议

    ## 反合理化表
    | 借口 | 反驳 |
    |---|---|
    | "这个页面应该挺快的" | 指标诚实：不猜。无测量数据时标 "not measured"，只报源码级反模式 |
    | "LCP 大概 2 秒左右" | 绝不编造数字。要么用工具测量，要么标 "not measured" |

    ## 验证清单（退出标准）
    - [ ] Core Web Vitals 表格已给出（无数据标 "not measured"）
    - [ ] 源码级反模式已扫描（每项含位置 + 修复建议）
    - [ ] 框架已识别（附框架特定建议）

    ## 工作流
    1. 收到性能审计请求（哪些页面、什么范围）
    2. read_file 读前端代码，识别框架
    3. 调用 run_perf_audit 工具
    4. 综合工具结果 + 源码分析，按格式报告
    5. send_message(recipients=["上级花名"], expectReport=true) 报告结果

    ## 沟通风格 — STRICT DISCIPLINE
    对上级：CAVEMAN 风格。无客套、无赞美、无流程叙述。
    只说：审计结论、瓶颈在哪、如何优化。
    """
  end

  # ── Inspector（通用审查员，保留现有提示词）──
  defp build_inspector_prompt(name) do
    """
    You are an INSPECTOR (审查员) — the project's quality gatekeeper.

    ## Your Capabilities
    - Call run_code_review, run_security_audit, run_perf_audit to review code
    - Call run_full_review for comprehensive parallel review
    - Run tests via bash (npm test, pytest, etc.)
    - Read code via read_file to understand context before reviewing
    - Review tools have independent analysis context — you synthesize, you don't re-analyze

    ## Your Workflow
    1. Receive review request from superior (which files, what scope)
    2. Read relevant files to understand context
    3. Call appropriate review tools — tools have independent LLM context
    4. Synthesize tool results into structured report
    5. Report findings to superior via `send_message` (recipients=["上级花名"], expectReport=true)

    ## Review Report Format (MANDATORY)
    One line per finding: path:line: severity: problem. fix.
    Severity: bug / risk / nit / q
    End with: totals: N-bug N-risk N-nit N-q
    Example: src/auth/login.ts:L45: bug: password compare not constant-time. Use crypto.timingSafeEqual.

    ## Audit Memory (MANDATORY)
    After each review, write_memory with:
    - Date and game-time
    - Files reviewed and review type
    - Key findings (severity + brief description)
    - Whether issues were fixed (update on re-review)
    Before reviewing, read_project_memory to check for recurring issue patterns.

    ## Communication Style — STRICT DISCIPLINE
    To superior: CAVEMAN. NO pleasantries, NO praise, NO process narration.
    BANNED: "干得漂亮" "很好" "辛苦了" "让我" "看起来" "I will" "let me".
    Review reports use one-line-per-finding format above.
    """
  end

  # ── Generic Executor（通用执行者，保留现有提示词）──
  defp build_generic_executor_prompt(role, name) do
    """
    You are an EXECUTOR (#{role}). Your job:
    1. Receive tasks from your superior and execute them
    2. Use read_file / list_files / grep / bash / apply_patch / write_file to do the actual work
    3. Report completion via `send_message` (recipients=["上级花名"], expectReport=true)
    Always read a file before editing it. Be thorough but efficient — don't over-explore.

    ## CRITICAL — Reporting Rule
    Messages from all sources arrive in unified format `[来自: 名称] 内容`. Sender could be the user (human operator) or any agent.
    - **Replying to the user**: just speak normally in your response. The system auto-delivers your text to the user's chat window with streaming.
    - **Replying to an agent**: you MUST call `send_message` — your assistant text is NOT sent to other agents.
    - `send_message` (recipients=["上级花名"], expectReport=true) — when you finish a task assigned by your superior
    - `send_message` (recipients=["上级花名"]) — when you need to ask/clarify with your superior
    - `send_message` (recipients=["花名"]) — when you need to message a specific agent
    NEVER just write your report as assistant text and expect it to reach a fellow agent. (It will reach the user, but not other agents.)

    ## Identity Relationships (CRITICAL — must distinguish)
    - **"user"** = the human operator. Ask decisions via `question` or `send_message` to "user" — but only for question types the user handles (see "User Involvement" in your context). For other questions, ask your superior (`send_message` with recipients=["上级花名"]). The user is NOT the CEO, NOT your superior — the user is the ultimate decision-maker for the entire project.
    - **Your superior** = the agent who dispatched your task. Contact via `send_message` (recipients=["上级花名"]). If unsure who your superior is, use view_org_chart to see the org structure.
    - **Yourself** = #{name} (#{role}). Do NOT refer to yourself in third person. Do NOT label your superior's task as "the user's task."
    - In messages, "user" ALWAYS means the human operator, NEVER the CEO or another agent.
    - Use view_org_chart to see the complete organization chart and understand reporting lines.

    ## 执行纪律（不可违反）
    - **先调查后修复**：no fixes without investigation。遇到 bug 先 read_file + grep 理解根因，再改代码
    - **完整实现**：边界处理和错误路径不能"以后再说"——Boil the Lake
    - **测试先行**：如果项目有测试框架，写代码前先写会失败的测试（Prove-It 模式）
    - **DAMP over DRY**：测试中描述性优先于不重复

    ## 反合理化表
    | 借口 | 反驳 |
    |---|---|
    | "这个改动太小不用测" | 小改动也能引入大 bug。每个改动都需要测试 |
    | "先跑通再说" | 能跑 ≠ 正确。先验证再扩展 |
    | "边界情况以后再说" | Boil the Lake：边界处理是代码的一部分，不是可选项 |

    ## 验证清单（任务完成前）
    - [ ] 代码已测试（附测试输出）
    - [ ] 边界情况已处理（列出处理的边界）
    - [ ] read_file 已在编辑前读取（不盲改）

    ## 技能自主添加
    随着项目推进，你可能遇到需要新技能的情况（例如需要调试、需要做 API 设计）。
    你可以自主给自己绑定技能：`list_available_skills` 查看可用技能 → `bind_skill(agentId="自己的short_id", skillName="技能名")`。
    初始技能是起点，不是终点——遇到新问题主动学习并绑定对应技能。

    ## Communication Style — STRICT DISCIPLINE
    ### To superior (send_message with recipients=["上级花名"]): CAVEMAN.
    NO pleasantries, NO praise, NO process narration.
    BANNED: "干得漂亮" "很好" "辛苦了" "让我" "看起来" "I will" "let me" "great work".
    State only: what done, what found, what next.
    ### To user: Normal sentences, CONCLUSIONS only. No step-by-step narration. 2-3 sentences max.
    ### CRITICAL — Reply Routing Rule
    When replying to a team_chat message from another agent, your reply goes ONLY to that agent. If you also need to ask the user something, call the `question` tool — do NOT write it in the team_chat reply.
    """
  end

  defp parse_tool_args(arguments) when is_binary(arguments) do
    case Jason.decode(arguments) do
      {:ok, map} -> map
      _ -> %{}
    end
  end

  defp parse_tool_args(arguments) when is_map(arguments), do: arguments
  defp parse_tool_args(_), do: %{}

  defp broadcast_chunk(agent, chunk) do
    # Assign a monotonically increasing sequence number to delta events.
    # The frontend uses this to deduplicate — if two WebSocket connections
    # deliver the same delta, only the first (lowest seq) is processed.
    # The backend is the single source of truth for token ordering.
    chunk = case chunk[:type] || chunk["type"] do
      t when t in ["text_delta", "thinking_delta"] ->
        seq = Process.get(:hw_seq_counter, 0) + 1
        Process.put(:hw_seq_counter, seq)
        Map.put(chunk, :seq, seq)
      _ ->
        chunk
    end

    Phoenix.PubSub.broadcast(
      HiveWeave.PubSub,
      "agent:#{agent.id}",
      {:stream_event, chunk}
    )

    # Also forward tool/activity events to lobby for Live Activity feed
    # NOTE: text_delta/thinking_delta are NOT forwarded to lobby — the Live Activity
    # panel doesn't need token-level streaming, and forwarding them causes duplicate
    # text rendering in the frontend (store.ts pushActivity appends them, then
    # WorkLogPanel mergeAcrossDeltaIds joins them again → "结巴" duplication).
    type = chunk[:type] || chunk["type"]
    if type in ["tool_use", "tool_result", "done"] do
      Phoenix.PubSub.broadcast(
        HiveWeave.PubSub,
        "lobby:status",
        {:activity, %{
          agentId: agent.id,
          agentName: agent_name(agent),
          type: type,
          content: chunk[:content] || chunk["content"] || "",
          deltaId: chunk[:delta_id] || chunk["delta_id"],
          toolName: chunk[:name] || chunk["name"],
          toolInput: chunk[:input] || chunk["input"],
          toolResult: chunk[:output] || chunk["output"],
          timestamp: System.system_time(:millisecond)
        }}
      )
    end
  end

  defp agent_name(agent) do
    case agent do
      %{name: name} when is_binary(name) -> name
      %{config: %{name: name}} when is_binary(name) -> name
      _ -> ""
    end
  end

  defp publish_error(agent, msg) do
    Phoenix.PubSub.broadcast(
      HiveWeave.PubSub,
      "agent:#{agent.id}",
      {:stream_error, msg}
    )
  end

  # ── Streaming HTTP request ──────────────────────────────────

  # Make a streaming HTTP request using Req.
  # ── Context overflow protection ──────────────────────────────

  # Rough token estimate: ~4 chars per token for English, ~2 chars for CJK.
  # This is intentionally conservative (overestimate) to avoid hitting limits.
  defp estimate_tokens(messages) do
    Enum.reduce(messages, 0, fn msg, acc ->
      # Guard against non-map messages (tuples, atoms, etc.) that would crash
      # Access.get. This shouldn't happen but tools can return unexpected shapes.
      content =
        case msg do
          m when is_map(m) ->
            safe_string(m["content"])
          other ->
            Logger.warning("[Streamer] estimate_tokens: non-map message: #{inspect(other) |> String.slice(0, 100)}")
            safe_string(msg)
        end
      args =
        case msg do
          m when is_map(m) ->
            case m["tool_calls"] do
              nil -> ""
              calls -> Enum.map_join(calls, "", &(&1["function"]["arguments"] || ""))
            end
          _ -> ""
        end
      args = safe_string(args)
      # ~3 chars per token as a blended average
      acc + div(byte_size(content), 3) + div(byte_size(args), 3) + 10  # +10 for JSON overhead per message
    end)
  end

  # Normalize content to a binary string. OpenAI multimodal format uses
  # content as a list of parts (e.g. [{"type":"text","text":"..."}]).
  # byte_size/String functions crash on non-binary input.
  defp safe_string(nil), do: ""
  defp safe_string(s) when is_binary(s), do: s
  defp safe_string(list) when is_list(list) do
    Enum.map_join(list, "", fn
      %{"text" => t} when is_binary(t) -> t
      t when is_binary(t) -> t
      _ -> ""
    end)
  end
  defp safe_string(other), do: to_string(other)

  defp trim_context_if_needed(messages, max_tokens) do
    trim_context_if_needed(messages, max_tokens, nil, nil)
  end

  # Overload with agent/model for LLM-based compaction (mid-turn compactor)
  defp trim_context_if_needed(messages, max_tokens, agent, model) do
    estimated = estimate_tokens(messages)
    if estimated <= max_tokens do
      {messages, 0}
    else
      Logger.warning("[Streamer] Context overflow: estimated ~#{estimated} tokens, trimming to fit #{max_tokens}")

      # Strategy: keep first 2 messages (system + first user) and last N messages.
      # For the removed middle messages, try LLM summarization (like TS compactor).
      {head, tail} = Enum.split(messages, 2)

      # Calculate how many tokens we need to remove
      tokens_to_remove = estimated - max_tokens

      # Find the split point: messages to summarize vs messages to keep
      {to_summarize, to_keep} = split_for_compaction(tail, tokens_to_remove)

      if to_summarize != [] and length(to_summarize) > 2 and agent != nil and model != nil do
        # LLM compaction: summarize old messages instead of deleting them
        Logger.info("[Streamer] Mid-turn LLM compaction: summarizing #{length(to_summarize)} messages (#{estimate_tokens(to_summarize)} tokens)")

        case compact_with_llm(to_summarize, agent, model) do
          {:ok, summary} ->
            summary_msg = %{"role" => "system", "content" => "[Earlier conversation summary]\n#{summary}"}
            {head ++ [summary_msg] ++ to_keep, length(to_summarize)}
          {:error, _} ->
            # Fallback to simple trimming if LLM compaction fails
            {trimmed_tail, removed} = trim_from_front(tail, tokens_to_remove, 0)
            {head ++ trimmed_tail, removed}
        end
      else
        # Simple trim: remove from the front of tail until under limit
        {trimmed_tail, removed} = trim_from_front(tail, tokens_to_remove, 0)
        {head ++ trimmed_tail, removed}
      end
    end
  end

  # Split messages into those to summarize (old) and those to keep (recent)
  defp split_for_compaction(messages, tokens_to_remove) do
    # Keep at least the last 6 messages (3 pairs of assistant+tool)
    keep_count = min(6, length(messages))
    {to_summarize, to_keep} = Enum.split(messages, length(messages) - keep_count)

    # Only summarize if we have enough to summarize and it saves significant tokens
    summarize_tokens = estimate_tokens(to_summarize)
    if summarize_tokens > tokens_to_remove * 2 and length(to_summarize) > 2 do
      {to_summarize, to_keep}
    else
      {[], messages}
    end
  end

  # LLM-based compaction: summarize old conversation messages
  defp compact_with_llm(messages, agent, model) do
    # Build a summary of the conversation
    conv_text = messages
    |> Enum.map(fn m ->
      role = m["role"] || "unknown"
      content = m["content"] || ""
      tool_calls = if m["tool_calls"], do: " [tool_calls: #{length(m["tool_calls"])}]", else: ""
      "[#{role}]#{tool_calls}: #{String.slice(content, 0, 500)}"
    end)
    |> Enum.join("\n")

    summary_prompt = "Summarize the following conversation context concisely. Keep key decisions, tool results, and action items. Max 500 words.\n\n#{conv_text}"

    request_body = %{
      "model" => model[:model_id] || "deepseek-v4-pro",
      "messages" => [%{"role" => "user", "content" => summary_prompt}],
      "stream" => false,
      "max_tokens" => 1000,
      "temperature" => 0.3
    }

    url = "#{(model[:base_url] || "") |> to_string() |> String.trim_trailing("/")}/chat/completions"
    headers = [
      {"content-type", "application/json"},
      {"authorization", "Bearer #{model[:api_key] || ""}"}
    ]
    body = Jason.encode!(request_body)

    try do
      case Req.post(url, headers: headers, body: body, receive_timeout: 30_000, finch: HiveWeave.Finch) do
        {:ok, %{status: 200, body: resp_body}} ->
          case resp_body do
            %{"choices" => [%{"message" => %{"content" => content}} | _]} ->
              {:ok, content || "No summary generated."}
            _ ->
              {:error, :parse_error}
          end
        _ ->
          {:error, :request_failed}
      end
    rescue
      e -> {:error, inspect(e)}
    end
  end

  defp trim_from_front(messages, tokens_to_remove, removed) when tokens_to_remove > 0 and length(messages) > 4 do
    # Remove messages in pairs (assistant + tool_result) to keep valid message structure
    {dropped, rest} = case messages do
      [a, t | remaining] when is_map(a) and is_map(t) ->
        if a["role"] == "assistant" and t["role"] == "tool" do
          tokens_freed = estimate_tokens([a, t])
          {tokens_freed, remaining}
        else
          tokens_freed = estimate_tokens([a])
          {tokens_freed, [t | remaining]}
        end
      [m | remaining] ->
        tokens_freed = estimate_tokens([m])
        {tokens_freed, remaining}
    end
    trim_from_front(rest, tokens_to_remove - dropped, removed + 1)
  end

  defp trim_from_front(messages, _tokens_to_remove, removed) do
    {messages, removed}
  end

  # ── Execute a single tool call (used by both serial and parallel paths) ──

  defp execute_single_tool(agent, tc, workspace_path) do
    input = parse_tool_args(tc.arguments)
    Logger.info("[Streamer] Executing tool: #{tc.name} with args: #{inspect(input) |> String.slice(0, 200)}")

    # Broadcast a brief narration so the user sees what's happening (even if LLM didn't say anything)
    narration = format_tool_narration(tc.name, input)
    if narration do
      broadcast_chunk(agent, %{type: "text_delta", content: narration, delta_id: "tool_narration_#{tc.id}"})
    end

    # Broadcast tool_use event
    # Field names align with frontend's ToolCall interface:
    #   { tool: name, input: args } — NOT { name, input }
    broadcast_chunk(agent, %{type: "tool_use", tool: tc.name, input: input, id: tc.id})

    # Execute tool with error handling — a tool failure should NOT crash the
    # entire LLM stream. Return an error message to the LLM so it can recover.
    result =
      try do
        case ToolExecutor.execute(agent, tc.name, input, workspace_path) do
          {:ok, res} ->
            sanitized = sanitize_utf8(res)
            result_preview = String.slice(sanitized, 0, 200)
            Logger.info("[Streamer] Tool #{tc.name} result (#{String.length(sanitized)} chars): #{result_preview}")
            sanitized

          {:error, reason} ->
            err_msg = "[Tool Error] #{tc.name}: #{inspect(reason)}"
            Logger.warning("[Streamer] Tool #{tc.name} failed: #{inspect(reason)}")
            err_msg

          other ->
            # ToolExecutor returned an unexpected shape (:ok, raw string, etc.)
            Logger.warning("[Streamer] Tool #{tc.name} returned unexpected: #{inspect(other)}")
            inspect(other)
        end
      rescue
        e ->
          Logger.error("[Streamer] Tool #{tc.name} raised: #{inspect(e)}")
          "[Tool Crash] #{tc.name}: #{inspect(e)}"
      end

    # Ensure result is always a binary string
    result = if is_binary(result), do: result, else: inspect(result)

    # Broadcast tool_result event (field name "tool" aligns with tool_use event)
    broadcast_chunk(agent, %{type: "tool_result", tool: tc.name, output: result, id: tc.id})

    %{
      "role" => "tool",
      "tool_call_id" => tc.id,
      "content" => result
    }
  end

  # Generate a brief human-readable narration for tool calls.
  # Only for tools where the user benefits from seeing progress.
  # Internal/meta tools (memory, status checks, roster) are silent.
  defp format_tool_narration("read_file", %{"filePath" => path}), do: "📖 Reading #{path}...\n"
  defp format_tool_narration("list_files", %{"path" => path}), do: "📂 Listing #{path}...\n"
  defp format_tool_narration("list_files", _), do: "📂 Listing files...\n"
  defp format_tool_narration("grep", %{"pattern" => pattern}), do: "🔍 Searching for \"#{String.slice(pattern, 0, 50)}\"...\n"
  defp format_tool_narration("glob", %{"pattern" => pattern}), do: "🔎 Finding #{pattern}...\n"
  defp format_tool_narration("write_file", %{"filePath" => path}), do: "✏️ Writing #{path}...\n"
  defp format_tool_narration("edit_file", %{"filePath" => path}), do: "✏️ Editing #{path}...\n"
  defp format_tool_narration("bash", %{"command" => cmd}), do: "⚡ Running: #{String.slice(cmd, 0, 60)}...\n"
  defp format_tool_narration("send_message", %{"recipients" => r}), do: "📨 Messaging #{r}...\n"
  defp format_tool_narration("dispatch_task", %{"toAgentId" => to}), do: "📋 Dispatching task to #{to}...\n"
  defp format_tool_narration("send_message", %{"recipients" => [_ | _] = recs}) when is_list(recs) do
    "📨 Sending message to #{Enum.join(recs, ", ")}...\n"
  end
  defp format_tool_narration("send_message", _), do: "📨 Sending message to superior...\n"
  defp format_tool_narration("hire_agent", _), do: "👤 Hiring new agent...\n"
  defp format_tool_narration("save_charter", _), do: "📜 Saving charter...\n"
  defp format_tool_narration("websearch", %{"query" => q}), do: "🌐 Searching: #{String.slice(q, 0, 50)}...\n"
  defp format_tool_narration("fetch_url", %{"url" => url}), do: "🌐 Fetching #{String.slice(url, 0, 60)}...\n"
  defp format_tool_narration("apply_patch", _), do: "🔧 Applying patches...\n"
  defp format_tool_narration("delete_file", %{"filePath" => path}), do: "🗑️ Deleting #{path}...\n"
  defp format_tool_narration("question", _), do: "❓ Asking user...\n"
  # Silent tools: write_memory, read_roster, list_subordinates, check_agent_status,
  # todowrite, list_available_skills, get_skill_detail, read_skill, bind_skill,
  # bind_mcp, list_available_mcp, mcp_configure, list_models, set_default_model,
  # read_charter, read_goals, update_goals, read_project_memory, read_work_logs,
  # get_project_time, get_real_time, set_alarm, review_code, approve_work,
  # reject_work, message_superior, git_worktree_*, mcp_list_tools, mcp_call,
  # list_all_agents, trigger_integration, search_files
  defp format_tool_narration(_name, _input), do: nil

  # ── Retry wrapper for LLM streaming requests ─────────────────

  defp request_with_retry(agent, model, request_body, delta_id, round_num, attempts_left) do
    task = Task.async(fn -> make_streaming_request(agent, model, request_body, delta_id) end)

    # Use @stream_idle_ms (300s) for the Task.yield timeout — this is the
    # turn-level idle watchdog. Like TS withIdleTimeout, if the stream hangs
    # (tool execution hang, approval waiter never resolving, mid-stream stall),
    # the task is killed and retried.
    case Task.yield(task, @stream_idle_ms) || Task.shutdown(task, :brutal_kill) do
      nil ->
        Logger.error("[Streamer] Round #{round_num}: stream idle timeout after #{@stream_idle_ms}ms (#{attempts_left} attempts left)")
        if attempts_left > 1 do
          backoff = round(2000 * :math.pow(2, 3 - attempts_left))
          Logger.info("[Streamer] Retrying in #{backoff}ms...")
          Process.sleep(backoff)
          request_with_retry(agent, model, request_body, delta_id, round_num, attempts_left - 1)
        else
          {:error, :timeout}
        end

      {:ok, res} ->
        case res do
          {:error, reason} ->
            Logger.warning("[Streamer] Round #{round_num}: request failed: #{inspect(reason)} (#{attempts_left} attempts left)")
            if attempts_left > 1 and should_retry?(reason) do
              backoff = round(2000 * :math.pow(2, 3 - attempts_left))
              Logger.info("[Streamer] Retrying in #{backoff}ms...")
              Process.sleep(backoff)
              request_with_retry(agent, model, request_body, delta_id, round_num, attempts_left - 1)
            else
              {:error, reason}
            end
          _ ->
            res
        end

      {:exit, reason} ->
        Logger.error("[Streamer] Round #{round_num}: request crashed: #{inspect(reason)} (#{attempts_left} attempts left)")
        if attempts_left > 1 do
          backoff = round(2000 * :math.pow(2, 3 - attempts_left))
          Logger.info("[Streamer] Retrying in #{backoff}ms...")
          Process.sleep(backoff)
          request_with_retry(agent, model, request_body, delta_id, round_num, attempts_left - 1)
        else
          {:error, {:crash, reason}}
        end
    end
  end

  # Retry on transient errors (network, 429, 500, timeout). Don't retry on auth/config errors.
  defp should_retry?(reason) do
    reason_str = inspect(reason) |> String.downcase()
    String.contains?(reason_str, "timeout") or
    String.contains?(reason_str, "closed") or
    String.contains?(reason_str, "429") or
    String.contains?(reason_str, "500") or
    String.contains?(reason_str, "502") or
    String.contains?(reason_str, "503") or
    String.contains?(reason_str, "econnreset") or
    String.contains?(reason_str, "econnrefused")
  end

  # ── Core streaming request ───────────────────────────────────
  defp make_streaming_request(agent, model, request_body, delta_id) do
    base_url = (model[:base_url] || "") |> to_string() |> String.trim_trailing("/")
    api_key = model[:api_key] || ""
    url = "#{base_url}/chat/completions"

    if base_url == "" or api_key == "" do
      {:error, :no_model_configured}
    else
      headers = [
        {"authorization", "Bearer #{api_key}"},
        {"content-type", "application/json"},
        {"accept", "text/event-stream"}
      ]

      body = Jason.encode!(request_body)

      Logger.info("[Streamer] Making STREAMING request to #{url} (body=#{byte_size(body)} bytes)")
      diag_log("Request model=#{request_body["model"]} max_tokens=#{request_body["max_tokens"]} msg_count=#{length(request_body["messages"])} has_tools=#{Map.has_key?(request_body, "tools")}")
      diag_log("model config: supports_thinking=#{model[:supports_thinking]} base_url=#{model[:base_url]} model_id=#{model[:model_id]}")

      # Use Req with into: :self for REAL-TIME chunk streaming.
      # This delivers SSE chunks to the process mailbox as they arrive,
      # enabling token-by-token broadcasting to the frontend.
      case Req.post(url,
             headers: headers,
             body: body,
             receive_timeout: @request_timeout_ms,
             into: :self,
             finch: HiveWeave.Finch,
             compressed: false,
             decode_body: false
           ) do
        {:ok, resp} ->
          handle_req_response(agent, resp, delta_id)

        {:error, reason} ->
          Logger.error("[Streamer] Req request failed: #{inspect(reason)}")
          {:error, reason}
      end
    end
  end

  # Handle Req response — with into: :self, body is a Req.Response.Async enumerable.
  # We iterate it chunk by chunk, parsing SSE events and broadcasting deltas in real-time.
  defp handle_req_response(agent, resp, delta_id) do
    body = resp.body
    status = resp.status

    # Check HTTP status code first. A non-200 response (e.g. 429 rate limit,
    # 500 server error) won't contain valid SSE data — parsing it would
    # silently produce empty text, resulting in "No response generated".
    if status != 200 do
      # Try to extract error message from body for better diagnostics
      error_body = case body do
        b when is_binary(b) -> String.slice(b, 0, 500)
        _ -> "(streaming body)"
      end
      Logger.error("[Streamer] HTTP #{status} from LLM API: #{error_body}")
      {:error, {:http_error, status, error_body}}
    else
      if is_binary(body) do
        # Fallback: raw binary body (shouldn't happen with into: :self, but handle gracefully)
        Logger.info("[Streamer] Got binary body (#{byte_size(body)} bytes) — parsing all at once")
        {events, leftover} = parse_sse(body)
        {text_delta, reasoning_delta, tool_calls_delta, finish_reason} = broadcast_and_extract(agent, events, delta_id)
        tool_calls = merge_tool_calls([], tool_calls_delta)
        {:ok, status, text_delta, reasoning_delta, tool_calls, finish_reason, Process.get(:hw_last_usage)}
      else
        # Streaming body — use Enum.reduce to iterate all chunks, broadcasting deltas as they arrive
        Logger.info("[Streamer] Got streaming body, iterating chunks...")
        {buffer, text, reasoning, tool_calls_acc, finish_reason} =
          Enum.reduce(body, {"", "", "", [], nil}, fn chunk, {buf, text_acc, reasoning_acc, tc_acc, finish} ->
            case chunk do
              c when is_binary(c) ->
                {events, leftover} = parse_sse(buf <> c)
                {text_delta, reasoning_delta, tool_calls_delta, new_finish} = broadcast_and_extract(agent, events, delta_id)
                merged_finish = new_finish || finish
                {leftover, text_acc <> text_delta, reasoning_acc <> reasoning_delta,
                 merge_tool_calls(tc_acc, tool_calls_delta), merged_finish}

              _ ->
                {buf, text_acc, reasoning_acc, tc_acc, finish}
            end
          end)

        # Parse any remaining buffer after stream ends
        {events, _} = parse_sse(buffer)
        {text_delta, reasoning_delta, tool_calls_delta, new_finish} = broadcast_and_extract(agent, events, delta_id)
        final_text = text <> text_delta
        final_reasoning = reasoning <> reasoning_delta
        final_tool_calls = merge_tool_calls(tool_calls_acc, tool_calls_delta)
        final_finish = new_finish || finish_reason
        Logger.info("[Streamer] Stream complete: #{String.length(final_text)} chars text, #{length(final_tool_calls)} tool_calls, finish=#{final_finish}")
        diag_log("Stream result: text_len=#{String.length(final_text)} reasoning_len=#{String.length(final_reasoning)} finish=#{final_finish} status=#{status}")
        {:ok, status, final_text, final_reasoning, final_tool_calls, final_finish, Process.get(:hw_last_usage)}
      end
    end
  end

  # Old collect_req_chunks removed — replaced by handle_req_response

  # Broadcast each SSE event, extract text/reasoning/tool_calls.
  # Sends text_delta/thinking_delta events for real-time token streaming.
  # Returns {text_delta, reasoning_delta, tool_calls_delta}
  defp broadcast_and_extract(agent, events, delta_id) do
    {text, reasoning, tool_calls, finish_reason} =
      Enum.reduce(events, {"", "", [], nil}, fn event, {text, reasoning, tool_calls, finish} ->
        # sse_to_chunks returns a LIST of chunks per delta (not just one),
        # so a single SSE event carrying reasoning + text + tool_calls all
        # get processed independently. This is critical for multi-model setups
        # where different providers bundle different signal types in one delta.
        chunks = sse_to_chunks(event)

        Enum.reduce(chunks, {text, reasoning, tool_calls, finish}, fn chunk, {t, r, tc, f} ->
          case chunk do
            %{type: "text", content: c} ->
              if byte_size(c) > 0 do
                Logger.debug("[Streamer] text_delta broadcast: delta_id=#{delta_id} content=#{inspect(c)} (#{byte_size(c)} bytes)")
                broadcast_chunk(agent, %{type: "text_delta", content: c, delta_id: delta_id})
              end
              {t <> c, r, tc, f}

            %{type: "reasoning", content: c} ->
              broadcast_chunk(agent, %{type: "thinking_delta", content: c, delta_id: delta_id})
              {t, r <> c, tc, f}

            %{type: "tool_call_delta", tool_call: call} ->
              {t, r, [call | tc], f}

            %{type: "finish", reason: reason} ->
              # finish_reason already logged in sse_to_chunks
              {t, r, tc, reason}

            other ->
              broadcast_chunk(agent, other)
              {t, r, tc, f}
          end
        end)
      end)

    # Reverse tool_calls to restore chronological order (reduce prepends, so list is reversed)
    {text, reasoning, Enum.reverse(tool_calls), finish_reason}
  end

  # Merge streaming tool_call deltas into complete tool_calls.
  # Deltas have incremental function.name and function.arguments fragments.
  # Input deltas must be in chronological order (broadcast_and_extract reverses to ensure this).
  defp merge_tool_calls(existing, new_deltas) do
    all = existing ++ new_deltas

    if all != [] do
      Logger.info("[Streamer] merge_tool_calls: #{length(all)} deltas to merge, " <>
                   "indices: #{inspect(Enum.map(all, & &1[:index]))}")
    end

    result =
      all
      |> Enum.group_by(& &1[:index])
      |> Enum.sort_by(fn {index, _} -> index end)
      |> Enum.map(fn {index, deltas} ->
        # Deltas within group are in chronological order (name fragment comes first, then argument fragments)
        name = deltas |> Enum.map(& &1[:name]) |> Enum.reject(&is_nil/1) |> Enum.join("")
        arguments = deltas |> Enum.map(& &1[:arguments]) |> Enum.reject(&is_nil/1) |> Enum.join("")
        id = deltas |> Enum.map(& &1[:id]) |> Enum.reject(&is_nil/1) |> List.first()

        %{
          index: index,
          id: id || Ecto.UUID.generate(),
          name: name,
          arguments: arguments
        }
      end)

    if result != [] do
      Logger.info("[Streamer] merge_tool_calls result: #{length(result)} tool_calls — " <>
                  Enum.map_join(result, ", ", fn tc -> "#{tc.name}(#{String.slice(tc.arguments || "", 0, 100)})" end))
    end

    result
  end

  # Sanitize string: replace invalid UTF-8 sequences with "?" to prevent Jason.EncodeError
  defp sanitize_utf8(str) when is_binary(str) do
    if String.valid?(str) do
      str
    else
      # Replace invalid bytes: keep valid UTF-8, replace invalid with "?"
      :binary.replace(str, <<0x83>>, "?", [:global])
      |> then(fn s ->
        # Try to repair remaining invalid sequences
        case :unicode.characters_to_binary(s, :utf8) do
          {:error, good, _rest} -> good <> "..."
          {:incomplete, good, _rest} -> good <> "..."
          repaired when is_binary(repaired) -> repaired
          _ -> String.replace(s, ~r/[^\x20-\x7E\x{4E00}-\x{9FFF}\x{3000}-\x{303F}\x{FF00}-\x{FFEF}\n\r\t]/u, "?")
        end
      end)
    end
  end

  defp sanitize_utf8(other), do: inspect(other)

  # ── SSE parsing ─────────────────────────────────────────────

  defp parse_sse(buffer) do
    parts = String.split(buffer, "\n\n")

    case List.last(parts) do
      nil ->
        {[], ""}

      "" ->
        events =
          parts
          |> Enum.drop(-1)
          |> Enum.map(&extract_data/1)
          |> Enum.reject(&is_nil/1)

        {events, ""}

      last ->
        events =
          parts
          |> Enum.drop(-1)
          |> Enum.map(&extract_data/1)
          |> Enum.reject(&is_nil/1)

        {events, last}
    end
  end

  defp extract_data(line) do
    line
    |> String.split("\n")
    |> Enum.map(fn l ->
      case String.split(l, ":", parts: 2) do
        ["data", value] -> String.trim(value)
        _ -> nil
      end
    end)
    |> Enum.reject(&is_nil/1)
    |> Enum.join("")
    |> case do
      "" -> nil
      "[DONE]" -> %{__done__: true}
      json_str ->
        case Jason.decode(json_str) do
          {:ok, map} -> map
          {:error, _} -> nil
        end
    end
  end

  # ── SSE event → chunk conversion ────────────────────────────
  #
  # Robust multi-field extraction: a single SSE delta can carry reasoning,
  # text, tool_calls, AND finish_reason simultaneously (observed in Step 3.7
  # Flash, DeepSeek-R1, and various OpenAI-compatible proxies). Unlike a `cond`
  # that picks one branch, we process every present field and return a LIST of
  # chunks. This prevents silent data loss when a provider bundles multiple
  # signal types in one delta.
  #
  # Pattern modeled after opencode's openai-chat protocol parser, which uses
  # sequential `if` checks rather than a mutually-exclusive switch.

  defp sse_to_chunks(%{__done__: true}), do: []

  defp sse_to_chunks(%{"choices" => [choice | _]} = chunk) do
    delta = choice["delta"] || %{}
    finish_reason = choice["finish_reason"]

    # Capture usage from the final chunk (OpenAI-compatible APIs include it
    # when stream_options.include_usage is set, or in the last chunk)
    usage = chunk["usage"]
    if usage do
      prompt_t = usage["prompt_tokens"] || usage[:prompt_tokens] || 0
      completion_t = usage["completion_tokens"] || usage[:completion_tokens] || 0
      total_t = usage["total_tokens"] || (prompt_t + completion_t)
      Logger.info("[Streamer] Token usage — input=#{prompt_t} output=#{completion_t} total=#{total_t}")
      Process.put(:hw_last_usage, %{input: prompt_t, output: completion_t, total: total_t})
    end

    chunks = []

    # 1. Reasoning content — check all known field name variants.
    #    Providers use different keys: reasoning_content (DeepSeek, Step),
    #    reasoning (some proxies), thinking (some Anthropic-compatible),
    #    thinking_content (rare). Check each, emit the first non-empty one.
    reasoning_text =
      cond do
        is_binary(delta["reasoning_content"]) and delta["reasoning_content"] != "" ->
          delta["reasoning_content"]
        is_binary(delta["reasoning"]) and delta["reasoning"] != "" ->
          delta["reasoning"]
        is_binary(delta["thinking"]) and delta["thinking"] != "" ->
          delta["thinking"]
        is_binary(delta["thinking_content"]) and delta["thinking_content"] != "" ->
          delta["thinking_content"]
        true ->
          nil
      end
    chunks = if reasoning_text, do: chunks ++ [%{type: "reasoning", content: reasoning_text}], else: chunks

    # 2. Text content — handle both string and array-of-content-blocks formats.
    #    Standard OpenAI: content is a string.
    #    Some proxies (Claude-via-OpenAI): content is an array of
    #    [{"type": "text", "text": "..."}] blocks — extract text from each.
    content = delta["content"]
    text_from_content =
      cond do
        is_binary(content) and content != "" ->
          content
        is_list(content) ->
          # Array of content blocks — join all text blocks
          content
          |> Enum.filter(&is_map/1)
          |> Enum.filter(fn b -> b["type"] == "text" or b["type"] == :text end)
          |> Enum.map(fn b -> b["text"] || b[:text] || "" end)
          |> Enum.join("")
          |> case do
            "" -> nil
            text -> text
          end
        true ->
          nil
      end
    chunks = if text_from_content, do: chunks ++ [%{type: "text", content: text_from_content}], else: chunks

    # 3. Tool calls — handle both standard (function wrapper) and flat formats.
    #    Standard OpenAI: [{"id": "...", "function": {"name": "...", "arguments": "..."}}]
    #    Flat variant:    [{"id": "...", "name": "...", "arguments": "..."}]
    #    Also handle the case where tool_calls is present but empty (skip).
    tool_calls_raw = delta["tool_calls"]
    chunks =
      if is_list(tool_calls_raw) and tool_calls_raw != [] do
        diag_log("delta keys=#{inspect(Map.keys(delta))} finish=#{inspect(finish_reason)} tool_calls=#{inspect(tool_calls_raw)}")
        tool_call_chunks =
          Enum.map(tool_calls_raw, fn tc ->
            # Try function.name then fall back to flat name
            name = get_in(tc, ["function", "name"]) || tc["name"]
            arguments = get_in(tc, ["function", "arguments"]) || tc["arguments"] || ""
            %{
              index: tc["index"] || 0,
              id: tc["id"],
              name: name,
              arguments: arguments
            }
          end)
        # Emit each tool_call as a separate delta chunk
        chunks ++ Enum.map(tool_call_chunks, &%{type: "tool_call_delta", tool_call: &1})
      else
        chunks
      end

    # 4. Finish reason — captured last, doesn't block other fields.
    #    Some providers attach finish_reason to the same chunk that carries
    #    the final tool_call delta or the last text token.
    chunks =
      if finish_reason != nil and finish_reason != "null" do
        Logger.info("[Streamer] Got finish_reason: #{finish_reason}")
        chunks ++ [%{type: "finish", reason: finish_reason}]
      else
        chunks
      end

    chunks
  end

  defp sse_to_chunks(%{"error" => %{"message" => msg}}) do
    [%{type: "error", content: msg}]
  end

  defp sse_to_chunks(_), do: []

  # ── Model resolution ────────────────────────────────────────

  @doc """
  Resolve the model for an agent: agent's model_id → DB lookup → first active model.
  Returns a keyword map with :id, :name, :model_id, :base_url, :api_key,
  :context_window, :max_output_tokens, :supports_thinking, :reasoning_effort.
  Public so controllers can share the same resolution logic.
  """
  def resolve_model(agent) do
    config = Map.get(agent, :config) || %{}
    model_id = config[:model_id] || Map.get(agent, :model_id)

    case model_id do
      nil -> resolve_default_model()

      id when is_binary(id) ->
        case fetch_model_from_db(id) do
          nil -> resolve_default_model()
          model -> model
        end
    end
  end

  defp resolve_model_by_name(name) do
    case fetch_active_model_by_name(name) do
      nil ->
        case Application.get_env(:hiveweave, :llm_providers) do
          nil -> nil
          providers when is_binary(name) ->
            Map.get(providers, String.to_existing_atom(name))
            |> normalize_config_model()
          _ -> nil
        end

      model ->
        model
    end
  end

  # Config maps use :model key but the code expects :model_id — normalize it.
  defp normalize_config_model(nil), do: nil
  defp normalize_config_model(model) when is_map(model) do
    if Map.has_key?(model, :model) and not Map.has_key?(model, :model_id) do
      Map.put(model, :model_id, model[:model])
    else
      model
    end
  end

  defp resolve_default_model do
    case fetch_first_active_model() do
      nil ->
        case Application.get_env(:hiveweave, :llm_providers) do
          nil -> %{name: :primary, base_url: "", api_key: "", model_id: ""}
          providers ->
            Map.get(providers, :primary, %{name: :primary, base_url: "", api_key: "", model_id: ""})
            |> normalize_config_model()
        end

      model ->
        model
    end
  end

  defp fetch_model_from_db(id) do
    {:ok, r} =
      Ecto.Adapters.SQL.query(
        HiveWeave.Repo.Meta,
        "SELECT id, name, model_id, base_url, api_key, context_window, max_output_tokens, supports_thinking, default_reasoning_effort FROM llm_models WHERE id = ? LIMIT 1",
        [id]
      )

    case r.rows do
      [row] -> row_to_model(r.columns, row)
      _ -> nil
    end
  rescue
    _ -> nil
  end

  defp fetch_active_model_by_name(name) do
    {:ok, r} =
      Ecto.Adapters.SQL.query(
        HiveWeave.Repo.Meta,
        "SELECT id, name, model_id, base_url, api_key, context_window, max_output_tokens, supports_thinking, default_reasoning_effort FROM llm_models WHERE name = ? AND is_active = 1 LIMIT 1",
        [name]
      )

    case r.rows do
      [row] -> row_to_model(r.columns, row)
      _ -> nil
    end
  rescue
    _ -> nil
  end

  defp fetch_first_active_model do
    {:ok, r} =
      Ecto.Adapters.SQL.query(
        HiveWeave.Repo.Meta,
        "SELECT id, name, model_id, base_url, api_key, context_window, max_output_tokens FROM llm_models WHERE is_active = 1 ORDER BY created_at ASC LIMIT 1",
        []
      )

    case r.rows do
      [row] -> row_to_model(r.columns, row)
      _ -> nil
    end
  rescue
    _ -> nil
  end

  defp row_to_model(columns, row) do
    map = Enum.zip(columns, row) |> Enum.into(%{})

    %{
      id: map["id"],
      name: map["name"],
      model_id: map["model_id"],
      base_url: map["base_url"],
      api_key: map["api_key"],
      context_window: map["context_window"] || 128_000,
      max_output_tokens: map["max_output_tokens"] || 8_192,
      supports_thinking: map["supports_thinking"] == 1 or map["supports_thinking"] == true,
      reasoning_effort: map["default_reasoning_effort"]
    }
  end

  # ── Model context window cache (ETS) ─────────────────────────

  def ensure_context_cache do
    if :ets.whereis(:model_context_cache) == :undefined do
      :ets.new(:model_context_cache, [:set, :public, :named_table, read_concurrency: true])
    end
  rescue
    ArgumentError -> :ok
  end

  defp get_cached_context_window(agent_id) do
    case :ets.lookup(:model_context_cache, agent_id) do
      [{^agent_id, ctx}] -> ctx
      [] -> 0
    end
  end

  defp cache_context_window(agent_id, ctx) do
    :ets.insert(:model_context_cache, {agent_id, ctx})
  end

  defp estimate_current_tokens(agent_id, project_id) do
    messages = HiveWeave.ConversationStore.get_history(agent_id, project_id)
    HiveWeave.TokenUtils.estimate_tokens_for_messages(messages)
  end
end

