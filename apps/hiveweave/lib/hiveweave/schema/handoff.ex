defmodule HiveWeave.Schema.Handoff do
  use Ecto.Schema
  import Ecto.Changeset

  schema "handoffs" do
    field :from_agent_id, :string
    field :to_agent_id, :string
    field :module_id, :string
    field :status, :string  # pending | accepted | completed | approved
    field :summary, :string
    field :expect_report, :integer, default: 0
    field :reported_up, :integer, default: 0
    field :created_at, :integer
    field :updated_at, :integer
    field :completed_at, :integer
  end

  def changeset(handoff, attrs) do
    handoff
    |> cast(attrs, [
      :from_agent_id, :to_agent_id, :module_id, :status, :summary,
      :expect_report, :reported_up, :created_at, :updated_at, :completed_at
    ])
    |> validate_required([:from_agent_id, :to_agent_id, :status, :created_at])
  end
end
