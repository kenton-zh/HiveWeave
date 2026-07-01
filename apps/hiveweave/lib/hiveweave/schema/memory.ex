defmodule HiveWeave.Schema.Memory do
  use Ecto.Schema
  import Ecto.Changeset

  schema "memories" do
    field :agent_id, :string
    field :scope, :string  # project | agent_private | archive
    field :module_id, :string
    field :type, :string
    field :content, :string
    field :source_agent_id, :string
    field :metadata, :string, default: "{}"
    field :created_at, :integer
    field :updated_at, :integer

  end

  def changeset(memory, attrs) do
    memory
    |> cast(attrs, [
      :agent_id, :scope, :module_id, :type, :content, :source_agent_id,
      :metadata, :created_at, :updated_at
    ])
    |> validate_required([:scope, :type, :content, :created_at, :updated_at])
  end
end
