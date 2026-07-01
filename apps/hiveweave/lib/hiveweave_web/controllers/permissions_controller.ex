defmodule HiveWeaveWeb.PermissionsController do
  @moduledoc """
  Permissions and approval endpoints.

  Mirrors the legacy TS routes:
    GET    /api/permissions/rules/:agent_id
    PATCH  /api/permissions/rules/:agent_id
    GET    /api/permissions/pending/:agent_id
    GET    /api/permissions/pending/project/:project_id
    POST   /api/permissions/respond
  """
  use Phoenix.Controller

  import Ecto.Query

  alias HiveWeave.Schema.Agent
  alias HiveWeave.Schema.PermissionRequest

  plug :accepts, ["json"]

  # ---------------------------------------------------------------------------
  # Effective rules (rules/mode)
  # ---------------------------------------------------------------------------

  def get_rules(conn, %{"agent_id" => agent_id}) do
    case load_agent(agent_id) do
      nil ->
        conn
        |> put_status(404)
        |> json(%{error: "Agent not found"})

      agent ->
        json(conn, %{
          permissionMode: agent.permission_mode,
          allowedTools: decode_json_array(agent.allowed_tools),
          deniedTools: decode_json_array(agent.denied_tools),
          askTools: decode_json_array(agent.ask_tools),
          mcpServers: decode_json_array(agent.mcp_servers),
          boundSkills: decode_json_array(agent.bound_skills)
        })
    end
  end

  def update_rules(conn, %{"agent_id" => agent_id} = params) do
    case load_agent(agent_id) do
      nil ->
        conn
        |> put_status(404)
        |> json(%{error: "Agent not found"})

      agent ->
        attrs = %{}
        attrs = if Map.has_key?(params, "permissionMode"), do: Map.put(attrs, :permission_mode, params["permissionMode"]), else: attrs
        attrs = if Map.has_key?(params, "allowedTools"), do: Map.put(attrs, :allowed_tools, Jason.encode!(params["allowedTools"] || [])), else: attrs
        attrs = if Map.has_key?(params, "deniedTools"), do: Map.put(attrs, :denied_tools, Jason.encode!(params["deniedTools"] || [])), else: attrs
        attrs = if Map.has_key?(params, "askTools"), do: Map.put(attrs, :ask_tools, Jason.encode!(params["askTools"] || [])), else: attrs
        attrs = if Map.has_key?(params, "mcpServers"), do: Map.put(attrs, :mcp_servers, Jason.encode!(params["mcpServers"] || [])), else: attrs
        attrs = if Map.has_key?(params, "boundSkills"), do: Map.put(attrs, :bound_skills, Jason.encode!(params["boundSkills"] || [])), else: attrs

        case agent
             |> Ecto.Changeset.change(attrs)
             |> HiveWeave.Repo.Meta.update() do
          {:ok, updated} ->
            json(conn, %{
              permissionMode: updated.permission_mode,
              allowedTools: decode_json_array(updated.allowed_tools),
              deniedTools: decode_json_array(updated.denied_tools),
              askTools: decode_json_array(updated.ask_tools),
              mcpServers: decode_json_array(updated.mcp_servers),
              boundSkills: decode_json_array(updated.bound_skills)
            })

          {:error, _} = err ->
            conn
            |> put_status(500)
            |> json(%{error: "Failed to update rules"})
        end
    end
  end

  # ---------------------------------------------------------------------------
  # Pending approval requests
  # ---------------------------------------------------------------------------

  def get_pending(conn, %{"agent_id" => agent_id}) do
    case load_agent(agent_id) do
      nil ->
        conn
        |> put_status(404)
        |> json(%{error: "Agent not found in any project"})

      _agent ->
        uuid = to_uuid(agent_id)
        requests =
          HiveWeave.Repo.Meta.all(
            from(r in PermissionRequest,
              where: r.agent_id == ^uuid and r.status == "pending",
              order_by: [desc: r.created_at]
            )
          )
          |> Enum.map(&serialize_request/1)

        json(conn, requests)
    end
  end

  def get_project_pending(conn, %{"project_id" => project_id}) do
    uuid = to_uuid(project_id)
    agent_ids =
      case uuid do
        nil ->
          HiveWeave.Repo.Meta.all(
            from(a in Agent, where: a.project_id == ^project_id, select: a.id)
          )
          |> Enum.map(& &1)

        _ ->
          HiveWeave.Repo.Meta.all(
            from(a in Agent, where: a.project_id == ^uuid, select: a.id)
          )
          |> Enum.map(& &1)
      end

    requests =
      if Enum.empty?(agent_ids) do
        []
      else
        HiveWeave.Repo.Meta.all(
          from(r in PermissionRequest,
            where: r.agent_id in ^agent_ids and r.status == "pending",
            order_by: [desc: r.created_at]
          )
        )
        |> Enum.map(&serialize_request/1)
      end

    json(conn, requests)
  end

  # ---------------------------------------------------------------------------
  # Respond
  # ---------------------------------------------------------------------------

  def respond(conn, params) do
    request_id = params["requestId"]
    approved = params["approved"] == true
    remember = params["remember"] == true
    user_note = params["userNote"]

    cond do
      is_nil(request_id) or request_id == "" ->
        conn |> put_status(400) |> json(%{error: "requestId is required"})

      true ->
        case HiveWeave.Repo.Meta.get(PermissionRequest, request_id) do
          nil ->
            conn |> put_status(404) |> json(%{error: "Approval request not found"})

          request ->
            if request.status != "pending" do
              conn |> put_status(400) |> json(%{error: "Request is no longer pending", current_status: request.status})
            else
              new_status = if approved, do: "approved", else: "rejected"

              {:ok, updated} =
                request
                |> Ecto.Changeset.change(%{
                  status: new_status,
                  user_note: user_note,
                  remember: remember,
                  updated_at: System.system_time(:millisecond)
                })
                |> HiveWeave.Repo.Meta.update()

              # Notify the waiting process via ApprovalService
              decision = if approved, do: :approved, else: :rejected
              HiveWeave.Services.Approval.resolve_request(request_id, decision, user_note)

              json(conn, %{ok: true, request: serialize_request(updated)})
            end
        end
    end
  end

  # ---------------------------------------------------------------------------
  # Helpers
  # ---------------------------------------------------------------------------

  defp load_agent(agent_id) do
    case to_uuid(agent_id) do
      uuid when not is_nil(uuid) -> HiveWeave.Repo.Meta.get(Agent, uuid)
      _ -> HiveWeave.Repo.Meta.one(from(a in Agent, where: a.id == ^agent_id))
    end
  rescue
    _ -> nil
  end

  defp to_uuid(id) do
    case Ecto.UUID.cast(id) do
      {:ok, uuid} -> uuid
      :error -> nil
    end
  end

  defp serialize_request(r) do
    %{
      id: r.id,
      agentId: r.agent_id,
      toolName: r.tool_name,
      toolArguments: r.tool_arguments || "{}",
      description: r.description,
      status: r.status,
      createdAt: r.created_at,
      updatedAt: r.updated_at
    }
  end

  defp decode_json_array(nil), do: []
  defp decode_json_array("[]"), do: []

  defp decode_json_array(str) do
    case Jason.decode(str) do
      {:ok, list} when is_list(list) -> list
      _ -> []
    end
  end
end

