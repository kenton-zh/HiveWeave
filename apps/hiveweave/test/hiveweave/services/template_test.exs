defmodule HiveWeave.Services.TemplateTest do
  use ExUnit.Case

  alias HiveWeave.Services.Template

  @test_prefix "test_template_"

  setup do
    test_id = "#{@test_prefix}#{System.unique_integer([:positive])}"
    on_exit(fn ->
      Ecto.Adapters.SQL.query(HiveWeave.Repo.Meta, "DELETE FROM agent_templates WHERE id = ?", [test_id])
    end)
    {:ok, test_id: test_id}
  end

  # ── list_templates/1 ────────────────────────────────────────────

  describe "list_templates/1" do
    test "returns a list with no args" do
      result = Template.list_templates()
      assert is_list(result)
    end

    test "returns a list with empty map" do
      result = Template.list_templates(%{})
      assert is_list(result)
    end

    test "includes previously created template", %{test_id: id} do
      unique_source = "test-source-#{System.unique_integer([:positive])}"
      Template.create_template(%{id: id, name: "List Template", source: unique_source})
      templates = Template.list_templates(%{source: unique_source})
      assert length(templates) >= 1
      assert Enum.any?(templates, fn t -> t.id == id end)
    end

    test "filters by source", %{test_id: id} do
      Template.create_template(%{id: id, name: "Source Filter", source: "unique-source-99"})
      result = Template.list_templates(%{source: "unique-source-99"})
      assert length(result) >= 1
      assert Enum.all?(result, fn t -> t.source == "unique-source-99" end)
    end

    test "filters by division", %{test_id: id} do
      Template.create_template(%{id: id, name: "Div Template", division: "engineering-88"})
      result = Template.list_templates(%{division: "engineering-88"})
      assert length(result) >= 1
      assert Enum.all?(result, fn t -> t.division == "engineering-88" end)
    end

    test "filters by search on name", %{test_id: id} do
      Template.create_template(%{id: id, name: "Searchable Unique", description: "Some desc"})
      result = Template.list_templates(%{search: "Searchable Unique"})
      assert length(result) >= 1
      assert Enum.any?(result, fn t -> t.name == "Searchable Unique" end)
    end

    test "filters by search on description", %{test_id: id} do
      Template.create_template(%{id: id, name: "Search Desc", description: "find-me-xyz-123"})
      result = Template.list_templates(%{search: "find-me-xyz-123"})
      assert length(result) >= 1
    end

    test "returns empty for non-matching search", %{test_id: id} do
      Template.create_template(%{id: id, name: "No Match", description: "abc"})
      result = Template.list_templates(%{search: "zzz-nonexistent-99999"})
      assert result == []
    end

    test "prompt_body is NOT included in list (only description)", %{test_id: id} do
      unique_source = "no-body-#{System.unique_integer([:positive])}"
      Template.create_template(%{id: id, name: "No Body", source: unique_source, prompt_body: "secret system prompt"})
      templates = Template.list_templates(%{source: unique_source})
      assert length(templates) >= 1
      template = Enum.find(templates, fn t -> t.id == id end)
      refute Map.has_key?(template, :prompt_body)
    end
  end

  # ── get_template/1 ──────────────────────────────────────────────

  describe "get_template/1" do
    test "returns {:error, :not_found} for non-existent id" do
      assert {:error, :not_found} = Template.get_template("nonexistent_template_12345")
    end

    test "returns {:ok, map} with prompt_body for existing template", %{test_id: id} do
      Template.create_template(%{id: id, name: "Get Template", source: "agency-agents", role: "engineer", prompt_body: "You are a helpful assistant."})
      assert {:ok, template} = Template.get_template(id)
      assert template.id == id
      assert template.name == "Get Template"
      assert template.source == "agency-agents"
      assert template.role == "engineer"
      assert template.prompt_body == "You are a helpful assistant."
    end

    test "returns all fields including color, emoji, vibe", %{test_id: id} do
      Template.create_template(%{id: id, name: "Full Template", color: "#FF0000", emoji: "🤖", vibe: "friendly", description: "A helpful bot"})
      {:ok, template} = Template.get_template(id)
      assert template.color == "#FF0000"
      assert template.emoji == "🤖"
      assert template.vibe == "friendly"
      assert template.description == "A helpful bot"
    end
  end

  # ── create_template/1 ───────────────────────────────────────────

  describe "create_template/1" do
    test "creates with defaults", %{test_id: id} do
      assert {:ok, result} = Template.create_template(%{id: id, name: "Default Template"})
      assert result.id == id
      assert result.name == "Default Template"
      {:ok, template} = Template.get_template(id)
      assert template.source == "custom"
      assert template.role == "specialist"
      assert template.division == ""
      assert template.color == ""
      assert template.emoji == ""
      assert template.vibe == ""
      assert template.description == ""
      assert template.prompt_body == ""
    end

    test "generates UUID if no id provided" do
      {:ok, result} = Template.create_template(%{name: "Auto ID Template"})
      assert is_binary(result.id)
      assert byte_size(result.id) > 0
      Ecto.Adapters.SQL.query(HiveWeave.Repo.Meta, "DELETE FROM agent_templates WHERE id = ?", [result.id])
    end
  end

  # ── round-trip ──────────────────────────────────────────────────

  describe "round-trip" do
    test "create -> get -> list verifies presence", %{test_id: id} do
      Template.create_template(%{id: id, name: "Round Trip Template", source: "test", division: "qa", role: "tester", prompt_body: "system: you are a tester"})
      {:ok, template} = Template.get_template(id)
      assert template.name == "Round Trip Template"
      assert template.source == "test"
      assert template.role == "tester"
      templates = Template.list_templates(%{source: "test"})
      assert Enum.any?(templates, fn t -> t.id == id end)
    end
  end
end
