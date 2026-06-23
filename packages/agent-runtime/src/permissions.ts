/**
 * HiveWeave Tool Definitions — OpenAI Function Calling Format
 *
 * Defines the tools available to agents based on their permission level:
 * - Coordinator: dispatch, review, and orchestration tools
 * - Executor: work logging, completion reporting, memory access
 * - HR: all coordinator tools + personnel management tools (hire, transfer, dismiss, roster)
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
      description:
        "Dispatch a task to a subordinate agent. The subordinate will receive this task and should begin working on it.",
      parameters: {
        type: "object",
        properties: {
          toAgentId: {
            type: "string",
            description: "UUID of the subordinate agent to dispatch the task to.",
          },
          description: {
            type: "string",
            description: "A clear, actionable description of the task to be performed.",
          },
          expectReport: {
            type: "boolean",
            description: "Set to true if you need the subordinate to report results back via message_superior when done. Use only when results must be relayed back (e.g. information queries). Defaults to false for fire-and-forget tasks.",
          },
        },
        required: ["toAgentId", "description"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__read_work_logs",
      description:
        "Read recent work logs of a subordinate agent to understand their progress.",
      parameters: {
        type: "object",
        properties: {
          subordinateId: {
            type: "string",
            description: "UUID of the subordinate agent whose logs to read.",
          },
          limit: {
            type: "string",
            description: "Maximum number of log entries to return (default 10).",
          },
        },
        required: ["subordinateId"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__review_code",
      description:
        "Review a subordinate's recent work by reading their work logs as a code review proxy.",
      parameters: {
        type: "object",
        properties: {
          subordinateId: {
            type: "string",
            description: "UUID of the subordinate agent to review.",
          },
          limit: {
            type: "string",
            description: "Number of recent log entries to review (default 5).",
          },
        },
        required: ["subordinateId"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__approve_work",
      description:
        "Approve a subordinate's completed work. Records the approval in the work log.",
      parameters: {
        type: "object",
        properties: {
          subordinateId: {
            type: "string",
            description: "UUID of the subordinate whose work is approved.",
          },
          review: {
            type: "string",
            description: "Optional review comment about the quality of the work.",
          },
        },
        required: ["subordinateId"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__reject_work",
      description:
        "Reject a subordinate's work with feedback for revision. Records the rejection in the work log.",
      parameters: {
        type: "object",
        properties: {
          subordinateId: {
            type: "string",
            description: "UUID of the subordinate whose work is rejected.",
          },
          feedback: {
            type: "string",
            description: "Explanation of what needs to be revised or improved.",
          },
        },
        required: ["subordinateId", "feedback"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__trigger_integration",
      description:
        "Trigger an integration test or build process for the current module.",
      parameters: {
        type: "object",
        properties: {
          module: {
            type: "string",
            description: "Name or identifier of the module to integrate.",
          },
        },
        required: [],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__list_subordinates",
      description:
        "List all your direct subordinates with their name, role, status, and current task information. Use this to check your team roster before dispatching tasks.",
      parameters: {
        type: "object",
        properties: {},
        required: [],
      },
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
      description:
        "Write a work log entry documenting progress on a task. Use this to record what you did.",
      parameters: {
        type: "object",
        properties: {
          type: {
            type: "string",
            description: "Log entry type.",
            enum: ["discussion", "decision", "completion", "error"],
          },
          summary: {
            type: "string",
            description: "Brief summary of what was done or decided.",
          },
        },
        required: ["summary"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__report_completion",
      description:
        "Report that a task has been completed. This updates the agent status, writes a completion log, and marks the associated task handoff as completed. If you received a pending task from your coordinator, calling this will notify them.",
      parameters: {
        type: "object",
        properties: {
          summary: {
            type: "string",
            description: "Summary of what was accomplished.",
          },
          handoffId: {
            type: "string",
            description: "Optional UUID of the specific task handoff being completed. If omitted, the most recent pending/accepted handoff is completed automatically.",
          },
        },
        required: ["summary"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__read_project_memory",
      description:
        "Read the shared project constitution and memories. Use this to understand project-wide context.",
      parameters: {
        type: "object",
        properties: {},
        required: [],
      },
    },
  },
];

// ---------------------------------------------------------------------------
// Shared tools — available to ALL agents (coordinators, executors, and HR)
// ---------------------------------------------------------------------------

const SHARED_TOOLS: ChatCompletionTool[] = [
  {
    type: "function",
    function: {
      name: "hiveweave__message_superior",
      description:
        "Send a message to your superior (the coordinator/manager above you in the hierarchy). Use this to ask questions, request clarification, report issues, or provide updates that your superior should know about.",
      parameters: {
        type: "object",
        properties: {
          message: {
            type: "string",
            description: "The message to send to your superior. Be clear and concise.",
          },
        },
        required: ["message"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__message_peer",
      description:
        "Send a message to a peer agent for collaboration or information exchange. Use this when you need to coordinate with another agent at a similar level, share findings, or request assistance from a peer.",
      parameters: {
        type: "object",
        properties: {
          toAgentId: {
            type: "string",
            description: "UUID of the peer agent to message.",
          },
          message: {
            type: "string",
            description: "The message to send to the peer. Be clear about what you need or want to share.",
          },
          expectReport: {
            type: "boolean",
            description: "Set to true if you need the peer to reply to your message. Defaults to false.",
          },
        },
        required: ["toAgentId", "message"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__read_roster",
      description:
        "Read the current personnel roster (staffing table) for the project. Shows all active agents with their position, department, responsibilities, and reporting structure. Use this to understand the team composition.",
      parameters: {
        type: "object",
        properties: {},
        required: [],
      },
    },
  },
  // ── Binding tools — manage skills and MCP server bindings ──
  // Following OpenCode/OpenClaw pattern: per-agent tool/skill binding via
  // configuration, tools injected alongside built-in tools at runtime.
  {
    type: "function",
    function: {
      name: "hiveweave__bind_skill",
      description:
        "Bind (assign) a skill to an agent. The agent will gain access to this skill's capabilities. You can bind skills to yourself or to your subordinates. Use list_available_skills first to see what skills are available.",
      parameters: {
        type: "object",
        properties: {
          agentId: {
            type: "string",
            description: "UUID of the target agent (can be yourself or a subordinate).",
          },
          skillName: {
            type: "string",
            description: "Name of the skill to bind. Use list_available_skills to see available options.",
          },
        },
        required: ["agentId", "skillName"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__unbind_skill",
      description:
        "Remove a skill binding from an agent. The agent will lose access to this skill's capabilities. You can unbind skills from yourself or your subordinates.",
      parameters: {
        type: "object",
        properties: {
          agentId: {
            type: "string",
            description: "UUID of the target agent (can be yourself or a subordinate).",
          },
          skillName: {
            type: "string",
            description: "Name of the skill to unbind.",
          },
        },
        required: ["agentId", "skillName"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__bind_mcp",
      description:
        "Bind (connect) an MCP server to an agent. The agent will gain access to the MCP server's tools at runtime. You can bind MCP servers to yourself or your subordinates. Use list_available_mcp first to see what MCP servers are available.",
      parameters: {
        type: "object",
        properties: {
          agentId: {
            type: "string",
            description: "UUID of the target agent (can be yourself or a subordinate).",
          },
          mcpServer: {
            type: "string",
            description: "Name/identifier of the MCP server to bind. Use list_available_mcp to see available options.",
          },
        },
        required: ["agentId", "mcpServer"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__unbind_mcp",
      description:
        "Remove an MCP server binding from an agent. The agent will lose access to the MCP server's tools. You can unbind MCP servers from yourself or your subordinates.",
      parameters: {
        type: "object",
        properties: {
          agentId: {
            type: "string",
            description: "UUID of the target agent (can be yourself or a subordinate).",
          },
          mcpServer: {
            type: "string",
            description: "Name/identifier of the MCP server to unbind.",
          },
        },
        required: ["agentId", "mcpServer"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__list_available_skills",
      description:
        "Search and list available skills from the ClawHub skill registry (https://clawhub.ai). Returns skill names, descriptions, and stats. Use this before calling bind_skill to discover what skills exist. Supports keyword search.",
      parameters: {
        type: "object",
        properties: {
          search: {
            type: "string",
            description: "Optional search keyword to filter skills (e.g. 'code review', 'testing', 'database'). Leave empty to list recent skills.",
          },
        },
        required: [],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__get_skill_detail",
      description:
        "Get the full details of a specific skill from ClawHub, including its complete SKILL.md instructions, metadata, and owner info. Use this after list_available_skills to inspect a skill before deciding to bind it.",
      parameters: {
        type: "object",
        properties: {
          slug: {
            type: "string",
            description: "The skill slug (e.g. 'clawseccheck', 'pixellab-ai'). Use the slug returned by list_available_skills.",
          },
        },
        required: ["slug"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__read_skill",
      description:
        "Load the full SKILL.md instructions for a skill that is bound to you. Your Active Skills section shows only summaries to save context — use this tool when a task matches a skill's description and you need its detailed instructions to proceed. Only works for skills already bound to your agent.",
      parameters: {
        type: "object",
        properties: {
          slug: {
            type: "string",
            description: "The slug of the bound skill to load (e.g. 'clawseccheck'). Must be one of your active skills.",
          },
        },
        required: ["slug"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__list_available_mcp",
      description:
        "List all available MCP servers that can be bound to agents. Returns server names and descriptions. Use this before calling bind_mcp to discover what MCP servers exist in the system.",
      parameters: {
        type: "object",
        properties: {},
        required: [],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__write_memory",
      description:
        "Save an important fact, decision, or lesson to your permanent private memory. This information persists across sessions and server restarts — it will always be available in your context, even when conversation history is trimmed. Use this proactively for: key architectural decisions, important facts about the project, lessons learned, work patterns, or anything you need to remember long-term. Memory is private to you and does not affect other agents.",
      parameters: {
        type: "object",
        properties: {
          type: {
            type: "string",
            description: "Category of the memory.",
            enum: ["decision", "fact", "lesson", "pattern", "preference", "progress"],
          },
          content: {
            type: "string",
            description: "The memory content. Be concise but complete — this should be understandable on its own without additional context.",
          },
        },
        required: ["type", "content"],
      },
    },
  },
];

// ---------------------------------------------------------------------------
// HR-only tools — personnel management (only available to agents with role "hr")
// ---------------------------------------------------------------------------

const HR_TOOLS: ChatCompletionTool[] = [
  {
    type: "function",
    function: {
      name: "hiveweave__create_agent",
      description:
        "Create (hire/recruit) a new agent in the organization. parentId is REQUIRED unless you intentionally place under the CEO: set parentId to the CEO or a business manager (coordinator) who should own the new hire. Never set parentId to yourself (HR). Do not create root-level agents unless the CEO explicitly directs it. Use this to execute staffing per the project charter.",
      parameters: {
        type: "object",
        properties: {
          name: {
            type: "string",
            description: "Display name of the new agent (e.g. '前端开发专家', '测试工程师', '产品经理').",
          },
          role: {
            type: "string",
            description: "Role of the new agent. Common roles: hr, architect, manager, developer, qa, devops. You may also use custom role names as needed.",
          },
          goal: {
            type: "string",
            description: "The core objective/goal description for this agent. Be specific about what they should accomplish.",
          },
          backstory: {
            type: "string",
            description: "Optional background story or persona description that influences the agent's communication style and expertise.",
          },
          permissionType: {
            type: "string",
            description: "Whether this agent is a coordinator (manages sub-agents) or executor (does the work directly). Default: executor.",
            enum: ["coordinator", "executor"],
          },
          parentId: {
            type: "string",
            description: "UUID of the parent agent in the org hierarchy. Required in practice: use the CEO or the requesting business manager. If omitted, defaults to the CEO agent (not root).",
          },
          position: {
            type: "string",
            description: "Job position/title for the roster (e.g. '前端开发工程师', '测试负责人').",
          },
          department: {
            type: "string",
            description: "Department the agent belongs to (e.g. '工程部', '产品部', '质量部').",
          },
          responsibilities: {
            type: "string",
            description: "Description of the agent's key responsibilities.",
          },
          skills: {
            type: "string",
            description: "Comma-separated list of skill names to bind to this agent at creation (e.g. 'code-review,testing'). Leave empty for no initial skills. Use list_available_skills to see options.",
          },
          mcpServers: {
            type: "string",
            description: "Comma-separated list of MCP server names to bind to this agent at creation (e.g. 'github,filesystem'). Leave empty for no initial MCP servers. Use list_available_mcp to see options.",
          },
        },
        required: ["name", "role", "goal"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__transfer_agent",
      description:
        "Transfer (re-assign) an existing agent to a different parent in the organization hierarchy. Use this to restructure the team.",
      parameters: {
        type: "object",
        properties: {
          agentId: {
            type: "string",
            description: "UUID of the agent to transfer.",
          },
          newParentId: {
            type: "string",
            description: "UUID of the new parent agent. Pass empty string or null to make the agent root-level.",
          },
        },
        required: ["agentId"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__dismiss_agent",
      description:
        "Dismiss (terminate) an agent from the organization. The agent will be archived (soft-delete) and removed from the active roster. The agent must have no active subordinates — transfer or dismiss subordinates first.",
      parameters: {
        type: "object",
        properties: {
          agentId: {
            type: "string",
            description: "UUID of the agent to dismiss.",
          },
          reason: {
            type: "string",
            description: "Reason for dismissal.",
          },
        },
        required: ["agentId"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__update_roster",
      description:
        "Update the roster (personnel record) for an existing agent. Use this to change position, department, responsibilities, or notes.",
      parameters: {
        type: "object",
        properties: {
          agentId: {
            type: "string",
            description: "UUID of the agent whose roster record to update.",
          },
          position: {
            type: "string",
            description: "Updated job position/title.",
          },
          department: {
            type: "string",
            description: "Updated department.",
          },
          responsibilities: {
            type: "string",
            description: "Updated responsibilities.",
          },
          notes: {
            type: "string",
            description: "Additional notes about the agent.",
          },
          status: {
            type: "string",
            description: "Updated status.",
            enum: ["active", "inactive", "probation", "terminated"],
          },
        },
        required: ["agentId"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__list_all_agents",
      description:
        "List ALL agents in the organization with their full hierarchy, roles, and status. Unlike list_subordinates (which only shows direct children), this shows the entire org tree in a flat list with path information.",
      parameters: {
        type: "object",
        properties: {},
        required: [],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__browse_templates",
      description:
        "Browse available agent templates from the template catalog. Templates are pre-built professional personas that can be used to quickly create agents with well-defined roles. Use this to discover what specialist agents are available for recruitment. Filter by division (e.g. engineering, design, marketing) or search by keyword.",
      parameters: {
        type: "object",
        properties: {
          division: {
            type: "string",
            description: "Filter by division/category. Available: engineering, design, marketing, sales, product, project-management, testing, security, finance, game-development, academic, specialized, support, and more.",
          },
          search: {
            type: "string",
            description: "Search keyword to filter templates by name, vibe, or description.",
          },
          role: {
            type: "string",
            description: "Filter by HiveWeave role: developer, qa, designer, manager, marketing, sales, specialist, etc.",
          },
        },
        required: [],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__create_from_template",
      description:
        "Create a new agent from a template. The template provides the agent's name, role, goal, and backstory automatically. You can optionally override the name or set a parent in the hierarchy. Use browse_templates first to find a suitable template ID.",
      parameters: {
        type: "object",
        properties: {
          templateId: {
            type: "string",
            description: "UUID of the template to use. Get this from browse_templates results.",
          },
          name: {
            type: "string",
            description: "Optional: override the template's default agent name.",
          },
          parentId: {
            type: "string",
            description: "UUID of the parent agent in the org hierarchy. If omitted, the new agent becomes root-level.",
          },
          permissionType: {
            type: "string",
            description: "Whether this agent is a coordinator or executor. Default comes from template if not specified.",
            enum: ["coordinator", "executor"],
          },
          position: {
            type: "string",
            description: "Job position/title for the roster. Defaults to template name.",
          },
          department: {
            type: "string",
            description: "Department for the roster. Defaults to template division.",
          },
          skills: {
            type: "string",
            description: "Comma-separated list of skill names to bind to this agent at creation. Leave empty for no initial skills. Use list_available_skills to see options.",
          },
          mcpServers: {
            type: "string",
            description: "Comma-separated list of MCP server names to bind to this agent at creation. Leave empty for no initial MCP servers. Use list_available_mcp to see options.",
          },
        },
        required: ["templateId"],
      },
    },
  },
];

// ---------------------------------------------------------------------------
// File tools — available to ALL agents (workspace file operations)
// ---------------------------------------------------------------------------

const FILE_TOOLS: ChatCompletionTool[] = [
  {
    type: "function",
    function: {
      name: "hiveweave__read_file",
      description:
        "Read the contents of a file in the project workspace. Returns file content with line numbers. Use this to examine existing code, configuration, or any text file before making changes. Always read a file before editing it.",
      parameters: {
        type: "object",
        properties: {
          filePath: {
            type: "string",
            description: "Path to the file relative to the workspace root (e.g. 'src/index.ts', 'README.md').",
          },
          offset: {
            type: "number",
            description: "Line number to start reading from (0-based). Use this to skip the beginning of large files.",
          },
          limit: {
            type: "number",
            description: "Maximum number of lines to read (default: 2000). Use for reading portions of large files.",
          },
        },
        required: ["filePath"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__write_file",
      description:
        "Write content to a file in the project workspace. Creates the file if it doesn't exist, overwrites if it does. Parent directories are created automatically. Supports append mode. Safety: skips write if content is unchanged; rejects overwrite if file was modified since your last read (read it again first).",
      parameters: {
        type: "object",
        properties: {
          filePath: {
            type: "string",
            description: "Path to the file relative to the workspace root (e.g. 'src/components/Header.tsx').",
          },
          content: {
            type: "string",
            description: "The content to write. For append mode, this is added to the end of the file.",
          },
          append: {
            type: "boolean",
            description: "If true, append content to the end of the file instead of overwriting. Default: false.",
          },
        },
        required: ["filePath", "content"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__edit_file",
      description:
        "Make a precise find-and-replace edit in a file. The oldText must match exactly once in the file (including whitespace and indentation). Use this for targeted modifications instead of rewriting the entire file. Read the file first to ensure you have the exact text.",
      parameters: {
        type: "object",
        properties: {
          filePath: {
            type: "string",
            description: "Path to the file relative to the workspace root.",
          },
          oldText: {
            type: "string",
            description: "The exact text to find and replace. Must match precisely once (including whitespace/indentation). Leave empty to create a new file.",
          },
          newText: {
            type: "string",
            description: "The replacement text. Leave empty to delete the matched text.",
          },
        },
        required: ["filePath", "oldText", "newText"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__list_files",
      description:
        "List the contents of a directory in the project workspace. Shows files and subdirectories in a tree format with file sizes. Automatically skips node_modules, .git, dist, and other common ignored directories.",
      parameters: {
        type: "object",
        properties: {
          dirPath: {
            type: "string",
            description: "Directory path relative to the workspace root (e.g. 'src/components'). Leave empty or use '.' for the workspace root.",
          },
          recursive: {
            type: "boolean",
            description: "Whether to list subdirectories recursively. Default: true.",
          },
        },
        required: [],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__search_files",
      description:
        "Search file contents in the project workspace using a regex pattern (like grep). Returns matching lines with file paths and line numbers. Useful for finding where specific code, functions, or patterns are used across the project.",
      parameters: {
        type: "object",
        properties: {
          pattern: {
            type: "string",
            description: "Regex pattern to search for (case-insensitive). Example: 'function\\\\s+handleAuth' to find function definitions.",
          },
          searchPath: {
            type: "string",
            description: "Directory to search in, relative to workspace root. Leave empty to search the entire workspace.",
          },
          include: {
            type: "string",
            description: "File extension filter (e.g. '*.ts', '*.tsx'). Only matching files will be searched.",
          },
        },
        required: ["pattern"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__delete_file",
      description:
        "Delete a single file from the project workspace. Use this to remove files that are no longer needed (e.g. after refactoring or renaming). Cannot delete directories — use delete_directory or run_command for that. Sensitive files (keys, .env, credentials) are protected.",
      parameters: {
        type: "object",
        properties: {
          filePath: {
            type: "string",
            description: "Path to the file relative to the workspace root (e.g. 'src/old-module.ts').",
          },
        },
        required: ["filePath"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__run_command",
      description:
        "Execute a shell command within the project workspace. Use for running build tools (npm, yarn, pnpm), tests (jest, pytest, vitest), git operations, linters, compilers, scripts, and any CLI tool. Commands are sandboxed to the workspace directory. Commands must be non-interactive (use flags like --yes, -y, --non-interactive).",
      parameters: {
        type: "object",
        properties: {
          command: {
            type: "string",
            description: "The shell command to execute. Use platform-appropriate syntax (cmd on Windows, bash on Unix). Examples: 'npm test', 'git status', 'python main.py', 'npx tsc --noEmit'.",
          },
          cwd: {
            type: "string",
            description: "Optional subdirectory within the workspace to run the command in. Defaults to workspace root.",
          },
          timeout: {
            type: "string",
            description: "Maximum execution time in milliseconds. Defaults to 120000 (2 minutes). Maximum 600000 (10 minutes).",
          },
        },
        required: ["command"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__glob",
      description:
        "Find files matching a glob pattern within the workspace. Returns matching file paths sorted by modification time. Use for discovering project structure, finding specific file types, or batch file operations. Examples: '**/*.ts', 'src/**/*.test.js', '*.json'.",
      parameters: {
        type: "object",
        properties: {
          pattern: {
            type: "string",
            description: "Glob pattern to match (e.g. '**/*.ts', 'src/**/*.test.js', '*.json'). Supports **, *, ?, and brace expansion.",
          },
          cwd: {
            type: "string",
            description: "Optional subdirectory to search within. Defaults to workspace root.",
          },
          limit: {
            type: "string",
            description: "Maximum number of results to return. Defaults to 500.",
          },
        },
        required: ["pattern"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__fetch_url",
      description:
        "Fetch content from a URL and return it as text. HTML pages are converted to markdown for readability. Use for reading documentation, API references, or any publicly accessible web resource. Does not support authenticated/private URLs.",
      parameters: {
        type: "object",
        properties: {
          url: {
            type: "string",
            description: "The URL to fetch. Must be http or https.",
          },
          maxChars: {
            type: "string",
            description: "Maximum characters to return. Defaults to 50000. Use lower values for large pages when you only need key sections.",
          },
        },
        required: ["url"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__move_file",
      description:
        "Move or rename a file or directory within the workspace. Can rename without moving, or move without renaming. Use for reorganizing project files, renaming modules, or relocating assets.",
      parameters: {
        type: "object",
        properties: {
          source: {
            type: "string",
            description: "Current path of the file or directory, relative to workspace root.",
          },
          destination: {
            type: "string",
            description: "New path for the file or directory, relative to workspace root.",
          },
          overwrite: {
            type: "boolean",
            description: "If true, overwrite existing files at the destination. Defaults to false.",
          },
        },
        required: ["source", "destination"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__create_directory",
      description:
        "Create a directory (and any necessary parent directories) within the workspace. Use when organizing project structure before creating files.",
      parameters: {
        type: "object",
        properties: {
          path: {
            type: "string",
            description: "Directory path to create, relative to workspace root (e.g. 'src/components/ui').",
          },
        },
        required: ["path"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__delete_directory",
      description:
        "Delete an empty directory within the workspace. The directory must be empty — delete files inside first if needed. For non-empty directories, use run_command with appropriate flags.",
      parameters: {
        type: "object",
        properties: {
          path: {
            type: "string",
            description: "Directory path to delete, relative to workspace root.",
          },
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
      description:
        "Save or update the project charter (mission, goals, roles, artifact kinds, staffing policy). CEO-only. Use after aligning with the user on org design.",
      parameters: {
        type: "object",
        properties: {
          charterJson: {
            type: "string",
            description: "Full ProjectCharter JSON string. Merge with read_charter output when updating.",
          },
        },
        required: ["charterJson"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "hiveweave__read_charter",
      description:
        "Read the current project charter (roles, artifact kinds, staffing policy). Available to CEO and HR.",
      parameters: {
        type: "object",
        properties: {},
        required: [],
      },
    },
  },
];

const CEO_COORDINATOR_TOOL_NAMES = new Set([
  "hiveweave__dispatch_task",
  "hiveweave__read_work_logs",
  "hiveweave__review_code",
  "hiveweave__approve_work",
  "hiveweave__reject_work",
  "hiveweave__list_subordinates",
]);

const MESSAGE_SUPERIOR_TOOL_NAMES = new Set(["hiveweave__message_superior"]);
const MESSAGE_PEER_ROSTER_TOOL_NAMES = new Set([
  "hiveweave__message_peer",
  "hiveweave__read_roster",
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

/** CEO may explore the workspace read-only to design org — no write/run */
const CEO_READONLY_FILE_TOOL_NAMES = new Set([
  "hiveweave__read_file",
  "hiveweave__list_files",
  "hiveweave__search_files",
  "hiveweave__glob",
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

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Get the HiveWeave tool definitions for a given permission level and role.
 *
 * @param permissionType - "coordinator" or "executor"
 * @param role - The agent's role (e.g. "hr", "architect", "manager", etc.)
 * @returns Array of OpenAI ChatCompletionTool definitions
 */
export function getHiveWeaveTools(
  permissionType: "coordinator" | "executor",
  role: string = "",
): ChatCompletionTool[] {
  const normalizedRole = role.toLowerCase();
  const allPools = [...COORDINATOR_TOOLS, ...EXECUTOR_TOOLS, ...SHARED_TOOLS, ...HR_TOOLS, ...CHARTER_TOOLS, ...FILE_TOOLS];

  if (normalizedRole === "ceo") {
    return uniqueTools([
      ...pickTools(COORDINATOR_TOOLS, CEO_COORDINATOR_TOOL_NAMES),
      ...pickTools(CHARTER_TOOLS, SAVE_CHARTER_TOOL_NAMES),
      ...pickTools(CHARTER_TOOLS, READ_CHARTER_TOOL_NAMES),
      ...pickTools(SHARED_TOOLS, MESSAGE_PEER_ROSTER_TOOL_NAMES),
      ...pickTools(HR_TOOLS, LIST_ALL_AGENTS_TOOL_NAMES),
      ...pickTools(SHARED_TOOLS, BINDING_AND_MEMORY_TOOL_NAMES),
      ...pickTools(EXECUTOR_TOOLS, READ_PROJECT_MEMORY_TOOL_NAMES),
      ...pickTools(FILE_TOOLS, CEO_READONLY_FILE_TOOL_NAMES),
    ]);
  }

  if (normalizedRole === "hr") {
    return uniqueTools([
      ...pickTools(SHARED_TOOLS, MESSAGE_SUPERIOR_TOOL_NAMES),
      ...pickTools(SHARED_TOOLS, MESSAGE_PEER_ROSTER_TOOL_NAMES),
      ...pickTools(HR_TOOLS, HR_PERSONNEL_TOOL_NAMES),
      ...pickTools(HR_TOOLS, LIST_ALL_AGENTS_TOOL_NAMES),
      ...pickTools(CHARTER_TOOLS, READ_CHARTER_TOOL_NAMES),
      ...pickTools(SHARED_TOOLS, new Set(["hiveweave__write_memory"])),
    ]);
  }

  const base = permissionType === "coordinator" ? COORDINATOR_TOOLS : EXECUTOR_TOOLS;
  return [...base, ...SHARED_TOOLS, ...FILE_TOOLS];
}
