defmodule HiveWeave.Schema.AgentCharter do
  use Ecto.Schema
  import Ecto.Changeset

  schema "agent_charters" do
    field :project_id, :string
    field :agent_id, :string
    field :title, :string
    field :content, :string
    field :status, :string, default: "draft"
    field :created_at, :integer
    field :updated_at, :integer

  end

  def changeset(charter, attrs) do
    charter
    |> cast(attrs, [:project_id, :agent_id, :title, :content, :status, :created_at, :updated_at])
    |> validate_required([:project_id, :title, :content, :created_at, :updated_at])
  end
end
