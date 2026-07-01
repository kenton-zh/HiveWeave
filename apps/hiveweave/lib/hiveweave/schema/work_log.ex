defmodule HiveWeave.Schema.WorkLog do
  use Ecto.Schema
  import Ecto.Changeset

  schema "work_logs" do
    field :agent_id, :string
    field :project_id, :string
    field :task_id, :string
    field :action, :string
    field :summary, :string
    field :details, :string
    field :metadata, :string, default: "{}"
    field :created_at, :integer

  end

  def changeset(log, attrs) do
    log
    |> cast(attrs, [
      :agent_id, :project_id, :task_id, :action, :summary, :details,
      :metadata, :created_at
    ])
    |> validate_required([:agent_id, :action, :created_at])
  end
end
