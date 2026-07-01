defmodule HiveWeave.Schema.Merge do
  use Ecto.Schema
  import Ecto.Changeset

  schema "merges" do
    field :project_id, :string
    field :from_agent_id, :string
    field :to_agent_id, :string
    field :status, :string, default: "pending"
    field :conflict_data, :string, default: "[]"
    field :resolution, :string
    field :created_at, :integer
    field :completed_at, :integer

  end

  def changeset(merge, attrs) do
    merge
    |> cast(attrs, [
      :project_id, :from_agent_id, :to_agent_id, :status,
      :conflict_data, :resolution, :created_at, :completed_at
    ])
    |> validate_required([:project_id, :created_at])
  end
end
