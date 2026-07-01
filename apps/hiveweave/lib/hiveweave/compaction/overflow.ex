defmodule HiveWeave.Compaction.Overflow do
  @moduledoc """
  Token budget calculation and overflow detection.
  Ported from OpenCode's overflow.ts.
  """

  @compaction_buffer 20_000

  @doc """
  Calculate usable token budget for a model.
  Returns 0 for unbounded/unknown models.
  """
  def usable(model) when is_map(model) do
    context = model[:context_window] || 0
    if context <= 0, do: 0, else: context - @compaction_buffer
  end

  @doc """
  Check if current token usage exceeds usable budget.
  """
  def overflow?(model, tokens_used) when is_map(model) do
    u = usable(model)
    u > 0 and tokens_used >= u
  end

  @doc """
  Calculate budget for history after model switch.
  Returns the new budget if compaction is needed, nil otherwise.
  """
  def check_model_switch(old_context, new_context, current_tokens) do
    cond do
      new_context <= 0 -> nil
      old_context <= 0 ->
        usable = max(0, new_context - @compaction_buffer)
        if current_tokens > usable, do: usable, else: nil
      new_context < old_context ->
        usable = max(0, new_context - @compaction_buffer)
        if current_tokens > usable, do: usable, else: nil
      true -> nil
    end
  end
end
