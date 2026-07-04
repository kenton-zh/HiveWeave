defmodule HiveWeaveWeb.ProjectsController do
  use Phoenix.Controller

  import Ecto.Query
  alias HiveWeave.Schema.{Agent, Project}
  require Logger

  plug :accepts, ["json"]

  def index(conn, _params) do
    projects = HiveWeave.Repo.Meta.all(Project) |> Enum.map(&serialize_project/1)
    json(conn, %{projects: projects})
  end

  def show(conn, %{"id" => id}) do
    project = get_project(id)
    case project do
      nil ->
        conn
        |> put_status(404)
        |> json(%{error: "Not found"})
      p ->
        # Auto-boot project if not already running (e.g. after backend restart)
        ensure_project_booted(p)
        json(conn, %{project: serialize_project(p)})
    end
  end

  def create(conn, %{"name" => name} = params) do
    attrs = %{
      name: name,
      description: params["description"],
      workspace_path: params["workspacePath"],
      org_paradigm: params["orgParadigm"],
      charter_json: params["charterJson"],
      language: params["language"] || "zh",
      created_at: System.system_time(:millisecond)
    }

    case %Project{} |> Project.changeset(attrs) |> HiveWeave.Repo.Meta.insert() do
      {:ok, project} ->
        # Auto-create CEO and HR agents FIRST (before starting supervisors)
        main_agent_id = ensure_ceo_hr(project.id)

        # Start project supervisor (even without workspace_path — agents need GenServers)
        ws = project.workspace_path || ""
        case HiveWeave.ProjectSupervisor.start_project(project.id, ws) do
          {:ok, _} -> :ok
          {:error, {:already_started, _}} -> :ok
          other -> IO.warn("ProjectSupervisor start failed: #{inspect(other)}")
        end

        json(conn, %{project: serialize_project(project), mainAgentId: main_agent_id})

      {:error, changeset} ->
        conn
        |> put_status(422)
        |> json(%{errors: changeset_errors(changeset)})
    end
  end

  def update(conn, %{"id" => id} = params) do
    case get_project(id) do
      nil ->
        conn
        |> put_status(404)
        |> json(%{error: "Not found"})

      project ->
        attrs = %{
          description: params["description"],
          org_paradigm: params["orgParadigm"]
        }

        project
        |> Project.changeset(attrs)
        |> HiveWeave.Repo.Meta.update()
        |> case do
          {:ok, p} -> json(conn, %{project: serialize_project(p)})
          {:error, _} -> json(conn, %{error: "Failed to update project"}) |> Plug.Conn.put_status(500)
        end
    end
  end

  def delete(conn, %{"id" => id}) do
    case get_project(id) do
      nil ->
        conn
        |> put_status(404)
        |> json(%{error: "Not found"})

      project ->
        # Wrap the entire deletion in try/rescue. When multiple projects
        # share the same workspace_path, DB pool operations can raise
        # (e.g. DBConnection errors when killing shared connections).
        # We must still return 200 so the frontend can remove the project
        # from the list — the meta-DB record is the source of truth.
        try do
          do_delete_project(conn, project)
        rescue
          e ->
            Logger.error("[delete] Project #{project.id} deletion failed: #{inspect(e)}\n#{Exception.format_stacktrace(__STACKTRACE__)}")
            # Still try to clear the deleting flag so re-creation works
            try do
              HiveWeave.Repo.ProjectFactory.clear_deleting(project.id)
            catch
              :exit, _ -> :ok
            end
            json(conn, %{ok: true, dbLeftover: true, warning: "Project deleted with cleanup errors"})
        end
    end
  end

  defp do_delete_project(conn, project) do
    # 1. Stop project supervisor — bounded by 3s. Agent GenServers and
    #    any in-flight LLM Tasks must die before we close the DB pool.
    stop_project_bounded(project.id, 3_000)

    # 2. Close the per-project DB pool — marks project as 'deleting' so
    #    no new pool can be created, then kills connection processes.
    stop_repo_bounded(project.id, 5_000)

    # 3. Delete agents for this project from the meta DB FIRST.
    #    This ensures the project disappears from the org tree immediately.
    try do
      HiveWeave.Repo.Meta.query("DELETE FROM agents WHERE project_id = ?", [project.id])
    rescue
      e -> Logger.warning("Failed to delete agents for project #{project.id}: #{inspect(e)}")
    end

    # 4. Drop the project record itself.
    case HiveWeave.Repo.Meta.delete(project) do
      {:ok, _} -> :ok
      {:error, reason} ->
        Logger.error("Failed to delete project record #{project.id}: #{inspect(reason)}")
    end

    # 5. Clean up .hiveweave/ files SYNCHRONOUSLY.
    #    The previous design spawned a background task, but that task dies if
    #    the server is killed/restarted before it finishes — leaving .hiveweave/
    #    behind forever. The TS version awaits `rm(hwDir)` synchronously, and
    #    so must we. The pool connections are already dead (step 2), so the
    #    SQLite file handle is released; a short retry loop handles the
    #    Windows handle-release lag.
    db_leftover =
      if project.workspace_path && project.workspace_path != "" do
        cleanup_project_workspace(project.workspace_path)
      else
        false
      end

    # 6. Clear the 'deleting' flag so the project can be re-created if needed.
    try do
      HiveWeave.Repo.ProjectFactory.clear_deleting(project.id)
    catch
      :exit, reason ->
        Logger.warning("[delete] clear_deleting failed for #{project.id}: #{inspect(reason)}")
    end

    json(conn, %{ok: true, dbLeftover: db_leftover})
  end

  # ── Project-deletion helpers ──────────────────────────────────
  # On Windows the per-project SQLite file stays LOCKED as long as any
  # BEAM process holds an Exqlite connection (a DBConnection pool worker).
  # The old code ran `GenServer.stop(pool)` inside ProjectFactory's
  # handle_call, which (a) blocks the single shared ProjectFactory GenServer
  # for every project, and (b) when a connection is checked out by a running
  # agent, the stop hangs/times out — crashing ProjectFactory and leaving
  # the pool ORPHANED but alive. `File.rm(data.db)` then fails forever and
  # the file is left behind (the exact bug this fixes).
  #
  # Fix: untrack the pool instantly (ProjectFactory stays responsive), then
  # TERMINATE the pool process for real here — graceful stop first, then a
  # force `:kill` (untrappable) if it doesn't exit. Killing the pool
  # supervisor kills its connection workers, whose NIF resource destructor
  # closes the SQLite handle, releasing the file. The same pattern is
  # applied to the project supervisor so no agent GenServer survives to
  # re-lock the file.

  defp stop_project_bounded(project_id, _timeout_ms) do
    case HiveWeave.ProjectSupervisor.supervisor_pid(project_id) do
      nil -> :ok
      pid -> ensure_terminated(pid, 3_000)
    end
  end

  defp stop_repo_bounded(project_id, _timeout_ms) do
    pool =
      try do
        HiveWeave.Repo.ProjectFactory.stop_repo(project_id)
      catch
        :exit, reason ->
          Logger.warning("[delete] stop_repo call failed for #{project_id}: #{inspect(reason)}")
          {:ok, nil}
      end

    case pool do
      {:ok, nil} ->
        Logger.info("[delete] stop_repo returned nil pool for #{project_id} — pool was not tracked")
        :ok

      {:ok, p} ->
        Logger.info("[delete] stop_repo returned pool #{inspect(p)} for #{project_id}")
        # Kill the Exqlite connection processes FIRST — they live under a
        # separate supervisor and are what actually holds the SQLite file
        # handle. Then stop the pool GenServer.
        kill_pool_connections(p)
        ensure_terminated(p, 2_000)
        :ok
    end
  end

  # The DBConnection connection processes (the ones holding the Exqlite /
  # SQLite handle) do NOT live under the pool GenServer. They live under a
  # DBConnection.ConnectionPool.Pool supervisor — itself a child of the
  # global DBConnection.ConnectionPool.Supervisor — and are linked to the
  # pool GenServer only via DBConnection.Watcher. Killing the pool GenServer
  # triggers an async `:sys.terminate` cascade in the Watcher that can HANG
  # (e.g. when a connection is mid-query), leaving the SQLite file locked
  # forever on Windows.
  #
  # So we find THIS pool's Pool-supervisor — the one whose connection
  # child-spec ids embed our pool GenServer pid as `owner` — and terminate it
  # directly. Force-killing the Pool supervisor kills its connection children
  # (linked), whose NIF resource destructor closes the SQLite handle and
  # releases the file.
  defp kill_pool_connections(pool_gen_pid) do
    sup = DBConnection.ConnectionPool.Supervisor

    all_pool_sups =
      try do
        Supervisor.which_children(sup)
      catch
        :exit, reason ->
          Logger.warning("[delete] kill_pool_connections: which_children(sup) failed: #{inspect(reason)}")
          []
      end

    Logger.info("[delete] kill_pool_connections: pool_gen_pid=#{inspect(pool_gen_pid)}, sup_children_count=#{length(all_pool_sups)}")

    try do
      for {_id, pool_sup, _type, _mods} <- all_pool_sups,
          is_pid(pool_sup) do
        if pool_sup_owns?(pool_sup, pool_gen_pid) do
          Logger.info("[delete] found pool supervisor #{inspect(pool_sup)} (owner #{inspect(pool_gen_pid)})")

          # Kill each connection process DIRECTLY. The connection processes
          # hold the Exqlite NIF resource (sqlite3 db handle). Graceful
          # supervisor shutdown calls terminate → disconnect → Sqlite3.close,
          # but that can fail silently (e.g. unfinalized statements on
          # Windows).  A direct :kill is untrappable — the process dies
          # immediately and the NIF resource destructor runs during BEAM GC.
          conn_pids =
            try do
              for {child_id, conn_pid, _t, _m} <- Supervisor.which_children(pool_sup),
                  is_pid(conn_pid),
                  match?({_, ^pool_gen_pid, _}, child_id) do
                conn_pid
              end
            catch
              :exit, _ -> []
            end

          Logger.info("[delete] found #{length(conn_pids)} connection processes to kill")

          for conn_pid <- conn_pids do
            Logger.info("[delete] killing connection #{inspect(conn_pid)}")
            Process.exit(conn_pid, :kill)
          end

          # Wait for connections to actually die
          for conn_pid <- conn_pids do
            ref = Process.monitor(conn_pid)
            receive do
              {:DOWN, ^ref, :process, ^conn_pid, _} -> :ok
            after
              1_000 -> Process.demonitor(ref, [:flush])
            end
          end

          # Now stop the pool supervisor itself
          ensure_terminated(pool_sup, 1_000)
        else
          # Log non-matching pool sups for debugging
          :ok
        end
      end
    catch
      :exit, reason ->
        Logger.warning("[delete] kill_pool_connections enumerate failed: #{inspect(reason)}")
    end

    # Force garbage collection across all processes so the NIF resource
    # destructor (sqlite3_close) actually runs and releases the file handle.
    force_global_gc()

    :ok
  end

  defp force_global_gc do
    Enum.each(Process.list(), fn pid ->
      if Process.alive?(pid) do
        try do
          :erlang.garbage_collect(pid)
        catch
          :error, _ -> :ok
        end
      end
    end)
  end

  defp pool_sup_owns?(pool_sup, pool_gen_pid) do
    try do
      Enum.any?(Supervisor.which_children(pool_sup), fn
        {{_mod, ^pool_gen_pid, _n}, _conn_pid, _t, _m} -> true
        _ -> false
      end)
    catch
      :exit, _ -> false
    end
  end

  # Terminate `pid` (a supervisor / DBConnection pool) with a graceful stop,
  # then force-kill if it doesn't exit within a grace window. Always waits
  # for the :DOWN so the caller can be sure the process — and any file
  # handles it owns — is gone before proceeding.
  defp ensure_terminated(pid, graceful_ms) do
    ref = Process.monitor(pid)

    try do
      GenServer.stop(pid, :normal, graceful_ms)
    catch
      :exit, reason ->
        Logger.warning("[delete] graceful stop of #{inspect(pid)} failed: #{inspect(reason)}")
    end

    receive do
      {:DOWN, ^ref, :process, ^pid, _reason} ->
        :ok
    after
      500 ->
        Process.demonitor(ref, [:flush])
        Process.exit(pid, :kill)
        Logger.warning("[delete] force-killing #{inspect(pid)}")
        receive do
          {:DOWN, ^ref, :process, ^pid, _reason} -> :ok
        after
          2_000 ->
            Logger.error("[delete] #{inspect(pid)} did not die after :kill")
            :ok
        end
    end

    :ok
  end

  defp cleanup_project_workspace(workspace_path) do
    hw_dir = Path.join(workspace_path, ".hiveweave")
    db_path = Path.join(hw_dir, "data.db")

    cleanup_git_worktrees(workspace_path)

    # The pool connections were just killed and global GC was forced.
    # Give the OS a beat to actually release the file handle after the
    # NIF resource destructor (sqlite3_close) has run.
    Process.sleep(500)

    db_deleted =
      case delete_file_with_retry(db_path) do
        :ok ->
          true
        {:error, reason} ->
          Logger.error("Failed to delete #{db_path} after retries: #{inspect(reason)}")
          false
      end

    # Force-remove the entire .hiveweave directory.
    # File.rm_rf on Windows often fails on non-empty subdirectories
    # (worktrees, tool_outputs) or when files are still locked.
    # Fall back to `cmd /c rd /s /q` which is more aggressive.
    force_remove_dir(hw_dir)

    # If the DB file still couldn't be deleted (e.g. pool release was
    # delayed), keep retrying in the background so it isn't left behind
    # forever. Best-effort; never blocks the HTTP response.
    unless db_deleted do
      spawn(fn -> async_cleanup_db(db_path, hw_dir) end)
    end

    not db_deleted
  end

  # Aggressively remove a directory tree on Windows.
  # Tries File.rm_rf first; if that fails, falls back to `cmd /c rd /s /q`
  # which ignores read-only attributes and forces recursive deletion.
  defp force_remove_dir(dir) do
    case File.rm_rf(dir) do
      {:ok, _} ->
        :ok

      {:error, reason, failed_path} ->
        Logger.warning("File.rm_rf partial fail at #{inspect(failed_path)}: #{inspect(reason)}, trying cmd rd /s /q")

        # Windows fallback: rd /s /q is the most forceful directory removal.
        # /s = remove all subdirectories and files; /q = quiet, no confirmation
        case System.cmd("cmd", ["/c", "rd", "/s", "/q", dir], stderr_to_stdout: true) do
          {_, 0} ->
            Logger.info("rd /s /q succeeded for #{dir}")
            :ok

          {output, _exit} ->
            Logger.error("rd /s /q also failed for #{dir}: #{output}")
            {:error, reason}
        end
    end
  end

  defp async_cleanup_db(db_path, hw_dir) do
    Enum.reduce_while(1..15, :ok, fn n, _ ->
      Process.sleep(2_000)
      case File.rm(db_path) do
        :ok ->
          Logger.info("[delete] async cleanup deleted #{db_path} on attempt #{n}")
          force_remove_dir(hw_dir)
          {:halt, :ok}

        {:error, :enoent} ->
          # DB already gone, but .hiveweave dir might still have subdirs
          force_remove_dir(hw_dir)
          {:halt, :ok}

        {:error, _reason} ->
          {:cont, :ok}
      end
    end)
  end

  # Background cleanup: called via spawn/1 after the delete API has already
  # returned. Retries file deletion for up to ~60 seconds, then gives up.
  defp cleanup_project_workspace_background(workspace_path, project_id) do
    hw_dir = Path.join(workspace_path, ".hiveweave")
    db_path = Path.join(hw_dir, "data.db")

    # Clean up git worktrees first (best-effort)
    cleanup_git_worktrees(workspace_path)

    # Retry loop: try every 2 seconds for 30 attempts (60 seconds total).
    # The SQLite file handle is released when the BEAM GC collects the Exqlite
    # NIF resource. This can take several seconds on Windows.
    result =
      Enum.reduce_while(1..30, :not_deleted, fn n, _acc ->
        Process.sleep(2_000)

        # Force GC before each attempt — the NIF resource destructor
        # (sqlite3_close) runs during GC.
        force_global_gc()

        case File.rm(db_path) do
          :ok ->
            Logger.info("[delete-bg] Deleted #{db_path} on attempt #{n} (project #{project_id})")
            {:halt, :ok}

          {:error, :enoent} ->
            Logger.info("[delete-bg] #{db_path} already gone (attempt #{n})")
            {:halt, :ok}

          {:error, reason} ->
            Logger.debug("[delete-bg] Attempt #{n}/30 failed for #{db_path}: #{inspect(reason)}")
            {:cont, :not_deleted}
        end
      end)

    # Try to remove the entire .hiveweave directory
    force_remove_dir(hw_dir)

    result
  end

  # Best-effort cleanup of hiveweave-managed git worktrees and branches.
  # Mirrors apps/server/src/routes/projects.ts:282-311.
  # Kill processes whose command line references the .hiveweave/worktrees path.
  # On Windows, these lock files and block directory deletion.
  # Uses PowerShell via System.cmd for cross-platform compatibility (no-op on non-Windows).
  defp kill_worktree_processes(workspace_path) do
    wt_dir = Path.join(workspace_path, ".hiveweave/worktrees") |> String.replace("/", "\\")

    case :os.type() do
      {:win32, _} ->
        # Find and kill node/esbuild/vite/next processes whose CommandLine
        # references the worktrees path. PowerShell gives us CommandLine access.
        # Pass wt_dir via environment variable to prevent command injection
        # through workspace_path containing quotes, $, or other special chars.
        script = """
        $wtDir = $env:HW_WT_DIR
        if (-not (Test-Path $wtDir)) { exit 0 }
        $procs = Get-CimInstance Win32_Process | Where-Object {
            $_.CommandLine -like "*$wtDir*" -and
            $_.Name -in @("node.exe", "esbuild.exe", "next-server.exe", "vite.exe")
        }
        foreach ($p in $procs) {
            try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop; Write-Host "Killed $($p.Name) PID $($p.ProcessId)" }
            catch { Write-Host "Failed to kill PID $($p.ProcessId): $_" }
        }
        """

        tmp = System.tmp_dir!() |> Path.join("hw_kill_procs_#{System.unique_integer([:positive])}.ps1")
        File.write!(tmp, script)

        try do
          {output, 0} = System.cmd("powershell", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", tmp], env: [{"HW_WT_DIR", wt_dir}], stderr_to_stdout: true)
          if String.length(output) > 0 do
            Logger.info("[delete] Worktree process cleanup: #{String.trim(output)}")
          end
          # Give processes time to release file handles
          Process.sleep(1000)
        rescue
          e -> Logger.warning("kill_worktree_processes failed: #{inspect(e)}")
        after
          File.rm(tmp)
        end

      _ ->
        :ok
    end
  end

  defp cleanup_git_worktrees(workspace_path) do
    # Kill processes whose working directory is under .hiveweave/worktrees/.
    # Agents may have started dev servers (next dev, vite, esbuild) inside
    # worktrees; these lock files and prevent directory deletion on Windows.
    kill_worktree_processes(workspace_path)

    try do
      {out, _} =
        System.cmd(
          "git",
          ["-C", workspace_path, "worktree", "list", "--porcelain"],
          stderr_to_stdout: true
        )

      out
      |> String.split("\n", trim: true)
      |> Enum.filter(&String.starts_with?(&1, "worktree "))
      |> Enum.map(&String.trim_leading(&1, "worktree "))
      |> Enum.filter(&(Path.relative_to(&1, workspace_path) |> String.starts_with?(".hiveweave")))
      |> Enum.each(fn wt_path ->
        try do
          {_output, exit} =
            System.cmd("git", ["-C", workspace_path, "worktree", "remove", "--force", wt_path],
              stderr_to_stdout: true
            )

          # If git worktree remove failed (non-zero exit), force-delete the dir.
          # Common on Windows with Chinese paths or locked files.
          if exit != 0 do
            Logger.warning("git worktree remove failed for #{wt_path} (exit #{exit}), force-removing dir")
            force_remove_dir(wt_path)
          end
        rescue
          e ->
            Logger.warning("git worktree remove raised for #{wt_path}: #{inspect(e)}")
            force_remove_dir(wt_path)
        end
      end)
    rescue
      _ -> :ok
    end

    try do
      {out, _} =
        System.cmd("git", ["-C", workspace_path, "branch", "--list", "hw/*"], stderr_to_stdout: true)

      out
      |> String.split("\n", trim: true)
      |> Enum.map(&String.trim(&1, " *"))
      |> Enum.each(fn branch ->
        try do
          System.cmd("git", ["-C", workspace_path, "branch", "-D", branch], stderr_to_stdout: true)
        rescue
          _ -> :ok
        end
      end)
    rescue
      _ -> :ok
    end

    try do
      System.cmd("git", ["-C", workspace_path, "worktree", "prune"], stderr_to_stdout: true)
    rescue
      _ -> :ok
    end

    :ok
  end

  # Delete a single file with exponential backoff. On Windows the OS may
  # take a moment to release the SQLite handle even after the pool was
  # killed, so we retry up to ~12s total (150, 300, 600, 1200, 2400, 3000,
  # 3000, 3000ms) before giving up (the async fallback keeps trying after).
  defp delete_file_with_retry(path) do
    do_delete_with_retry(path, 8, 150)
  end

  defp do_delete_with_retry(_path, 0, _backoff), do: {:error, :exhausted}

  defp do_delete_with_retry(path, attempts, backoff) do
    case File.rm(path) do
      :ok -> :ok
      {:error, :enoent} -> :ok
      {:error, _reason} ->
        Process.sleep(backoff)
        do_delete_with_retry(path, attempts - 1, min(backoff * 2, 3_000))
    end
  end

  def game_time(conn, %{"id" => project_id}) do
    seconds = HiveWeave.GameTime.Server.get_current_time(project_id)
    # Format game time as "Day N HH:MM" (REAL_SECONDS_PER_GAME_DAY = 3600)
    {day, time_in_day} = if seconds > 0 do
      {div(seconds, 3600) + 1, rem(seconds, 3600)}
    else
      {1, 0}
    end
    # 3600 real seconds = 1 game day (24h), so 1 game hour = 150s, 1 game minute = 2.5s
    total_minutes = div(time_in_day * 24 * 60, 3600)
    hours = div(total_minutes, 60)
    mins = rem(total_minutes, 60)
    formatted = "Day #{day} #{String.pad_leading(Integer.to_string(hours), 2, "0")}:#{String.pad_leading(Integer.to_string(mins), 2, "0")}"
    json(conn, %{gameSeconds: seconds, projectId: project_id, formatted: formatted})
  end

  def goals(conn, %{"id" => project_id}) do
    case get_project(project_id) do
      nil ->
        conn
        |> put_status(404)
        |> json(%{error: "Not found"})
      project ->
        goals = if project.charter_json, do: safe_decode(project.charter_json), else: nil
        json(conn, %{goals: goals})
    end
  end

  def update_goals(conn, %{"id" => project_id} = params) do
    case get_project(project_id) do
      nil ->
        conn
        |> put_status(404)
        |> json(%{error: "Not found"})
      project ->
        project
        |> Project.changeset(%{charter_json: Jason.encode!(params)})
        |> HiveWeave.Repo.Meta.update()
        |> case do
          {:ok, p} ->
            HiveWeave.Services.Charter.touch_goals_version(project_id)
            json(conn, %{ok: true, project: serialize_project(p)})
          {:error, _} -> json(conn, %{error: "Failed to update goals"}) |> Plug.Conn.put_status(500)
        end
    end
  end

  def update_workspace(conn, %{"id" => project_id} = params) do
    case get_project(project_id) do
      nil ->
        conn
        |> put_status(404)
        |> json(%{error: "Not found"})

      project ->
        new_path = params["workspacePath"]

        cond do
          # Case 1: path is nil — clear workspace, evict DB, unregister agents
          new_path == nil or new_path == "" ->
            # Stop project supervisor — kills all agent GenServers (unregister agents)
            try do
              HiveWeave.ProjectSupervisor.stop_project(project.id)
            catch
              :exit, _ -> :ok
            end

            # Evict the per-project DB pool so the SQLite file handle is released
            HiveWeave.Repo.ProjectFactory.evict(project.id)

            # Clear workspace_path in the project record
            case project
                 |> Project.changeset(%{workspace_path: nil})
                 |> HiveWeave.Repo.Meta.update() do
              {:ok, updated} ->
                json(conn, %{ok: true, project: serialize_project(updated)})
              {:error, _} ->
                conn |> put_status(500) |> json(%{error: "Failed to clear workspace path"})
            end

          # Case 2: path unchanged — no-op
          new_path == project.workspace_path ->
            json(conn, %{ok: true, project: serialize_project(project)})

          # Case 3: path changed — move .hiveweave/, update record, re-register agents
          true ->
            # Validate the new workspace path exists and is a directory
            cond do
              not is_binary(new_path) or String.trim(new_path) == "" ->
                conn |> put_status(400) |> json(%{error: "workspacePath must be a non-empty string"})

              not File.dir?(new_path) ->
                conn |> put_status(400) |> json(%{error: "Workspace path does not exist or is not a directory: #{new_path}"})

              true ->
                old_path = project.workspace_path

            # Stop project supervisor to release DB handles
            try do
              HiveWeave.ProjectSupervisor.stop_project(project.id)
            catch
              :exit, _ -> :ok
            end

            # Evict old DB pool
            HiveWeave.Repo.ProjectFactory.evict(project.id)

            # Give the OS a beat to release the SQLite file handle after the
            # pool is killed (NIF resource destructor runs during GC).
            Process.sleep(500)

            # Copy .hiveweave/ directory from old to new location (if old exists)
            if old_path && old_path != "" do
              old_hw = Path.join(old_path, ".hiveweave")

              if File.exists?(old_hw) do
                new_hw = Path.join(new_path, ".hiveweave")

                # Ensure parent directory exists
                File.mkdir_p(new_hw)

                # Copy old .hiveweave/ to new location
                case File.cp_r(old_hw, new_hw) do
                  {:ok, _} ->
                    # Delete old .hiveweave/ directory
                    File.rm_rf(old_hw)
                  {:error, reason, _file} ->
                    Logger.warning("Failed to copy .hiveweave/ to new workspace: #{inspect(reason)}")
                end
              end
            end

            # Update project record with new workspace_path
            case project
                 |> Project.changeset(%{workspace_path: new_path})
                 |> HiveWeave.Repo.Meta.update() do
              {:ok, updated} ->
                # Sync workspace_path in agents table to match the project record.
                # The agents table has a redundant workspace_path column used as
                # a fallback by project_factory.ex:open_project_db. Without this
                # sync, a DB recovery would open the old (deleted) workspace.
                try do
                  HiveWeave.Repo.Meta.query!(
                    "UPDATE agents SET workspace_path = ? WHERE project_id = ?",
                    [new_path, project.id]
                  )
                rescue
                  e -> Logger.warning("Failed to sync agents.workspace_path: #{inspect(e)}")
                end

                # Re-register agents by restarting the project supervisor
                case HiveWeave.ProjectSupervisor.start_project(updated.id, new_path) do
                  {:ok, _} -> :ok
                  {:error, {:already_started, _}} -> :ok
                  other -> Logger.warning("Failed to restart project supervisor: #{inspect(other)}")
                end

                json(conn, %{ok: true, project: serialize_project(updated)})

              {:error, _} ->
                conn |> put_status(500) |> json(%{error: "Failed to update workspace path"})
            end
          end
        end
    end
  end

  defp get_project(id) do
    case Ecto.UUID.cast(id) do
      {:ok, uuid} -> HiveWeave.Repo.Meta.get(Project, uuid)
      :error ->
        HiveWeave.Repo.Meta.one(from(p in Project, where: p.id == ^id))
    end
  rescue
    _ -> nil
  end

  defp ensure_project_booted(project) do
    # Check if project is already running
    case Registry.lookup(HiveWeave.ProjectRegistry, project.id) do
      [] ->
        # Not running — boot it
        ws = project.workspace_path || ""
        case HiveWeave.ProjectSupervisor.start_project(project.id, ws) do
          {:ok, _} -> :ok
          {:error, {:already_started, _}} -> :ok
          other -> IO.warn("Auto-boot failed for project #{project.id}: #{inspect(other)}")
        end
      _ -> :ok  # Already running
    end
  rescue
    _ -> :ok
  end

  defp serialize_project(nil), do: nil
  defp serialize_project(p) do
    %{
      id: p.id,
      name: p.name,
      description: p.description,
      workspace_path: p.workspace_path,
      workspacePath: p.workspace_path,
      org_paradigm: p.org_paradigm,
      orgParadigm: p.org_paradigm,
      charter_json: p.charter_json,
      charterJson: p.charter_json,
      created_at: p.created_at,
      createdAt: p.created_at
    }
  end

  defp safe_decode(nil), do: nil
  defp safe_decode(str) do
    try do
      Jason.decode!(str)
    rescue
      _ -> nil
    end
  end

  defp changeset_errors(changeset) do
    Ecto.Changeset.traverse_errors(changeset, fn {msg, _} -> msg end)
  end

  # Auto-create CEO and HR agents on project creation.
  # Mirrors the TS backend's startup behavior in apps/server/src/index.ts.
  defp ensure_ceo_hr(project_id) do
    # Skip if CEO already exists
    existing_ceo =
      HiveWeave.Repo.Meta.one(
        from(a in Agent, where: a.project_id == ^project_id and a.role == "ceo", limit: 1)
      )

    if existing_ceo do
      existing_ceo.id
    else
      now = System.system_time(:millisecond)

      # Derive unique short_ids scoped to this project (existing schema has a
      # global unique index on short_id, so we include the project id suffix).
      project_suffix =
        project_id
        |> to_string()
        |> String.replace("-", "")
        |> String.slice(0, 6)
        |> String.upcase()

      ceo_short = "A001-#{project_suffix}"
      hr_short = "A002-#{project_suffix}"
      qa_short = "A003-#{project_suffix}"

      ceo_name = generate_flower_name()
      ceo_id = Ecto.UUID.generate()

      # Pick a default model: prefer the first active model that's NOT opencode.ai/zen
      # (those free gateways often don't support tool calling)
      default_model_id = pick_default_model()

      # Default skills for CEO: strategic planning, spec-driven, documentation,
      # context management, and the meta-skill for using other skills.
      ceo_skills = [
        "planning-and-task-breakdown",
        "spec-driven-development",
        "documentation-and-adrs",
        "doubt-driven-development",
        "context-engineering",
        "using-agent-skills"
      ]
      ceo_skills_json = Jason.encode!(ceo_skills)

      ceo_attrs = %{
        id: ceo_id,
        short_id: ceo_short,
        project_id: project_id,
        name: ceo_name,
        role: "ceo",
        parent_id: nil,
        status: "active",
        goal: "维护项目章程；选定组织范式；协调业务负责人",
        backstory:
          "花名#{ceo_name}，35岁，三次创业两次失败。第一次死在现金流，第二次死在合伙人跑路。第三次总算活了下来，但因为太累把公司卖了。现在只想用AI搭一个不会吵架的团队。口头禅：不急，先把方向聊清楚。",
        skills: ceo_skills_json,
        model_id: default_model_id,
        permission_type: "coordinator",
        permission_mode: "full",
        allowed_tools: "[]",
        denied_tools: "[]",
        ask_tools: "[]",
        mcp_servers: "[]",
        bound_skills: ceo_skills_json,
        created_at: now,
        updated_at: now
      }

      case %Agent{}
           |> Ecto.Changeset.change(ceo_attrs)
           |> HiveWeave.Repo.Meta.insert() do
        {:ok, _} ->
          # Create HR under CEO
          hr_name = generate_flower_name()
          hr_id = Ecto.UUID.generate()

          # Default skills for HR: interview/hiring, documentation,
          # and the meta-skill for using other skills.
          hr_skills = [
            "interview-me",
            "documentation-and-adrs",
            "using-agent-skills"
          ]
          hr_skills_json = Jason.encode!(hr_skills)

          hr_attrs = %{
            id: hr_id,
            short_id: hr_short,
            project_id: project_id,
            name: hr_name,
            role: "hr",
            parent_id: ceo_id,
            status: "active",
            goal: "人员招聘与配置；协调 agent 间协作",
            backstory:
              "花名#{hr_name}，32岁，前身是某大厂HRBP。因为帮一位被裁的同事争取到了超额补偿，被上级视为不够冷酷而调离。离职后决定用自己的方式帮人找到合适的位置。",
            skills: hr_skills_json,
            model_id: default_model_id,
            permission_type: "coordinator",
            permission_mode: "full",
            allowed_tools: "[]",
            denied_tools: "[]",
            ask_tools: "[]",
            mcp_servers: "[]",
            bound_skills: hr_skills_json,
            created_at: now,
            updated_at: now
          }

          case %Agent{}
               |> Ecto.Changeset.change(hr_attrs)
               |> HiveWeave.Repo.Meta.insert() do
            {:ok, _} ->
              # Create QA Engineer under CEO
              qa_name = generate_flower_name()
              qa_id = Ecto.UUID.generate()

              # Default skills for QA Engineer: code review and testing.
              qa_skills = [
                "code-review-and-quality",
                "test-driven-development"
              ]
              qa_skills_json = Jason.encode!(qa_skills)

              qa_attrs = %{
                id: qa_id,
                short_id: qa_short,
                project_id: project_id,
                name: qa_name,
                role: "qa_engineer",
                parent_id: ceo_id,
                status: "active",
                goal: "代码质量保证与测试审查",
                backstory:
                  "花名#{qa_name}，30岁，曾在大厂担任测试架构师。一次发布事故中，因为自动化测试覆盖不足导致线上故障，从此立誓要让每一段代码都经得起考验。信奉'质量不是检查出来的，是构建出来的'。",
                skills: qa_skills_json,
                model_id: default_model_id,
                permission_type: "executor",
                permission_mode: "full",
                allowed_tools: "[]",
                denied_tools: "[]",
                ask_tools: "[]",
                mcp_servers: "[]",
                bound_skills: qa_skills_json,
                created_at: now,
                updated_at: now
              }

              case %Agent{}
                   |> Ecto.Changeset.change(qa_attrs)
                   |> HiveWeave.Repo.Meta.insert() do
                {:ok, _} ->
                  # Create roster record for QA Engineer
                  try do
                    HiveWeave.Services.Roster.update_roster(project_id, qa_id, %{
                      position: "质量保证工程师",
                      department: "质量部"
                    })
                  rescue
                    _ -> :ok
                  end
                  ceo_id

                {:error, _} -> ceo_id
              end

            {:error, _} -> ceo_id
          end

        {:error, _} ->
          nil
      end
    end
  end

  # Pick a default model for new agents: prefer models that support tool calling
  # (skip free gateways like opencode.ai/zen which often don't support tools)
  defp pick_default_model do
    # 1. Try global setting "default_model_coordinator" first
    case get_global_setting("default_model_coordinator") do
      nil -> :not_found
      model_id ->
        # Verify the model exists and is active
        {:ok, r} = Ecto.Adapters.SQL.query(
          HiveWeave.Repo.Meta,
          "SELECT id FROM llm_models WHERE id = ? AND is_active = 1",
          [model_id]
        )
        case r.rows do
          [[^model_id]] -> model_id
          _ -> :not_found
        end
    end
    |> case do
      model_id when is_binary(model_id) -> model_id
      :not_found ->
        # 2. Fall back: first active non-opencode model
        {:ok, r} = Ecto.Adapters.SQL.query(
          HiveWeave.Repo.Meta,
          "SELECT id, base_url FROM llm_models WHERE is_active = 1 ORDER BY created_at ASC",
          []
        )
        preferred =
          r.rows
          |> Enum.find(fn [_id, base_url] ->
            url = base_url || ""
            not String.contains?(url, "opencode.ai") and not String.contains?(url, "/zen/")
          end)
        case preferred do
          [id | _] -> id
          nil ->
            case r.rows do
              [[id | _] | _] -> id
              [] -> nil
            end
        end
    end
  rescue
    _ -> nil
  end

  defp get_global_setting(key) do
    {:ok, r} = Ecto.Adapters.SQL.query(
      HiveWeave.Repo.Meta,
      "SELECT value FROM global_settings WHERE key = ?",
      [key]
    )
    case r.rows do
      [[value]] -> value
      _ -> nil
    end
  rescue
    _ -> nil
  end

  # Simplified flower-name generator (Chinese style names).
  defp generate_flower_name do
    surnames = ~w(苏 林 陈 黄 周 吴 徐 孙 马 朱 胡 郭 何 罗 郑 梁 谢 宋 唐 韩 曹 许)

    given_names = [
      "清秋",
      "听雪",
      "观潮",
      "摘星",
      "踏雪",
      "问月",
      "望舒",
      "怀瑾",
      "昭明",
      "思齐",
      "明远",
      "行之",
      "知言",
      "守拙",
      "归真",
      "蕴之",
      "予安",
      "寄北",
      "怀瑜",
      "如琢"
    ]

    Enum.random(surnames) <> Enum.random(given_names)
  end
end

