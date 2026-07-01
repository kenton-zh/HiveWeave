defmodule HiveWeave.Schema.PermissionRequest do
  use Ecto.Schema
  import Ecto.Changeset

  @primary_key {:id, :binary_id, autogenerate: true}
  @foreign_key_type :binary_id
  schema "permission_requests" do
    field :agent_id, :binary_id
    field :tool_name, :string
    field :tool_arguments, :string, default: "{}"
    field :description, :string, default: ""
    field :status, :string, default: "pending"
    field :remember, :boolean, default: false
    field :user_note, :string
    field :created_at, :integer
    field :updated_at, :integer
  end

  def changeset(req, attrs) do
    req
    |> cast(attrs, [
      :id, :agent_id, :tool_name, :tool_arguments, :description, :status,
      :remember, :user_note, :created_at, :updated_at
    ])
    |> validate_required([:agent_id, :tool_name, :created_at])
  end
end



