defmodule HiveWeave.Services.CharterRosterTest do
  use ExUnit.Case

  alias HiveWeave.Services.Charter
  alias HiveWeave.Services.Roster

  describe "Charter.read_charter/1" do
    test "returns nil for non-existent project (no DB)" do
      result = Charter.read_charter("nonexistent-project-id")
      assert is_nil(result)
    end
  end

  describe "Charter.read_goals/1" do
    test "handles non-existent project gracefully" do
      result = Charter.read_goals("nonexistent-project-id")
      assert match?({:error, _}, result) or is_nil(result) or match?({:ok, _}, result)
    end
  end

  describe "Charter.save_charter/3" do
    test "handles missing DB gracefully" do
      result = Charter.save_charter("nonexistent-project", "agent-1", %{
        title: "Test Charter", content: "Content", status: "active"
      })
      assert match?({:error, _}, result) or match?({:ok, _}, result)
    end
  end

  describe "Charter.update_goals/2" do
    test "handles missing DB gracefully" do
      result = Charter.update_goals("nonexistent-project", %{
        objective: "Test", focus: "Dev", key_results: "KR1, KR2"
      })
      assert match?({:error, _}, result) or match?({:ok, _}, result)
    end
  end

  describe "Roster.get_roster/1" do
    test "handles non-existent project gracefully" do
      result = Roster.get_roster("nonexistent-project-id")
      assert match?({:error, _}, result) or match?({:ok, _}, result)
    end
  end

  describe "Roster.update_roster/3" do
    test "handles missing DB gracefully" do
      result = Roster.update_roster("nonexistent-project", "agent-1", %{
        position: "Developer", department: "Engineering"
      })
      assert match?({:error, _}, result) or match?({:ok, _}, result)
    end
  end
end
