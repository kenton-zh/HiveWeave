defmodule HiveWeaveWeb.Presence do
  use Phoenix.Presence,
    otp_app: :hiveweave,
    pubsub_server: HiveWeave.PubSub
end
