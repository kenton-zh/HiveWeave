defmodule HiveWeave.TokenUtilsTest do
  use ExUnit.Case

  alias HiveWeave.TokenUtils

  test "estimate_tokens for empty string" do
    assert TokenUtils.estimate_tokens("") == 0
  end

  test "estimate_tokens for ASCII text" do
    text = String.duplicate("a", 100)
    assert TokenUtils.estimate_tokens(text) == 25
  end

  test "estimate_tokens for non-string returns 0" do
    assert TokenUtils.estimate_tokens(nil) == 0
    assert TokenUtils.estimate_tokens(123) == 0
  end

  test "calculate_history_budget respects context window" do
    budget = TokenUtils.calculate_history_budget(200_000)
    assert budget == 180_000  # 200K - 20K buffer
  end

  test "calculate_history_budget respects minimum" do
    # Even with small context, budget is at least preserve_recent_min
    budget = TokenUtils.calculate_history_budget(5_000)
    assert budget >= TokenUtils.get_preserve_recent_min()
  end

  test "calculate_usable_context" do
    assert TokenUtils.calculate_usable_context(128_000) == 108_000
  end

  test "truncate_tool_output truncates long text" do
    long_text = String.duplicate("a", 60_000)
    result = TokenUtils.truncate_tool_output(long_text)
    assert String.length(result) < 60_000
    assert String.contains?(result, "truncated")
  end

  test "truncate_tool_output keeps short text" do
    short = "short text"
    assert TokenUtils.truncate_tool_output(short) == short
  end

  test "truncate_tool_output respects custom max" do
    text = String.duplicate("x", 200)
    result = TokenUtils.truncate_tool_output(text, 50)
    assert String.length(result) > 50
    assert String.contains?(result, "truncated")
  end

  test "compute_prefix_hash produces 16-char hex" do
    hash = TokenUtils.compute_prefix_hash("test content")
    assert String.length(hash) == 16
    assert Regex.match?(~r/^[0-9a-f]{16}$/, hash)
  end

  test "compute_prefix_hash is deterministic" do
    assert TokenUtils.compute_prefix_hash("test") == TokenUtils.compute_prefix_hash("test")
  end

  test "compute_prefix_hash differs for different inputs" do
    assert TokenUtils.compute_prefix_hash("a") != TokenUtils.compute_prefix_hash("b")
  end

  test "constants are exposed" do
    assert TokenUtils.get_compaction_buffer() == 20_000
    assert TokenUtils.get_preserve_recent_min() == 4_000
    assert TokenUtils.get_preserve_recent_max() == 16_000
    assert TokenUtils.get_default_tail_turns() == 20
  end

  describe "truncate_tool_output_full/1" do
    test "returns output unchanged if under limits" do
      short = "Hello World"
      assert TokenUtils.truncate_tool_output_full(short) == "Hello World"
    end

    test "truncates output that exceeds line limit" do
      lines = Enum.map(1..2500, fn i -> "line #{i}" end)
      long = Enum.join(lines, "\n")
      result = TokenUtils.truncate_tool_output_full(long)

      assert String.contains?(result, "truncated")
      assert String.contains?(result, "hiveweave_tool_output")
      assert String.contains?(result, "line 1")
      assert String.contains?(result, "line 20")
      assert String.contains?(result, "... [output truncated")
    end

    test "truncates output that exceeds byte limit" do
      chunk = String.duplicate("A", 1000)
      big = chunk <> "\n" <> chunk <> "\n" <> chunk
      very_big = String.duplicate(big, 20)
      result = TokenUtils.truncate_tool_output_full(very_big)

      assert String.contains?(result, "truncated")
      assert String.contains?(result, "hiveweave_tool_output")
    end

    test "saves output to temp file when truncated" do
      lines = Enum.map(1..3000, fn i -> "content line #{i}" end)
      long = Enum.join(lines, "\n")
      result = TokenUtils.truncate_tool_output_full(long)

      [_, path] = Regex.run(~r/Full output saved to (.+?)\] /, result)
      assert File.exists?(path), "Expected temp file #{path} to exist"
    end

    test "handles empty string" do
      assert TokenUtils.truncate_tool_output_full("") == ""
    end

    test "raises FunctionClauseError for nil (only binary accepted)" do
      assert_raise FunctionClauseError, fn ->
        TokenUtils.truncate_tool_output_full(nil)
      end
    end

    test "preserves head and tail lines in preview" do
      lines = Enum.map(1..2500, fn i -> "line_#{i}" end)
      long = Enum.join(lines, "\n")
      result = TokenUtils.truncate_tool_output_full(long)

      assert String.contains?(result, "line_1")
      assert String.contains?(result, "line_20")
      assert String.contains?(result, "line_2496")
      assert String.contains?(result, "line_2500")

      refute String.contains?(result, "line_1000")
    end
  end

  describe "cleanup_tool_outputs/0" do
    test "runs without error (no temp files to clean)" do
      assert TokenUtils.cleanup_tool_outputs() == :ok
    end

    test "runs without error even when temp dir does not exist" do
      # cleanup_tool_outputs checks File.dir? first, so no-op if dir missing
      assert TokenUtils.cleanup_tool_outputs() == :ok
    end
  end
end
