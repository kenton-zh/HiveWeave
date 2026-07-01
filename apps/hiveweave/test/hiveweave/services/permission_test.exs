defmodule HiveWeave.Services.PermissionTest do
  use ExUnit.Case

  alias HiveWeave.Services.Permission

  setup do
    default_agent = %{
      id: Ecto.UUID.generate(),
      project_id: "test-perm",
      name: "TestAgent",
      permission_type: "readwrite",
      ask_tools: "[]",
      denied_tools: "[]",
      allowed_tools: "[]"
    }
    {:ok, agent: default_agent}
  end

  # ── Readonly mode ──────────────────────────────────────────────

  describe "evaluate/2 - readonly mode" do
    test "allows read_file", ctx do
      agent = Map.put(ctx.agent, :permission_type, "readonly")
      assert Permission.evaluate(agent, "read_file") == :allow
    end

    test "denies bash in readonly mode", ctx do
      agent = Map.put(ctx.agent, :permission_type, "readonly")
      assert Permission.evaluate(agent, "bash") == :ask
    end
  end

  # ── Readwrite mode ─────────────────────────────────────────────

  describe "evaluate/2 - readwrite mode" do
    test "allows bash in readwrite mode (no deny override)", ctx do
      agent = Map.put(ctx.agent, :permission_type, "readwrite")
      assert Permission.evaluate(agent, "bash") == :allow
    end

    test "deny list overrides readwrite preset", ctx do
      agent = %{
        ctx.agent
        | permission_type: "readwrite",
          denied_tools: ~s|["bash"]|
      }
      assert Permission.evaluate(agent, "bash") == :deny
    end

    test "ask list overrides readwrite preset (but below deny)", ctx do
      agent = %{
        ctx.agent
        | permission_type: "readwrite",
          ask_tools: ~s|["bash"]|
      }
      assert Permission.evaluate(agent, "bash") == :ask
    end

    test "non-preset tools default to ask in readwrite", ctx do
      agent = Map.put(ctx.agent, :permission_type, "readwrite")
      assert Permission.evaluate(agent, "nonexistent_tool_xyz") == :ask
    end
  end

  # ── Full mode ──────────────────────────────────────────────────

  describe "evaluate/2 - full mode" do
    test "allows bash in full mode", ctx do
      agent = Map.put(ctx.agent, :permission_type, "full")
      assert Permission.evaluate(agent, "bash") == :allow
    end

    test "allows unknown tool in full mode", ctx do
      agent = Map.put(ctx.agent, :permission_type, "full")
      assert Permission.evaluate(agent, "some_random_tool") == :allow
    end

    test "deny still takes priority in full mode", ctx do
      agent = %{
        ctx.agent
        | permission_type: "full",
          denied_tools: ~s|["bash"]|
      }
      assert Permission.evaluate(agent, "bash") == :deny
    end

    test "ask takes priority over default allow in full mode", ctx do
      agent = %{
        ctx.agent
        | permission_type: "full",
          ask_tools: ~s|["bash"]|
      }
      assert Permission.evaluate(agent, "bash") == :ask
    end
  end

  # ── Custom mode ────────────────────────────────────────────────

  describe "evaluate/2 - custom mode" do
    test "uses deny list", ctx do
      agent = %{
        ctx.agent
        | permission_type: "custom",
          denied_tools: ~s|["bash"]|
      }
      assert Permission.evaluate(agent, "bash") == :deny
    end

    test "uses ask list", ctx do
      agent = %{
        ctx.agent
        | permission_type: "custom",
          ask_tools: ~s|["bash"]|
      }
      assert Permission.evaluate(agent, "bash") == :ask
    end

    test "uses allow list", ctx do
      agent = %{
        ctx.agent
        | permission_type: "custom",
          allowed_tools: ~s|["bash"]|
      }
      assert Permission.evaluate(agent, "bash") == :allow
    end

    test "deny takes priority over ask", ctx do
      agent = %{
        ctx.agent
        | permission_type: "custom",
          denied_tools: ~s|["bash"]|,
          ask_tools: ~s|["bash"]|
      }
      assert Permission.evaluate(agent, "bash") == :deny
    end

    test "deny takes priority over allow", ctx do
      agent = %{
        ctx.agent
        | permission_type: "custom",
          denied_tools: ~s|["bash"]|,
          allowed_tools: ~s|["bash"]|
      }
      assert Permission.evaluate(agent, "bash") == :deny
    end

    test "ask takes priority over allow", ctx do
      agent = %{
        ctx.agent
        | permission_type: "custom",
          ask_tools: ~s|["bash"]|,
          allowed_tools: ~s|["bash"]|
      }
      assert Permission.evaluate(agent, "bash") == :ask
    end

    test "deny > ask > allow priority chain", ctx do
      agent = %{
        ctx.agent
        | permission_type: "custom",
          denied_tools: ~s|["bash"]|,
          ask_tools: ~s|["bash"]|,
          allowed_tools: ~s|["bash"]|
      }
      assert Permission.evaluate(agent, "bash") == :deny
    end

    test "defaults to ask for unknown tools", ctx do
      agent = %{
        ctx.agent
        | permission_type: "custom",
          ask_tools: "[]",
          denied_tools: "[]",
          allowed_tools: "[]"
      }
      assert Permission.evaluate(agent, "unknown_tool") == :ask
    end
  end

  # ── matches_pattern?/2 ─────────────────────────────────────────

  describe "matches_pattern?/2" do
    test "exact match" do
      assert Permission.matches_pattern?("bash", ["bash"])
    end

    test "exact match with multiple patterns" do
      assert Permission.matches_pattern?("bash", ["read_file", "bash", "grep"])
    end

    test "wildcard * matches everything" do
      assert Permission.matches_pattern?("anything", ["*"])
      assert Permission.matches_pattern?("some.deeply.nested.tool", ["*"])
    end

    test "prefix wildcard: mcp__*" do
      assert Permission.matches_pattern?("mcp__github__repos", ["mcp__*"])
      assert Permission.matches_pattern?("mcp__filesystem__read", ["mcp__*"])
    end

    test "prefix wildcard: git_worktree_*" do
      assert Permission.matches_pattern?("git_worktree_create", ["git_worktree_*"])
      assert Permission.matches_pattern?("git_worktree_merge", ["git_worktree_*"])
      assert Permission.matches_pattern?("git_worktree_status", ["git_worktree_*"])
    end

    test "suffix wildcard" do
      assert Permission.matches_pattern?("system_bash", ["*_bash"])
    end

    test "middle wildcard" do
      assert Permission.matches_pattern?("tool_foo_result", ["tool_*_result"])
    end

    test "no match with different name" do
      refute Permission.matches_pattern?("bash", ["read_file"])
    end

    test "no match - partial substring is not a match" do
      # "bash" is not matched by "b*sh" unless *, but "bash_tool" should match "bash_*"
      assert Permission.matches_pattern?("bash_tool", ["bash_*"])
      refute Permission.matches_pattern?("bash", ["bash_extra"])
    end

    test "empty patterns list returns false" do
      refute Permission.matches_pattern?("bash", [])
    end

    test "nil patterns list returns false" do
      refute Permission.matches_pattern?("bash", nil)
    end

    test "rejects when tool_name does not match any pattern" do
      refute Permission.matches_pattern?("read_file", ["bash", "write_file"])
    end
  end

  # ── evaluate_custom/2 ──────────────────────────────────────────

  describe "evaluate_custom/2" do
    test "returns :deny when tool matches denied list", ctx do
      agent = %{ctx.agent | denied_tools: ~s|["bash", "write_file"]|}
      assert Permission.evaluate_custom(agent, "bash") == :deny
      assert Permission.evaluate_custom(agent, "write_file") == :deny
    end

    test "returns :ask when tool matches ask list (not denied)", ctx do
      agent = %{ctx.agent | ask_tools: ~s|["write_file"]|, denied_tools: "[]"}
      assert Permission.evaluate_custom(agent, "write_file") == :ask
    end

    test "returns :allow when tool matches allowed list (not denied or asked)", ctx do
      agent = %{ctx.agent | allowed_tools: ~s|["read_file"]|}
      assert Permission.evaluate_custom(agent, "read_file") == :allow
    end

    test "defaults to :ask when not in any list", ctx do
      agent = %{ctx.agent | denied_tools: "[]", ask_tools: "[]", allowed_tools: "[]"}
      assert Permission.evaluate_custom(agent, "some_unknown_tool") == :ask
    end

    test "handles empty json strings for lists", ctx do
      agent = %{ctx.agent | denied_tools: "", ask_tools: "", allowed_tools: ""}
      assert Permission.evaluate_custom(agent, "bash") == :ask
    end

    test "handles nil for all lists", ctx do
      agent = %{ctx.agent | denied_tools: nil, ask_tools: nil, allowed_tools: nil}
      assert Permission.evaluate_custom(agent, "bash") == :ask
    end
  end

  # ── get_permission_mode/1 ──────────────────────────────────────

  describe "get_permission_mode/1" do
    test "returns permission_type field", ctx do
      agent = %{ctx.agent | permission_type: "coordinator"}
      assert Permission.get_permission_mode(agent) == "coordinator"
    end

    test "defaults to executor when field is missing" do
      agent = %{id: "abc", name: "NoPerm"}
      assert Permission.get_permission_mode(agent) == "executor"
    end

    test "defaults to executor when field is nil" do
      agent = %{id: "abc", name: "NilPerm", permission_type: nil}
      assert Permission.get_permission_mode(agent) == "executor"
    end
  end

  # ── Edge cases ─────────────────────────────────────────────────

  describe "edge cases" do
    test "invalid json in denied_tools falls back to empty list", ctx do
      agent = %{ctx.agent | permission_type: "custom", denied_tools: "not-valid-json"}
      assert Permission.evaluate(agent, "bash") == :ask
    end

    test "empty string list element in ask_tools", ctx do
      agent = %{ctx.agent | permission_type: "custom", ask_tools: ~s|[""]|}
      # Pattern "" won't match "bash", so falls to :ask default
      assert Permission.evaluate(agent, "bash") == :ask
    end

    test "multiple tools with wildcard in denied", ctx do
      agent = %{
        ctx.agent
        | permission_type: "full",
          denied_tools: ~s|["mcp__*", "fetch_url"]|
      }
      assert Permission.evaluate(agent, "mcp__github__repos") == :deny
      assert Permission.evaluate(agent, "fetch_url") == :deny
      assert Permission.evaluate(agent, "read_file") == :allow
    end
  end
end
