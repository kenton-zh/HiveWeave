defmodule HiveWeaveWeb.Endpoint do
  use Phoenix.Endpoint, otp_app: :hiveweave

  socket "/socket", HiveWeaveWeb.UserSocket,
    websocket: [
      check_origin: ["http://localhost:5173", "http://localhost:4000", "http://localhost:3200", "//localhost"],
      serializer: [
        {Phoenix.Socket.V2.JSONSerializer, "~> 2.0.0"},
        {Phoenix.Socket.V1.JSONSerializer, "~> 1.0.0"}
      ]
    ],
    longpoll: false

  plug Plug.RequestId
  plug Plug.Logger
  plug Plug.Telemetry, event_prefix: [:phoenix, :endpoint]
  plug CORSPlug, origin: ["http://localhost:5173", "http://localhost:4000", "http://localhost:3200"]
  plug Plug.Parsers,
    parsers: [:urlencoded, :json],
    pass: ["*/*"],
    json_decoder: Phoenix.json_library()

  plug HiveWeaveWeb.Router
end

