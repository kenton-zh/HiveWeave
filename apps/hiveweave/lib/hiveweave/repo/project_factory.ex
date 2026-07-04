defmodule HiveWeave.Repo.ProjectFactory do
  @moduledoc """
  Manages per-project database connections.

  Each project has its own SQLite database stored at
  `<workspace_path>/.hiveweave/data.db`.

  Uses DBConnection + Exqlite.Connection pools (one pool per project).
  journal_mode=DELETE avoids -wal/-shm files on Windows.

  The factory also maintains an agent_id → project_id lookup cache
  (backed by the Meta DB agents table) so services like Inbox can
  route queries without knowing the project_id upfront.
  """
  use GenServer

  import Ecto.Query
  alias HiveWeave.Schema.{Agent, Project}
  alias HiveWeave.Repo.Meta

  require Logger

  defstruct pools: %{}, agent_cache: %{}, deleting: MapSet.new()

  def start_link(opts) do
    GenServer.start_link(__MODULE__, opts, name: __MODULE__)
  end

  def child_spec(opts) do
    %{
      id: __MODULE__,
      start: {__MODULE__, :start_link, [opts]},
      type: :worker,
      restart: :permanent
    }
  end

  @impl true
  def init(_opts) do
    {:ok, %__MODULE__{}}
  end

  # ── Public API ─────────────────────────────────────────────

  @doc """
  Ensure a per-project DB pool is started for the given project_id.
  Returns {:ok, pool_pid} or {:error, reason}.
  """
  def ensure_repo(project_id) do
    GenServer.call(__MODULE__, {:ensure_repo, project_id}, 30_000)
  end

  @doc """
  Get the DB pool for a project. Returns {:ok, pool} or {:error, :not_found}.
  """
  def get_repo(project_id) do
    GenServer.call(__MODULE__, {:get_repo, project_id})
  end

  @doc """
  Untrack a project's DB pool and return its pid (or nil).

  This does NOT terminate the pool — the caller must terminate the returned
  pid to release the SQLite file handle. Terminating the pool here would
  block (or crash) this GenServer if a connection is checked out, taking
  down every other project's pool with it.
  """
  def stop_repo(project_id) do
    GenServer.call(__MODULE__, {:stop_repo, project_id})
  end

  @doc """
  Clear the 'deleting' flag for a project so ensure_repo can create pools again.
  Call this after deletion is complete (or failed) to allow the project to be
  re-created if needed.
  """
  def clear_deleting(project_id) do
    GenServer.call(__MODULE__, {:clear_deleting, project_id})
  end

  @doc """
  Evict a project's DB pool and clear its agent cache entries.

  Unlike stop_repo/1, this does NOT mark the project as 'deleting' — a new
  pool can be created immediately on the next query (e.g. after a workspace
  path change). Used by the workspace-update endpoint to release the old
  SQLite file handle so the .hiveweave/ directory can be moved.
  """
  def evict(project_id) do
    GenServer.call(__MODULE__, {:evict, project_id})
  end

  @doc """
  Execute a raw SQL query against a project's database.
  Automatically resolves the project_id from agent_id if needed.
  Returns {:ok, %{columns: [...], rows: [...], num_rows: N}} or {:error, reason}.
  """
  def query_for_agent(agent_id, sql, params \\ []) do
    case resolve_project(agent_id) do
      {:ok, project_id} ->
        with_project_recovery(project_id, fn ->
          with {:ok, pool} <- ensure_repo(project_id) do
            run_query(pool, sql, params)
          end
        end)

      {:error, _} = err ->
        err
    end
  catch
    :exit, reason ->
      Logger.warning("[ProjectFactory] query_for_agent exit for agent #{agent_id}: #{inspect(reason)}")
      {:error, :exit}
  end

  @doc """
  Execute a raw SQL query against a project's database by project_id.
  """
  def query(project_id, sql, params \\ []) do
    with_project_recovery(project_id, fn ->
      with {:ok, pool} <- ensure_repo(project_id) do
        run_query(pool, sql, params)
      end
    end)
  catch
    :exit, reason ->
      Logger.warning("[ProjectFactory] query exit for project #{project_id}: #{inspect(reason)}")
      {:error, :exit}
  end

  @doc """
  Resolve the project_id for a given agent_id.
  Uses an in-memory cache, falls back to Meta DB.
  """
  def resolve_project(agent_id) do
    GenServer.call(__MODULE__, {:resolve_project, agent_id})
  end

  # ── GenServer callbacks ────────────────────────────────────

  @impl true
  def handle_call({:ensure_repo, project_id}, _from, state) do
    # If the project is being deleted, refuse to create a new pool.
    # This prevents concurrent API requests (e.g. frontend polling) from
    # re-opening the SQLite file after kill_pool_connections has run.
    if MapSet.member?(state.deleting, project_id) do
      {:reply, {:error, :deleting}, state}
    else
      case Map.get(state.pools, project_id) do
        nil ->
          case open_project_db(project_id) do
            {:ok, pool} ->
              new_state = %{state | pools: Map.put(state.pools, project_id, pool)}
              {:reply, {:ok, pool}, new_state}

            {:error, reason} = err ->
              Logger.error("Failed to open project DB for #{project_id}: #{inspect(reason)}")
              {:reply, err, state}
          end

        pool ->
          if Process.alive?(pool) do
            {:reply, {:ok, pool}, state}
          else
            case open_project_db(project_id) do
              {:ok, new_pool} ->
                new_state = %{state | pools: Map.put(state.pools, project_id, new_pool)}
                {:reply, {:ok, new_pool}, new_state}

              err ->
                {:reply, err, state}
            end
          end
      end
    end
  end

  @impl true
  def handle_call({:get_repo, project_id}, _from, state) do
    case Map.get(state.pools, project_id) do
      nil -> {:reply, {:error, :not_found}, state}
      pool -> {:reply, {:ok, pool}, state}
    end
  end

  @impl true
  def handle_call({:stop_repo, project_id}, _from, state) do
    # Mark the project as 'deleting' so ensure_repo refuses to create a new
    # pool. This prevents concurrent API requests from re-opening the SQLite
    # file after kill_pool_connections has run.
    state = %{state | deleting: MapSet.put(state.deleting, project_id)}

    # Untrack the pool and hand its pid back to the caller. The caller
    # terminates the pool for real (graceful stop + force-kill) so that a
    # stuck connection can never block or crash this GenServer — which is
    # shared by every project.
    case Map.pop(state.pools, project_id) do
      {nil, _} ->
        {:reply, {:ok, nil}, state}

      {pool, new_pools} ->
        {:reply, {:ok, pool}, %{state | pools: new_pools}}
    end
  end

  @impl true
  def handle_call({:clear_deleting, project_id}, _from, state) do
    {:reply, :ok, %{state | deleting: MapSet.delete(state.deleting, project_id)}}
  end

  @impl true
  def handle_call({:evict, project_id}, _from, state) do
    # Untrack the pool WITHOUT marking as 'deleting' so a new pool can be
    # created immediately on the next ensure_repo call.
    {pool, new_pools} = Map.pop(state.pools, project_id)

    # If there's a live pool, try to stop it gracefully to release the
    # SQLite file handle. Best-effort — don't block the GenServer.
    if pool && Process.alive?(pool) do
      try do
        GenServer.stop(pool, :normal, 3_000)
      catch
        :exit, reason ->
          Logger.warning("[ProjectFactory] evict: graceful stop failed for #{project_id}: #{inspect(reason)}")
          Process.exit(pool, :kill)
      end
    end

    # Clear agent cache entries for this project so resolve_project
    # re-reads from the Meta DB on next access.
    new_agent_cache =
      state.agent_cache
      |> Enum.reject(fn {_agent_id, cached_project_id} -> cached_project_id == project_id end)
      |> Map.new()

    {:reply, :ok, %{state | pools: new_pools, agent_cache: new_agent_cache}}
  end

  @impl true
  def handle_call({:resolve_project, agent_id}, _from, state) do
    case Map.get(state.agent_cache, agent_id) do
      nil ->
        case Meta.one(from a in Agent, where: a.id == ^agent_id, select: a.project_id) do
          nil ->
            {:reply, {:error, :agent_not_found}, state}

          project_id ->
            new_state = %{state | agent_cache: Map.put(state.agent_cache, agent_id, project_id)}
            {:reply, {:ok, project_id}, new_state}
        end

      project_id ->
        {:reply, {:ok, project_id}, state}
    end
  end

  # ── Private helpers ────────────────────────────────────────

  # Project-aware recovery: on a SQLite connection error, evict the cached
  # pool for this project (so a broken connection is replaced with a fresh
  # one) and retry the operation once.
  defp with_project_recovery(project_id, fun) do
    fun.()
  rescue
    e in [DBConnection.ConnectionError, Exqlite.Error] ->
      Logger.warning(
        "[ProjectFactory] SQLite error for project #{project_id}, evicting pool and retrying: #{inspect(e)}"
      )

      evict_pool(project_id)
      fun.()
  end

  # Stop and untrack a project's DB pool, then clear the 'deleting' flag so
  # ensure_repo/1 can open a fresh pool on the next call.
  defp evict_pool(project_id) do
    case stop_repo(project_id) do
      {:ok, pool} when is_pid(pool) ->
        GenServer.stop(pool, :normal, 5_000)

      _ ->
        :ok
    end

    clear_deleting(project_id)
  catch
    :exit, _ -> :ok
  end

  defp open_project_db(project_id) do
    # Primary source: the projects table.
    workspace_path =
      Meta.one(from p in Project, where: p.id == ^project_id, select: p.workspace_path)

    # Fallback: any agent in this project that carries a redundant workspace_path.
    # This mirrors the TS agentRegistry lookup and is what makes persistence
    # survive a projects-table wipe. See Application.recover_projects_from_agents/0
    # for the boot-time repair that keeps the two stores consistent.
    workspace_path =
      case workspace_path do
        nil ->
          Meta.one(
            from a in Agent,
              where: a.project_id == ^project_id and not is_nil(a.workspace_path) and a.workspace_path != "",
              select: a.workspace_path,
              limit: 1
          )

        ws ->
          ws
      end

    case workspace_path do
      nil ->
        {:error, :project_or_workspace_not_found}

      workspace_path ->
        db_path = Path.join(workspace_path, ".hiveweave/data.db")

        case File.mkdir_p(Path.dirname(db_path)) do
          :ok ->
            :ok
          {:error, reason} ->
            Logger.error("Failed to create DB directory for #{project_id}: #{inspect(reason)}")
            {:error, {:mkdir_failed, reason}}
        end
        |> case do
          :ok ->
            # Start a DBConnection pool with Exqlite.Connection
            opts = [
              database: db_path,
              pool_size: 5,
              journal_mode: :delete,
              busy_timeout: 5000
            ]

            case DBConnection.start_link(Exqlite.Connection, opts) do
              {:ok, pool} ->
                # Set pragmas (journal_mode + busy_timeout)
                with {:ok, _, _} <- exec_sql(pool, "PRAGMA journal_mode=DELETE", []),
                     {:ok, _, _} <- exec_sql(pool, "PRAGMA busy_timeout=5000", []),
                     :ok <- init_project_tables(pool) do
                  Logger.info("Opened project DB for #{project_id} at #{db_path}")
                  {:ok, pool}
                else
                  {:error, reason} ->
                    Logger.error("Failed to init project DB for #{project_id}: #{inspect(reason)}")
                    # Clean up the pool if init failed
                    GenServer.stop(pool)
                    {:error, reason}
                end

              {:error, reason} = err ->
                Logger.error("Failed to open project DB: #{inspect(reason)}")
                err
            end

          err ->
            err
        end
    end
  end

  defp init_project_tables(pool) do
    tables = [
      """
      CREATE TABLE IF NOT EXISTS inbox (
        id TEXT PRIMARY KEY,
        from_agent_id TEXT NOT NULL,
        to_agent_id TEXT NOT NULL,
        message TEXT,
        read INTEGER DEFAULT 0,
        created_at INTEGER,
        message_type TEXT,
        expect_report INTEGER DEFAULT 0
      )
      """,
      """
      CREATE TABLE IF NOT EXISTS chat_messages (
        id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT,
        thinking TEXT,
        tool_calls TEXT,
        tool_call_id TEXT,
        is_streaming INTEGER DEFAULT 0,
        is_background INTEGER DEFAULT 0,
        is_read INTEGER DEFAULT 1,
        is_context INTEGER DEFAULT 0,
        team_from_agent_id TEXT,
        team_to_agent_id TEXT,
        images TEXT,
        metadata TEXT,
        created_at INTEGER
      )
      """,
      """
      CREATE TABLE IF NOT EXISTS conversation_turns (
        id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        turn_index INTEGER NOT NULL DEFAULT 0,
        raw_messages TEXT NOT NULL DEFAULT '[]',
        approx_tokens INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER
      )
      """,
      """
      CREATE TABLE IF NOT EXISTS memories (
        id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        scope TEXT DEFAULT 'agent',
        module_id TEXT,
        type TEXT DEFAULT 'fact',
        content TEXT,
        source_agent_id TEXT,
        metadata TEXT,
        created_at INTEGER,
        updated_at INTEGER
      )
      """,
      """
      CREATE TABLE IF NOT EXISTS handoffs (
        id TEXT PRIMARY KEY,
        from_agent_id TEXT,
        to_agent_id TEXT,
        summary TEXT,
        status TEXT,
        created_at INTEGER
      )
      """,
      """
      CREATE TABLE IF NOT EXISTS work_logs (
        id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        project_id TEXT,
        session_id TEXT,
        task_id TEXT,
        action TEXT,
        type TEXT,
        summary TEXT,
        content TEXT,
        details TEXT DEFAULT '{}',
        metadata TEXT DEFAULT '{}',
        created_at INTEGER
      )
      """,
      """
      CREATE TABLE IF NOT EXISTS agent_events (
        id TEXT PRIMARY KEY,
        agent_id TEXT,
        event_type TEXT,
        payload TEXT,
        created_at INTEGER
      )
      """,
      """
      CREATE TABLE IF NOT EXISTS scheduled_alarms (
        id TEXT PRIMARY KEY,
        project_id TEXT,
        from_agent_id TEXT,
        to_agent_id TEXT,
        purpose TEXT,
        fire_at_game_seconds INTEGER,
        status TEXT DEFAULT 'pending',
        fired INTEGER DEFAULT 0,
        fired_at INTEGER,
        created_at INTEGER
      )
      """,
      """
      CREATE TABLE IF NOT EXISTS questions (
        id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        project_id TEXT,
        question TEXT NOT NULL,
        answer TEXT,
        status TEXT DEFAULT 'pending',
        created_at INTEGER,
        answered_at INTEGER
      )
      """,
      """
      CREATE TABLE IF NOT EXISTS todos (
        id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        project_id TEXT,
        content TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        priority TEXT DEFAULT 'medium',
        created_at INTEGER,
        updated_at INTEGER
      )
      """,
      """
      CREATE TABLE IF NOT EXISTS permission_requests (
        id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        project_id TEXT,
        tool_name TEXT NOT NULL,
        tool_arguments TEXT DEFAULT '{}',
        description TEXT DEFAULT '',
        status TEXT DEFAULT 'pending',
        remember INTEGER DEFAULT 0,
        user_note TEXT,
        created_at INTEGER,
        updated_at INTEGER
      )
      """,
      """
      CREATE TABLE IF NOT EXISTS team_chat_dedupe (
        id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        dedupe_key TEXT NOT NULL,
        created_at INTEGER
      )
      """,
      """
      CREATE TABLE IF NOT EXISTS personnel_records (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        position TEXT,
        department TEXT,
        responsibilities TEXT,
        notes TEXT,
        status TEXT DEFAULT 'active',
        updated_by TEXT,
        updated_at INTEGER
      )
      """,
      """
      CREATE TABLE IF NOT EXISTS agent_charters (
        id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        content TEXT,
        version TEXT DEFAULT '1.0',
        created_at INTEGER,
        updated_at INTEGER
      )
      """,
      """
      CREATE TABLE IF NOT EXISTS game_time_state (
        id TEXT PRIMARY KEY,
        project_id TEXT,
        game_seconds INTEGER DEFAULT 0,
        updated_at INTEGER
      )
      """,
      """
      CREATE TABLE IF NOT EXISTS modules (
        id TEXT PRIMARY KEY,
        project_id TEXT,
        name TEXT NOT NULL,
        path TEXT NOT NULL,
        description TEXT,
        created_at INTEGER,
        updated_at INTEGER
      )
      """
    ]

    Enum.reduce_while(tables, :ok, fn sql, _acc ->
      case exec_sql(pool, sql, []) do
        {:ok, _, _} -> {:cont, :ok}
        {:error, reason} -> {:halt, {:error, reason}}
      end
    end)
    |> case do
      :ok ->
        # Migrate existing tables: add columns that may be missing
        # (TS version created tables without these; they were added via ALTER)
        migrate_project_tables(pool)
        # Verify tables were created
        case exec_sql(pool, "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name", []) do
          {:ok, _, %Exqlite.Result{rows: rows}} ->
            table_names = Enum.map(rows, fn [n] -> n end)
            Logger.info("[ProjectFactory] Tables in project DB: #{inspect(table_names)}")
          _ ->
            Logger.warning("[ProjectFactory] Failed to verify tables in project DB")
        end
        :ok
      err ->
        err
    end
  end

  defp migrate_project_tables(pool) do
    migrations = [
      # chat_messages
      {"chat_messages", "is_streaming", "ALTER TABLE chat_messages ADD COLUMN is_streaming INTEGER DEFAULT 0"},
      {"chat_messages", "is_read", "ALTER TABLE chat_messages ADD COLUMN is_read INTEGER DEFAULT 1"},
      {"chat_messages", "thinking", "ALTER TABLE chat_messages ADD COLUMN thinking TEXT"},
      {"chat_messages", "tool_call_id", "ALTER TABLE chat_messages ADD COLUMN tool_call_id TEXT"},
      {"chat_messages", "metadata", "ALTER TABLE chat_messages ADD COLUMN metadata TEXT"},
      {"chat_messages", "images", "ALTER TABLE chat_messages ADD COLUMN images TEXT"},
      {"chat_messages", "team_from_agent_id", "ALTER TABLE chat_messages ADD COLUMN team_from_agent_id TEXT"},
      {"chat_messages", "team_to_agent_id", "ALTER TABLE chat_messages ADD COLUMN team_to_agent_id TEXT"},
      {"chat_messages", "is_context", "ALTER TABLE chat_messages ADD COLUMN is_context INTEGER DEFAULT 0"},
      # inbox
      {"inbox", "priority", "ALTER TABLE inbox ADD COLUMN priority TEXT DEFAULT 'normal'"},
      {"inbox", "subject", "ALTER TABLE inbox ADD COLUMN subject TEXT"},
      {"inbox", "content", "ALTER TABLE inbox ADD COLUMN content TEXT"},
      {"inbox", "status", "ALTER TABLE inbox ADD COLUMN status TEXT DEFAULT 'unread'"},
      {"inbox", "is_read", "ALTER TABLE inbox ADD COLUMN is_read INTEGER DEFAULT 0"},
      {"inbox", "metadata", "ALTER TABLE inbox ADD COLUMN metadata TEXT DEFAULT '{}'"},
      {"inbox", "read_at", "ALTER TABLE inbox ADD COLUMN read_at INTEGER"},
      # work_logs
      {"work_logs", "project_id", "ALTER TABLE work_logs ADD COLUMN project_id TEXT"},
      {"work_logs", "session_id", "ALTER TABLE work_logs ADD COLUMN session_id TEXT"},
      {"work_logs", "task_id", "ALTER TABLE work_logs ADD COLUMN task_id TEXT"},
      {"work_logs", "action", "ALTER TABLE work_logs ADD COLUMN action TEXT"},
      {"work_logs", "type", "ALTER TABLE work_logs ADD COLUMN type TEXT"},
      {"work_logs", "summary", "ALTER TABLE work_logs ADD COLUMN summary TEXT"},
      {"work_logs", "details", "ALTER TABLE work_logs ADD COLUMN details TEXT DEFAULT '{}'"},
      {"work_logs", "metadata", "ALTER TABLE work_logs ADD COLUMN metadata TEXT DEFAULT '{}'"},
      # handoffs
      {"handoffs", "module_id", "ALTER TABLE handoffs ADD COLUMN module_id TEXT"},
      {"handoffs", "expect_report", "ALTER TABLE handoffs ADD COLUMN expect_report INTEGER DEFAULT 0"},
      {"handoffs", "reported_up", "ALTER TABLE handoffs ADD COLUMN reported_up INTEGER DEFAULT 0"},
      {"handoffs", "updated_at", "ALTER TABLE handoffs ADD COLUMN updated_at INTEGER"},
      {"handoffs", "context_delivered", "ALTER TABLE handoffs ADD COLUMN context_delivered INTEGER DEFAULT 0"},
      # memories (old schema had layer instead of scope)
      {"memories", "scope", "ALTER TABLE memories ADD COLUMN scope TEXT DEFAULT 'agent'"},
      {"memories", "module_id", "ALTER TABLE memories ADD COLUMN module_id TEXT"},
      {"memories", "type", "ALTER TABLE memories ADD COLUMN type TEXT DEFAULT 'fact'"},
      {"memories", "source_agent_id", "ALTER TABLE memories ADD COLUMN source_agent_id TEXT"},
      {"memories", "updated_at", "ALTER TABLE memories ADD COLUMN updated_at INTEGER"},
      # scheduled_alarms
      {"scheduled_alarms", "fired", "ALTER TABLE scheduled_alarms ADD COLUMN fired INTEGER DEFAULT 0"},
      {"scheduled_alarms", "fired_at", "ALTER TABLE scheduled_alarms ADD COLUMN fired_at INTEGER"},
      # conversation_turns (TS version had different columns; add the ones ConversationStore needs)
      {"conversation_turns", "turn_index", "ALTER TABLE conversation_turns ADD COLUMN turn_index INTEGER DEFAULT 0"},
      {"conversation_turns", "raw_messages", "ALTER TABLE conversation_turns ADD COLUMN raw_messages TEXT DEFAULT '[]'"},
      {"conversation_turns", "approx_tokens", "ALTER TABLE conversation_turns ADD COLUMN approx_tokens INTEGER DEFAULT 0"},
      {"conversation_turns", "role", "ALTER TABLE conversation_turns ADD COLUMN role TEXT"},
      # agents — reasoning_effort (TS compatibility)
      {"agents", "reasoning_effort", "ALTER TABLE agents ADD COLUMN reasoning_effort TEXT"},
      # projects — goals_json (TS compatibility, separate from charter_json)
      {"projects", "goals_json", "ALTER TABLE projects ADD COLUMN goals_json TEXT"}
    ]

    Enum.each(migrations, fn {table, col, sql} ->
      case exec_sql(pool, sql, []) do
        {:ok, _, _} -> Logger.info("Migration applied: #{table}.#{col}")
        {:error, %Exqlite.Error{message: "duplicate column name: " <> _}} -> :ok
        {:error, _} -> :ok
      end
    end)
  end

  # Execute SQL via DBConnection + Exqlite.Query
  defp exec_sql(pool, sql, params) do
    query = %Exqlite.Query{name: sql, statement: sql}
    DBConnection.prepare_execute(pool, query, params)
  end

  defp run_query(pool, sql, params) do
    case exec_sql(pool, sql, params) do
      {:ok, _query, %Exqlite.Result{columns: cols, rows: rows, num_rows: num}} ->
        # Preserve original num_rows from Exqlite (for UPDATE/INSERT/DELETE, this is affected rows)
        {:ok, %{columns: cols || [], rows: rows || [], num_rows: num}}

      {:error, reason} ->
        Logger.error("ProjectFactory query failed: #{inspect(reason)} SQL: #{String.slice(sql, 0, 100)}")
        {:error, reason}
    end
  rescue
    e ->
      Logger.error("ProjectFactory query exception: #{inspect(e)}")
      {:error, e}
  end
end
