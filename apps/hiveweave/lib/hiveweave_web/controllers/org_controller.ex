defmodule HiveWeaveWeb.OrgController do
  use Phoenix.Controller

  alias HiveWeave.Services.Org

  plug :accepts, ["json"]

  def tree(conn, params) do
    project_id = params["projectId"]
    tree = Org.build_tree(project_id)
    json(conn, %{tree: tree})
  end

  def list_agents(conn, params) do
    project_id = params["projectId"] || params["project_id"]

    agents =
      case project_id do
        nil -> Org.list_agents(nil) |> Enum.map(&serialize_agent/1)
        id -> Org.list_agents(id) |> Enum.map(&serialize_agent/1)
      end

    json(conn, %{agents: agents})
  end

  def show_agent(conn, %{"id" => id}) do
    case get_agent_by_id(id) do
      nil ->
        conn
        |> put_status(404)
        |> json(%{error: "Not found"})
      agent -> json(conn, %{agent: serialize_agent(agent)})
    end
  end

  def create_agent(conn, %{"name" => name} = params) do
    attrs = %{
      short_id: Org.generate_short_id(),
      name: name,
      project_id: params["projectId"],
      role: params["role"] || "executor",
      parent_id: params["parentId"],
      goal: params["goal"] || "",
      backstory: params["backstory"] || "",
      permission_type: params["permissionType"] || "executor",
      model_id: params["modelId"],
      created_at: System.system_time(:millisecond),
      updated_at: System.system_time(:millisecond)
    }

    case Org.create_agent(attrs) do
      {:ok, agent} -> json(conn, %{agent: serialize_agent(agent)})
      {:error, changeset} ->
        conn
        |> put_status(422)
        |> json(%{errors: format_errors(changeset)})
    end
  end

  def update_agent(conn, %{"id" => id} = params) do
    attrs = Map.take(params, ["name", "goal", "status", "backstory", "model_id", "modelId", "parent_id", "parentId", "permission_type", "permissionType", "module_id", "moduleId"])
    |> Map.put("updated_at", System.system_time(:millisecond))

    # Normalize camelCase keys to snake_case
    attrs = attrs
    |> normalize_key("modelId", "model_id")
    |> normalize_key("parentId", "parent_id")
    |> normalize_key("permissionType", "permission_type")
    |> normalize_key("moduleId", "module_id")

    case Org.update_agent(id, attrs) do
      {:ok, agent} -> json(conn, %{agent: serialize_agent(agent)})
      {:error, :not_found} ->
        conn
        |> put_status(404)
        |> json(%{error: "Not found"})
      {:error, changeset} ->
        conn
        |> put_status(422)
        |> json(%{errors: format_errors(changeset)})
    end
  end

  def delete_agent(conn, %{"id" => id}) do
    case Org.delete_agent(id) do
      {:ok, _} -> json(conn, %{ok: true})
      {:error, :not_found} ->
        conn
        |> put_status(404)
        |> json(%{error: "Not found"})
      {:error, _} ->
        conn
        |> put_status(500)
        |> json(%{error: "Delete failed"})
    end
  end

  def children(conn, %{"id" => id}) do
    case get_agent_by_id(id) do
      nil ->
        conn
        |> put_status(404)
        |> json(%{error: "Not found"})
      agent ->
        children_list = Org.get_children(agent.project_id, id)
        json(conn, %{children: Enum.map(children_list, &serialize_agent/1)})
    end
  end

  def list_modules(conn, params) do
    project_id = params["projectId"] || params["project_id"]

    modules =
      case project_id do
        nil -> []
        pid ->
          case HiveWeave.Repo.ProjectFactory.query(
                 pid,
                 "SELECT id, name, parent_module_id, status, current_agent_id, created_at, updated_at FROM modules ORDER BY created_at ASC",
                 []
               ) do
            {:ok, r} ->
              r.rows
              |> Enum.map(fn row -> Enum.zip(r.columns, row) |> Enum.into(%{}) end)
              |> Enum.map(&serialize_module_map/1)

            {:error, _} ->
              []
          end
      end

    json(conn, %{modules: modules})
  rescue
    _ -> json(conn, %{modules: []})
  catch
    :exit, _ -> json(conn, %{modules: []})
  end

  defp get_agent_by_id(id) do
    Org.get_agent(id)
  rescue
    _ -> nil
  end

  defp normalize_key(map, from, to) do
    case Map.pop(map, from) do
      {nil, map} -> map
      {val, map} -> Map.put(map, to, val)
    end
  end

  defp serialize_agent(nil), do: nil
  defp serialize_agent(a) do
    %{
      id: a.id,
      short_id: a.short_id,
      shortId: a.short_id,
      project_id: a.project_id,
      projectId: a.project_id,
      name: a.name,
      role: a.role,
      parent_id: a.parent_id,
      parentId: a.parent_id,
      module_id: a.module_id,
      moduleId: a.module_id,
      status: a.status,
      goal: a.goal,
      backstory: a.backstory,
      skills: a.skills,
      model_id: a.model_id,
      modelId: a.model_id,
      permission_type: a.permission_type,
      permissionType: a.permission_type,
      permission_mode: a.permission_mode,
      permissionMode: a.permission_mode,
      allowed_tools: a.allowed_tools,
      allowedTools: a.allowed_tools,
      denied_tools: a.denied_tools,
      deniedTools: a.denied_tools,
      ask_tools: a.ask_tools,
      askTools: a.ask_tools,
      mcp_servers: a.mcp_servers,
      mcpServers: a.mcp_servers,
      bound_skills: a.bound_skills,
      boundSkills: a.bound_skills,
      created_at: a.created_at,
      createdAt: a.created_at,
      updated_at: a.updated_at,
      updatedAt: a.updated_at
    }
  end

  defp format_errors(changeset) do
    Ecto.Changeset.traverse_errors(changeset, fn {msg, _} -> msg end)
  end

  defp serialize_module_map(m) do
    %{
      "id" => m["id"],
      "name" => m["name"],
      "parent_module_id" => m["parent_module_id"],
      "parentModuleId" => m["parent_module_id"],
      "status" => m["status"],
      "current_agent_id" => m["current_agent_id"],
      "currentAgentId" => m["current_agent_id"],
      "created_at" => m["created_at"],
      "createdAt" => m["created_at"],
      "updated_at" => m["updated_at"],
      "updatedAt" => m["updated_at"]
    }
  end
end

