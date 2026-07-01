defmodule HiveWeave.Schema.ProjectIndex do
  use Ecto.Schema
  import Ecto.Changeset

  schema "project_index" do
    field :project_id, :string
    field :key, :string
    field :value, :string
    field :updated_at, :integer

  end

  def changeset(entry, attrs) do
    entry
    |> cast(attrs, [:project_id, :key, :value, :updated_at])
    |> validate_required([:project_id, :key])
  end
end
