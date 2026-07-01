defmodule HiveWeave.Services.Approval do
  @moduledoc """
  ApprovalService — async approval flow for tool permission requests.

  When an agent attempts to use a tool listed in its `ask_tools`, the
  ToolExecutor creates a permission request here and blocks until the
  user (or coordinator) resolves it.

  Flow:
    1. ToolExecutor calls `request_permission/5` before executing
    2. Request is saved to meta DB + ETS (request_id → caller_pid)
    3. Frontend receives PubSub broadcast, shows approval dialog
    4. User approves/rejects via PermissionsController API
    5. Controller calls `resolve_request/3`
    6. Waiting process receives the result and continues

  Timeout: 120 seconds (configurable).
  """

  require Logger

  @approval_timeout_ms 120_000
  @table :approval_requests

  def ensure_table do
    if :ets.whereis(@table) == :undefined do
      :ets.new(@table, [:set, :public, :named_table, read_concurrency: true])
    end
  rescue
    ArgumentError -> :ok  # Already exists
  end

  @doc """
  Request permission for a tool execution. Blocks until resolved or timeout.
  Returns :ok if approved, {:error, reason} if rejected/timeout.
  """
  def request_permission(agent_id, project_id, tool_name, tool_args, description \\ "") do
    ensure_table()

    request_id = Ecto.UUID.generate()
    now = System.system_time(:millisecond)
    caller = self()

    # Save to ETS for lookup when resolved
    :ets.insert(@table, {request_id, caller})

    # Save to meta DB
    args_json = Jason.encode!(tool_args)

    {:ok, _} = Ecto.Adapters.SQL.query(
      HiveWeave.Repo.Meta,
      """
      INSERT INTO permission_requests (id, agent_id, project_id, tool_name, tool_arguments, description, status, created_at, updated_at)
      VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
      """,
      [request_id, agent_id, project_id, tool_name, args_json, description, now, now]
    )

    # Broadcast to frontend
    Phoenix.PubSub.broadcast(
      HiveWeave.PubSub,
      "project:#{project_id}",
      {:permission_request, %{
        id: request_id,
        agent_id: agent_id,
        tool_name: tool_name,
        tool_arguments: tool_args,
        description: description,
        timestamp: now
      }}
    )

    Logger.info("[Approval] Request #{request_id} created for agent #{agent_id} tool=#{tool_name}")

    # Wait for response
    receive do
      {:approval_result, ^request_id, :approved} ->
        Logger.info("[Approval] Request #{request_id} approved")
        :ok

      {:approval_result, ^request_id, {:rejected, reason}} ->
        Logger.info("[Approval] Request #{request_id} rejected: #{reason}")
        {:error, {:rejected, reason}}
    after
      @approval_timeout_ms ->
        # Clean up ETS entry
        :ets.delete(@table, request_id)
        # Update DB status
        Ecto.Adapters.SQL.query(
          HiveWeave.Repo.Meta,
          "UPDATE permission_requests SET status = 'timeout', updated_at = ? WHERE id = ?",
          [System.system_time(:millisecond), request_id]
        )
        Logger.warning("[Approval] Request #{request_id} timed out")
        {:error, :timeout}
    end
  end

  @doc """
  Resolve a pending permission request. Called by PermissionsController.
  """
  def resolve_request(request_id, decision, user_note \\ nil) do
    ensure_table()

    # Update DB
    status = if decision == :approved, do: "approved", else: "rejected"
    now = System.system_time(:millisecond)

    {:ok, _} = Ecto.Adapters.SQL.query(
      HiveWeave.Repo.Meta,
      "UPDATE permission_requests SET status = ?, user_note = ?, updated_at = ? WHERE id = ?",
      [status, user_note, now, request_id]
    )

    # Notify waiting process
    case :ets.lookup(@table, request_id) do
      [{^request_id, pid}] ->
        result = if decision == :approved, do: :approved, else: {:rejected, user_note || "rejected"}
        send(pid, {:approval_result, request_id, result})
        :ets.delete(@table, request_id)
        Logger.info("[Approval] Request #{request_id} resolved as #{status}")
        :ok

      [] ->
        Logger.warning("[Approval] Request #{request_id} not found in ETS (may have timed out)")
        {:error, :not_found}
    end
  end

  @doc """
  Get pending permission requests for a project.
  """
  def get_pending_requests(project_id) do
    {:ok, r} = Ecto.Adapters.SQL.query(
      HiveWeave.Repo.Meta,
      "SELECT id, agent_id, tool_name, tool_arguments, description, status, created_at FROM permission_requests WHERE project_id = ? AND status = 'pending' ORDER BY created_at DESC",
      [project_id]
    )

    r.rows
    |> Enum.map(fn [id, agent_id, tool_name, args, desc, status, created] ->
      %{
        id: id,
        agent_id: agent_id,
        tool_name: tool_name,
        tool_arguments: args,
        description: desc,
        status: status,
        created_at: created
      }
    end)
  rescue
    _ -> []
  end

  @doc """
  Check if a tool requires permission for the given agent.
  """
  def tool_requires_permission?(agent, tool_name) do
    ask_tools = get_ask_tools(agent)
    tool_name in ask_tools
  end

  defp get_ask_tools(agent) when is_map(agent) do
    raw = Map.get(agent, :ask_tools) || "[]"

    case Jason.decode(raw) do
      {:ok, list} when is_list(list) -> list
      _ -> []
    end
  rescue
    _ -> []
  end

  defp get_ask_tools(_), do: []

  @doc """
  Remember a permanently approved tool pattern for an agent.
  Saves to a 'permission_rules' table (upsert by agent_id + pattern).
  """
  def remember_approval(agent_id, project_id, tool_pattern) do
    now = System.system_time(:millisecond)
    id = Ecto.UUID.generate()

    {:ok, _} = Ecto.Adapters.SQL.query(
      HiveWeave.Repo.Meta,
      "DELETE FROM permission_rules WHERE agent_id = ? AND tool_pattern = ?",
      [agent_id, tool_pattern]
    )

    {:ok, _} = Ecto.Adapters.SQL.query(
      HiveWeave.Repo.Meta,
      "INSERT INTO permission_rules (id, agent_id, project_id, tool_pattern, action, created_at) VALUES (?, ?, ?, ?, 'allow', ?)",
      [id, agent_id, project_id, tool_pattern, now]
    )

    Logger.info("[Approval] Remembered allow rule: #{tool_pattern} for agent #{agent_id}")
    :ok
  rescue
    e ->
      Logger.warning("[Approval] Failed to remember rule: #{inspect(e)}")
      :error
  end

  @doc """
  Clear all pending (orphaned) permission requests on startup.
  """
  def cleanup_orphaned_requests do
    now = System.system_time(:millisecond)
    {:ok, _} = Ecto.Adapters.SQL.query(
      HiveWeave.Repo.Meta,
      "UPDATE permission_requests SET status = 'orphaned', updated_at = ? WHERE status = 'pending'",
      [now]
    )
    Logger.info("[Approval] Cleaned up orphaned permission requests")
    :ok
  end

  @doc """
  Load permanently saved permission rules for an agent.
  """
  def load_saved_rules(agent_id) do
    case Ecto.Adapters.SQL.query(
      HiveWeave.Repo.Meta,
      "SELECT tool_pattern FROM permission_rules WHERE agent_id = ? AND action = 'allow'",
      [agent_id]
    ) do
      {:ok, r} -> r.rows |> List.flatten() |> Enum.reject(&is_nil/1)
      _ -> []
    end
  rescue
    _ -> []
  end
end
