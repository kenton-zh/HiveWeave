defmodule HiveWeave.EventAudit do
  @moduledoc """
  Lightweight event audit log.

  Records key events to the agent_events table in the PER-PROJECT database
  (not the Meta DB). The agent_events table is created by ProjectFactory's
  init_project_tables/1.

  Not full Event Sourcing - this is for read/timeline queries only.
  """
  use GenServer

  require Logger

  alias HiveWeave.Repo.ProjectFactory

  def start_link(_opts) do
    GenServer.start_link(__MODULE__, [], name: __MODULE__)
  end

  @impl true
  def init(_) do
    {:ok, %{}}
  end

  @doc """
  Log an event to the audit table in the agent's per-project DB.
  """
  def log(agent_id, event_type, payload \\ %{}) do
    json = Jason.encode!(payload)
    id = Ecto.UUID.generate()
    created_at = System.system_time(:millisecond)
    event_type_str = to_string(event_type)

    # Async insert to avoid blocking — routes to per-project DB via ProjectFactory
    Task.start(fn ->
      try do
        ProjectFactory.query_for_agent(
          agent_id,
          "INSERT INTO agent_events (id, agent_id, event_type, payload, created_at) VALUES (?, ?, ?, ?, ?)",
          [id, agent_id, event_type_str, json, created_at]
        )
      rescue
        e -> Logger.warning("EventAudit insert failed: #{inspect(e)}")
      end
    end)

    :ok
  end

  @doc """
  Get timeline of events for an agent from the per-project DB.
  """
  def timeline(agent_id, since_ms \\ hours_ago(1)) do
    case ProjectFactory.query_for_agent(
           agent_id,
           "SELECT id, agent_id, event_type, payload, created_at FROM agent_events WHERE agent_id = ? AND created_at > ? ORDER BY created_at DESC LIMIT 100",
           [agent_id, since_ms]
         ) do
      {:ok, r} ->
        r.rows |> Enum.map(fn row -> Enum.zip(r.columns, row) |> Enum.into(%{}) end)

      {:error, _} ->
        []
    end
  rescue
    _ -> []
  end

  defp hours_ago(h) do
    System.system_time(:millisecond) - h * 3_600_000
  end
end
