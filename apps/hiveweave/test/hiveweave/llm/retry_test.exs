defmodule HiveWeave.LLM.RetryTest do
  use ExUnit.Case

  alias HiveWeave.LLM.Retry

  test "retryable_status? returns true for retryable codes" do
    assert Retry.retryable_status?(429)
    assert Retry.retryable_status?(503)
    assert Retry.retryable_status?(504)
    assert Retry.retryable_status?(529)
  end

  test "retryable_status? returns false for non-retryable codes" do
    refute Retry.retryable_status?(200)
    refute Retry.retryable_status?(400)
    refute Retry.retryable_status?(401)
    refute Retry.retryable_status?(500)
  end

  test "with_retry returns success on first try" do
    result = Retry.with_retry(fn -> {:ok, :done} end)
    assert result == {:ok, :done}
  end

  test "with_retry returns error after max retries" do
    counter = :counters.new(1, [])
    fun = fn ->
      :counters.add(counter, 1, 1)
      {:error, :failed}
    end

    result = Retry.with_retry(fun)
    assert {:error, _} = result
    # Should have retried at least once
    assert :counters.get(counter, 1) > 1
  end
end
