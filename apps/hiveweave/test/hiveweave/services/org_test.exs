defmodule HiveWeave.Services.OrgTest do
  use ExUnit.Case

  alias HiveWeave.Services.Org

  test "generate_short_id produces A001 style IDs" do
    id1 = Org.generate_short_id()
    id2 = Org.generate_short_id()
    assert String.starts_with?(id1, "A")
    assert String.length(id1) == 4
  end

  test "list_agents returns empty when no project" do
    result = Org.list_agents("nonexistent-project-id")
    assert is_list(result)
  end

  test "get_agent returns nil for non-existent agent" do
    result = Org.get_agent("nonexistent-agent-id")
    assert result == nil
  end

  test "get_agent_by_role returns nil for non-existent role" do
    result = Org.get_agent_by_role("nonexistent", "ceo")
    assert result == nil
  end

  test "build_tree returns empty list when no agents" do
    result = Org.build_tree("nonexistent-project-id")
    assert result == []
  end
end
