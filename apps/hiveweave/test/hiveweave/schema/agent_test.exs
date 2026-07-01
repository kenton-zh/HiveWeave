defmodule HiveWeave.Schema.AgentTest do
  use ExUnit.Case

  alias HiveWeave.Schema.Agent

  test "changeset with valid attrs" do
    attrs = %{name: "Test Agent", role: "executor"}
    changeset = Agent.changeset(%Agent{}, attrs)
    assert changeset.valid?
  end

  test "changeset requires name" do
    changeset = Agent.changeset(%Agent{}, %{role: "executor"})
    refute changeset.valid?
  end

  test "changeset requires role" do
    changeset = Agent.changeset(%Agent{}, %{name: "Test"})
    refute changeset.valid?
  end

  test "changeset validates permission_type" do
    changeset = Agent.changeset(%Agent{}, %{name: "Test", role: "executor", permission_type: "invalid"})
    refute changeset.valid?
  end

  test "changeset accepts valid permission_type" do
    changeset = Agent.changeset(%Agent{}, %{name: "Test", role: "executor", permission_type: "coordinator"})
    assert changeset.valid?
  end
end
