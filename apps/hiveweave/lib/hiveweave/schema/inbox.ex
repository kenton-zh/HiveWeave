defmodule HiveWeave.Schema.Inbox do
  use Ecto.Schema
  import Ecto.Changeset

  schema "inbox" do
    field :from_agent_id, :string
    field :to_agent_id, :string
    field :message, :string
    field :message_type, :string  # superior | peer | alarm
    field :read, :integer, default: 0
    field :expect_report, :integer, default: 0
    field :priority, :string, default: "normal"  # low | normal | urgent
    field :created_at, :integer
    field :read_at, :integer
  end

  def changeset(inbox, attrs) do
    inbox
    |> cast(attrs, [
      :from_agent_id, :to_agent_id, :message, :message_type, :read,
      :expect_report, :priority, :created_at, :read_at
    ])
    |> validate_required([:to_agent_id, :message_type, :message, :created_at])
  end
end
