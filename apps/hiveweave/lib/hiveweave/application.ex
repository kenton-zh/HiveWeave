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

    children = [
      # Global system state (paused flag + hourly approval-cleanup timer).
      # Started first so the ETS table exists before any child reads it.
      HiveWeave.Services.SystemState,

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
    alias HiveWeave.Schema.{Project, Agent}

    # 0. Ensure projects table has language column (runtime migration)
    # SQLite does NOT support "ADD COLUMN IF NOT EXISTS" — check via PRAGMA first.
    # Default 'zh' — user is Chinese, frontend defaults to "zh".
    try do
      {:ok, pragma} = Ecto.Adapters.SQL.query(HiveWeave.Repo.Meta, "PRAGMA table_info(projects)", [])
      has_language = Enum.any?(pragma.rows, fn row -> match?([_, "language" | _], row) end)

      unless has_language do
        HiveWeave.Repo.Meta.query!("ALTER TABLE projects ADD COLUMN language TEXT DEFAULT 'zh'")
      end

      HiveWeave.Repo.Meta.query!("UPDATE projects SET language = 'zh' WHERE language IS NULL OR language = 'en'")
    rescue
      e -> Logger.warning("language column migration failed: #{inspect(e)}")
    end

    # 0b. Ensure agents table has workspace_path column (runtime migration).
    # This is the durable equivalent of the TS in-memory agentRegistry — it lets
    # ProjectFactory reopen per-project DBs even when the projects table is empty.
    try do
      {:ok, pragma} = Ecto.Adapters.SQL.query(HiveWeave.Repo.Meta, "PRAGMA table_info(agents)", [])
      has_ws = Enum.any?(pragma.rows, fn row -> match?([_, "workspace_path" | _], row) end)

      unless has_ws do
        HiveWeave.Repo.Meta.query!("ALTER TABLE agents ADD COLUMN workspace_path TEXT")
        Logger.info("[Boot] Added workspace_path column to agents table")
      end
    rescue
      e -> Logger.warning("agents.workspace_path migration failed: #{inspect(e)}")
    end

    # 0c. Recover lost projects table rows from the agents table.
    # If a project row was deleted but its agents still carry workspace_path,
    # recreate the project row so the per-project DB can be reopened on restart.
    # This is the single most important persistence guard: it makes the
    # workspace_path on agents the source of truth for "where is this project's
    # DB", so a projects-table wipe no longer orphans every conversation.
    try do
      recover_projects_from_agents()
    rescue
      e -> Logger.warning("recover_projects_from_agents failed: #{inspect(e)}")
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

    # 3. Migrate CEO/HR agents that don't have a flower name (花名).
    #    Done before starting projects so spawned agent GenServers pick up
    #    the new name. Mirrors the TS startup rename of legacy CEO/HR agents.
    try do
      migrate_flower_names(projects)
    rescue
      e -> Logger.warning("flower name migration failed: #{inspect(e)}")
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

  defp migrate_flower_names(projects) do
    import Ecto.Query
    alias HiveWeave.Schema.Agent

    Enum.each(projects, fn p ->
      agents =
        try do
          HiveWeave.Repo.Meta.all(
            from a in Agent,
              where: a.project_id == ^p.id and a.role in ["ceo", "hr", "CEO", "HR"]
          )
        rescue
          _ -> []
        end

      Enum.each(agents, fn agent ->
        unless HiveWeave.Names.is_flower_name?(agent.name) do
          new_name = HiveWeave.Names.generate_flower_name()
          Logger.info("Migrating agent #{agent.id} (#{agent.name} -> #{new_name})")

          HiveWeave.Repo.Meta.update_all(
            from(a in Agent, where: a.id == ^agent.id),
            set: [name: new_name]
          )
        end
      end)
    end)
  end

  # Reconstruct missing projects rows from the agents table so per-project DBs
  # can be reopened after a projects-table wipe. Also backfills workspace_path
  # onto any agent row that is still missing it (legacy rows created before the
  # column existed).
  #
  # Bidirectional repair:
  #   • projects row missing  + agent has workspace_path → recreate projects row
  #   • projects row present  + agent missing workspace_path → backfill agent
  # This makes the two stores self-healing: as long as ONE of them still has the
  # workspace_path, both end up consistent after boot.
  defp recover_projects_from_agents do
    import Ecto.Query
    alias HiveWeave.Schema.{Project, Agent}
    alias HiveWeave.Repo.Meta

    now = System.system_time(:millisecond)

    # All known project_ids from either table.
    project_ids_from_projects =
      Meta.all(from p in Project, select: p.id) |> Enum.reject(&is_nil/1)

    project_ids_from_agents =
      Meta.all(from a in Agent, where: not is_nil(a.project_id), select: a.project_id, distinct: true)

    all_project_ids = Enum.uniq(project_ids_from_projects ++ project_ids_from_agents)

    Enum.each(all_project_ids, fn project_id ->
      projects_ws =
        Meta.one(from p in Project, where: p.id == ^project_id, select: p.workspace_path)

      agent_ws =
        Meta.one(
          from a in Agent,
            where: a.project_id == ^project_id and not is_nil(a.workspace_path) and a.workspace_path != "",
            select: a.workspace_path,
            limit: 1
        )

      cond do
        # Both present — make sure they agree; prefer the projects table value.
        projects_ws && agent_ws && projects_ws == agent_ws ->
          :ok

        # Projects row missing but an agent knows the workspace_path → recreate.
        is_nil(projects_ws) && agent_ws ->
          Logger.info("[Boot] Recovering project #{project_id} from agents (ws=#{agent_ws})")

          # insert_all bypasses changesets, so validate_required doesn't run.
          # We provide name + created_at (the required fields) explicitly.
          Meta.insert_all(
            Project,
            [
              %{
                id: project_id,
                name: "Recovered Project",
                workspace_path: agent_ws,
                language: "zh",
                created_at: now
              }
            ],
            on_conflict: {:replace, [:workspace_path]},
            conflict_target: :id
          )

        # Projects row exists but agents lack workspace_path → backfill agents.
        projects_ws && is_nil(agent_ws) ->
          Meta.update_all(
            from(a in Agent, where: a.project_id == ^project_id and (is_nil(a.workspace_path) or a.workspace_path == "")),
            set: [workspace_path: projects_ws]
          )

          Logger.info("[Boot] Backfilled workspace_path onto agents of project #{project_id}")

        # Both present but disagree — trust the projects table, fix agents.
        projects_ws && agent_ws && projects_ws != agent_ws ->
          Meta.update_all(
            from(a in Agent, where: a.project_id == ^project_id),
            set: [workspace_path: projects_ws]
          )

          Logger.warning(
            "[Boot] workspace_path mismatch for project #{project_id}: projects=#{projects_ws} agents=#{agent_ws} — trusted projects table"
          )

        # Neither has it — nothing we can recover automatically.
        true ->
          Logger.warning("[Boot] Project #{project_id} has no workspace_path in either projects or agents table")
      end
    end)
  end

  @impl true
  def config_change(changed, _new, removed) do
    HiveWeaveWeb.Endpoint.config_change(changed, removed)
    :ok
  end

  @impl true
  def prep_stop(_state) do
    # Called BEFORE the supervision tree is torn down on SIGINT/SIGTERM.
    # This is the correct hook to persist game time — by the time stop/1
    # runs, the per-project GameTime servers have already been terminated.
    Logger.info("[Application] Graceful shutdown — persisting game time for all projects")

    project_ids =
      try do
        Registry.select(HiveWeave.ProjectRegistry, [{{:"$1", :_, :_}, [], [:"$1"]}])
      catch
        :exit, _ -> []
      end

    Enum.each(project_ids, fn project_id ->
      case GenServer.whereis(HiveWeave.GameTime.Server.name(project_id)) do
        nil ->
          :ok

        pid ->
          try do
            GenServer.call(pid, :persist, 5_000)
          catch
            :exit, _ -> :ok
          end
      end
    end)

    :ok
  end

  @impl true
  def stop(_state) do
    # Supervision tree already stopped by this point; game-time persistence
    # is handled in prep_stop/1. Kept as the documented shutdown callback.
    Logger.info("[Application] Stopped")
    :ok
  end
end
