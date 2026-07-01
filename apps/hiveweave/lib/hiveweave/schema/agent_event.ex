defmodule HiveWeave.Schema.AgentEvent do
  use Ecto.Schema
  import Ecto.Changeset

  schema "agent_events" do
    field :agent_id, :string
    field :event_type, :string
    field :payload, :string, default: "{}"
    field :created_at, :integer

  end

  def changeset(event, attrs) do
    event
    |> cast(attrs, [:agent_id, :event_type, :payload, :created_at])
    |> validate_required([:event_type, :created_at])
  end
end
