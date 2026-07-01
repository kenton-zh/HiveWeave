defmodule HiveWeave.Services.GitWorktreeTest do
  use ExUnit.Case

  alias HiveWeave.Services.GitWorktree

  setup do
    tmp_dir = Path.join(System.tmp_dir!(), "hw_git_test_#{System.unique_integer()}")
    File.mkdir_p!(tmp_dir)
    on_exit(fn -> File.rm_rf!(tmp_dir) end)
    {:ok, workspace: tmp_dir}
  end

  defp git(cmd, cwd) do
    System.cmd("cmd", ["/c", "git #{cmd}"], cd: cwd)
  end

  defp git!(cmd, cwd) do
    case git(cmd, cwd) do
      {output, 0} -> output
      {output, code} -> raise "git #{cmd} failed (#{code}): #{output}"
    end
  end

  defp init_git_repo(ws) do
    git!("init", ws)
    git!(~s|config user.email "test@test.com"|, ws)
    git!(~s|config user.name "Test"|, ws)
    git!(~s|commit --allow-empty -m "init"|, ws)
    :ok
  end

  describe "ensure_git_repo/1" do
    test "returns ok without init when .git exists", %{workspace: ws} do
      git!("init", ws)
      assert {:ok, false} = GitWorktree.ensure_git_repo(ws)
    end
  end

  describe "create/4" do
    @tag :git
    test "creates a worktree directory and branch", %{workspace: ws} do
      init_git_repo(ws)

      result = GitWorktree.create(ws, "A001", "test-task")
      assert {:ok, %{path: path, branch: branch}} = result
      assert String.contains?(path, "A001")
      assert branch == "hw/A001/test-task"
    end

    @tag :git
    test "returns ok when worktree already exists", %{workspace: ws} do
      init_git_repo(ws)

      {:ok, %{path: path, branch: branch}} = GitWorktree.create(ws, "A002", "duplicate-task")

      result = GitWorktree.create(ws, "A002", "duplicate-task")
      assert {:ok, %{path: ^path, branch: ^branch}} = result
    end
  end

  describe "list/1" do
    @tag :git
    test "returns entries after creating a worktree", %{workspace: ws} do
      init_git_repo(ws)

      {:ok, _} = GitWorktree.create(ws, "A003", "list-task")

      {:ok, entries} = GitWorktree.list(ws)
      assert length(entries) >= 1
    end
  end

  describe "checkpoint/3" do
    test "returns error for non-existent worktree", %{workspace: ws} do
      assert {:error, reason} = GitWorktree.checkpoint(ws, "NONEXIST", "save")
      assert String.contains?(reason, "does not exist")
    end

    @tag :git
    test "creates a checkpoint on an active worktree", %{workspace: ws} do
      init_git_repo(ws)

      {:ok, _} = GitWorktree.create(ws, "A004", "checkpoint-task")

      File.write!(Path.join(ws, ".hiveweave/worktrees/A004/test.txt"), "checkpoint me")

      result = GitWorktree.checkpoint(ws, "A004", "save state")
      assert {:ok, %{hash: hash, count: count}} = result
      assert is_binary(hash)
      assert is_integer(count)
    end
  end

  describe "remove/3" do
    test "handles non-existent worktree gracefully", %{workspace: ws} do
      assert {:ok, %{removed: true}} = GitWorktree.remove(ws, "NONEXIST", "some-task")
    end

    @tag :git
    test "removes an existing worktree", %{workspace: ws} do
      init_git_repo(ws)

      {:ok, _} = GitWorktree.create(ws, "A007", "remove-task")

      result = GitWorktree.remove(ws, "A007", "remove-task")
      assert {:ok, %{removed: true}} = result
    end
  end

  describe "status/2" do
    test "returns nil for non-existent worktree", %{workspace: ws} do
      assert {:ok, nil} = GitWorktree.status(ws, "NONEXIST")
    end

    @tag :git
    test "returns status for an active worktree", %{workspace: ws} do
      init_git_repo(ws)

      {:ok, _} = GitWorktree.create(ws, "A005", "status-task")

      {:ok, status} = GitWorktree.status(ws, "A005")
      assert is_map(status)
      assert status.short_id == "A005"
      assert status.active == true
      assert is_binary(status.branch)
      assert is_binary(status.head)
      assert is_list(status.checkpoints)
    end
  end

  describe "merge/4" do
    test "returns error for non-existent worktree", %{workspace: ws} do
      assert {:error, _} = GitWorktree.merge(ws, "NONEXIST", "no-task")
    end
  end

  describe "rollback/3" do
    test "returns error for non-existent worktree", %{workspace: ws} do
      assert {:error, reason} = GitWorktree.rollback(ws, "NONEXIST")
      assert String.contains?(reason, "does not exist")
    end
  end

  describe "get_worktree_path/2" do
    test "returns nil for non-existent worktree", %{workspace: ws} do
      assert GitWorktree.get_worktree_path(ws, "NONEXIST") == nil
    end

    @tag :git
    test "returns path for an existing worktree", %{workspace: ws} do
      init_git_repo(ws)

      {:ok, _} = GitWorktree.create(ws, "A006", "path-task")

      path = GitWorktree.get_worktree_path(ws, "A006")
      assert is_binary(path)
      assert String.contains?(path, "A006")
    end
  end
end
