defmodule HiveWeave.Services.OrgExtTest do
  use ExUnit.Case

  alias HiveWeave.Services.Org

  describe "transfer_agent/3" do
    test "returns error for non-existent agent (no DB)" do
      result = Org.transfer_agent("nonexistent-project", "nonexistent-agent", "nonexistent-parent")
      assert match?({:error, _}, result)
    end

    test "returns error when agent_id equals new_parent_id (cycle prevention)" do
      result = Org.transfer_agent("proj", "same-id", "same-id")
      assert {:error, msg} = result
      assert String.contains?(msg, "own parent")
    end

    test "non-self transfer for non-existent agent reaches DB checks" do
      result = Org.transfer_agent("any-project", "agent-a", "agent-b")
      assert match?({:error, _}, result)
    end
  end

  describe "dismiss_agent/2" do
    test "returns error for non-existent agent (no DB)" do
      result = Org.dismiss_agent("nonexistent-project", "nonexistent-agent")
      assert match?({:error, _}, result)
    end
  end

  describe "get_all_descendants/2" do
    test "returns empty list for non-existent agent" do
      result = Org.get_all_descendants("nonexistent-project", "nonexistent-agent")
      assert result == []
    end

    test "returns empty list for non-existent project" do
      result = Org.get_all_descendants("", "")
      assert result == []
    end
  end
end
