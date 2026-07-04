defmodule HiveWeave.Services.SystemState do
  @moduledoc """
  Global system state managed via ETS for cross-process visibility.

  Stores:
    - :system_paused (boolean) — whether the system is globally paused

  This module is a GenServer so it can own a periodic timer that runs the
  hourly approval-cleanup sweep (mirrors the TS `runGameTimeTick` cleanup of
  orphaned permission requests). The ETS table is :public, so `paused?/0`,
  `pause/0` and `resume/0` read/write it directly without going through the
  GenServer mailbox.
  """

  use GenServer

  require Logger

  @table_name :hiveweave_system_state
  # 1 hour in milliseconds
  @hourly_cleanup_interval_ms 3_600_000

  # ── Public API (ETS-direct, no GenServer call) ──────────────

  def paused? do
    case :ets.lookup(@table_name, :system_paused) do
      [{:system_paused, paused}] -> paused
      [] -> false
    end
  end

  def pause do
    :ets.insert(@table_name, {:system_paused, true})
  end

  def resume do
    :ets.insert(@table_name, {:system_paused, false})
  end

  # ── GenServer lifecycle ─────────────────────────────────────

  def start_link(opts) do
    GenServer.start_link(__MODULE__, opts, name: __MODULE__)
  end

  def child_spec(opts) do
    %{
      id: __MODULE__,
      start: {__MODULE__, :start_link, [opts]},
      type: :worker,
      restart: :permanent
    }
  end

  @impl true
  def init(_opts) do
    table =
      case :ets.info(@table_name) do
        :undefined ->
          :ets.new(@table_name, [:named_table, :set, :public, read_concurrency: true])

        _ ->
          @table_name
      end

    :ets.insert(table, {:system_paused, false})

    # Schedule the hourly approval-cleanup sweep.
    :timer.send_interval(@hourly_cleanup_interval_ms, :hourly_cleanup)

    {:ok, %{}}
  end

  @impl true
  def handle_info(:hourly_cleanup, state) do
    try do
      result = HiveWeave.Services.Approval.cleanup_orphaned_requests()
      Logger.info("[SystemState] Hourly approval cleanup result: #{inspect(result)}")
    rescue
      e ->
        Logger.warning("[SystemState] Hourly approval cleanup failed: #{inspect(e)}")
    end

    {:noreply, state}
  end

  @impl true
  def handle_info(_msg, state) do
    {:noreply, state}
  end
end
