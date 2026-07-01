defmodule HiveWeave.Schema.LlmModel do
  use Ecto.Schema
  import Ecto.Changeset

  @primary_key {:id, :string, autogenerate: false}
  schema "llm_models" do
    field :name, :string
    field :model_id, :string
    field :base_url, :string
    field :api_key, :string
    field :context_window, :integer, default: 128_000
    field :max_output_tokens, :integer, default: 8_192
    field :supports_thinking, :boolean, default: false
    field :default_reasoning_effort, :string
    field :temperature, :string
    field :is_active, :boolean, default: true
    field :created_at, :integer
    field :updated_at, :integer
    field :inserted_at, :any, virtual: true
    field :updated_at_auto, :any, virtual: true
  end

  def changeset(model, attrs) do
    model
    |> cast(attrs, [
      :id, :name, :model_id, :base_url, :api_key, :context_window, :max_output_tokens,
      :supports_thinking, :default_reasoning_effort, :temperature, :is_active,
      :created_at, :updated_at
    ])
    |> validate_required([:name, :model_id, :base_url, :api_key])
  end
end


