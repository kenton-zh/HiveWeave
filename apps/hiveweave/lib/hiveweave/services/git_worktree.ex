defmodule HiveWeave.Services.GitWorktree do
  @moduledoc """
  GitWorktreeService — isolated worktrees per agent, managed by coordinators.

  Each leaf agent gets an isolated git worktree under `.hiveweave/worktrees/<shortId>/`
  on branch `hw/<shortId>/<task-slug>`. Coordinators control the full lifecycle.

  Design (mirrors TS):
    - Tools are coordinator-only — executors cannot create/merge worktrees.
    - Checkpoints are lightweight commits (add -A + commit) on the agent branch.
    - Merge is a fast-forward merge into the main branch, then cleanup.
    - Rollback is git reset --hard to a specific commit (or HEAD~1 by default).
  """

  require Logger

  @worktree_dir ".hiveweave/worktrees"
  @checkpoint_prefix "checkpoint:"
  @git_timeout_ms 30_000

  # ── Helpers ──────────────────────────────────────────────────

  defp git(args, cwd, _timeout \\ @git_timeout_ms)

  defp git(args, cwd, _timeout) when is_binary(args) do
    git(parse_git_args(args), cwd)
  end

  defp git(args, cwd, _timeout) when is_list(args) do
    cmd_name = hd(args)
    case System.cmd("git", args, cd: cwd, stderr_to_stdout: true) do
      {output, 0} -> String.trim(output)
      {output, code} ->
        raise "git #{cmd_name} failed (exit #{code}): #{String.slice(output, 0, 300)}"
    end
  rescue
    e in RuntimeError -> reraise e, __STACKTRACE__
    e -> raise "git call failed: #{inspect(e)}"
  end

  defp git_safe(args, cwd) do
    try do
      {:ok, git(args, cwd)}
    rescue
      e ->
        Logger.warning("[GitWorktree] git command failed: #{inspect(e)}")
        {:error, :git_failed}
    end
  end

  defp parse_git_args(str) do
    # Simple arg parser: split on space, preserve quoted strings
    str
    |> String.trim()
    |> split_quoted()
  end

  defp split_quoted(str) do
    split_quoted(str, "", [], false)
  end

  defp split_quoted("", current, acc, _in_quote) do
    if current != "", do: Enum.reverse([current | acc]), else: Enum.reverse(acc)
  end

  defp split_quoted(<<?", rest::binary>>, current, acc, false) do
    split_quoted(rest, current, acc, true)
  end

  defp split_quoted(<<?", rest::binary>>, current, acc, true) do
    split_quoted(rest, current, acc, false)
  end

  defp split_quoted(<<" ", rest::binary>>, current, acc, false) do
    if current != "" do
      split_quoted(rest, "", [current | acc], false)
    else
      split_quoted(rest, "", acc, false)
    end
  end

  defp split_quoted(<<c::utf8, rest::binary>>, current, acc, in_quote) do
    split_quoted(rest, current <> <<c::utf8>>, acc, in_quote)
  end

  defp slugify(name) do
    name
    |> String.replace(~r/[\s\/\\]+/, "-")
    |> String.replace(~r/[^a-zA-Z0-9_\-\x{4e00}-\x{9fff}]+/u, "")
    |> String.slice(0, 40)
    |> String.replace(~r/^-+|-+$/, "")
    |> case do
      "" -> "task"
      s -> s
    end
  end

  defp branch_name(short_id, task_name) do
    "hw/#{short_id}/#{slugify(task_name)}"
  end

  defp worktree_path(workspace_path, short_id) do
    Path.join([workspace_path, @worktree_dir, short_id])
  end

  defp dot_git?(path) do
    File.exists?(Path.join(path, ".git"))
  end

  defp rename_to_main(workspace_path) do
    # Try to rename master->main; ignore failure (may already be "main" or "trunk")
    case git_safe("branch -m master main", workspace_path) do
      {:ok, _} -> {:ok, :renamed}
      {:error, _} -> {:ok, :already_main}
    end
  end

  defp ensure_git_identity(workspace_path) do
    # Set local git identity if not already configured (needed for commits)
    _ = git_safe("config user.email \"hiveweave@agent.local\"", workspace_path)
    _ = git_safe("config user.name \"HiveWeave Agent\"", workspace_path)
    :ok
  end

  defp create_worktree_branch(workspace_path, path, branch, base_branch) do
    fwd_path = String.replace(path, "\\", "/")

    attempts = [
      ~s|worktree add #{fwd_path} -b #{branch} origin/#{base_branch}|,
      ~s|worktree add #{fwd_path} -b #{branch} #{base_branch}|,
      ~s|worktree add #{fwd_path} -b #{branch} master|,
    ]

    result =
      Enum.find_value(attempts, fn cmd ->
        case git_safe(cmd, workspace_path) do
          {:ok, _} -> {:ok, %{path: path, branch: branch}}
          {:error, _} -> nil
        end
      end)

    case result do
      nil ->
        Logger.error("[GitWorktree] All worktree add attempts failed. workspace=#{workspace_path}, path=#{path}, branch=#{branch}")
        {:error, "Failed to create worktree"}
      ok -> ok
    end
  end

  @doc """
  Get the absolute path to an agent's worktree directory.
  Returns nil if no worktree exists.
  """
  def get_worktree_path(workspace_path, short_id) do
    path = worktree_path(workspace_path, short_id)
    if dot_git?(path), do: path, else: nil
  end

  @doc """
  Ensure the workspace is a git repo. Auto-inits if not.
  Returns {:ok, initialized_bool} or {:error, reason}.
  """
  def ensure_git_repo(workspace_path) do
    dot_git = Path.join(workspace_path, ".git")

    if File.exists?(dot_git) do
      {:ok, false}
    else
      case git_safe("--version", workspace_path) do
        {:error, _} ->
          {:error, "Git is not installed or not on PATH."}

        {:ok, _} ->
          with {:ok, _} <- git_safe("init", workspace_path),
               {:ok, _} <- rename_to_main(workspace_path),
               # Ensure git user config exists (needed for commit)
               :ok <- ensure_git_identity(workspace_path),
               {:ok, _} <- git_safe(
                 ~s|commit --allow-empty -m "root: initialized by HiveWeave"|,
                 workspace_path
               ) do
            Logger.info("[GitWorktree] Initialized git repo at #{workspace_path}")
            {:ok, true}
          else
            {:error, _} -> {:error, "Failed to initialize git repository."}
          end
      end
    end
  end

  # ── 1. CREATE ────────────────────────────────────────────────

  @doc """
  Allocate an isolated worktree + branch for a subordinate agent.
  Returns {:ok, %{path: ..., branch: ...}} or {:error, reason}.
  """
  def create(workspace_path, short_id, task_name, base_branch \\ "main") do
    # Auto-init git if workspace isn't a repo yet
    case ensure_git_repo(workspace_path) do
      {:error, reason} -> {:error, reason}
      {:ok, _initialized} -> do_create(workspace_path, short_id, task_name, base_branch)
    end
  end

  defp do_create(workspace_path, short_id, task_name, base_branch) do
    wt_root = Path.join(workspace_path, @worktree_dir)
    File.mkdir_p!(wt_root)

    path = worktree_path(workspace_path, short_id)
    branch = branch_name(short_id, task_name)

    # Already exists
    if dot_git?(path) do
      {:ok, %{path: path, branch: branch}}
    else
      # Try remote branch first, then local main, then master
      result =
        create_worktree_branch(workspace_path, path, branch, base_branch)

      Logger.info("[GitWorktree] Created worktree for #{short_id}: #{path}")
      result
    end
  end

  # ── 2. CHECKPOINT ────────────────────────────────────────────

  @doc """
  Snapshot current state in the agent's worktree (git add -A + commit).
  Returns {:ok, %{hash: ..., count: ...}} or {:error, reason}.
  """
  def checkpoint(workspace_path, short_id, message) do
    path = worktree_path(workspace_path, short_id)

    unless File.dir?(path) do
      {:error, "Worktree for #{short_id} does not exist."}
    else
      # Stage everything
      case git_safe("add -A", path) do
        {:error, _} -> {:error, "Failed to stage files"}
        {:ok, _} ->
          # Check if there's anything to commit
          case git_safe("status --porcelain", path) do
            {:ok, status} when status == "" ->
              {:ok, git_safe("rev-parse --short HEAD", path) |> elem(1) |> then(fn h -> %{hash: h, count: 0} end)}

            _ ->
              commit_msg = "#{@checkpoint_prefix} #{message}"
              escaped_msg = String.replace(commit_msg, "\"", "\\\"")

              case git_safe(~s|commit -m "#{escaped_msg}"|, path) do
                {:error, _} -> {:error, "Failed to create checkpoint commit"}
                {:ok, _} ->
                  hash = git_safe("rev-parse --short HEAD", path) |> elem(1)
                  count = count_checkpoints(path)
                  {:ok, %{hash: hash, count: count}}
              end
          end
      end
    end
  end

  defp count_checkpoints(path) do
    case git_safe(~s|log --oneline --grep="#{@checkpoint_prefix}" --since="7 days ago"|, path) do
      {:ok, log} when log != "" ->
        log |> String.split("\n") |> length()

      _ -> 1
    end
  end

  # ── 3. MERGE ──────────────────────────────────────────────────

  @doc """
  QA passed → merge agent branch into target, cleanup worktree.
  Returns {:ok, %{merged: true, hash: ...}} or {:error, reason}.
  """
  def merge(workspace_path, short_id, task_name, target_branch \\ "main") do
    branch = branch_name(short_id, task_name)

    with {:ok, _} <- git_safe(~s|checkout "#{target_branch}"|, workspace_path) do
      case git_safe(~s|merge "#{branch}" --no-edit|, workspace_path) do
        {:ok, _} ->
          {:ok, hash} = git_safe("rev-parse --short HEAD", workspace_path)
          _ = remove(workspace_path, short_id, task_name)
          {:ok, %{merged: true, hash: hash}}

        {:error, _} ->
          _ = git_safe("merge --abort", workspace_path)
          {:error, "Merge conflict for #{short_id} into #{target_branch}. Resolve manually or rollback."}
      end
    end
  end

  # ── 4. ROLLBACK ──────────────────────────────────────────────

  @doc """
  Reset agent's worktree to a previous checkpoint (or HEAD~1 by default).
  Returns {:ok, %{hash: ..., message: ...}} or {:error, reason}.
  """
  def rollback(workspace_path, short_id, commit_hash \\ nil) do
    path = worktree_path(workspace_path, short_id)

    unless File.dir?(path) do
      {:error, "Worktree for #{short_id} does not exist."}
    else
      target = if commit_hash do
        commit_hash
      else
        case git_safe(~s|log --format=%H --grep="#{@checkpoint_prefix}" -1|, path) do
          {:ok, h} when h != "" -> h
          _ -> nil
        end
      end

      if is_nil(target) do
        {:error, "No checkpoints found for #{short_id}."}
      else
        with {:ok, _} <- git_safe(~s|reset --hard "#{target}"|, path),
             {:ok, hash} <- git_safe("rev-parse --short HEAD", path),
             {:ok, msg} <- git_safe("log -1 --format=%s", path) do
          {:ok, %{hash: hash, message: msg}}
        else
          {:error, _} -> {:error, "Rollback failed for #{short_id}"}
        end
      end
    end
  end

  # ── 5. REMOVE ────────────────────────────────────────────────

  @doc """
  Discard agent's worktree (rejected/obsolete work).
  Returns {:ok, %{removed: true}}.
  """
  def remove(workspace_path, short_id, task_name \\ nil) do
    path = worktree_path(workspace_path, short_id)

    # Prune worktree from git's registry
    case git_safe(~s|worktree remove "#{path}" --force|, workspace_path) do
      {:ok, _} -> :ok
      {:error, _} ->
        # Worktree may not be registered — delete directory manually
        try do
          System.cmd("cmd", ["/c", "rmdir /s /q \"#{path}\""], timeout: 10_000)
        rescue
          _ -> :ok
        end
    end

    # Delete the branch
    if task_name do
      branch = branch_name(short_id, task_name)
      _ = git_safe(~s|branch -D "#{branch}"|, workspace_path)
    end

    {:ok, %{removed: true}}
  end

  # ── 6. LIST ──────────────────────────────────────────────────

  @doc """
  List all HiveWeave-managed worktrees.
  Returns {:ok, worktree_entries} or {:ok, []}.
  """
  def list(workspace_path) do
    case git_safe("worktree list", workspace_path) do
      {:ok, raw} ->
        entries =
          raw
          |> String.split("\n")
          |> Enum.map(&String.trim/1)
          |> Enum.flat_map(fn line ->
            # Format: "<path>  <hash> [<branch>]"
            case Regex.run(~r/^(.+?)\s+([a-f0-9]+)\s*(?:\[(.+?)\])?$/, line) do
              nil -> []
              [_, wt_path, hash, branch] ->
                dir_name = Path.basename(wt_path)

                if String.contains?(wt_path, @worktree_dir) do
                  [%{
                    short_id: dir_name,
                    path: String.trim(wt_path),
                    branch: (branch || "") |> String.trim(),
                    head: String.slice(String.trim(hash), 0, 7),
                    active: File.exists?(String.trim(wt_path))
                  }]
                else
                  []
                end
            end
          end)

        {:ok, entries}

      {:error, _} -> {:ok, []}
    end
  end

  # ── 7. STATUS ────────────────────────────────────────────────

  @doc """
  Detailed status of one agent's worktree.
  Returns {:ok, status_map} or {:ok, nil} if not found.
  """
  def status(workspace_path, short_id) do
    path = worktree_path(workspace_path, short_id)

    unless File.dir?(path) do
      {:ok, nil}
    else
      with {:ok, head} <- git_safe("rev-parse --short HEAD", path),
           {:ok, branch} <- git_safe("rev-parse --abbrev-ref HEAD", path) do

        has_uncommitted =
          case git_safe("status --porcelain", path) do
            {:ok, st} -> byte_size(st) > 0
            _ -> true
          end

        checkpoints =
          case git_safe(
                 ~s|log --oneline --grep="#{@checkpoint_prefix}" -20|, path
               ) do
            {:ok, log} when log != "" ->
              entries =
                log
                |> String.split("\n")
                |> Enum.map(fn line ->
                  case Regex.run(~r/^([a-f0-9]+)\s+(.+)$/, line) do
                    [_, h, msg] ->
                      %{
                        hash: h,
                        date: "",
                        message: String.replace_prefix(msg, @checkpoint_prefix <> " ", "")
                      }
                    nil -> nil
                  end
                end)
                |> Enum.reject(&is_nil/1)

              # Enrich with dates via fuller log
              enrich_with_dates(path, entries)

            _ -> []
          end

        {:ok, %{
          short_id: short_id,
          branch: branch,
          active: true,
          has_uncommitted: has_uncommitted,
          head: head,
          checkpoints: checkpoints
        }}
      else
        {:error, _} -> {:ok, nil}
      end
    end
  end

  defp enrich_with_dates(path, checkpoints) do
    case git_safe(
           ~s'log --format="%h|%ad|%s" --date=short --grep="#{@checkpoint_prefix}" -20',
           path
         ) do
      {:ok, full_log} when full_log != "" ->
        _result =
          full_log
          |> String.split("\n")
          |> Enum.reduce(checkpoints, fn line, acc ->
            parts = String.split(line, "|", parts: 3)
            if length(parts) == 3 do
              [h, date | [_msg]] = parts
              Enum.map(acc, fn cp ->
                if cp.hash == h, do: %{cp | date: date}, else: cp
              end)
            else
              acc
            end
          end)

      _ -> :ok
    end
  end
end
