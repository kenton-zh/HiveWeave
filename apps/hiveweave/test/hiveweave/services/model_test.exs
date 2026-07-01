defmodule HiveWeave.Services.ModelTest do
  use ExUnit.Case

  alias HiveWeave.Services.Model

  @test_prefix "test_model_"

  setup do
    test_id = "#{@test_prefix}#{System.unique_integer([:positive])}"
    on_exit(fn ->
      Model.delete_model(test_id)
    end)
    {:ok, test_id: test_id}
  end

  # ── list_models/0 ───────────────────────────────────────────────

  describe "list_models/0" do
    test "returns a list" do
      result = Model.list_models()
      assert is_list(result)
    end

    test "includes previously created model", %{test_id: id} do
      Model.create_model(%{id: id, name: "List Test Model", model_id: "list-model", base_url: "https://api.example.com/v1"})
      models = Model.list_models()
      assert Enum.any?(models, fn m -> m.id == id end)
    end

    test "does not include deleted model", %{test_id: id} do
      Model.create_model(%{id: id, name: "Temp Model", model_id: "temp-model", base_url: "https://api.example.com/v1"})
      Model.delete_model(id)
      models = Model.list_models()
      refute Enum.any?(models, fn m -> m.id == id end)
    end

    test "masks api_key in list", %{test_id: id} do
      Model.create_model(%{id: id, name: "Key Test", model_id: "key-test", base_url: "https://api.example.com/v1", api_key: "sk-super-secret-key-12345"})
      models = Model.list_models()
      model = Enum.find(models, fn m -> m.id == id end)
      refute model.api_key == "sk-super-secret-key-12345"
      assert String.ends_with?(model.api_key, "...")
    end
  end

  # ── get_model/1 ─────────────────────────────────────────────────

  describe "get_model/1" do
    test "returns {:error, :not_found} for non-existent id" do
      assert {:error, :not_found} = Model.get_model("nonexistent_model_12345")
    end

    test "returns {:ok, map} for existing model", %{test_id: id} do
      Model.create_model(%{id: id, name: "Get Test Model", model_id: "get-model", base_url: "https://api.example.com/v1", api_key: "sk-secret"})
      assert {:ok, model} = Model.get_model(id)
      assert model.id == id
      assert model.name == "Get Test Model"
      assert model.model_id == "get-model"
      assert model.api_key == "sk-secret"
    end

    test "returns full api_key in get (not masked)", %{test_id: id} do
      Model.create_model(%{id: id, name: "Full Key Test", model_id: "full-key", base_url: "https://api.example.com/v1", api_key: "sk-full-secret-key"})
      {:ok, model} = Model.get_model(id)
      assert model.api_key == "sk-full-secret-key"
    end
  end

  # ── create_model/1 ──────────────────────────────────────────────

  describe "create_model/1" do
    test "creates with defaults", %{test_id: id} do
      assert {:ok, result} = Model.create_model(%{id: id, name: "Default Model", model_id: "def-model", base_url: "https://api.example.com/v1"})
      assert result.id == id
      assert result.name == "Default Model"
      assert {:ok, model} = Model.get_model(id)
      assert model.context_window == 128_000
      assert model.max_output_tokens == 8_192
      assert model.is_active == true
    end

    test "creates with custom context_window", %{test_id: id} do
      Model.create_model(%{id: id, name: "Big Context", model_id: "big-ctx", base_url: "https://api.example.com/v1", context_window: 256_000})
      {:ok, model} = Model.get_model(id)
      assert model.context_window == 256_000
    end

    test "creates with is_active = false", %{test_id: id} do
      Model.create_model(%{id: id, name: "Inactive", model_id: "inactive", base_url: "https://api.example.com/v1", is_active: false})
      {:ok, model} = Model.get_model(id)
      assert model.is_active == false
    end

    test "generates UUID if no id provided" do
      {:ok, result} = Model.create_model(%{name: "Auto ID", model_id: "auto-id", base_url: "https://api.example.com/v1"})
      assert is_binary(result.id)
      assert byte_size(result.id) > 0
      Model.delete_model(result.id)
    end
  end

  # ── update_model/2 ──────────────────────────────────────────────

  describe "update_model/2" do
    test "updates name", %{test_id: id} do
      Model.create_model(%{id: id, name: "Old Name", model_id: "upd-model", base_url: "https://api.example.com/v1"})
      assert {:ok, ^id} = Model.update_model(id, %{name: "New Name"})
      {:ok, model} = Model.get_model(id)
      assert model.name == "New Name"
    end

    test "updates is_active", %{test_id: id} do
      Model.create_model(%{id: id, name: "Toggle", model_id: "toggle", base_url: "https://api.example.com/v1"})
      Model.update_model(id, %{is_active: false})
      {:ok, model} = Model.get_model(id)
      assert model.is_active == false
      Model.update_model(id, %{is_active: true})
      {:ok, model} = Model.get_model(id)
      assert model.is_active == true
    end

    test "returns error when no fields to update", %{test_id: id} do
      Model.create_model(%{id: id, name: "No Update", model_id: "no-upd", base_url: "https://api.example.com/v1"})
      assert {:error, "No fields to update"} = Model.update_model(id, %{})
    end
  end

  # ── delete_model/1 ──────────────────────────────────────────────

  describe "delete_model/1" do
    test "returns :ok for non-existent id" do
      assert :ok = Model.delete_model("nonexistent_model_12345")
    end

    test "deletes existing model", %{test_id: id} do
      Model.create_model(%{id: id, name: "Delete Me", model_id: "del-me", base_url: "https://api.example.com/v1"})
      assert :ok = Model.delete_model(id)
      assert {:error, :not_found} = Model.get_model(id)
    end

    test "idempotent delete is safe", %{test_id: id} do
      Model.create_model(%{id: id, name: "Idempotent", model_id: "idem", base_url: "https://api.example.com/v1"})
      assert :ok = Model.delete_model(id)
      assert :ok = Model.delete_model(id)
    end
  end

  # ── get_active_models/0 ─────────────────────────────────────────

  describe "get_active_models/0" do
    test "returns only active models", %{test_id: id} do
      active_id = "#{id}_active"
      inactive_id = "#{id}_inactive"
      on_exit(fn ->
        Model.delete_model(active_id)
        Model.delete_model(inactive_id)
      end)
      Model.create_model(%{id: active_id, name: "Active", model_id: "active-m", base_url: "https://api.example.com/v1", is_active: true})
      Model.create_model(%{id: inactive_id, name: "Inactive", model_id: "inactive-m", base_url: "https://api.example.com/v1", is_active: false})
      active = Model.get_active_models()
      assert Enum.any?(active, fn m -> m.id == active_id end)
      refute Enum.any?(active, fn m -> m.id == inactive_id end)
    end
  end

  # ── round-trip ──────────────────────────────────────────────────

  describe "round-trip" do
    test "create -> get -> update -> get -> delete -> get", %{test_id: id} do
      Model.create_model(%{id: id, name: "Round Trip", model_id: "rt-model", base_url: "https://api.example.com/v1", api_key: "sk-test"})
      {:ok, model} = Model.get_model(id)
      assert model.name == "Round Trip"
      Model.update_model(id, %{name: "Updated", context_window: 64_000})
      {:ok, model} = Model.get_model(id)
      assert model.name == "Updated"
      assert model.context_window == 64_000
      Model.delete_model(id)
      assert {:error, :not_found} = Model.get_model(id)
    end
  end
end
