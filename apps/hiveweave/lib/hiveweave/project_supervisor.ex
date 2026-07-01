defmodule HiveWeave.ProjectSupervisor do
  @moduledoc """
  Top-level project supervisor.

  Manages the lifecycle of all active projects. Each project gets:
  - A dedicated agent supervisor
  - A game time server
  - An inbox/notifier

  Projects are started/stopped dynamically.
  """
  use DynamicSupervisor

  import Ecto.Query
  alias HiveWeave.Schema.Agent

  require Logger

  def start_link(_opts) do
    DynamicSupervisor.start_link(__MODULE__, [], name: __MODULE__)
  end

  @impl true
  def init(_opts) do
    DynamicSupervisor.init(
      strategy: :one_for_one,
      max_restarts: 5,
      max_seconds: 60
    )
  end

  @doc """
  Start a new project session.
  """
  def start_project(project_id, workspace_path) do
    child_spec = %{
      id: project_id,
      start: {__MODULE__, :start_project_children, [project_id, workspace_path]},
      type: :supervisor,
      restart: :transient
    }
    DynamicSupervisor.start_child(__MODULE__, child_spec)
  end

  def start_project_children(project_id, _workspace_path) do
    children = [
      # Agent supervisor for this project
      {HiveWeave.Agents.AgentSupervisor, project_id},
      # Game time server for this project
      {HiveWeave.GameTime.Server, project_id}
    ]

    case Supervisor.start_link(children, strategy: :one_for_one, max_restarts: 3, max_seconds: 60) do
      {:ok, _sup_pid} = ok ->
        # Register this project supervisor in the ProjectRegistry
        # so stop_project/1 can look it up.
        Registry.register(HiveWeave.ProjectRegistry, project_id, self())
        # Start an Agent GenServer for every agent already persisted for this project.
        spawn_agents(project_id)
        ok

      other ->
        other
    end
  end

  defp spawn_agents(project_id) do
    case HiveWeave.Repo.Meta.all(from a in Agent, where: a.project_id == ^project_id) do
      [] ->
        :ok

      agents ->
        Enum.each(agents, fn agent ->
          # Skip if a live GenServer is already running for this agent.
          name = HiveWeave.Agents.Agent.name(project_id, agent.id)

          case Process.whereis(name) do
            nil ->
              config = %{
                id: agent.id,
                project_id: agent.project_id,
                name: agent.name,
                role: agent.role,
                permission_type: agent.permission_type || "executor",
                model_id: agent.model_id,
                short_id: agent.short_id,
                goal: agent.goal,
                bound_skills: agent.bound_skills,
                mcp_servers: agent.mcp_servers,
                ask_tools: agent.ask_tools,
                denied_tools: agent.denied_tools,
                allowed_tools: agent.allowed_tools,
                config: %{goal: agent.goal}
              }

              case HiveWeave.Agents.AgentSupervisor.start_agent(project_id, config) do
                {:ok, _pid} ->
                  Logger.info("Started agent GenServer #{agent.id} (#{agent.name})")

                {:error, {:already_started, _pid}} ->
                  :ok

                {:error, reason} ->
                  Logger.warning("Failed to start agent #{agent.id}: #{inspect(reason)}")
              end

            _pid ->
              :ok
          end
        end)
    end
  rescue
    e ->
      Logger.warning("spawn_agents/1 failed: #{inspect(e)}")
      :ok
  end

  @doc """
  Look up the project's top-level supervisor pid (or nil if not running).
  """
  def supervisor_pid(project_id) do
    case Registry.lookup(HiveWeave.ProjectRegistry, project_id) do
      [] -> nil
      [{pid, _}] -> pid
    end
  end

  @doc """
  Stop a project session.
  """
  def stop_project(project_id) do
    # Find the supervisor pid via the ProjectRegistry.
    case Registry.lookup(HiveWeave.ProjectRegistry, project_id) do
      [] -> {:error, :not_found}
      [{pid, _}] -> Supervisor.stop(pid)
    end
  end
end
