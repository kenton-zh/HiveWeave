defmodule HiveWeave.ToolExecutor do
  @moduledoc """
  Routes tool calls to implementations with permission gating + sandbox checks.

  Mirrors the TS ToolExecutor design:
  - LLM-visible tools are filtered by role (coordinator vs executor)
  - Runtime security: self-destructive command blocking, path sandbox
  - Returns human-readable string results for LLM consumption
  - Errors are caught and returned as "Error: ..." strings

  Tool dispatch is via a simple `execute/4` function — no Effect-TS, no
  behaviour indirection. Each tool is a private function in this module.
  """

  require Logger

  # ── Tool definitions (OpenAI function-calling format) ───────

  @doc """
  Get the list of tools available to an agent, filtered by permission type.
  Coordinator (CEO) gets read-only tools; Executor gets full set.
  """
  def get_tools(permission_type, role \\ nil) do
    normalized_role = if is_binary(role), do: String.downcase(role), else: nil
    case permission_type do
      "coordinator" -> coordinator_tools(normalized_role)
      _ when normalized_role in ["reviewer", "inspector", "审查员", "qa"] ->
        executor_tools_for_reviewer()
      _ -> executor_tools()
    end
  end

  # ---- Shared core tools (all roles get these) ----
  defp core_tools do
    [
      message_superior_tool(),
      send_message_tool(),
      read_roster_tool(),
      check_agent_status_tool(),
      write_memory_tool(),
      fetch_url_tool(),
      get_project_time_tool(),
      get_real_time_tool(),
      set_alarm_tool(),
      question_tool(),
      todowrite_tool(),
      websearch_tool(),
      read_goals_tool(),
      mcp_list_tools_tool(),
      mcp_call_tool()
    ]
  end

  # ---- Read-only file tools (CEO, HR, Coordinator get these; Executor gets full set) ----
  defp readonly_file_tools do
    [
      read_file_tool(),
      list_files_tool(),
      grep_tool(),
      glob_tool(),
      search_files_tool()
    ]
  end

  # ---- Full file tools (Executor, QA only) ----
  defp full_file_tools do
    [
      bash_tool(),
      read_file_tool(),
      list_files_tool(),
      grep_tool(),
      glob_tool(),
      apply_patch_tool(),
      write_file_tool(),
      edit_file_tool(),
      delete_file_tool(),
      move_file_tool(),
      create_directory_tool(),
      delete_directory_tool(),
      search_files_tool()
    ]
  end

  # ---- Git worktree tools (CEO, HR, Coordinator only) ----
  defp git_worktree_tools do
    [
      git_worktree_create_tool(),
      git_worktree_checkpoint_tool(),
      git_worktree_merge_tool(),
      git_worktree_rollback_tool(),
      git_worktree_remove_tool(),
      git_worktree_list_tool(),
      git_worktree_status_tool()
    ]
  end

  # ---- Coordinator management tools (dispatch/review/approve) ----
  defp management_tools do
    [
      dispatch_task_tool(),
      read_work_logs_tool(),
      review_code_tool(),
      approve_work_tool(),
      reject_work_tool(),
      list_subordinates_tool()
    ]
  end

  # ---- Binding tools (CEO, HR only) ----
  defp binding_tools do
    [
      bind_skill_tool(),
      unbind_skill_tool(),
      list_available_skills_tool(),
      get_skill_detail_tool(),
      read_skill_tool(),
      bind_mcp_tool(),
      unbind_mcp_tool(),
      list_available_mcp_tool()
    ]
  end

  # ---- Charter tools ----
  defp charter_readonly do
    [read_charter_tool()]
  end

  defp charter_full do
    [save_charter_tool(), read_charter_tool()]
  end

  # ---- Executor-specific tools ----
  defp executor_specific_tools do
    [
      write_work_log_tool(),
      report_completion_tool(),
      read_project_memory_tool()
    ]
  end

  # ---- QA review tools (QA only) ----
  defp qa_review_tools do
    [
      run_code_review_tool(),
      run_security_audit_tool(),
      run_tests_tool(),
      run_perf_audit_tool(),
      run_full_review_tool()
    ]
  end

  # ---- Admin tools (CEO, HR, Coordinator) ----
  defp admin_tools do
    [
      mcp_configure_tool(),
      list_models_tool(),
      set_default_model_tool()
    ]
  end

  # ================================================================
  # CEO: management + git_worktree + charter_full + binding + admin
  #      + readonly_file + core + read_project_memory + update_goals + list_all_agents
  # ================================================================
  defp coordinator_tools("ceo") do
    management_tools() ++
    git_worktree_tools() ++
    charter_full() ++
    binding_tools() ++
    admin_tools() ++
    readonly_file_tools() ++
    core_tools() ++
    [read_project_memory_tool(), update_goals_tool(), list_all_agents_tool(), trigger_integration_tool(), write_work_log_tool()]
  end

  # ================================================================
  # HR: hire/transfer/dismiss + binding + admin + charter_readonly
  #     + git_worktree + readonly_file + core + list_all_agents
  #     NO management tools, NO file write, NO save_charter, NO update_goals
  # ================================================================
  defp coordinator_tools("hr") do
    [hire_agent_tool(), transfer_agent_tool(), dismiss_agent_tool(), update_roster_tool()] ++
    binding_tools() ++
    admin_tools() ++
    charter_readonly() ++
    git_worktree_tools() ++
    readonly_file_tools() ++
    core_tools() ++
    [list_all_agents_tool(), write_work_log_tool()]
  end

  # ================================================================
  # Generic Coordinator (architect, manager, etc.):
  #   management + git_worktree + admin + readonly_file + core
  #   + update_goals + trigger_integration
  #   NO hire_agent, NO binding, NO save_charter, NO file write, NO list_all_agents
  # ================================================================
  defp coordinator_tools(_role) do
    management_tools() ++
    git_worktree_tools() ++
    admin_tools() ++
    readonly_file_tools() ++
    core_tools() ++
    [update_goals_tool(), trigger_integration_tool(), read_charter_tool(), write_work_log_tool()]
  end

  # ================================================================
  # Executor (leaf node): full_file + executor_specific + core
  #   NO management, NO git_worktree, NO hire_agent, NO binding,
  #   NO save_charter, NO admin, NO update_goals, NO mcp_configure
  # ================================================================
  defp executor_tools do
    full_file_tools() ++
    executor_specific_tools() ++
    core_tools() ++
    [read_skill_tool()]
  end

  # Reviewer/Inspector: file read + bash (run tests) + review tools + core + read_skill
  defp executor_tools_for_reviewer do
    full_file_tools() ++
    qa_review_tools() ++
    executor_specific_tools() ++
    core_tools() ++
    [read_skill_tool()]
  end

  defp bash_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "bash",
        "description" => "Execute a shell command in the project workspace. Supports pipes, multi-line scripts.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "command" => %{"type" => "string", "description" => "Shell command"},
            "workdir" => %{"type" => "string", "description" => "Working dir"}
          },
          "required" => ["command"]
        }
      }
    }
  end

  defp read_file_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "read_file",
        "description" => "Read file with line numbers.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "filePath" => %{"type" => "string", "description" => "File path"},
            "offset" => %{"type" => "integer", "description" => "Start line"},
            "limit" => %{"type" => "integer", "description" => "Max lines"}
          },
          "required" => ["filePath"]
        }
      }
    }
  end

  defp list_files_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "list_files",
        "description" => "List files and directories in a path.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "path" => %{"type" => "string", "description" => "Dir path"}
          }
        }
      }
    }
  end

  defp grep_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "grep",
        "description" => "Search file contents with regex.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "pattern" => %{"type" => "string", "description" => "Regex pattern"},
            "path" => %{"type" => "string", "description" => "Search path"},
            "include" => %{"type" => "string", "description" => "Glob filter"}
          },
          "required" => ["pattern"]
        }
      }
    }
  end

  defp glob_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "glob",
        "description" => "Find files by glob pattern.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "pattern" => %{"type" => "string", "description" => "Glob pattern"},
            "path" => %{"type" => "string", "description" => "Search dir"}
          },
          "required" => ["pattern"]
        }
      }
    }
  end

  defp apply_patch_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "apply_patch",
        "description" => "Apply file patches (add/update/delete).",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "patches" => %{
              "type" => "array",
              "description" => "List of patches to apply",
              "items" => %{
                "type" => "object",
                "properties" => %{
                  "op" => %{"type" => "string", "enum" => ["add", "update", "delete"]},
                  "filePath" => %{"type" => "string", "description" => "File path"},
                  "content" => %{"type" => "string", "description" => "Content for add"},
                  "oldString" => %{"type" => "string", "description" => "Text to find"},
                  "newString" => %{"type" => "string", "description" => "Replacement"}
                },
                "required" => ["op", "filePath"]
              }
            }
          },
          "required" => ["patches"]
        }
      }
    }
  end

  defp todowrite_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "todowrite",
        "description" => "Update task list.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "todos" => %{
              "type" => "array",
              "items" => %{
                "type" => "object",
                "properties" => %{
                  "content" => %{"type" => "string"},
                  "status" => %{"type" => "string", "enum" => ["pending", "in_progress", "completed"]},
                  "priority" => %{"type" => "string", "enum" => ["high", "medium", "low"]}
                },
                "required" => ["content", "status"]
              }
            }
          },
          "required" => ["todos"]
        }
      }
    }
  end

  defp question_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "question",
        "description" => "Ask user a question. Blocks until answered.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "question" => %{"type" => "string", "description" => "The question to ask"}
          },
          "required" => ["question"]
        }
      }
    }
  end

  # ── MCP tools ─────────────────────────────────────────────────

  defp mcp_list_tools_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "mcp_list_tools",
        "description" => "List all tools on connected MCP servers. No parameters required.",
        "parameters" => %{"type" => "object", "properties" => %{}}
      }
    }
  end

  defp mcp_call_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "mcp_call",
        "description" => "Call a tool on an MCP server.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "server" => %{"type" => "string", "description" => "MCP server name"},
            "tool" => %{"type" => "string", "description" => "Tool name"},
            "arguments" => %{"type" => "object", "description" => "Arguments"}
          },
          "required" => ["server", "tool"]
        }
      }
    }
  end

  defp mcp_configure_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "mcp_configure",
        "description" => "Configure a new MCP server.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "name" => %{"type" => "string", "description" => "MCP server name"},
            "transport" => %{"type" => "string", "enum" => ["stdio", "http"], "description" => "stdio or http"},
            "command" => %{"type" => "string", "description" => "Command (stdio)"},
            "url" => %{"type" => "string", "description" => "URL (http)"}
          },
          "required" => ["name", "transport"]
        }
      }
    }
  end

  # ── Dispatch / communication tools ──────────────────────────

  defp dispatch_task_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "dispatch_task",
        "description" => "Assign a task to a subordinate. Auto-triggers them.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "toAgentId" => %{"type" => "string", "description" => "Subordinate name/short_id/role/UUID"},
            "description" => %{"type" => "string", "description" => "Task description for subordinate. CAVEMAN style: terse, drop articles/filler, fragments OK. Keep technical terms, file paths, exact. NOT for user."},
            "expectReport" => %{"type" => "boolean", "description" => "If true, subordinate MUST report back results (default: true)"}
          },
          "required" => ["toAgentId", "description"]
        }
      }
    }
  end

  defp hire_agent_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "hire_agent",
        "description" => "Create a new agent and start it. HR only.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "name" => %{"type" => "string", "description" => "Agent name (Chinese)"},
            "role" => %{"type" => "string", "description" => "Role title"},
            "permissionType" => %{"type" => "string", "description" => "'executor' or 'coordinator'"},
            "goal" => %{"type" => "string", "description" => "Agent goal"},
            "backstory" => %{"type" => "string", "description" => "Background (optional)"},
            "skills" => %{"type" => "string", "description" => "Skill slugs (comma-separated)"},
            "mcpServers" => %{"type" => "string", "description" => "MCP servers (comma-separated)"}
          },
          "required" => ["name", "role", "goal"]
        }
      }
    }
  end

  defp list_available_skills_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "list_available_skills",
        "description" => "Search available skills.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "search" => %{"type" => "string", "description" => "Search keyword (e.g. 'frontend', 'testing', 'code review')"}
          }
        }
      }
    }
  end

  defp get_skill_detail_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "get_skill_detail",
        "description" => "Preview skill instructions.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "slug" => %{"type" => "string", "description" => "Skill slug to preview"}
          },
          "required" => ["slug"]
        }
      }
    }
  end

  defp read_skill_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "read_skill",
        "description" => "Load full instructions of an ALREADY BOUND skill at runtime. Use this when a task matches a bound skill's description.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "slug" => %{"type" => "string", "description" => "Skill slug to read"}
          },
          "required" => ["slug"]
        }
      }
    }
  end

  defp bind_skill_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "bind_skill",
        "description" => "Bind a skill to an agent (yourself or a subordinate). The skill's instructions will be injected into the agent's prompt.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "agentId" => %{"type" => "string", "description" => "Target agent ID (your own ID or a subordinate's ID)"},
            "skillName" => %{"type" => "string", "description" => "Skill slug to bind"}
          },
          "required" => ["agentId", "skillName"]
        }
      }
    }
  end

  defp unbind_skill_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "unbind_skill",
        "description" => "Remove a skill binding from an agent.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "agentId" => %{"type" => "string", "description" => "Target agent ID"},
            "skillName" => %{"type" => "string", "description" => "Skill slug to unbind"}
          },
          "required" => ["agentId", "skillName"]
        }
      }
    }
  end

  defp list_available_mcp_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "list_available_mcp",
        "description" => "List available MCP servers that can be bound to agents.",
        "parameters" => %{"type" => "object", "properties" => %{}}
      }
    }
  end

  defp bind_mcp_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "bind_mcp",
        "description" => "Bind an MCP server to an agent, giving them access to its tools.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "agentId" => %{"type" => "string", "description" => "Target agent ID"},
            "mcpServer" => %{"type" => "string", "description" => "MCP server name to bind"}
          },
          "required" => ["agentId", "mcpServer"]
        }
      }
    }
  end

  defp unbind_mcp_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "unbind_mcp",
        "description" => "Remove an MCP server binding from an agent.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "agentId" => %{"type" => "string", "description" => "Target agent ID"},
            "mcpServer" => %{"type" => "string", "description" => "MCP server name to unbind"}
          },
          "required" => ["agentId", "mcpServer"]
        }
      }
    }
  end

  defp report_completion_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "report_completion",
        "description" => "Report that your current task is complete. Your superior will be notified.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "summary" => %{"type" => "string", "description" => "Summary of what was accomplished. CAVEMAN style: terse, drop articles/filler, fragments OK. Keep technical terms, file paths, exact. Example: 'Login done. JWT auth + bcrypt. 3 files. Tests pass.'"},
            "handoffId" => %{"type" => "string", "description" => "Specific handoff ID (optional, defaults to most recent)"}
          }
        }
      }
    }
  end

  defp message_superior_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "message_superior",
        "description" => "Send a message to your direct superior (parent agent). Use for status updates, questions, or reporting results.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "message" => %{"type" => "string", "description" => "Message content. CAVEMAN style: terse, drop articles/filler, fragments OK. Keep technical terms, file paths, exact. NOT for user."},
            "priority" => %{"type" => "string", "enum" => ["low", "normal", "urgent"], "description" => "Message priority (default: normal)"}
          },
          "required" => ["message"]
        }
      }
    }
  end

  defp send_message_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "send_message",
        "description" => "Send a message to an agent (by name/short_id/role) or 'user'.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "content" => %{"type" => "string", "description" => "Message content. If recipient is 'user': normal, complete, friendly. If recipient is an agent: CAVEMAN style — terse, drop articles/filler, fragments OK, keep technical terms exact."},
            "recipients" => %{"type" => "array", "items" => %{"type" => "string"}, "description" => "Agent name/short_id/role or 'user'"},
            "expectReport" => %{"type" => "boolean", "description" => "If true, expect a reply (default: false)"},
            "priority" => %{"type" => "string", "enum" => ["low", "normal", "urgent"], "description" => "Priority (default: normal)"}
          },
          "required" => ["content", "recipients"]
        }
      }
    }
  end

  defp read_work_logs_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "read_work_logs",
        "description" => "Read work logs. If subordinateId is provided, reads that subordinate's logs. Otherwise reads your own logs.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "subordinateId" => %{"type" => "string", "description" => "Subordinate agent ID (optional)"},
            "limit" => %{"type" => "integer", "description" => "Max logs to return (default: 10)"}
          }
        }
      }
    }
  end

  defp approve_work_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "approve_work",
        "description" => "Approve a subordinate's completed work.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "subordinateId" => %{"type" => "string", "description" => "Subordinate agent ID"},
            "review" => %{"type" => "string", "description" => "Optional review comment"}
          },
          "required" => ["subordinateId"]
        }
      }
    }
  end

  defp reject_work_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "reject_work",
        "description" => "Reject a subordinate's completed work and request rework. The subordinate will be notified.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "subordinateId" => %{"type" => "string", "description" => "Subordinate agent ID"},
            "feedback" => %{"type" => "string", "description" => "Feedback explaining what needs to be fixed"}
          },
          "required" => ["subordinateId", "feedback"]
        }
      }
    }
  end

  defp list_subordinates_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "list_subordinates",
        "description" => "List your direct subordinates with their current status, pending tasks, and recent work logs.",
        "parameters" => %{"type" => "object", "properties" => %{}}
      }
    }
  end

  defp review_code_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "review_code",
        "description" => "Review a subordinate's recent work. Reads their work logs and changed files, then provides a structured review summary.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "subordinateId" => %{"type" => "string", "description" => "The subordinate agent's ID (UUID or short ID)"},
            "limit" => %{"type" => "integer", "description" => "Max work log entries to review (default: 5)"}
          },
          "required" => ["subordinateId"]
        }
      }
    }
  end

  defp trigger_integration_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "trigger_integration",
        "description" => "Trigger integration test or build to validate merged subordinate work. Typically runs npm test / mix test / cargo test / pytest.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "command" => %{"type" => "string", "description" => "Custom test/build command (default: auto-detect based on project)"},
            "workdir" => %{"type" => "string", "description" => "Working directory for the command (default: workspace root)"}
          }
        }
      }
    }
  end

  defp write_work_log_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "write_work_log",
        "description" => "Write a work log entry to record progress, decisions, or issues.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "type" => %{"type" => "string", "enum" => ["discussion", "completion", "error", "decision"], "description" => "Log type (default: discussion)"},
            "summary" => %{"type" => "string", "description" => "Short summary of the log entry"}
          },
          "required" => ["summary"]
        }
      }
    }
  end

  defp read_project_memory_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "read_project_memory",
        "description" => "Read shared project memories (constitution). These are shared across all agents.",
        "parameters" => %{"type" => "object", "properties" => %{}}
      }
    }
  end

  defp write_memory_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "write_memory",
        "description" => "Save a memory to your private working memory. This persists across sessions.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "type" => %{"type" => "string", "enum" => ["decision", "fact", "lesson", "pattern", "preference", "progress"], "description" => "Memory type"},
            "content" => %{"type" => "string", "description" => "Memory content"}
          },
          "required" => ["type", "content"]
        }
      }
    }
  end

  # ── Git worktree tools (coordinator only) ───────────────────

  defp git_worktree_create_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "git_worktree_create",
        "description" => "Create an isolated git worktree for a subordinate agent under .hiveweave/worktrees/<shortId>/.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "shortId" => %{"type" => "string", "description" => "Short ID of the subordinate agent"},
            "taskName" => %{"type" => "string", "description" => "Task name for the branch (sluggified)"},
            "baseBranch" => %{"type" => "string", "description" => "Base branch to fork from (default: main)"}
          },
          "required" => ["shortId", "taskName"]
        }
      }
    }
  end

  defp git_worktree_checkpoint_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "git_worktree_checkpoint",
        "description" => "Save a lightweight checkpoint commit in a subordinate's worktree (git add -A && git commit).",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "shortId" => %{"type" => "string", "description" => "Short ID of the subordinate agent"},
            "message" => %{"type" => "string", "description" => "Checkpoint message describing the saved state"}
          },
          "required" => ["shortId", "message"]
        }
      }
    }
  end

  defp git_worktree_merge_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "git_worktree_merge",
        "description" => "Merge a subordinate's worktree branch into main. Only use after QA passes.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "shortId" => %{"type" => "string", "description" => "Short ID of the subordinate agent"},
            "taskName" => %{"type" => "string", "description" => "Task name matching the worktree branch"},
            "targetBranch" => %{"type" => "string", "description" => "Target branch to merge into (default: main)"}
          },
          "required" => ["shortId", "taskName"]
        }
      }
    }
  end

  defp git_worktree_rollback_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "git_worktree_rollback",
        "description" => "Reset a subordinate's worktree to a previous checkpoint (default: last checkpoint).",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "shortId" => %{"type" => "string", "description" => "Short ID of the subordinate agent"},
            "commitHash" => %{"type" => "string", "description" => "Specific commit hash to rollback to (default: last checkpoint)"}
          },
          "required" => ["shortId"]
        }
      }
    }
  end

  defp git_worktree_remove_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "git_worktree_remove",
        "description" => "Remove a subordinate's worktree and delete its branch (rejected/obsolete work).",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "shortId" => %{"type" => "string", "description" => "Short ID of the subordinate agent"},
            "taskName" => %{"type" => "string", "description" => "Task name matching the branch to delete (optional)"}
          },
          "required" => ["shortId"]
        }
      }
    }
  end

  defp git_worktree_list_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "git_worktree_list",
        "description" => "List all HiveWeave-managed worktrees with status.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{}
        }
      }
    }
  end

  defp git_worktree_status_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "git_worktree_status",
        "description" => "Show detailed status of one subordinate's worktree (branch, HEAD, checkpoints, uncommitted changes).",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "shortId" => %{"type" => "string", "description" => "Short ID of the subordinate agent"}
          },
          "required" => ["shortId"]
        }
      }
    }
  end

  # ── File operation tools ─────────────────────────────────────

  defp write_file_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "write_file",
        "description" => "Write content to a file. Creates parent directories automatically. Use append=true to add content instead of overwriting.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "filePath" => %{"type" => "string", "description" => "File path relative to workspace"},
            "content" => %{"type" => "string", "description" => "The content to write to the file"},
            "append" => %{"type" => "boolean", "description" => "If true, append content instead of overwriting (default: false)"}
          },
          "required" => ["filePath", "content"]
        }
      }
    }
  end

  defp edit_file_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "edit_file",
        "description" => "Edit a file by finding oldString and replacing with newString. oldString must match exactly once in the file.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "filePath" => %{"type" => "string", "description" => "File path relative to workspace"},
            "oldString" => %{"type" => "string", "description" => "The exact text to find in the file"},
            "newString" => %{"type" => "string", "description" => "The replacement text"}
          },
          "required" => ["filePath", "oldString", "newString"]
        }
      }
    }
  end

  defp delete_file_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "delete_file",
        "description" => "Delete a single file from the workspace.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "filePath" => %{"type" => "string", "description" => "File path relative to workspace"}
          },
          "required" => ["filePath"]
        }
      }
    }
  end

  defp move_file_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "move_file",
        "description" => "Move or rename a file or directory within the workspace.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "source" => %{"type" => "string", "description" => "Source path relative to workspace"},
            "destination" => %{"type" => "string", "description" => "Destination path relative to workspace"},
            "overwrite" => %{"type" => "boolean", "description" => "If true, overwrite destination if it exists (default: false)"}
          },
          "required" => ["source", "destination"]
        }
      }
    }
  end

  defp create_directory_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "create_directory",
        "description" => "Create a directory and any necessary parent directories.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "path" => %{"type" => "string", "description" => "Directory path relative to workspace"}
          },
          "required" => ["path"]
        }
      }
    }
  end

  defp delete_directory_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "delete_directory",
        "description" => "Delete an empty directory. Will fail if directory is not empty.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "path" => %{"type" => "string", "description" => "Directory path relative to workspace"}
          },
          "required" => ["path"]
        }
      }
    }
  end

  defp search_files_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "search_files",
        "description" => "Search file contents using regex. Returns matching lines with file paths and line numbers.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "pattern" => %{"type" => "string", "description" => "Regular expression pattern"},
            "path" => %{"type" => "string", "description" => "Search path (relative to workspace, default: root)"},
            "include" => %{"type" => "string", "description" => "Glob filter (e.g. *.ts)"}
          },
          "required" => ["pattern"]
        }
      }
    }
  end

  # ── Charter & Goals tools (A) ────────────────────────────────

  defp save_charter_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "save_charter",
        "description" => "Save or update the project charter (coordinator-only).",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "title" => %{"type" => "string", "description" => "Charter title"},
            "content" => %{"type" => "string", "description" => "Charter content"}
          },
          "required" => ["title", "content"]
        }
      }
    }
  end

  defp read_charter_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "read_charter",
        "description" => "Read the current project charter.",
        "parameters" => %{"type" => "object", "properties" => %{}}
      }
    }
  end

  defp read_goals_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "read_goals",
        "description" => "Read the enterprise goals for the project.",
        "parameters" => %{"type" => "object", "properties" => %{}}
      }
    }
  end

  defp update_goals_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "update_goals",
        "description" => "Update enterprise goals (coordinator-only).",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "objective" => %{"type" => "string", "description" => "Primary objective"},
            "focus" => %{"type" => "string", "description" => "Focus area"},
            "keyResults" => %{"type" => "array", "items" => %{"type" => "string"}, "description" => "Key results (list of strings)"}
          }
        }
      }
    }
  end

  # ── Game Time tools (B) ──────────────────────────────────────

  defp get_project_time_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "get_project_time",
        "description" => "Get the current game time (simulated clock) for the project.",
        "parameters" => %{"type" => "object", "properties" => %{}}
      }
    }
  end

  defp get_real_time_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "get_real_time",
        "description" => "Get the current real-world time.",
        "parameters" => %{"type" => "object", "properties" => %{}}
      }
    }
  end

  defp set_alarm_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "set_alarm",
        "description" => "Schedule an alarm at a game-time offset for a target agent.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "purpose" => %{"type" => "string", "description" => "Reason for the alarm"},
            "fromAgentId" => %{"type" => "string", "description" => "The agent ID setting the alarm"},
            "fireAtGameSeconds" => %{"type" => "integer", "description" => "Game-time second offset when the alarm should fire"}
          },
          "required" => ["purpose", "fromAgentId", "fireAtGameSeconds"]
        }
      }
    }
  end

  # ── HR Management tools (C) ──────────────────────────────────

  defp transfer_agent_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "transfer_agent",
        "description" => "Transfer an agent to a new parent (coordinator-only, verifies no cycle).",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "agentId" => %{"type" => "string", "description" => "Agent ID to transfer"},
            "newParentId" => %{"type" => "string", "description" => "New parent agent ID"}
          },
          "required" => ["agentId", "newParentId"]
        }
      }
    }
  end

  defp dismiss_agent_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "dismiss_agent",
        "description" => "Soft-dismiss an agent (archive) (coordinator-only, verifies no subordinates).",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "agentId" => %{"type" => "string", "description" => "Agent ID to dismiss"}
          },
          "required" => ["agentId"]
        }
      }
    }
  end

  defp update_roster_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "update_roster",
        "description" => "Update an agent's personnel record (position, department, responsibilities).",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "agentId" => %{"type" => "string", "description" => "Agent ID"},
            "position" => %{"type" => "string", "description" => "Job position"},
            "department" => %{"type" => "string", "description" => "Department name"},
            "responsibilities" => %{"type" => "string", "description" => "Role responsibilities"}
          },
          "required" => ["agentId"]
        }
      }
    }
  end

  defp read_roster_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "read_roster",
        "description" => "Read the full personnel roster for the project.",
        "parameters" => %{"type" => "object", "properties" => %{}}
      }
    }
  end

  defp list_all_agents_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "list_all_agents",
        "description" => "List all agents with their hierarchy in the project.",
        "parameters" => %{"type" => "object", "properties" => %{}}
      }
    }
  end

  defp check_agent_status_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "check_agent_status",
        "description" => "Check whether an agent is busy or idle.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "agentId" => %{"type" => "string", "description" => "Agent ID to check"}
          },
          "required" => ["agentId"]
        }
      }
    }
  end

  # ── Code Review tools (D) ────────────────────────────────────

  defp run_code_review_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "run_code_review",
        "description" => "Run a 5-axis code review (correctness, readability, architecture, security, performance) via LLM.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "filePaths" => %{"type" => "array", "items" => %{"type" => "string"}, "description" => "File paths to review (relative to workspace)"}
          },
          "required" => ["filePaths"]
        }
      }
    }
  end

  defp run_security_audit_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "run_security_audit",
        "description" => "Run an OWASP Top 10 security audit via LLM.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "filePaths" => %{"type" => "array", "items" => %{"type" => "string"}, "description" => "File paths to audit"}
          },
          "required" => ["filePaths"]
        }
      }
    }
  end

  defp run_tests_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "run_tests",
        "description" => "Analyze test quality and coverage via LLM.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "sourceFiles" => %{"type" => "array", "items" => %{"type" => "string"}, "description" => "Source files to analyze for coverage"},
            "testFiles" => %{"type" => "array", "items" => %{"type" => "string"}, "description" => "Test files to review (optional)"}
          },
          "required" => ["sourceFiles"]
        }
      }
    }
  end

  defp run_perf_audit_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "run_perf_audit",
        "description" => "Run a web performance audit via LLM (bundle size, rendering, loading, network, runtime).",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "filePaths" => %{"type" => "array", "items" => %{"type" => "string"}, "description" => "File paths to audit"}
          },
          "required" => ["filePaths"]
        }
      }
    }
  end

  defp run_full_review_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "run_full_review",
        "description" => "Run a combined review (all 4 dimensions: code, security, tests, performance) via LLM in parallel. Returns overall score and PASS/FAIL.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "filePaths" => %{"type" => "array", "items" => %{"type" => "string"}, "description" => "File paths to review"},
            "testFiles" => %{"type" => "array", "items" => %{"type" => "string"}, "description" => "Test files to review (optional)"}
          },
          "required" => ["filePaths"]
        }
      }
    }
  end

  defp websearch_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "websearch",
        "description" => "Search the web using DuckDuckGo HTML search.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "query" => %{"type" => "string", "description" => "Search query"},
            "numResults" => %{"type" => "integer", "description" => "Number of results to return (default: 5, max: 8)"}
          },
          "required" => ["query"]
        }
      }
    }
  end

  defp fetch_url_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "fetch_url",
        "description" => "Fetch content from a URL and convert to markdown.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "url" => %{"type" => "string", "description" => "The URL to fetch"},
            "format" => %{"type" => "string", "enum" => ["markdown", "text"], "description" => "Output format (default: markdown)"}
          },
          "required" => ["url"]
        }
      }
    }
  end

  # ── Model management tools (coordinator only) ───────────────

  defp list_models_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "list_models",
        "description" => "List all available LLM models and their current roles. Use switch_model to change your model.",
        "parameters" => %{"type" => "object", "properties" => %{}}
      }
    }
  end

  defp set_default_model_tool do
    %{
      "type" => "function",
      "function" => %{
        "name" => "set_default_model",
        "description" => "Set the default LLM model for a role (coordinator or executor). Use list_models first to see available model IDs.",
        "parameters" => %{
          "type" => "object",
          "properties" => %{
            "role" => %{"type" => "string", "description" => "Role: 'coordinator' or 'executor'"},
            "modelId" => %{"type" => "string", "description" => "Model ID from list_models"}
          },
          "required" => ["role", "modelId"]
        }
      }
    }
  end

  # ── Execute entry point ─────────────────────────────────────

  @doc """
  Execute a tool call. Returns {:ok, result_string} or {:error, reason}.
  The result_string is human-readable text for LLM consumption.
  """
  def execute(agent, tool_name, input, workspace_path) do
    # Strip hiveweave__ prefix if present
    name = String.replace_leading(tool_name, "hiveweave__", "")

    Logger.info("[ToolExecutor] Agent #{agent.id} calling #{name} with #{inspect(input) |> String.slice(0, 200)}")

    # Permission gating: evaluate against permission presets + glob rules
    case HiveWeave.Services.Permission.evaluate(agent, name) do
      :deny ->
        {:ok, "Permission denied: #{name} is blocked for this agent."}

      :ask ->
        # Check if there's a saved allow rule first
        saved_rules = HiveWeave.Services.Approval.load_saved_rules(agent.id)
        if HiveWeave.Services.Permission.matches_pattern?(name, saved_rules) do
          try do
            result = dispatch(name, input, workspace_path, agent)
            truncated = HiveWeave.TokenUtils.truncate_tool_output_full(result)
            {:ok, truncated}
          rescue
            e ->
              msg = "Error: #{inspect(e)}"
              Logger.error("[ToolExecutor] #{name} failed: #{msg}")
              {:ok, msg}
          end
        else
          Logger.info("[ToolExecutor] Tool #{name} requires permission for agent #{agent.id}")

          desc = "Agent #{Map.get(agent, :name, agent.id)} wants to use #{name}"
          case HiveWeave.Services.Approval.request_permission(
                 agent.id, agent.project_id, name, input, desc
               ) do
            :ok ->
              try do
                result = dispatch(name, input, workspace_path, agent)
                truncated = HiveWeave.TokenUtils.truncate_tool_output_full(result)
                {:ok, truncated}
              rescue
                e ->
                  msg = "Error: #{inspect(e)}"
                  Logger.error("[ToolExecutor] #{name} failed: #{msg}")
                  {:ok, msg}
              end

            {:error, {:rejected, reason}} ->
              {:ok, "Permission rejected: #{reason}"}

            {:error, :timeout} ->
              {:ok, "Permission request timed out (120s). The user may be away."}
          end
        end

      :allow ->
        try do
          result = dispatch(name, input, workspace_path, agent)
          truncated = HiveWeave.TokenUtils.truncate_tool_output_full(result)
          {:ok, truncated}
        rescue
          e ->
            msg = "Error: #{inspect(e)}"
            Logger.error("[ToolExecutor] #{name} failed: #{msg}")
            {:ok, msg}
        end
    end
  end

  # ── Tool dispatch ───────────────────────────────────────────

  defp dispatch("bash", input, workspace_path, agent) do
    eff_ws = get_effective_workspace(agent, workspace_path)
    command = input["command"] || ""
    workdir = input["workdir"] || ""

    if command == "" do
      "Error: command is required"
    else
      # Security: block self-destructive commands
      case check_self_destructive(command) do
        {:block, reason} ->
          "Error: Command blocked: #{reason}"

        :ok ->
          execute_bash(command, workdir, eff_ws)
      end
    end
  end

  defp dispatch("read_file", input, workspace_path, agent) do
    eff_ws = get_effective_workspace(agent, workspace_path)
    file_path = input["filePath"] || ""
    offset = input["offset"] || 0
    limit = input["limit"] || 2000

    execute_read_file(file_path, offset, limit, eff_ws)
  end

  defp dispatch("list_files", input, workspace_path, agent) do
    eff_ws = get_effective_workspace(agent, workspace_path)
    path = input["path"] || ""
    execute_list_files(path, eff_ws)
  end

  defp dispatch("grep", input, workspace_path, agent) do
    eff_ws = get_effective_workspace(agent, workspace_path)
    pattern = input["pattern"] || ""
    path = input["path"] || ""
    include = input["include"]

    execute_grep(pattern, path, include, eff_ws)
  end

  defp dispatch("apply_patch", input, workspace_path, agent) do
    eff_ws = get_effective_workspace(agent, workspace_path)
    patches = cond do
      # Standard format: patches array
      is_list(input["patches"]) -> input["patches"]

      # LLM passed direct parameters (common with some models)
      is_binary(input["filePath"]) ->
        op = input["op"] || cond do
          Map.has_key?(input, "oldString") -> "update"
          Map.has_key?(input, "content") -> "add"
          true -> "add"
        end
        [Map.merge(input, %{"op" => op})]

      true -> []
    end
    execute_apply_patch(patches, eff_ws)
  end

  defp dispatch("todowrite", input, _workspace_path, agent) do
    todos = input["todos"] || []
    execute_todowrite(todos, agent)
  end

  defp dispatch("question", input, _workspace_path, agent) do
    question = input["question"] || ""
    execute_question(question, agent)
  end

  # ── Dispatch / communication tools ──────────────────────────

  defp dispatch("dispatch_task", input, _workspace_path, agent) do
    to_agent_id = resolve_agent_id(input["toAgentId"], agent)
    description = input["description"] || ""
    expect_report = Map.get(input, "expectReport", true)

    if to_agent_id == nil or description == "" do
      "Error: toAgentId and description are required"
    else
      project_id = agent.project_id
      session_id = "session_#{System.system_time(:millisecond)}"

      # Add game time prefix to inter-agent message content
      time_prefix = try do
        game_seconds = HiveWeave.GameTime.Server.get_current_time(project_id)
        day = div(game_seconds, 86400)
        hours = div(rem(game_seconds, 86400), 3600)
        minutes = div(rem(game_seconds, 3600), 60)
        "[D#{day} #{String.pad_leading(Integer.to_string(hours), 2, "0")}:#{String.pad_leading(Integer.to_string(minutes), 2, "0")}] "
      rescue
        _ -> ""
      end
      prefixed_content = time_prefix <> description

      {:ok, _} = HiveWeave.Services.Dispatch.dispatch_task(project_id, agent.id, to_agent_id, description, session_id)

      # create_handoff now deduplicates: returns {:ok, id} and a flag indicating if it was a dup
      case HiveWeave.Services.Handoff.create_handoff(project_id, agent.id, to_agent_id, description, expect_report: expect_report) do
        {:ok, _handoff_id} ->
          # Record team chat messages (visible in "团队沟通" panel)
          # Outgoing record on sender's side, incoming record on recipient's side
          HiveWeave.Services.TeamChat.record_message(agent.id, agent.id, to_agent_id, prefixed_content)
          HiveWeave.Services.TeamChat.record_message(to_agent_id, agent.id, to_agent_id, prefixed_content)

          # Broadcast dispatch event for frontend animation
          Phoenix.PubSub.broadcast(HiveWeave.PubSub, "project:#{project_id}", {:dispatch, %{from: agent.id, to: to_agent_id, description: description}})

          # Trigger the subordinate agent asynchronously
          HiveWeave.Agents.Agent.trigger_subordinate(to_agent_id)

          "Task dispatched to agent #{to_agent_id}. They will begin working on it automatically."

        {:error, reason} ->
          "Error dispatching task: #{inspect(reason)}"
      end
    end
  end

  defp dispatch("hire_agent", input, _workspace_path, agent) do
    # 运行时角色检查 — 只有 HR 能招聘
    unless agent.role && String.downcase(agent.role) == "hr" do
      raise "Permission denied: only HR can hire agents"
    end

    name = input["name"] || ""
    role = input["role"] || ""
    goal = input["goal"] || ""
    backstory = input["backstory"] || ""
    permission_type = input["permissionType"] || "executor"

    # Parse skills and MCP servers from comma-separated strings
    initial_skills = parse_comma_list(input["skills"])
    initial_mcp = parse_comma_list(input["mcpServers"])

    if name == "" or role == "" or goal == "" do
      "Error: name, role, and goal are required"
    else
      # 中文名校验
      unless contains_chinese?(name) do
        raise "Agent name must contain Chinese characters (花名). Example: 折纸, 拾光, 鹿鸣"
      end

      project_id = agent.project_id
      agent_id = Ecto.UUID.generate()
      short_id = HiveWeave.Services.Org.generate_short_id()

      # 如果没有传 parent_id，默认挂到 CEO 下而非 HR 自己
      parent_id = case input["parentId"] || input["parent_id"] do
        nil ->
          # 默认挂到 CEO 下
          case HiveWeave.Services.Org.get_agent_by_role(project_id, "ceo") do
            %{} = ceo -> ceo.id
            nil -> agent.id  # 如果没有 CEO，回退到调用者
          end
        pid -> resolve_agent_id(pid, agent)
      end

      attrs = %{
        id: agent_id,
        project_id: project_id,
        name: name,
        role: role,
        parent_id: parent_id,
        permission_type: permission_type,
        goal: goal,
        backstory: backstory,
        status: "active",
        model_id: resolve_default_model_for_role(permission_type),
        short_id: short_id,
        skills: Jason.encode!(initial_skills),
        bound_skills: Jason.encode!(initial_skills),
        mcp_servers: Jason.encode!(initial_mcp),
        created_at: System.system_time(:second),
        updated_at: System.system_time(:second)
      }

      case HiveWeave.Services.Org.create_agent(attrs) do
        {:ok, new_agent} ->
          # Start the agent GenServer
          case HiveWeave.Agents.AgentSupervisor.start_agent(project_id, new_agent) do
            {:ok, _pid} ->
              # Resolve parent name for broadcast (parent may be CEO, not HR)
              parent_name = case HiveWeave.Services.Org.get_agent(parent_id) do
                %{} = pa -> pa.name || agent.name
                nil -> agent.name
              end

              # Broadcast hire event for frontend
              Phoenix.PubSub.broadcast(HiveWeave.PubSub, "project:#{project_id}", {:agent_hired, %{
                agentId: agent_id,
                name: name,
                role: role,
                short_id: short_id,
                parent_id: parent_id,
                parent_name: parent_name
              }})

              # Also broadcast to lobby for Live Activity
              Phoenix.PubSub.broadcast(HiveWeave.PubSub, "lobby:status", {:activity, %{
                agentId: agent.id,
                agentName: agent.name,
                type: "hire_agent",
                content: "Hired #{name} (#{role}) — ID: #{agent_id}",
                toolName: "hire_agent",
                timestamp: System.system_time(:millisecond)
              }})

              Logger.info("[ToolExecutor] Agent #{agent.name} hired new agent: #{name} (#{role}), ID: #{agent_id}, skills: #{inspect(initial_skills)}, mcp: #{inspect(initial_mcp)}")

              # 自动创建 Roster 记录
              try do
                HiveWeave.Services.Roster.update_roster(project_id, agent_id, %{
                  position: input["role"] || input["position"] || "",
                  department: input["department"] || "",
                  responsibilities: input["goal"] || ""
                })
              rescue
                _ -> :ok  # Roster 创建失败不影响 agent 创建
              end

              skill_info = if initial_skills != [], do: ", Skills: #{Enum.join(initial_skills, ", ")}", else: ""
              mcp_info = if initial_mcp != [], do: ", MCP: #{Enum.join(initial_mcp, ", ")}", else: ""

              "Successfully hired #{name} as #{role} (ID: #{agent_id}, Short ID: #{short_id}#{skill_info}#{mcp_info}). " <>
              "The new agent has been started and is ready to receive tasks. Use dispatch_task to assign work to them."

            {:error, reason} ->
              Logger.error("[ToolExecutor] Failed to start agent GenServer for #{name}: #{inspect(reason)}")
              "Agent #{name} was created in database but failed to start process: #{inspect(reason)}"
          end

        {:error, changeset} ->
          errors = changeset.errors |> Enum.map(fn {field, {msg, _}} -> "#{field}: #{msg}" end) |> Enum.join(", ")
          "Error: Failed to create agent — #{errors}"
      end
    end
  end

  # ── Skill & MCP management tools ─────────────────────────────

  defp dispatch("list_available_skills", input, _workspace_path, _agent) do
    search = input["search"]
    HiveWeave.SkillRegistry.list_available_skills(search)
  end

  defp dispatch("get_skill_detail", input, _workspace_path, _agent) do
    slug = input["slug"]
    if not is_binary(slug) or slug == "" do
      "Error: get_skill_detail requires a slug parameter."
    else
      HiveWeave.SkillRegistry.get_skill_detail(slug)
    end
  end

  defp dispatch("read_skill", input, _workspace_path, agent) do
    slug = input["slug"]
    if not is_binary(slug) or slug == "" do
      "Error: read_skill requires a slug parameter."
    else
      bound = parse_json_list(agent.bound_skills)
      HiveWeave.SkillRegistry.read_skill(slug, bound)
    end
  end

  defp dispatch("bind_skill", input, _workspace_path, agent) do
    target_id = String.trim(input["agentId"] || "")
    skill_name = input["skillName"]

    if target_id == "" or not is_binary(skill_name) or skill_name == "" do
      "Error: bind_skill requires agentId and skillName."
    else
      case resolve_and_update_agent(target_id, agent, fn a ->
        bound = parse_json_list(a.bound_skills)
        if skill_name in bound do
          {:error, "Skill '#{skill_name}' is already bound to this agent."}
        else
          new_bound = bound ++ [skill_name]
          {Map.put(a, :bound_skills, Jason.encode!(new_bound)), "Bound skill '#{skill_name}' to agent #{a.name}. Bound skills: #{Enum.join(new_bound, ", ")}"}
        end
      end) do
        {:ok, msg} -> msg
        {:error, msg} -> msg
      end
    end
  end

  defp dispatch("unbind_skill", input, _workspace_path, agent) do
    target_id = String.trim(input["agentId"] || "")
    skill_name = input["skillName"]

    if target_id == "" or not is_binary(skill_name) or skill_name == "" do
      "Error: unbind_skill requires agentId and skillName."
    else
      case resolve_and_update_agent(target_id, agent, fn a ->
        bound = parse_json_list(a.bound_skills)
        if skill_name not in bound do
          {:error, "Skill '#{skill_name}' is not bound to this agent."}
        else
          new_bound = List.delete(bound, skill_name)
          {Map.put(a, :bound_skills, Jason.encode!(new_bound)), "Unbound skill '#{skill_name}' from agent #{a.name}. Remaining skills: #{if new_bound == [], do: "none", else: Enum.join(new_bound, ", ")}"}
        end
      end) do
        {:ok, msg} -> msg
        {:error, msg} -> msg
      end
    end
  end

  defp dispatch("list_available_mcp", _input, _workspace_path, _agent) do
    HiveWeave.SkillRegistry.list_available_mcp()
  end

  defp dispatch("bind_mcp", input, _workspace_path, agent) do
    target_id = String.trim(input["agentId"] || "")
    mcp_server = input["mcpServer"]

    if target_id == "" or not is_binary(mcp_server) or mcp_server == "" do
      "Error: bind_mcp requires agentId and mcpServer."
    else
      case resolve_and_update_agent(target_id, agent, fn a ->
        mcp_list = parse_json_list(a.mcp_servers)
        if mcp_server in mcp_list do
          {:error, "MCP server '#{mcp_server}' is already bound to this agent."}
        else
          new_mcp = mcp_list ++ [mcp_server]
          {Map.put(a, :mcp_servers, Jason.encode!(new_mcp)), "Bound MCP server '#{mcp_server}' to agent #{a.name}. Bound MCP servers: #{Enum.join(new_mcp, ", ")}"}
        end
      end) do
        {:ok, msg} -> msg
        {:error, msg} -> msg
      end
    end
  end

  defp dispatch("unbind_mcp", input, _workspace_path, agent) do
    target_id = String.trim(input["agentId"] || "")
    mcp_server = input["mcpServer"]

    if target_id == "" or not is_binary(mcp_server) or mcp_server == "" do
      "Error: unbind_mcp requires agentId and mcpServer."
    else
      case resolve_and_update_agent(target_id, agent, fn a ->
        mcp_list = parse_json_list(a.mcp_servers)
        if mcp_server not in mcp_list do
          {:error, "MCP server '#{mcp_server}' is not bound to this agent."}
        else
          new_mcp = List.delete(mcp_list, mcp_server)
          {Map.put(a, :mcp_servers, Jason.encode!(new_mcp)), "Unbound MCP server '#{mcp_server}' from agent #{a.name}. Remaining: #{if new_mcp == [], do: "none", else: Enum.join(new_mcp, ", ")}"}
        end
      end) do
        {:ok, msg} -> msg
        {:error, msg} -> msg
      end
    end
  end

  defp dispatch("report_completion", input, _workspace_path, agent) do
    summary = input["summary"] || "Task completed"
    handoff_id = input["handoffId"]
    project_id = agent.project_id
    session_id = "session_#{System.system_time(:millisecond)}"

    {:ok, _} = HiveWeave.Services.Dispatch.write_work_log(project_id, agent.id, session_id, "completion", summary)
    {:ok, result} = HiveWeave.Services.Handoff.complete_handoff(project_id, agent.id, handoff_id)

    # Trigger superior to review
    parent_id = get_parent_id(agent)
    if parent_id do
      HiveWeave.Agents.Agent.trigger_coordinator(parent_id)
    end

    "Completion reported#{if result.completed, do: " (handoff marked completed)", else: " (no active handoff found)"}. Your superior has been notified."
  end

  defp dispatch("message_superior", input, _workspace_path, agent) do
    message = input["message"] || ""
    priority = input["priority"] || "normal"
    parent_id = get_parent_id(agent)

    if parent_id == nil do
      "Warning: You have no superior to message. You are at the top of the hierarchy."
    else
      project_id = agent.project_id

      # Add game time prefix to inter-agent message content
      time_prefix = try do
        game_seconds = HiveWeave.GameTime.Server.get_current_time(project_id)
        day = div(game_seconds, 86400)
        hours = div(rem(game_seconds, 86400), 3600)
        minutes = div(rem(game_seconds, 3600), 60)
        "[D#{day} #{String.pad_leading(Integer.to_string(hours), 2, "0")}:#{String.pad_leading(Integer.to_string(minutes), 2, "0")}] "
      rescue
        _ -> ""
      end
      prefixed_content = time_prefix <> message

      opts = %{priority: priority}
      {:ok, _} = HiveWeave.Services.Inbox.send_message(agent.id, parent_id, "superior", message, opts)

      # Record team chat messages (visible in "团队沟通" panel)
      # Outgoing record on sender's side, incoming record on superior's side
      HiveWeave.Services.TeamChat.record_message(agent.id, agent.id, parent_id, prefixed_content)
      HiveWeave.Services.TeamChat.record_message(parent_id, agent.id, parent_id, prefixed_content)

      # Mark handoffs as reported up
      HiveWeave.Services.Handoff.mark_reported_up(project_id, agent.id)

      # Trigger superior to process the message
      HiveWeave.Agents.Agent.trigger_coordinator(parent_id)

      "Message sent to your superior (priority: #{priority})."
    end
  end

  defp dispatch("send_message", input, _workspace_path, agent) do
    content = input["content"] || ""
    recipients = input["recipients"] || []
    expect_report = Map.get(input, "expectReport", false)
    priority = input["priority"] || "normal"
    project_id = agent.project_id

    # Add game time prefix to inter-agent messages
    time_prefix = try do
      game_seconds = HiveWeave.GameTime.Server.get_current_time(project_id)
      day = div(game_seconds, 86400)
      hours = div(rem(game_seconds, 86400), 3600)
      minutes = div(rem(game_seconds, 3600), 60)
      "[D#{day} #{String.pad_leading(Integer.to_string(hours), 2, "0")}:#{String.pad_leading(Integer.to_string(minutes), 2, "0")}] "
    rescue
      _ -> ""
    end
    prefixed_content = time_prefix <> content

    results = Enum.map(recipients, fn r ->
      cond do
        r == "user" ->
          # Save to chat_messages as assistant message (visible in user's chat window)
          msg_id = Ecto.UUID.generate()
          now_ms = System.system_time(:millisecond)
          sql = """
          INSERT INTO chat_messages (id, agent_id, role, content, tool_calls, is_background, is_read, is_streaming, created_at)
          VALUES (?, ?, 'assistant', ?, '[]', 0, 0, 0, ?)
          """
          HiveWeave.Repo.ProjectFactory.query_for_agent(agent.id, sql, [msg_id, agent.id, "📩 #{content}", now_ms])

          # Log user_ping event for the ping badge in org tree
          HiveWeave.EventAudit.log(agent.id, "user_ping", %{from: agent.id, message: content})

          # Also broadcast via PubSub for real-time UI updates
          Phoenix.PubSub.broadcast(HiveWeave.PubSub, "project:#{project_id}", {:user_ping, %{from: agent.id, message: content, agent_id: agent.id}})

          "user: notified"

        true ->
          resolved_id = resolve_agent_id(r, agent)
          if resolved_id do
            if resolved_id == agent.id do
              "⚠️ #{r}: Cannot send message to yourself — skipping self-send"
            else
              opts = %{priority: priority, expect_report: expect_report, message_type: "peer"}
              {:ok, _} = HiveWeave.Services.Inbox.send_message(agent.id, resolved_id, "peer", prefixed_content, opts)

              # Record team chat messages (visible in "团队沟通" panel)
              # Outgoing record on sender's side
              HiveWeave.Services.TeamChat.record_message(agent.id, agent.id, resolved_id, prefixed_content)
              # Incoming record on recipient's side
              HiveWeave.Services.TeamChat.record_message(resolved_id, agent.id, resolved_id, prefixed_content)

              # Trigger recipient if it's a coordinator
              trigger_agent(resolved_id)

              "#{r}: sent"
            end
          else
            "#{r}: agent not found"
          end
      end
    end)

    "Message sent to: #{Enum.join(results, ", ")}"
  end

  defp dispatch("read_work_logs", input, _workspace_path, agent) do
    subordinate_id = input["subordinateId"]
    limit = input["limit"] || 10
    project_id = agent.project_id

    resolved_id = if subordinate_id, do: resolve_agent_id(subordinate_id, agent), else: agent.id

    logs = HiveWeave.Services.Dispatch.get_subordinate_logs(project_id, resolved_id, limit)

    if logs == [] do
      "No work logs found."
    else
      formatted = Enum.map(logs, fn log ->
        "[#{log.type}] #{log.summary} (#{format_time(log.created_at)})"
      end)
      Enum.join(formatted, "\n")
    end
  end

  defp dispatch("approve_work", input, _workspace_path, agent) do
    subordinate_id = resolve_agent_id(input["subordinateId"], agent)
    review = input["review"]
    project_id = agent.project_id
    session_id = "session_#{System.system_time(:millisecond)}"

    if subordinate_id == nil do
      "Error: subordinateId is required"
    else
      {:ok, _} = HiveWeave.Services.Dispatch.approve_work(project_id, agent.id, session_id, subordinate_id, review)
      {:ok, result} = HiveWeave.Services.Handoff.approve_handoff(project_id, agent.id, subordinate_id)

      if result.approved do
        "Work approved for agent #{subordinate_id}."
      else
        "No completed work found to approve for agent #{subordinate_id}."
      end
    end
  end

  defp dispatch("reject_work", input, _workspace_path, agent) do
    subordinate_id = resolve_agent_id(input["subordinateId"], agent)
    feedback = input["feedback"] || "Rework required"
    project_id = agent.project_id
    session_id = "session_#{System.system_time(:millisecond)}"

    if subordinate_id == nil do
      "Error: subordinateId is required"
    else
      {:ok, _} = HiveWeave.Services.Dispatch.reject_work(project_id, agent.id, session_id, subordinate_id, feedback)
      {:ok, _} = HiveWeave.Services.Handoff.reopen_handoff(project_id, agent.id, subordinate_id)

      # Send rework notification via inbox
      rework_msg = "[REWORK REQUESTED] #{feedback}"
      {:ok, _} = HiveWeave.Services.Inbox.send_message(agent.id, subordinate_id, "superior", rework_msg, %{priority: "urgent", expect_report: true})

      # Trigger subordinate to do rework
      HiveWeave.Agents.Agent.trigger_subordinate(subordinate_id)

      "Work rejected for agent #{subordinate_id}. Rework notification sent."
    end
  end

  defp dispatch("list_subordinates", _input, _workspace_path, agent) do
    project_id = agent.project_id
    children = HiveWeave.Services.Org.get_children(project_id, agent.id)

    if children == [] do
      "You have no direct subordinates."
    else
      entries = Enum.map(children, fn child ->
        pending = HiveWeave.Services.Handoff.get_pending_handoffs(project_id, child.id)
        accepted = HiveWeave.Services.Handoff.get_accepted_handoffs(project_id, child.id)
        logs = HiveWeave.Services.Dispatch.get_subordinate_logs(project_id, child.id, 3)

        status = cond do
          length(pending) > 0 -> "has #{length(pending)} pending task(s)"
          length(accepted) > 0 -> "working on #{length(accepted)} task(s)"
          true -> "idle"
        end

        recent_logs = if logs == [], do: "  (no recent logs)", else: Enum.map(logs, fn l -> "  [#{l.type}] #{l.summary}" end) |> Enum.join("\n")

        "#{child.name} (#{child.role}) — ID: #{child.id} — #{status}\n#{recent_logs}"
      end)

      Enum.join(entries, "\n---\n")
    end
  end

  defp dispatch("write_work_log", input, _workspace_path, agent) do
    type = input["type"] || "discussion"
    summary = input["summary"] || ""
    project_id = agent.project_id
    session_id = "session_#{System.system_time(:millisecond)}"

    {:ok, _} = HiveWeave.Services.Dispatch.write_work_log(project_id, agent.id, session_id, type, summary)
    "Work log recorded: [#{type}] #{summary}"
  end

  defp dispatch("read_project_memory", _input, _workspace_path, agent) do
    memories = HiveWeave.Services.Memory.get_project_memories(agent.project_id)

    if memories == [] do
      "No project memories found."
    else
      formatted = Enum.map(memories, fn m ->
        "[#{m.type}] #{String.slice(m.content || "", 0, 200)}"
      end)
      Enum.join(formatted, "\n---\n")
    end
  end

  defp dispatch("write_memory", input, _workspace_path, agent) do
    type = input["type"] || "fact"
    content = input["content"] || ""

    {:ok, id} = HiveWeave.Services.Memory.write_memory(agent.project_id,
      agent_id: agent.id,
      scope: "agent",
      type: type,
      content: content,
      source_agent_id: agent.id
    )

    "Memory saved (id: #{id}, type: #{type}). This will persist across sessions."
  end

  # ── Git worktree tools (coordinator only) ────────────────────

  defp dispatch("git_worktree_create", input, workspace_path, _agent) do
    short_id = input["shortId"] || ""
    task_name = input["taskName"] || ""
    base_branch = input["baseBranch"] || "main"

    if short_id == "" or task_name == "" do
      "Error: shortId and taskName are required"
    else
      case HiveWeave.Services.GitWorktree.create(workspace_path, short_id, task_name, base_branch) do
        {:ok, %{path: path, branch: branch}} ->
          "Worktree created. Path: #{path}, Branch: #{branch}"
        {:error, reason} ->
          "Error: #{reason}"
      end
    end
  end

  defp dispatch("git_worktree_checkpoint", input, workspace_path, _agent) do
    short_id = input["shortId"] || ""
    message = input["message"] || ""

    if short_id == "" or message == "" do
      "Error: shortId and message are required"
    else
      case HiveWeave.Services.GitWorktree.checkpoint(workspace_path, short_id, message) do
        {:ok, %{hash: hash, count: count}} ->
          "Checkpoint saved. Hash: #{hash}, Total checkpoints: #{count}"
        {:error, reason} ->
          "Error: #{reason}"
      end
    end
  end

  defp dispatch("git_worktree_merge", input, workspace_path, agent) do
    short_id = input["shortId"] || ""
    task_name = input["taskName"] || ""
    target_branch = input["targetBranch"] || "main"

    if short_id == "" or task_name == "" do
      "Error: shortId and taskName are required"
    else
      case HiveWeave.Services.GitWorktree.merge(workspace_path, short_id, task_name, target_branch) do
        {:ok, %{merged: true, hash: hash}} ->
          # Auto-update goals: mark matching keyResult as done
          maybe_update_goal_status(agent.project_id, task_name)
          "Merge successful. Merged into #{target_branch} at #{hash}. Worktree cleaned up."
        {:error, reason} ->
          "Error: #{reason}"
      end
    end
  end

  defp dispatch("git_worktree_rollback", input, workspace_path, _agent) do
    short_id = input["shortId"] || ""
    commit_hash = input["commitHash"]

    if short_id == "" do
      "Error: shortId is required"
    else
      case HiveWeave.Services.GitWorktree.rollback(workspace_path, short_id, commit_hash) do
        {:ok, %{hash: hash, message: msg}} ->
          "Rollback complete. Now at #{hash}: #{msg}"
        {:error, reason} ->
          "Error: #{reason}"
      end
    end
  end

  defp dispatch("git_worktree_remove", input, workspace_path, _agent) do
    short_id = input["shortId"] || ""
    task_name = input["taskName"]

    if short_id == "" do
      "Error: shortId is required"
    else
      case HiveWeave.Services.GitWorktree.remove(workspace_path, short_id, task_name) do
        {:ok, %{removed: true}} ->
          "Worktree removed for #{short_id}."
        {:error, reason} ->
          "Error: #{reason}"
      end
    end
  end

  defp dispatch("git_worktree_list", _input, workspace_path, _agent) do
    case HiveWeave.Services.GitWorktree.list(workspace_path) do
      {:ok, entries} when entries == [] ->
        "No HiveWeave-managed worktrees found."
      {:ok, entries} ->
        formatted = Enum.map(entries, fn e ->
          status = if e.active, do: "active", else: "inactive"
          "#{e.short_id} | #{e.branch || "(detached)"} | #{e.head} | #{status}"
        end)
        Enum.join(formatted, "\n")
      {:error, reason} ->
        "Error: #{reason}"
    end
  end

  defp dispatch("git_worktree_status", input, workspace_path, _agent) do
    short_id = input["shortId"] || ""

    if short_id == "" do
      "Error: shortId is required"
    else
      case HiveWeave.Services.GitWorktree.status(workspace_path, short_id) do
        {:ok, nil} ->
          "No worktree found for #{short_id}."
        {:ok, s} ->
          checkpoints_str = if s.checkpoints == [] do
            "  (none)"
          else
            Enum.map(s.checkpoints, fn cp ->
              "  #{cp.hash} #{cp.date} #{cp.message}"
            end) |> Enum.join("\n")
          end

          uncommitted = if s.has_uncommitted, do: " (uncommitted changes)", else: ""

          "Worktree #{s.short_id}\n" <>
          "  Branch: #{s.branch}\n" <>
          "  HEAD:   #{s.head}#{uncommitted}\n" <>
          "  Checkpoints:\n#{checkpoints_str}"
        {:error, reason} ->
          "Error: #{reason}"
      end
    end
  end

  # ── File operation tools ──────────────────────────────────────

  defp dispatch("write_file", input, workspace_path, agent) do
    eff_ws = get_effective_workspace(agent, workspace_path)
    file_path = input["filePath"] || ""
    content = input["content"] || ""
    append = Map.get(input, "append", false)

    execute_write_file(file_path, content, append, eff_ws)
  end

  defp dispatch("edit_file", input, workspace_path, agent) do
    eff_ws = get_effective_workspace(agent, workspace_path)
    file_path = input["filePath"] || ""
    old_string = input["oldString"] || ""
    new_string = input["newString"] || ""

    if file_path == "" or old_string == "" do
      "Error: filePath and oldString are required"
    else
      execute_edit_file(file_path, old_string, new_string, eff_ws)
    end
  end

  defp dispatch("delete_file", input, workspace_path, agent) do
    eff_ws = get_effective_workspace(agent, workspace_path)
    file_path = input["filePath"] || ""

    execute_delete_file(file_path, eff_ws)
  end

  defp dispatch("move_file", input, workspace_path, agent) do
    eff_ws = get_effective_workspace(agent, workspace_path)
    source = input["source"] || ""
    destination = input["destination"] || ""
    overwrite = Map.get(input, "overwrite", false)

    execute_move_file(source, destination, overwrite, eff_ws)
  end

  defp dispatch("create_directory", input, workspace_path, agent) do
    eff_ws = get_effective_workspace(agent, workspace_path)
    path = input["path"] || ""

    execute_create_directory(path, eff_ws)
  end

  defp dispatch("delete_directory", input, workspace_path, agent) do
    eff_ws = get_effective_workspace(agent, workspace_path)
    path = input["path"] || ""

    execute_delete_directory(path, eff_ws)
  end

  defp dispatch("search_files", input, workspace_path, _agent) do
    pattern = input["pattern"] || ""
    path = input["path"] || ""
    include = input["include"]

    execute_search_files(pattern, path, include, workspace_path)
  end

  # ── Charter & Goals dispatch (A) ─────────────────────────

  defp dispatch("save_charter", input, _workspace_path, agent) do
    # 运行时角色检查 — 只有 CEO 能保存章程
    unless agent.role && String.downcase(agent.role) == "ceo" do
      raise "Permission denied: only CEO can save charter"
    end

    title = input["title"] || ""
    content = input["content"] || ""
    if title == "" or content == "" do
      "Error: title and content are required"
    else
      case HiveWeave.Services.Charter.save_charter(agent.project_id, agent.id, %{title: title, content: content, status: "active"}) do
        {:ok, _charter} -> "Charter saved successfully: #{title}"
        {:error, reason} -> "Error: #{inspect(reason)}"
      end
    end
  end

  defp dispatch("read_charter", _input, _workspace_path, agent) do
    # 运行时角色检查 — 只有 CEO 和 HR 能读章程
    role = if agent.role, do: String.downcase(agent.role), else: ""
    unless role in ["ceo", "hr", "coordinator"] do
      raise "Permission denied: only CEO, HR, and coordinators can read charter"
    end

    case HiveWeave.Services.Charter.read_charter(agent.project_id) do
      {:ok, charter} ->
        "### #{charter.title}\n\n#{charter.content}\n\nStatus: #{charter.status}"
      nil -> "No charter found for this project."
      {:error, reason} -> "Error: #{inspect(reason)}"
    end
  end

  defp dispatch("read_goals", _input, _workspace_path, agent) do
    case HiveWeave.Services.Charter.read_goals(agent.project_id) do
      {:ok, nil} -> "No enterprise goals found for this project."
      {:ok, goals} when is_map(goals) ->
        objective = Map.get(goals, "objective", Map.get(goals, :objective, ""))
        focus = Map.get(goals, "focus", Map.get(goals, :focus, ""))
        krs = Map.get(goals, "keyResults", Map.get(goals, :keyResults, []))
        kr_text = Enum.map(krs, fn kr ->
          cond do
            is_binary(kr) -> "  - #{kr}"
            is_map(kr) -> "  - #{Map.get(kr, "text", Map.get(kr, :text, ""))} [#{Map.get(kr, "status", Map.get(kr, :status, "todo"))}]"
            true -> "  - #{kr}"
          end
        end) |> Enum.join("\n")
        "Objective: #{objective || "(none)"}\n\nFocus: #{focus || "(none)"}\n\nKey Results:\n#{kr_text}"
      {:ok, goals} when is_binary(goals) ->
        goals
      {:error, reason} -> "Error: #{inspect(reason)}"
    end
  end

  defp dispatch("update_goals", input, _workspace_path, agent) do
    # 运行时角色检查
    role = if agent.role, do: String.downcase(agent.role), else: ""
    unless role in ["ceo", "coordinator", "manager"] do
      raise "Permission denied: only CEO and coordinators can update goals"
    end

    objective = input["objective"]
    focus = input["focus"]
    key_results = input["keyResults"]

    if is_nil(objective) and is_nil(focus) and is_nil(key_results) do
      "Error: at least one of objective, focus, or keyResults is required"
    else
      attrs = %{}
      attrs = if objective, do: Map.put(attrs, :objective, objective), else: attrs
      attrs = if focus, do: Map.put(attrs, :focus, focus), else: attrs
      attrs = if key_results, do: Map.put(attrs, :key_results, key_results), else: attrs

      case HiveWeave.Services.Charter.update_goals(agent.project_id, attrs) do
        {:ok, _goals} -> "Enterprise goals updated."
        {:error, reason} -> "Error: #{inspect(reason)}"
      end
    end
  end

  # ── Game Time dispatch (B) ──────────────────────────────

  defp dispatch("get_project_time", _input, _workspace_path, agent) do
    game_seconds = HiveWeave.GameTime.Server.get_current_time(agent.project_id)
    days = div(game_seconds, 86400)
    hours = div(rem(game_seconds, 86400), 3600)
    minutes = div(rem(game_seconds, 3600), 60)
    "Current project time: Day #{days}, #{hours}:#{String.pad_leading(Integer.to_string(minutes), 2, "0")} (game seconds: #{game_seconds})"
  end

  defp dispatch("get_real_time", _input, _workspace_path, _agent) do
    now = DateTime.utc_now()
    "Current real-world time: #{now.year}-#{String.pad_leading(Integer.to_string(now.month), 2, "0")}-#{String.pad_leading(Integer.to_string(now.day), 2, "0")} #{String.pad_leading(Integer.to_string(now.hour), 2, "0")}:#{String.pad_leading(Integer.to_string(now.minute), 2, "0")}:#{String.pad_leading(Integer.to_string(now.second), 2, "0")} UTC"
  end

  defp dispatch("set_alarm", input, _workspace_path, agent) do
    purpose = input["purpose"] || ""
    from_agent = input["fromAgentId"] || ""
    fire_at = input["fireAtGameSeconds"]

    if purpose == "" or from_agent == "" or is_nil(fire_at) do
      "Error: purpose, fromAgentId, and fireAtGameSeconds are required"
    else
      alarm = %{
        id: Ecto.UUID.generate(),
        project_id: agent.project_id,
        from_agent_id: from_agent,
        to_agent_id: agent.id,
        purpose: purpose,
        fire_at_game_seconds: fire_at
      }
      case HiveWeave.GameTime.Server.schedule_alarm(agent.project_id, alarm) do
        {:ok, alarm_id} -> "Alarm scheduled (id: #{alarm_id}). Will fire at game second #{fire_at}."
        _ -> "Alarm scheduled."
      end
    end
  end

  # ── HR Management dispatch (C) ──────────────────────────

  defp dispatch("transfer_agent", input, _workspace_path, agent) do
    # 运行时角色检查 — 只有 HR 能转移 agent
    unless agent.role && String.downcase(agent.role) == "hr" do
      raise "Permission denied: only HR can transfer agents"
    end

    project_id = agent.project_id
    target_id = resolve_agent_id(input["agentId"], agent)
    new_parent_id = resolve_agent_id(input["newParentId"] || input["parent_id"], agent)

    if is_nil(target_id) or is_nil(new_parent_id) do
      "Error: agentId and newParentId are required"
    else
      # 禁止转移自己
      if target_id == agent.id do
        raise "HR cannot transfer themselves"
      end

      # 循环检测：检查新 parent 是否是 agent 的后代（防止创建环）
      descendants = HiveWeave.Services.Org.get_all_descendants(project_id, target_id)
      descendant_ids = Enum.map(descendants, & &1.id)
      if new_parent_id in descendant_ids do
        raise "Cannot transfer agent to its own descendant — this would create a cycle"
      end
      if new_parent_id == target_id do
        raise "Cannot transfer agent to be its own parent"
      end

      case HiveWeave.Services.Org.transfer_agent(project_id, target_id, new_parent_id) do
        {:ok, _} -> "Agent #{target_id} transferred to new parent #{new_parent_id}."
        {:error, reason} -> "Error: #{reason}"
      end
    end
  end

  defp dispatch("dismiss_agent", input, _workspace_path, agent) do
    # 运行时角色检查 — 只有 HR 能解雇 agent
    unless agent.role && String.downcase(agent.role) == "hr" do
      raise "Permission denied: only HR can dismiss agents"
    end

    project_id = agent.project_id
    target_id = resolve_agent_id(input["agentId"], agent)

    if is_nil(target_id) do
      "Error: agentId is required"
    else
      # 禁止自解雇
      if target_id == agent.id do
        raise "HR cannot dismiss themselves"
      end

      # 活跃子 agent 检查
      children = HiveWeave.Services.Org.get_children(project_id, target_id)
      active_children = Enum.filter(children, fn c -> c.status not in ["archived", "dismissed"] end)
      if length(active_children) > 0 do
        child_names = active_children |> Enum.map(& &1.name) |> Enum.join(", ")
        raise "Cannot dismiss agent with active subordinates: #{child_names}. Dismiss them first."
      end

      case HiveWeave.Services.Org.dismiss_agent(project_id, target_id) do
        {:ok, _} -> "Agent #{target_id} dismissed and archived."
        {:error, reason} -> "Error: #{reason}"
      end
    end
  end

  defp dispatch("update_roster", input, _workspace_path, agent) do
    # 运行时角色检查 — 只有 HR 能更新人事档案
    unless agent.role && String.downcase(agent.role) == "hr" do
      raise "Permission denied: only HR can update roster"
    end

    target_id = resolve_agent_id(input["agentId"], agent)

    if is_nil(target_id) do
      "Error: agentId is required"
    else
      position = input["position"]
      department = input["department"]
      responsibilities = input["responsibilities"]

      attrs = %{}
      attrs = if position, do: Map.put(attrs, :position, position), else: attrs
      attrs = if department, do: Map.put(attrs, :department, department), else: attrs
      attrs = if responsibilities, do: Map.put(attrs, :responsibilities, responsibilities), else: attrs

      case HiveWeave.Services.Roster.update_roster(agent.project_id, target_id, attrs) do
        {:ok, _} -> "Roster updated for agent #{target_id}."
        {:error, reason} -> "Error: #{reason}"
      end
    end
  end

  defp dispatch("read_roster", _input, _workspace_path, agent) do
    case HiveWeave.Services.Roster.get_roster(agent.project_id) do
      {:ok, entries} when entries == [] ->
        "No roster entries found."
      {:ok, entries} ->
        formatted = Enum.map(entries, fn e ->
          "#{e.agent_name || e.agent_id} | #{e.position || "(no position)"} | #{e.department || "(no department)"} | #{String.slice(e.responsibilities || "", 0, 120)}"
        end)
        Enum.join(formatted, "\n")
      {:error, reason} -> "Error: #{inspect(reason)}"
    end
  end

  defp dispatch("list_all_agents", _input, _workspace_path, agent) do
    project_id = agent.project_id
    agents = HiveWeave.Services.Org.list_agents(project_id)

    if length(agents) == 0 do
      "No agents in project."
    else
      # Build tree from root (CEO)
      root_agents = Enum.filter(agents, fn a -> a.parent_id == nil or a.parent_id == "" end)

      tree_lines = build_agent_tree(root_agents, agents, 0)
      "Organization Tree (#{length(agents)} agents):\n" <> Enum.join(tree_lines, "\n")
    end
  end

  defp dispatch("check_agent_status", input, _workspace_path, agent) do
    project_id = agent.project_id
    agent_id = input["agentId"]

    if agent_id do
      # Single agent status
      resolved_id = resolve_agent_id(agent_id, agent)
      if resolved_id do
        case HiveWeave.Services.Org.get_agent(resolved_id) do
          %{} = target ->
            # Check real-time processing status from GenServer
            is_processing = try do
              case GenServer.call(HiveWeave.Agents.Agent.name(project_id, resolved_id), :get_state, 3_000) do
                %{status: :processing} -> true
                _ -> false
              end
            rescue
              _ -> false
            end

            status_badge = cond do
              is_processing -> "🔴 working"
              target.status in ["active", "idle"] -> "🟢 idle"
              target.status in ["archived", "dismissed"] -> "🔲 archived"
              true -> "❓ #{target.status}"
            end

            "#{target.name} (#{target.short_id || "no-id"}) — #{status_badge} | role: #{target.role}"
          nil ->
            "Agent not found: #{agent_id}"
        end
      else
        "Agent not found: #{agent_id}"
      end
    else
      # List all agents in project
      agents = HiveWeave.Services.Org.list_agents(project_id)
      if length(agents) == 0 do
        "No agents in project."
      else
        lines = Enum.map(agents, fn a ->
          is_processing = try do
            case GenServer.call(HiveWeave.Agents.Agent.name(project_id, a.id), :get_state, 1_000) do
              %{status: :processing} -> true
              _ -> false
            end
          rescue
            _ -> false
          end

          badge = cond do
            is_processing -> "🔴"
            a.status in ["active", "idle"] -> "🟢"
            a.status in ["archived", "dismissed"] -> "🔲"
            true -> "❓"
          end
          perm = if a.permission_type == "coordinator", do: "👔", else: "⚙️"
          "  #{badge} #{perm} #{a.name} (#{a.short_id || "no-id"}) — #{a.role}"
        end)
        "Agent Status (#{length(agents)} total):\n" <> Enum.join(lines, "\n")
      end
    end
  end

  # ── Code Review dispatch (D) ────────────────────────────

  defp dispatch("run_code_review", input, workspace_path, agent) do
    file_paths = input["filePaths"] || []
    if file_paths == [] do
      "Error: filePaths is required (list of file paths to review)"
    else
      execute_review("code_review", file_paths, [], workspace_path, agent)
    end
  end

  defp dispatch("run_security_audit", input, workspace_path, agent) do
    file_paths = input["filePaths"] || []
    if file_paths == [] do
      "Error: filePaths is required (list of file paths to audit)"
    else
      execute_review("security_audit", file_paths, [], workspace_path, agent)
    end
  end

  defp dispatch("run_tests", input, workspace_path, agent) do
    source_files = input["sourceFiles"] || []
    test_files = input["testFiles"] || []
    if source_files == [] do
      "Error: sourceFiles is required (list of source file paths)"
    else
      execute_review("test_review", source_files, test_files, workspace_path, agent)
    end
  end

  defp dispatch("run_perf_audit", input, workspace_path, agent) do
    file_paths = input["filePaths"] || []
    if file_paths == [] do
      "Error: filePaths is required (list of file paths to audit)"
    else
      execute_review("perf_audit", file_paths, [], workspace_path, agent)
    end
  end

  defp dispatch("run_full_review", input, workspace_path, agent) do
    file_paths = input["filePaths"] || []
    test_files = input["testFiles"] || []
    if file_paths == [] do
      "Error: filePaths is required (list of file paths to review)"
    else
      execute_full_review(file_paths, test_files, workspace_path, agent)
    end
  end

  defp dispatch("websearch", input, _workspace_path, _agent) do
    query = input["query"] || ""
    limit = input["numResults"] || 5
    if query == "" do
      "Error: query is required"
    else
      execute_websearch(query, min(limit, 8))
    end
  end

  defp dispatch("fetch_url", input, _workspace_path, _agent) do
    url = input["url"] || ""
    format = input["format"] || "markdown"

    if url == "" do
      "Error: url is required"
    else
      uri = URI.parse(url)
      cond do
        uri.scheme not in ["http", "https"] ->
          "Error: Only http/https URLs are supported"
        String.contains?(uri.host || "", "localhost") or
        String.contains?(uri.host || "", "127.0.0.1") or
        String.contains?(uri.host || "", "0.0.0.0") ->
          "Error: Local addresses blocked for security (SSRF prevention)"
        true ->
          execute_fetch_url(url, format)
      end
    end
  end

  # ── MCP tools dispatch ────────────────────────────────────────

  defp dispatch("mcp_list_tools", _input, _workspace_path, _agent) do
    mcp_list = HiveWeave.SkillRegistry.list_available_mcp()
    if mcp_list == "" or String.contains?(mcp_list, "No MCP servers") do
      "No MCP servers configured."
    else
      # For each MCP server, list its tools (we just show the server list since we don't have live connections)
      "Available MCP servers:\n#{mcp_list}\n\nUse mcp_configure to add new MCP servers. Use mcp_call to invoke MCP tools."
    end
  end

  defp dispatch("mcp_call", input, _workspace_path, agent) do
    server = input["server"] || ""
    tool = input["tool"] || ""
    arguments = input["arguments"] || %{}

    if server == "" or tool == "" do
      "Error: server and tool are required"
    else
      # Check if server is bound to this agent
      bound_mcp = parse_json_list(agent.mcp_servers || "[]")
      if server not in bound_mcp do
        "Error: MCP server '#{server}' is not bound to this agent. Use bind_mcp first."
      else
        execute_mcp_call(agent, server, tool, arguments)
      end
    end
  end

  defp dispatch("mcp_configure", input, _workspace_path, _agent) do
    name = input["name"] || ""
    transport = input["transport"] || "http"
    command = input["command"]
    url = input["url"]

    if name == "" do
      "Error: name is required"
    else
      # Ensure table exists in meta DB
      ensure_mcp_servers_table()

      # Save to meta DB for persistence
      id = Ecto.UUID.generate()
      now = System.system_time(:millisecond)

      {:ok, _} = Ecto.Adapters.SQL.query(
        HiveWeave.Repo.Meta,
        "INSERT INTO mcp_servers (id, name, transport, command, url, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        [id, name, transport, command || "", url || "", now]
      )

      "MCP server '#{name}' configured (transport: #{transport}). Use bind_mcp to bind it to agents."
    end
  rescue
    e -> "Error configuring MCP server: #{inspect(e)}"
  end

  # ── review_code dispatch (coordinator only) ─────────────────

  defp dispatch("review_code", input, workspace_path, agent) do
    subordinate_id = resolve_agent_id(input["subordinateId"], agent)
    limit = input["limit"] || 5

    if is_nil(subordinate_id) do
      "Error: subordinateId is required"
    else
      logs = HiveWeave.Services.Dispatch.get_subordinate_logs(agent.project_id, subordinate_id, limit)

      short_id =
        case HiveWeave.Services.Org.get_agent(subordinate_id) do
          %{short_id: sid} when not is_nil(sid) -> sid
          _ -> nil
        end

      worktree_info =
        if short_id do
          case HiveWeave.Services.GitWorktree.status(workspace_path, short_id) do
            {:ok, s} when not is_nil(s) ->
              "\n\n## Worktree Status\nBranch: #{s.branch}\nHEAD: #{s.head}\nUncommitted: #{s.has_uncommitted}\nCheckpoints: #{length(s.checkpoints)}"

            _ ->
              ""
          end
        else
          ""
        end

      if logs == [] do
        "No work logs found for this subordinate.#{worktree_info}"
      else
        formatted =
          Enum.map(logs, fn log ->
            "[#{log.type}] #{log.summary}"
          end)

        "## Subordinate Work Review\n\n" <>
          "Agent: #{subordinate_id}\n" <>
          "Recent Work Logs:\n#{Enum.join(formatted, "\n")}" <>
          worktree_info <>
          "\n\nUse approve_work or reject_work based on this review."
      end
    end
  end

  # ── trigger_integration dispatch (coordinator only) ──────────

  defp dispatch("trigger_integration", input, workspace_path, agent) do
    command = input["command"]
    workdir = input["workdir"]

    cmd =
      if command do
        command
      else
        cond do
          File.exists?(Path.join(workspace_path, "mix.exs")) -> "mix test"
          File.exists?(Path.join(workspace_path, "package.json")) -> "npm test"
          File.exists?(Path.join(workspace_path, "Cargo.toml")) -> "cargo test"
          File.exists?(Path.join(workspace_path, "go.mod")) -> "go test ./..."
          File.exists?(Path.join(workspace_path, "Makefile")) -> "make test"
          true -> "echo 'No auto-detected test framework. Please specify a command.'"
        end
      end

    cwd = if workdir, do: Path.expand(workdir, workspace_path), else: workspace_path

    if not String.starts_with?(Path.expand(cwd), Path.expand(workspace_path)) do
      "Error: workdir must be within workspace"
    else
      task = Task.async(fn ->
        System.cmd("cmd", ["/c", cmd], cd: cwd, stderr_to_stdout: true)
      end)

      case Task.yield(task, 300_000) || Task.shutdown(task, :brutal_kill) do
        {:ok, {output, 0}} ->
          preview =
            if byte_size(output) > 5000,
              do: String.slice(output, 0, 5000) <> "\n... (truncated)",
              else: output

          "Integration test PASSED:\n#{preview}"

        {:ok, {output, code}} ->
          preview =
            if byte_size(output) > 5000,
              do: String.slice(output, -5000, 5000) <> "\n... (truncated)",
              else: output

          "Integration test FAILED (exit #{code}):\n#{preview}"

        nil ->
          "Error: Integration test timed out after 300 seconds"
      end
    end
  rescue
    e -> "Error triggering integration: #{inspect(e)}"
  end

  # ── glob dispatch ────────────────────────────────────────────

  defp dispatch("glob", input, workspace_path, _agent) do
    pattern = input["pattern"] || ""
    path = input["path"] || ""

    if pattern == "" do
      "Error: pattern is required"
    else
      search_path = if path == "", do: workspace_path, else: Path.expand(path, workspace_path)

      if not String.starts_with?(Path.expand(search_path), Path.expand(workspace_path)) do
        "Error: path must be within workspace"
      else
        execute_glob(pattern, search_path, workspace_path)
      end
    end
  end

  # ── Model management dispatch ────────────────────────────────

  defp dispatch("list_models", _input, _workspace_path, _agent) do
    case HiveWeave.Services.Model.get_active_models() do
      [] -> "No active models configured. Ask the user to add models via the LLM Models panel."
      models ->
        default_coord = HiveWeave.Services.Settings.get("default_model_coordinator")
        default_exec = HiveWeave.Services.Settings.get("default_model_executor")

        formatted = Enum.map(models, fn m ->
          role_label = cond do
            m.id == default_coord -> " [DEFAULT: coordinator]"
            m.id == default_exec -> " [DEFAULT: executor]"
            true -> ""
          end
          "#{m.name} (#{m.model_id})#{role_label}\n  ID: #{m.id}"
        end)

        "## Available Models\n\n#{Enum.join(formatted, "\n\n")}\n\nUse the Settings panel or ask the user to set default models via:\n  default_model_coordinator (for CEO/HR/coordinators)\n  default_model_executor (for executors)"
    end
  end

  defp dispatch("set_default_model", input, _workspace_path, _agent) do
    role = input["role"] || ""
    model_id = input["modelId"] || ""

    if role == "" or model_id == "" do
      "Error: role and modelId are required"
    else
      setting_key = case role do
        "coordinator" -> "default_model_coordinator"
        "executor" -> "default_model_executor"
        _ -> nil
      end

      if is_nil(setting_key) do
        "Error: role must be 'coordinator' or 'executor'"
      else
        case HiveWeave.Services.Model.get_model(model_id) do
          {:ok, model} ->
            HiveWeave.Services.Settings.set(setting_key, model_id)
            "Default model for #{role} set to: #{model.name} (#{model.model_id}). New agents of this role will use this model."
          {:error, _} ->
            "Error: Model not found. Use list_models to see available models."
        end
      end
    end
  end

  defp dispatch(unknown, _input, _workspace_path, _agent) do
    "Error: Unknown tool '#{unknown}'"
  end

  # ── Helpers for org tree (list_all_agents) ───────────────────

  defp build_agent_tree([], _all_agents, _depth), do: []
  defp build_agent_tree(agents, all_agents, depth) do
    indent = String.duplicate("  ", depth)
    Enum.flat_map(agents, fn a ->
      perm = if a.permission_type == "coordinator", do: "👔", else: "⚙️"
      line = "#{indent}#{perm} #{a.name} (#{a.short_id || "no-id"}) — #{a.role}"

      # Find children
      children = Enum.filter(all_agents, fn c -> c.parent_id == a.id end)
      child_lines = build_agent_tree(children, all_agents, depth + 1)

      [line] ++ child_lines
    end)
  end

  # ── Helpers for skill/MCP management ─────────────────────────

  defp resolve_and_update_agent(target_id, caller_agent, update_fn) do
    target = HiveWeave.Repo.Meta.get(HiveWeave.Schema.Agent, target_id)

    cond do
      is_nil(target) ->
        {:error, "Agent not found with ID #{target_id}."}

      target.project_id != caller_agent.project_id ->
        {:error, "Target agent belongs to a different project."}

      target_id == caller_agent.id ->
        do_update_agent(target, update_fn)

      target.parent_id == caller_agent.id ->
        do_update_agent(target, update_fn)

      true ->
        {:error, "You can only bind/unbind skills on yourself or your direct subordinates."}
    end
  end

  defp do_update_agent(agent, update_fn) do
    case update_fn.(agent) do
      {:error, _} = err -> err

      {updated_attrs, msg} ->
        changeset = HiveWeave.Schema.Agent.changeset(agent, %{
          bound_skills: Map.get(updated_attrs, :bound_skills, agent.bound_skills),
          mcp_servers: Map.get(updated_attrs, :mcp_servers, agent.mcp_servers)
        })

        case HiveWeave.Repo.Meta.update(changeset) do
          {:ok, _} -> {:ok, msg}
          {:error, cs} ->
            errors = cs.errors |> Enum.map(fn {f, {m, _}} -> "#{f}: #{m}" end) |> Enum.join(", ")
            {:error, "Failed to update agent: #{errors}"}
        end
    end
  end

  defp parse_comma_list(nil), do: []
  defp parse_comma_list(""), do: []
  defp parse_comma_list(str) when is_binary(str) do
    str
    |> String.split(",")
    |> Enum.map(&String.trim/1)
    |> Enum.reject(&(&1 == ""))
  end

  defp parse_json_list(nil), do: []
  defp parse_json_list(""), do: []
  defp parse_json_list(json) when is_binary(json) do
    case Jason.decode(json) do
      {:ok, list} when is_list(list) -> list
      _ -> []
    end
  end

  # ── Worktree path routing ────────────────────────────────────

  defp get_effective_workspace(agent, workspace_path) do
    short_id = Map.get(agent, :short_id) || Map.get(agent, "short_id")
    Logger.debug("[ToolExecutor] get_effective_workspace: agent_id=#{inspect(Map.get(agent, :id))} short_id=#{inspect(short_id)} workspace=#{inspect(workspace_path)}")
    if short_id do
      case HiveWeave.Services.GitWorktree.get_worktree_path(workspace_path, short_id) do
        nil ->
          Logger.debug("[ToolExecutor] No worktree found for #{short_id}, using main workspace")
          workspace_path
        wt_path ->
          Logger.info("[ToolExecutor] Using worktree path for #{short_id}: #{wt_path}")
          wt_path
      end
    else
      Logger.debug("[ToolExecutor] Agent has no short_id, using main workspace")
      workspace_path
    end
  end

  # ── Agent resolution helpers ────────────────────────────────

  defp resolve_agent_id(nil, _agent), do: nil
  defp resolve_agent_id(id, agent) when is_binary(id) do
    input = String.trim(id)
    project_id = agent.project_id

    # 1. Try direct UUID lookup
    case HiveWeave.Services.Org.get_agent(input) do
      %{} = a -> a.id
      nil ->
        agents = HiveWeave.Services.Org.list_agents(project_id)
        Enum.find_value(agents, fn a ->
          cond do
            # 2. UUID exact match
            a.id == input -> a.id
            # 3. short_id exact match (e.g. "A001")
            a.short_id && String.downcase(a.short_id) == String.downcase(input) -> a.id
            # 4. UUID prefix match (at least 6 chars)
            String.length(input) >= 6 && String.starts_with?(a.id, input) -> a.id
            # 5. short_id prefix match
            a.short_id && String.starts_with?(String.downcase(a.short_id), String.downcase(input)) -> a.id
            # 6. Name exact match (case-insensitive) — allows CEO to message HR by name
            a.name && String.downcase(a.name) == String.downcase(input) -> a.id
            # 7. Name partial/contains match (case-insensitive)
            a.name && String.contains?(String.downcase(a.name), String.downcase(input)) -> a.id
            # 8. Role match (e.g. "hr", "HR") — convenient for CEO to message HR by role
            a.role && String.downcase(a.role) == String.downcase(input) -> a.id
            true -> nil
          end
        end)
    end
  end

  defp contains_chinese?(str) when is_binary(str) do
    String.match?(str, ~r/[\x{4e00}-\x{9fff}]/u)
  end
  defp contains_chinese?(_), do: false

  defp get_parent_id(agent) do
    agent = if is_map(agent), do: agent, else: %{}
    Map.get(agent, :parent_id)
  end

  defp trigger_agent(agent_id) do
    # Determine if agent is coordinator or executor, trigger accordingly
    case HiveWeave.Services.Org.get_agent(agent_id) do
      %{role: role} when role in ["ceo", "coordinator", "hr"] ->
        HiveWeave.Agents.Agent.trigger_coordinator(agent_id)
      _ ->
        HiveWeave.Agents.Agent.trigger_subordinate(agent_id)
    end
  rescue
    _ -> :ok
  end

  defp format_time(ms) when is_integer(ms) do
    {:ok, dt} = DateTime.from_unix(div(ms, 1000))
    "#{dt.hour}:#{String.pad_leading(Integer.to_string(dt.minute), 2, "0")}:#{String.pad_leading(Integer.to_string(dt.second), 2, "0")}"
  rescue
    _ -> to_string(ms)
  end
  defp format_time(_), do: "unknown"

  # ── bash ────────────────────────────────────────────────────

  defp check_self_destructive(command) do
    cmd_lower = String.downcase(command)

    # System-level destructive commands
    system_patterns = [
      ~r/rm\s+-rf\s+\//,
      ~r/format\s+[a-z]:/i,
      ~r/diskpart/i,
      ~r/shutdown/i,
      ~r/reboot/i,
      ~r/poweroff/i,
      ~r/halt\b/i
    ]

    if Enum.any?(system_patterns, &Regex.match?(&1, cmd_lower)) do
      {:block, "system-level destructive command"}
    else
      :ok
    end
  end

  defp execute_bash(command, workdir, workspace_path) do
    cwd =
      if workdir == "" do
        workspace_path
      else
        Path.expand(workdir, workspace_path)
      end

    # Validate cwd is within workspace
    if not String.starts_with?(Path.expand(cwd), Path.expand(workspace_path)) do
      "Error: Sandbox violation - workdir must be within workspace"
    else
      # Execute with timeout (System.cmd doesn't support :timeout, use Task.yield)
      task = Task.async(fn ->
        System.cmd("cmd", ["/c", command],
          cd: cwd,
          env: [{"HIVEWEAVE_BASH", "1"}, {"HIVEWEAVE_WORKSPACE", cwd}],
          stderr_to_stdout: true
        )
      end)

      case Task.yield(task, 120_000) || Task.shutdown(task, :brutal_kill) do
        {:ok, {output, 0}} ->
          truncate_output(output)

        {:ok, {output, exit_code}} ->
          truncate_output(output) <> "\nExit code: #{exit_code}"

        {:exit, :timeout} ->
          "Error: Command timed out after 120 seconds"

        nil ->
          "Error: Command timed out after 120 seconds"
      end
    end
  rescue
    e -> "Error: #{inspect(e)}"
  end

  defp truncate_output(output) do
    max_bytes = 1_048_576  # 1MB
    if byte_size(output) > max_bytes do
      binary_part(output, 0, max_bytes) <>
        "\n... [output truncated at 1MB]"
    else
      output
    end
  end

  # ── read_file ───────────────────────────────────────────────

  defp execute_read_file(file_path, offset, limit, workspace_path) do
    case validate_file_path(file_path, workspace_path) do
      {:error, reason} ->
        "Error: #{reason}"

      {:ok, full_path} ->
        case File.read(full_path) do
          {:ok, content} ->
            # Skip binary files (SQLite DBs, images, etc.)
            if String.valid?(content) do
              lines = String.split(content, "\n")
              total = length(lines)

              start_line = max(offset, 0)
              end_line = min(start_line + limit, total)

              selected = Enum.slice(lines, start_line, end_line - start_line)

              # Format with line numbers
              formatted =
                selected
                |> Enum.with_index(start_line)
                |> Enum.map(fn {line, idx} -> "#{idx + 1}: #{line}" end)
                |> Enum.join("\n")

              formatted <> "\n\n(Showing lines #{start_line + 1}-#{end_line} of #{total})"
            else
              # Binary file — refuse to read, return safe message
              file_size = byte_size(content)
              "Error: Cannot display binary file (#{file_size} bytes). This appears to be a binary format (e.g., database, image, compiled asset). Use list_files to see metadata instead."
            end

          {:error, :enoent} ->
            "Error: File not found: #{file_path}"

          {:error, reason} ->
            "Error: #{inspect(reason)}"
        end
    end
  end

  # ── list_files ──────────────────────────────────────────────

  defp execute_list_files(path, workspace_path) do
    full_path = if path == "", do: workspace_path, else: Path.expand(path, workspace_path)

    if not String.starts_with?(full_path, Path.expand(workspace_path)) do
      "Error: Sandbox violation - path must be within workspace"
    else
      case File.ls(full_path) do
        {:ok, entries} ->
          formatted =
            entries
            |> Enum.map(fn entry ->
              entry_path = Path.join(full_path, entry)
              if File.dir?(entry_path) do
                "[DIR]  #{entry}/"
              else
                size = case File.stat(entry_path) do
                  {:ok, %{size: s}} -> s
                  _ -> 0
                end
                "[FILE] #{entry} (#{format_size(size)})"
              end
            end)
            |> Enum.join("\n")

          if formatted == "" do
            "(empty directory)"
          else
            formatted
          end

        {:error, :enoent} ->
          "Error: Directory not found: #{path}"

        {:error, reason} ->
          "Error: #{inspect(reason)}"
      end
    end
  end

  defp format_size(bytes) when bytes < 1024, do: "#{bytes}B"
  defp format_size(bytes) when bytes < 1_048_576, do: "#{Float.round(bytes / 1024, 1)}KB"
  defp format_size(bytes), do: "#{Float.round(bytes / 1_048_576, 1)}MB"

  # ── grep ────────────────────────────────────────────────────

  defp execute_grep(pattern, path, include, workspace_path) do
    search_path = if path == "", do: workspace_path, else: Path.expand(path, workspace_path)

    if not String.starts_with?(search_path, Path.expand(workspace_path)) do
      "Error: Sandbox violation - path must be within workspace"
    else
      # Try ripgrep first, fall back to Elixir-based search
      case try_ripgrep(pattern, search_path, include) do
        {:ok, output} -> output
        {:error, :not_found} ->
          # Fallback: walk files and search
          elixir_grep(pattern, search_path, include)
      end
    end
  rescue
    e -> "Error: #{inspect(e)}"
  end

  defp try_ripgrep(pattern, search_path, include) do
    args = ["--line-number", "--no-heading", "--color=never", "-e", pattern]
    args = if include, do: args ++ ["--glob", include], else: args
    args = args ++ [search_path]

    case System.cmd("rg", args, stderr_to_stdout: true) do
      {output, 0} ->
        # Limit to 100 results
        lines = String.split(output, "\n") |> Enum.take(100)
        {:ok, Enum.join(lines, "\n")}

      {_output, 1} ->
        {:ok, "No matches found."}

      {_output, _code} ->
        {:error, :not_found}
    end
  rescue
    # rg not installed
    _ -> {:error, :not_found}
  end

  defp elixir_grep(pattern, search_path, include) do
    regex = case Regex.compile(pattern) do
      {:ok, r} -> r
      {:error, _} -> Regex.compile(Regex.escape(pattern)) |> elem(1)
    end

    glob = include || "**/*"

    search_path
    |> Path.join(glob)
    |> Path.wildcard()
    |> Enum.filter(&File.regular?/1)
    |> Enum.take(500)  # Safety limit
    |> Enum.flat_map(fn file ->
      case File.read(file) do
        {:ok, content} ->
          content
          |> String.split("\n")
          |> Enum.with_index(1)
          |> Enum.filter(fn {line, _idx} -> Regex.match?(regex, line) end)
          |> Enum.map(fn {line, idx} ->
            rel_path = Path.relative_to(file, search_path)
            "#{rel_path}:#{idx}: #{String.slice(line, 0, 500)}"
          end)

        _ -> []
      end
    end)
    |> Enum.take(100)
    |> case do
      [] -> "No matches found."
      results -> Enum.join(results, "\n")
    end
  end

  # ── apply_patch ─────────────────────────────────────────────

  defp execute_apply_patch(patches, workspace_path) do
    if patches == [] do
      "Error: No patches provided. Use the 'patches' array with 'op', 'filePath', and 'content'/'oldString'/'newString' fields."
    else
      results =
        patches
        |> Enum.map(fn patch -> apply_single_patch(patch, workspace_path) end)

      Enum.join(results, "\n")
    end
  end

  defp apply_single_patch(%{"op" => "add", "filePath" => file_path, "content" => content}, workspace_path) do
    full_path = Path.expand(file_path, workspace_path)

    cond do
      not String.starts_with?(full_path, Path.expand(workspace_path)) ->
        "ERROR: Sandbox violation: #{file_path}"

      File.exists?(full_path) ->
        "ERROR: File already exists: #{file_path}"

      true ->
        File.mkdir_p!(Path.dirname(full_path))
        case File.write(full_path, content) do
          :ok -> "Created #{file_path} (#{byte_size(content)} bytes)"
          {:error, reason} -> "ERROR: #{inspect(reason)}"
        end
    end
  end

  defp apply_single_patch(%{"op" => "update", "filePath" => file_path, "oldString" => old_str, "newString" => new_str}, workspace_path) do
    full_path = Path.expand(file_path, workspace_path)

    cond do
      not String.starts_with?(full_path, Path.expand(workspace_path)) ->
        "ERROR: Sandbox violation: #{file_path}"

      not File.exists?(full_path) ->
        "ERROR: File not found: #{file_path}"

      true ->
        case File.read(full_path) do
          {:ok, content} ->
            count = :binary.matches(content, old_str) |> length()

            cond do
              count == 0 ->
                "ERROR: oldString not found in #{file_path}. Please read the file first."

              count > 1 ->
                "ERROR: oldString found #{count} times in #{file_path}. Add more context to make it unique."

              true ->
                new_content = String.replace(content, old_str, new_str)
                case File.write(full_path, new_content) do
                  :ok ->
                    new_lines = String.split(new_str, "\n") |> length()
                    old_lines = String.split(old_str, "\n") |> length()
                    line_diff = new_lines - old_lines
                    "Updated #{file_path} (#{if line_diff >= 0, do: "+#{line_diff}", else: "#{line_diff}"} lines)"

                  {:error, reason} ->
                    "ERROR: #{inspect(reason)}"
                end
            end

          {:error, reason} ->
            "ERROR: #{inspect(reason)}"
        end
    end
  end

  defp apply_single_patch(%{"op" => "delete", "filePath" => file_path}, workspace_path) do
    full_path = Path.expand(file_path, workspace_path)

    cond do
      not String.starts_with?(full_path, Path.expand(workspace_path)) ->
        "ERROR: Sandbox violation: #{file_path}"

      not File.exists?(full_path) ->
        "ERROR: File not found: #{file_path}"

      true ->
        case File.rm(full_path) do
          :ok -> "Deleted #{file_path}"
          {:error, reason} -> "ERROR: #{inspect(reason)}"
        end
    end
  end

  defp apply_single_patch(%{"op" => op}, _workspace_path) do
    "ERROR: Unknown op: #{op}"
  end

  # ── todowrite ───────────────────────────────────────────────

  defp execute_todowrite(todos, agent) do
    now_ms = System.system_time(:millisecond)
    project_id = agent.project_id
    agent_id = agent.id

    # Delete existing todos for this agent, then insert new ones
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

    # Broadcast todo update via PubSub
    Phoenix.PubSub.broadcast(
      HiveWeave.PubSub,
      "agent:#{agent_id}",
      {:todo_update, todos}
    )

    count = length(todos)
    completed = Enum.count(todos, fn t -> t["status"] == "completed" end)
    "Updated task list: #{completed}/#{count} completed"
  rescue
    e ->
      Logger.warning("execute_todowrite failed: #{inspect(e)}")
      "Updated task list (broadcast only)"
  end

  # ── question ────────────────────────────────────────────────

  defp execute_question(question, agent) do
    agent_id = agent.id
    project_id = agent.project_id
    question_id = Ecto.UUID.generate()
    now_ms = System.system_time(:millisecond)

    # Persist question to per-project DB
    HiveWeave.Repo.ProjectFactory.query_for_agent(agent_id,
      "INSERT INTO questions (id, agent_id, project_id, question, status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)",
      [question_id, agent_id, project_id, question, now_ms])

    # Broadcast question to frontend via PubSub
    Phoenix.PubSub.broadcast(
      HiveWeave.PubSub,
      "agent:#{agent_id}",
      {:question, %{
        id: question_id,
        agent_id: agent_id,
        question: question,
        timestamp: now_ms
      }}
    )

    # Also broadcast to project channel for global question dialog
    Phoenix.PubSub.broadcast(
      HiveWeave.PubSub,
      "project:#{project_id}",
      {:question, %{
        id: question_id,
        agent_id: agent_id,
        agent_name: agent.name,
        question: question,
        timestamp: now_ms
      }}
    )

    # BLOCK and wait for the user's answer (120 second timeout).
    #
    # This function runs inside the LLM Task process (spawned via
    # Task.Supervisor.async_nolink in Agent.handle_call(:chat, ...)).
    # The Agent GenServer forwards {:question_answer, question_id, answer}
    # messages it receives to this Task's PID via its handle_info clause.
    # While blocked here the Task cannot process other tool calls — this is
    # the intended blocking-question behaviour.
    answer = receive do
      {:question_answer, ^question_id, user_answer} ->
        user_answer
    after
      120_000 ->
        "⏰ Question timed out (120s). Proceeding without user input."
    end

    # Mark the question as answered in the per-project DB
    answered_at = System.system_time(:millisecond)

    HiveWeave.Repo.ProjectFactory.query_for_agent(agent_id,
      "UPDATE questions SET status = 'answered', answer = ?, answered_at = ? WHERE id = ?",
      [answer, answered_at, question_id])

    "User answered: #{answer}"
  rescue
    e ->
      Logger.warning("execute_question failed: #{inspect(e)}")
      "Question sent to user. Stop and wait for their response."
  end

  # ── File operation implementations ────────────────────────────

  # Auto-mark a keyResult as "done" when a task is merged to main.
  # Matches by checking if the task_name appears in the keyResult text.
  defp maybe_update_goal_status(project_id, task_name) do
    case HiveWeave.Services.Charter.read_goals(project_id) do
      {:ok, goals} when is_map(goals) ->
        krs = Map.get(goals, "keyResults", Map.get(goals, :keyResults, []))
        task_lower = String.downcase(task_name)

        updated_krs = Enum.map(krs, fn kr ->
          kr_text = cond do
            is_binary(kr) -> kr
            is_map(kr) -> Map.get(kr, "text", Map.get(kr, :text, ""))
            true -> ""
          end
          # If this keyResult's text contains part of the task name, mark as done
          if kr_text != "" and String.contains?(String.downcase(kr_text), String.slice(task_lower, 0, min(String.length(task_lower), 20))) do
            if is_map(kr) do
              Map.put(kr, "status", "done")
            else
              %{"text" => kr, "status" => "done", "owner" => nil}
            end
          else
            kr
          end
        end)

        goals_json = Jason.encode!(%{
          "objective" => Map.get(goals, "objective", Map.get(goals, :objective, "")),
          "focus" => Map.get(goals, "focus", Map.get(goals, :focus, "")),
          "keyResults" => updated_krs
        })

        sql = "UPDATE projects SET charter_json = ? WHERE id = ?"
        Ecto.Adapters.SQL.query(HiveWeave.Repo.Meta, sql, [goals_json, project_id])

      _ -> :skip
    end
  rescue
    e -> Logger.warning("[Goals] Failed to auto-update goal status: #{inspect(e)}")
  end

  defp validate_file_path(file_path, workspace_path) do
    full_path = Path.expand(file_path, workspace_path)
    expanded_ws = Path.expand(workspace_path)

    unless String.starts_with?(full_path, expanded_ws) do
      {:error, "Sandbox violation - file must be within workspace"}
    else
      # Normalize backslashes to forward slashes for consistent matching on Windows
      normalized_full = String.replace(full_path, "\\", "/")

      # Block .hiveweave access EXCEPT for worktree paths (.hiveweave/worktrees/)
      # Worktree paths are valid working directories set by get_effective_workspace
      is_worktree_path = String.contains?(normalized_full, ".hiveweave/worktrees/")
      if String.contains?(normalized_full, ".hiveweave") and not is_worktree_path do
        {:error, "Access denied: .hiveweave directory is restricted"}
      else
        if Regex.match?(~r{/aws/credentials$}, normalized_full) do
          {:error, "Access denied"}
        else
          basename = Path.basename(full_path)

          sensitive_patterns = [
            ~r/^\.env(\..*)?$/,
            ~r/^id_rsa(\.pub)?$/,
            ~r/^id_ed25519(\.pub)?$/,
            ~r/\.pem$/,
            ~r/\.p12$/,
            ~r/\.pfx$/,
            ~r/^credentials(\.json)?$/,
            ~r/\.key$/,
            ~r/^\.htpasswd$/,
            ~r/^shadow$/,
            ~r/\.keystore$/,
            ~r/^token(\.json)?$/,
            ~r/^secrets(\.(json|ya?ml))?$/,
            ~r/^secret\.ya?ml$/,
            ~r/^\.npmrc$/,
            ~r/^\.pypirc$/,
            ~r/^netrc$/,
          ]

          if Enum.any?(sensitive_patterns, &Regex.match?(&1, basename)) do
            {:error, "Access denied"}
          else
            {:ok, full_path}
          end
        end
      end
    end
  end

  defp execute_write_file(file_path, content, append, workspace_path) do
    case validate_file_path(file_path, workspace_path) do
      {:error, reason} -> "Error: #{reason}"
      {:ok, full_path} ->
        action = if append, do: "Appended to", else: "Wrote"

        cond do
          not append and File.exists?(full_path) ->
            case File.read(full_path) do
              {:ok, existing} ->
                if existing == content do
                  "No change: file already has same content. Write skipped."
                else
                  do_write(full_path, content, action)
                end
              {:error, _} ->
                do_write(full_path, content, action)
            end

          true ->
            File.mkdir_p!(Path.dirname(full_path))
            do_write(full_path, content, action, append)
        end
    end
  end

  defp do_write(full_path, content, action, append \\ false) do
    mode = if append, do: [:append], else: []
    case File.write(full_path, content, mode) do
      :ok ->
        lines = content |> String.split("\n") |> length()
        bytes = byte_size(content)
        "#{action}: #{full_path} (#{lines} lines, #{bytes} bytes)"
      {:error, reason} ->
        "Error: #{inspect(reason)}"
    end
  end

  defp execute_edit_file(file_path, old_string, new_string, workspace_path) do
    case validate_file_path(file_path, workspace_path) do
      {:error, reason} -> "Error: #{reason}"
      {:ok, full_path} ->
        case File.read(full_path) do
          {:ok, content} ->
            count =
              if old_string == "" do
                0
              else
                :binary.matches(content, old_string) |> length()
              end

            cond do
              count == 0 ->
                "Error: oldString not found"

              count > 1 ->
                "Error: oldString found #{count} times, add more context"

              true ->
                new_content = String.replace(content, old_string, new_string)
                case File.write(full_path, new_content) do
                  :ok ->
                    old_lines = old_string |> String.split("\n") |> length()
                    new_lines = new_string |> String.split("\n") |> length()
                    "Edited: #{file_path} (replaced #{old_lines} line(s) with #{new_lines} line(s))"
                  {:error, reason} ->
                    "Error: #{inspect(reason)}"
                end
            end

          {:error, :enoent} ->
            "Error: File not found: #{file_path}"

          {:error, reason} ->
            "Error: #{inspect(reason)}"
        end
    end
  end

  defp execute_delete_file(file_path, workspace_path) do
    case validate_file_path(file_path, workspace_path) do
      {:error, reason} -> "Error: #{reason}"
      {:ok, full_path} ->
        cond do
          File.dir?(full_path) ->
            "Error: #{file_path} is a directory. Use delete_directory to remove directories."

          not File.exists?(full_path) ->
            "Error: File not found: #{file_path}"

          true ->
            case File.rm(full_path) do
              :ok -> "Deleted: #{file_path}"
              {:error, reason} -> "Error: #{inspect(reason)}"
            end
        end
    end
  end

  defp execute_move_file(source, destination, overwrite, workspace_path) do
    case validate_file_path(source, workspace_path) do
      {:error, reason} -> "Error: #{reason}"
      {:ok, full_source} ->
        case validate_file_path(destination, workspace_path) do
          {:error, reason} -> "Error: #{reason}"
          {:ok, full_dest} ->
            cond do
              not File.exists?(full_source) ->
                "Error: Source not found: #{source}"

              File.exists?(full_dest) and not overwrite ->
                "Error: Destination already exists: #{destination}. Use overwrite=true to replace."

              true ->
                File.mkdir_p!(Path.dirname(full_dest))
                case File.rename(full_source, full_dest) do
                  :ok -> "Moved: #{source} -> #{destination}"
                  {:error, reason} -> "Error: #{inspect(reason)}"
                end
            end
        end
    end
  end

  defp execute_create_directory(path, workspace_path) do
    case validate_file_path(path, workspace_path) do
      {:error, reason} -> "Error: #{reason}"
      {:ok, full_path} ->
        case File.mkdir_p(full_path) do
          :ok -> "Directory created: #{path}"
          {:error, reason} -> "Error: #{inspect(reason)}"
        end
    end
  end

  defp execute_delete_directory(path, workspace_path) do
    case validate_file_path(path, workspace_path) do
      {:error, reason} -> "Error: #{reason}"
      {:ok, full_path} ->
        cond do
          not File.dir?(full_path) ->
            "Error: #{path} is not a directory"

          true ->
            case File.rmdir(full_path) do
              :ok -> "Directory deleted: #{path}"
              {:error, :enotempty} ->
                "Error: Directory not empty: #{path}"
              {:error, reason} -> "Error: #{inspect(reason)}"
            end
        end
    end
  end

  defp execute_search_files(pattern, path, include, workspace_path) do
    search_path = if path == "" or is_nil(path), do: workspace_path, else: Path.expand(path, workspace_path)

    if not String.starts_with?(Path.expand(search_path), Path.expand(workspace_path)) do
      "Error: Sandbox violation - path must be within workspace"
    else
      regex = case Regex.compile(pattern) do
        {:ok, r} -> r
        {:error, _} -> Regex.compile(Regex.escape(pattern)) |> elem(1)
      end

      skip_set = MapSet.new(~w(node_modules .git .hiveweave dist build .next .nuxt .turbo coverage deps _build))

      search_path
      |> walk_files_recursive(skip_set)
      |> Enum.take(500)
      |> Enum.filter(fn file ->
        cond do
          not File.regular?(file) -> false
          is_nil(include) or include == "" -> true
          true ->
            rel = String.replace(Path.relative_to(file, search_path), "\\", "/")
            matches_simple_glob?(rel, include)
        end
      end)
      |> Enum.filter(fn file ->
        case File.stat(file) do
          {:ok, %{size: size}} -> size <= 250_000
          _ -> false
        end
      end)
      |> Enum.flat_map(fn file ->
        case File.read(file) do
          {:ok, content} ->
            content
            |> String.split("\n")
            |> Enum.with_index(1)
            |> Enum.filter(fn {line, _} -> Regex.match?(regex, line) end)
            |> Enum.map(fn {line, lineno} ->
              rel_path = Path.relative_to(file, search_path)
              "#{rel_path}:#{lineno}: #{String.slice(line, 0, 500)}"
            end)
          _ -> []
        end
      end)
      |> Enum.take(100)
      |> case do
        [] -> "No matches found."
        results -> Enum.join(results, "\n")
      end
    end
  rescue
    e -> "Error: #{inspect(e)}"
  end

  defp walk_files_recursive(dir, skip_set) do
    case File.ls(dir) do
      {:ok, entries} ->
        Enum.flat_map(entries, fn entry ->
          if MapSet.member?(skip_set, entry) do
            []
          else
            full = Path.join(dir, entry)
            cond do
              File.dir?(full) -> walk_files_recursive(full, skip_set)
              File.regular?(full) -> [full]
              true -> []
            end
          end
        end)
      {:error, _} -> []
    end
  end

  defp matches_simple_glob?(path, glob) do
    regex_str =
      glob
      |> Regex.escape()
      |> String.replace("\\*\\*", ".+")
      |> String.replace("\\*", "[^/\\\\]*")
      |> String.replace("\\?", ".")

    case Regex.compile("^#{regex_str}$") do
      {:ok, r} -> Regex.match?(r, path)
      {:error, _} -> true
    end
  end

  # ── Review helpers ───────────────────────────────────────

  defp execute_review(review_type, source_files, test_files, workspace_path, agent) do
    eff_ws = get_effective_workspace(agent, workspace_path)

    file_contents =
      Enum.map(source_files, fn fp ->
        full = Path.expand(fp, eff_ws)
        case File.read(full) do
          {:ok, content} -> {fp, String.slice(content, 0, 12000)}
          _ -> {fp, nil}
        end
      end)
      |> Enum.reject(fn {_, c} -> is_nil(c) end)

    if file_contents == [] do
      "Error: No readable files found for review."
    else
      system_prompt = build_review_system_prompt(review_type)
      user_prompt =
        file_contents
        |> Enum.map(fn {path, code} -> "### #{path}\n```\n#{code}\n```" end)
        |> Enum.join("\n\n")

      user_prompt = if test_files != [] do
        test_block =
          Enum.map(test_files, fn fp ->
            full = Path.expand(fp, eff_ws)
            case File.read(full) do
              {:ok, c} -> "### #{fp}\n```\n#{String.slice(c, 0, 8000)}\n```"
              _ -> nil
            end
          end)
          |> Enum.reject(&is_nil/1)
          |> Enum.join("\n\n")
        user_prompt <> "\n\n## Test Files\n\n#{test_block}"
      else
        user_prompt
      end

      case call_review_llm(agent, system_prompt, user_prompt) do
        {:ok, text} -> text
        {:error, reason} -> "Error: Review failed — #{inspect(reason)}"
      end
    end
  end

  defp execute_full_review(file_paths, test_files, workspace_path, agent) do
    tasks = [
      Task.async(fn -> execute_review("code_review", file_paths, test_files, workspace_path, agent) end),
      Task.async(fn -> execute_review("security_audit", file_paths, [], workspace_path, agent) end),
      Task.async(fn -> execute_review("test_review", file_paths, test_files, workspace_path, agent) end),
      Task.async(fn -> execute_review("perf_audit", file_paths, [], workspace_path, agent) end)
    ]

    results = Enum.map(tasks, fn task ->
      case Task.yield(task, 60_000) || Task.shutdown(task, :brutal_kill) do
        {:ok, result} -> result
        nil -> "Review timed out."
      end
    end)

    # Parse scores from each result to compute overall
    scores =
      results
      |> Enum.map(fn r ->
        case Jason.decode(r) do
          {:ok, %{"score" => s}} when is_number(s) -> s
          _ -> nil
        end
      end)
      |> Enum.reject(&is_nil/1)

    overall_score = if scores == [], do: 0, else: Enum.sum(scores) |> div(length(scores))

    all_passed =
      results
      |> Enum.all?(fn r ->
        case Jason.decode(r) do
          {:ok, %{"passed" => p}} -> p == true
          _ -> false
        end
      end)

    "## Code Review\n#{Enum.at(results, 0)}\n\n" <>
    "## Security Audit\n#{Enum.at(results, 1)}\n\n" <>
    "## Test Review\n#{Enum.at(results, 2)}\n\n" <>
    "## Performance Audit\n#{Enum.at(results, 3)}\n\n" <>
    "---\n" <>
    "Overall Score: #{overall_score}/100 — #{if all_passed, do: "PASS", else: "FAIL"}"
  end

  defp build_review_system_prompt("code_review") do
    persona = load_persona("code-reviewer")
    skill = load_skill_content("code-review-and-quality")
    base = "You are a senior code reviewer performing a five-axis review:\n1. **Correctness** — bugs, edge cases, error handling gaps\n2. **Readability** — naming, comments, complexity, clarity\n3. **Architecture** — separation of concerns, coupling, patterns\n4. **Security** — injection, auth, data exposure (not a full audit)\n5. **Performance** — obvious bottlenecks, N+1 queries, memory leaks"
    format = "\n\nReturn ONLY valid JSON, no markdown or commentary:\n{\n  \"passed\": true/false,\n  \"score\": 0-100,\n  \"summary\": \"<one-paragraph overall assessment>\",\n  \"issues\": [\n    {\n      \"severity\": \"critical\" | \"major\" | \"minor\" | \"info\",\n      \"file\": \"<file path>\",\n      \"line\": <number or null>,\n      \"title\": \"<short title>\",\n      \"description\": \"<detailed explanation>\",\n      \"suggestion\": \"<how to fix>\"\n    }\n  ]\n}\nCRITICAL = security hole, data loss, crash. MAJOR = wrong behavior, broken feature.\nMINOR = style, naming, minor duplication. INFO = observation, no action needed."
    combine_review_prompt(base, persona, skill, format)
  end

  defp build_review_system_prompt("security_audit") do
    persona = load_persona("security-auditor")
    skill = load_skill_content("security-and-hardening")
    base = "You are a security engineer performing a focused vulnerability audit. Check for:\n1. **OWASP Top 10** — injection, broken auth, sensitive data exposure, XXE, access control, misconfig, XSS, deserialization, known vulns, logging gaps\n2. **Secrets & Keys** — hardcoded API keys, tokens, passwords, private keys\n3. **Input Validation** — missing sanitization, unsafe deserialization, prototype pollution\n4. **Auth & Authz** — missing auth checks, privilege escalation paths, session issues\n5. **Dependencies** — note any risky imports or patterns (can't check versions)"
    format = "\n\nReturn ONLY valid JSON:\n{\n  \"passed\": true/false,\n  \"score\": 0-100,\n  \"summary\": \"<one-paragraph assessment>\",\n  \"issues\": [\n    {\n      \"severity\": \"critical\" | \"major\" | \"minor\" | \"info\",\n      \"file\": \"<file path>\",\n      \"line\": <number or null>,\n      \"title\": \"<short title>\",\n      \"description\": \"<detailed explanation>\",\n      \"suggestion\": \"<how to fix>\"\n    }\n  ]\n}\nCRITICAL = exploitable vulnerability, exposed secret. MAJOR = insecure pattern, missing protection.\nMINOR = best-practice deviation. INFO = observation."
    combine_review_prompt(base, persona, skill, format)
  end

  defp build_review_system_prompt("test_review") do
    persona = load_persona("test-engineer")
    skill = load_skill_content("test-driven-development")
    base = "You are a QA engineer analyzing test quality and coverage. Review the following code and tests:\n\n1. **Coverage gaps** — which code paths are untested?\n2. **Test quality** — are tests meaningful or just coverage padding?\n3. **Edge cases** — missing boundary conditions, error paths, null/undefined\n4. **Test structure** — clarity, isolation, setup/teardown\n5. **Missing test types** — unit, integration, snapshot, e2e gaps"
    format = "\n\nReturn ONLY valid JSON:\n{\n  \"passed\": true/false,\n  \"score\": 0-100,\n  \"summary\": \"<one-paragraph assessment>\",\n  \"issues\": [\n    {\n      \"severity\": \"critical\" | \"major\" | \"minor\" | \"info\",\n      \"file\": \"<file path>\",\n      \"line\": <number or null>,\n      \"title\": \"<short title>\",\n      \"description\": \"<detailed explanation>\",\n      \"suggestion\": \"<how to fix>\"\n    }\n  ]\n}\nCRITICAL = core logic completely untested, broken test. MAJOR = significant coverage gap.\nMINOR = weak assertions, missing edge-case test. INFO = style suggestion."
    combine_review_prompt(base, persona, skill, format)
  end

  defp build_review_system_prompt("perf_audit") do
    persona = load_persona("web-performance-auditor")
    skill = load_skill_content("performance-optimization")
    base = "You are a web performance engineer auditing frontend code. Check for:\n\n1. **Bundle size** — large imports, tree-shaking issues, duplicate deps\n2. **Rendering** — unnecessary re-renders, missing memo, large component trees\n3. **Loading** — missing lazy loading, code splitting gaps, waterfall requests\n4. **Network** — unoptimized assets, missing compression hints, chatty APIs\n5. **Runtime** — memory leaks (event listeners, intervals), heavy computations on main thread\n6. **Images & Assets** — missing srcset, unoptimized formats, layout shift"
    format = "\n\nReturn ONLY valid JSON:\n{\n  \"passed\": true/false,\n  \"score\": 0-100,\n  \"summary\": \"<one-paragraph assessment>\",\n  \"issues\": [\n    {\n      \"severity\": \"critical\" | \"major\" | \"minor\" | \"info\",\n      \"file\": \"<file path>\",\n      \"line\": <number or null>,\n      \"title\": \"<short title>\",\n      \"description\": \"<detailed explanation>\",\n      \"suggestion\": \"<how to fix>\"\n    }\n  ]\n}\nCRITICAL = blocking perf issue (>3s impact). MAJOR = significant slowdown.\nMINOR = optimization opportunity. INFO = observation."
    combine_review_prompt(base, persona, skill, format)
  end

  defp combine_review_prompt(base, persona, skill, format) do
    parts = [base]
    parts = if persona != "", do: parts ++ ["\n\n--- Persona ---\n#{persona}"], else: parts
    parts = if skill != "", do: parts ++ ["\n\n--- Skill Workflow ---\n#{skill}"], else: parts
    Enum.join(parts) <> format
  end

  defp load_persona(name) do
    case HiveWeave.SkillRegistry.read_external_persona(name) do
      {:ok, content} -> content
      :not_found -> ""
    end
  end

  defp load_skill_content(slug) do
    case HiveWeave.SkillRegistry.read_external_skill_file(slug) do
      {:ok, content} -> content
      :not_found -> ""
    end
  end

  defp call_review_llm(agent, system_prompt, user_prompt) do
    model = resolve_model_for_agent(agent)
    if model == nil do
      {:error, "No LLM model configured for review"}
    else
      url = String.trim_trailing(model[:base_url] || "", "/") <> "/chat/completions"
      body = %{
        "model" => model[:model_id],
        "messages" => [
          %{"role" => "system", "content" => system_prompt},
          %{"role" => "user", "content" => user_prompt}
        ],
        "temperature" => 0.3,
        "max_tokens" => 2000
      }
      headers = [
        {"content-type", "application/json"},
        {"authorization", "Bearer #{model[:api_key] || ""}"}
      ]
      case Req.post(url, headers: headers, body: Jason.encode!(body), receive_timeout: 30_000) do
        {:ok, %{status: 200, body: resp_body}} ->
          decoded = if is_binary(resp_body), do: Jason.decode!(resp_body), else: resp_body
          content = extract_content(decoded)
          if content, do: {:ok, content}, else: {:error, :empty_response}
        {:ok, %{status: s, body: b}} -> {:error, {:http_error, s, b}}
        {:error, reason} -> {:error, reason}
      end
    end
  end

  defp resolve_model_for_agent(agent) do
    model_id = Map.get(agent, :model_id)
    if is_binary(model_id) and model_id != "" do
      {:ok, r} = HiveWeave.Repo.Meta.query(
        "SELECT id, name, model_id, base_url, api_key, context_window FROM llm_models WHERE id = ? LIMIT 1", [model_id])
      case r.rows do
        [[id, name, mid, base_url, api_key, ctx] | _] ->
          %{id: id, name: name, model_id: mid, base_url: base_url, api_key: api_key, context_window: ctx}
        _ -> nil
      end
    else
      nil
    end
  rescue
    _ -> nil
  end

  defp extract_content(%{"choices" => [%{"message" => %{"content" => content}} | _]}), do: content
  defp extract_content(_), do: nil

  # ── Web search helpers ──────────────────────────────────

  defp execute_websearch(query, limit) do
    url = "https://html.duckduckgo.com/html/?q=#{URI.encode_www_form(query)}"
    headers = [{"User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}]
    case Req.get(url, headers: headers, receive_timeout: 15_000) do
      {:ok, %{status: 200, body: html}} ->
        results = extract_search_results(html, limit)
        if results == [] do
          "No results found."
        else
          results
          |> Enum.with_index(1)
          |> Enum.map(fn {r, i} ->
            "#{i}. #{r.title}\n   #{r.url}\n   #{r.snippet}"
          end)
          |> Enum.join("\n\n")
        end
      {:ok, %{status: s}} -> "Error: Search returned HTTP #{s}"
      {:error, reason} -> "Error: Search failed — #{inspect(reason)}"
    end
  end

  defp extract_search_results(html, limit) do
    Regex.scan(~r/<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>([^<]*)<\/a>/s, html)
    |> Enum.take(limit)
    |> Enum.map(fn [_, url, title] ->
      title = title |> String.replace(~r/\s+/, " ") |> String.trim()
      snippet = extract_duckduckgo_snippet(html, url) |> then(fn s ->
        if byte_size(s) > 160, do: String.slice(s, 0, 160) <> "...", else: s
      end)
      %{title: title, url: url, snippet: snippet}
    end)
  end

  defp extract_duckduckgo_snippet(_html, _url), do: ""

  defp resolve_default_model_id do
    case Ecto.Adapters.SQL.query(HiveWeave.Repo.Meta,
      "SELECT id FROM llm_models WHERE is_active = 1 ORDER BY created_at ASC LIMIT 1", []) do
      {:ok, %{rows: [[id] | _]}} -> id
      _ -> nil
    end
  rescue
    _ -> nil
  end

  defp resolve_default_model_for_role(permission_type) do
    setting_key = if permission_type in ["coordinator", "ceo", "hr"] do
      "default_model_coordinator"
    else
      "default_model_executor"
    end

    case HiveWeave.Services.Settings.get(setting_key) do
      nil -> resolve_default_model_id()
      model_id when is_binary(model_id) ->
        case verify_model_active(model_id) do
          true -> model_id
          false -> resolve_default_model_id()
        end
    end
  end

  defp verify_model_active(model_id) do
    {:ok, r} = Ecto.Adapters.SQL.query(HiveWeave.Repo.Meta,
      "SELECT is_active FROM llm_models WHERE id = ?", [model_id])
    case r.rows do
      [[1] | _] -> true
      _ -> false
    end
  rescue
    _ -> false
  end

  # ── fetch_url helpers ──────────────────────────────────

  defp execute_fetch_url(url, format) do
    headers = [
      {"user-agent", "Mozilla/5.0 (compatible; HiveWeave/1.0)"},
      {"accept", "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8"}
    ]

    case Req.get(url, headers: headers, receive_timeout: 30_000, max_body: 5_000_000) do
      {:ok, %{status: 200, body: body, headers: resp_headers}} ->
        content_type = get_content_type(resp_headers)

        cond do
          String.contains?(content_type, "text/html") ->
            html = if is_binary(body), do: body, else: inspect(body)
            if format == "text" do
              html_to_text(html)
            else
              html_to_markdown(html)
            end
          String.contains?(content_type, "text/") ->
            if is_binary(body), do: String.slice(body, 0, 50000), else: inspect(body) |> String.slice(0, 50000)
          true ->
            preview = if is_binary(body), do: String.slice(body, 0, 1000), else: inspect(body) |> String.slice(0, 1000)
            "(binary content, #{byte_size(body)} bytes) First 1000 chars:\n#{preview}"
        end

      {:ok, %{status: s}} -> "Error: HTTP #{s}"
      {:error, reason} -> "Error: #{inspect(reason)}"
    end
  rescue
    e -> "Error: #{inspect(e)}"
  end

  defp html_to_text(html) do
    html
    |> String.replace(~r/<script[^>]*>.*?<\/script>/si, "")
    |> String.replace(~r/<style[^>]*>.*?<\/style>/si, "")
    |> String.replace(~r/<[^>]+>/, "")
    |> String.replace(~r/&nbsp;/, " ")
    |> String.replace(~r/&amp;/, "&")
    |> String.replace(~r/&lt;/, "<")
    |> String.replace(~r/&gt;/, ">")
    |> String.replace(~r/&quot;/, "\"")
    |> String.replace(~r/\s+/, " ")
    |> String.trim()
    |> String.slice(0, 50000)
  end

  defp html_to_markdown(html) do
    html
    |> String.replace(~r/<script[^>]*>.*?<\/script>/si, "")
    |> String.replace(~r/<style[^>]*>.*?<\/style>/si, "")
    |> String.replace(~r/<h1[^>]*>(.*?)<\/h1>/si, "# \\1\n\n")
    |> String.replace(~r/<h2[^>]*>(.*?)<\/h2>/si, "## \\1\n\n")
    |> String.replace(~r/<h3[^>]*>(.*?)<\/h3>/si, "### \\1\n\n")
    |> String.replace(~r/<h4[^>]*>(.*?)<\/h4>/si, "#### \\1\n\n")
    |> String.replace(~r/<p[^>]*>(.*?)<\/p>/si, "\\1\n\n")
    |> String.replace(~r/<li[^>]*>(.*?)<\/li>/si, "- \\1\n")
    |> String.replace(~r/<strong[^>]*>(.*?)<\/strong>/si, "**\\1**")
    |> String.replace(~r/<b[^>]*>(.*?)<\/b>/si, "**\\1**")
    |> String.replace(~r/<em[^>]*>(.*?)<\/em>/si, "*\\1*")
    |> String.replace(~r/<i[^>]*>(.*?)<\/i>/si, "*\\1*")
    |> String.replace(~r/<a[^>]*href="([^"]*)"[^>]*>(.*?)<\/a>/si, "[\\2](\\1)")
    |> String.replace(~r/<code[^>]*>(.*?)<\/code>/si, "`\\1`")
    |> String.replace(~r/<pre[^>]*>(.*?)<\/pre>/si, "```\n\\1\n```\n")
    |> String.replace(~r/<br\s*\/?>/i, "\n")
    |> String.replace(~r/<[^>]+>/, "")
    |> String.replace(~r/&nbsp;/, " ")
    |> String.replace(~r/&amp;/, "&")
    |> String.replace(~r/&lt;/, "<")
    |> String.replace(~r/&gt;/, ">")
    |> String.replace(~r/&quot;/, "\"")
    |> String.replace(~r/\n{3,}/, "\n\n")
    |> String.trim()
    |> String.slice(0, 50000)
  end

  defp get_content_type(headers) when is_list(headers) do
    Enum.find_value(headers, "", fn {k, v} ->
      if String.downcase(k) == "content-type", do: String.downcase(v)
    end)
  end
  defp get_content_type(headers) when is_map(headers) do
    (headers["content-type"] || headers[:content_type] || "") |> String.downcase()
  end
  defp get_content_type(_), do: ""

  # ── MCP helpers ───────────────────────────────────────────

  defp execute_mcp_call(_agent, server, tool, arguments) do
    url = guess_mcp_url(server)

    body = Jason.encode!(%{
      jsonrpc: "2.0",
      id: 1,
      method: "tools/call",
      params: %{
        name: tool,
        arguments: arguments
      }
    })

    headers = [
      {"content-type", "application/json"},
      {"accept", "application/json"}
    ]

    case Req.post(url, headers: headers, body: body, receive_timeout: 30_000) do
      {:ok, %{status: 200, body: resp}} ->
        decoded = if is_binary(resp), do: Jason.decode!(resp), else: resp
        result = get_in(decoded, ["result", "content"]) || get_in(decoded, ["result"]) || inspect(decoded)
        if is_list(result), do: Enum.map_join(result, "\n", &extract_mcp_text/1), else: to_string(result)
      {:ok, %{status: s}} -> "Error: MCP server returned HTTP #{s}"
      {:error, reason} -> "Error: MCP call failed — #{inspect(reason)}"
    end
  rescue
    e -> "Error: #{inspect(e)}"
  end

  defp guess_mcp_url(server) do
    case server do
      "github" -> "https://api.githubcopilot.com/mcp"
      "filesystem" -> "http://localhost:3456/mcp"
      "browser" -> "http://localhost:3457/mcp"
      "database" -> "http://localhost:3458/mcp"
      "slack" -> "http://localhost:3459/mcp"
      _ -> "http://localhost:#{3000 + :erlang.phash2(server, 1000)}/mcp"
    end
  end

  defp extract_mcp_text(%{"text" => text}), do: text
  defp extract_mcp_text(%{"type" => "text", "text" => text}), do: text
  defp extract_mcp_text(item) when is_binary(item), do: item
  defp extract_mcp_text(item), do: inspect(item)

  defp ensure_mcp_servers_table do
    Ecto.Adapters.SQL.query(
      HiveWeave.Repo.Meta,
      "CREATE TABLE IF NOT EXISTS mcp_servers (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, transport TEXT NOT NULL DEFAULT 'http', command TEXT DEFAULT '', url TEXT DEFAULT '', created_at INTEGER)",
      []
    )
  rescue
    _ -> :ok
  end

  # ── glob helpers ──────────────────────────────────────────────

  defp execute_glob(pattern, search_path, workspace_path) do
    # Use forward slashes for cross-platform wildcard compatibility
    fwd_search = String.replace(search_path, "\\", "/")
    full_pattern = Path.join(fwd_search, pattern)

    skip_dirs = ~w(node_modules .git .hiveweave dist build .next .nuxt .turbo coverage deps _build .elixir_ls)

    results =
      full_pattern
      |> Path.wildcard()
      |> Enum.filter(fn entry ->
        not Enum.any?(skip_dirs, fn skip ->
          segments = Path.split(entry)
          skip in segments
        end)
      end)
      |> Enum.take(500)

    if results == [] do
      "No files matching '#{pattern}'."
    else
      entries =
        results
        |> Enum.map(fn abs_path ->
          rel = Path.relative_to(abs_path, workspace_path)
          type = if File.dir?(abs_path), do: "[DIR]", else: "[FILE]"

          size =
            if File.regular?(abs_path) do
              case File.stat(abs_path) do
                {:ok, %{size: s}} when s < 1024 -> "#{s}B"
                {:ok, %{size: s}} when s < 1_048_576 -> "#{Float.round(s / 1024, 1)}KB"
                {:ok, %{size: s}} -> "#{Float.round(s / 1_048_576, 1)}MB"
                _ -> ""
              end
            else
              ""
            end

          "#{type} #{rel} #{size}"
        end)

      "## Glob: #{pattern} (#{length(entries)} results)\n#{Enum.join(entries, "\n")}"
    end
  end

end
