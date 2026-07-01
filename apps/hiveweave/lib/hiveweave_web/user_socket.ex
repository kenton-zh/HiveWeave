defmodule HiveWeaveWeb.UserSocket do
  use Phoenix.Socket

  channel "lobby:*", HiveWeaveWeb.LobbyChannel
  channel "project:*", HiveWeaveWeb.ProjectChannel
  channel "agent:*", HiveWeaveWeb.AgentChannel

  @impl true
  def connect(params, socket, _connect_info) do
    case env_key() do
      nil -> {:ok, socket}
      expected ->
        provided = params["api_key"] || params["apiKey"] || ""
        if Plug.Crypto.secure_compare(provided, expected) do
          {:ok, socket}
        else
          :error
        end
    end
  end

  @impl true
  def id(_socket), do: nil

  defp env_key do
    env = System.get_env("HIVEWEAVE_API_KEY", "")
    if env == "", do: nil, else: env
  end
end
