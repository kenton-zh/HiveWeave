defmodule HiveWeave.Schema.ConversationTurn do
  use Ecto.Schema
  import Ecto.Changeset

  @primary_key {:id, :binary_id, autogenerate: true}
  @foreign_key_type :binary_id
  schema "conversation_turns" do
    field :agent_id, :binary_id
    field :turn_index, :integer
    field :role, :string
    field :content, :string
    field :tool_calls, :string, default: "[]"
    field :tool_call_id, :string
    field :prefix_hash, :string
    field :tokens_estimate, :integer, default: 0
    field :created_at, :integer

  end

  def changeset(turn, attrs) do
    turn
    |> cast(attrs, [
      :id, :agent_id, :turn_index, :role, :content, :tool_calls, :tool_call_id,
      :prefix_hash, :tokens_estimate, :created_at
    ])
    |> validate_required([:agent_id, :turn_index, :role, :created_at])
  end
end

