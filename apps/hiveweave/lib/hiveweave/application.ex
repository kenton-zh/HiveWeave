defmodule HiveWeave.Application do
  use Application

  require Logger

  @impl true
  def start(_type, _args) do
    finch_opts = [
      name: HiveWeave.Finch,
      pool_size: 5
    ]

    finch_opts =
      if proxy = System.get_env("HTTPS_PROXY") || System.get_env("HTTP_PROXY") do
        Logger.info("Finch using proxy: #{proxy}")
        Keyword.put(finch_opts, :proxy, proxy)
      else
        finch_opts
      end

    # Create ApprovalService ETS table (owned by this process, persists)
    HiveWeave.Services.Approval.ensure_table()

    # Global system state (paused, etc.) — ETS-backed for cross-process visibility
    HiveWeave.Services.SystemState.start_link([])

    children = [
      # Telemetry supervisor
      HiveWeave.Telemetry,

      # Meta DB repository
      HiveWeave.Repo.Meta,

      # Project registry (lookup project supervisor pids by project_id)
      {Registry, keys: :unique, name: HiveWeave.ProjectRegistry},

      # Phoenix Endpoint (web server)
      HiveWeaveWeb.Endpoint,

      # PubSub for cross-process messaging
      {Phoenix.PubSub, name: HiveWeave.PubSub},

      # Presence for real-time status tracking
      HiveWeaveWeb.Presence,

      # Task supervisor for tool execution
      {Task.Supervisor, name: HiveWeave.TaskSupervisor},

      # HTTP client (Finch instance) for LLM streaming
      {Finch, finch_opts},

      # Circuit Breaker for LLM providers
      HiveWeave.LLM.CircuitBreaker,

      # Event audit logger
      HiveWeave.EventAudit,

      # Per-agent conversation history (token-budget trimmed, per-project DB)
      HiveWeave.ConversationStore,

      # Per-project database factory (manages dynamic Ecto Repo instances)
      HiveWeave.Repo.ProjectFactory,

      # Project supervisor (manages per-project repos and agent supervisors)
      HiveWeave.ProjectSupervisor
    ]

    opts = [strategy: :rest_for_one, name: HiveWeave.Supervisor]

    case Supervisor.start_link(children, opts) do
      {:ok, _pid} = ok ->
        # Boot existing projects asynchronously so we don't block endpoint startup.
        Task.start(fn -> boot_existing_projects() end)
        ok

      other ->
        other
    end
  end

  defp boot_existing_projects do
    import Ecto.Query
    alias HiveWeave.Schema.Project

    # 0. Ensure projects table has language column (runtime migration)
    # Default 'zh' — user is Chinese, frontend defaults to "zh".
    # Existing projects with NULL or 'en' are upgraded to 'zh'.
    try do
      HiveWeave.Repo.Meta.query!(
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS language TEXT DEFAULT 'zh'"
      )
      HiveWeave.Repo.Meta.query!(
        "UPDATE projects SET language = 'zh' WHERE language IS NULL OR language = 'en'"
      )
    rescue
      _ -> :ok
    end

    # 1. Clear zombie streaming flags on startup
    try do
      HiveWeave.Services.ChatMessage.clear_stuck_streaming()
    rescue
      e -> Logger.warning("clear_stuck_streaming failed: #{inspect(e)}")
    end

    # 2. Clean up orphaned permission requests
    try do
      HiveWeave.Services.Approval.cleanup_orphaned_requests()
    rescue
      e -> Logger.warning("cleanup_orphaned_requests failed: #{inspect(e)}")
    end

    projects =
      try do
        HiveWeave.Repo.Meta.all(from p in Project, select: %{id: p.id, workspace_path: p.workspace_path})
      rescue
        e ->
          Logger.warning("boot_existing_projects: failed to list projects: #{inspect(e)}")
          []
      end

    Enum.each(projects, fn p ->
      case HiveWeave.ProjectSupervisor.start_project(p.id, p.workspace_path) do
        {:ok, _pid} ->
          Logger.info("Booted project #{p.id}")

        {:error, {:already_started, _pid}} ->
          :ok

        {:error, reason} ->
          Logger.warning("Failed to boot project #{p.id}: #{inspect(reason)}")
      end
    end)

    # 3. Wake agents with pending work after all projects are booted
    Task.start(fn ->
      Process.sleep(1000)
      wake_agents_with_pending_work(projects)
    end)
  end

  defp wake_agents_with_pending_work(projects) do
    import Ecto.Query
    alias HiveWeave.Schema.Agent

    Enum.each(projects, fn p ->
      agents = try do
        HiveWeave.Repo.Meta.all(from a in Agent, where: a.project_id == ^p.id and a.status == "active")
      rescue
        _ -> []
      end

      Enum.each(agents, fn agent ->
        # Check if agent has pending inbox or handoffs
        has_pending = try do
          pending_inbox = HiveWeave.Services.Inbox.get_pending_messages(agent.id, 1)
          pending_handoffs = HiveWeave.Services.Handoff.get_pending_handoffs(p.id, agent.id)
          length(pending_inbox) > 0 or length(pending_handoffs) > 0
        rescue
          _ -> false
        end

        if has_pending do
          Logger.info("Waking agent #{agent.name} (#{agent.id}) with pending work")
          try do
            HiveWeave.Agents.Agent.trigger_subordinate(agent.id)
          rescue
            _ -> :ok
          end
        end
      end)
    end)
  end

  @impl true
  def config_change(changed, _new, removed) do
    HiveWeaveWeb.Endpoint.config_change(changed, removed)
    :ok
  end
end
