defmodule HiveWeave.Services.Org do
  @moduledoc """
  Organization service - CRUD for agents, tree traversal.
  """
  import Ecto.Query
  alias HiveWeave.Schema.Agent

  @doc """
  List all agents for a project.
  """
  def list_agents(project_id) do
    case project_id do
      nil -> HiveWeave.Repo.Meta.all(Agent)
      id ->
        # Try as binary_id, fall back to text
        case Ecto.UUID.cast(id) do
          {:ok, uuid} ->
            HiveWeave.Repo.Meta.all(from(a in Agent, where: a.project_id == ^uuid, order_by: [asc: a.created_at]))
          :error ->
            HiveWeave.Repo.Meta.all(from(a in Agent, where: a.project_id == ^id, order_by: [asc: a.created_at]))
        end
    end
  rescue
    _ -> []
  end

  @doc """
  Get agent by ID.
  """
  def get_agent(agent_id) do
    # Use raw text query because Agent's :id is :binary_id in schema
    # but Drizzle stores it as TEXT in SQLite. Repo.get/2 mismatches.
    HiveWeave.Repo.Meta.one(
      from(a in Agent, where: a.id == ^agent_id)
    )
  rescue
    _ -> nil
  end

  @doc """
  Get agent by role (ceo, hr, etc.) within a project.
  """
  def get_agent_by_role(project_id, role) do
    case Ecto.UUID.cast(project_id) do
      {:ok, uuid} ->
        HiveWeave.Repo.Meta.one(
          from(a in Agent, where: a.project_id == ^uuid and a.role == ^role)
        )
      :error ->
        HiveWeave.Repo.Meta.one(
          from(a in Agent, where: a.project_id == ^project_id and a.role == ^role)
        )
    end
  rescue
    _ -> nil
  end

  @doc """
  Get direct children of an agent.
  """
  def get_children(project_id, agent_id) do
    list_agents(project_id)
    |> Enum.filter(fn a -> a.parent_id == agent_id end)
  rescue
    _ -> []
  end

  @doc """
  Build the org tree for a project.
  """
  def build_tree(project_id) do
    agents = list_agents(project_id)
    agents_map = Map.new(agents, fn a -> {a.id, a} end)

    children_map =
      agents
      |> Enum.group_by(fn a -> a.parent_id end)

    build_tree_nodes(project_id, nil, children_map, agents_map)
  end

  defp build_tree_nodes(_project_id, parent_id, children_map, _agents_map) do
    children = Map.get(children_map, parent_id, [])

    Enum.map(children, fn child ->
      grandchildren = build_tree_nodes(child.project_id, child.id, children_map, _agents_map)

      %{
        id: child.id,
        short_id: child.short_id,
        name: child.name,
        role: child.role,
        status: child.status,
        permission_type: child.permission_type,
        goal: child.goal,
        children: if(grandchildren == [], do: nil, else: grandchildren)
      }
    end)
  end

  @doc """
  Create a new agent.

  Backfills `workspace_path` from the projects table so the agents row carries
  a redundant copy of the workspace path. This lets ProjectFactory reopen the
  per-project DB even if the projects row is later lost — mirrors the TS
  agentRegistry, but durable. See ProjectFactory.open_project_db/1.
  """
  def create_agent(attrs) do
    attrs =
      if Map.has_key?(attrs, :workspace_path) or Map.has_key?(attrs, "workspace_path") do
        attrs
      else
        case resolve_workspace_path_for_agent(attrs) do
          nil -> attrs
          ws_path -> Map.put(attrs, :workspace_path, ws_path)
        end
      end

    result =
      %Agent{}
      |> Agent.changeset(attrs)
      |> HiveWeave.Repo.Meta.insert()

    # Notify frontend to refresh org tree
    if match?({:ok, _}, result) do
      broadcast_org_changed()
    end

    result
  end

  # Resolve the workspace_path to backfill onto a new agent.
  # 1. Look up the project_id in the projects table.
  # 2. Fall back to a sibling agent's workspace_path (same project_id) if the
  #    projects row is missing — this covers the recovery window after a
  #    projects-table wipe where agents have already been repaired.
  defp resolve_workspace_path_for_agent(attrs) do
    project_id = attrs[:project_id] || attrs["project_id"]
    if is_nil(project_id), do: nil, else: resolve_workspace_path(project_id)
  end

  defp resolve_workspace_path(project_id) do
    import Ecto.Query
    alias HiveWeave.Repo.Meta
    alias HiveWeave.Schema.{Project, Agent}

    case Meta.one(from p in Project, where: p.id == ^project_id, select: p.workspace_path) do
      nil ->
        Meta.one(from a in Agent, where: a.project_id == ^project_id and not is_nil(a.workspace_path), select: a.workspace_path, limit: 1)

      ws_path ->
        ws_path
    end
  end

  @doc """
  Update an agent.
  """
  def update_agent(agent_id, attrs) do
    case get_agent(agent_id) do
      nil -> {:error, :not_found}
      agent ->
        agent
        |> Agent.changeset(attrs)
        |> HiveWeave.Repo.Meta.update()
    end
  end

  @doc """
  Delete an agent.
  """
  def delete_agent(agent_id) do
    case get_agent(agent_id) do
      nil -> {:error, :not_found}
      agent ->
        result = HiveWeave.Repo.Meta.delete(agent)
        if match?({:ok, _}, result), do: broadcast_org_changed()
        result
    end
  end

  # ── Org change broadcast ─────────────────────────────────────

  defp broadcast_org_changed do
    Phoenix.PubSub.broadcast(
      HiveWeave.PubSub,
      "lobby:status",
      {:org_changed}
    )
  end

  @doc """
  Transfer an agent to a new parent. Verifies no cycle.
  """
  def transfer_agent(project_id, agent_id, new_parent_id) do
    # Check no cycle: new_parent must not be the agent itself
    if agent_id == new_parent_id do
      {:error, "Cannot transfer an agent to be its own parent"}
    else
      # Check new_parent is not a descendant of agent_id
      descendants = get_all_descendants(project_id, agent_id)
      if Enum.any?(descendants, fn a -> a.id == new_parent_id end) do
        {:error, "Cannot transfer: new parent is a descendant (would create a cycle)"}
      else
        update_agent(agent_id, %{parent_id: new_parent_id, updated_at: System.system_time(:second)})
      end
    end
  end

  @doc """
  Dismiss (soft-delete) an agent. Verifies no subordinates.
  """
  def dismiss_agent(project_id, agent_id) do
    children = get_children(project_id, agent_id)
    if children != [] do
      {:error, "Cannot dismiss agent with #{length(children)} subordinate(s). Transfer or dismiss them first."}
    else
      case update_agent(agent_id, %{status: "archived", updated_at: System.system_time(:second)}) do
        {:ok, agent} ->
          # Archive agent memories
          HiveWeave.Services.Memory.archive_agent_memories(project_id, agent_id)
          {:ok, agent}
        err -> err
      end
    end
  end

  @doc """
  Get all descendants of an agent (recursive).
  """
  def get_all_descendants(project_id, agent_id) do
    children = get_children(project_id, agent_id)
    children ++ Enum.flat_map(children, fn c -> get_all_descendants(project_id, c.id) end)
  end

  @doc """
  Generate a short ID for an agent (A001, A002, etc.)
  """
  def generate_short_id do
    count = count_all_agents()
    letter_index = rem(div(count, 1000), 26)
    letter = <<?A + letter_index>>
    num = rem(count, 1000) + 1
    "#{letter}#{String.pad_leading(Integer.to_string(num), 3, "0")}"
  end

  defp count_all_agents do
    case Ecto.Adapters.SQL.query(HiveWeave.Repo.Meta,
      "SELECT COUNT(*) FROM agents", []) do
      {:ok, %{rows: [[count] | _]}} -> count
      _ -> :rand.uniform(1000)
    end
  rescue
    _ -> :rand.uniform(1000)
  end
end
