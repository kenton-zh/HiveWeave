defmodule HiveWeave.Repo.Meta do
  use Ecto.Repo,
    otp_app: :hiveweave,
    adapter: Ecto.Adapters.SQLite3
end
