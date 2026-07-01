defmodule HiveWeave.ConversationStoreTest do
  use ExUnit.Case

  describe "detect_doom_loop/1" do
    test "returns :ok for empty messages" do
      assert HiveWeave.ConversationStore.detect_doom_loop([]) == :ok
    end

    test "returns :ok for messages with no tool_calls" do
      msgs = [
        %{"role" => "user", "content" => "hello"},
        %{"role" => "assistant", "content" => "hi"}
      ]

      assert HiveWeave.ConversationStore.detect_doom_loop(msgs) == :ok
    end

    test "returns :ok for fewer than 3 identical tool calls" do
      msgs = [
        %{"role" => "assistant", "tool_calls" => [
          %{"function" => %{"name" => "read_file", "arguments" => ~s|{"filePath":"a.txt"}|}}
        ]},
        %{"role" => "tool", "tool_call_id" => "1", "content" => "result"},
        %{"role" => "assistant", "tool_calls" => [
          %{"function" => %{"name" => "read_file", "arguments" => ~s|{"filePath":"b.txt"}|}}
        ]}
      ]

      assert HiveWeave.ConversationStore.detect_doom_loop(msgs) == :ok
    end

    test "returns :ok for 3 different tool calls" do
      msgs = [
        %{"role" => "assistant", "tool_calls" => [
          %{"function" => %{"name" => "read_file", "arguments" => ~s|{"filePath":"a.txt"}|}}
        ]},
        %{"role" => "tool", "tool_call_id" => "1", "content" => "result"},
        %{"role" => "assistant", "tool_calls" => [
          %{"function" => %{"name" => "list_files", "arguments" => ~s|{"path":"."}|}}
        ]},
        %{"role" => "tool", "tool_call_id" => "2", "content" => "result"},
        %{"role" => "assistant", "tool_calls" => [
          %{"function" => %{"name" => "grep", "arguments" => ~s|{"pattern":"hello"}|}}
        ]}
      ]

      assert HiveWeave.ConversationStore.detect_doom_loop(msgs) == :ok
    end

    test "returns :ok for 3 calls with same name but different args" do
      msgs = [
        %{"role" => "assistant", "tool_calls" => [
          %{"function" => %{"name" => "read_file", "arguments" => ~s|{"filePath":"a.txt"}|}}
        ]},
        %{"role" => "tool", "tool_call_id" => "1", "content" => "result"},
        %{"role" => "assistant", "tool_calls" => [
          %{"function" => %{"name" => "read_file", "arguments" => ~s|{"filePath":"a.txt"}|}}
        ]},
        %{"role" => "tool", "tool_call_id" => "2", "content" => "result"},
        %{"role" => "assistant", "tool_calls" => [
          %{"function" => %{"name" => "read_file", "arguments" => ~s|{"filePath":"c.txt"}|}}
        ]}
      ]

      assert HiveWeave.ConversationStore.detect_doom_loop(msgs) == :ok
    end

    test "detects doom loop: 3 consecutive identical tool calls" do
      msgs = [
        %{"role" => "assistant", "tool_calls" => [
          %{"function" => %{"name" => "bash", "arguments" => ~s|{"command":"ls"}|}}
        ]},
        %{"role" => "tool", "tool_call_id" => "1", "content" => "ok"},
        %{"role" => "assistant", "tool_calls" => [
          %{"function" => %{"name" => "bash", "arguments" => ~s|{"command":"ls"}|}}
        ]},
        %{"role" => "tool", "tool_call_id" => "2", "content" => "ok"},
        %{"role" => "assistant", "tool_calls" => [
          %{"function" => %{"name" => "bash", "arguments" => ~s|{"command":"ls"}|}}
        ]}
      ]

      assert {:doom_loop, "bash"} = HiveWeave.ConversationStore.detect_doom_loop(msgs)
    end

    test "detects doom loop even with extra non-tool messages in between" do
      msgs = [
        %{"role" => "assistant", "tool_calls" => [
          %{"function" => %{"name" => "bash", "arguments" => ~s|{"command":"x"}|}}
        ]},
        %{"role" => "tool", "tool_call_id" => "1", "content" => "ok"},
        %{"role" => "user", "content" => "please continue"},
        %{"role" => "assistant", "tool_calls" => [
          %{"function" => %{"name" => "bash", "arguments" => ~s|{"command":"x"}|}}
        ]},
        %{"role" => "tool", "tool_call_id" => "2", "content" => "ok"},
        %{"role" => "assistant", "content" => "let me try again"},
        %{"role" => "assistant", "tool_calls" => [
          %{"function" => %{"name" => "bash", "arguments" => ~s|{"command":"x"}|}}
        ]}
      ]

      assert {:doom_loop, "bash"} = HiveWeave.ConversationStore.detect_doom_loop(msgs)
    end

    test "only counts last 3 tool calls (not all tool calls in history)" do
      msgs = [
        %{"role" => "assistant", "tool_calls" => [
          %{"function" => %{"name" => "old_tool", "arguments" => ~s|{"a":1}|}}
        ]},
        %{"role" => "tool", "tool_call_id" => "0", "content" => "ok"},
        %{"role" => "assistant", "tool_calls" => [
          %{"function" => %{"name" => "bash", "arguments" => ~s|{"command":"x"}|}}
        ]},
        %{"role" => "tool", "tool_call_id" => "1", "content" => "ok"},
        %{"role" => "assistant", "tool_calls" => [
          %{"function" => %{"name" => "bash", "arguments" => ~s|{"command":"x"}|}}
        ]},
        %{"role" => "tool", "tool_call_id" => "2", "content" => "ok"},
        %{"role" => "assistant", "tool_calls" => [
          %{"function" => %{"name" => "bash", "arguments" => ~s|{"command":"x"}|}}
        ]}
      ]

      assert {:doom_loop, "bash"} = HiveWeave.ConversationStore.detect_doom_loop(msgs)
    end

    test "returns :ok when the 3rd last call breaks the streak" do
      msgs = [
        %{"role" => "assistant", "tool_calls" => [
          %{"function" => %{"name" => "read_file", "arguments" => ~s|{"filePath":"x"}|}}
        ]},
        %{"role" => "tool", "tool_call_id" => "1", "content" => "ok"},
        %{"role" => "assistant", "tool_calls" => [
          %{"function" => %{"name" => "bash", "arguments" => ~s|{"command":"ls"}|}}
        ]},
        %{"role" => "tool", "tool_call_id" => "2", "content" => "ok"},
        %{"role" => "assistant", "tool_calls" => [
          %{"function" => %{"name" => "bash", "arguments" => ~s|{"command":"ls"}|}}
        ]},
        %{"role" => "tool", "tool_call_id" => "3", "content" => "ok"},
        %{"role" => "assistant", "tool_calls" => [
          %{"function" => %{"name" => "bash", "arguments" => ~s|{"command":"ls"}|}}
        ]},
        %{"role" => "tool", "tool_call_id" => "4", "content" => "ok"},
        %{"role" => "assistant", "tool_calls" => [
          %{"function" => %{"name" => "bash", "arguments" => ~s|{"command":"ls"}|}}
        ]}
      ]

      assert {:doom_loop, "bash"} = HiveWeave.ConversationStore.detect_doom_loop(msgs)
    end

    test "single message with multiple tool_calls where one repeats 3x triggers doom loop" do
      msgs = [
        %{"role" => "assistant", "tool_calls" => [
          %{"function" => %{"name" => "read_file", "arguments" => ~s|{"filePath":"a.txt"}|}},
          %{"function" => %{"name" => "bash", "arguments" => ~s|{"command":"x"}|}},
          %{"function" => %{"name" => "bash", "arguments" => ~s|{"command":"x"}|}},
          %{"function" => %{"name" => "bash", "arguments" => ~s|{"command":"x"}|}}
        ]}
      ]

      assert {:doom_loop, "bash"} = HiveWeave.ConversationStore.detect_doom_loop(msgs)
    end
  end
end
