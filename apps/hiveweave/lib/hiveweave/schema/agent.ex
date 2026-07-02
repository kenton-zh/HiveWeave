defmodule HiveWeave.Schema.Agent do
  use Ecto.Schema
  import Ecto.Changeset

  @primary_key {:id, :binary_id, autogenerate: true}
  @foreign_key_type :binary_id
  schema "agents" do
    field :short_id, :string
    field :project_id, :binary_id
    field :name, :string
    field :role, :string
    field :parent_id, :binary_id
    field :module_id, :binary_id
    field :status, :string, default: "created"
    field :goal, :string, default: ""
    field :backstory, :string, default: ""
    field :skills, :string, default: "[]"
    field :model_id, :string
    field :permission_type, :string, default: "executor"
    field :permission_mode, :string, default: "full"
    field :allowed_tools, :string, default: "[]"
    field :denied_tools, :string, default: "[]"
    field :ask_tools, :string, default: "[]"
    field :mcp_servers, :string, default: "[]"
    field :bound_skills, :string, default: "[]"
    field :last_seen_log_at, :integer
    field :created_at, :integer
    field :updated_at, :integer
    field :reasoning_effort, :string, default: nil
  end

  def changeset(agent, attrs) do
    agent
    |> cast(attrs, [
      :id, :short_id, :project_id, :name, :role, :parent_id, :module_id,
      :status, :goal, :backstory, :skills, :permission_type, :permission_mode,
      :allowed_tools, :denied_tools, :ask_tools, :mcp_servers, :bound_skills,
      :last_seen_log_at, :created_at, :updated_at, :reasoning_effort, :model_id
    ])
    |> validate_required([:name, :role])
    |> validate_inclusion(:status, ["created", "active", "promoted", "receiving", "merging", "dissolving", "archived"])
    |> validate_inclusion(:permission_type, ["coordinator", "executor"])
  end
end


