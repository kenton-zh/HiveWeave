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

  @doc """
  Stream a chat completion from the LLM.
  """
  def stream(agent, message, opts, parent) do
    Logger.info("[Streamer] stream/4 CALLED for agent #{agent.id}")
    ensure_context_cache()
    agent = reload_agent(agent)

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
    initial_messages = build_messages(agent, message, opts, history)

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
           created_at: now_ms
         }) do
      {:ok, _} -> :ok
      {:error, reason} -> Logger.warning("Failed to save placeholder: #{inspect(reason)}")
    end

    broadcast_chunk(agent, %{type: "start", id: assistant_msg_id})

    # Run the tool loop
    result = run_tool_loop(
      agent, model, provider_name, tools, workspace_path,
      initial_messages, "", parent, 0, [], assistant_msg_id
    )

    finalize_stream(agent, result, assistant_msg_id, message, start_time, parent, model, provider_name)
  end

  # ── Tool loop: stream → check tool_calls → execute → repeat ─

  defp run_tool_loop(agent, model, provider_name, tools, workspace_path, messages, text_acc, parent, round_num, tool_history \\ [], assistant_msg_id \\ nil) do
    max_rounds = max_tool_rounds_for(agent.role)
    if round_num >= max_rounds do
      Logger.warning("[Streamer] Max tool rounds (#{max_rounds}) reached for agent #{agent.id} (role: #{agent.role})")

      # Like OpenCode: do one final LLM call WITHOUT tools, asking the agent
      # to summarize what it accomplished and what remains. This gives the user
      # a meaningful message instead of a silent cut-off.
      summary = make_max_rounds_summary(agent, model, provider_name, messages, parent)
      final_text = if text_acc == "", do: summary, else: text_acc <> "\n\n" <> summary
      {:ok, final_text, tool_history}
    else
      # Context overflow protection: estimate token count and trim old messages if needed.
      # Most models support ~128K tokens, but tools + system prompt already consume ~15-20K.
      # We keep the latest messages and trim from the middle to stay under ~100K tokens.
      {messages, trimmed_count} = trim_context_if_needed(messages, 100_000)

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
        "temperature" => 0.7
      }

      # Add tools if available
      request_body = if tools != [], do: Map.put(request_body, "tools", tools), else: request_body

      Logger.info("[Streamer] Round #{round_num}: sending request to LLM with #{length(messages)} messages")

      # Generate a unique delta_id for this round so the frontend can group tokens
      delta_id = "r#{round_num}_#{:rand.uniform(999999)}"

      # Wrap HTTP request with retry logic (3 attempts, exponential backoff)
      result = request_with_retry(agent, model, request_body, delta_id, round_num, 3)

      case result do
        {:ok, _status, new_text, _reasoning, tool_calls, finish_reason} ->
          combined_text = text_acc <> new_text

          Logger.info("[Streamer] Round #{round_num}: got #{String.length(new_text)} chars text, #{length(tool_calls)} tool_calls, finish=#{finish_reason}")

          # Handle truncated responses (finish_reason = "length" or "content_filter")
          cond do
            finish_reason in ["length", "content_filter"] and tool_calls != [] ->
              Logger.warning("[Streamer] Round #{round_num}: finish_reason=#{finish_reason}, tool_calls may be incomplete — discarding")

              # Discard potentially incomplete tool_calls, append a warning to text
              warning = "\n\n⚠️ Response was truncated (#{finish_reason}). Some tool calls may be incomplete."
              {:ok, combined_text <> warning, tool_history}

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
                run_tool_loop_with_tools(agent, model, provider_name, tools, workspace_path, messages, combined_text, parent, round_num, tool_history, tool_calls, new_text, assistant_msg_id)
              else
                run_tool_loop_with_tools(agent, model, provider_name, tools, workspace_path, messages, combined_text, parent, round_num, tool_history, tool_calls, new_text, assistant_msg_id)
              end

            true ->
              # No tool calls — we're done
              Logger.info("[Streamer] Round #{round_num}: done, no more tool_calls, total text=#{String.length(combined_text)} chars")

              # Handle empty response: LLM returned neither text nor tool_calls
              if combined_text == "" do
                {:ok, "⚠️ No response generated. Please try again or rephrase your request.", tool_history}
              else
                {:ok, combined_text, tool_history}
              end
          end

        {:error, reason} ->
          Logger.error("[Streamer] Round #{round_num}: LLM error: #{inspect(reason)}")
          {:error, reason}
      end
    end
  end

  # ── Execute tools and continue loop (extracted from run_tool_loop) ──

  defp run_tool_loop_with_tools(agent, model, provider_name, tools, workspace_path, messages, combined_text, parent, round_num, tool_history, tool_calls, new_text, assistant_msg_id) do
    # Save accumulated text to DB so a page refresh doesn't lose it
    if assistant_msg_id && combined_text != "" do
      try do
        HiveWeave.Services.ChatMessage.update_message(agent.id, assistant_msg_id, %{
          content: combined_text,
          is_streaming: true
        })
      rescue
        _ -> :ok
      end
    end

    # Build assistant message with tool_calls for the messages array
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

    # Execute tool calls in parallel (independent tools can run concurrently)
    tool_results =
      if length(tool_calls) > 1 do
        tasks = Enum.map(tool_calls, fn tc ->
          Task.async(fn ->
            {tc, execute_single_tool(agent, tc, workspace_path)}
          end)
        end)
        Enum.map(tasks, fn task ->
          {_tc, result} = Task.await(task, 120_000)
          result
        end)
      else
        Enum.map(tool_calls, fn tc ->
          execute_single_tool(agent, tc, workspace_path)
        end)
      end

    # Append assistant + tool results to messages, continue loop
    new_messages = messages ++ [assistant_msg | tool_results]

    # Accumulate tool calls for history
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

    run_tool_loop(agent, model, provider_name, tools, workspace_path, new_messages, combined_text, parent, round_num + 1, new_tool_history, assistant_msg_id)
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
               receive_timeout: 30_000,
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

  defp finalize_stream(agent, result, assistant_msg_id, user_message, start_time, parent, model, provider_name) do
    case result do
      {:ok, full_text, tool_history} ->
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
          is_streaming: false,
          tool_calls: tool_calls_json
        })

        broadcast_chunk(agent, %{type: "done", status: :ok, id: assistant_msg_id})
        send(parent, {:stream_done, %{status: :ok, content: display_text}})

        # Persist turn to ConversationStore
        turn_messages = [
          %{"role" => "user", "content" => user_message || ""},
          %{"role" => "assistant", "content" => display_text}
        ]
        HiveWeave.ConversationStore.append_turn(agent.id, agent.project_id, turn_messages)

        HiveWeave.Telemetry.llm_stream_done(provider_name, model[:model_id], duration, :ok)
        HiveWeave.LLM.CircuitBreaker.report_success(provider_name)
        {:ok, :completed, display_text}

      {:error, reason} ->
        # Just mark streaming as done. The placeholder may already have partial
        # content from mid-stream updates (run_tool_loop_with_tools updates
        # content during tool rounds). We don't have access to the accumulated
        # text/tool_calls here, so we leave whatever was last written.
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
  defp build_messages(agent, message, opts, history) do
    sys_identity = build_identity_prompt(agent)       # Static, prefix-cached by LLM API
    sys_context = build_context_prompt(agent)          # Dynamic, changes on memory/skill updates

    user =
      if opts[:images] && length(opts[:images]) > 0 do
        %{
          "role" => "user",
          "content" =>
            [
              %{"type" => "text", "text" => message || ""}
              | Enum.map(opts[:images], fn img ->
                  %{"type" => "image_url", "image_url" => %{"url" => img}}
                end)
            ]
        }
      else
        %{"role" => "user", "content" => message || ""}
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
  defp build_identity_prompt(agent) do
    name = (is_map(agent) && Map.get(agent, :name)) || "Agent"
    role = (is_map(agent) && Map.get(agent, :role)) || "executor"
    permission_type = get_agent_permission_type(agent)
    lang = get_project_language(agent)

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
      if lang == "zh" do
        """
        你是 "#{name}"，HiveWeave 工程组织中的 #{role}。
        #{if is_binary(goal) and goal != "", do: "## Your Role\n#{goal}", else: ""}
        #{if is_binary(backstory) and backstory != "", do: "## Background\n#{backstory}", else: ""}

        ## 重要：HiveWeave 系统目录
        - **`.hiveweave`** 是工作区根目录下的系统目录。
        - **绝不读取、写入、编辑、移动或删除 `.hiveweave` 中的任何文件。**
        - **绝不运行针对 `.hiveweave` 的 shell 命令**（rm, mv, cp 等）。

        ## 权限级别：#{permission_type}
        #{if permission_type == "coordinator" do
          build_coordinator_prompt(role, name, lang)
        else
          build_executor_prompt(role, lang)
        end}

        ## 诚实与正直规则（强制 — 零容忍）
        - **绝不声称做了你实际上没做的事。** 没调用工具 = 没执行操作。句号。
        - **绝不编造结果、ID 或结果。** 只报告工具实际返回的内容。
        - **缺少某个工具就如实说明。** 不要假装做了。
        - **工具调用失败就如实报告。** 不要掩盖错误或假装成功。
        - **绝不写工作日志声称完成了你实际没做的工作。**
        - 违反这些规则是最严重的错误。诚实高于一切。

        ## 决策规则（强制）
        - **绝不做影响项目方向、架构或资源分配的自主决策。**
        - 面对重要决策：问用户（send_message to "user"）或问上级（message_superior）。
        - **任何风险操作**（删除文件、修改关键系统、不可逆变更），先咨询用户或上级。
        - 不要假设 — 问。适用于所有层级的所有 Agent。

        ## 通信规则
        - **必须用花名称呼其他 Agent，绝不用 ID。** 用 list_subordinates 查花名。
        - **绝不在未调用 check_agent_status 的情况下声称同事"在工作中"、"忙碌"或"空闲"。**
        - report_completion 后，必须用 message_superior 发送简要总结
        - 被阻塞时用 message_superior 请求澄清
        - 主动用工具记录进展

        ## ⚠️ 行动纪律（关键）
        - 不要在执行工具前输出总结或计划作为最终消息。
        - 如果你说"我要保存 charter" — 必须在同一轮调用 `save_charter`。
        - 如果你说"我要通知 HR" — 必须在同一轮调用 `send_message` 给 HR。
        - 如果你说"我要派发任务" — 必须在同一轮调用 `dispatch_task`。
        - 只有文字描述而没有调用工具 = 失败。
        - **调用工具前写一句简短说明**（如"读取 docker-compose.yml 检查技术栈..."）。用户会实时看到。
        - 不要在所有操作完成前写长总结。
        """
        |> String.trim()
      else
        """
        You are "#{name}", a #{role} in the HiveWeave engineering organization.
      #{if is_binary(goal) and goal != "", do: "## Your Role\n#{goal}", else: ""}
      #{if is_binary(backstory) and backstory != "", do: "## Background\n#{backstory}", else: ""}

      ## IMPORTANT: HiveWeave System Directory
      - **`.hiveweave`** is the HiveWeave system directory at the workspace root.
      - **NEVER read, write, edit, move, or delete any files inside `.hiveweave`.**
      - **NEVER run shell commands that target `.hiveweave`** (rm, mv, cp, etc.).

      ## Permission Level: #{permission_type}
      #{if permission_type == "coordinator" do
        build_coordinator_prompt(role, name, lang)
      else
        build_executor_prompt(role, lang)
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
      - When faced with consequential decisions: ask the user (send_message to "user") or ask your superior (message_superior).
      - **For any risky action** (deleting files, modifying critical systems, irreversible changes), consult the user or superior first.
      - Do not assume — ask. Applies to ALL agents at ALL levels.

      ## Communication Rules
      - Always respond in the same language the user uses
      - **MANDATORY: Address other agents by their name, NEVER by ID.** Use list_subordinates to learn names.
      - **NEVER claim a colleague is "working", "busy", or "idle" without calling check_agent_status first.**
      - After report_completion, ALWAYS message_superior with a brief summary
      - If blocked, use message_superior for clarification
      - Use tools proactively to record progress

      ## ⚠️ ACTION DISCIPLINE (CRITICAL)
      - DO NOT output a summary or plan as your final message without executing the tools first.
      - If you say "I will save the charter" — you MUST call `save_charter` in the same turn.
      - If you say "I will instruct HR" — you MUST call `send_message` to HR in the same turn.
      - If you say "I will dispatch tasks" — you MUST call `dispatch_task` in the same turn.
      - A text-only response that describes actions without calling tools is a FAILURE.
      - **ALWAYS write a brief one-sentence note BEFORE calling a tool** (e.g. "Reading docker-compose.yml to check the tech stack..."). The user sees this in real-time while the tool runs.
      - Do NOT write long summaries until all actions are complete.
      """
      |> String.trim()
      end

    %{"role" => "system", "content" => prompt}
  end

  # Dynamic context prompt — rebuilt each turn from current memories + skills.
  defp build_context_prompt(agent) do
    mem_block = build_memory_block(agent)
    skill_block = HiveWeave.SkillRegistry.build_active_skills_section(
      Map.get(agent, :bound_skills) || "[]"
    )

    parts = [mem_block, skill_block] |> Enum.reject(&(&1 == nil or &1 == ""))

    if parts == [] do
      nil
    else
      %{"role" => "system", "content" => Enum.join(parts, "\n\n")}
    end
  end

  # Deprecated: kept for backwards compatibility. New code uses build_identity_prompt + build_context_prompt.
  defp build_system_prompt(agent) do
    identity = build_identity_prompt(agent)
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
          Map.put(agent, :model_id, db_agent.model_id)
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

  defp build_coordinator_prompt(role, name, lang) do
    normalized = String.downcase(role || "")

    cond do
      normalized == "ceo" ->
        if lang == "zh" do
          """
          你是 CEO — 项目负责人。人类操作员在你之上，是最终决策者。

          ## 你的使命
          - **设计并维护项目章程**，使用 `read_charter` 和 `save_charter`。
          - **将所有招聘工作委托给 HR** — 你不自己调用 hire_agent。通过 `send_message` 向 HR 发送招聘需求（需要什么角色、什么技能、几个人）。只有 HR 能 `hire_agent`。
          - **协调业务经理** — 派发任务、审查工作、批准/拒绝交付物。
          - **管理开发生命周期**：DEFINE → PLAN → BUILD → VERIFY → REVIEW → SHIP

          ## 招聘流程（强制）
          需要招聘时：
          1. 设计组织架构并保存到 charter
          2. 用 `list_subordinates` 找到你的 HR agent 的花名
          3. 用 `send_message` 给 HR 发招聘请求（哪些角色、几个人、什么技能、什么目标）
          4. 等待 HR 报告新招聘 agent 的花名和 ID
          5. 然后用 `dispatch_task` 给新 agent 派发工作

          绝不自己调用 `hire_agent`。那是 HR 的专属工具。
          绝不要只说"我会通知 HR" — 必须实际调用 `send_message` 与 HR 通信。

          ## 开发生命周期 — DEFINE → PLAN → BUILD → VERIFY → REVIEW → SHIP
          每个阶段有强制技能。在开始阶段前调用 `read_skill("<slug>")`：
          - DEFINE:  read_skill("spec-driven-development")
          - PLAN:    read_skill("planning-and-task-breakdown")
          - BUILD:   派发给 executor（他们加载 incremental-implementation + test-driven-development）
          - VERIFY:  executor 自测；有问题用 read_skill("debugging-and-error-recovery")
          - REVIEW:  派发给审查员做 code-review-and-quality + security audit
          - SHIP:    read_skill("shipping-and-launch")，执行上线前检查清单
          bugfix 或单行修改可跳过 DEFINE/PLAN，直接 BUILD→VERIFY→REVIEW。

          ### 阶段 1 — DEFINE
          - 通过 `send_message` 向用户提问
          - 把 spec 文档写入 `write_memory`
          - 获得用户明确确认

          ### 阶段 2 — PLAN
          - 把 spec 拆解为原子任务
          - 按依赖排序
          - 写入 `todowrite`

          ### 阶段 3 — BUILD
          - 一次用 `dispatch_task` 派发一个任务
          - 用 `git_worktree_create` 为 executor 创建隔离 worktree
          - 用 `git_worktree_checkpoint` 保存进度，`git_worktree_merge` 合并完成的工作
          - 通过 `read_work_logs` 审查，然后 `approve_work` 或 `reject_work`
          - 批准后才派发下一个任务

          ### 阶段 4 — VERIFY
          - 逐项检查验收标准
          - 用 `read_file`、`list_files`、`grep` 验证

          ### 阶段 5 — REVIEW
          - 派发给审查员做独立代码审查 + 安全审计
          - 审查员报告结构化发现；你根据结果批准/拒绝
          - 关键模块（认证、支付、数据库迁移、安全敏感代码）必须走 REVIEW

          ### 阶段 6 — SHIP
          - 执行上线前检查清单（read_skill "shipping-and-launch"）
          - 验证测试通过、无回归、文档已更新
          - 合并 worktree 到 main

          ## 升级
          - 你向人类操作员汇报。用 `send_message` 发给 "user"。
          - 不要无止境地列文件。读 2-3 个文件后立即设计并行动。

          ## 通信风格 — 严格纪律
          ### 对其他 Agent（report_completion, send_message to agent, dispatch_task）
          极简。不客套，不夸奖，不叙述过程。
          禁止：干得漂亮/很好/太棒了/辛苦了/整装待发/让我/看起来/I will now/let me。
          只说：做了什么，发现什么，下一步。片段可以。技术术语精确。
          示例："团队已组建. 7人. 技能已绑定. 等待优先级指示."
          ### 对用户（send_message to user）
          正常完整句子。但：只报告结论，不叙述过程。
          不要描述每一步操作（"让我先确认..."、"现在我来检查..."）。
          用户要结果，不要内心独白。每条消息最多 2-3 句。
          示例："7人团队已组建完成，技能已绑定。请问优先启动哪个模块？"
          """
        else
        """
        You are the CEO — the project leader. The human operator sits above you and is the ultimate authority.

        ## Your Mission
        - **Design and maintain the project charter** using `read_charter` and `save_charter`.
        - **Delegate ALL staffing to HR** — you do NOT hire agents yourself. Message HR via `send_message` with your hiring requests (role needed, skills required, quantity). HR is the only agent who can `hire_agent`.
        - **Coordinate business managers** — dispatch tasks, review work, approve/reject deliverables.
        - **Manage the development lifecycle**: DEFINE → PLAN → BUILD → VERIFY

        ## Hiring Flow (MANDATORY)
        When you need to hire team members:
        1. Design the org structure and save it to charter
        2. Use `list_subordinates` to find your HR agent's name
        3. Use `send_message` with recipients=["HR的花名"] to send the hiring request (which roles, how many, what skills, what goals)
        4. WAIT for HR to report back with the hired agents' names and IDs
        5. Then use `dispatch_task` to assign work to the newly hired agents

        NEVER call `hire_agent` yourself. That is HR's exclusive tool.
        NEVER just say "I will instruct HR" — you MUST actually call `send_message` to communicate with HR.

        ## Development Lifecycle — DEFINE → PLAN → BUILD → VERIFY → REVIEW → SHIP
        Each phase has a mandatory skill. Call `read_skill("<slug>")` BEFORE starting the phase:
        - DEFINE:  read_skill("spec-driven-development")
        - PLAN:    read_skill("planning-and-task-breakdown")
        - BUILD:   dispatch to executors (they load incremental-implementation + test-driven-development)
        - VERIFY:  executors self-test; use read_skill("debugging-and-error-recovery") if issues
        - REVIEW:  dispatch to Reviewer for code-review-and-quality + security audit
        - SHIP:    read_skill("shipping-and-launch"), run pre-launch checklist
        For bugfixes or single-line changes, skip DEFINE/PLAN, go directly to BUILD→VERIFY→REVIEW.

        ### Phase 1 — DEFINE
        - Ask clarifying questions via `send_message` to the user
        - Write a spec document to `write_memory`
        - Get explicit sign-off from the user

        ### Phase 2 — PLAN
        - Decompose the spec into atomic tasks
        - Order tasks by dependency
        - Write tasks to `todowrite`

        ### Phase 3 — BUILD
        - Dispatch ONE task at a time with `dispatch_task`
        - Use `git_worktree_create` to create isolated worktrees for executors before they code
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

        ## Escalation
        - You report to the human operator. Use `send_message` with recipient "user".
        - Do NOT endlessly list files. After 2-3 file reads, immediately design and act.

        ## Communication Style — STRICT DISCIPLINE
        ### Language Rule (CRITICAL)
        Follow user's language. User speaks Chinese → you speak Chinese. User speaks English → you speak English.
        NEVER repeat the same content in two languages. NEVER mix languages in one message.
        If user writes Chinese, ALL your output is Chinese (including tool call narrations, send_message content, report_completion).
        Technical terms (API names, code, file paths, CLI commands) stay in original form — do NOT translate them.

        ### To other agents (report_completion, send_message to agent, dispatch_task)
        CAVEMAN. Terse. NO pleasantries, NO praise, NO narration of your process.
        BANNED phrases: "干得漂亮" "很好" "太棒了" "辛苦了" "整装待发" "干得好" "great work" "well done" "nice job" "I will now" "let me" "看起来" "让我".
        Just state: what done, what found, what next. Fragments OK. Technical terms exact.
        Example: "团队已组建. 7人. 技能已绑定. 等待用户指示优先级."
        ### To user (send_message to user)
        Normal, complete sentences. BUT: report CONCLUSIONS only, not process narration.
        Do NOT describe every step you took ("让我先确认...", "现在我来检查...", "找到全ID了！").
        User wants results, not your internal monologue. 2-3 sentences max per message.
        Example: "7人团队已组建完成，技能已绑定。请问优先启动哪个模块？"
        """
        end

      normalized == "hr" ->
        if lang == "zh" do
          """
          你是 HR agent — CEO 下的招聘执行者。

          ## 你的权限
          - **只有你能 `hire_agent`** — 创建、调动、解雇 agent。
          - 通过 `update_roster` / `read_roster` 维护人员名册。
          - 招聘前用 `read_charter` 读取章程，了解组织架构。

          ## 招聘流程（强制）
          - 经理/CEO 通过 `send_message` 给你发招聘需求。
          - 你评估需求，然后用 `hire_agent` 创建 agent。
          - **完成任何招聘任务后，必须通过 `send_message` 向请求者报告。** 告知：创建了哪些 agent，他们的花名和角色。
          - 不要默默完成工作 — 必须报告。

          ## 命名规则（强制）
          你创建的每个 agent 必须有：
          - **有创意的中文花名** — 两字诗意昵称。示例：折纸、拾光、鹿鸣、鲸落、极光、星芒
          - **中文职位**（如 前端工程师, 后端开发, 测试工程师）
          - `name` 参数 = 花名。`role` 参数 = 职位。
          - 每个 agent 应有独特的、好记的花名。

          ## `backstory`（关键）
          写一段简短的个人叙事（2-4句）。不是项目相关的。包括过往经验、性格特点、爱好。让每个人感觉像真实角色。

          ## 技能绑定
          - 用 `list_available_skills("keyword")` 搜索匹配新 agent 角色的技能。
          - 通过 `skills` 参数传递技能 slug。
          - 用 `list_available_mcp` 检查可用的 MCP 服务器。

          ## 招聘技能标准（强制）
          招聘时按角色绑定技能：
          | 角色关键词 | 绑定技能 |
          |---|---|
          | CEO/首席执行官 | planning-and-task-breakdown, spec-driven-development, documentation-and-adrs, doubt-driven-development, context-engineering, using-agent-skills |
          | HR/人力资源 | interview-me, documentation-and-adrs, using-agent-skills |
          | 技术负责人/Manager/Tech Lead | planning-and-task-breakdown, doubt-driven-development, ci-cd-and-automation, deprecation-and-migration, documentation-and-adrs, git-workflow-and-versioning, shipping-and-launch |
          | Developer/开发/engineer | incremental-implementation, test-driven-development, source-driven-development, debugging-and-error-recovery, git-workflow-and-versioning, documentation-and-adrs, frontend-ui-engineering, api-and-interface-design |
          | 审查员/Reviewer/Inspector/QA | test-driven-development, browser-testing-with-devtools, debugging-and-error-recovery, code-simplification |
          - 始终通过 `skills` 参数传递（逗号分隔的 slug）。
          - 角色不匹配任何行则不绑定技能 — agent 可通过 list_available_skills 自行发现。
          - 招聘后可通过 bind_skill / unbind_skill 调整。

          ## 铁律 — HR 绝不能有子节点
          绝不把 parentId 设为自己的 ID。你是服务角色，不是组织管理者。
          新 agent 默认挂在 CEO 或请求方业务经理下。

          ## 你不做的事
          - 不碰文件/代码工具 — executor 写代码。
          - 不做派发/审查/批准 — 那是 coordinator 的工具。
          """
        else
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

        ## What You Do NOT Do
        - No file/code tools — executors write code.
        - No dispatch/review/approve — those are coordinator tools.
        """
        end

      true ->
        if lang == "zh" do
          """
          你是 COORDINATOR (#{role})。你的职责：
          1. 分析项目代码库（用 read_file / list_files / grep — 但限制 3-4 次调用，不要过度探索）
          2. 设计工作计划并给下属派发任务
          3. 用 `dispatch_task` 给下属派发工作
          4. 用 `git_worktree_create` 为下属创建隔离 worktree
          5. 用 `git_worktree_checkpoint` 保存进度，`git_worktree_merge` 合并完成的工作
          6. 通过 `read_work_logs` 审查下属工作，然后 `approve_work` 或 `reject_work`
          7. 通过 `send_message` 向用户报告结果
          重要：不要无止境地列文件。读 2-3 个文件后立即设计并行动。

          ## 审查 & 质量门禁
          - Developer 自测自己的代码（bash 测试 + read_skill test-driven-development）
          - 派发给审查员：
            1. 关键模块（认证、支付、数据库迁移、安全敏感代码）
            2. 上线/合并前的门禁
            3. Developer 的工作可疑或不完整时
          - 审查员通过审查工具做独立审计，报告结构化发现
          - 你根据审查员报告做批准/拒绝决策
          - 非关键工作，通过 read_work_logs 审查后直接批准

          ## 人员
          - 需要招人时通过 `send_message` 给 HR 发招聘请求。
          - 不要自己调用 `hire_agent` — 那是 HR 的专属工具。

          ## 通信风格 — 严格纪律
          ### 对其他 Agent：极简。不客套，不夸奖，不叙述过程。
          禁止：干得漂亮/很好/辛苦了/让我/看起来/I will now/let me/great work。
          只说：做了什么，发现什么，下一步。
          ### 对用户：正常句子，只报告结论。不逐步叙述。最多 2-3 句。
          """
        else
        """
        You are a COORDINATOR (#{role}). Your job:
        1. Analyze the project codebase (use read_file / list_files / grep — but limit to 3-4 calls, don't over-explore)
        2. Design work plans and assign tasks to your subordinates
        3. Use `dispatch_task` to assign work to your subordinates
        4. Use `git_worktree_create` to create isolated worktrees for subordinates before they code
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

        ## Communication Style — STRICT DISCIPLINE
        ### Language Rule: Follow user's language. Chinese in → Chinese out. English in → English out. NEVER repeat in two languages.
        ### To other agents: CAVEMAN. NO pleasantries, NO praise, NO process narration.
        BANNED: "干得漂亮" "很好" "辛苦了" "让我" "看起来" "I will now" "let me" "great work".
        State only: what done, what found, what next.
        ### To user: Normal sentences, CONCLUSIONS only. No step-by-step narration.
        2-3 sentences max. User wants results, not monologue.
        """
        end
    end
  end

  defp build_executor_prompt(role, lang) do
    normalized = String.downcase(role || "")

    cond do
      normalized in ["reviewer", "inspector", "审查员", "qa", "测试专员"] ->
        if lang == "zh" do
          """
          你是审查员 — 项目的质量守门人。

          ## 你的能力
          - 调用 run_code_review、run_security_audit、run_perf_audit 审查代码
          - 调用 run_full_review 做综合并行审查
          - 通过 bash 跑测试（npm test, pytest 等）
          - 通过 read_file 读代码理解上下文后再审查
          - 审查工具有独立分析上下文 — 你做综合，不做重复分析

          ## 你的工作流
          1. 收到上级的审查请求（哪些文件、什么范围）
          2. 读相关文件理解上下文
          3. 调用适当的审查工具 — 工具有独立 LLM 上下文
          4. 把工具结果综合为结构化报告
          5. 通过 report_completion 向上级报告

          ## 审查报告格式（强制）
          每条发现一行：path:line: severity: problem. fix.
          严重度：bug / risk / nit / q
          结尾：totals: N-bug N-risk N-nit N-q
          示例：src/auth/login.ts:L45: bug: password compare not constant-time. Use crypto.timingSafeEqual.

          ## 审计记忆（强制）
          每次审查后用 write_memory 记录：
          - 日期和游戏时间
          - 审查的文件和审查类型
          - 关键发现（严重度 + 简要描述）
          - 问题是否已修复（复审时更新）
          审查前用 read_project_memory 检查历史问题模式。

          ## 通信风格 — 严格纪律
          ### 语言规则：跟随用户语言。中文输入 → 中文输出。英文输入 → 英文输出。绝不双语重复。
          对上级：极简。不客套，不夸奖，不叙述过程。
          禁止：干得漂亮/很好/辛苦了/让我/看起来/I will/let me。
          审查报告用上述一行一发现格式。
          """
        else
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
        5. Report findings to superior via report_completion

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
        ### Language Rule: Follow user's language. Chinese in → Chinese out. English in → English out. NEVER repeat in two languages.
        To superior: CAVEMAN. NO pleasantries, NO praise, NO process narration.
        BANNED: "干得漂亮" "很好" "辛苦了" "让我" "看起来" "I will" "let me".
        Review reports use one-line-per-finding format above.
        """
        end

      true ->
        if lang == "zh" do
          """
          你是 EXECUTOR (#{role})。你的职责：
          1. 接收上级的任务并执行
          2. 用 read_file / list_files / grep / bash / apply_patch / write_file 做实际工作
          3. 通过 `report_completion` 或 `message_superior` 报告完成
          编辑前必须先读文件。要彻底但高效 — 不要过度探索。

          ## 通信风格 — 严格纪律
          ### 语言规则：跟随用户语言。中文输入 → 中文输出。英文输入 → 英文输出。绝不双语重复。
          ### 对上级（report_completion, send_message to agent）：极简。
          不客套，不夸奖，不叙述过程。
          禁止：干得漂亮/很好/辛苦了/让我/看起来/I will/let me/great work。
          只说：做了什么，发现什么，下一步。
          ### 对用户：正常句子，只报告结论。不逐步叙述。最多 2-3 句。
          """
        else
        """
        You are an EXECUTOR (#{role}). Your job:
        1. Receive tasks from your superior and execute them
        2. Use read_file / list_files / grep / bash / apply_patch / write_file to do the actual work
        3. Report completion via `report_completion` or `message_superior`
        Always read a file before editing it. Be thorough but efficient — don't over-explore.

        ## Communication Style — STRICT DISCIPLINE
        ### Language Rule: Follow user's language. Chinese in → Chinese out. English in → English out. NEVER repeat in two languages.
        ### To superior (report_completion, send_message to agent): CAVEMAN.
        NO pleasantries, NO praise, NO process narration.
        BANNED: "干得漂亮" "很好" "辛苦了" "让我" "看起来" "I will" "let me" "great work".
        State only: what done, what found, what next.
        ### To user: Normal sentences, CONCLUSIONS only. No step-by-step narration. 2-3 sentences max.
        """
        end
    end
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
      content = safe_string(msg["content"])
      args = case msg["tool_calls"] do
        nil -> ""
        calls -> Enum.map_join(calls, "", &(&1["function"]["arguments"] || ""))
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
    estimated = estimate_tokens(messages)
    if estimated <= max_tokens do
      {messages, 0}
    else
      Logger.warning("[Streamer] Context overflow: estimated ~#{estimated} tokens, trimming to fit #{max_tokens}")

      # Strategy: keep first 2 messages (system + first user) and last N messages.
      # Remove from the middle (old tool results and assistant messages).
      # Each removed pair is assistant + tool_result.
      {head, tail} = Enum.split(messages, 2)

      # Remove from the front of tail until under limit
      {trimmed_tail, removed} = trim_from_front(tail, estimated - max_tokens, 0)
      {head ++ trimmed_tail, removed}
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
    broadcast_chunk(agent, %{type: "tool_use", name: tc.name, input: input, id: tc.id})

    {:ok, result} = ToolExecutor.execute(agent, tc.name, input, workspace_path)

    # Sanitize tool output: replace invalid UTF-8 bytes to prevent Jason.EncodeError
    result = sanitize_utf8(result)

    result_preview = String.slice(result, 0, 200)
    Logger.info("[Streamer] Tool #{tc.name} result (#{String.length(result)} chars): #{result_preview}")

    # Broadcast tool_result event
    broadcast_chunk(agent, %{type: "tool_result", name: tc.name, output: result, id: tc.id})

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

    case Task.yield(task, @request_timeout_ms) || Task.shutdown(task, :brutal_kill) do
      nil ->
        Logger.error("[Streamer] Round #{round_num}: request timed out after #{@request_timeout_ms}ms (#{attempts_left} attempts left)")
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
        {:ok, status, text_delta, reasoning_delta, tool_calls, finish_reason}
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
        {:ok, status, final_text, final_reasoning, final_tool_calls, final_finish}
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
        case sse_to_chunk(event) do
          %{type: "text", content: c} ->
            # Broadcast as text_delta for real-time token streaming
            if byte_size(c) > 0 do
              Logger.debug("[Streamer] text_delta broadcast: delta_id=#{delta_id} content=#{inspect(c)} (#{byte_size(c)} bytes)")
              broadcast_chunk(agent, %{type: "text_delta", content: c, delta_id: delta_id})
            end
            {text <> c, reasoning, tool_calls, finish}

          %{type: "reasoning", content: c} ->
            # Broadcast as thinking_delta for real-time thinking display
            broadcast_chunk(agent, %{type: "thinking_delta", content: c, delta_id: delta_id})
            {text, reasoning <> c, tool_calls, finish}

          %{type: "tool_call_delta", tool_call: tc} ->
            {text, reasoning, [tc | tool_calls], finish}

          %{type: "finish", reason: reason} ->
            Logger.info("[Streamer] Got finish_reason: #{reason}")
            {text, reasoning, tool_calls, reason}

          nil ->
            {text, reasoning, tool_calls, finish}

          other ->
            broadcast_chunk(agent, other)
            {text, reasoning, tool_calls, finish}
        end
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

  defp sse_to_chunk(%{__done__: true}), do: nil

  defp sse_to_chunk(%{"choices" => [choice | _]}) do
    delta = choice["delta"] || %{}
    finish_reason = choice["finish_reason"]

    cond do
      # Finish reason — capture it (length/content_filter means truncation)
      finish_reason != nil and finish_reason != "null" ->
        %{type: "finish", reason: finish_reason}

      # Text content
      Map.has_key?(delta, "content") and is_binary(delta["content"]) ->
        %{type: "text", content: delta["content"]}

      # Reasoning content
      Map.has_key?(delta, "reasoning_content") and is_binary(delta["reasoning_content"]) ->
        %{type: "reasoning", content: delta["reasoning_content"]}

      # Tool calls
      Map.has_key?(delta, "tool_calls") ->
        tool_calls = delta["tool_calls"]
        |> Enum.map(fn tc ->
          %{
            index: tc["index"] || 0,
            id: tc["id"],
            name: get_in(tc, ["function", "name"]),
            arguments: get_in(tc, ["function", "arguments"]) || ""
          }
        end)
        %{type: "tool_call_delta", tool_call: hd(tool_calls)}

      true ->
        nil
    end
  end

  defp sse_to_chunk(%{"error" => %{"message" => msg}}) do
    %{type: "error", content: msg}
  end

  defp sse_to_chunk(_), do: nil

  # ── Model resolution ────────────────────────────────────────

  defp resolve_model(agent) do
    model_id = agent.config[:model_id] || agent.model_id

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
        "SELECT id, name, model_id, base_url, api_key, context_window, max_output_tokens FROM llm_models WHERE id = ? LIMIT 1",
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
        "SELECT id, name, model_id, base_url, api_key, context_window, max_output_tokens FROM llm_models WHERE name = ? AND is_active = 1 LIMIT 1",
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
      context_window: map["context_window"],
      max_output_tokens: map["max_output_tokens"]
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
