defmodule HiveWeave.LLM.Retry do
  @moduledoc """
  Retry logic for LLM API calls.

  Handles exponential backoff with jitter and Retry-After header parsing.
  """

  @max_retries 2
  @base_delay_ms 500
  @max_delay_ms 10_000

  @retryable_statuses MapSet.new([429, 503, 504, 529])

  def with_retry(fun) do
    do_retry(fun, 0)
  end

  defp do_retry(fun, attempt) do
    case fun.() do
      {:ok, _} = result ->
        result

      {:error, {:http_error, status, headers}} when status == 429 or status == 503 or status == 504 ->
        if attempt < @max_retries do
          delay = calculate_delay(attempt, headers)
          Process.sleep(delay)
          do_retry(fun, attempt + 1)
        else
          {:error, :max_retries_exceeded}
        end

      {:error, _reason} = error ->
        if attempt < @max_retries and retryable_error?(:other) do
          Process.sleep(calculate_delay(attempt, %{}))
          do_retry(fun, attempt + 1)
        else
          error
        end
    end
  end

  defp calculate_delay(attempt, headers) do
    case parse_retry_after(headers) do
      nil ->
        backoff = trunc(:math.pow(2, attempt) * @base_delay_ms)
        jitter = :rand.uniform(100)
        min(backoff + jitter, @max_delay_ms)
      ms -> ms
    end
  end

  defp parse_retry_after(headers) do
    case Map.get(headers, "retry-after-ms") || Map.get(headers, "retry-after") do
      nil -> nil
      val ->
        case Integer.parse(val) do
          {ms, ""} -> ms
          _ -> nil
        end
    end
  end

  defp retryable_error?(_), do: true

  def retryable_status?(status), do: MapSet.member?(@retryable_statuses, status)
end
