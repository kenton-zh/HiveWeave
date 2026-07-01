defmodule HiveWeave.Services.SystemState do
  @moduledoc """
  Global system state managed via ETS for cross-process visibility.

  Stores:
    - :system_paused (boolean) — whether the system is globally paused
  """

  @table_name :hiveweave_system_state

  def start_link(_opts) do
    table =
      case :ets.info(@table_name) do
        :undefined ->
          :ets.new(@table_name, [:named_table, :set, :public, read_concurrency: true])
        _ ->
          @table_name
      end

    :ets.insert(table, {:system_paused, false})

    {:ok, %{}}
  end

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

  def child_spec(opts) do
    %{
      id: __MODULE__,
      start: {__MODULE__, :start_link, [opts]},
      type: :worker,
      restart: :permanent
    }
  end
end
