defmodule HiveWeaveWeb.LobbyChannel do
  use Phoenix.Channel

  require Logger

  @impl true
  def join("lobby:status", _payload, socket) do
    # Return the list of agents currently in :processing state.
    # This ensures that WebSocket reconnects don't lose processing status
    # (which would happen if we returned an empty list and the status_change
    # events had already been missed).
    processing_agents = HiveWeave.Agents.Agent.list_processing_agents()
    agent_ids = Enum.map(processing_agents, fn {agent_id, _project_id} -> agent_id end)

    payload = %{
      agentIds: agent_ids,
      paused: false
    }

    # Subscribe to lobby status events
    Phoenix.PubSub.subscribe(HiveWeave.PubSub, "lobby:status")

    # Push the initial snapshot as an "init" event so the frontend's
    # channel.on("init", ...) handler receives it. Returning {:ok, payload}
    # only sends the join reply, not a push event the frontend listens for.
    send(self(), {:push_init, payload})

    {:ok, payload, socket}
  end

  @impl true
  def handle_info({:push_init, payload}, socket) do
    push(socket, "init", payload)
    {:noreply, socket}
  end

  @impl true
  def handle_in("ping", _payload, socket) do
    {:reply, {:ok, %{pong: System.system_time()}}, socket}
  end

  # ── PubSub handlers ──────────────────────────────────────────

  @impl true
  def handle_info({:status_change, agent_id, status}, socket) do
    processing = status == :processing
    push(socket, "status_change", %{agentId: agent_id, processing: processing})
    {:noreply, socket}
  end

  @impl true
  def handle_info({:org_changed}, socket) do
    push(socket, "org_changed", %{})
    {:noreply, socket}
  end

  # Stream events forwarded as activity entries for Live Activity feed
  @impl true
  def handle_info({:stream_event, event}, socket) do
    type = event[:type] || event["type"] || "text"

    activity =
      case type do
        "text_delta" ->
          %{
            agentId: event[:agent_id] || event["agent_id"],
            agentName: event[:agent_name] || event["agent_name"] || "",
            type: "text_delta",
            content: event[:content] || event["content"] || "",
            deltaId: event[:delta_id] || event["deltaId"] || event["delta_id"],
            timestamp: System.system_time(:millisecond)
          }

        "thinking_delta" ->
          %{
            agentId: event[:agent_id] || event["agent_id"],
            agentName: event[:agent_name] || event["agent_name"] || "",
            type: "thinking_delta",
            content: event[:content] || event["content"] || "",
            deltaId: event[:delta_id] || event["deltaId"] || event["delta_id"],
            timestamp: System.system_time(:millisecond)
          }

        "tool_use" ->
          %{
            agentId: event[:agent_id] || event["agent_id"],
            agentName: event[:agent_name] || event["agent_name"] || "",
            type: "tool_use",
            toolName: event[:name] || event["name"],
            toolInput: event[:input] || event["input"],
            timestamp: System.system_time(:millisecond)
          }

        "tool_result" ->
          %{
            agentId: event[:agent_id] || event["agent_id"],
            agentName: event[:agent_name] || event["agent_name"] || "",
            type: "tool_result",
            toolName: event[:name] || event["name"],
            toolResult: event[:output] || event["output"],
            timestamp: System.system_time(:millisecond)
          }

        "done" ->
          %{
            agentId: event[:agent_id] || event["agent_id"],
            agentName: event[:agent_name] || event["agent_name"] || "",
            type: "done",
            timestamp: System.system_time(:millisecond)
          }

        _ ->
          nil
      end

    if activity do
      push(socket, "activity", activity)
    end

    {:noreply, socket}
  end

  @impl true
  def handle_info({:activity, entry}, socket) do
    push(socket, "activity", entry)
    {:noreply, socket}
  end

  @impl true
  def handle_info(_msg, socket), do: {:noreply, socket}
end
