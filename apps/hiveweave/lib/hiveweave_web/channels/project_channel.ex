defmodule HiveWeaveWeb.ProjectChannel do
  use Phoenix.Channel

  alias HiveWeave.Services.Org
  alias HiveWeave.Services.Inbox

  @impl true
  def join("project:" <> project_id, _payload, socket) do
    socket = assign(socket, :project_id, project_id)
    {:ok, %{projectId: project_id}, socket}
  end

  @impl true
  def handle_info({:status_change, agent_id, status}, socket) do
    push(socket, "status_change", %{agentId: agent_id, processing: status == :processing})
    {:noreply, socket}
  end

  @impl true
  def handle_info({:game_time_tick, game_seconds}, socket) do
    push(socket, "game_time", %{
      gameSeconds: game_seconds,
      realTimestamp: System.system_time(:millisecond)
    })
    {:noreply, socket}
  end

  @impl true
  def handle_info({:agent_hired, data}, socket) do
    push(socket, "agent_hired", data)
    {:noreply, socket}
  end

  @impl true
  def handle_info({:dispatch, data}, socket) do
    push(socket, "dispatch", data)
    {:noreply, socket}
  end

  @impl true
  def handle_info(_msg, socket), do: {:noreply, socket}
end
