defmodule HiveWeave.ToolExecutorP2Test do
  use ExUnit.Case

  alias HiveWeave.ToolExecutor

  setup do
    agent = %{
      id: Ecto.UUID.generate(),
      project_id: "test-p2",
      name: "TestAgent",
      role: "executor",
      permission_type: "executor",
      short_id: "TES2",
      model_id: nil,
      ask_tools: "[]",
      denied_tools: "[]",
      allowed_tools: "[]",
      bound_skills: "[]",
      mcp_servers: "[]"
    }
    {:ok, agent: agent}
  end

  # ── MCP tools: definition tests ────────────────────────────────

  describe "MCP tools - definitions" do
    test "mcp_list_tools is available to executors" do
      tools = ToolExecutor.get_tools("executor")
      names = Enum.map(tools, fn t -> get_in(t, ["function", "name"]) end)
      assert "mcp_list_tools" in names
    end

    test "mcp_list_tools is available to coordinators" do
      tools = ToolExecutor.get_tools("coordinator")
      names = Enum.map(tools, fn t -> get_in(t, ["function", "name"]) end)
      assert "mcp_list_tools" in names
    end

    test "mcp_list_tools has no required parameters" do
      tools = ToolExecutor.get_tools("executor")
      mcp_list = Enum.find(tools, fn t -> get_in(t, ["function", "name"]) == "mcp_list_tools" end)
      assert mcp_list["type"] == "function"
      assert mcp_list["function"]["parameters"]["type"] == "object"
      assert mcp_list["function"]["parameters"]["properties"] == %{}
    end

    test "mcp_call is available to executors" do
      tools = ToolExecutor.get_tools("executor")
      names = Enum.map(tools, fn t -> get_in(t, ["function", "name"]) end)
      assert "mcp_call" in names
    end

    test "mcp_call is available to coordinators" do
      tools = ToolExecutor.get_tools("coordinator")
      names = Enum.map(tools, fn t -> get_in(t, ["function", "name"]) end)
      assert "mcp_call" in names
    end

    test "mcp_call requires server and tool in schema" do
      tools = ToolExecutor.get_tools("executor")
      mcp_call = Enum.find(tools, fn t -> get_in(t, ["function", "name"]) == "mcp_call" end)
      required = mcp_call["function"]["parameters"]["required"]
      assert "server" in required
      assert "tool" in required
      assert "arguments" not in required
    end

    test "mcp_configure is available to executors" do
      tools = ToolExecutor.get_tools("executor")
      names = Enum.map(tools, fn t -> get_in(t, ["function", "name"]) end)
      assert "mcp_configure" in names
    end

    test "mcp_configure is available to coordinators" do
      tools = ToolExecutor.get_tools("coordinator")
      names = Enum.map(tools, fn t -> get_in(t, ["function", "name"]) end)
      assert "mcp_configure" in names
    end

    test "mcp_configure requires name and transport in schema" do
      tools = ToolExecutor.get_tools("executor")
      mcp_conf = Enum.find(tools, fn t -> get_in(t, ["function", "name"]) == "mcp_configure" end)
      required = mcp_conf["function"]["parameters"]["required"]
      assert "name" in required
      assert "transport" in required
    end

    test "mcp_configure transport enum is [stdio, http]" do
      tools = ToolExecutor.get_tools("executor")
      mcp_conf = Enum.find(tools, fn t -> get_in(t, ["function", "name"]) == "mcp_configure" end)
      transport_prop = mcp_conf["function"]["parameters"]["properties"]["transport"]
      assert transport_prop["enum"] == ["stdio", "http"]
    end
  end

  # ── MCP tools: dispatch tests ──────────────────────────────────

  describe "MCP tools - dispatch" do
    test "mcp_list_tools returns server list or empty message", %{agent: agent} do
      result = ToolExecutor.execute(agent, "mcp_list_tools", %{}, ".")
      assert {:ok, msg} = result
      assert is_binary(msg)
    end

    test "mcp_call requires server and tool", %{agent: agent} do
      result = ToolExecutor.execute(agent, "mcp_call", %{}, ".")
      assert {:ok, msg} = result
      assert String.contains?(msg, "Error") or String.contains?(msg, "required")
    end

    test "mcp_call with only server fails", %{agent: agent} do
      result = ToolExecutor.execute(agent, "mcp_call", %{"server" => "github"}, ".")
      assert {:ok, msg} = result
      assert String.contains?(msg, "Error") or String.contains?(msg, "required")
    end

    test "mcp_call with only tool fails", %{agent: agent} do
      result = ToolExecutor.execute(agent, "mcp_call", %{"tool" => "list_repos"}, ".")
      assert {:ok, msg} = result
      assert String.contains?(msg, "Error") or String.contains?(msg, "required")
    end

    test "mcp_call returns error for unbound server", %{agent: agent} do
      agent = %{agent | mcp_servers: ~s|["filesystem"]|}
      result = ToolExecutor.execute(agent, "mcp_call", %{"server" => "github", "tool" => "list_repos"}, ".")
      assert {:ok, msg} = result
      assert String.contains?(msg, "Error") or String.contains?(msg, "not bound")
    end

    test "mcp_configure requires name", %{agent: agent} do
      result = ToolExecutor.execute(agent, "mcp_configure", %{}, ".")
      assert {:ok, msg} = result
      assert String.contains?(msg, "Error") or String.contains?(msg, "required")
    end

    test "mcp_configure with valid name and transport succeeds", %{agent: agent} do
      suffix = :rand.uniform(999_999)
      result = ToolExecutor.execute(agent, "mcp_configure", %{
        "name" => "test-server-#{suffix}",
        "transport" => "http",
        "url" => "http://localhost:9999/mcp"
      }, ".")
      assert {:ok, msg} = result
      assert String.contains?(msg, "configured")
    end

    test "mcp_configure with stdio transport succeeds", %{agent: agent} do
      suffix = :rand.uniform(999_999)
      result = ToolExecutor.execute(agent, "mcp_configure", %{
        "name" => "test-stdio-#{suffix}",
        "transport" => "stdio",
        "command" => "node"
      }, ".")
      assert {:ok, msg} = result
      assert String.contains?(msg, "configured")
    end
  end

  # ── fetch_url tool: definition tests ───────────────────────────

  describe "fetch_url tool - definitions" do
    test "fetch_url is available to executors" do
      tools = ToolExecutor.get_tools("executor")
      names = Enum.map(tools, fn t -> get_in(t, ["function", "name"]) end)
      assert "fetch_url" in names
    end

    test "fetch_url is available to coordinators" do
      tools = ToolExecutor.get_tools("coordinator")
      names = Enum.map(tools, fn t -> get_in(t, ["function", "name"]) end)
      assert "fetch_url" in names
    end

    test "fetch_url requires url in schema" do
      tools = ToolExecutor.get_tools("executor")
      fetch_tool = Enum.find(tools, fn t -> get_in(t, ["function", "name"]) == "fetch_url" end)
      required = fetch_tool["function"]["parameters"]["required"]
      assert "url" in required
    end

    test "fetch_url format enum is [markdown, text]" do
      tools = ToolExecutor.get_tools("executor")
      fetch_tool = Enum.find(tools, fn t -> get_in(t, ["function", "name"]) == "fetch_url" end)
      format_prop = fetch_tool["function"]["parameters"]["properties"]["format"]
      assert format_prop["enum"] == ["markdown", "text"]
    end
  end

  # ── fetch_url tool: dispatch tests ─────────────────────────────

  describe "fetch_url tool - dispatch" do
    test "fetch_url requires url parameter", %{agent: agent} do
      result = ToolExecutor.execute(agent, "fetch_url", %{}, ".")
      assert {:ok, msg} = result
      assert String.contains?(msg, "Error") or String.contains?(msg, "required")
    end

    test "fetch_url blocks localhost URLs", %{agent: agent} do
      result = ToolExecutor.execute(agent, "fetch_url", %{"url" => "http://localhost:3000/test"}, ".")
      assert {:ok, msg} = result
      assert String.contains?(msg, "Error") or String.contains?(msg, "blocked") or
             String.contains?(msg, "Local")
    end

    test "fetch_url blocks 127.0.0.1 URLs", %{agent: agent} do
      result = ToolExecutor.execute(agent, "fetch_url", %{"url" => "http://127.0.0.1:8080/api"}, ".")
      assert {:ok, msg} = result
      assert String.contains?(msg, "Error") or String.contains?(msg, "blocked") or
             String.contains?(msg, "Local")
    end

    test "fetch_url blocks 0.0.0.0 URLs", %{agent: agent} do
      result = ToolExecutor.execute(agent, "fetch_url", %{"url" => "http://0.0.0.0:3000/"}, ".")
      assert {:ok, msg} = result
      assert String.contains?(msg, "Error") or String.contains?(msg, "blocked") or
             String.contains?(msg, "Local")
    end

    test "fetch_url blocks non-http schemes", %{agent: agent} do
      result = ToolExecutor.execute(agent, "fetch_url", %{"url" => "file:///etc/passwd"}, ".")
      assert {:ok, msg} = result
      assert String.contains?(msg, "Error") or String.contains?(msg, "Only http")
    end

    test "fetch_url blocks ftp scheme", %{agent: agent} do
      result = ToolExecutor.execute(agent, "fetch_url", %{"url" => "ftp://example.com/file"}, ".")
      assert {:ok, msg} = result
      assert String.contains?(msg, "Error") or String.contains?(msg, "Only http")
    end
  end

  # ── No duplicate tools ─────────────────────────────────────────

  describe "no duplicate tools" do
    test "coordinator tools have no duplicates" do
      tools = ToolExecutor.get_tools("coordinator")
      names = Enum.map(tools, fn t -> get_in(t, ["function", "name"]) end)
      for name <- names do
        count = Enum.count(names, fn n -> n == name end)
        assert count == 1, "Expected #{name} to appear exactly once, got #{count}"
      end
    end

    test "executor tools have no duplicates" do
      tools = ToolExecutor.get_tools("executor")
      names = Enum.map(tools, fn t -> get_in(t, ["function", "name"]) end)
      for name <- names do
        count = Enum.count(names, fn n -> n == name end)
        assert count == 1, "Expected #{name} to appear exactly once, got #{count}"
      end
    end
  end

  # ── Tool schema validation (P2 tools) ──────────────────────────

  describe "P2 tool schema validation" do
    test "all P2 tools have valid function-calling schema for executors" do
      p2_tools = ~w(mcp_list_tools mcp_call mcp_configure fetch_url)
      tools = ToolExecutor.get_tools("executor")
      names_map = Map.new(tools, fn t -> {get_in(t, ["function", "name"]), t} end)

      for name <- p2_tools do
        tool = names_map[name]
        assert tool != nil, "Expected #{name} to be present"
        assert tool["type"] == "function"
        assert is_binary(tool["function"]["name"])
        assert tool["function"]["name"] != ""
        assert tool["function"]["parameters"]["type"] == "object"
      end
    end

    test "all P2 tools have valid function-calling schema for coordinators" do
      p2_tools = ~w(mcp_list_tools mcp_call mcp_configure fetch_url)
      tools = ToolExecutor.get_tools("coordinator")
      names_map = Map.new(tools, fn t -> {get_in(t, ["function", "name"]), t} end)

      for name <- p2_tools do
        tool = names_map[name]
        assert tool != nil, "Expected #{name} to be present"
        assert tool["type"] == "function"
        assert is_binary(tool["function"]["name"])
        assert tool["function"]["name"] != ""
        assert tool["function"]["parameters"]["type"] == "object"
      end
    end
  end

  # ── Edge cases ─────────────────────────────────────────────────

  describe "MCP edge cases" do
    test "mcp_call with empty server string", %{agent: agent} do
      result = ToolExecutor.execute(agent, "mcp_call", %{"server" => "", "tool" => "test"}, ".")
      assert {:ok, msg} = result
      assert String.contains?(msg, "Error") or String.contains?(msg, "required")
    end

    test "mcp_call with empty tool string", %{agent: agent} do
      result = ToolExecutor.execute(agent, "mcp_call", %{"server" => "github", "tool" => ""}, ".")
      assert {:ok, msg} = result
      assert String.contains?(msg, "Error") or String.contains?(msg, "required")
    end

    test "mcp_configure with empty name string", %{agent: agent} do
      result = ToolExecutor.execute(agent, "mcp_configure", %{"name" => "", "transport" => "http"}, ".")
      assert {:ok, msg} = result
      assert String.contains?(msg, "Error") or String.contains?(msg, "required")
    end

    test "fetch_url with empty url string", %{agent: agent} do
      result = ToolExecutor.execute(agent, "fetch_url", %{"url" => ""}, ".")
      assert {:ok, msg} = result
      assert String.contains?(msg, "Error") or String.contains?(msg, "required")
    end

    test "hiveweave__ prefix works for mcp_list_tools", %{agent: agent} do
      result = ToolExecutor.execute(agent, "hiveweave__mcp_list_tools", %{}, ".")
      assert {:ok, msg} = result
      assert is_binary(msg)
    end

    test "hiveweave__ prefix works for fetch_url", %{agent: agent} do
      result = ToolExecutor.execute(agent, "hiveweave__fetch_url", %{"url" => "ftp://bad"}, ".")
      assert {:ok, msg} = result
      # Should still block the unsafe URL
      assert String.contains?(msg, "Error") or String.contains?(msg, "Only http")
    end
  end
end
