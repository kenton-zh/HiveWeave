defmodule HiveWeaveWeb.ExtraController do
  @moduledoc """
  Consolidated controller for v1.5 endpoints that were not ported one-to-one.

  Each action is a thin wrapper that reads from the meta DB / in-memory state
  and returns data in the same shape as the legacy TS backend.
  """
  use Phoenix.Controller

  import Ecto.Query

  alias HiveWeave.Schema.{
    Agent,
    AgentEvent,
    AgentTemplate,
    GlobalSetting,
    Project
  }

  plug :accepts, ["json"]

  # ---------------------------------------------------------------------------
  # Chat messages (DB-backed)
  # ---------------------------------------------------------------------------

  def chat_messages(conn, %{"agentId" => agent_id}) do
    # Route through ProjectFactory to read from the per-project DB.
    # The Meta DB may have a legacy chat_messages table but it's not where
    # new messages are stored.
    messages =
      case HiveWeave.Repo.ProjectFactory.query_for_agent(
             agent_id,
             "SELECT id, agent_id, role, content, tool_calls, is_background, is_read, is_streaming, is_context, thinking, images, team_from_agent_id, team_to_agent_id, created_at FROM chat_messages WHERE agent_id = ? ORDER BY created_at ASC LIMIT 200",
             [agent_id]
           ) do
        {:ok, r} ->
          r.rows
          |> Enum.map(fn row ->
            Enum.zip(r.columns, row) |> Enum.into(%{})
          end)
          |> Enum.map(&serialize_chat_message_map/1)

        {:error, _} ->
          []
      end

    json(conn, messages)
  rescue
    _ -> json(conn, [])
  catch
    :exit, _ -> json(conn, [])
  end

  # ---------------------------------------------------------------------------
  # LLM Models CRUD
  # ---------------------------------------------------------------------------

  def llm_models_index(conn, _params) do
    # Use raw SQL to avoid Ecto's auto-timestamp injection
    case raw_query(
           "SELECT id, name, model_id, base_url, api_key, context_window, max_output_tokens, supports_thinking, default_reasoning_effort, temperature, is_active, created_at, updated_at FROM llm_models ORDER BY created_at DESC"
         ) do
      {:ok, rows} ->
        json(conn, %{models: Enum.map(rows, &row_to_model/1)})
      {:error, _} ->
        json(conn, %{models: []})
    end
  end

  def llm_models_create(conn, params) do
    now = System.system_time(:millisecond)
    id = Ecto.UUID.generate()

    sql = """
    INSERT INTO llm_models (id, name, model_id, base_url, api_key, context_window, max_output_tokens, supports_thinking, default_reasoning_effort, temperature, is_active, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    args = [
      id,
      params["name"] || "",
      params["model_id"] || params["modelId"] || "",
      params["base_url"] || params["baseUrl"] || "",
      params["api_key"] || params["apiKey"] || "",
      parse_int(params["context_window"] || params["contextWindow"], 128_000),
      parse_int(params["max_output_tokens"] || params["maxOutputTokens"], 8_192),
      if(params["supports_thinking"] || params["supportsThinking"], do: 1, else: 0),
      params["default_reasoning_effort"] || params["defaultReasoningEffort"],
      params["temperature"],
      if(params["is_active"] === nil || params["is_active"] == true, do: 1, else: 0),
      now,
      now
    ]

    case raw_execute(sql, args) do
      {:ok, _} -> json(conn, %{ok: true, id: id})
      {:error, _} -> conn |> put_status(500) |> json(%{error: "Failed to create model"})
    end
  end

  def llm_model_show(conn, %{"id" => id}) do
    case raw_query(
           "SELECT id, name, model_id, base_url, api_key, context_window, max_output_tokens, supports_thinking, default_reasoning_effort, temperature, is_active, created_at, updated_at FROM llm_models WHERE id = ?",
           [id]
         ) do
      {:ok, []} -> conn |> put_status(404) |> json(%{error: "Model not found"})
      {:ok, [row | _]} -> json(conn, %{model: row_to_model(row)})
      {:error, _} -> conn |> put_status(500) |> json(%{error: "Query failed"})
    end
  end

  def llm_model_update(conn, %{"id" => id} = params) do
    now = System.system_time(:millisecond)
    sets = []

    sets =
      if params["name"], do: [{sets, "name = ?", params["name"]}], else: sets
    sets =
      if params["model_id"] || params["modelId"],
        do: [{sets, "model_id = ?", params["model_id"] || params["modelId"]}], else: sets
    sets =
      if params["base_url"] || params["baseUrl"],
        do: [{sets, "base_url = ?", params["base_url"] || params["baseUrl"]}], else: sets
    sets =
      if params["api_key"] || params["apiKey"],
        do: [{sets, "api_key = ?", params["api_key"] || params["apiKey"]}], else: sets
    sets = if params["is_active"] || params["isActive"],
            do: [{sets, "is_active = ?", if(params["is_active"] || params["isActive"], do: 1, else: 0)}], else: sets

    set_clauses =
      sets
      |> Enum.map(fn {_, clause, _} -> clause end)
      |> Enum.join(", ")

    set_clauses = set_clauses <> ", updated_at = ?"

    args =
      sets
      |> Enum.map(fn {_, _, val} -> val end)
      |> Kernel.++([now, id])

    sql = "UPDATE llm_models SET #{set_clauses} WHERE id = ?"

    case raw_execute(sql, args) do
      {:ok, 0} -> conn |> put_status(404) |> json(%{error: "Model not found"})
      {:ok, _} -> json(conn, %{ok: true})
      {:error, _} -> conn |> put_status(500) |> json(%{error: "Update failed"})
    end
  end

  def llm_model_delete(conn, %{"id" => id}) do
    case raw_execute("DELETE FROM llm_models WHERE id = ?", [id]) do
      {:ok, 0} -> conn |> put_status(404) |> json(%{error: "Model not found"})
      {:ok, _} -> json(conn, %{ok: true})
      {:error, _} -> conn |> put_status(500) |> json(%{error: "Delete failed"})
    end
  end

  def llm_model_test(conn, %{"id" => id}) do
    # Stub: real implementation would POST to the model endpoint
    json(conn, %{ok: true, model: id, latencyMs: 0, message: "Test endpoint stub"})
  end

  # ---------------------------------------------------------------------------
  # Agent Templates
  # ---------------------------------------------------------------------------

  def templates_index(conn, params) do
    query =
      from(t in AgentTemplate, order_by: [asc: t.name])

    query =
      case params["division"] do
        nil -> query
        d -> where(query, [t], t.division == ^d)
      end

    query =
      case params["role"] do
        nil -> query
        r -> where(query, [t], t.role == ^r)
      end

    templates = HiveWeave.Repo.Meta.all(query) |> Enum.map(&serialize_template/1)
    json(conn, %{templates: templates})
  end

  def template_divisions(conn, _params) do
    divisions =
      AgentTemplate
      |> HiveWeave.Repo.Meta.all()
      |> Enum.map(& &1.division)
      |> Enum.filter(&(not is_nil(&1) and &1 != ""))
      |> Enum.uniq()
      |> Enum.sort()

    json(conn, %{divisions: divisions})
  end

  def template_show(conn, %{"id" => id}) do
    case HiveWeave.Repo.Meta.get(AgentTemplate, id) do
      nil -> conn |> put_status(404) |> json(%{error: "Template not found"})
      template -> json(conn, %{template: serialize_template(template)})
    end
  end

  # ---------------------------------------------------------------------------
  # Communications (in-memory + inbox table)
  # ---------------------------------------------------------------------------

  def communications_index(conn, params) do
    limit = parse_int(params["limit"], 50)
    project_id = params["projectId"]

    # Build agent_ids filter for per-project DB query
    agent_ids =
      case project_id do
        nil -> nil
        pid ->
          uuid = to_uuid(pid)
          if uuid, do: agent_ids_for_project(uuid), else: nil
      end

    comms =
      case first_agent_for_query(agent_ids) do
        nil ->
          []

        first_agent_id ->
          # Query the per-project DB via the first agent (ProjectFactory resolves project)
          # then filter in Elixir for all matching agents.
          sql = "SELECT id, from_agent_id, to_agent_id, message_type, message, subject, content, priority, status, read, is_read, metadata, created_at FROM inbox ORDER BY created_at DESC LIMIT ?"

          case HiveWeave.Repo.ProjectFactory.query_for_agent(first_agent_id, sql, [limit]) do
            {:ok, r} ->
              r.rows
              |> Enum.map(fn row -> Enum.zip(r.columns, row) |> Enum.into(%{}) end)
              |> Enum.filter(fn row ->
                # If we have agent_ids filter, apply it
                case agent_ids do
                  nil -> true
                  ids ->
                    Map.get(row, "from_agent_id") in ids or Map.get(row, "to_agent_id") in ids
                end
              end)
              |> Enum.map(&serialize_inbox_map_as_comm/1)

            {:error, _} ->
              []
          end
      end

    json(conn, comms)
  rescue
    _ -> json(conn, [])
  catch
    :exit, _ -> json(conn, [])
  end

  def communications_create(conn, %{"toAgentId" => to_id} = params) do
    case HiveWeave.Services.Inbox.send_message(
           params["fromAgentId"] || "user",
           to_id,
           params["type"] || "message",
           params["content"] || "",
           subject: params["subject"],
           priority: params["priority"],
           metadata: params["metadata"]
         ) do
      {:ok, msg} -> json(conn, %{ok: true, message: msg})
      error -> conn |> put_status(500) |> json(%{error: "Failed to create question"})
    end
  end

  # ---------------------------------------------------------------------------
  # User Pings (use agent_events table as the source)
  # ---------------------------------------------------------------------------

  def user_pings_index(conn, params) do
    limit = parse_int(params["limit"], 50)
    unread_only = params["unreadOnly"] == "true"

    # agent_events is in per-project DB. We need at least one agent to resolve
    # the project. Use the first agent from a project that has a workspace_path.
    first_agent_id =
      case HiveWeave.Repo.Meta.one(
             from(a in Agent,
               join: p in HiveWeave.Schema.Project, on: a.project_id == p.id,
               where: not is_nil(p.workspace_path) and p.workspace_path != "",
               select: a.id,
               limit: 1
             )
           ) do
        nil -> nil
        id -> id
      end

    pings =
      case first_agent_id do
        nil ->
          []

        aid ->
          sql =
            if unread_only do
              "SELECT id, agent_id, event_type, payload, created_at FROM agent_events WHERE event_type = 'user_ping' ORDER BY created_at DESC LIMIT ?"
            else
              "SELECT id, agent_id, event_type, payload, created_at FROM agent_events ORDER BY created_at DESC LIMIT ?"
            end

          case HiveWeave.Repo.ProjectFactory.query_for_agent(aid, sql, [limit]) do
            {:ok, r} ->
              r.rows
              |> Enum.map(fn row -> Enum.zip(r.columns, row) |> Enum.into(%{}) end)
              |> Enum.map(&serialize_event_map_as_ping/1)

            {:error, _} ->
              []
          end
      end

    json(conn, pings)
  rescue
    _ -> json(conn, [])
  catch
    :exit, _ -> json(conn, [])
  end

  def user_ping_read(conn, %{"id" => _id}) do
    # In a real impl we'd mark the ping as read in DB
    json(conn, %{ok: true})
  end

  # ---------------------------------------------------------------------------
  # Project Alarms
  # ---------------------------------------------------------------------------

  def project_alarms_index(conn, %{"project_id" => project_id} = params) do
    include_fired = params["includeFired"] == "true"
    uuid = to_uuid(project_id)
    agent_ids = if uuid, do: agent_ids_for_project(uuid), else: []

    alarms =
      case List.first(agent_ids) do
        nil ->
          []

        first_agent_id ->
          # Build placeholders for agent_ids filter
          placeholders = Enum.map_join(agent_ids, ",", fn _ -> "?" end)

          sql =
            if include_fired do
              "SELECT id, project_id, from_agent_id, to_agent_id, purpose, fire_at_game_seconds, status, fired, fired_at, created_at FROM scheduled_alarms WHERE to_agent_id IN (#{placeholders}) OR from_agent_id IN (#{placeholders}) ORDER BY fire_at_game_seconds ASC"
            else
              "SELECT id, project_id, from_agent_id, to_agent_id, purpose, fire_at_game_seconds, status, fired, fired_at, created_at FROM scheduled_alarms WHERE (to_agent_id IN (#{placeholders}) OR from_agent_id IN (#{placeholders})) AND fired = 0 ORDER BY fire_at_game_seconds ASC"
            end

          # agent_ids appears twice (for to_agent_id and from_agent_id)
          params = agent_ids ++ agent_ids

          case HiveWeave.Repo.ProjectFactory.query_for_agent(first_agent_id, sql, params) do
            {:ok, r} ->
              r.rows
              |> Enum.map(fn row -> Enum.zip(r.columns, row) |> Enum.into(%{}) end)
              |> Enum.map(&serialize_alarm_map/1)

            {:error, _} ->
              []
          end
      end

    json(conn, alarms)
  rescue
    _ -> json(conn, [])
  catch
    :exit, _ -> json(conn, [])
  end

  def project_alarms_create(conn, %{"project_id" => project_id} = params) do
    now = System.system_time(:millisecond)

    from_agent_id = params["fromAgentId"]
    to_agent_id = params["toAgentId"]
    purpose = params["purpose"] || ""
    fire_at = parse_int(params["fireAtGameSeconds"], 0)

    uuid = to_uuid(project_id)

    case uuid do
      nil ->
        conn |> put_status(400) |> json(%{error: "Invalid project_id"})

      _ ->
        alarm = %{
          id: Ecto.UUID.generate(),
          from_agent_id: from_agent_id,
          to_agent_id: to_agent_id,
          purpose: purpose,
          fire_at_game_seconds: fire_at,
          fired: false
        }

        # GameTime.Server.schedule_alarm persists to DB + loads into memory
        case HiveWeave.GameTime.Server.schedule_alarm(uuid, alarm) do
          {:ok, alarm_id} ->
            json(conn, %{
              ok: true,
              alarm: %{
                id: alarm_id,
                from_agent_id: from_agent_id,
                fromAgentId: from_agent_id,
                to_agent_id: to_agent_id,
                toAgentId: to_agent_id,
                purpose: purpose,
                fire_at_game_seconds: fire_at,
                fireAtGameSeconds: fire_at,
                fired: false,
                fired_at: nil,
                firedAt: nil,
                created_at: now,
                createdAt: now
              }
            })

          _ ->
            conn |> put_status(500) |> json(%{error: "Failed to create alarm"})
        end
    end
  end

  def project_alarm_cancel(conn, %{"project_id" => project_id, "id" => id}) do
    uuid = to_uuid(project_id)

    case uuid do
      nil ->
        conn |> put_status(400) |> json(%{error: "Invalid project_id"})

      _ ->
        # GameTime.Server.cancel_alarm updates DB + removes from memory
        HiveWeave.GameTime.Server.cancel_alarm(uuid, id)
        json(conn, %{ok: true})
    end
  end

  # ---------------------------------------------------------------------------
  # Todos
  # ---------------------------------------------------------------------------

  def chat_todos(conn, %{"agentId" => agent_id}) do
    todos =
      case HiveWeave.Repo.ProjectFactory.query_for_agent(
             agent_id,
             "SELECT id, agent_id, content, status, priority, created_at, updated_at FROM todos WHERE agent_id = ? ORDER BY created_at ASC",
             [agent_id]
           ) do
        {:ok, r} ->
          r.rows
          |> Enum.map(fn row ->
            [id, _agent, content, status, priority, _created, _updated] = row
            %{
              id: id,
              content: content,
              status: status,
              priority: priority
            }
          end)

        {:error, _} ->
          []
      end

    json(conn, %{todos: todos})
  rescue
    _ -> json(conn, %{todos: []})
  catch
    :exit, _ -> json(conn, %{todos: []})
  end

  def chat_todos_write(conn, %{"agentId" => agent_id} = params) do
    todos = params["todos"] || []
    now_ms = System.system_time(:millisecond)

    # Get project_id from agent
    project_id =
      case HiveWeave.Repo.ProjectFactory.resolve_project(agent_id) do
        {:ok, pid} -> pid
        _ -> nil
      end

    # Delete existing todos, insert new ones
    HiveWeave.Repo.ProjectFactory.query_for_agent(agent_id,
      "DELETE FROM todos WHERE agent_id = ?", [agent_id])

    Enum.each(todos, fn t ->
      id = Ecto.UUID.generate()
      content = t["content"] || t["task"] || ""
      status = t["status"] || "pending"
      priority = t["priority"] || "medium"

      HiveWeave.Repo.ProjectFactory.query_for_agent(agent_id,
        "INSERT INTO todos (id, agent_id, project_id, content, status, priority, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [id, agent_id, project_id, content, status, priority, now_ms, now_ms])
    end)

    json(conn, %{ok: true, todos: todos})
  rescue
    _ -> json(conn, %{ok: true, todos: params["todos"] || []})
  catch
    :exit, _ -> json(conn, %{ok: true, todos: params["todos"] || []})
  end

  # ---------------------------------------------------------------------------
  # Work Logs
  # ---------------------------------------------------------------------------

  def work_logs_index(conn, %{"agentId" => agent_id} = params) do
    limit = parse_int(params["limit"], 50)

    logs =
      case HiveWeave.Repo.ProjectFactory.query_for_agent(
             agent_id,
             "SELECT id, agent_id, project_id, session_id, task_id, action, type, summary, content, details, metadata, created_at FROM work_logs WHERE agent_id = ? ORDER BY created_at DESC LIMIT ?",
             [agent_id, limit]
           ) do
        {:ok, r} ->
          r.rows
          |> Enum.map(fn row -> Enum.zip(r.columns, row) |> Enum.into(%{}) end)
          |> Enum.map(&serialize_work_log_map/1)

        {:error, _} ->
          []
      end

    json(conn, %{logs: logs})
  rescue
    _ -> json(conn, %{logs: []})
  catch
    :exit, _ -> json(conn, %{logs: []})
  end

  # ---------------------------------------------------------------------------
  # Questions
  # ---------------------------------------------------------------------------

  def chat_questions_index(conn, params) do
    project_id = params["projectId"]
    agent_id = params["agentId"]

    questions =
      cond do
        agent_id != nil and agent_id != "" ->
          # Questions for a specific agent
          case HiveWeave.Repo.ProjectFactory.query_for_agent(
                 agent_id,
                 "SELECT id, agent_id, question, answer, status, created_at, answered_at FROM questions WHERE agent_id = ? AND status = 'pending' ORDER BY created_at DESC",
                 [agent_id]
               ) do
            {:ok, r} -> parse_question_rows(r)
            {:error, _} -> []
          end

        project_id != nil and project_id != "" ->
          # Questions for an entire project
          case HiveWeave.Repo.ProjectFactory.query(
                 project_id,
                 "SELECT id, agent_id, question, answer, status, created_at, answered_at FROM questions WHERE status = 'pending' ORDER BY created_at DESC",
                 []
               ) do
            {:ok, r} -> parse_question_rows(r)
            {:error, _} -> []
          end

        true ->
          []
      end

    json(conn, %{questions: questions})
  rescue
    _ -> json(conn, %{questions: []})
  catch
    :exit, _ -> json(conn, %{questions: []})
  end

  def chat_questions_answer(conn, %{"id" => question_id} = params) do
    answer = params["answer"] || ""

    # Find the question across all project DBs — we need the agent_id to route
    # Since questions are per-project, try resolving via the agent_id if provided
    agent_id = params["agentId"]

    if agent_id do
      now_ms = System.system_time(:millisecond)

      case HiveWeave.Repo.ProjectFactory.query_for_agent(
             agent_id,
             "UPDATE questions SET answer = ?, status = 'answered', answered_at = ? WHERE id = ?",
             [answer, now_ms, question_id]
           ) do
        {:ok, _} ->
          # Broadcast the answer to the agent (frontend real-time update)
          Phoenix.PubSub.broadcast(
            HiveWeave.PubSub,
            "agent:#{agent_id}",
            {:question_answered, %{id: question_id, answer: answer}}
          )

          # Deliver the answer to the agent's GenServer, which forwards it to
          # the blocked LLM Task process waiting in receive (blocking question).
          # Resolve project_id first so we can address the GenServer by name.
          project_id =
            case HiveWeave.Repo.ProjectFactory.resolve_project(agent_id) do
              {:ok, pid} -> pid
              _ -> nil
            end

          if project_id do
            agent_name = HiveWeave.Agents.Agent.name(project_id, agent_id)

            case Process.whereis(agent_name) do
              nil -> :ok
              _pid -> send(agent_name, {:question_answer, question_id, answer})
            end
          end

          json(conn, %{ok: true, answer: answer})

        {:error, _reason} ->
          conn |> put_status(500) |> json(%{error: "Failed to save answer"})
      end
    else
      conn |> put_status(400) |> json(%{error: "agentId is required to answer a question"})
    end
  end

  defp parse_question_rows(r) do
    r.rows
    |> Enum.map(fn row ->
      [id, agent_id, question, answer, status, created_at, answered_at] = row
      %{
        id: id,
        agentId: agent_id,
        question: question,
        answer: answer,
        status: status,
        createdAt: created_at,
        answeredAt: answered_at
      }
    end)
  end

  # ---------------------------------------------------------------------------
  # Filesystem browse
  # ---------------------------------------------------------------------------

  def fs_browse(conn, %{"path" => path}) do
    result =
      try do
        safe_path = sanitize_browse_path(path)
        browse_path(safe_path)
      rescue
        e -> %{error: Exception.message(e) || "Access denied", path: path, parent: nil, entries: []}
      end

    json(conn, result)
  end

  def fs_browse(conn, _params) do
    result =
      try do
        browse_path(sanitize_browse_path(System.user_home() || "C:\\"))
      rescue
        e -> %{error: inspect(e), path: nil, parent: nil, entries: []}
      end

    json(conn, result)
  end

  # ---------------------------------------------------------------------------
  # Helpers
  # ---------------------------------------------------------------------------

  defp raw_query(sql, args \\ []) do
    case Ecto.Adapters.SQL.query(HiveWeave.Repo.Meta, sql, args) do
      {:ok, %{rows: rows}} -> {:ok, rows}
      {:ok, result} -> {:ok, Map.get(result, :rows, [])}
      err -> err
    end
  rescue
    _ -> {:error, :query_failed}
  end

  defp raw_execute(sql, args \\ []) do
    case Ecto.Adapters.SQL.query(HiveWeave.Repo.Meta, sql, args) do
      {:ok, %{num_rows: n}} -> {:ok, n}
      {:ok, _} -> {:ok, 0}
      err -> err
    end
  rescue
    _ -> {:error, :execute_failed}
  end

  defp mask_api_key(nil), do: nil
  defp mask_api_key(key) when is_binary(key) do
    if byte_size(key) > 8, do: "#{String.slice(key, 0, 8)}...", else: key
  end

  defp row_to_model(row) when is_tuple(row) do
    %{
      id: elem(row, 0),
      name: elem(row, 1),
      model_id: elem(row, 2),
      modelId: elem(row, 2),
      base_url: elem(row, 3),
      baseUrl: elem(row, 3),
      api_key: mask_api_key(elem(row, 4)),
      apiKey: mask_api_key(elem(row, 4)),
      context_window: elem(row, 5),
      contextWindow: elem(row, 5),
      max_output_tokens: elem(row, 6),
      maxOutputTokens: elem(row, 6),
      supports_thinking: elem(row, 7) == 1,
      supportsThinking: elem(row, 7) == 1,
      default_reasoning_effort: elem(row, 8),
      defaultReasoningEffort: elem(row, 8),
      temperature: elem(row, 9),
      is_active: elem(row, 10) == 1,
      isActive: elem(row, 10) == 1,
      created_at: elem(row, 11),
      updated_at: elem(row, 12)
    }
  end

  defp row_to_model(row) when is_list(row), do: row_to_model(List.to_tuple(row))

  defp sanitize_browse_path(path) do
    resolved =
      path
      |> Path.expand()
      |> String.replace("\\", "/")

    blocklist = [
      "/etc/passwd",
      "/etc/shadow",
      "/root/",
      "/var/run/",
      "/proc/",
      "/sys/",
      "/Windows/System32/config/",
      "/Windows/System32/drivers/etc/",
      "C:/Windows/System32/config/",
      "C:/Windows/System32/drivers/etc/"
    ]

    if Enum.any?(blocklist, &String.starts_with?(String.downcase(resolved), &1 |> String.downcase())) do
      raise "Access denied to system path"
    end

    resolved
  end

  defp browse_path(path) do
    cond do
      path == "" or is_nil(path) ->
        empty_result(nil)

      File.exists?(path) == false ->
        empty_result(path)

      File.regular?(path) ->
        empty_result(path)

      true ->
        do_browse(path)
    end
  end

  defp empty_result(path) do
    %{
      currentPath: path,
      parentPath: nil,
      entries: [],
      drives: list_windows_drives(),
      isRoot: true
    }
  end

  defp list_windows_drives do
    # Try to list drive letters on Windows; fall back to ["C:\\"].
    cond do
      match?({:win32, _}, :os.type()) ->
        try do
          # Read drive letters via File.ls of "C:\\".. "Z:\\" is expensive;
          # use a simpler heuristic: just list "C:\\" and any other letter
          # that has a "Users" or "Windows" subdir.
          for letter <- ~w(C D E F G H I J K L M N O P Q R S T U V W X Y Z),
              letter_drive = letter <> ":\\",
              File.exists?(letter_drive),
              do: letter_drive
        rescue
          _ -> ["C:\\"]
        end

      true ->
        ["/"]
    end
  end

  defp do_browse(path) do
    parent = Path.dirname(path)
    is_root = parent == path

    entries =
      case File.ls(path) do
        {:ok, names} ->
          Enum.map(names, fn name ->
            full = Path.join(path, name)
            stat = File.stat(full)

            %{
              name: name,
              path: full,
              fullPath: full,
              is_dir: stat != {:error, :enoent} && File.dir?(full),
              isDir: stat != {:error, :enoent} && File.dir?(full),
              size: case stat do
                {:ok, %{size: s}} -> s
                _ -> nil
              end,
              modified: case stat do
                {:ok, %{mtime: t}} -> mtime_to_iso(t)
                _ -> nil
              end
            }
          end)
          |> Enum.sort_by(& &1.name)

        {:error, reason} ->
          [%{name: "Error: #{inspect(reason)}", path: path, fullPath: path, is_dir: false, isDir: false}]
      end

    %{
      currentPath: path,
      parentPath: if(is_root, do: nil, else: parent),
      entries: entries,
      drives: list_windows_drives(),
      isRoot: is_root
    }
  end

  defp mtime_to_iso({{y, m, d}, {h, mi, s}}) do
    "#{y}-#{pad(m)}-#{pad(d)}T#{pad(h)}:#{pad(mi)}:#{pad(s)}"
  end

  defp mtime_to_iso(_), do: nil

  defp pad(n) when n < 10, do: "0#{n}"
  defp pad(n), do: "#{n}"

  defp agent_ids_for_project(project_id) do
    HiveWeave.Repo.Meta.all(
      from(a in Agent, where: a.project_id == ^project_id, select: a.id)
    )
  end

  # Pick the first agent_id from a list (or any agent with a valid project) to
  # use as the routing key for ProjectFactory.query_for_agent.
  defp first_agent_for_query(nil) do
    HiveWeave.Repo.Meta.one(
      from(a in Agent,
        join: p in HiveWeave.Schema.Project, on: a.project_id == p.id,
        where: not is_nil(p.workspace_path) and p.workspace_path != "",
        select: a.id,
        limit: 1
      )
    )
  end

  defp first_agent_for_query([]) do
    first_agent_for_query(nil)
  end

  defp first_agent_for_query(ids) when is_list(ids) do
    List.first(ids)
  end

  defp fetch_model(id) do
    HiveWeave.Repo.Meta.get(LlmModel, id) ||
      HiveWeave.Repo.Meta.one(from(m in LlmModel, where: m.id == ^id))
  rescue
    _ -> nil
  end

  defp model_attrs_from_params(params) do
    now = System.system_time(:millisecond)

    %{}
    |> put_if_present("id", params["id"])
    |> put_if_present("name", params["name"])
    |> put_if_present("model_id", params["model_id"] || params["modelId"])
    |> put_if_present("base_url", params["base_url"] || params["baseUrl"])
    |> put_if_present("api_key", params["api_key"] || params["apiKey"])
    |> put_if_present("context_window", parse_int(params["context_window"] || params["contextWindow"], 128_000))
    |> put_if_present("max_output_tokens", parse_int(params["max_output_tokens"] || params["maxOutputTokens"], 8_192))
    |> put_if_present("supports_thinking", params["supports_thinking"] || params["supportsThinking"] || false)
    |> put_if_present("default_reasoning_effort", params["default_reasoning_effort"] || params["defaultReasoningEffort"])
    |> put_if_present("temperature", params["temperature"])
    |> put_if_present("is_active", params["is_active"] || params["isActive"] || true)
    |> Map.put(:updated_at, now)
  end

  defp put_if_present(map, _, nil), do: map
  defp put_if_present(map, key, value), do: Map.put(map, key, value)

  defp parse_int(nil, default), do: default
  defp parse_int(s, default) when is_integer(s), do: s

  defp parse_int(s, default) when is_binary(s) do
    case Integer.parse(s) do
      {n, _} -> n
      :error -> default
    end
  end

  defp parse_int(_, default), do: default

  defp to_uuid(id) do
    case Ecto.UUID.cast(id) do
      {:ok, uuid} -> uuid
      :error -> nil
    end
  end

  defp format_errors(cs) do
    Ecto.Changeset.traverse_errors(cs, fn {msg, _} -> msg end)
  end

  # Serializers
  defp serialize_model(m) do
    %{
      id: m.id,
      name: m.name,
      model_id: m.model_id,
      modelId: m.model_id,
      base_url: m.base_url,
      baseUrl: m.base_url,
      api_key: m.api_key,
      apiKey: m.api_key,
      context_window: m.context_window,
      contextWindow: m.context_window,
      max_output_tokens: m.max_output_tokens,
      maxOutputTokens: m.max_output_tokens,
      supports_thinking: m.supports_thinking,
      supportsThinking: m.supports_thinking,
      default_reasoning_effort: m.default_reasoning_effort,
      defaultReasoningEffort: m.default_reasoning_effort,
      temperature: m.temperature,
      is_active: m.is_active,
      isActive: m.is_active,
      created_at: m.created_at,
      updated_at: m.updated_at
    }
  end

  defp serialize_template(t) do
    %{
      id: t.id,
      source: t.source,
      division: t.division,
      name: t.name,
      role: t.role,
      color: t.color,
      emoji: t.emoji,
      vibe: t.vibe,
      description: t.description,
      prompt_body: t.prompt_body,
      promptBody: t.prompt_body,
      original_file: t.original_file,
      originalFile: t.original_file,
      created_at: t.created_at,
      createdAt: t.created_at
    }
  end

  defp serialize_inbox_as_comm(i) do
    %{
      id: i.id,
      from_agent_id: i.from_agent_id,
      fromAgentId: i.from_agent_id,
      to_agent_id: i.to_agent_id,
      toAgentId: i.to_agent_id,
      type: i.type,
      subject: i.subject,
      content: i.content,
      status: i.status,
      metadata: decode_json(i.metadata),
      created_at: i.created_at,
      createdAt: i.created_at
    }
  end

  defp serialize_event_as_ping(e) do
    payload = decode_json(e.payload) || %{}
    agent_id = e.agent_id
    # Try to get agent name
    agent_name =
      case agent_id do
        nil -> nil
        _ ->
          case HiveWeave.Repo.Meta.get(Agent, agent_id) do
            nil -> nil
            a -> a.name
          end
      end

    %{
      id: e.id,
      agent_id: agent_id,
      agentId: agent_id,
      agent_name: agent_name,
      agentName: agent_name,
      type: e.event_type,
      content: payload["content"] || payload["message"] || "",
      tool_name: payload["toolName"] || payload["tool_name"],
      toolName: payload["toolName"] || payload["tool_name"],
      tool_input: payload["toolInput"] || payload["tool_input"],
      toolInput: payload["toolInput"] || payload["tool_input"],
      timestamp: e.created_at,
      read: false
    }
  end

  defp serialize_alarm(a) do
    %{
      id: a.id,
      from_agent_id: a.from_agent_id,
      fromAgentId: a.from_agent_id,
      to_agent_id: a.to_agent_id,
      toAgentId: a.to_agent_id,
      purpose: a.purpose,
      fire_at_game_seconds: a.fire_at_game_seconds,
      fireAtGameSeconds: a.fire_at_game_seconds,
      fired: a.fired == true,
      fired_at: a.fired_at,
      firedAt: a.fired_at,
      created_at: a.created_at,
      createdAt: a.created_at
    }
  end

  defp serialize_work_log(l) do
    %{
      id: l.id,
      agent_id: l.agent_id,
      agentId: l.agent_id,
      project_id: l.project_id,
      projectId: l.project_id,
      action: l.action,
      summary: l.summary,
      details: l.details,
      metadata: decode_json(l.metadata),
      created_at: l.created_at,
      createdAt: l.created_at
    }
  end

  defp serialize_chat_message(m) do
    %{
      id: m.id,
      agent_id: m.agent_id,
      agentId: m.agent_id,
      role: m.role,
      content: m.content,
      tool_calls: m.tool_calls,
      toolCalls: m.tool_calls,
      images: m.images,
      is_background: m.is_background,
      isBackground: m.is_background,
      is_read: m.is_read,
      isRead: m.is_read,
      is_streaming: m.is_streaming,
      isStreaming: m.is_streaming,
      is_context: m.is_context,
      isContext: m.is_context,
      team_from_agent_id: m.team_from_agent_id,
      teamFromAgentId: m.team_from_agent_id,
      team_to_agent_id: m.team_to_agent_id,
      teamToAgentId: m.team_to_agent_id,
      created_at: m.created_at,
      createdAt: m.created_at
    }
  end

  defp decode_json(nil), do: nil

  defp decode_json(str) do
    case Jason.decode(str) do
      {:ok, v} -> v
      _ -> nil
    end
  end

  # ── Map-based serializers (for raw DB rows with string keys) ────────

  defp serialize_chat_message_map(m) do
    %{
      "id" => m["id"],
      "agent_id" => m["agent_id"],
      "agentId" => m["agent_id"],
      "role" => m["role"],
      "content" => m["content"],
      "tool_calls" => m["tool_calls"],
      "toolCalls" => m["tool_calls"],
      "thinking" => m["thinking"],
      "images" => m["images"],
      "is_background" => m["is_background"],
      "isBackground" => m["is_background"],
      "is_read" => m["is_read"],
      "isRead" => m["is_read"],
      "is_streaming" => m["is_streaming"],
      "isStreaming" => m["is_streaming"],
      "is_context" => m["is_context"],
      "isContext" => m["is_context"],
      "team_from_agent_id" => m["team_from_agent_id"],
      "teamFromAgentId" => m["team_from_agent_id"],
      "team_to_agent_id" => m["team_to_agent_id"],
      "teamToAgentId" => m["team_to_agent_id"],
      "created_at" => m["created_at"],
      "createdAt" => m["created_at"]
    }
  end

  defp serialize_inbox_map_as_comm(m) do
    # The per-project inbox table has columns: message_type, message (legacy)
    # plus migrated columns: subject, content, priority, status.
    # Fall back to legacy columns if the migrated ones are NULL.
    type = m["message_type"] || m["type"] || "message"
    content = m["content"] || m["message"] || ""
    subject = m["subject"]
    status = m["status"] || (if m["is_read"] == 1 or m["read"] == 1, do: "read", else: "unread")

    %{
      "id" => m["id"],
      "from_agent_id" => m["from_agent_id"],
      "fromAgentId" => m["from_agent_id"],
      "to_agent_id" => m["to_agent_id"],
      "toAgentId" => m["to_agent_id"],
      "type" => type,
      "subject" => subject,
      "content" => content,
      "status" => status,
      "metadata" => decode_json(m["metadata"]),
      "created_at" => m["created_at"],
      "createdAt" => m["created_at"]
    }
  end

  defp serialize_event_map_as_ping(m) do
    payload = decode_json(m["payload"]) || %{}
    agent_id = m["agent_id"]

    agent_name =
      case agent_id do
        nil -> nil
        _ ->
          case HiveWeave.Repo.Meta.get(Agent, agent_id) do
            nil -> nil
            a -> a.name
          end
      end

    %{
      "id" => m["id"],
      "agent_id" => agent_id,
      "agentId" => agent_id,
      "agent_name" => agent_name,
      "agentName" => agent_name,
      "type" => m["event_type"],
      "content" => payload["content"] || payload["message"] || "",
      "tool_name" => payload["toolName"] || payload["tool_name"],
      "toolName" => payload["toolName"] || payload["tool_name"],
      "tool_input" => payload["toolInput"] || payload["tool_input"],
      "toolInput" => payload["toolInput"] || payload["tool_input"],
      "timestamp" => m["created_at"],
      "read" => false
    }
  end

  defp serialize_alarm_map(m) do
    fired_val = m["fired"]
    fired? = fired_val == true or fired_val == 1

    %{
      "id" => m["id"],
      "from_agent_id" => m["from_agent_id"],
      "fromAgentId" => m["from_agent_id"],
      "to_agent_id" => m["to_agent_id"],
      "toAgentId" => m["to_agent_id"],
      "purpose" => m["purpose"],
      "fire_at_game_seconds" => m["fire_at_game_seconds"],
      "fireAtGameSeconds" => m["fire_at_game_seconds"],
      "fired" => fired?,
      "fired_at" => m["fired_at"],
      "firedAt" => m["fired_at"],
      "created_at" => m["created_at"],
      "createdAt" => m["created_at"]
    }
  end

  defp serialize_work_log_map(m) do
    %{
      "id" => m["id"],
      "agent_id" => m["agent_id"],
      "agentId" => m["agent_id"],
      "project_id" => m["project_id"],
      "projectId" => m["project_id"],
      "action" => m["action"] || m["type"],
      "summary" => m["summary"] || m["content"],
      "details" => m["details"],
      "metadata" => decode_json(m["metadata"]),
      "created_at" => m["created_at"],
      "createdAt" => m["created_at"]
    }
  end
end
