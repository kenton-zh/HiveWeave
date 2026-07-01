defmodule HiveWeave.Services.Permission do
  @moduledoc """
  Permission evaluation with 4-mode presets and glob pattern matching.
  
  Modes:
    - readonly: read-only tools (read_file, list_files, grep, read_skill, etc.)
    - readwrite: readonly + write/edit/bash tools
    - full: all tools allowed by default (ask for dangerous ones)
    - custom: user-defined allow/deny/ask lists
  
  Rule evaluation: deny → ask → allow (deny takes priority)
  Supports wildcard/glob matching for tool patterns.
  """

  require Logger

  @readonly_tools ~w(read_file list_files grep read_skill read_project_memory 
    read_charter read_goals get_project_time get_real_time question todowrite
    message_superior send_message write_work_log check_agent_status
    read_roster list_subordinates list_all_agents read_work_logs
    get_skill_detail list_available_skills list_available_mcp
    git_worktree_list git_worktree_status)

  @readwrite_tools @readonly_tools ++ ~w(write_file edit_file delete_file 
    move_file create_directory delete_directory search_files bash apply_patch
    write_memory report_completion dispatch_task hire_agent
    approve_work reject_work websearch run_code_review run_security_audit
    run_tests run_perf_audit run_full_review)

  @dangerous_tools ~w(bash apply_patch write_file edit_file delete_file 
    delete_directory git_worktree_create git_worktree_merge 
    git_worktree_remove dismiss_agent transfer_agent save_charter update_goals)

  @doc """
  Evaluate whether a tool is allowed for an agent.
  Returns :allow, :deny, or :ask.
  """
  def evaluate(agent, tool_name) do
    mode = get_permission_mode(agent)
    
    case mode do
      "full" -> check_rules(agent, tool_name, :allow)
      "readwrite" -> check_rules(agent, tool_name, :allow, @readwrite_tools)
      "readonly" -> check_rules(agent, tool_name, :allow, @readonly_tools)
      "custom" -> evaluate_custom(agent, tool_name)
      _ -> :allow  # default: allow for unknown modes
    end
  end

  @doc """
  Evaluate using deny/ask/allow lists with glob matching.
  Order: deny → ask → allow (deny takes priority)
  """
  def evaluate_custom(agent, tool_name) do
    denied = get_denied_tools(agent)
    ask = get_ask_tools(agent)
    allowed = get_allowed_tools(agent)

    cond do
      matches_pattern?(tool_name, denied) -> :deny
      matches_pattern?(tool_name, ask) -> :ask
      matches_pattern?(tool_name, allowed) -> :allow
      true -> :ask  # default: ask if not in any list
    end
  end

  @doc """
  Glob pattern matching for tool names.
  Supports: * (any chars), ToolName(arg*) (prefix match on args)
  """
  def matches_pattern?(tool_name, patterns) when is_list(patterns) do
    Enum.any?(patterns, fn p -> single_match?(tool_name, p) end)
  end
  def matches_pattern?(_tool_name, _), do: false

  defp single_match?(tool_name, pattern) when is_binary(pattern) do
    cond do
      pattern == "*" -> true
      pattern == tool_name -> true
      String.contains?(pattern, "*") ->
        regex = pattern
          |> Regex.escape()
          |> String.replace("\\*", ".*")
        Regex.match?(~r/^#{regex}$/, tool_name)
      true -> false
    end
  end

  @doc """
  Get the permission mode for an agent.
  """
  def get_permission_mode(agent) when is_map(agent) do
    Map.get(agent, :permission_type) || "executor"
  end

  defp get_denied_tools(agent) do
    parse_list(Map.get(agent, :denied_tools))
  end

  defp get_ask_tools(agent) do
    parse_list(Map.get(agent, :ask_tools))
  end

  defp get_allowed_tools(agent) do
    parse_list(Map.get(agent, :allowed_tools))
  end

  defp parse_list(nil), do: []
  defp parse_list(""), do: []
  defp parse_list(json) when is_binary(json) do
    case Jason.decode(json) do
      {:ok, list} when is_list(list) -> list
      _ -> []
    end
  rescue
    _ -> []
  end

  defp check_rules(agent, tool_name, default, preset \\ nil) do
    denied = get_denied_tools(agent)
    ask = get_ask_tools(agent)
    allowed = get_allowed_tools(agent)

    cond do
      matches_pattern?(tool_name, denied) -> :deny
      matches_pattern?(tool_name, ask) -> :ask
      matches_pattern?(tool_name, allowed) -> :allow
      preset && tool_name in preset -> default
      preset && tool_name not in preset -> :ask
      true -> default
    end
  end
end
