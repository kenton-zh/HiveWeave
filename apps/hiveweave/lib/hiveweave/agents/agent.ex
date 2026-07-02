defmodule HiveWeave.Agents.Agent do
  @moduledoc """
  Agent GenServer - represents a single AI agent in the org.

  State machine:
    :idle -> :processing (on chat message)
    :processing -> :idle (on LLM done)

  Future-proofed for Kairo-style office view (v2):
    - position: tile coordinates (nil in v1.5)
    - target: target agent/location (nil in v1.5)
    - face: sprite direction
    - action: current action (in v1.5, mirrors status)
  """
  use GenServer

  require Logger

  alias HiveWeave.Agents.Agent

  defstruct [
    :id,
    :project_id,
    :name,
    :role,
    :permission_type,
    :model_id,
    :llm_task,
    :safety_timer,
    status: :idle,
    # Kairo预留字段 (v1.5 永远 nil)
    position: nil,
    target: nil,
    face: :down,
    action: :idle,
    # 工作状态
    current_job: nil,
    last_heartbeat: nil,
    # 配置
    config: %{}
  ]

  def start_link(agent_config) do
    GenServer.start_link(__MODULE__, agent_config, name: name(agent_config.project_id, agent_config.id))
  end

  def name(project_id, agent_id) do
    :"agent_#{project_id}_#{agent_id}"
  end

  @doc """
  Returns a list of {agent_id, project_id} tuples for all agents currently
  in :processing state. Used by the lobby channel to populate the initial
  processing list on WebSocket (re)connect.
  """
  def list_processing_agents do
    # Agents are registered as :"agent_<project_id>_<agent_id>"
    # but supervisors are :"agent_supervisor_<project_id>" — exclude those.
    Process.list()
    |> Enum.filter(fn pid ->
      case Process.info(pid, :registered_name) do
        {:registered_name, name} when is_atom(name) ->
          name_str = Atom.to_string(name)
          String.starts_with?(name_str, "agent_") and
            not String.starts_with?(name_str, "agent_supervisor_")
        _ -> false
      end
    end)
    |> Enum.flat_map(fn pid ->
      try do
        state = GenServer.call(pid, :get_state, 1_000)
        if state.status == :processing do
          [{state.id, state.project_id}]
        else
          []
        end
      catch
        :exit, _ -> []
      end
    end)
  end

  @impl true
  def init(agent_config) do
    # Register in agent registry (via ProjectSupervisor / AgentSupervisor)
    state = %Agent{
      id: agent_config.id,
      project_id: agent_config.project_id,
      name: agent_config.name,
      role: agent_config.role,
      permission_type: agent_config.permission_type || "executor",
      model_id: agent_config.model_id,
      config: Map.get(agent_config, :config) || %{},
      last_heartbeat: System.system_time(:millisecond)
    }

    # Preload conversation history asynchronously (warm cache)
    Task.start(fn ->
      try do
        HiveWeave.ConversationStore.get_history(agent_config.id, agent_config.project_id, nil)
      rescue
        _ -> :ok
      end
    end)

    {:ok, state}
  end

  @doc """
  Send a chat message to this agent. Returns :ok or {:error, :busy}.
  """
  def chat(agent_pid, message, opts \\ []) do
    GenServer.call(agent_pid, {:chat, message, opts}, 30_000)
  end

  @doc """
  Cancel the current processing job.
  """
  def cancel(agent_pid) do
    GenServer.cast(agent_pid, :cancel)
  end

  @doc """
  Get the current state of this agent.
  """
  def get_state(agent_pid) do
    GenServer.call(agent_pid, :get_state)
  end

  @doc """
  Trigger a subordinate agent (executor) to process pending tasks/messages.
  This is called after dispatch_task or when a rework request is sent.
  Asynchronously spawns a task that loads context and runs LLM.
  """
  def trigger_subordinate(agent_id) do
    spawn(fn ->
      do_trigger(agent_id, :subordinate)
    end)
    :ok
  end

  @doc """
  Trigger a coordinator agent to process pending inbox messages.
  Only runs if the coordinator has unread messages (avoids wasting tokens).
  """
  def trigger_coordinator(agent_id) do
    spawn(fn ->
      do_trigger(agent_id, :coordinator)
    end)
    :ok
  end

  # ── Trigger implementation ──────────────────────────────────

  defp do_trigger(agent_id, trigger_type) do
    # Small delay to let DB writes settle
    Process.sleep(100)

    # Get agent from DB
    agent = HiveWeave.Services.Org.get_agent(agent_id)

    cond do
      agent == nil ->
        Logger.warning("[Trigger] Agent #{agent_id} not found")

      agent.status in ["archived", "dismissed"] ->
        Logger.info("[Trigger] Agent #{agent_id} is #{agent.status}, skipping")

      true ->
        # For coordinator: check if there are pending messages first
        if trigger_type == :coordinator do
          pending = HiveWeave.Services.Inbox.get_pending_messages(agent_id)
          if pending == [] do
            Logger.info("[Trigger] Coordinator #{agent_id} has no pending messages, skipping")
            :skip
          else
            run_triggered_agent(agent, trigger_type)
          end
        else
          # Subordinate: always run (has pending handoffs or rework)
          run_triggered_agent(agent, trigger_type)
        end
    end
  rescue
    e ->
      Logger.error("[Trigger] Error triggering #{agent_id}: #{inspect(e)}\n#{Exception.format_stacktrace(__STACKTRACE__)}")
  end

  defp run_triggered_agent(agent, trigger_type) do
    project_id = agent.project_id
    agent_id = agent.id

    # Check if agent is already processing
    try do
      case GenServer.call(name(project_id, agent_id), :get_state, 5_000) do
        %{status: :processing} ->
          Logger.info("[Trigger] Agent #{agent_id} is already processing, skipping")
          :skip

        _ ->
          # Accept pending handoffs
          HiveWeave.Services.Handoff.accept_pending_handoffs(project_id, agent_id)

          # Build context message from handoffs + inbox
          trigger_result = build_trigger_context(agent, trigger_type)

          case trigger_result do
            nil ->
              Logger.info("[Trigger] Agent #{agent_id} has no context to process, skipping")
              :skip

            {context, inbox_msg_ids} ->
              Logger.info("[Trigger] Triggering agent #{agent.name} (#{trigger_type}) with context: #{String.slice(context, 0, 100)}")

              # Save as background message
              msg_id = Ecto.UUID.generate()
              now_ms = System.system_time(:millisecond)

              HiveWeave.Services.ChatMessage.save_message(%{
                id: msg_id,
                agent_id: agent_id,
                role: "user",
                content: context,
                is_background: true,
                is_read: false,
                is_streaming: false,
                is_context: true,
                created_at: now_ms
              })

              # Call the agent's chat handler directly
              result = GenServer.call(name(project_id, agent_id), {:chat, context, [trigger: true]}, 30_000)

              # Only mark the inbox messages that were included in the context as read.
              # This avoids the race condition where new messages arrive between
              # build_trigger_context and mark_all_read — those new messages must
              # remain unread for the next trigger cycle.
              case result do
                {:error, :busy} ->
                  Logger.warning("[Trigger] Agent #{agent_id} was busy, inbox messages left unread for retry")
                {:error, :paused} ->
                  Logger.warning("[Trigger] Agent #{agent_id} system paused, inbox messages left unread for retry")
                {:error, reason} ->
                  Logger.warning("[Trigger] Agent #{agent_id} trigger failed: #{inspect(reason)}, inbox messages left unread for retry")
                _ ->
                  HiveWeave.Services.Inbox.mark_read_by_ids(agent_id, inbox_msg_ids)
              end
          end
      end
    catch
      :exit, {:noproc, _} ->
        Logger.warning("[Trigger] Agent #{agent_id} GenServer not running, cannot trigger")
      :exit, {:timeout, _} ->
        Logger.warning("[Trigger] Agent #{agent_id} GenServer timed out, may be processing")
    end
  end

  defp build_trigger_context(agent, trigger_type) do
    project_id = agent.project_id
    agent_id = agent.id

    # Get pending handoffs (only those not yet delivered as context)
    pending_handoffs = HiveWeave.Services.Handoff.get_pending_handoffs(project_id, agent_id)
    accepted_handoffs = HiveWeave.Services.Handoff.get_accepted_handoffs(project_id, agent_id)

    # Get pending inbox messages (unread only — mark_all_read is called after each trigger)
    inbox_messages = HiveWeave.Services.Inbox.get_pending_messages(agent_id)

    # Separate rework messages
    {rework_msgs, other_msgs} = Enum.split_with(inbox_messages, fn m ->
      String.contains?(m.message || "", "[REWORK REQUESTED]")
    end)

    # Get unreported handoffs (for coordinator self-check — uses ALL accepted, not just undelivered)
    unreported = HiveWeave.Services.Handoff.get_unreported_accepted_handoffs(project_id, agent_id)

    # Build context blocks
    blocks = []

    # Handoff block (only include handoffs not yet delivered as context)
    {blocks, delivered_handoff_ids} = if pending_handoffs != [] or accepted_handoffs != [] do
      all_handoffs = pending_handoffs ++ accepted_handoffs
      handoff_text = Enum.map(all_handoffs, fn h ->
        from_name = agent_name(h.from_agent_id)
        "  - From: #{from_name}\n    Task: #{h.summary}\n    Status: #{h.status}#{if h.expect_report, do: " (report required)", else: ""}"
      end) |> Enum.join("\n")

      ids = Enum.map(all_handoffs, & &1.id)
      {blocks ++ ["## Pending Tasks (respond in CAVEMAN style)\n#{handoff_text}"], ids}
    else
      {blocks, []}
    end

    # Rework block
    blocks = if rework_msgs != [] do
      rework_text = Enum.map(rework_msgs, fn m ->
        from_name = agent_name(m.from_agent_id)
        "  - From: #{from_name}\n    #{m.message}"
      end) |> Enum.join("\n")

      blocks ++ ["## WORK REJECTED — Rework Required\n#{rework_text}\n\nYou must fix the issues and call report_completion again."]
    else
      blocks
    end

    # Inbox messages block
    blocks = if other_msgs != [] do
      msg_text = Enum.map(other_msgs, fn m ->
        prefix = if m.expect_report, do: "**[REPLY REQUIRED]** ", else: ""
        from_name = agent_name(m.from_agent_id)
        "  - From: #{from_name} (type=#{m.message_type}, priority=#{m.priority})\n    #{prefix}#{m.message}"
      end) |> Enum.join("\n")

      blocks ++ ["## Messages (from other agents — reply in CAVEMAN style, NO pleasantries)\n#{msg_text}"]
    else
      blocks
    end

    # Coordinator: add subordinate logs + self-check
    blocks = if trigger_type == :coordinator do
      children = HiveWeave.Services.Org.get_children(project_id, agent_id)

      # Subordinate logs
      child_logs = Enum.flat_map(children, fn child ->
        logs = HiveWeave.Services.Dispatch.get_subordinate_logs(project_id, child.id, 5)
        Enum.map(logs, fn l -> "  [#{child.name}] [#{l.type}] #{l.summary}" end)
      end)

      blocks = if child_logs != [] do
        blocks ++ ["## Subordinate Work Logs (terse format)\n#{Enum.join(child_logs, "\n")}"]
      else
        blocks
      end

      # Self-check: unreported handoffs (last expression = return value of do-block)
      if unreported != [] do
        blocks ++ ["## IMPORTANT — Report Required\nYou have #{length(unreported)} task(s) with expect_report that haven't been reported up. You MUST call message_superior to report results to your superior."]
      else
        blocks
      end
    else
      blocks
    end

    # If nothing to process, return nil
    if blocks == [] do
      nil
    else
      # Mark handoffs as delivered so they won't be re-injected on subsequent triggers
      if delivered_handoff_ids != [] do
        HiveWeave.Services.Handoff.mark_delivered(project_id, delivered_handoff_ids)
      end

      inbox_msg_ids = Enum.map(inbox_messages, & &1.id)
      {Enum.join(blocks, "\n\n") <> "\n\n---\nProcess the above. Use tools to work on tasks, report results.", inbox_msg_ids}
    end
  end

  # Helper: resolve agent_id to flower name for human-readable context
  defp agent_name(agent_id) do
    case HiveWeave.Services.Org.get_agent(agent_id) do
      %{name: name} when is_binary(name) and name != "" -> name
      _ -> agent_id
    end
  rescue
    _ -> agent_id
  end

  # Server callbacks

  @impl true
  def handle_call({:chat, message, opts}, _from, %{status: :processing} = state) do
    {:reply, {:error, :busy}, state}
  end

  @impl true
  def handle_call({:chat, message, opts}, _from, state) do
    if HiveWeave.Services.SystemState.paused?() do
      {:reply, {:error, :paused}, state}
    else
      parent = self()

      # Emit telemetry
      HiveWeave.Telemetry.agent_chat_start(state.id, "user")

      # Audit log
      HiveWeave.EventAudit.log(state.id, :chat_start, %{message_length: String.length(message || "")})

      # Cancel any previous safety timer to prevent stale timeouts
      if state.safety_timer, do: Process.cancel_timer(state.safety_timer)

      # Spawn the LLM task
      task = Task.Supervisor.async_nolink(HiveWeave.TaskSupervisor, fn ->
        HiveWeave.LLM.Streamer.stream(state, message, opts, parent)
      end)

      # Safety timeout: reset to idle after 5 minutes if task doesn't complete
      timer_ref = Process.send_after(self(), :safety_timeout, 300_000)

      new_state = %{state |
        status: :processing,
        llm_task: task,
        safety_timer: timer_ref,
        current_job: %{message: message, started_at: System.system_time(:millisecond)}
      }

      # Broadcast status change: processing
      broadcast_status(state.project_id, state.id, :processing)

      {:reply, :ok, new_state}
    end
  end

  @impl true
  def handle_call(:get_state, _from, state) do
    {:reply, state, state}
  end

  @impl true
  def handle_cast(:cancel, %{status: :processing, llm_task: task} = state) when not is_nil(task) do
    Logger.info("Agent #{state.id} cancelled by user")
    Task.Supervisor.terminate_child(HiveWeave.TaskSupervisor, task.pid)
    if state.safety_timer, do: Process.cancel_timer(state.safety_timer)

    # Broadcast done event so frontend stops showing streaming indicator
    Phoenix.PubSub.broadcast(
      HiveWeave.PubSub,
      "agent:#{state.id}",
      {:stream_event, %{type: "done", error: "cancelled"}}
    )

    new_state = %{state | status: :idle, llm_task: nil, current_job: nil, safety_timer: nil}
    broadcast_status(state.project_id, state.id, :idle)
    {:noreply, new_state}
  end

  @impl true
  def handle_cast(:cancel, state) do
    {:noreply, state}
  end

  @impl true
  def handle_info({ref, result}, %{llm_task: %Task{ref: ref}} = state) do
    # LLM task completed successfully
    Process.demonitor(ref, [:flush])
    if state.safety_timer, do: Process.cancel_timer(state.safety_timer)

    duration = (System.system_time(:millisecond) - (state.current_job.started_at || 0))
    tokens = extract_tokens(result)

    new_state = %{state |
      status: :idle,
      llm_task: nil,
      safety_timer: nil,
      current_job: nil,
      last_heartbeat: System.system_time(:millisecond)
    }

    # Telemetry + audit
    HiveWeave.Telemetry.agent_chat_done(state.id, duration, tokens)
    HiveWeave.EventAudit.log(state.id, :chat_done, %{duration_ms: duration, tokens: tokens})

    # Broadcast status change
    broadcast_status(state.project_id, state.id, :idle)

    # Self-retrigger: if new inbox messages arrived while we were processing, trigger again
    pending = HiveWeave.Services.Inbox.get_pending_messages(state.id)
    # Also check for unanswered user messages in chat_messages (user sent a
    # message while agent was busy — the message was saved to DB but not processed)
    has_unanswered_user_msgs = HiveWeave.Services.ChatMessage.has_unanswered_user_messages?(state.id)
    if pending != [] or has_unanswered_user_msgs do
      spawn(fn ->
        Process.sleep(500)
        # Use the correct trigger method based on agent role
        if state.config[:role] == "ceo" or state.config[:role] == "hr" do
          HiveWeave.Agents.Agent.trigger_coordinator(state.id)
        else
          HiveWeave.Agents.Agent.trigger_subordinate(state.id)
        end
      end)
    end

    {:noreply, new_state}
  end

  @impl true
  def handle_info({:DOWN, ref, :process, _pid, reason}, %{llm_task: %Task{ref: ref}} = state) do
    # LLM task crashed
    Logger.warning("Agent #{state.id} LLM task crashed: #{inspect(reason)}")

    HiveWeave.Telemetry.llm_stream_fail(state.config[:provider] || "primary", inspect(reason))
    HiveWeave.EventAudit.log(state.id, :llm_fail, %{reason: inspect(reason)})

    if state.safety_timer, do: Process.cancel_timer(state.safety_timer)
    new_state = %{state | status: :idle, llm_task: nil, safety_timer: nil, current_job: nil}
    broadcast_status(state.project_id, state.id, :idle)

    {:noreply, new_state}
  end

  @impl true
  def handle_info(:safety_timeout, %{status: :processing} = state) do
    Logger.warning("Agent #{state.id} safety timeout - force resetting to idle")

    # Kill the LLM task if still running
    if state.llm_task do
      Task.Supervisor.terminate_child(HiveWeave.TaskSupervisor, state.llm_task.pid)
    end

    # Mark streaming messages as done
    Phoenix.PubSub.broadcast(
      HiveWeave.PubSub,
      "agent:#{state.id}",
      {:stream_event, %{type: "done", error: "timeout"}}
    )

    new_state = %{state | status: :idle, llm_task: nil, safety_timer: nil, current_job: nil}
    broadcast_status(state.project_id, state.id, :idle)

    {:noreply, new_state}
  end

  @impl true
  def handle_info(:safety_timeout, state) do
    # Not processing, ignore timeout
    {:noreply, state}
  end

  @impl true
  def handle_info({:force_reset}, state) do
    Logger.info("Agent #{state.id} force reset to idle")

    if state.llm_task do
      Task.Supervisor.terminate_child(HiveWeave.TaskSupervisor, state.llm_task.pid)
    end
    if state.safety_timer, do: Process.cancel_timer(state.safety_timer)

    new_state = %{state | status: :idle, llm_task: nil, safety_timer: nil, current_job: nil}
    broadcast_status(state.project_id, state.id, :idle)

    {:noreply, new_state}
  end

  # ── Blocking-question support ──────────────────────────────
  # The LLM Task process (state.llm_task) may be blocked inside a `receive`
  # in ToolExecutor.execute_question/2, waiting for {:question_answer, qid, ans}.
  # The ExtraController.chat_questions_answer endpoint sends the answer to this
  # GenServer (addressable by its registered name); we forward it to the Task PID.
  @impl true
  def handle_info({:question_answer, question_id, answer}, %{llm_task: %Task{pid: pid}} = state) when is_pid(pid) do
    send(pid, {:question_answer, question_id, answer})
    {:noreply, state}
  end

  @impl true
  def handle_info({:question_answer, _question_id, _answer}, state) do
    # No active LLM task running — nothing is waiting, drop the answer.
    {:noreply, state}
  end

  @impl true
  def handle_info(_msg, state) do
    {:noreply, state}
  end

  @impl true
  def terminate(reason, state) do
    Logger.info("Agent #{state.id} terminating: #{inspect(reason)}")
    HiveWeave.Telemetry.agent_crash(state.id, reason)
    :ok
  end

  # Private helpers

  defp extract_tokens(result) do
    case result do
      {:ok, :completed, text} when is_binary(text) ->
        div(byte_size(text), 4)
      {:ok, :completed} ->
        0
      _ ->
        0
    end
  end

  defp broadcast_status(project_id, agent_id, status) do
    Phoenix.PubSub.broadcast(
      HiveWeave.PubSub,
      "project:#{project_id}",
      {:status_change, agent_id, status}
    )
    Phoenix.PubSub.broadcast(
      HiveWeave.PubSub,
      "agent:#{agent_id}",
      {:status_change, agent_id, status}
    )
    Phoenix.PubSub.broadcast(
      HiveWeave.PubSub,
      "lobby:status",
      {:status_change, agent_id, status}
    )
  end
end
