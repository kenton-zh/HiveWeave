defmodule HiveWeave.Services.SettingsTest do
  use ExUnit.Case

  alias HiveWeave.Services.Settings

  @test_prefix "test_setting_"

  setup do
    test_key = "#{@test_prefix}#{System.unique_integer([:positive])}"
    on_exit(fn ->
      Settings.delete(test_key)
    end)
    {:ok, test_key: test_key}
  end

  # ── get/1 ──────────────────────────────────────────────────────

  describe "get/1" do
    test "returns nil for non-existent key" do
      result = Settings.get("nonexistent_key_12345")
      assert is_nil(result)
    end

    test "returns value for existing key", %{test_key: key} do
      Settings.set(key, "hello_world")
      assert Settings.get(key) == "hello_world"
    end

    test "returns value as string (not integer)", %{test_key: key} do
      Settings.set(key, 42)
      result = Settings.get(key)
      # set/2 converts to string via to_string/1
      assert result == "42"
    end

    test "returns nil for deleted key", %{test_key: key} do
      Settings.set(key, "value")
      Settings.delete(key)
      assert is_nil(Settings.get(key))
    end
  end

  # ── set/2 ──────────────────────────────────────────────────────

  describe "set/2" do
    test "sets a string value and returns {:ok, value}", %{test_key: key} do
      result = Settings.set(key, "test_value")
      assert {:ok, "test_value"} = result
      assert Settings.get(key) == "test_value"
    end

    test "overwrites existing value", %{test_key: key} do
      Settings.set(key, "old")
      Settings.set(key, "new")
      assert Settings.get(key) == "new"
    end

    test "handles integer values", %{test_key: key} do
      assert {:ok, 42} = Settings.set(key, 42)
      assert Settings.get(key) == "42"
    end

    test "handles empty string value", %{test_key: key} do
      assert {:ok, ""} = Settings.set(key, "")
      assert Settings.get(key) == ""
    end

    test "handles long string values", %{test_key: key} do
      long = String.duplicate("x", 10_000)
      assert {:ok, ^long} = Settings.set(key, long)
      assert Settings.get(key) == long
    end

    test "function has arity 2" do
      assert function_exported?(Settings, :set, 2)
    end
  end

  # ── all/0 ──────────────────────────────────────────────────────

  describe "all/0" do
    test "returns a map" do
      result = Settings.all()
      assert is_map(result)
    end

    test "includes previously set key", %{test_key: key} do
      Settings.set(key, "all_test_value")
      map = Settings.all()
      assert Map.has_key?(map, key)
      assert map[key] == "all_test_value"
    end

    test "does not include deleted key", %{test_key: key} do
      Settings.set(key, "temp")
      Settings.delete(key)
      map = Settings.all()
      refute Map.has_key?(map, key)
    end
  end

  # ── delete/1 ───────────────────────────────────────────────────

  describe "delete/1" do
    test "handles non-existent key gracefully" do
      result = Settings.delete("nonexistent_key_12345")
      assert result == :ok
    end

    test "deletes existing key and returns :ok", %{test_key: key} do
      Settings.set(key, "to_delete")
      assert Settings.delete(key) == :ok
      assert is_nil(Settings.get(key))
    end

    test "idempotent delete is safe", %{test_key: key} do
      Settings.set(key, "x")
      assert Settings.delete(key) == :ok
      assert Settings.delete(key) == :ok
    end
  end

  # ── Integration: round-trip ────────────────────────────────────

  describe "round-trip" do
    test "set -> get -> delete -> get returns nil", %{test_key: key} do
      Settings.set(key, "roundtrip")
      assert Settings.get(key) == "roundtrip"
      Settings.delete(key)
      assert is_nil(Settings.get(key))
    end

    test "set multiple keys and verify all", %{test_key: key} do
      key2 = "#{key}_b"
      on_exit(fn -> Settings.delete(key2) end)
      Settings.set(key, "a")
      Settings.set(key2, "b")
      map = Settings.all()
      assert map[key] == "a"
      assert map[key2] == "b"
    end
  end
end
