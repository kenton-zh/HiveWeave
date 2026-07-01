defmodule HiveWeaveWeb.AgentChannel do
  use Phoenix.Channel

  alias HiveWeave.Services.Org
  alias HiveWeave.Services.Inbox
  alias HiveWeave.Services.ChatMessage
  alias HiveWeave.Agents.Agent

  require Logger

  @impl true
  def join("agent:" <> agent_id, _payload, socket) do
    case Org.get_agent(agent_id) do
      nil ->
        {:error, %{reason: "agent_not_found"}}

      agent ->
        # Auto-boot project if not running (e.g. after backend restart)
        ensure_project_booted(agent.project_id)

        # Subscribe to PubSub topics ONCE in join — NOT in handle_in("chat").
        # Phoenix.PubSub subscriptions are additive per process: calling subscribe
        # twice from the same process on the same topic delivers each broadcast
        # twice. Previously, subscribing in handle_in("chat") caused event delivery
        # to multiply with each message sent (2nd message = 2x tokens, 3rd = 3x, …).
        Phoenix.PubSub.subscribe(HiveWeave.PubSub, "agent:#{agent_id}")
        Phoenix.PubSub.subscribe(HiveWeave.PubSub, "project:#{agent.project_id}")

        socket =
          socket
          |> assign(:agent_id, agent_id)
          |> assign(:project_id, agent.project_id)
          |> assign(:agent_name, agent.name)

        # Get initial history
        history = ChatMessage.get_messages(agent_id, 50)
        inbox = Inbox.get_inbox(agent_id)

        {:ok, %{
          agentId: agent_id,
          name: agent.name,
          role: agent.role,
          history: history,
          inbox: inbox
        }, socket}
    end
  end

  @impl true
  def handle_in("chat", %{"message" => message} = payload, socket) do
    agent_id = socket.assigns.agent_id
    project_id = socket.assigns.project_id

    # Save user message
    case ChatMessage.save_message(%{
           agent_id: agent_id,
           role: "user",
           content: message,
           images: if(payload["images"], do: Jason.encode!(payload["images"]), else: nil),
           is_read: true,
           is_streaming: false,
           created_at: System.system_time(:millisecond)
         }) do
      {:ok, user_msg} ->
        push(socket, "message_id", %{role: "user", id: user_msg.id})

        # Find agent GenServer
        case find_agent_pid(project_id, agent_id) do
          nil ->
            push(socket, "error", %{message: "Agent not running"})
            {:noreply, socket}

          pid ->
            # PubSub subscription already active (subscribed in join/3).
            # Send chat to agent
            case Agent.chat(pid, message, images: payload["images"]) do
              :ok ->
                {:noreply, socket}

              {:error, :busy} ->
                push(socket, "error", %{message: "Agent is busy"})
                {:noreply, socket}
            end
        end

      {:error, reason} ->
        Logger.error("Failed to save user message for agent #{agent_id}: #{inspect(reason)}")
        push(socket, "error", %{message: "Failed to save message (project database not available)"})
        {:noreply, socket}
    end
  end

  @impl true
  def handle_in("cancel", _payload, socket) do
    agent_id = socket.assigns.agent_id
    project_id = socket.assigns.project_id

    case find_agent_pid(project_id, agent_id) do
      nil -> :ok
      pid -> Agent.cancel(pid)
    end

    {:reply, :ok, socket}
  end

  @impl true
  def handle_in("ping", _payload, socket) do
    {:reply, {:ok, %{pong: System.system_time()}}, socket}
  end

  # Private helpers

  defp ensure_project_booted(project_id) do
    case Registry.lookup(HiveWeave.ProjectRegistry, project_id) do
      [] ->
        case HiveWeave.ProjectSupervisor.start_project(project_id, "") do
          {:ok, _} -> :ok
          {:error, {:already_started, _}} -> :ok
          _ -> :ok
        end
      _ -> :ok
    end
  rescue
    _ -> :ok
  end

  defp find_agent_pid(project_id, agent_id) do
    name = :"agent_#{project_id}_#{agent_id}"
    case Process.whereis(name) do
      nil -> nil
      pid when is_pid(pid) -> pid
    end
  end

  defp subscribe_to_agent(project_id, agent_id) do
    Phoenix.PubSub.subscribe(HiveWeave.PubSub, "agent:#{agent_id}")
    Phoenix.PubSub.subscribe(HiveWeave.PubSub, "project:#{project_id}")
  end
  @impl true
  def handle_info({:stream_event, event}, socket) do
    type = event[:type] || event["type"] || "text"

    case type do
      "text_delta" ->
        # Real-time text token — push immediately for streaming display
        push(socket, "stream_chunk", %{text: event[:content] || event["content"] || "", delta: true, deltaId: event[:delta_id] || event["deltaId"]})

      "thinking_delta" ->
        # Real-time thinking token — push as reasoning delta
        push(socket, "stream_chunk", %{text: event[:content] || event["content"] || "", reasoning: true, delta: true, deltaId: event[:delta_id] || event["deltaId"]})

      "text" ->
        text = event[:content] || event["content"] || ""
        push(socket, "stream_chunk", %{text: text})

      "reasoning" ->
        # Forward reasoning as stream_chunk too (frontend may display it differently)
        text = event[:content] || event["content"] || ""
        push(socket, "stream_chunk", %{text: text, reasoning: true})

      "tool_use" ->
        push(socket, "stream_tool", %{
          type: "tool_use",
          name: event[:name] || event["name"],
          input: event[:input] || event["input"],
          id: event[:id] || event["id"]
        })

      "tool_result" ->
        push(socket, "stream_tool", %{
          type: "tool_result",
          name: event[:name] || event["name"],
          output: event[:output] || event["output"],
          id: event[:id] || event["id"]
        })

      "done" ->
        push(socket, "done", %{})

      "start" ->
        # Send as message_id (same as TS version) so frontend creates assistant bubble immediately
        push(socket, "message_id", %{role: "assistant", id: event[:id] || event["id"]})

      _other ->
        # Unknown event type, pass through as stream_chunk
        push(socket, "stream_chunk", event)
    end

    {:noreply, socket}
  end

  @impl true
  def handle_info({:stream_error, message}, socket) do
    push(socket, "error", %{message: message})
    push(socket, "done", %{})

    {:noreply, socket}
  end

  @impl true
  def handle_info({:status_change, agent_id, status}, socket) do
    if agent_id == socket.assigns.agent_id do
      push(socket, "status_change", %{agentId: agent_id, processing: status == :processing})
    end
    {:noreply, socket}
  end

  @impl true
  def handle_info(_msg, socket), do: {:noreply, socket}
end
