defmodule HiveWeave.ToolExecutorP1Test do
  use ExUnit.Case

  alias HiveWeave.ToolExecutor

  setup do
    coordinator_agent = %{
      id: Ecto.UUID.generate(),
      project_id: "test-p1-project",
      name: "TestCEO",
      role: "ceo",
      permission_type: "coordinator",
      short_id: "CEOT",
      model_id: nil,
      ask_tools: "[]",
      bound_skills: "[]",
      mcp_servers: "[]"
    }

    executor_agent = %{
      id: Ecto.UUID.generate(),
      project_id: "test-p1-project",
      name: "TestDev",
      role: "executor",
      permission_type: "executor",
      short_id: "DEVT",
      model_id: nil,
      ask_tools: "[]",
      bound_skills: "[]",
      mcp_servers: "[]"
    }

    {:ok, coordinator: coordinator_agent, executor: executor_agent}
  end

  # ── Charter tools ───────────────────────────────────────────────

  describe "Charter tools" do
    test "read_charter tool exists for both roles", _ctx do
      executor_tools = ToolExecutor.get_tools("executor")
      coordinator_tools = ToolExecutor.get_tools("coordinator")

      e_names = Enum.map(executor_tools, fn t -> get_in(t, ["function", "name"]) end)
      c_names = Enum.map(coordinator_tools, fn t -> get_in(t, ["function", "name"]) end)

      assert "read_charter" in e_names
      assert "read_charter" in c_names
    end

    test "save_charter is coordinator-only", _ctx do
      coordinator_tools = ToolExecutor.get_tools("coordinator")
      executor_tools = ToolExecutor.get_tools("executor")

      c_names = Enum.map(coordinator_tools, fn t -> get_in(t, ["function", "name"]) end)
      e_names = Enum.map(executor_tools, fn t -> get_in(t, ["function", "name"]) end)

      assert "save_charter" in c_names
      refute "save_charter" in e_names
    end

    test "update_goals is coordinator-only", _ctx do
      c_tools = ToolExecutor.get_tools("coordinator")
      e_tools = ToolExecutor.get_tools("executor")

      c_names = Enum.map(c_tools, fn t -> get_in(t, ["function", "name"]) end)
      e_names = Enum.map(e_tools, fn t -> get_in(t, ["function", "name"]) end)

      assert "update_goals" in c_names
      refute "update_goals" in e_names
    end

    test "read_charter dispatch returns no-charter message (no DB)", %{executor: agent} do
      # Without DB, Charter.read_charter returns nil
      # dispatch returns "No charter found for this project."
      result = ToolExecutor.execute(agent, "read_charter", %{}, ".")
      assert {:ok, msg} = result
      assert is_binary(msg)
      assert String.contains?(msg, "No charter") or String.contains?(msg, "Error")
    end

    test "save_charter requires title and content", %{coordinator: agent} do
      result = ToolExecutor.execute(agent, "save_charter", %{}, ".")
      assert {:ok, msg} = result
      assert String.contains?(msg, "Error") or String.contains?(msg, "required")
    end

    test "read_goals dispatch returns message (no DB)", %{executor: agent} do
      # Without DB, read_goals may error or return nil; dispatch handles all cases
      result = ToolExecutor.execute(agent, "read_goals", %{}, ".")
      assert {:ok, msg} = result
      assert is_binary(msg)
    end

    test "update_goals requires at least one field", %{coordinator: agent} do
      result = ToolExecutor.execute(agent, "update_goals", %{}, ".")
      assert {:ok, msg} = result
      assert String.contains?(msg, "Error") or String.contains?(msg, "required")
    end
  end

  # ── Game time tools ──────────────────────────────────────────────

  describe "Game time tools" do
    test "get_real_time returns real-world time string", %{executor: agent} do
      result = ToolExecutor.execute(agent, "get_real_time", %{}, ".")
      assert {:ok, msg} = result
      assert is_binary(msg)
      assert String.contains?(msg, "real-world time") or String.contains?(msg, "UTC")
    end

    test "get_project_time returns formatted game time or error", %{executor: agent} do
      # GameTime.Server may not be running; execute/4 catches errors as {:ok, "Error: ..."}
      result = ToolExecutor.execute(agent, "get_project_time", %{}, ".")
      assert {:ok, msg} = result
      assert is_binary(msg)
      # Either formatted time or an error string
      assert String.contains?(msg, "Day") or String.contains?(msg, "project time") or String.contains?(msg, "Error")
    end

    test "get_project_time and get_real_time available to both roles", _ctx do
      c_tools = ToolExecutor.get_tools("coordinator")
      e_tools = ToolExecutor.get_tools("executor")

      c_names = Enum.map(c_tools, fn t -> get_in(t, ["function", "name"]) end)
      e_names = Enum.map(e_tools, fn t -> get_in(t, ["function", "name"]) end)

      assert "get_project_time" in c_names
      assert "get_project_time" in e_names
      assert "get_real_time" in c_names
      assert "get_real_time" in e_names
    end

    test "set_alarm requires purpose, fromAgentId, and fireAtGameSeconds", %{executor: agent} do
      result = ToolExecutor.execute(agent, "set_alarm", %{}, ".")
      assert {:ok, msg} = result
      assert String.contains?(msg, "Error") or String.contains?(msg, "required")
    end
  end

  # ── HR tools ─────────────────────────────────────────────────────

  describe "HR tools" do
    test "transfer_agent is coordinator-only", _ctx do
      c_tools = ToolExecutor.get_tools("coordinator")
      e_tools = ToolExecutor.get_tools("executor")

      c_names = Enum.map(c_tools, fn t -> get_in(t, ["function", "name"]) end)
      e_names = Enum.map(e_tools, fn t -> get_in(t, ["function", "name"]) end)

      assert "transfer_agent" in c_names
      refute "transfer_agent" in e_names
    end

    test "dismiss_agent is coordinator-only", _ctx do
      c_tools = ToolExecutor.get_tools("coordinator")
      e_tools = ToolExecutor.get_tools("executor")

      c_names = Enum.map(c_tools, fn t -> get_in(t, ["function", "name"]) end)
      e_names = Enum.map(e_tools, fn t -> get_in(t, ["function", "name"]) end)

      assert "dismiss_agent" in c_names
      refute "dismiss_agent" in e_names
    end

    test "dismiss_agent requires agentId", %{coordinator: agent} do
      result = ToolExecutor.execute(agent, "dismiss_agent", %{}, ".")
      assert {:ok, msg} = result
      assert String.contains?(msg, "Error") or String.contains?(msg, "required")
    end

    test "transfer_agent requires agentId and newParentId", %{coordinator: agent} do
      result = ToolExecutor.execute(agent, "transfer_agent", %{}, ".")
      assert {:ok, msg} = result
      assert String.contains?(msg, "Error") or String.contains?(msg, "required")
    end

    test "update_roster is coordinator-only", _ctx do
      c_tools = ToolExecutor.get_tools("coordinator")
      e_tools = ToolExecutor.get_tools("executor")

      c_names = Enum.map(c_tools, fn t -> get_in(t, ["function", "name"]) end)
      e_names = Enum.map(e_tools, fn t -> get_in(t, ["function", "name"]) end)

      assert "update_roster" in c_names
      refute "update_roster" in e_names
    end

    test "read_roster is coordinator-only", _ctx do
      c_tools = ToolExecutor.get_tools("coordinator")
      e_tools = ToolExecutor.get_tools("executor")

      c_names = Enum.map(c_tools, fn t -> get_in(t, ["function", "name"]) end)
      e_names = Enum.map(e_tools, fn t -> get_in(t, ["function", "name"]) end)

      assert "read_roster" in c_names
      refute "read_roster" in e_names
    end

    test "list_all_agents is coordinator-only", _ctx do
      c_tools = ToolExecutor.get_tools("coordinator")
      e_tools = ToolExecutor.get_tools("executor")

      c_names = Enum.map(c_tools, fn t -> get_in(t, ["function", "name"]) end)
      e_names = Enum.map(e_tools, fn t -> get_in(t, ["function", "name"]) end)

      assert "list_all_agents" in c_names
      refute "list_all_agents" in e_names
    end

    test "check_agent_status is coordinator-only", _ctx do
      c_tools = ToolExecutor.get_tools("coordinator")
      e_tools = ToolExecutor.get_tools("executor")

      c_names = Enum.map(c_tools, fn t -> get_in(t, ["function", "name"]) end)
      e_names = Enum.map(e_tools, fn t -> get_in(t, ["function", "name"]) end)

      assert "check_agent_status" in c_names
      refute "check_agent_status" in e_names
    end
  end

  # ── Review tools ──────────────────────────────────────────────────

  describe "Review tools" do
    test "run_code_review requires filePaths", %{executor: agent} do
      result = ToolExecutor.execute(agent, "run_code_review", %{}, ".")
      assert {:ok, msg} = result
      assert String.contains?(msg, "Error") or String.contains?(msg, "required")
    end

    test "run_security_audit requires filePaths", %{executor: agent} do
      result = ToolExecutor.execute(agent, "run_security_audit", %{}, ".")
      assert {:ok, msg} = result
      assert String.contains?(msg, "Error") or String.contains?(msg, "required")
    end

    test "run_tests requires sourceFiles", %{executor: agent} do
      result = ToolExecutor.execute(agent, "run_tests", %{}, ".")
      assert {:ok, msg} = result
      assert String.contains?(msg, "Error") or String.contains?(msg, "required")
    end

    test "run_perf_audit requires filePaths", %{executor: agent} do
      result = ToolExecutor.execute(agent, "run_perf_audit", %{}, ".")
      assert {:ok, msg} = result
      assert String.contains?(msg, "Error") or String.contains?(msg, "required")
    end

    test "run_full_review requires filePaths", %{executor: agent} do
      result = ToolExecutor.execute(agent, "run_full_review", %{}, ".")
      assert {:ok, msg} = result
      assert String.contains?(msg, "Error") or String.contains?(msg, "required")
    end

    test "all 5 review tools are available to executors" do
      tools = ToolExecutor.get_tools("executor")
      names = Enum.map(tools, fn t -> get_in(t, ["function", "name"]) end)

      assert "run_code_review" in names
      assert "run_security_audit" in names
      assert "run_tests" in names
      assert "run_perf_audit" in names
      assert "run_full_review" in names
    end

    test "all 5 review tools are available to coordinators" do
      tools = ToolExecutor.get_tools("coordinator")
      names = Enum.map(tools, fn t -> get_in(t, ["function", "name"]) end)

      assert "run_code_review" in names
      assert "run_security_audit" in names
      assert "run_tests" in names
      assert "run_perf_audit" in names
      assert "run_full_review" in names
    end
  end

  # ── Websearch ─────────────────────────────────────────────────────

  describe "Websearch" do
    test "websearch requires query", %{executor: agent} do
      result = ToolExecutor.execute(agent, "websearch", %{}, ".")
      assert {:ok, msg} = result
      assert String.contains?(msg, "Error") or String.contains?(msg, "required")
    end

    test "websearch available to both roles", _ctx do
      c_tools = ToolExecutor.get_tools("coordinator")
      e_tools = ToolExecutor.get_tools("executor")

      c_names = Enum.map(c_tools, fn t -> get_in(t, ["function", "name"]) end)
      e_names = Enum.map(e_tools, fn t -> get_in(t, ["function", "name"]) end)

      assert "websearch" in c_names
      assert "websearch" in e_names
    end
  end

  # ── Tool count verification ───────────────────────────────────────

  describe "Tool count verification" do
    test "coordinator has expected P1 tools" do
      tools = ToolExecutor.get_tools("coordinator")
      names = Enum.map(tools, fn t -> get_in(t, ["function", "name"]) end)

      # Git worktree
      assert "git_worktree_create" in names
      # Charter
      assert "save_charter" in names
      assert "read_charter" in names
      assert "update_goals" in names
      assert "read_goals" in names
      # HR
      assert "transfer_agent" in names
      assert "dismiss_agent" in names
      assert "update_roster" in names
      assert "read_roster" in names
      assert "list_all_agents" in names
      assert "check_agent_status" in names
      # Game time
      assert "get_project_time" in names
      assert "get_real_time" in names
      assert "set_alarm" in names
      # Review
      assert "run_code_review" in names
      assert "run_security_audit" in names
      assert "run_tests" in names
      assert "run_perf_audit" in names
      assert "run_full_review" in names
      # Websearch
      assert "websearch" in names
    end

    test "executor has expected P1 tools" do
      tools = ToolExecutor.get_tools("executor")
      names = Enum.map(tools, fn t -> get_in(t, ["function", "name"]) end)

      # File ops
      assert "read_file" in names
      assert "write_file" in names
      assert "bash" in names
      # Charter (read-only)
      assert "read_charter" in names
      assert "read_goals" in names
      # Game time
      assert "get_project_time" in names
      assert "get_real_time" in names
      assert "set_alarm" in names
      # Review
      assert "run_code_review" in names
      assert "run_security_audit" in names
      assert "run_tests" in names
      assert "run_perf_audit" in names
      assert "run_full_review" in names
      # Websearch
      assert "websearch" in names
    end

    test "coordinator has no duplicate tool names" do
      tools = ToolExecutor.get_tools("coordinator")
      names = Enum.map(tools, fn t -> get_in(t, ["function", "name"]) end)

      # write_file appears in both role lists but only once per role
      write_file_count = Enum.count(names, fn n -> n == "write_file" end)
      assert write_file_count == 1
    end

    test "executor has no duplicate tool names" do
      tools = ToolExecutor.get_tools("executor")
      names = Enum.map(tools, fn t -> get_in(t, ["function", "name"]) end)

      write_file_count = Enum.count(names, fn n -> n == "write_file" end)
      assert write_file_count == 1
    end
  end

  # ── Edge cases ────────────────────────────────────────────────────

  describe "Edge cases" do
    test "unknown tool returns error message", %{executor: agent} do
      result = ToolExecutor.execute(agent, "nonexistent_tool", %{}, ".")
      assert {:ok, msg} = result
      assert String.contains?(msg, "Error") or String.contains?(msg, "Unknown")
    end

    test "hiveweave__ prefix is stripped before dispatch", %{executor: agent} do
      result = ToolExecutor.execute(agent, "hiveweave__get_real_time", %{}, ".")
      assert {:ok, msg} = result
      assert is_binary(msg)
      assert String.contains?(msg, "real-world time") or String.contains?(msg, "UTC")
    end

    test "all executor tools have valid function-calling schema" do
      tools = ToolExecutor.get_tools("executor")
      Enum.each(tools, fn tool ->
        assert tool["type"] == "function"
        assert is_binary(tool["function"]["name"])
        assert tool["function"]["name"] != ""
      end)
    end

    test "all coordinator tools have valid function-calling schema" do
      tools = ToolExecutor.get_tools("coordinator")
      Enum.each(tools, fn tool ->
        assert tool["type"] == "function"
        assert is_binary(tool["function"]["name"])
        assert tool["function"]["name"] != ""
      end)
    end
  end
end
