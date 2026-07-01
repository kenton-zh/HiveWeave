defmodule HiveWeave.Schema.Module do
  use Ecto.Schema
  import Ecto.Changeset

  schema "modules" do
    field :name, :string
    field :parent_module_id, :string
    field :status, :string, default: "active"
    field :current_agent_id, :string
    field :created_at, :integer
    field :updated_at, :integer

  end

  def changeset(module, attrs) do
    module
    |> cast(attrs, [:name, :parent_module_id, :status, :current_agent_id, :created_at, :updated_at])
    |> validate_required([:name, :created_at, :updated_at])
  end
end
