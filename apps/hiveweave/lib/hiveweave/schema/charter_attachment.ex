defmodule HiveWeave.Schema.CharterAttachment do
  use Ecto.Schema
  import Ecto.Changeset

  schema "charter_attachments" do
    field :charter_id, :string
    field :filename, :string
    field :content_type, :string
    field :file_path, :string
    field :file_size, :integer
    field :created_at, :integer

  end

  def changeset(attachment, attrs) do
    attachment
    |> cast(attrs, [
      :charter_id, :filename, :content_type, :file_path, :file_size, :created_at
    ])
    |> validate_required([:charter_id, :filename, :file_path, :created_at])
  end
end
