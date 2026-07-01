defmodule HiveWeave.Schema.ScheduledAlarm do
  use Ecto.Schema
  import Ecto.Changeset

  schema "scheduled_alarms" do
    field :project_id, :string
    field :from_agent_id, :string
    field :to_agent_id, :string
    field :purpose, :string
    field :fire_at_game_seconds, :integer
    field :fired, :boolean, default: false
    field :fired_at, :integer
    field :created_at, :integer

  end

  def changeset(alarm, attrs) do
    alarm
    |> cast(attrs, [
      :project_id, :from_agent_id, :to_agent_id, :purpose,
      :fire_at_game_seconds, :fired, :fired_at, :created_at
    ])
    |> validate_required([:project_id, :to_agent_id, :fire_at_game_seconds, :created_at])
  end
end
