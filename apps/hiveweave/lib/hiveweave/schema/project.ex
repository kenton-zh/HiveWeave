defmodule HiveWeave.Schema.Project do
  use Ecto.Schema
  import Ecto.Changeset

  @primary_key {:id, :binary_id, autogenerate: true}
  @foreign_key_type :binary_id
  schema "projects" do
    field :name, :string
    field :description, :string
    field :workspace_path, :string
    field :org_paradigm, :string
    field :charter_json, :string
    field :goals_json, :string
    field :created_at, :integer
  end

  def changeset(project, attrs) do
    project
    |> cast(attrs, [:id, :name, :description, :workspace_path, :org_paradigm, :charter_json, :goals_json, :created_at])
    |> validate_required([:name, :created_at])
  end
end


