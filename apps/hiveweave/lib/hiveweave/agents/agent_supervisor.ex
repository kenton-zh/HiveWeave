defmodule HiveWeave.Agents.AgentSupervisor do
  @moduledoc """
  Dynamic supervisor for agent GenServers within a project.

  Each agent is a separate GenServer process. When an agent crashes,
  it's restarted automatically. If too many crashes happen in a short
  period, the supervisor stops restarting (crash storm prevention).
  """
  use DynamicSupervisor

  require Logger

  def start_link(project_id) do
    DynamicSupervisor.start_link(
      __MODULE__,
      project_id,
      name: supervisor_name(project_id)
    )
  end

  defp supervisor_name(project_id) do
    :"agent_supervisor_#{project_id}"
  end

  @impl true
  def init(project_id) do
    DynamicSupervisor.init(
      strategy: :one_for_one,
      max_restarts: 5,
      max_seconds: 60
    )
  end

  @doc """
  Start a new agent process.
  """
  def start_agent(project_id, agent_config) do
    spec = %{
      id: agent_config.id,
      start: {HiveWeave.Agents.Agent, :start_link, [agent_config]},
      restart: :transient,
      type: :worker
    }
    DynamicSupervisor.start_child(supervisor_name(project_id), spec)
  end

  @doc """
  Stop an agent process.
  """
  def stop_agent(project_id, agent_id) do
    name = HiveWeave.Agents.Agent.name(project_id, agent_id)
    case Process.whereis(name) do
      nil -> {:error, :not_found}
      pid when is_pid(pid) -> DynamicSupervisor.terminate_child(supervisor_name(project_id), pid)
    end
  end
end
