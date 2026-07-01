defmodule HiveWeaveWeb.HealthControllerTest do
  use ExUnit.Case

  test "health response has expected shape" do
    result = %{
      status: "ok",
      version: "0.2.0",
      timestamp: System.system_time(:millisecond)
    }
    assert result[:status] == "ok"
    assert is_binary(result[:version])
    assert is_integer(result[:timestamp])
  end
end
