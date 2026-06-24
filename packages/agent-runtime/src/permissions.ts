/**
 * HiveWeave Tool Definitions — OpenAI Function Calling Format
 *
 * Tool pools:
 * - COORDINATOR_TOOLS: dispatch, review, orchestration
 * - EXECUTOR_TOOLS: work logging, completion, memory
 * - CORE_TOOLS: messaging + memory (all agents)
 * - BINDING_TOOLS: skill/MCP management (CEO/HR only)
 * - HR_TOOLS: personnel management (HR only)
 * - FILE_TOOLS: workspace file operations (executors + CEO read-only)
 * - CHARTER_TOOLS: project charter (CEO writes, CEO+HR read)
 */

// ---------------------------------------------------------------------------
// Types (OpenAI-compatible)
// ---------------------------------------------------------------------------

export interface ChatCompletionTool {
  type: "function";
  function: {
    name: string;
    description: string;
    parameters: {
      type: "object";
      properties: Record<string, { type: string; description: string; enum?: string[] }>;
      required: string[];
    };
  };
}

// ---------------------------------------------------------------------------
// Coordinator tools — dispatch, review, orchestration
// ---------------------------------------------------------------------------

const COORDINATOR_TOOLS: ChatCompletionTool[] = [
  {
    type: "function",
    function: {
      name: "hiveweave__dispatch_task",
      description: "Dispatch a task to a subordinate agent.",
      parameters: {
        type: "object",
        properties: {
          toAgentId: { type: "string", description: "Subordinate agent UUID." },
          description: { type: "string", description: "Clear, actionable task description." },
          expectReport: { type: "boolean", description: "True if subordinate must report results back. Default false." },
        },
        required: ["toAgentId", "description"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__read_work_logs",
      description: "Read recent work logs of a subordinate.",
      parameters: {
        type: "object",
        properties: {
          subordinateId: { type: "string", description: "Subordinate agent UUID." },
          limit: { type: "string", description: "Max entries to return (default 10)." },
        },
        required: ["subordinateId"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__review_code",
      description: "Review a subordinate's recent work via their work logs.",
      parameters: {
        type: "object",
        properties: {
          subordinateId: { type: "string", description: "Subordinate agent UUID." },
          limit: { type: "string", description: "Recent log entries to review (default 5)." },
        },
        required: ["subordinateId"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__approve_work",
      description: "Approve a subordinate's completed work.",
      parameters: {
        type: "object",
        properties: {
          subordinateId: { type: "string", description: "Subordinate agent UUID." },
          review: { type: "string", description: "Optional quality comment." },
        },
        required: ["subordinateId"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__reject_work",
      description: "Reject a subordinate's work with revision feedback.",
      parameters: {
        type: "object",
        properties: {
          subordinateId: { type: "string", description: "Subordinate agent UUID." },
          feedback: { type: "string", description: "What needs to be revised." },
        },
        required: ["subordinateId", "feedback"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__trigger_integration",
      description: "Trigger integration test or build for the current module.",
      parameters: {
        type: "object",
        properties: {
          module: { type: "string", description: "Module name or identifier." },
        },
        required: [],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__list_subordinates",
      description: "List direct subordinates with name, role, status, and current task.",
      parameters: { type: "object", properties: {}, required: [] },
    },
  },
];

// ---------------------------------------------------------------------------
// Executor tools — work logging, completion, memory
// ---------------------------------------------------------------------------

const EXECUTOR_TOOLS: ChatCompletionTool[] = [
  {
    type: "function",
    function: {
      name: "hiveweave__write_work_log",
      description: "Write a work log entry documenting progress.",
      parameters: {
        type: "object",
        properties: {
          type: { type: "string", description: "Log type.", enum: ["discussion", "decision", "completion", "error"] },
          summary: { type: "string", description: "Brief summary of what was done." },
        },
        required: ["summary"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__report_completion",
      description: "Report task completion. Updates status, writes completion log, and notifies coordinator.",
      parameters: {
        type: "object",
        properties: {
          summary: { type: "string", description: "What was accomplished." },
          handoffId: { type: "string", description: "Optional handoff UUID. If omitted, most recent pending handoff is completed." },
        },
        required: ["summary"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__read_project_memory",
      description: "Read shared project constitution and memories.",
      parameters: { type: "object", properties: {}, required: [] },
    },
  },
];

// ---------------------------------------------------------------------------
// Core tools — messaging + memory (available to ALL agents)
// ---------------------------------------------------------------------------

const CORE_TOOLS: ChatCompletionTool[] = [
  {
    type: "function",
    function: {
      name: "hiveweave__message_superior",
      description: "Send a message to your superior in the hierarchy.",
      parameters: {
        type: "object",
        properties: {
          message: { type: "string", description: "Clear and concise message." },
        },
        required: ["message"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__send_message",
      description: "Send a message to one or more recipients. Use \"user\" for the human operator, or agent names/IDs for colleagues. Can send to multiple recipients at once.",
      parameters: {
        type: "object",
        properties: {
          content: { type: "string", description: "Message content." },
          recipients: { type: "string", description: 'Comma-separated list of recipients. Use "user" for the human operator, and/or agent names for colleagues. Example: "user, 后端开发工程师"\nOptions: "user" = human operator; agent names or IDs for colleagues.' },
        },
        required: ["content", "recipients"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__read_roster",
      description: "Read the personnel roster (all active agents, positions, reporting structure).",
      parameters: { type: "object", properties: {}, required: [] },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__write_memory",
      description: "Save a fact, decision, or lesson to your permanent private memory. Persists across sessions.",
      parameters: {
        type: "object",
        properties: {
          type: { type: "string", description: "Memory category.", enum: ["decision", "fact", "lesson", "pattern", "preference", "progress"] },
          content: { type: "string", description: "Concise, self-contained content." },
        },
        required: ["type", "content"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__fetch_url",
      description: "Fetch web page content as text (HTML converted to markdown). Use for reading documentation, API references, or any publicly accessible URL.",
      parameters: {
        type: "object",
        properties: {
          url: { type: "string", description: "HTTP/HTTPS URL to fetch." },
          maxChars: { type: "string", description: "Max characters to return. Default 50000." },
        },
        required: ["url"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__get_project_time",
      description: "Get the current project (game) time. All inter-agent communication and deadlines use project time.",
      parameters: { type: "object", properties: {}, required: [] },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__get_real_time",
      description: "Get the current real-world time. Use ONLY when interacting with the outside world (news, web, calendar events). Convert project-time deadlines to real time before external queries.",
      parameters: { type: "object", properties: {}, required: [] },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__set_alarm",
      description: "Schedule an alarm message to yourself or another agent at a future project time. The recipient receives a queued inbox message when the alarm fires. Use this to plan reminders and avoid missing deadlines.",
      parameters: {
        type: "object",
        properties: {
          purpose: { type: "string", description: "What to remind about when the alarm fires." },
          targetAgentId: { type: "string", description: "Recipient agent name or ID. Omit to alarm yourself." },
          dueInGameDays: { type: "string", description: "Project days from now until alarm fires." },
          dueInGameHours: { type: "string", description: "Project hours from now until alarm fires." },
          dueInGameMinutes: { type: "string", description: "Project minutes from now until alarm fires." },
          dueInGameSeconds: { type: "string", description: "Project seconds from now until alarm fires." },
        },
        required: ["purpose"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__question",
      description: "Ask the human operator a question when you need a decision. Supports predefined options or free-text answer. Blocks until answered.",
      parameters: {
        type: "object",
        properties: {
          question: { type: "string", description: "The question to ask." },
          options: { type: "array", description: "Optional predefined choices [{label, description}]. Max 4." },
        },
        required: ["question"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__todowrite",
      description: "Maintain a structured task list. Pass the complete list of current todos with statuses. The human operator can see your progress.",
      parameters: {
        type: "object",
        properties: {
          todos: { type: "array", description: "Array of {content: string, status: pending|in_progress|completed|cancelled}." },
        },
        required: ["todos"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__websearch",
      description: "Search the web via DuckDuckGo. Returns titles, URLs, and snippets. No API key needed.",
      parameters: {
        type: "object",
        properties: {
          query: { type: "string", description: "Search query." },
          numResults: { type: "string", description: "Max results (default 8, max 10)." },
        },
        required: ["query"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__mcp_call",
      description: "Call a tool on a connected MCP server. Use mcp_list_tools first to see available tools.",
      parameters: {
        type: "object",
        properties: {
          serverName: { type: "string", description: "MCP server name." },
          toolName: { type: "string", description: "Tool name to call." },
          args: { type: "array", description: "Tool arguments as key-value JSON object." },
        },
        required: ["serverName", "toolName"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__mcp_list_tools",
      description: "List all available tools on a connected MCP server, or all servers if no name given.",
      parameters: {
        type: "object",
        properties: {
          serverName: { type: "string", description: "Optional: specific server name." },
        },
        required: [],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__mcp_configure",
      description: "Install or update an MCP server configuration. Pass name, transport (stdio|http), and the required fields.",
      parameters: {
        type: "object",
        properties: {
          name: { type: "string", description: "MCP server name." },
          transport: { type: "string", description: "'stdio' or 'http'." },
          command: { type: "string", description: "Command for stdio (e.g. 'npx')." },
          args: { type: "array", description: "Args for stdio (e.g. ['-y', '@anthropic/mcp-filesystem'])." },
          cwd: { type: "string", description: "Working dir for stdio." },
          url: { type: "string", description: "URL for HTTP transport." },
          enabled: { type: "string", description: "'true' or 'false' (default true)." },
        },
        required: ["name", "transport"],
      },
    },
  },
];

// ---------------------------------------------------------------------------
// Binding tools — skill/MCP management (CEO and HR only)
// ---------------------------------------------------------------------------

const BINDING_TOOLS: ChatCompletionTool[] = [
  {
    type: "function",
    function: {
      name: "hiveweave__bind_skill",
      description: "Bind a skill to an agent (self or subordinate). Use list_available_skills first.",
      parameters: {
        type: "object",
        properties: {
          agentId: { type: "string", description: "Target agent UUID." },
          skillName: { type: "string", description: "Skill name to bind." },
        },
        required: ["agentId", "skillName"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__unbind_skill",
      description: "Remove a skill binding from an agent.",
      parameters: {
        type: "object",
        properties: {
          agentId: { type: "string", description: "Target agent UUID." },
          skillName: { type: "string", description: "Skill name to unbind." },
        },
        required: ["agentId", "skillName"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__bind_mcp",
      description: "Bind an MCP server to an agent. Use list_available_mcp first.",
      parameters: {
        type: "object",
        properties: {
          agentId: { type: "string", description: "Target agent UUID." },
          mcpServer: { type: "string", description: "MCP server name." },
        },
        required: ["agentId", "mcpServer"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__unbind_mcp",
      description: "Remove an MCP server binding from an agent.",
      parameters: {
        type: "object",
        properties: {
          agentId: { type: "string", description: "Target agent UUID." },
          mcpServer: { type: "string", description: "MCP server name." },
        },
        required: ["agentId", "mcpServer"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__list_available_skills",
      description: "Search available skills from ClawHub. Use before bind_skill.",
      parameters: {
        type: "object",
        properties: {
          search: { type: "string", description: "Optional keyword filter." },
        },
        required: [],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__get_skill_detail",
      description: "Get full details of a skill from ClawHub before binding.",
      parameters: {
        type: "object",
        properties: {
          slug: { type: "string", description: "Skill slug from list_available_skills." },
        },
        required: ["slug"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__read_skill",
      description: "Load full SKILL.md for a bound skill. Use when a task matches a skill's description.",
      parameters: {
        type: "object",
        properties: {
          slug: { type: "string", description: "Bound skill slug to load." },
        },
        required: ["slug"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__list_available_mcp",
      description: "List available MCP servers. Use before bind_mcp.",
      parameters: { type: "object", properties: {}, required: [] },
    },
  },
];

// ---------------------------------------------------------------------------
// HR-only tools — personnel management
// ---------------------------------------------------------------------------

const HR_TOOLS: ChatCompletionTool[] = [
  {
    type: "function",
    function: {
      name: "hiveweave__create_agent",
      description: "Create a new agent. Never set parentId to yourself (HR). Place under CEO or requesting manager.",
      parameters: {
        type: "object",
        properties: {
          name: { type: "string", description: "Agent display name." },
          role: { type: "string", description: "Agent role (hr, architect, manager, developer, qa, devops, etc.)." },
          goal: { type: "string", description: "Core objective for this agent." },
          backstory: { type: "string", description: "Optional persona description." },
          permissionType: { type: "string", description: "coordinator or executor. Default: executor.", enum: ["coordinator", "executor"] },
          parentId: { type: "string", description: "Parent agent UUID. Defaults to CEO if omitted." },
          position: { type: "string", description: "Job position/title." },
          department: { type: "string", description: "Department name." },
          responsibilities: { type: "string", description: "Key responsibilities." },
          skills: { type: "string", description: "Comma-separated skill names to bind at creation." },
          mcpServers: { type: "string", description: "Comma-separated MCP server names to bind at creation." },
        },
        required: ["name", "role", "goal"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__transfer_agent",
      description: "Transfer an agent to a different parent in the hierarchy.",
      parameters: {
        type: "object",
        properties: {
          agentId: { type: "string", description: "Agent UUID to transfer." },
          newParentId: { type: "string", description: "New parent UUID. Empty for root-level." },
        },
        required: ["agentId"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__dismiss_agent",
      description: "Dismiss (archive) an agent. Must have no active subordinates first.",
      parameters: {
        type: "object",
        properties: {
          agentId: { type: "string", description: "Agent UUID to dismiss." },
          reason: { type: "string", description: "Reason for dismissal." },
        },
        required: ["agentId"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__update_roster",
      description: "Update an agent's personnel record (position, department, responsibilities, status).",
      parameters: {
        type: "object",
        properties: {
          agentId: { type: "string", description: "Agent UUID." },
          position: { type: "string", description: "Updated position." },
          department: { type: "string", description: "Updated department." },
          responsibilities: { type: "string", description: "Updated responsibilities." },
          notes: { type: "string", description: "Additional notes." },
          status: { type: "string", description: "Updated status.", enum: ["active", "inactive", "probation", "terminated"] },
        },
        required: ["agentId"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__list_all_agents",
      description: "List ALL agents with full hierarchy, roles, and status.",
      parameters: { type: "object", properties: {}, required: [] },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__browse_templates",
      description: "Browse agent templates from the catalog. Filter by division or search keyword.",
      parameters: {
        type: "object",
        properties: {
          division: { type: "string", description: "Filter: engineering, design, marketing, sales, product, etc." },
          search: { type: "string", description: "Search keyword." },
          role: { type: "string", description: "Filter by role: developer, qa, designer, manager, etc." },
        },
        required: [],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__create_from_template",
      description: "Create an agent from a template. Use browse_templates first to find a template ID.",
      parameters: {
        type: "object",
        properties: {
          templateId: { type: "string", description: "Template UUID from browse_templates." },
          name: { type: "string", description: "Optional name override." },
          parentId: { type: "string", description: "Parent agent UUID. Root-level if omitted." },
          permissionType: { type: "string", description: "coordinator or executor.", enum: ["coordinator", "executor"] },
          position: { type: "string", description: "Job position. Defaults to template name." },
          department: { type: "string", description: "Department. Defaults to template division." },
          skills: { type: "string", description: "Comma-separated skill names." },
          mcpServers: { type: "string", description: "Comma-separated MCP server names." },
        },
        required: ["templateId"],
      },
    },
  },
];

// ---------------------------------------------------------------------------
// File tools — workspace file operations (executors only; CEO gets read-only subset)
// ---------------------------------------------------------------------------

const FILE_TOOLS: ChatCompletionTool[] = [
  {
    type: "function",
    function: {
      name: "hiveweave__read_file",
      description: "Read file contents with line numbers. Always read before editing.",
      parameters: {
        type: "object",
        properties: {
          filePath: { type: "string", description: "Path relative to workspace root." },
          offset: { type: "number", description: "Start line (0-based). Default 0." },
          limit: { type: "number", description: "Max lines (default 2000)." },
        },
        required: ["filePath"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__write_file",
      description: "Write/create a file. Supports append mode. Skips if unchanged; rejects if file modified since last read.",
      parameters: {
        type: "object",
        properties: {
          filePath: { type: "string", description: "Path relative to workspace root." },
          content: { type: "string", description: "File content." },
          append: { type: "boolean", description: "Append instead of overwrite. Default false." },
        },
        required: ["filePath", "content"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__edit_file",
      description: "Find-and-replace edit. oldText must match exactly once. Read file first.",
      parameters: {
        type: "object",
        properties: {
          filePath: { type: "string", description: "Path relative to workspace root." },
          oldText: { type: "string", description: "Exact text to find. Must match once. Empty to create file." },
          newText: { type: "string", description: "Replacement text. Empty to delete." },
        },
        required: ["filePath", "oldText", "newText"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__list_files",
      description: "List directory contents (files + subdirs with sizes).",
      parameters: {
        type: "object",
        properties: {
          dirPath: { type: "string", description: "Directory path. '.' or empty for root." },
          recursive: { type: "boolean", description: "List recursively. Default true." },
        },
        required: [],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__search_files",
      description: "Search file contents with regex. Returns matching lines with file paths.",
      parameters: {
        type: "object",
        properties: {
          pattern: { type: "string", description: "Regex pattern (case-insensitive)." },
          searchPath: { type: "string", description: "Directory to search. Empty for entire workspace." },
          include: { type: "string", description: "File extension filter (e.g. '*.ts')." },
        },
        required: ["pattern"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__delete_file",
      description: "Delete a single file. Cannot delete directories. Sensitive files are protected.",
      parameters: {
        type: "object",
        properties: {
          filePath: { type: "string", description: "Path relative to workspace root." },
        },
        required: ["filePath"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__bash",
      description: "Execute a shell command with full bash features (heredoc, pipes, multi-line). Uses Git Bash on Windows. Supports ssh, docker, git, npm, python and any CLI tool. Default timeout: 2 min, max: 10 min.",
      parameters: {
        type: "object",
        properties: {
          command: { type: "string", description: "Shell command string to execute. Supports heredoc, pipes, && and ; separators." },
          workdir: { type: "string", description: "Working directory. Defaults to workspace root." },
          timeout: { type: "string", description: `Timeout in ms. Default ${2*60*1000}, max ${10*60*1000}.` },
        },
        required: ["command"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__run_command",
      description: "Execute a shell command in the workspace. Must be non-interactive.",
      parameters: {
        type: "object",
        properties: {
          command: { type: "string", description: "Shell command to run." },
          cwd: { type: "string", description: "Subdirectory to run in. Default: workspace root." },
          timeout: { type: "string", description: "Max ms (default 120000, max 600000)." },
        },
        required: ["command"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__grep",
      description: "Search files with regex pattern. Returns file paths with matching line numbers and content.",
      parameters: {
        type: "object",
        properties: {
          pattern: { type: "string", description: "Regular expression to search for." },
          path: { type: "string", description: "File/directory to search. Default: workspace root." },
          include: { type: "string", description: "Glob filter (e.g. '*.ts')." },
          head_limit: { type: "number", description: "Max results. Default 100." },
        },
        required: ["pattern"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__apply_patch",
      description: "Apply structured patches (add/update/delete files). Each patch entry specifies op, filePath, and content or oldString/newString.",
      parameters: {
        type: "object",
        properties: {
          description: { type: "string", description: "Summary of what this patch does." },
          patches: { type: "array", description: "Array of {op, filePath, content?, oldString?, newString?}." },
        },
        required: ["patches"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__glob",
      description: "Find files by glob pattern. Returns paths sorted by modification time.",
      parameters: {
        type: "object",
        properties: {
          pattern: { type: "string", description: "Glob pattern (e.g. '**/*.ts')." },
          cwd: { type: "string", description: "Search within subdirectory." },
          limit: { type: "string", description: "Max results. Default 500." },
        },
        required: ["pattern"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__fetch_url",
      description: "Fetch URL content as text (HTML converted to markdown). Public URLs only.",
      parameters: {
        type: "object",
        properties: {
          url: { type: "string", description: "HTTP/HTTPS URL." },
          maxChars: { type: "string", description: "Max characters. Default 50000." },
        },
        required: ["url"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__move_file",
      description: "Move or rename a file/directory within the workspace.",
      parameters: {
        type: "object",
        properties: {
          source: { type: "string", description: "Current path." },
          destination: { type: "string", description: "New path." },
          overwrite: { type: "boolean", description: "Overwrite existing. Default false." },
        },
        required: ["source", "destination"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__create_directory",
      description: "Create a directory (including parent directories).",
      parameters: {
        type: "object",
        properties: {
          path: { type: "string", description: "Directory path relative to workspace root." },
        },
        required: ["path"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__delete_directory",
      description: "Delete an empty directory.",
      parameters: {
        type: "object",
        properties: {
          path: { type: "string", description: "Directory path relative to workspace root." },
        },
        required: ["path"],
      },
    },
  },
];

// ---------------------------------------------------------------------------
// Charter tools — project charter (CEO writes, CEO+HR read)
// ---------------------------------------------------------------------------

const CHARTER_TOOLS: ChatCompletionTool[] = [
  {
    type: "function",
    function: {
      name: "hiveweave__save_charter",
      description: "Save/update the project charter. CEO-only.",
      parameters: {
        type: "object",
        properties: {
          charterJson: { type: "string", description: "Full ProjectCharter JSON string." },
        },
        required: ["charterJson"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__read_charter",
      description: "Read the current project charter.",
      parameters: { type: "object", properties: {}, required: [] },
    },
  },
];

// ---------------------------------------------------------------------------
// Tool name sets for pickTools()
// ---------------------------------------------------------------------------

const CEO_COORDINATOR_TOOL_NAMES = new Set([
  "hiveweave__dispatch_task",
  "hiveweave__read_work_logs",
  "hiveweave__review_code",
  "hiveweave__approve_work",
  "hiveweave__reject_work",
  "hiveweave__list_subordinates",
]);

const MESSAGE_SUPERIOR_TOOL_NAMES = new Set(["hiveweave__message_superior"]);
const CORE_TOOLS_NAMES = new Set([
  "hiveweave__message_superior",
  "hiveweave__send_message",
  "hiveweave__read_roster",
  "hiveweave__write_memory",
  "hiveweave__fetch_url",
  "hiveweave__get_project_time",
  "hiveweave__get_real_time",
  "hiveweave__set_alarm",
  "hiveweave__question",
  "hiveweave__todowrite",
  "hiveweave__websearch",
  "hiveweave__mcp_call",
  "hiveweave__mcp_list_tools",
  "hiveweave__mcp_configure",
  "hiveweave__list_available_mcp",
  "hiveweave__bind_mcp",
  "hiveweave__unbind_mcp",
]);
const BINDING_AND_MEMORY_TOOL_NAMES = new Set([
  "hiveweave__bind_skill",
  "hiveweave__unbind_skill",
  "hiveweave__bind_mcp",
  "hiveweave__unbind_mcp",
  "hiveweave__list_available_skills",
  "hiveweave__get_skill_detail",
  "hiveweave__read_skill",
  "hiveweave__list_available_mcp",
  "hiveweave__write_memory",
]);
const READ_PROJECT_MEMORY_TOOL_NAMES = new Set(["hiveweave__read_project_memory"]);
const LIST_ALL_AGENTS_TOOL_NAMES = new Set(["hiveweave__list_all_agents"]);
const READ_CHARTER_TOOL_NAMES = new Set(["hiveweave__read_charter"]);
const SAVE_CHARTER_TOOL_NAMES = new Set(["hiveweave__save_charter"]);

const CEO_READONLY_FILE_TOOL_NAMES = new Set([
  "hiveweave__read_file",
  "hiveweave__list_files",
  "hiveweave__search_files",
  "hiveweave__glob",
  "hiveweave__grep",
  "hiveweave__fetch_url",
]);

const HR_PERSONNEL_TOOL_NAMES = new Set([
  "hiveweave__create_agent",
  "hiveweave__transfer_agent",
  "hiveweave__dismiss_agent",
  "hiveweave__update_roster",
  "hiveweave__browse_templates",
  "hiveweave__create_from_template",
]);

function pickTools(pool: ChatCompletionTool[], names: Set<string>): ChatCompletionTool[] {
  return pool.filter((t) => names.has(t.function.name));
}

function uniqueTools(tools: ChatCompletionTool[]): ChatCompletionTool[] {
  const seen = new Set<string>();
  const out: ChatCompletionTool[] = [];
  for (const t of tools) {
    if (seen.has(t.function.name)) continue;
    seen.add(t.function.name);
    out.push(t);
  }
  return out;
}

/**
 * Get tool definitions for an agent based on permission level and role.
 *
 * Tool allocation:
 * - CEO: coordinator tools + charter + binding + read-only files + memory
 * - HR: message_superior + peer/roster + personnel + list_all + charter_read + memory + binding
 * - Coordinator: coordinator tools + core tools (NO file tools, NO binding tools)
 * - Executor: executor tools + core tools + file tools (NO binding tools)
 */
export function getHiveWeaveTools(
  permissionType: "coordinator" | "executor",
  role: string = "",
): ChatCompletionTool[] {
  const normalizedRole = role.toLowerCase();
  const allPools = [...COORDINATOR_TOOLS, ...EXECUTOR_TOOLS, ...CORE_TOOLS, ...BINDING_TOOLS, ...HR_TOOLS, ...CHARTER_TOOLS, ...FILE_TOOLS];

  if (normalizedRole === "ceo") {
    return uniqueTools([
      ...pickTools(COORDINATOR_TOOLS, CEO_COORDINATOR_TOOL_NAMES),
      ...pickTools(CHARTER_TOOLS, SAVE_CHARTER_TOOL_NAMES),
      ...pickTools(CHARTER_TOOLS, READ_CHARTER_TOOL_NAMES),
      ...pickTools(CORE_TOOLS, CORE_TOOLS_NAMES),
      ...pickTools(HR_TOOLS, LIST_ALL_AGENTS_TOOL_NAMES),
      ...pickTools(BINDING_TOOLS, BINDING_AND_MEMORY_TOOL_NAMES),
      ...pickTools(EXECUTOR_TOOLS, READ_PROJECT_MEMORY_TOOL_NAMES),
      ...pickTools(FILE_TOOLS, CEO_READONLY_FILE_TOOL_NAMES),
    ]);
  }

  if (normalizedRole === "hr") {
    return uniqueTools([
      ...pickTools(CORE_TOOLS, MESSAGE_SUPERIOR_TOOL_NAMES),
      ...pickTools(CORE_TOOLS, CORE_TOOLS_NAMES),
      ...pickTools(HR_TOOLS, HR_PERSONNEL_TOOL_NAMES),
      ...pickTools(HR_TOOLS, LIST_ALL_AGENTS_TOOL_NAMES),
      ...pickTools(CHARTER_TOOLS, READ_CHARTER_TOOL_NAMES),
      ...pickTools(BINDING_TOOLS, BINDING_AND_MEMORY_TOOL_NAMES),
    ]);
  }

  // Coordinator: coordination + core messaging + memory — NO file tools, NO binding tools
  if (permissionType === "coordinator") {
    return [...COORDINATOR_TOOLS, ...CORE_TOOLS];
  }

  // Executor: work tools + core messaging + memory + full file tools — NO binding tools
  return [...EXECUTOR_TOOLS, ...CORE_TOOLS, ...FILE_TOOLS];
}
