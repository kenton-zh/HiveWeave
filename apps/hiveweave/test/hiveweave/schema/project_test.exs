defmodule HiveWeave.Schema.ProjectTest do
  use ExUnit.Case

  alias HiveWeave.Schema.Project

  test "changeset with valid attrs" do
    attrs = %{name: "Test Project", created_at: 1_700_000_000_000}
    changeset = Project.changeset(%Project{}, attrs)
    assert changeset.valid?
  end

  test "changeset requires name" do
    changeset = Project.changeset(%Project{}, %{created_at: 1_700_000_000_000})
    refute changeset.valid?
  end

  test "changeset allows optional fields" do
    attrs = %{name: "Test", created_at: 1_700_000_000_000, description: "desc", workspace_path: "/tmp/x"}
    changeset = Project.changeset(%Project{}, attrs)
    assert changeset.valid?
  end
end
