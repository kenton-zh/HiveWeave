defmodule HiveWeave.Schema.AgentTemplate do
  use Ecto.Schema
  import Ecto.Changeset

  @primary_key {:id, :string, autogenerate: false}
  schema "agent_templates" do
    field :source, :string, default: "agency-agents"
    field :division, :string, default: ""
    field :name, :string
    field :role, :string, default: "specialist"
    field :color, :string, default: ""
    field :emoji, :string, default: ""
    field :vibe, :string, default: ""
    field :description, :string, default: ""
    field :prompt_body, :string, default: ""
    field :original_file, :string, default: ""
    field :created_at, :integer
  end

  def changeset(template, attrs) do
    template
    |> cast(attrs, [
      :id, :source, :division, :name, :role, :color, :emoji, :vibe,
      :description, :prompt_body, :original_file, :created_at
    ])
    |> validate_required([:name, :created_at])
  end
end

