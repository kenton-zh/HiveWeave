defmodule HiveWeave.Schema.PersonnelRecord do
  use Ecto.Schema
  import Ecto.Changeset

  schema "personnel_records" do
    field :project_id, :string
    field :agent_id, :string
    field :position, :string
    field :department, :string
    field :responsibilities, :string
    field :notes, :string
    field :status, :string, default: "active"  # active | inactive | archived
    field :updated_by, :string
    field :updated_at, :integer

  end

  def changeset(record, attrs) do
    record
    |> cast(attrs, [
      :project_id, :agent_id, :position, :department, :responsibilities,
      :notes, :status, :updated_by, :updated_at
    ])
    |> validate_required([:project_id, :agent_id])
  end
end
