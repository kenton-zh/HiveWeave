defmodule HiveWeave.TokenUtils do
  @moduledoc """
  Token estimation and budget calculation utilities.

  Uses char-ratio heuristic:
  - English: ~4 chars per token
  - CJK: ~1.5 chars per token
  """

  # Constants for context window management
  @compaction_buffer 20_000
  @preserve_recent_min 4_000
  @preserve_recent_max 16_000
  @default_tail_turns 20
  @tool_output_max_chars 50_000
  @tool_output_truncate_threshold 30_000

  @doc """
  Estimate token count for a string.
  """
  def estimate_tokens(text) when is_binary(text) do
    cjk_count = count_cjk(text)
    ascii_count = byte_size(text) - cjk_count * 3
    div(cjk_count, 3) + div(ascii_count, 4)
  end

  def estimate_tokens(_), do: 0

  defp count_cjk(text) do
    # Simplified CJK detection
    text
    |> String.to_charlist()
    |> Enum.count(fn c -> c >= 0x4E00 and c <= 0x9FFF end)
  end

  @doc """
  Calculate the history budget based on context window.
  """
  def calculate_history_budget(context_window, output_reserve \\ @compaction_buffer) do
    max(context_window - output_reserve, @preserve_recent_min)
  end

  @doc """
  Calculate usable context for input.
  """
  def calculate_usable_context(context_window) do
    context_window - @compaction_buffer
  end

  @doc """
  Calculate the budget for preserving recent messages.
  """
  def calculate_preserve_recent_budget do
    @preserve_recent_max
  end

  @doc """
  Truncate tool output if it exceeds the threshold.
  """
  def truncate_tool_output(text, max_chars \\ @tool_output_max_chars)
  def truncate_tool_output(text, max_chars) when is_binary(text) do
    if String.length(text) > max_chars do
      String.slice(text, 0, max_chars) <> "\n... [truncated, original length: #{String.length(text)}]"
    else
      text
    end
  end
  def truncate_tool_output(other, _max_chars), do: to_string(other)

  @doc """
  Compute a hash for prefix caching identification.
  """
  def compute_prefix_hash(content) when is_binary(content) do
    :crypto.hash(:sha256, content) |> Base.encode16(case: :lower) |> String.slice(0, 16)
  end

  def get_compaction_buffer, do: @compaction_buffer
  def get_preserve_recent_min, do: @preserve_recent_min
  def get_preserve_recent_max, do: @preserve_recent_max
  def get_default_tail_turns, do: @default_tail_turns
  def get_tool_output_max_chars, do: @tool_output_max_chars
  def get_tool_output_truncate_threshold, do: @tool_output_truncate_threshold

  # ── Smart tool output truncation ─────────────────────────────

  @tool_output_max_lines 2000
  @tool_output_max_bytes 50_000

  @doc """
  Smart truncation of tool output. If output exceeds limits, saves to temp file
  and returns a bounded preview (head + tail) with a file path hint.
  Pattern mirrored from OpenCode's ToolOutputStore.
  """
  def truncate_tool_output_full(output) when is_binary(output) do
    lines = String.split(output, "\n")
    bytes = byte_size(output)

    if length(lines) > @tool_output_max_lines or bytes > @tool_output_max_bytes do
      file_path = save_tool_output(output)

      head_lines = Enum.take(lines, 20)
      tail_lines = if length(lines) > 20, do: Enum.take(lines, -5), else: []

      marker = "\n\n... [output truncated: #{length(lines)} lines, #{bytes} bytes. Full output saved to #{file_path}] ...\n\n"

      (head_lines ++ [marker] ++ tail_lines)
      |> Enum.join("\n")
    else
      output
    end
  end

  @doc """
  Estimate total tokens for a list of messages.
  """
  def estimate_tokens_for_messages(messages) when is_list(messages) do
    Enum.reduce(messages, 0, fn msg, acc ->
      content_tokens = estimate_tokens(msg["content"] || "")
      tool_tokens = if Map.has_key?(msg, "tool_calls") do
        (msg["tool_calls"] || [])
        |> Enum.reduce(0, fn tc, sum ->
          sum + estimate_tokens(tc["function"]["arguments"] || "")
        end)
      else
        0
      end
      acc + content_tokens + tool_tokens
    end)
  end

  def estimate_tokens_for_messages(_), do: 0

  defp save_tool_output(output) do
    tmp_dir = Path.join(System.tmp_dir!(), "hiveweave_tool_output")
    File.mkdir_p!(tmp_dir)

    filename = "tool_#{System.system_time(:millisecond)}_#{:rand.uniform(9999)}.txt"
    full_path = Path.join(tmp_dir, filename)

    File.write!(full_path, output)
    full_path
  end

  @doc """
  Clean up tool output files older than 7 days.
  """
  def cleanup_tool_outputs do
    tmp_dir = Path.join(System.tmp_dir!(), "hiveweave_tool_output")
    if File.dir?(tmp_dir) do
      case File.ls(tmp_dir) do
        {:ok, files} ->
          Enum.each(files, fn f ->
            full = Path.join(tmp_dir, f)
            case File.stat(full) do
              {:ok, %{mtime: mtime}} ->
                mtime_dt = NaiveDateTime.from_erl!(mtime)
                age_seconds = NaiveDateTime.diff(NaiveDateTime.utc_now(), mtime_dt, :second)
                if age_seconds > 7 * 86400, do: File.rm(full)
              _ -> :ok
            end
          end)
        _ -> :ok
      end
    end
    :ok
  end
end
