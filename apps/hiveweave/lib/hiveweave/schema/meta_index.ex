defmodule HiveWeave.Schema.MetaIndex do
  use Ecto.Schema
  import Ecto.Changeset

  schema "meta_index" do
    field :key, :string
    field :value, :string
    field :updated_at, :integer

  end

  def changeset(entry, attrs) do
    entry
    |> cast(attrs, [:key, :value, :updated_at])
    |> validate_required([:key])
  end
end
