defmodule HiveWeave.Schema.ChatMessage do
  use Ecto.Schema
  import Ecto.Changeset

  @primary_key {:id, :binary_id, autogenerate: true}
  @foreign_key_type :binary_id
  schema "chat_messages" do
    field :agent_id, :binary_id
    field :role, :string
    field :content, :string
    field :tool_calls, :string, default: "[]"
    field :images, :string
    field :is_background, :boolean, default: false
    field :is_read, :boolean, default: true
    field :is_streaming, :boolean, default: false
    field :team_from_agent_id, :binary_id
    field :team_to_agent_id, :binary_id
    field :created_at, :integer

  end

  def changeset(message, attrs) do
    message
    |> cast(attrs, [
      :id, :agent_id, :role, :content, :tool_calls, :images, :is_background,
      :is_read, :is_streaming, :team_from_agent_id, :team_to_agent_id, :created_at
    ])
    |> validate_required([:agent_id, :role, :created_at])
  end
end

