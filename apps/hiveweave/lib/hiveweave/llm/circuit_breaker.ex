defmodule HiveWeave.LLM.CircuitBreaker do
  @moduledoc """
  Circuit Breaker for LLM providers.

  Three-state machine: closed -> open -> half_open -> closed
  With probe lock to prevent multiple agents from probing simultaneously in half_open state.

  The probe_owner tracks the caller Agent's PID (not the CircuitBreaker's own PID),
  and is monitored so that if the probing Agent crashes, the probe lock is released
  and the circuit returns to :open for a fresh cooldown.
  """
  use GenServer

  require Logger

  defstruct [
    :provider,
    state: :closed,
    fail_count: 0,
    fail_threshold: 3,
    cooldown_ms: 60_000,
    opened_at: nil,
    fallback: nil,
    probe_owner: nil,
    probe_ref: nil
  ]

  def start_link(_opts) do
    GenServer.start_link(__MODULE__, [], name: __MODULE__)
  end

  @impl true
  def init(_) do
    # Initialize circuit breakers for configured providers
    providers = Application.get_env(:hiveweave, :llm_providers, %{})
    breakers =
      for {name, config} <- providers, into: %{} do
        state = %__MODULE__{
          provider: name,
          fallback: Map.get(config, :fallback),
          fail_threshold: 3,
          cooldown_ms: 60_000
        }
        {name, state}
      end
    {:ok, %{breakers: breakers}}
  end

  @impl true
  def handle_call({:register, name, state}, _from, s) do
    {:reply, :ok, put_in(s, [Access.key!(:breakers), name], state)}
  end

  @impl true
  def handle_call({:check, name}, {caller_pid, _tag} = _from, %{breakers: breakers} = s) do
    case Map.get(breakers, name) do
      nil -> {:reply, :ok, s}
      breaker -> handle_check(breaker, s, caller_pid)
    end
  end

  # Closed: allow all requests
  defp handle_check(%{state: :closed}, s, _caller_pid) do
    {:reply, :ok, s}
  end

  # Open: check cooldown, maybe transition to half_open
  defp handle_check(%{state: :open, opened_at: opened_at, cooldown_ms: cd} = b, s, caller_pid)
       when is_integer(opened_at) do
    now = System.monotonic_time(:millisecond)
    if now - opened_at > cd do
      # Transition to half_open — first caller becomes the probe owner
      ref = Process.monitor(caller_pid)
      new_b = %{b | state: :half_open, probe_owner: caller_pid, probe_ref: ref}
      new_breakers = put_in(s.breakers, [b.provider], new_b)
      {:reply, :ok, %{s | breakers: new_breakers}}
    else
      {:reply, {:fallback, b.fallback}, s}
    end
  end

  # HalfOpen with no active probe — this caller becomes the probe
  defp handle_check(%{state: :half_open, probe_owner: nil} = b, s, caller_pid) do
    ref = Process.monitor(caller_pid)
    new_b = %{b | probe_owner: caller_pid, probe_ref: ref}
    new_breakers = put_in(s.breakers, [b.provider], new_b)
    {:reply, :ok, %{s | breakers: new_breakers}}
  end

  # HalfOpen: the probe owner is allowed through
  defp handle_check(%{state: :half_open, probe_owner: owner} = b, s, caller_pid)
       when owner == caller_pid do
    {:reply, :ok, s}
  end

  # HalfOpen: another agent is already probing — go to fallback
  defp handle_check(%{state: :half_open} = b, s, _caller_pid) do
    {:reply, {:fallback, b.fallback}, s}
  end

  @impl true
  def handle_cast({:success, name}, %{breakers: breakers} = s) do
    case Map.get(breakers, name) do
      nil -> {:noreply, s}
      breaker ->
        # Demonitor if we were monitoring the probe owner
        if breaker.probe_ref, do: Process.demonitor(breaker.probe_ref, [:flush])
        new_b = %{breaker | state: :closed, fail_count: 0, probe_owner: nil, probe_ref: nil}
        if breaker.state != :closed do
          HiveWeave.Telemetry.circuit_close(name)
        end
        {:noreply, put_in(s, [:breakers, name], new_b)}
    end
  end

  @impl true
  def handle_cast({:failure, name}, %{breakers: breakers} = s) do
    case Map.get(breakers, name) do
      nil -> {:noreply, s}
      breaker ->
        # Demonitor if we were monitoring the probe owner
        if breaker.probe_ref, do: Process.demonitor(breaker.probe_ref, [:flush])
        new_b = handle_failure(breaker)
        if new_b.state == :open and breaker.state != :open do
          HiveWeave.Telemetry.circuit_open(name)
        end
        {:noreply, put_in(s, [:breakers, name], new_b)}
    end
  end

  # Probe owner crashed — release the probe lock, return to :open
  @impl true
  def handle_info({:DOWN, ref, :process, pid, _reason}, %{breakers: breakers} = s) do
    new_breakers =
      Map.new(breakers, fn {name, b} ->
        if b.probe_ref == ref and b.probe_owner == pid do
          Logger.warning("Circuit Breaker probe owner #{inspect(pid)} for #{name} crashed, returning to :open")
          {name, %{b | state: :open, probe_owner: nil, probe_ref: nil,
            opened_at: System.monotonic_time(:millisecond)}}
        else
          {name, b}
        end
      end)
    {:noreply, %{s | breakers: new_breakers}}
  end

  @impl true
  def handle_info(_msg, s) do
    {:noreply, s}
  end

  defp handle_failure(%{state: :closed} = b) do
    count = b.fail_count + 1
    if count >= b.fail_threshold do
      %{b | state: :open, fail_count: count, opened_at: System.monotonic_time(:millisecond),
        probe_owner: nil, probe_ref: nil}
    else
      %{b | fail_count: count}
    end
  end

  defp handle_failure(%{state: :half_open} = b) do
    # Probe failed - back to open with fresh cooldown
    %{b | state: :open, opened_at: System.monotonic_time(:millisecond),
      probe_owner: nil, probe_ref: nil}
  end

  defp handle_failure(b), do: b

  # Public API
  def check(name) do
    GenServer.call(__MODULE__, {:check, name}, 5_000)
  end

  def report_success(name) do
    GenServer.cast(__MODULE__, {:success, name})
  end

  def report_failure(name) do
    GenServer.cast(__MODULE__, {:failure, name})
  end
end
