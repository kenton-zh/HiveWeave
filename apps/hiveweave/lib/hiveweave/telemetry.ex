defmodule HiveWeave.Telemetry do
  @moduledoc """
  Telemetry supervisor and event handlers.

  Attaches handlers to the telemetry events emitted throughout the system:
  - LLM streaming events
  - Agent state changes
  - Circuit breaker events
  - Tool execution events
  """

  use Supervisor

  def start_link(_opts) do
    Supervisor.start_link(__MODULE__, [], name: __MODULE__)
  end

  @impl true
  def init(_opts) do
    # Attach handlers on startup
    attach_handlers()

    children = [
      # Metrics reporter (optional - can be added later)
    ]

    Supervisor.init(children, strategy: :one_for_one)
  end

  defp attach_handlers do
    event_names = [
      [:hiveweave, :llm, :stream_start],
      [:hiveweave, :llm, :stream_chunk],
      [:hiveweave, :llm, :stream_done],
      [:hiveweave, :llm, :stream_fail],
      [:hiveweave, :agent, :chat_start],
      [:hiveweave, :agent, :chat_done],
      [:hiveweave, :agent, :crash],
      [:hiveweave, :circuit, :open],
      [:hiveweave, :circuit, :close]
    ]

    :telemetry.attach_many(
      "hiveweave-logger",
      event_names,
      &__MODULE__.dispatch_handler/4,
      nil
    )
  end

  def dispatch_handler(event, measurements, metadata, _config) do
    require Logger
    Logger.info("[Telemetry] #{inspect(event)} measurements=#{inspect(measurements)} metadata=#{inspect(metadata)}")
  end

  def handle_llm_event(event, _measurements, metadata, _config) do
    require Logger
    Logger.debug("[LLM] #{inspect(event)} provider=#{inspect(metadata[:provider])}")
  end

  def handle_agent_event(event, _measurements, metadata, _config) do
    require Logger
    Logger.debug("[Agent] #{inspect(event)} agent_id=#{inspect(metadata[:agent_id])}")
  end

  def handle_agent_crash(_event, _measurements, %{agent_id: id, reason: reason}, _config) do
    require Logger
    Logger.warning("[CRASH] Agent #{id} crashed: #{inspect(reason)}")
    HiveWeave.EventAudit.log(id, :crash, %{reason: inspect(reason)})
  end

  def handle_circuit_event(event, _measurements, metadata, _config) do
    require Logger
    Logger.info("[Circuit] #{inspect(event)} provider=#{inspect(metadata[:provider])}")
  end

  # Public API to emit events
  def llm_stream_start(provider, model) do
    :telemetry.execute(
      [:hiveweave, :llm, :stream_start],
      %{system_time: System.system_time()},
      %{provider: provider, model: model}
    )
  end

  def llm_stream_chunk(provider, latency_ms) do
    :telemetry.execute(
      [:hiveweave, :llm, :stream_chunk],
      %{latency_ms: latency_ms},
      %{provider: provider}
    )
  end

  def llm_stream_done(provider, model, duration_ms, status) do
    :telemetry.execute(
      [:hiveweave, :llm, :stream_done],
      %{duration_ms: duration_ms},
      %{provider: provider, model: model, status: status}
    )
  end

  def llm_stream_fail(provider, reason) do
    :telemetry.execute(
      [:hiveweave, :llm, :stream_fail],
      %{system_time: System.system_time()},
      %{provider: provider, reason: reason}
    )
  end

  def agent_chat_start(agent_id, from) do
    :telemetry.execute(
      [:hiveweave, :agent, :chat_start],
      %{system_time: System.system_time()},
      %{agent_id: agent_id, from: from}
    )
  end

  def agent_chat_done(agent_id, duration_ms, tokens) do
    :telemetry.execute(
      [:hiveweave, :agent, :chat_done],
      %{duration_ms: duration_ms},
      %{agent_id: agent_id, tokens: tokens}
    )
  end

  def agent_crash(agent_id, reason) do
    :telemetry.execute(
      [:hiveweave, :agent, :crash],
      %{system_time: System.system_time()},
      %{agent_id: agent_id, reason: reason}
    )
  end

  def circuit_open(provider) do
    :telemetry.execute(
      [:hiveweave, :circuit, :open],
      %{system_time: System.system_time()},
      %{provider: provider}
    )
  end

  def circuit_close(provider) do
    :telemetry.execute(
      [:hiveweave, :circuit, :close],
      %{system_time: System.system_time()},
      %{provider: provider}
    )
  end
end
