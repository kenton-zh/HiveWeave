defmodule HiveWeave.Schema.GlobalSetting do
  use Ecto.Schema
  import Ecto.Changeset

  @primary_key {:key, :string, autogenerate: false}
  schema "global_settings" do
    field :value, :string
    field :updated_at, :integer
  end

  def changeset(setting, attrs) do
    setting
    |> cast(attrs, [:key, :value, :updated_at])
    |> validate_required([:key, :value])
  end
end

