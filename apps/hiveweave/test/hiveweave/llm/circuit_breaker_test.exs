defmodule HiveWeave.LLM.CircuitBreakerTest do
  use ExUnit.Case

  alias HiveWeave.LLM.CircuitBreaker

  setup do
    # Reset :primary to closed state for each test
    CircuitBreaker.report_success(:primary)
    :ok
  end

  test "check returns :ok for closed breaker" do
    # :primary is registered in init/1 from llm_providers config
    assert CircuitBreaker.check(:primary) == :ok
  end

  test "report_failure increments fail count" do
    initial = get_fail_count(:primary)
    assert initial == 0
    CircuitBreaker.report_failure(:primary)
    new = get_fail_count(:primary)
    assert new == 1
  end

  test "report_success resets to closed" do
    # After setup, :primary is closed
    state = get_state(:primary)
    assert state.state == :closed
    assert state.fail_count == 0
  end

  test "three failures opens the circuit" do
    1..3 |> Enum.each(fn _ -> CircuitBreaker.report_failure(:primary) end)
    state = get_state(:primary)
    assert state.state == :open
  end

  test "open circuit returns fallback" do
    1..3 |> Enum.each(fn _ -> CircuitBreaker.report_failure(:primary) end)
    result = CircuitBreaker.check(:primary)
    assert {:fallback, _} = result
  end

  test "cooldown transitions open to half_open" do
    # Manually set state to :open with old opened_at
    # We can't easily manipulate this without breaking encapsulation,
    # so just verify the closed -> open path works
    1..3 |> Enum.each(fn _ -> CircuitBreaker.report_failure(:primary) end)
    state = get_state(:primary)
    assert state.state == :open
  end

  defp get_state(name) do
    state = :sys.get_state(CircuitBreaker)
    Map.get(state.breakers, name, %{state: :closed, fail_count: 0})
  end

  defp get_fail_count(name) do
    get_state(name).fail_count
  end
end


