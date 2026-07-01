defmodule HiveWeave.ToolExecutorFileTest do
  use ExUnit.Case

  alias HiveWeave.ToolExecutor

  setup do
    tmp_dir = Path.join(System.tmp_dir!(), "hw_file_test_#{System.unique_integer()}")
    File.mkdir_p!(tmp_dir)
    on_exit(fn -> File.rm_rf!(tmp_dir) end)

    agent = %{
      id: Ecto.UUID.generate(),
      project_id: "test-project",
      name: "TestAgent",
      role: "executor",
      permission_type: "executor",
      short_id: "TES1",
      ask_tools: "[]",
      bound_skills: "[]",
      mcp_servers: "[]"
    }

    {:ok, workspace: tmp_dir, agent: agent}
  end

  describe "apply_patch (add)" do
    test "creates a new file", %{workspace: ws, agent: agent} do
      result = ToolExecutor.execute(agent, "apply_patch", %{
        "op" => "add",
        "filePath" => "test.txt",
        "content" => "Hello World"
      }, ws)

      assert {:ok, msg} = result
      assert String.contains?(msg, "Created")
      assert File.exists?(Path.join(ws, "test.txt"))
    end

    test "errors when file already exists", %{workspace: ws, agent: agent} do
      file_path = Path.join(ws, "existing.txt")
      File.write!(file_path, "existing")

      result = ToolExecutor.execute(agent, "apply_patch", %{
        "op" => "add",
        "filePath" => "existing.txt",
        "content" => "new"
      }, ws)

      assert {:ok, msg} = result
      assert String.contains?(msg, "ERROR")
      assert String.contains?(msg, "already exists")
    end

    test "blocks paths outside workspace", %{workspace: ws, agent: agent} do
      result = ToolExecutor.execute(agent, "apply_patch", %{
        "op" => "add",
        "filePath" => "../../outside.txt",
        "content" => "bad"
      }, ws)

      assert {:ok, msg} = result
      assert String.contains?(msg, "Sandbox violation")
    end

    test "auto-creates parent directories", %{workspace: ws, agent: agent} do
      result = ToolExecutor.execute(agent, "apply_patch", %{
        "op" => "add",
        "filePath" => "deep/nested/path/test.txt",
        "content" => "nested"
      }, ws)

      assert {:ok, msg} = result
      assert String.contains?(msg, "Created")
      assert File.dir?(Path.join(ws, "deep/nested/path"))
      assert File.read!(Path.join(ws, "deep/nested/path/test.txt")) == "nested"
    end
  end

  describe "apply_patch (update)" do
    test "replaces text in a file", %{workspace: ws, agent: agent} do
      file_path = Path.join(ws, "edit_test.txt")
      File.write!(file_path, "Hello World")

      result = ToolExecutor.execute(agent, "apply_patch", %{
        "op" => "update",
        "filePath" => "edit_test.txt",
        "oldString" => "World",
        "newString" => "Elixir"
      }, ws)

      assert {:ok, msg} = result
      assert String.contains?(msg, "Updated")
      assert File.read!(file_path) == "Hello Elixir"
    end

    test "errors when oldString not found", %{workspace: ws, agent: agent} do
      file_path = Path.join(ws, "edit_test2.txt")
      File.write!(file_path, "Hello World")

      result = ToolExecutor.execute(agent, "apply_patch", %{
        "op" => "update",
        "filePath" => "edit_test2.txt",
        "oldString" => "NotFound",
        "newString" => "X"
      }, ws)

      assert {:ok, msg} = result
      assert String.contains?(msg, "ERROR")
      assert String.contains?(msg, "not found")
    end

    test "errors when file does not exist", %{workspace: ws, agent: agent} do
      result = ToolExecutor.execute(agent, "apply_patch", %{
        "op" => "update",
        "filePath" => "nonexistent.txt",
        "oldString" => "a",
        "newString" => "b"
      }, ws)

      assert {:ok, msg} = result
      assert String.contains?(msg, "ERROR")
      assert String.contains?(msg, "not found")
    end

    test "errors when oldString matches multiple times", %{workspace: ws, agent: agent} do
      file_path = Path.join(ws, "dup_test.txt")
      File.write!(file_path, "Hello Hello")

      result = ToolExecutor.execute(agent, "apply_patch", %{
        "op" => "update",
        "filePath" => "dup_test.txt",
        "oldString" => "Hello",
        "newString" => "Hi"
      }, ws)

      assert {:ok, msg} = result
      assert String.contains?(msg, "ERROR")
      assert String.contains?(msg, "Add more context")
    end
  end

  describe "apply_patch (delete)" do
    test "deletes a file", %{workspace: ws, agent: agent} do
      file_path = Path.join(ws, "to_delete.txt")
      File.write!(file_path, "delete me")

      result = ToolExecutor.execute(agent, "apply_patch", %{
        "op" => "delete",
        "filePath" => "to_delete.txt"
      }, ws)

      assert {:ok, msg} = result
      assert String.contains?(msg, "Deleted")
      refute File.exists?(file_path)
    end

    test "errors when file does not exist", %{workspace: ws, agent: agent} do
      result = ToolExecutor.execute(agent, "apply_patch", %{
        "op" => "delete",
        "filePath" => "nonexistent.txt"
      }, ws)

      assert {:ok, msg} = result
      assert String.contains?(msg, "ERROR")
      assert String.contains?(msg, "not found")
    end
  end

  describe "apply_patch (multiple patches)" do
    test "applies multiple ops in one call", %{workspace: ws, agent: agent} do
      result = ToolExecutor.execute(agent, "apply_patch", %{
        "patches" => [
          %{"op" => "add", "filePath" => "multi1.txt", "content" => "first"},
          %{"op" => "add", "filePath" => "multi2.txt", "content" => "second"}
        ]
      }, ws)

      assert {:ok, msg} = result
      assert String.contains?(msg, "multi1.txt")
      assert String.contains?(msg, "multi2.txt")
      assert File.exists?(Path.join(ws, "multi1.txt"))
      assert File.exists?(Path.join(ws, "multi2.txt"))
    end
  end

  describe "read_file" do
    test "reads file content", %{workspace: ws, agent: agent} do
      file_path = Path.join(ws, "readme.txt")
      File.write!(file_path, "Hello from Elixir")

      result = ToolExecutor.execute(agent, "read_file", %{
        "filePath" => "readme.txt"
      }, ws)

      assert {:ok, msg} = result
      assert String.contains?(msg, "Hello from Elixir")
    end

    test "handles non-existent file", %{workspace: ws, agent: agent} do
      result = ToolExecutor.execute(agent, "read_file", %{
        "filePath" => "missing.txt"
      }, ws)

      assert {:ok, msg} = result
      assert String.contains?(msg, "Error")
    end
  end

  describe "list_files" do
    test "lists files in a directory", %{workspace: ws, agent: agent} do
      File.write!(Path.join(ws, "a.txt"), "a")
      File.write!(Path.join(ws, "b.txt"), "b")
      File.mkdir_p!(Path.join(ws, "subdir"))

      result = ToolExecutor.execute(agent, "list_files", %{"path" => "."}, ws)

      assert {:ok, msg} = result
      assert String.contains?(msg, "a.txt")
      assert String.contains?(msg, "b.txt")
      assert String.contains?(msg, "subdir")
    end
  end

  describe "grep" do
    test "finds matching lines", %{workspace: ws, agent: agent} do
      File.write!(Path.join(ws, "search1.txt"), "hello world\nfoo bar\nhello again")

      result = ToolExecutor.execute(agent, "grep", %{
        "pattern" => "hello",
        "path" => "."
      }, ws)

      assert {:ok, msg} = result
      assert String.contains?(msg, "hello world") or String.contains?(msg, "hello again")
    end

    test "handles no matches", %{workspace: ws, agent: agent} do
      File.write!(Path.join(ws, "search_none.txt"), "nothing here")

      result = ToolExecutor.execute(agent, "grep", %{
        "pattern" => "zzz_never",
        "path" => "."
      }, ws)

      assert {:ok, msg} = result
      assert String.contains?(msg, "No matches")
    end
  end

  describe "unknown tool" do
    test "returns error for unknown tool name", %{workspace: ws, agent: agent} do
      result = ToolExecutor.execute(agent, "nonexistent_tool", %{}, ws)

      assert {:ok, msg} = result
      assert String.contains?(msg, "Error")
    end
  end
end
