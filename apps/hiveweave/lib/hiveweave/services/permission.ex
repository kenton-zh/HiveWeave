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

  Parameter-level matching: patterns like `Bash(npm *)` match a tool name
  AND its serialized arguments (glob on the arg string). The tool-name part
  inside the parentheses is compared case-insensitively so `Bash(npm *)`
  matches the `bash` tool. Plain (paren-less) patterns keep the original
  case-sensitive exact/glob behavior.
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

  # Shell tools with full command execution — denied for readonly mode even
  # with approval, since they grant unrestricted filesystem/process access.
  @shell_tools ~w(run_command bash)

  @doc """
  Evaluate whether a tool is allowed for an agent.
  Returns :allow, :deny, or :ask.

  `input` is the tool's input map. It is used for parameter-level pattern
  matching such as `Bash(npm *)`. It may be omitted for backward compatibility.
  """
  def evaluate(agent, tool_name, input \\ nil) do
    mode = get_permission_mode(agent)

    case mode do
      "full" -> check_rules(agent, tool_name, :allow, nil, input)
      "readwrite" -> check_rules(agent, tool_name, :allow, @readwrite_tools, input)
      "readonly" -> check_rules(agent, tool_name, :allow, @readonly_tools, input)
      "custom" -> evaluate_custom(agent, tool_name, input)
      _ -> :allow  # default: allow for unknown modes
    end
  end

  @doc """
  Evaluate using deny/ask/allow lists with glob matching.
  Order: deny → ask → allow (deny takes priority)

  `input` is the tool's input map, used for parameter-level patterns
  like `Bash(npm *)`.
  """
  def evaluate_custom(agent, tool_name, input \\ nil) do
    denied = get_denied_tools(agent)
    ask = get_ask_tools(agent)
    allowed = get_allowed_tools(agent)

    cond do
      matches_pattern?(tool_name, denied, input) -> :deny
      matches_pattern?(tool_name, ask, input) -> :ask
      matches_pattern?(tool_name, allowed, input) -> :allow
      true -> :ask  # default: ask if not in any list
    end
  end

  @doc """
  Glob pattern matching for tool names.
  Supports: * (any chars), ToolName(arg_pattern) (parameter-level match).

  `input` is the tool's input map. When a pattern contains parentheses, the
  arg_pattern is glob-matched against a serialized argument string derived
  from `input` (e.g. the bash `command`). Patterns without parentheses ignore
  `input` and behave as before (exact / glob match on the tool name only).
  """
  def matches_pattern?(tool_name, patterns, input \\ nil)

  def matches_pattern?(tool_name, patterns, input) when is_list(patterns) do
    args = extract_args_string(tool_name, input)
    Enum.any?(patterns, fn p -> single_match?(tool_name, p, args) end)
  end
  def matches_pattern?(_tool_name, _patterns, _input), do: false

  defp single_match?(tool_name, pattern, args) when is_binary(pattern) do
    cond do
      pattern == "*" -> true
      # Parameter-level pattern: "ToolName(arg_pattern)"
      has_param_pattern?(pattern) ->
        match_param_pattern?(tool_name, pattern, args)
      pattern == tool_name -> true
      String.contains?(pattern, "*") ->
        regex = glob_to_regex(pattern)
        Regex.match?(~r/^#{regex}$/, tool_name)
      true -> false
    end
  end

  # A param pattern looks like `ToolName(...)`: contains '(' and ends with ')'.
  defp has_param_pattern?(pattern) do
    String.contains?(pattern, "(") and String.ends_with?(pattern, ")")
  end

  defp match_param_pattern?(tool_name, pattern, args) do
    # Strict parse: ToolName(arg_pattern) where ToolName is word chars.
    case Regex.run(~r/^(\w+)\((.+)\)$/, pattern) do
      [_, rule_tool, arg_pattern] ->
        # Case-insensitive tool-name comparison so `Bash(npm *)` matches `bash`.
        if String.downcase(rule_tool) == String.downcase(tool_name) do
          regex = glob_to_regex(arg_pattern)
          Regex.match?(~r/^#{regex}$/, args)
        else
          false
        end

      _ ->
        false
    end
  end

  # Convert a glob pattern (with `*`) into a regex source string anchored full-match.
  defp glob_to_regex(pattern) do
    pattern
    |> Regex.escape()
    |> String.replace("\\*", ".*")
  end

  # Build the argument string matched by parameter-level patterns.
  # For `bash`, the natural argument is the command. For other tools, join
  # the input values with spaces. nil input yields "".
  defp extract_args_string(_tool_name, nil), do: ""
  defp extract_args_string("bash", input) when is_map(input) do
    # Check both string and atom keys — tool_executor may pass either form
    input["command"] || input[:command] || ""
  end
  defp extract_args_string(_tool_name, input) when is_map(input) do
    input
    |> Enum.map(fn
      {_k, v} when is_binary(v) -> v
      {_k, v} -> to_string(v)
    end)
    |> Enum.join(" ")
  end
  defp extract_args_string(_tool_name, input), do: to_string(input)

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

  defp check_rules(agent, tool_name, default, preset, input) do
    denied = get_denied_tools(agent)
    ask = get_ask_tools(agent)
    allowed = get_allowed_tools(agent)

    cond do
      matches_pattern?(tool_name, denied, input) -> :deny
      # Shell tools are denied for readonly mode even if not in the denied list
      preset == @readonly_tools and tool_name in @shell_tools -> :deny
      matches_pattern?(tool_name, ask, input) -> :ask
      matches_pattern?(tool_name, allowed, input) -> :allow
      preset && tool_name in preset -> default
      preset && tool_name not in preset -> :ask
      true -> default
    end
  end
end
