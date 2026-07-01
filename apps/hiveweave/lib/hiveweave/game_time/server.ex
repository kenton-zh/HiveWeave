defmodule HiveWeave.GameTime.Server do
  @moduledoc """
  Per-project simulated game time with DB-persisted alarms.

  - 15 real minutes per game day (REAL_SECONDS_PER_GAME_DAY = 900)
  - 5-second tick broadcasts time and fires due alarms
  - Alarms persisted to scheduled_alarms table in per-project DB
  - On init, loads all unfired alarms from DB so they survive restarts
  """

  use GenServer

  alias HiveWeave.Repo.ProjectFactory

  require Logger

  @real_seconds_per_game_day 900
  @tick_interval_ms 5_000
  @stall_check_interval_ticks 12  # 12 * 5s = 60s
  @stall_processing_threshold_ms 5 * 60 * 1000   # 5 min processing = stalled
  @stall_idle_threshold_ms 10 * 60 * 1000        # 10 min idle no heartbeat = stalled

  defstruct [
    :project_id,
    :current_game_seconds,
    :real_started_at,
    :alarms,
    :last_tick_at,
    tick_count: 0
  ]

  # Client API

  def start_link(project_id) do
    GenServer.start_link(__MODULE__, project_id, name: name(project_id))
  end

  def name(project_id) do
    :"game_time_#{project_id}"
  end

  def get_current_time(project_id) do
    GenServer.call(name(project_id), :get_current_time, 5_000)
  catch
    :exit, _ -> 0
  end

  def schedule_alarm(project_id, alarm) do
    GenServer.call(name(project_id), {:schedule_alarm, alarm})
  rescue
    _ -> :ok
  end

  def cancel_alarm(project_id, alarm_id) do
    GenServer.call(name(project_id), {:cancel_alarm, alarm_id})
  rescue
    _ -> :ok
  end

  # Server callbacks

  @impl true
  def init(project_id) do
    # Load unfired alarms from DB so they survive server restarts
    db_alarms = load_alarms_from_db(project_id)

    if db_alarms != [] do
      Logger.info("[GameTime] Loaded #{length(db_alarms)} pending alarm(s) from DB for project #{project_id}")
    end

    state = %__MODULE__{
      project_id: project_id,
      current_game_seconds: 0,
      real_started_at: System.system_time(:second),
      alarms: db_alarms,
      last_tick_at: System.system_time(:millisecond)
    }

    schedule_tick()
    {:ok, state}
  end

  @impl true
  def handle_call(:get_current_time, _from, state) do
    {:reply, state.current_game_seconds, state}
  end

  @impl true
  def handle_call({:schedule_alarm, alarm}, _from, state) do
    # Normalize alarm to atom-key map with required fields
    alarm = normalize_alarm(alarm)

    # Persist to DB
    persist_alarm(state.project_id, alarm)

    new_alarms = state.alarms ++ [alarm]
    {:reply, {:ok, alarm.id}, %{state | alarms: new_alarms}}
  end

  @impl true
  def handle_call({:cancel_alarm, alarm_id}, _from, state) do
    # Mark as cancelled in DB
    cancel_alarm_in_db(state.project_id, alarm_id)

    # Remove from memory
    new_alarms = Enum.reject(state.alarms, fn a -> a.id == alarm_id end)
    {:reply, :ok, %{state | alarms: new_alarms}}
  end

  @impl true
  def handle_info(:tick, state) do
    now = System.system_time(:second)
    elapsed_real = now - state.real_started_at
    new_game_time = div(elapsed_real * 86_400, @real_seconds_per_game_day)

    # Check for due alarms
    due_alarms = Enum.filter(state.alarms, fn alarm ->
      alarm.fire_at_game_seconds <= new_game_time and not alarm.fired
    end)

    # Fire due alarms and mark as fired in DB
    Enum.each(due_alarms, fn alarm ->
      fire_alarm(alarm)
      mark_alarm_fired(state.project_id, alarm.id)
    end)

    # Remove fired alarms from memory
    updated_alarms = Enum.reject(state.alarms, fn alarm ->
      alarm in due_alarms
    end)

    # Broadcast tick
    Phoenix.PubSub.broadcast(
      HiveWeave.PubSub,
      "project:#{state.project_id}",
      {:game_time_tick, new_game_time}
    )

    new_state = %{state |
      current_game_seconds: new_game_time,
      alarms: updated_alarms,
      last_tick_at: System.system_time(:millisecond),
      tick_count: state.tick_count + 1
    }

    # Periodic stall detection (every 60s)
    if rem(new_state.tick_count, @stall_check_interval_ticks) == 0 do
      spawn(fn -> check_stalled_agents(new_state.project_id) end)
    end

    schedule_tick()
    {:noreply, new_state}
  end

  defp schedule_tick do
    Process.send_after(self(), :tick, @tick_interval_ms)
  end

  defp fire_alarm(alarm) do
    Logger.info("[GameTime] Firing alarm: #{alarm.purpose} for agent #{alarm.to_agent_id}")

    # Broadcast to agent channel
    Phoenix.PubSub.broadcast(
      HiveWeave.PubSub,
      "agent:#{alarm.to_agent_id}",
      {:alarm_fired, alarm}
    )

    # Broadcast to project channel (for frontend animation)
    Phoenix.PubSub.broadcast(
      HiveWeave.PubSub,
      "project:#{alarm.project_id || alarm[:project_id]}",
      {:alarm_fired, alarm}
    )

    # Auto-trigger the agent — send inbox message and trigger
    # This wakes up the agent to process the alarm
    if alarm.to_agent_id do
      # Send an inbox message about the alarm
      alarm_msg = "[ALARM] #{alarm.purpose}"
      from_id = alarm.from_agent_id || alarm.to_agent_id

      HiveWeave.Services.Inbox.send_message(
        from_id,
        alarm.to_agent_id,
        "alarm",
        alarm_msg,
        %{priority: "urgent"}
      )

      # Trigger the agent to process the alarm
      HiveWeave.Agents.Agent.trigger_subordinate(alarm.to_agent_id)
    end
  end

  # ── DB persistence helpers ──────────────────────────────────

  defp load_alarms_from_db(project_id) do
    sql = "SELECT id, project_id, from_agent_id, to_agent_id, purpose, fire_at_game_seconds, status, fired, fired_at, created_at FROM scheduled_alarms WHERE fired = 0 AND status = 'pending' ORDER BY fire_at_game_seconds ASC"

    case ProjectFactory.query(project_id, sql, []) do
      {:ok, r} ->
        Enum.map(r.rows, fn row ->
          [id, proj, from_agent, to_agent, purpose, fire_at, _status, fired, _fired_at, _created] = row
          %{
            id: id,
            project_id: proj,
            from_agent_id: from_agent,
            to_agent_id: to_agent,
            purpose: purpose,
            fire_at_game_seconds: fire_at || 0,
            fired: false
          }
        end)

      {:error, reason} ->
        Logger.warning("[GameTime] Failed to load alarms from DB: #{inspect(reason)}")
        []
    end
  end

  defp persist_alarm(project_id, alarm) do
    now_ms = System.system_time(:millisecond)

    sql = "INSERT INTO scheduled_alarms (id, project_id, from_agent_id, to_agent_id, purpose, fire_at_game_seconds, status, fired, created_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?)"

    case ProjectFactory.query(project_id, sql, [
           alarm.id,
           project_id,
           alarm.from_agent_id,
           alarm.to_agent_id,
           alarm.purpose || "",
           alarm.fire_at_game_seconds,
           now_ms
         ]) do
      {:ok, _} ->
        Logger.info("[GameTime] Persisted alarm #{alarm.id} (#{alarm.purpose}) to DB for project #{project_id}")

      {:error, reason} ->
        Logger.error("[GameTime] Failed to persist alarm to DB: #{inspect(reason)}")
    end
  end

  defp mark_alarm_fired(project_id, alarm_id) do
    now_ms = System.system_time(:millisecond)

    sql = "UPDATE scheduled_alarms SET fired = 1, fired_at = ?, status = 'fired' WHERE id = ?"

    case ProjectFactory.query(project_id, sql, [now_ms, alarm_id]) do
      {:ok, _} ->
        :ok

      {:error, reason} ->
        Logger.warning("[GameTime] Failed to mark alarm #{alarm_id} as fired: #{inspect(reason)}")
    end
  end

  defp cancel_alarm_in_db(project_id, alarm_id) do
    sql = "UPDATE scheduled_alarms SET status = 'cancelled' WHERE id = ?"

    case ProjectFactory.query(project_id, sql, [alarm_id]) do
      {:ok, _} -> :ok
      {:error, _} -> :ok
    end
  end

  # ── Stall detection ────────────────────────────────────────

  defp check_stalled_agents(project_id) do
    agents = HiveWeave.Services.Org.list_agents(project_id)
    now_ms = System.system_time(:millisecond)

    Enum.each(agents, fn agent ->
      if agent.status == "active" do
        case check_agent_liveness(project_id, agent.id, now_ms) do
          {:stalled, reason} ->
            escalate_stall(project_id, agent, reason)
          :ok ->
            :ok
        end
      end
    end)
  rescue
    e ->
      Logger.warning("[GameTime] Stall check failed for project #{project_id}: #{inspect(e)}")
  end

  defp check_agent_liveness(project_id, agent_id, now_ms) do
    name = HiveWeave.Agents.Agent.name(project_id, agent_id)

    try do
      state = GenServer.call(name, :get_state, 3_000)

      cond do
        state.status == :processing and state.current_job != nil ->
          job_duration = now_ms - (state.current_job.started_at || now_ms)
          if job_duration > @stall_processing_threshold_ms do
            {:stalled, "processing for #{div(job_duration, 1000)}s"}
          else
            :ok
          end

        true ->
          idle_duration = now_ms - (state.last_heartbeat || now_ms)
          if idle_duration > @stall_idle_threshold_ms do
            {:stalled, "idle for #{div(idle_duration, 60_000)}min"}
          else
            :ok
          end
      end
    catch
      :exit, {:timeout, _} ->
        {:stalled, "GenServer timeout (3s)"}
      :exit, {:noproc, _} ->
        :ok  # Not started yet, not a stall
    end
  end

  defp escalate_stall(project_id, agent, reason) do
    Logger.warning("[GameTime] Agent #{agent.name} (#{agent.id}) stalled: #{reason}")

    if agent.parent_id do
      # Send inbox message to superior
      HiveWeave.Services.Inbox.send_message(
        agent.id,
        agent.parent_id,
        "escalation",
        "[ESCALATION] Your subordinate #{agent.name} appears stalled: #{reason}. Please check on them or reassign their task.",
        %{priority: "high"}
      )

      # Trigger the superior to process the escalation
      HiveWeave.Agents.Agent.trigger_coordinator(agent.parent_id)
    else
      # CEO stalled — broadcast user ping
      Phoenix.PubSub.broadcast(
        HiveWeave.PubSub,
        "project:#{project_id}",
        {:user_ping, %{
          agent_id: agent.id,
          agent_name: agent.name,
          message: "CEO #{agent.name} appears stalled: #{reason}",
          timestamp: System.system_time(:millisecond)
        }}
      )
    end
  end

  # Normalize incoming alarm (could be map with string keys, atom keys, or struct)
  defp normalize_alarm(alarm) when is_map(alarm) do
    %{
      id: get_field(alarm, :id) || Ecto.UUID.generate(),
      project_id: get_field(alarm, :project_id),
      from_agent_id: get_field(alarm, :from_agent_id) || get_field(alarm, :fromAgentId),
      to_agent_id: get_field(alarm, :to_agent_id) || get_field(alarm, :toAgentId),
      purpose: get_field(alarm, :purpose) || "",
      fire_at_game_seconds: get_field(alarm, :fire_at_game_seconds) || get_field(alarm, :fireAtGameSeconds) || 0,
      fired: false
    }
  end

  defp get_field(map, key) when is_atom(key) do
    Map.get(map, key) || Map.get(map, Atom.to_string(key))
  end
end
