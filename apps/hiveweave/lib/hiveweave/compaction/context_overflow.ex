defmodule HiveWeave.Compaction.ContextOverflow do
  @moduledoc "Detect context overflow from LLM provider error messages."

  @patterns [
    ~r/prompt is too long/i,
    ~r/input is too long for requested model/i,
    ~r/exceeds the context window/i,
    ~r/input token count.*exceeds the maximum/i,
    ~r/maximum prompt length is \d+/i,
    ~r/reduce the length of the messages/i,
    ~r/maximum context length is \d+ tokens/i,
    ~r/exceeds the limit of \d+/i,
    ~r/exceeds the available context size/i,
    ~r/greater than the context length/i,
    ~r/exceeded model token limit/i,
    ~r/context_length_exceeded/i,
    ~r/request entity too large/i,
    ~r/context length is only \d+ tokens/i,
    ~r/input length.*exceeds.*context length/i,
    ~r/too large for model with \d+ maximum context length/i,
    ~r/^4(00|13)\s*(status code)?\s*\(no body\)/i,
  ]

  @doc """
  Returns true if the error message indicates a context overflow.
  """
  def context_overflow?(message) when is_binary(message) do
    Enum.any?(@patterns, &Regex.match?(&1, message))
  end

  def context_overflow?(_), do: false
end
