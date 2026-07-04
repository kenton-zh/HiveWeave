defmodule HiveWeaveWeb.ChatController do
  use Phoenix.Controller

  alias HiveWeave.Services.{Org, ChatMessage, Inbox}
  alias HiveWeave.Agents.Agent

  require Logger

  plug :accepts, ["json"]

  @doc """
  Send a chat message to an agent. This is the main chat endpoint.
  The response is sent via the WebSocket channel, not this HTTP endpoint.
  This endpoint just triggers the agent to start processing.
  """
  @expert_command_map %{
    "review" => "code_reviewer",
    "test" => "test_engineer",
    "audit" => "security_auditor",
    "perf" => "web_perf_auditor"
  }

  defp parse_expert_command(message) do
    trimmed = String.trim(message || "")
    case Regex.run(~r/^\/(review|test|audit|perf)\s+(.+)$/i, trimmed) do
      [_, cmd, module_name] ->
        %{
          command: String.downcase(cmd),
          role: Map.get(@expert_command_map, String.downcase(cmd)),
          module: String.trim(module_name)
        }
      _ -> nil
    end
  end

  def send(conn, %{"agentId" => agent_id, "message" => message} = params) do
    case Org.get_agent(agent_id) do
      nil ->
        conn
        |> put_status(404)
        |> json(%{error: "Agent not found"})

      agent ->
        # ── Expert command routing (/review /test /audit /perf) ──
        # Like TS parseExpertCommand — route to specialist agent if available
        expert_cmd = parse_expert_command(message)

        if expert_cmd && expert_cmd[:role] do
          # Find expert agent by role in the same project
          project_agents = Org.list_agents(agent.project_id)
          expert_agent = Enum.find(project_agents, fn a ->
            String.downcase(a.role || "") == expert_cmd[:role]
          end)

          if expert_agent && expert_agent.id != agent_id do
            # Build the redirected message once — reused across all routing paths
            redirected_msg = "[来自: #{agent.name}]\n/#{expert_cmd[:command]} #{expert_cmd[:module]}\n\n请检查 \"#{expert_cmd[:module]}\" 并提供完整的审查报告。完成后用 report_completion + message_superior 返回报告。"

            # Expert found — check if busy via registered name
            expert_name = Agent.name(expert_agent.project_id, expert_agent.id)
            expert_pid = GenServer.whereis(expert_name)

            cond do
              expert_pid == nil ->
                # Expert agent not started — start it, then route
                ensure_agent_started(expert_agent)
                expert_pid = GenServer.whereis(expert_name)

                if expert_pid do
                  ChatMessage.save_message(%{agent_id: agent_id, role: "user", content: message, is_read: true, is_streaming: false})
                  ChatMessage.save_message(%{agent_id: expert_agent.id, role: "user", content: redirected_msg, is_read: true, is_streaming: false})
                  Agent.chat(expert_pid, redirected_msg)
                  json(conn, %{ok: true, routed: true, expert: expert_agent.name, started: true})
                else
                  nil  # Failed to start — fall through
                end

              true ->
                case Agent.get_state(expert_pid) do
                  %{status: :processing} ->
                    # Expert is busy — queue in inbox
                    Inbox.send_message(agent_id, expert_agent.id, "peer", redirected_msg, %{expect_report: true, priority: "normal"})

                    ChatMessage.save_message(%{agent_id: agent_id, role: "user", content: message, is_read: true, is_streaming: false})
                    ChatMessage.save_message(%{
                      agent_id: agent_id, role: "assistant",
                      content: "⏳ **#{expert_agent.name}**（#{expert_cmd[:role]}）正在处理其他请求，你的审查请求已加入队列，专家完成后会自动处理并通知你。",
                      is_read: true, is_streaming: false
                    })

                    json(conn, %{ok: true, queued: true, expert: expert_agent.name})

                  _ ->
                    # Expert is free — redirect message to expert
                    ChatMessage.save_message(%{agent_id: agent_id, role: "user", content: message, is_read: true, is_streaming: false})
                    ChatMessage.save_message(%{agent_id: expert_agent.id, role: "user", content: redirected_msg, is_read: true, is_streaming: false})
                    Agent.chat(expert_pid, redirected_msg)
                    json(conn, %{ok: true, routed: true, expert: expert_agent.name})
                end
            end
          else
            # No expert agent found — fall through to normal processing
            nil
          end
        else
          # Not an expert command — fall through to normal processing
          nil
        end

        # Normal processing (skip if expert routing already sent a response)
        if conn.state == :sent do
          # Expert routing already responded — skip normal processing
          conn
        else
        # Save user message
        case ChatMessage.save_message(%{
          agent_id: agent_id,
          role: "user",
          content: message,
          images: if(params["images"], do: Jason.encode!(params["images"]), else: nil),
          is_read: true,
          is_streaming: false,
          created_at: System.system_time(:millisecond)
        }) do
          {:ok, user_msg} ->
            # Find agent process
            case find_agent_pid(agent.project_id, agent_id) do
              nil ->
                # Agent not started yet - start it on demand
                ensure_agent_started(agent)
                json(conn, %{
                  ok: true,
                  userMessageId: user_msg.id,
                  note: "Agent started, please subscribe via WebSocket"
                })

              pid ->
                # Trigger chat
                case Agent.chat(pid, message, images: params["images"]) do
                  :ok ->
                    json(conn, %{
                      ok: true,
                      userMessageId: user_msg.id
                    })

                  {:error, :busy} ->
                    # Auto-reset stuck agent and retry
                    Logger.warning("Agent #{agent_id} was busy, force-resetting...")
                    Kernel.send(pid, {:force_reset})
                    :timer.sleep(500)

                    case Agent.chat(pid, message, images: params["images"]) do
                      :ok ->
                        json(conn, %{ok: true, userMessageId: user_msg.id, reset: true})

                      {:error, :busy} ->
                        conn
                        |> put_status(409)
                        |> json(%{error: "Agent is busy after reset"})
                    end
                end
            end

          {:error, _} ->
            conn
            |> put_status(500)
            |> json(%{error: "Failed to save message"})
        end
        end  # close if conn.halted / else
    end
  end

  @doc """
  Get chat history for an agent.
  """
  def history(conn, %{"agentId" => agent_id}) do
    messages = ChatMessage.get_messages(agent_id, 200) |> Enum.map(&serialize_message/1)
    json(conn, %{messages: messages})
  end

  @doc """
  Mark messages as read.
  """
  def mark_read(conn, %{"ids" => ids, "agentId" => agent_id}) do
    count = ChatMessage.mark_as_read(agent_id, ids)
    json(conn, %{ok: true, count: count})
  end

  @doc """
  Get inbox messages for an agent.
  """
  def inbox(conn, %{"agentId" => agent_id}) do
    messages = Inbox.get_inbox(agent_id) |> Enum.map(&serialize_inbox/1)
    unread = Inbox.get_unread_count(agent_id)
    json(conn, %{messages: messages, unreadCount: unread})
  end

  @doc """
  Send a message to another agent.
  """
  def send_inbox(conn, %{"fromAgentId" => from_id, "toAgentId" => to_id, "content" => content} = params) do
    case Inbox.send_message(from_id, to_id, params["type"] || "message", content,
           subject: params["subject"],
           priority: params["priority"],
           metadata: params["metadata"]) do
      {:ok, msg} -> json(conn, %{ok: true, message: serialize_inbox(msg)})
      error ->
        conn
        |> put_status(500)
        |> json(%{error: "Failed to send inbox message"})
    end
  end

  @doc """
  Pause the system.
  """
  def pause(conn, _params) do
    HiveWeave.Services.SystemState.pause()
    json(conn, %{paused: true})
  end

  @doc """
  Resume the system.
  """
  def resume(conn, _params) do
    HiveWeave.Services.SystemState.resume()
    json(conn, %{paused: false})
  end

  @doc """
  Get paused state.
  """
  def paused(conn, _params) do
    json(conn, %{paused: HiveWeave.Services.SystemState.paused?()})
  end

  @doc """
  Get unread background messages for an agent.
  """
  def unread(conn, %{"agentId" => agent_id}) do
    case Org.get_agent(agent_id) do
      nil ->
        conn
        |> put_status(404)
        |> json(%{error: "Agent not found"})

      _agent ->
        messages = ChatMessage.get_unread_background(agent_id) |> Enum.map(&serialize_message/1)
        json(conn, %{messages: messages, count: length(messages)})
    end
  end

  @doc """
  Force-clear a stuck processing state on an agent.
  """
  def reset_processing(conn, %{"agentId" => agent_id}) do
    case Org.get_agent(agent_id) do
      nil ->
        conn
        |> put_status(404)
        |> json(%{error: "Agent not found"})

      agent ->
        name = Agent.name(agent.project_id, agent_id)
        pid = GenServer.whereis(name)

        if pid do
          Kernel.send(pid, {:force_reset})
        end

        HiveWeaveWeb.Endpoint.broadcast(
          "agent:#{agent_id}",
          "status:changed",
          %{status: "idle"}
        )

        json(conn, %{ok: true, agentId: agent_id, processing: false})
    end
  end

  @doc """
  Get the resolved model that will be used for an agent.
  Delegates to HiveWeave.LLM.Streamer.resolve_model/1.
  """
  def resolved_model(conn, %{"agentId" => agent_id}) do
    case Org.get_agent(agent_id) do
      nil ->
        conn
        |> put_status(404)
        |> json(%{error: "Agent not found"})

      agent ->
        model = HiveWeave.LLM.Streamer.resolve_model(agent)
        model_id = Map.get(model, :id)
        model_name = Map.get(model, :name)

        if model_id do
          json(conn, %{agentId: agent_id, modelName: model_name, modelId: model_id, source: "auto"})
        else
          json(conn, %{agentId: agent_id, modelName: model_name, modelId: nil, source: "none"})
        end
    end
  end

  # Private helpers

  defp find_agent_pid(project_id, agent_id) do
    name = :"agent_#{project_id}_#{agent_id}"
    case Process.whereis(name) do
      nil -> nil
      pid when is_pid(pid) -> pid
    end
  end

  defp ensure_agent_started(agent) do
    case HiveWeave.Agents.AgentSupervisor.start_agent(agent.project_id, %{
      id: agent.id,
      project_id: agent.project_id,
      name: agent.name,
      role: agent.role,
      permission_type: agent.permission_type,
      model_id: agent.model_id
    }) do
      {:ok, _pid} -> :ok
      {:error, {:already_started, _pid}} -> :ok
      error -> Logger.warning("Failed to start agent: #{inspect(error)}")
    end
  end

  defp serialize_message(nil), do: nil
  defp serialize_message(m) do
    # Support both atom and string keys (DB returns string keys)
    g = fn key -> Map.get(m, key) || Map.get(m, to_string(key)) end
    %{
      id: g.(:id),
      agent_id: g.(:agent_id),
      agentId: g.(:agent_id),
      role: g.(:role),
      content: g.(:content),
      tool_calls: g.(:tool_calls),
      toolCalls: g.(:tool_calls),
      images: g.(:images),
      is_background: g.(:is_background),
      isBackground: g.(:is_background),
      is_read: g.(:is_read),
      isRead: g.(:is_read),
      is_streaming: g.(:is_streaming),
      isStreaming: g.(:is_streaming),
      is_context: g.(:is_context),
      isContext: g.(:is_context),
      created_at: g.(:created_at),
      createdAt: g.(:created_at),
      team_from_agent_id: g.(:team_from_agent_id),
      teamFromAgentId: g.(:team_from_agent_id),
      team_to_agent_id: g.(:team_to_agent_id),
      teamToAgentId: g.(:team_to_agent_id)
    }
  end

  defp serialize_inbox(nil), do: nil
  defp serialize_inbox(m) do
    # Support both Schema.Inbox structs and raw maps from Inbox.row_to_message
    # row_to_message uses :message, :message_type, :read; Schema uses :content, :type, :is_read
    content = Map.get(m, :content) || Map.get(m, :message)
    mtype = Map.get(m, :type) || Map.get(m, :message_type) || "message"
    is_read = Map.get(m, :is_read) || Map.get(m, :read) || false
    is_processed = Map.get(m, :is_processed, false)

    %{
      id: Map.get(m, :id),
      from_agent_id: Map.get(m, :from_agent_id),
      fromAgentId: Map.get(m, :from_agent_id),
      to_agent_id: Map.get(m, :to_agent_id),
      toAgentId: Map.get(m, :to_agent_id),
      type: mtype,
      subject: Map.get(m, :subject),
      content: content,
      priority: Map.get(m, :priority) || "normal",
      status: Map.get(m, :status) || if(is_read, do: "read", else: "unread"),
      is_read: is_read,
      isRead: is_read,
      is_processed: is_processed,
      isProcessed: is_processed,
      metadata: Map.get(m, :metadata) || "{}",
      created_at: Map.get(m, :created_at),
      createdAt: Map.get(m, :created_at),
      read_at: Map.get(m, :read_at),
      readAt: Map.get(m, :read_at),
      processed_at: Map.get(m, :processed_at),
      processedAt: Map.get(m, :processed_at),
      expect_report: Map.get(m, :expect_report, false),
      expectReport: Map.get(m, :expect_report, false)
    }
  end
end

