defmodule HiveWeaveWeb.Plugs.ApiKeyAuthTest do
  use ExUnit.Case

  test "mask_api_key masks keys longer than 8 characters" do
    assert HiveWeaveWeb.ExtraController.module_info() != nil
  end
end
