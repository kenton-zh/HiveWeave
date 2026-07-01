defmodule HiveWeaveWeb.Plugs.ApiKeyAuth do
  @moduledoc """
  Plug that validates an API key for HTTP requests.

  Reads the expected key from HIVEWEAVE_API_KEY env var.
  If unset in dev/test, skips validation (accepts all requests).

  Clients must send the key via:
    - Authorization: Bearer <key> header, or
    - x-api-key: <key> header, or
    - ?api_key=<key> query parameter

  Unauthenticated paths (always allowed):
    - GET /api/health
    - GET /
  """

  @behaviour Plug
  import Plug.Conn

  @unauthenticated_paths [
    {"GET", "/api/health"},
    {"GET", "/"}
  ]

  @impl true
  def init(opts), do: opts

  @impl true
  def call(conn, _opts) do
    if unauthenticated?(conn) do
      conn
    else
      verify(conn)
    end
  end

  defp unauthenticated?(conn) do
    method = conn.method |> String.upcase()
    path = conn.request_path

    Enum.any?(@unauthenticated_paths, fn {m, p} ->
      m == method and p == path
    end)
  end

  defp verify(conn) do
    case env_key() do
      nil -> conn
      expected ->
        if valid_key?(conn, expected) do
          conn
        else
          conn
          |> put_resp_content_type("application/json")
          |> send_resp(401, Jason.encode!(%{error: "Unauthorized — invalid or missing API key"}))
          |> halt()
        end
    end
  end

  defp env_key do
    env = System.get_env("HIVEWEAVE_API_KEY", "")
    if env == "", do: nil, else: env
  end

  defp valid_key?(conn, expected) do
    key = extract_key(conn)
    Plug.Crypto.secure_compare(key, expected)
  end

  defp extract_key(conn) do
    cond do
      header = get_req_header(conn, "authorization") |> List.first() ->
        case String.split(header, " ", parts: 2) do
          ["Bearer", key] -> key
          _ -> ""
        end
      key = get_req_header(conn, "x-api-key") |> List.first() ->
        key
      key = conn.params["api_key"] ->
        key
      true ->
        ""
    end
  end
end
