defmodule HiveWeave.Services.Template do
  @moduledoc """
  TemplateService — Agent personality template catalog CRUD.
  Used by HR to browse and quick-create agents with pre-configured roles.
  """

  require Logger

  @doc "List all templates with optional filters"
  def list_templates(opts \\ %{}) do
    source = opts[:source]
    division = opts[:division]
    search = opts[:search]

    {where_clause, params} = build_template_filters(source, division, search)
    sql = "SELECT id, source, division, name, role, color, emoji, vibe, description FROM agent_templates #{where_clause} ORDER BY source, division, name LIMIT 50"

    {:ok, r} = Ecto.Adapters.SQL.query(HiveWeave.Repo.Meta, sql, params)
    r.rows |> Enum.map(fn [id, src, div, name, role, color, emoji, vibe, desc] ->
      %{id: id, source: src, division: div, name: name, role: role, color: color, emoji: emoji, vibe: vibe, description: desc}
    end)
  rescue
    e ->
      Logger.warning("[Template] list_templates failed: #{inspect(e)}")
      []
  end

  defp build_template_filters(nil, nil, nil), do: {"", []}
  defp build_template_filters(source, division, search) do
    conditions = []
    params = []
    {conditions, params} = if source, do: {["source = ?" | conditions], [source | params]}, else: {conditions, params}
    {conditions, params} = if division, do: {["division = ?" | conditions], [division | params]}, else: {conditions, params}
    {conditions, params} = if search, do: {["(name LIKE ? OR description LIKE ?)" | conditions], ["%#{search}%" | ["%#{search}%" | params]]}, else: {conditions, params}
    if conditions != [], do: {"WHERE #{Enum.join(conditions, " AND ")}", params}, else: {"", []}
  end

  @doc "Get a template by ID"
  def get_template(id) do
    {:ok, r} = Ecto.Adapters.SQL.query(HiveWeave.Repo.Meta,
      "SELECT id, source, division, name, role, color, emoji, vibe, description, prompt_body FROM agent_templates WHERE id = ? LIMIT 1", [id])
    case r.rows do
      [[id, src, div, name, role, color, emoji, vibe, desc, body]] ->
        {:ok, %{id: id, source: src, division: div, name: name, role: role, color: color, emoji: emoji, vibe: vibe, description: desc, prompt_body: body}}
      _ -> {:error, :not_found}
    end
  rescue
    e -> {:error, inspect(e)}
  end

  @doc "Create a template"
  def create_template(attrs) do
    id = attrs[:id] || Ecto.UUID.generate()
    now = System.system_time(:millisecond)
    case Ecto.Adapters.SQL.query(HiveWeave.Repo.Meta,
      "INSERT INTO agent_templates (id, source, division, name, role, color, emoji, vibe, description, prompt_body, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
      [id, attrs[:source] || "custom", attrs[:division] || "", attrs[:name] || "", attrs[:role] || "specialist", attrs[:color] || "", attrs[:emoji] || "", attrs[:vibe] || "", attrs[:description] || "", attrs[:prompt_body] || "", now]) do
      {:ok, _} -> {:ok, %{id: id, name: attrs[:name]}}
      {:error, reason} -> {:error, inspect(reason)}
    end
  rescue
    e -> {:error, inspect(e)}
  end
end
