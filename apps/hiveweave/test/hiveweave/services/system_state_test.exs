defmodule HiveWeave.Services.SystemStateTest do
  use ExUnit.Case

  alias HiveWeave.Services.SystemState

  setup do
    SystemState.start_link([])
    SystemState.resume()
    :ok
  end

  describe "pause/resume" do
    test "starts as not paused" do
      refute SystemState.paused?()
    end

    test "pause sets state to paused" do
      SystemState.pause()
      assert SystemState.paused?()
    end

    test "resume clears paused state" do
      SystemState.pause()
      assert SystemState.paused?()
      SystemState.resume()
      refute SystemState.paused?()
    end
  end
end
