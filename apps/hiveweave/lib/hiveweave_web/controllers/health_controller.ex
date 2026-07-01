defmodule HiveWeaveWeb.HealthController do
  use Phoenix.Controller, namespace: HiveWeaveWeb

  def index(conn, _params) do
    json(conn, %{
      status: "ok",
      version: "0.2.0",
      timestamp: System.system_time(:millisecond)
    })
  end
end

