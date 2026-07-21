"""ToolExecutor — permission gating + tool dispatch + output truncation.

契约 02: 工具执行器 — 主分发器
- 接收 tool_name + tool_args，执行对应工具
- 执行前检查权限（PermissionService.evaluate → allow/deny/ask）
- ask → ApprovalService.request_permission（120s 超时）
- 工具输出截断（> 2000 行或 50KB 存临时文件，返回 head+tail 预览）
- 错误处理：工具异常不崩溃，返回 "Error: ..." 字符串
- 临时文件保留 7 天（.hiveweave/tool_outputs/<agent>_<ts>_<tool>.txt）
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.services.approval import (
    ApprovalService, PermissionRejected, PermissionTimeout,
)
from hiveweave.services.charter import CharterService
from hiveweave.services.inbox import InboxService
from hiveweave.services.org import OrgService
from hiveweave.services.permission import PermissionService
from hiveweave.services.roster import RosterService
from hiveweave.services.skill_registry import SkillRegistryService
from hiveweave.services.template import TemplateService
from hiveweave.tools.review import execute_review, ReviewLLMCallback
from hiveweave.tools.task_tools import TaskToolsMixin

log = structlog.get_logger(__name__)

# ── Constants (契约 02) ────────────────────────────────────

TOOL_OUTPUT_MAX_LINES = 2000
TOOL_OUTPUT_MAX_BYTES = 50_000
TOOL_OUTPUT_RETENTION_DAYS = 7
TOOL_OUTPUT_FILE_MAX_BYTES = 10 * 1024 * 1024  # 10MB
PREVIEW_HEAD_LINES = 20
PREVIEW_TAIL_LINES = 5
PREVIEW_TAIL_THRESHOLD = 25  # only include tail if total > 25 lines

APPROVAL_TIMEOUT_S = 120

# Tool name regex for filename sanitization (non-alphanumeric → "_")
_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")


# ── Tool parameter schemas ──────────────────────────────────
# Centralized JSON Schema definitions for every tool. Used for:
# 1. Sending to LLM (so it knows correct parameter names — no more guessing)
# 2. Validating LLM args before execution (auto-generate helpful errors)
# 3. Accepting multiple parameter name aliases (Python arg_name in "aliases")

TOOL_PARAM_SCHEMAS: dict[str, dict] = {
    "bash": {
        "description": "Executes a shell command on the local system. Use it to run CLI tools, scripts, git commands, or any system operation. Returns stdout and stderr of the command.",
        "properties": {
            "command": {"type": "string", "aliases": ["cmd", "run"]},
            "timeout": {"type": "integer", "aliases": ["timeout_ms", "timeoutMs"],
                        "description": "Timeout in milliseconds. Default: 120000 (2 min). Max: 600000 (10 min). Values 1-600 are treated as seconds (e.g. 30 = 30s). Use 120000 for npm install."},
        },
        "required": ["command"],
    },
    "browse": {
        "description": (
            "Drive a real Chromium browser via gstack browse CLI for UI E2E / visual QA. "
            "Use after start_dev_server/lookup_dev_server. Typical flow: "
            "browse(args=[\"goto\",\"http://127.0.0.1:PORT\"]) → "
            "browse(args=[\"snapshot\",\"-i\"]) → browse(args=[\"click\",\"@e3\"]) → "
            "browse(args=[\"screenshot\",\"evidence/bug.png\"]). "
            "Also: console, network, fill, text. Prefer this over raw bash $B."
        ),
        "properties": {
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": 'CLI argv e.g. ["goto","http://127.0.0.1:3000"] or ["snapshot","-i"]',
            },
            "command": {
                "type": "string",
                "aliases": ["cmd"],
                "description": "Alternative to args: space-separated subcommand string.",
            },
            "timeoutSec": {
                "type": "integer",
                "aliases": ["timeout_sec", "timeout"],
                "description": "Timeout in seconds (default 60, max 300).",
            },
        },
        "required": [],
    },
    "run_command": {
        "description": "Executes a command and returns the output. Similar to bash but with explicit working directory support. Use for running scripts, builds, tests, or any system command.",
        "properties": {
            "command": {"type": "string", "aliases": ["cmd", "run"]},
            "cwd": {"type": "string", "description": "Working directory (relative to workspace). Default: workspace root."},
            "timeout": {"type": "integer", "aliases": ["timeout_ms", "timeoutMs"],
                        "description": "Timeout in milliseconds. Default: 120000 (2 min). Max: 600000 (10 min). Values 1-600 are treated as seconds."},
        },
        "required": ["command"],
    },
    "read_file": {
        "description": "Reads the contents of a file from the filesystem. Use it to view source code, config files, logs, or any text file. Returns the file content with line numbers.",
        "properties": {
            "filePath": {"type": "string", "aliases": ["path", "file_path", "file"]},
            "offset": {"type": "integer", "aliases": ["startLine"],
                "description": "Starting line number (0-based, default: 0)."},
            "limit": {"type": "integer", "aliases": ["maxLines", "lineLimit"],
                "description": "Max lines to read (default: 2000)."},
        },
        "required": ["filePath"],
    },
    "write_file": {
        "description": "Creates a new file or overwrites an existing file with the given content. Use it to write source code, configs, or data files. No explicit return value on success.",
        "properties": {
            "filePath": {"type": "string", "aliases": ["path", "file_path", "file"]},
            "content": {"type": "string", "aliases": ["data", "text", "body"]},
        },
        "required": ["filePath", "content"],
    },
    "list_files": {
        "description": "Lists files and directories at the given path. Use it to explore directory structure, find files by location, or verify file existence. Returns a list of file/directory names.",
        "properties": {
            "dirPath": {"type": "string", "aliases": ["path", "directory", "dir"]},
            "recursive": {"type": "boolean", "description": "If true, list recursively. Default: false."},
            "maxdepth": {"type": "integer", "description": "Max depth when recursive (1-3). Default: 1."},
        },
        "required": [],
    },
    "grep": {
        "description": "Searches file CONTENTS using a regex pattern. Use it to find occurrences of text, code, or string matches inside files. Returns matching file paths and lines. For filename/glob-based search, use search_files instead.",
        "properties": {
            "pattern": {"type": "string", "aliases": ["regex", "query", "search"]},
            "path": {"type": "string", "aliases": ["filePath", "file", "directory", "dir"]},
            "include": {"type": "string", "aliases": ["glob", "filter"]},
            "head_limit": {"type": "integer", "aliases": ["headLimit", "maxResults", "limit"],
                "description": "Max results to return (default: 500)."},
            "context": {"type": "integer", "aliases": ["contextLines", "contextAround"],
                "description": "Number of context lines around each match (default: 0)."},
            "multiline": {"type": "boolean", "aliases": ["multiLine", "dotAll"]},
        },
        "required": ["pattern"],
    },
    "search_files": {
        "description": "Searches for files by FILENAME or GLOB pattern. Use it to find files by name, extension, or glob expression. Returns matching file paths. For content search inside files, use grep instead.",
        "properties": {
            "pattern": {"type": "string", "aliases": ["glob", "query", "search", "name"]},
            "directory": {"type": "string", "aliases": ["path", "dir"]},
        },
        "required": ["pattern"],
    },
    "edit_file": {
        "description": "Applies a targeted text replacement in a file. Use it to make surgical edits by locating a unique old_string and replacing it with new_string. Returns success or an error message if the old_string is not found.",
        "properties": {
            "filePath": {"type": "string", "aliases": ["path", "file_path", "file"]},
            "old_string": {"type": "string", "aliases": ["oldString", "old_str", "search", "find"]},
            "new_string": {"type": "string", "aliases": ["newString", "new_str", "replace", "replacement"]},
            "replace_all": {"type": "boolean", "aliases": ["replaceAll"]},
        },
        "required": ["filePath", "old_string", "new_string"],
    },
    "apply_patch": {
        "description": "Apply file patch operations (create/update/delete files). Each patch specifies a file path, operation type, and content.",
        "properties": {
            "patches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "op": {"type": "string", "description": "Operation: 'add' (create), 'update' (replace), or 'delete'"},
                        "filePath": {"type": "string", "description": "Path to the file (relative to workspace)"},
                        "oldString": {"type": "string", "description": "For update: text to find in the file"},
                        "newString": {"type": "string", "description": "For update: replacement text"},
                        "content": {"type": "string", "description": "For add: full file content"},
                    },
                },
                "description": "Array of patch operations",
            },
        },
        "required": [],
    },
    "websearch": {
        "description": "Searches the public internet using a text query. Use it to find current information, research topics, look up documentation, or answer questions. Returns search result snippets with URLs.",
        "properties": {
            "query": {"type": "string", "aliases": ["search", "q", "term"]},
            "numResults": {"type": "integer", "aliases": ["num_results", "limit", "count"],
                "description": "Number of results (1-8, default: 5)."},
        },
        "required": ["query"],
    },
    "question": {
        "description": "Asks the user a question and optionally presents a list of choices. Use it to request clarification, get input on decisions, or present options when human guidance is needed. Returns the user response.",
        "properties": {
            "question": {"type": "string", "aliases": ["message", "content", "query", "text"]},
            "options": {"type": "array", "aliases": ["choices"]},
        },
        "required": ["question"],
    },
    "todowrite": {
        "description": "Manages the agent's task list. Use it to plan tasks and track progress. When a task is completed, update its status to 'completed'. Use write_work_log for detailed work records.",
        "properties": {
            "todos": {"type": "array", "aliases": ["tasks", "items", "list"]},
        },
        "required": ["todos"],
    },
    "send_message": {
        "description": (
            "Sends a message to agents. Prefer ask_agent (needs reply) or "
            "notify_agent (FYI). Assistant text is private — recipients only see "
            "tool-sent messages. Every turn must end with commit_turn."
        ),
        "properties": {
            "recipients": {"type": "array", "aliases": ["recipient", "to", "targets"]},
            "message": {"type": "string", "aliases": ["content", "body", "text"]},
            "expectReport": {
                "type": "boolean",
                "aliases": ["expect_report"],
                "description": (
                    "Prefer ask_agent instead. True when recipient must reply. "
                    "Also auto-set when message text clearly requests a reply."
                ),
            },
            "priority": {"type": "string", "aliases": ["level"]},
        },
        "required": ["recipients", "message"],
    },
    "ask_agent": {
        "description": (
            "Ask agents and REQUIRE a reply. Use for verification, opinions, "
            "reports. Prefer over send_message(expectReport=true)."
        ),
        "properties": {
            "recipients": {"type": "array", "aliases": ["recipient", "to", "targets", "target"]},
            "message": {"type": "string", "aliases": ["content", "body", "text"]},
            "priority": {"type": "string", "aliases": ["level"]},
        },
        "required": ["recipients", "message"],
    },
    "notify_agent": {
        "description": (
            "FYI notify — does NOT require a reply. Prefer for status broadcasts."
        ),
        "properties": {
            "recipients": {"type": "array", "aliases": ["recipient", "to", "targets", "target"]},
            "message": {"type": "string", "aliases": ["content", "body", "text"]},
            "priority": {"type": "string", "aliases": ["level"]},
        },
        "required": ["recipients", "message"],
    },
    "commit_turn": {
        "description": (
            "MANDATORY end-of-turn return value (TurnResult). Every turn is a "
            "function call — you MUST commit_turn before stopping. "
            "phase: in_progress|waiting|blocked|done_slice. "
            "waiting/blocked require waiting_on. Assistant text is NOT a return value."
        ),
        "properties": {
            "phase": {
                "type": "string",
                "enum": ["in_progress", "waiting", "blocked", "done_slice"],
            },
            "summary": {
                "type": "string",
                "aliases": ["content", "message", "text"],
                "description": "1-2 sentences: what this turn did",
            },
            "waitingOn": {
                "type": "array",
                "aliases": ["waiting_on"],
                "description": (
                    "Required for waiting/blocked. "
                    "Items: {kind: agent|task|user|timer|external, ref: string, note?: string}"
                ),
            },
            "result": {
                "type": "object",
                "description": "Data plane (replies/tasks/artifacts). May be {}",
            },
            "extensions": {
                "type": "object",
                "description": "Forward-compatible extensions. May be {}",
            },
        },
        "required": ["phase", "summary"],
    },
    "defer_task_advance": {
        "description": (
            "不推进：本轮无法推动可行动任务时调用。声明后平台停止 [TASK ADVANCE] "
            "循环提醒，直到再次被唤醒。需要非空 reason。"
        ),
        "properties": {
            "reason": {
                "type": "string",
                "aliases": ["why", "note", "summary"],
                "description": "为何此刻无法推进（具体 blocker）",
            },
        },
        "required": ["reason"],
    },
    "hire_agent": {
        "description": "Creates and deploys a new agent with a specified name, role, goal, and backstory. Use it to bring new team members into the organization. Returns the new agent ID.",
        "properties": {
            "name": {"type": "string"},
            "role": {"type": "string", "description": "Chinese job title (display label — does NOT set permission; use permissionType). For executors MUST include owned module, e.g. 签到排行榜工程师 / 认证API工程师 — NOT bare 前端工程师."},
            "permissionType": {
                "type": "string",
                "enum": ["coordinator", "executor"],
                "description": "MANDATORY. coordinator = manages subordinates (dispatch_task/review_task); executor = hands-on work (claim_task/submit_task). CEO's hiring request specifies this — pass it through verbatim.",
            },
            "goal": {"type": "string"},
            "backstory": {"type": "string"},
            "skills": {"type": "array", "items": {"type": "string"}, "description": "Skills to bind. Tool skills: use \"#N\" to reference skills from list_available_skills by number (e.g. \"#1\"). Discipline skills: use full slug from matching table (e.g. \"self-review\", \"incremental-implementation\"). NOT raw tech names like 'React 18'."},
            "parentId": {"type": "string", "aliases": ["parent_id", "parent"]},
        },
        "required": ["name", "role"],
    },
    "read_charter": {
        "description": "Reads the organization charter document. Use it to review the mission, purpose, rules, and operating principles that govern the agent organization. Returns the charter text.",
        "properties": {},
        "required": [],
    },
    "save_charter": {
        "description": "Creates or updates the organization charter document. Use it to define or amend the mission, purpose, rules, and operating principles. Takes the charter content as input.",
        "properties": {
            "content": {"type": "string", "aliases": ["charter", "body", "text"]},
            "title": {"type": "string", "aliases": ["name"]},
        },
        "required": ["content"],
    },
    "read_goals": {
        "description": "Reads the current organizational goals and objectives. Use it to review what the organization is working toward. Returns the goals document.",
        "properties": {},
        "required": [],
    },
    "update_goals": {
        "description": "Updates the organizational goals, objectives, focus areas, and key results. Use it to set or revise what the organization and its agents are working toward.",
        "properties": {
            "objective": {"type": "string"},
            "focus": {"type": "string"},
            "keyResults": {"type": "array", "aliases": ["key_results"]},
            "userInvolvement": {"type": "string", "aliases": ["user_involvement"]},
        },
        "required": [],
    },
    "read_memory": {
        "description": "Reads previously stored memory or state for a specific module ID. Use it to retrieve saved information or context that was stored via write_memory. Returns the stored content.",
        "properties": {
            "moduleId": {"type": "string", "aliases": ["module_id", "id", "key"]},
        },
        "required": ["moduleId"],
    },
    "write_memory": {
        "description": "Writes content to the agent memory system under a given module ID, with optional tags. Use it to store information, context, or state for later retrieval by read_memory.",
        "properties": {
            "content": {"type": "string", "aliases": ["data", "body", "text", "memory"]},
            "moduleId": {"type": "string", "aliases": ["module_id", "id", "key"]},
            "tags": {"type": "array", "items": {"type": "string"}, "aliases": []},
        },
        "required": ["content"],
    },
    "list_available_skills": {
        "description": "Lists all skills available in the marketplace (built-in + external + skills.sh). Pass 'search' to filter by keyword. Returns numbered skills (e.g. #1, #2). Use \"#N\" in hire_agent's skills parameter to reference by number, or use full slug.",
        "properties": {
            "search": {"type": "string", "description": "Optional keyword to filter skills (e.g. 'react', 'testing', 'planning'). Case-insensitive."},
        },
        "required": [],
    },
    "read_skill": {
        "description": "Reads the documentation and definition of a specific skill by name or slug. Use it to understand what a skill does, how to use it, and how to invoke it.",
        "properties": {
            "skill": {"type": "string", "aliases": ["name", "slug", "id"]},
        },
        "required": ["skill"],
    },
    "read_roster": {
        "description": "Read the team roster listing all agents and their roles/departments.",
        "properties": {},
        "required": [],
    },
    "update_roster": {
        "description": "Update an agent's position, department, responsibilities, or status in the roster.",
        "properties": {
            "agentId": {"type": "string", "aliases": ["agent_id", "target", "id"]},
            "position": {"type": "string"},
            "department": {"type": "string"},
            "responsibilities": {"type": "string"},
            "status": {"type": "string"},
            "hire_date": {"type": "string", "aliases": ["hireDate"]},
        },
        "required": ["agentId"],
    },
    "view_org_chart": {
        "description": "View the full organizational hierarchy tree showing reporting lines.",
        "properties": {},
        "required": [],
    },
    "list_subordinates": {
        "description": "List your direct reports (subordinates).",
        "properties": {},
        "required": [],
    },
    "list_alarms": {
        "description": "List all pending scheduled alarms.",
        "properties": {},
        "required": [],
    },
    "cancel_alarm": {
        "description": "Cancel a scheduled alarm by its ID.",
        "properties": {
            "alarmId": {"type": "string", "aliases": ["alarm_id", "id"]},
        },
        "required": ["alarmId"],
    },
    "schedule_alarm": {
        "description": "Schedule an alarm to fire after a game-time delay, optionally repeating.",
        "properties": {
            "toAgentId": {"type": "string", "aliases": ["to_agent_id", "target"]},
            "purpose": {"type": "string", "aliases": ["message", "description"]},
            "fireInGameSeconds": {"type": "integer", "aliases": ["fire_in_game_seconds", "delay"],
                "description": "Delay in game-time seconds before the alarm fires."},
            "repeatIntervalSeconds": {"type": "integer", "aliases": ["repeat_interval_seconds", "interval"],
                "description": "If set, alarm repeats every N game-time seconds. Omit for one-shot."},
        },
        "required": ["toAgentId", "purpose", "fireInGameSeconds"],
    },
    "read_work_logs": {
        "description": "Read work logs. Can read any agent's logs (including your own, subordinates, or peers). Use agentId to specify whose logs to read; omit it to read all subordinates' logs. Each log entry shows what the agent did (type) and a summary.",
        "properties": {
            "agentId": {"type": "string", "aliases": ["agent_id", "target"]},
            "limit": {"type": "integer", "aliases": ["count", "max"]},
        },
        "required": [],
    },
    "run_code_review": {
        "description": "Analyze files for code quality, correctness, and style issues. Returns findings.",
        "properties": {
            "filePaths": {"type": "array", "items": {"type": "string"},
                "aliases": ["files", "target", "path", "file", "module"]},
            "testFiles": {"type": "array", "items": {"type": "string"},
                "aliases": ["test_files"]},
        },
        "required": ["filePaths"],
    },
    "run_security_audit": {
        "description": "Analyze files for security vulnerabilities. Returns findings.",
        "properties": {
            "filePaths": {"type": "array", "items": {"type": "string"},
                "aliases": ["files", "target", "path", "file", "module"]},
            "testFiles": {"type": "array", "items": {"type": "string"},
                "aliases": ["test_files"]},
        },
        "required": ["filePaths"],
    },
    "run_tests": {
        "description": "Run tests for specified files and return results.",
        "properties": {
            "filePaths": {"type": "array", "items": {"type": "string"},
                "aliases": ["files", "target", "path", "file", "module", "testPath"]},
            "testFiles": {"type": "array", "items": {"type": "string"},
                "aliases": ["test_files"]},
        },
        "required": ["filePaths"],
    },
    "run_perf_audit": {
        "description": "Analyze files for performance bottlenecks. Returns optimization suggestions.",
        "properties": {
            "filePaths": {"type": "array", "items": {"type": "string"},
                "aliases": ["files", "target", "path", "file", "module"]},
            "testFiles": {"type": "array", "items": {"type": "string"},
                "aliases": ["test_files"]},
        },
        "required": ["filePaths"],
    },
    "run_full_review": {
        "description": "Run all review types (code, security, tests, performance) combined.",
        "properties": {
            "filePaths": {"type": "array", "items": {"type": "string"},
                "aliases": ["files", "target", "path", "file", "module"]},
            "testFiles": {"type": "array", "items": {"type": "string"},
                "aliases": ["test_files"]},
        },
        "required": ["filePaths"],
    },
    "delete_file": {
        "description": "Permanently delete a file at the specified path.",
        "properties": {
            "path": {"type": "string", "aliases": ["filePath", "file_path", "file"]},
        },
        "required": ["path"],
    },
    "create_directory": {
        "description": "Create a new directory at the specified path.",
        "properties": {
            "path": {"type": "string", "aliases": ["dirPath", "directory", "dir"]},
        },
        "required": ["path"],
    },
    "delete_directory": {
        "description": "Permanently delete a directory and all its contents.",
        "properties": {
            "path": {"type": "string", "aliases": ["dirPath", "directory", "dir"]},
        },
        "required": ["path"],
    },
    # — Agent management —
    "dismiss_agent": {
        "description": "Permanently remove/fire an agent from the organization. Cannot be undone.",
        "properties": {
            "agentId": {"type": "string", "aliases": ["agent_id", "id", "target"]},
        },
        "required": ["agentId"],
    },
    "transfer_agent": {
        "description": "Reassign an agent to a new parent/supervisor in the hierarchy.",
        "properties": {
            "agentId": {"type": "string", "aliases": ["agent_id", "id"]},
            "newParentId": {"type": "string", "aliases": ["new_parent_id", "parentId", "parent_id", "target"]},
        },
        "required": ["agentId", "newParentId"],
    },
    "list_agent_templates": {
        "description": "List available agent templates for hiring.",
        "properties": {},
        "required": [],
    },
    "bind_skill": {
        "description": "Attach a skill to an agent, granting them that capability.",
        "properties": {
            "agentId": {"type": "string", "aliases": ["agent_id", "id"]},
            "skill": {"type": "string", "aliases": ["slug", "name", "skillSlug"]},
        },
        "required": ["agentId", "skill"],
    },
    "unbind_skill": {
        "description": "Remove a skill from an agent.",
        "properties": {
            "agentId": {"type": "string", "aliases": ["agent_id", "id"]},
            "skill": {"type": "string", "aliases": ["slug", "name", "skillSlug"]},
        },
        "required": ["agentId", "skill"],
    },
    # — Messaging —
    "message_subordinate": {
        "description": "Send a message to ALL your direct subordinates at once. Use dispatch_task to delegate specific work.",
        "properties": {
            "recipient": {"type": "string", "aliases": ["to", "target", "agentId", "agent_id"]},
            "message": {"type": "string", "aliases": ["content", "body", "text"]},
            "expectReport": {"type": "boolean", "aliases": ["expect_report"]},
        },
        "required": ["recipient", "message"],
    },
    "message_superior": {
        "description": "Send a message to your parent/superior. Use submit_task when finishing a delegated task. DEPRECATED: message_superior is only for questions, not task completion.",
        "properties": {
            "message": {"type": "string", "aliases": ["content", "body", "text"]},
            "expectReport": {"type": "boolean", "aliases": ["expect_report"]},
        },
        "required": ["message"],
    },
    "message_peer": {
        "description": "Send a direct message to a single peer agent at the same level.",
        "properties": {
            "recipient": {"type": "string", "aliases": ["to", "target", "agentId", "agent_id"]},
            "message": {"type": "string", "aliases": ["content", "body", "text"]},
        },
        "required": ["recipient", "message"],
    },
    "message_team": {
        "description": "Broadcast a message to every agent in a specified team.",
        "properties": {
            "teamId": {"type": "string", "aliases": ["team_id", "team"]},
            "message": {"type": "string", "aliases": ["content", "body", "text"]},
        },
        "required": ["teamId", "message"],
    },
    # — Dispatch + review —
    "dispatch_task": {
        "description": "Deliver work to a subordinate NOW: creates/reuses a Task Ledger entry AND sends inbox (wakes them). Default for immediate work. If you already called create_task (draft or queue), pass taskId to avoid a duplicate. create_task alone does NOT wake anyone.",
        "properties": {
            "target": {"type": "string", "aliases": ["toAgentId", "to_agent_id", "recipient", "agentId"]},
            "task": {"type": "string", "aliases": ["description", "message", "content", "summary"]},
            "expectReport": {"type": "boolean", "aliases": ["expect_report"]},
            "taskId": {"type": "string", "aliases": ["task_id", "existing_task_id"],
                "description": "Optional: reuse an existing task instead of creating a new one"},
        },
        "required": ["target", "task"],
    },
    "review": {
        "description": "Review code, design, or deliverables for quality. Specify reviewType (code_review/security_audit/test_review/perf_audit).",
        "properties": {
            "filePaths": {"type": "array", "items": {"type": "string"},
                "aliases": ["files", "target", "path", "file", "module"]},
            "reviewType": {"type": "string",
                "aliases": ["review_type", "type"],
                "description": "Review type: 'code_review', 'security_audit', 'test_review', or 'perf_audit'. Default: code_review."},
        },
        "required": ["filePaths"],
    },
    "request_review": {
        "description": "Ask a specific reviewer to review code, design, or deliverables.",
        "properties": {
            "reviewerId": {"type": "string", "aliases": ["reviewer_id", "reviewer", "target", "agentId"]},
            "target": {"type": "string", "aliases": ["file", "path", "module", "description"]},
            "reviewType": {"type": "string", "aliases": ["review_type", "type"]},
        },
        "required": ["reviewerId", "target"],
    },
    "report_completion": {
        "description": "REMOVED. Use submit_task(taskId, summary, testsPassed=true) instead.",
        "properties": {
            "summary": {"type": "string", "aliases": ["message", "content", "report", "description"]},
        },
        "required": ["summary"],
    },
    "approve_work": {
        "description": "REMOVED. Use review_task(taskId, decision='approve') instead.",
        "properties": {
            "subordinate": {"type": "string", "aliases": ["subordinateId", "subordinate_id", "agentId", "agent_id", "target"]},
        },
        "required": ["subordinate"],
    },
    "reject_work": {
        "description": "REMOVED. Use review_task(taskId, decision='rework', feedback=...) instead.",
        "properties": {
            "subordinate": {"type": "string", "aliases": ["subordinateId", "subordinate_id", "agentId", "agent_id", "target"]},
            "reason": {"type": "string", "aliases": ["feedback", "review", "comment", "message"]},
        },
        "required": ["subordinate", "reason"],
    },
    "write_work_log": {
        "description": "Record what you just did in your work log. Use todowrite for planning future tasks.",
        "properties": {
            "summary": {"type": "string", "aliases": ["message", "content", "description"]},
            "details": {"type": "string", "aliases": ["data", "extra"]},
            "type": {"type": "string", "aliases": ["logType", "log_type"]},
        },
        "required": ["summary"],
    },
    # — Git worktrees —
    # NOTE: git_worktree_create is intentionally excluded from executor tools.
    # Executors already work inside a worktree; allowing create causes nested
    # worktrees (D:\...\A005\.hiveweave\worktrees\A005\...). Only coordinator
    # can create worktrees (via hire_agent which auto-creates them).
    "git_worktree_list": {
        "description": "List all active git worktrees with their branch names and paths.",
        "properties": {},
        "required": [],
    },
    "git_worktree_merge": {
        "description": "Merge a worktree branch into main and remove the worktree. On conflict: abort + rework executor to rebase/merge main in their worktree. On success: spawn VERIFY only for tasks covered by this merge.",
        "properties": {
            "branchName": {"type": "string", "aliases": ["branch_name", "branch", "name"]},
        },
        "required": ["branchName"],
    },
    "git_worktree_remove": {
        "description": "Remove a worktree and its branch without merging. Discards changes.",
        "properties": {
            "branchName": {"type": "string", "aliases": ["branch_name", "branch", "name"]},
        },
        "required": ["branchName"],
    },
    "git_worktree_status": {
        "description": (
            "Show branch, dirty flag, and HEAD for an agent worktree. "
            "Pass shortId to inspect a subordinate's worktree."
        ),
        "properties": {
            "shortId": {
                "type": "string",
                "aliases": ["short_id", "agentShortId", "target"],
            },
        },
        "required": [],
    },
    "git_worktree_checkpoint": {
        "description": "Stage all changes and create a checkpoint commit in the active worktree.",
        "properties": {
            "message": {"type": "string", "aliases": ["commitMessage", "commit_message", "summary"]},
        },
        "required": ["message"],
    },
    # — Network + file ops —
    "webfetch": {
        "description": "Fetch a URL, extract readable text, and optionally answer a question about the page. Has SSRF protection.",
        "properties": {
            "url": {"type": "string", "aliases": ["link", "href", "address"]},
            "prompt": {"type": "string", "aliases": ["query", "question", "instruction"]},
        },
        "required": ["url"],
    },
    "move_file": {
        "description": "Move or rename a file or directory to a new location.",
        "properties": {
            "source": {"type": "string", "aliases": ["from", "src", "sourcePath", "source_path"]},
            "destination": {"type": "string", "aliases": ["to", "dst", "destPath", "dest_path", "target"]},
        },
        "required": ["source", "destination"],
    },
    # — Task Ledger tools (Task 4) —
    "create_task": {
        "description": "Write a task into the Task Ledger only (status=created). Does NOT send inbox or wake anyone — even with assigneeId. Use for (1) drafting with acceptance criteria before dispatch_task(taskId=...), or (2) queue-only parking until ready. To actually assign and wake a subordinate, you MUST call dispatch_task.",
        "properties": {
            "title": {"type": "string", "aliases": ["name", "summary"]},
            "description": {"type": "string", "aliases": ["detail", "body"]},
            "priority": {"type": "integer", "aliases": ["level"],
                "description": "Priority level (0-5, default: 2). Higher = more urgent."},
            "dueAt": {"type": "integer", "aliases": ["due_at", "deadline"],
                "description": "Due time in game-time seconds (epoch). Use 0 or omit for no deadline."},
            "assigneeId": {"type": "string", "aliases": ["assignee_id", "assignee"]},
            "acceptanceCriteria": {"type": "array", "items": {"type": "string"},
                "aliases": ["acceptance_criteria"]},
            "parentTaskId": {"type": "string", "aliases": ["parent_task_id", "parent"]},
            "dependsOn": {"type": "array", "items": {"type": "string"},
                "aliases": ["depends_on"]},
            "expectedModules": {"type": "array", "items": {"type": "string"},
                "aliases": ["expected_modules"]},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["title", "description"],
    },
    "claim_task": {
        "description": "Claim a task in 'created' status (created → claimed). Sets you as the assignee. Only unassigned or created tasks can be claimed.",
        "properties": {
            "taskId": {"type": "string", "aliases": ["task_id", "id"]},
        },
        "required": ["taskId"],
    },
    "update_task_status": {
        "description": "Update task status to 'running' (start or unblock work) or 'blocked' (with a reason). For 'running', tries start first then unblock. For 'blocked', requires blockedReason. If status omitted, defaults to 'running'.",
        "properties": {
            "taskId": {"type": "string", "aliases": ["task_id", "id"]},
            "status": {"type": "string",
                "description": "Target status: 'running' or 'blocked'. Defaults to 'running'.",
                "enum": ["running", "blocked"]},
            "blockedReason": {"type": "string",
                "aliases": ["blocked_reason", "reason"],
                "description": "Required when blocked. Prefer prefixes: dependency:<id|why>, timer:<why>, user:<why>, external:<why>. Do not stay running while waiting."},
        },
        "required": ["taskId"],
    },
    "update_progress": {
        "description": "Update task progress as a percentage (0-100). Does not change task status. Use while a task is running.",
        "properties": {
            "taskId": {"type": "string", "aliases": ["task_id", "id"]},
            "progress": {"type": "integer", "aliases": ["percent"],
                "description": "Progress percentage (0-100)."},
        },
        "required": ["taskId", "progress"],
    },
    "submit_task": {
        "description": "Submit a task for review. REQUIRES testsPassed=true after running real tests. Auto claim+start if needed. Optional testOutput strengthens evidence.",
        "properties": {
            "taskId": {"type": "string", "aliases": ["task_id", "id"]},
            "summary": {"type": "string", "aliases": ["report", "description"]},
            "commit": {"type": "string",
                "aliases": ["commitSha", "commit_sha"]},
            "filesChanged": {"type": "array", "items": {"type": "string"},
                "aliases": ["files_changed", "files"]},
            "testsPassed": {"type": "boolean", "aliases": ["tests_passed"],
                "description": "Must be true. Run tests first."},
            "testOutput": {"type": "string", "aliases": ["test_output", "testLog"],
                "description": "Brief test command output / proof."},
            "attestationIds": {
                "type": "array",
                "items": {"type": "string"},
                "aliases": ["attestation_ids"],
                "description": (
                    "Server-issued attestation ids from browse/bash. "
                    "Required for UI/code tasks."
                ),
            },
        },
        "required": ["summary", "testsPassed"],
    },
    "review_task": {
        "description": "Review a submitted task (reviewing → approved/rework). approve requires attestation evidence + assignee worktree context; does NOT spawn VERIFY — next call git_worktree_merge; VERIFY is created only after merge succeeds.",
        "properties": {
            "taskId": {"type": "string", "aliases": ["task_id", "id"]},
            "decision": {"type": "string",
                "description": "'approve' or 'rework'",
                "aliases": ["verdict"]},
            "feedback": {"type": "string",
                "aliases": ["comment", "reason"]},
        },
        "required": ["taskId", "decision"],
    },
    "get_tasks": {
        "description": "List tasks in the Task Ledger. Optional filters by status (e.g. 'created', 'claimed', 'running', 'blocked', 'submitted', 'reviewing', 'approved', 'rework', 'closed') or assignee. Excludes archived tasks.",
        "properties": {
            "status": {"type": "string"},
            "assigneeId": {"type": "string",
                "aliases": ["assignee_id", "assignee"]},
        },
        "required": [],
    },
}

def _resolve_alias_for_tool(arg_name: str, props: dict) -> str | None:
    """Check if arg_name is an alias for any known parameter in this tool.

    Returns the canonical parameter name, or None if unknown.
    """
    # Is it already a canonical name?
    if arg_name in props:
        return arg_name
    # Check aliases
    for prop_name, prop_schema in props.items():
        if arg_name in prop_schema.get("aliases", []):
            return prop_name
    return None


def validate_tool_args(tool_name: str, args: dict) -> tuple[dict, str | None]:
    """Validate and normalize tool arguments against the schema.

    Returns (normalized_args, error_message).
    - normalized_args: args with aliases resolved to canonical names
    - error_message: None if valid, else a helpful message listing
      the tool's expected parameters and what was received
    """
    schema = TOOL_PARAM_SCHEMAS.get(tool_name)
    if schema is None:
        # Fall back to @tool Pydantic schema so legacy path also alias-resolves
        try:
            import hiveweave.tools  # noqa: F401
            from hiveweave.tools.base import get_tool_def

            td = get_tool_def(tool_name)
            if td is not None:
                schema = td.to_llm_schema()
        except Exception:
            schema = None
    if schema is None:
        # Unknown tool — pass through as-is
        return args, None

    props: dict = schema.get("properties", {})
    normalized: dict = {}
    missing: list[str] = []
    unknown: list[str] = []

    # Check required params & resolve aliases (per-tool, no cross-tool leakage)
    for req in schema.get("required", []):
        found = False
        for key, value in args.items():
            if value is None:
                continue
            canonical = _resolve_alias_for_tool(key, props)
            if canonical == req:
                normalized[req] = value
                found = True
                break
        if not found:
            missing.append(req)

    # Resolve remaining args through per-tool aliases
    for key, value in args.items():
        if key in normalized:  # already resolved as a required param
            continue
        canonical = _resolve_alias_for_tool(key, props)
        if canonical is not None:
            if canonical not in normalized:
                normalized[canonical] = value
        else:
            unknown.append(key)

    # Coerce types: wrap single string → array when schema expects array
    for key, value in list(normalized.items()):
        prop = props.get(key, {})
        if prop.get("type") == "array" and isinstance(value, str):
            normalized[key] = [value]
        elif prop.get("type") == "boolean" and isinstance(value, str):
            normalized[key] = value.lower() in ("true", "1", "yes")
        elif prop.get("type") == "integer" and isinstance(value, str):
            try:
                normalized[key] = int(value)
            except ValueError:
                pass

    if missing:
        expected = ", ".join(f"'{r}'" for r in missing)
        received = ", ".join(f"'{k}'" for k in args.keys()) if args else "(none)"
        return normalized, (
            f"Missing required parameters: {expected}. "
            f"You passed: {received}. "
            f"Please retry with the correct parameter names."
        )

    if unknown:
        known = ", ".join(f"'{p}'" for p in props.keys())
        unknown_str = ", ".join(f"'{u}'" for u in unknown)
        return normalized, (
            f"Unknown parameters: {unknown_str}. "
            f"Expected: {known}. "
            f"Please retry with correct parameter names."
        )

    return normalized, None


def get_tool_schema_for_llm(tool_name: str) -> dict:
    """Get a clean JSON Schema for sending to the LLM (no aliases, no internals).

    Prefer the hand-tuned ``TOOL_PARAM_SCHEMAS`` entry when present. Otherwise
    fall back to the ``@tool`` registry Pydantic model — never return a bare
    ``additionalProperties: true`` object for a registered tool (that caused
    waive_attestation/cancel_task to arrive with ``parameters: []``).
    """
    schema = TOOL_PARAM_SCHEMAS.get(tool_name)
    if schema is None:
        # Lazy import: avoid circular import at module load
        try:
            import hiveweave.tools  # noqa: F401 — populate registry
            from hiveweave.tools.base import get_tool_def
        except Exception:
            return {"type": "object", "additionalProperties": True}
        td = get_tool_def(tool_name)
        if td is None:
            return {"type": "object", "additionalProperties": True}
        llm = td.to_llm_schema()
        clean_fb: dict = {"type": "object", "properties": {}}
        for name, prop in (llm.get("properties") or {}).items():
            clean_fb["properties"][name] = {
                k: v for k, v in prop.items() if k != "aliases"
            }
        if llm.get("required"):
            clean_fb["required"] = list(llm["required"])
        return clean_fb

    # Deep copy and strip aliases
    clean: dict = {"type": "object"}
    if "description" in schema:
        clean["description"] = schema["description"]
    if "properties" in schema:
        clean["properties"] = {}
        for name, prop in schema["properties"].items():
            clean_prop = {k: v for k, v in prop.items() if k != "aliases"}
            clean["properties"][name] = clean_prop
    if "required" in schema and schema["required"]:
        clean["required"] = schema["required"]
    return clean


def get_tool_description(tool_name: str) -> str:
    """Human/LLM tool description: manual schema first, else @tool registry."""
    desc = TOOL_PARAM_SCHEMAS.get(tool_name, {}).get("description")
    if desc:
        return desc
    try:
        import hiveweave.tools  # noqa: F401
        from hiveweave.tools.base import get_tool_def

        td = get_tool_def(tool_name)
        if td and td.description:
            return td.description
    except Exception:
        pass
    return f"Execute the {tool_name} tool."


# ── Result type ────────────────────────────────────────────

class ToolResult(dict):
    """Dict with success/output/error keys (returned by all tools)."""


# ── ToolExecutor ───────────────────────────────────────────

class ToolExecutor(TaskToolsMixin):
    """Routes tool calls to implementations with permission gating +
    sandbox checks + output truncation.

    Usage:
        executor = ToolExecutor(permission_service, approval_service)
        result = await executor.execute(agent_id, "bash",
                                        {"command": "ls"}, workspace_path)
        # result: {"success": bool, "output": str, "error": str | None}
    """

    def __init__(
        self,
        permission_service: PermissionService,
        approval_service: ApprovalService,
        review_llm_callback: ReviewLLMCallback | None = None,
    ) -> None:
        self.permission = permission_service
        self.approval = approval_service
        self.review_llm_callback = review_llm_callback
        # Service instances for high-level orchestration tools
        self._org = OrgService()
        self._inbox = InboxService()
        self._charter = CharterService()
        self._roster = RosterService()
        self._skills = SkillRegistryService()
        self._templates = TemplateService()

    # ── Public API ────────────────────────────────────────

    async def execute(
        self,
        agent_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
        workspace_path: str,
        project_root: str | None = None,
    ) -> dict[str, Any]:
        """Execute a tool call. Returns {success, output, error}.

        workspace_path: agent write sandbox (worktree for executors).
        project_root: project directory for read sandbox (defaults to inferred).
        """
        # 1. Strip hiveweave__ prefix
        name = tool_name
        if name.startswith("hiveweave__"):
            name = name[len("hiveweave__"):]

        log.info("tool.execute", agent_id=agent_id, tool=name,
                 args_preview=str(tool_args)[:200])

        # ── New pipeline path (Phase 2 migration) ──────────
        # Try the registered tool pipeline first. If the tool is registered
        # (via @tool decorator), it goes through Pydantic validation +
        # unified security checks + permission evaluation.
        # If the tool is NOT registered, fall through to the legacy path.
        from hiveweave.tools.file import infer_project_root
        from hiveweave.tools.pipeline import execute_registered_tool, ToolContext

        resolved_root = project_root or infer_project_root(workspace_path)

        # Build context for orchestration tools that need service access
        ctx = ToolContext(
            org=self._org,
            inbox=self._inbox,
            charter=self._charter,
            roster=self._roster,
            skills=self._skills,
            templates=self._templates,
            permission=self.permission,
            approval=self.approval,
            review_llm_callback=self.review_llm_callback,
            extra={"project_root": resolved_root},
        )

        registered_result = await execute_registered_tool(
            tool_name=name,
            raw_args=tool_args,
            agent_id=agent_id,
            workspace_path=workspace_path,
            permission=self.permission,
            approval=self.approval,
            ctx=ctx,
        )
        if registered_result is not None:
            # Tool was handled by the new pipeline — apply truncation and return
            if registered_result.get("output"):
                registered_result["output"] = self._maybe_save_large_output(
                    registered_result["output"], agent_id, name, workspace_path
                )
            return registered_result

        # ── Legacy path (unregistered tools) ───────────────
        # 1.5. Validate & normalize args against schema — auto-correct
        # parameter name mistakes (e.g. LLM passes "query" → canonical "pattern")
        normalized_args, validation_error = validate_tool_args(name, tool_args)
        if validation_error:
            log.info("tool.args_invalid", agent_id=agent_id, tool=name,
                     error=validation_error[:200])
            return self._error(f"Parameter error in '{name}': {validation_error}")
        tool_args = normalized_args

        # 2. Permission evaluation
        try:
            decision = await self.permission.evaluate(
                agent_id, name, tool_args
            )
        except Exception as exc:  # noqa: BLE001
            log.error("permission.evaluate_failed", error=str(exc))
            return self._error(f"Error: Permission check failed: {exc}")

        if decision == "deny":
            # 如实提示：返回 policy 硬门真实原因 + coordinator/HR 写白名单指引
            from hiveweave.services.policy import (
                infer_role_family,
                policy_service,
            )
            from hiveweave.tools.pipeline import build_deny_hint

            agent_info = await meta_db.get_agent_by_id(agent_id)
            family = infer_role_family(agent_info or {})
            hard_reason = (
                policy_service.hard_check(agent_info, name, tool_args)
                if agent_info
                else None
            )
            return self._error(build_deny_hint(name, family, hard_reason))

        if decision == "ask":
            # Request approval (120s timeout)
            try:
                await self.approval.request_permission(
                    agent_id=agent_id,
                    tool_name=name,
                    tool_args=tool_args,
                    description=f"Agent {agent_id} wants to use {name}",
                )
            except PermissionTimeout:
                return self._error(
                    "Permission request timed out (120s). "
                    "The user may be away."
                )
            except PermissionRejected as exc:
                return self._error(f"Permission rejected: {exc}")
            except Exception as exc:  # noqa: BLE001
                return self._error(
                    f"Error: Approval request failed: {exc}"
                )

        # 3. Dispatch to the tool implementation
        try:
            result = await self._dispatch(
                name, tool_args, agent_id, workspace_path
            )
        except Exception as exc:  # noqa: BLE001
            log.error("tool.dispatch_failed", tool=name, error=str(exc))
            return self._error(f"Error: {type(exc).__name__}: {exc}")

        # 4. Normalize result shape — R7: 统一工具返回契约
        # 所有工具必须返回 {success, output, error} 三字段。此处作为单一保障点，
        # 为任何遗漏字段的工具补默认值（success=True / output="" / error=None），
        # 确保下游消费方（agent / conversation store）总能拿到一致结构。
        if not isinstance(result, dict):
            result = {"success": True, "output": str(result), "error": None}
        result.setdefault("success", True)
        result.setdefault("output", "")
        result.setdefault("error", None)

        # 5. Apply large-output truncation (layer 1)
        if result.get("output"):
            truncated = self._maybe_save_large_output(
                result["output"], agent_id, name, workspace_path
            )
            result["output"] = truncated

        return result

    # ── Dispatch ─────────────────────────────────────────

    async def _dispatch(
        self,
        name: str,
        args: dict[str, Any],
        agent_id: str,
        workspace_path: str,
    ) -> dict[str, Any]:
        """Route to the specific tool implementation by name."""
        if name in (
            "review", "run_code_review", "run_security_audit", "run_tests",
            "run_perf_audit", "run_full_review",
        ):
            review_type_map = {
                "review": "full_review",
                "run_code_review": "code_review",
                "run_security_audit": "security_audit",
                "run_tests": "test_review",
                "run_perf_audit": "perf_audit",
                "run_full_review": "full_review",
            }
            review_type = review_type_map[name]
            file_paths = args.get("filePaths") or []
            test_files = args.get("testFiles") or []
            return await execute_review(
                review_type=review_type,
                file_paths=file_paths,
                test_files=test_files,
                workspace_path=workspace_path,
                call_llm=self.review_llm_callback,
            )

        # Unknown tool — contract 02 error handling
        return self._error(f"Unknown tool: {name}")

    # SSRF 防护：禁止访问内网地址
    _SSRF_BLOCKED_HOSTS = frozenset({
        "localhost", "127.0.0.1", "0.0.0.0", "::1",
        "169.254.169.254",  # 云元数据
        "metadata.google.internal",
    })

    @staticmethod
    def _is_ssrf_blocked(host: str) -> bool:
        """Check if a host is an internal/blocked address."""
        host_lower = host.lower().rstrip(".")
        if host_lower in ToolExecutor._SSRF_BLOCKED_HOSTS:
            return True
        # Block private IP ranges
        try:
            import ipaddress
            ip = ipaddress.ip_address(host_lower)
            return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        except ValueError:
            pass  # Not an IP, it's a hostname
        # Block common internal hostnames
        if host_lower.endswith(".internal") or host_lower.endswith(".local"):
            return True
        return False

    # ── Output truncation (layer 1) ──────────────────────

    def _maybe_save_large_output(
        self,
        output: str,
        agent_id: str,
        tool_name: str,
        workspace_path: str,
    ) -> str:
        """If output exceeds thresholds, save full to file and return preview.

        契约 02:
          - threshold: > 2000 lines OR > 50KB
          - file: .hiveweave/tool_outputs/<agent_id>_<ts>_<safe_tool>.txt
          - cap: 10MB per file
          - preview: head 20 lines + marker + tail 5 lines (tail only if > 25)
        """
        if not output:
            return output

        lines = output.split("\n")
        byte_len = len(output.encode("utf-8", errors="replace"))

        if len(lines) <= TOOL_OUTPUT_MAX_LINES \
                and byte_len <= TOOL_OUTPUT_MAX_BYTES:
            return output

        file_path = self._save_tool_output_file(
            output, agent_id, tool_name, workspace_path
        )

        head = lines[:PREVIEW_HEAD_LINES]
        tail = lines[-PREVIEW_TAIL_LINES:] if len(lines) > PREVIEW_TAIL_THRESHOLD \
            else []

        marker = (
            f"\n\n... [output truncated: {len(lines)} lines, "
            f"{byte_len} bytes. Full output saved to {file_path}] ...\n\n"
        )

        parts = head + [marker] + tail
        return "\n".join(parts)

    @staticmethod
    def _save_tool_output_file(
        output: str,
        agent_id: str,
        tool_name: str,
        workspace_path: str,
    ) -> str:
        """Save the full output to a temp file; return the file path.

        R6: 文件名内嵌创建时间戳（{agent_id}_{ts}_{tool}.txt），写入时 mtime
        也同步记录创建时间。cleanup_tool_outputs 据此判断保留期。
        """
        base_dir = workspace_path or os.getcwd()
        out_dir = Path(base_dir) / ".hiveweave" / "tool_outputs"
        out_dir.mkdir(parents=True, exist_ok=True)

        timestamp = int(time.time() * 1000)
        safe_name = _SAFE_NAME_RE.sub("_", tool_name)
        filename = f"{agent_id}_{timestamp}_{safe_name}.txt"
        full_path = out_dir / filename

        encoded = output.encode("utf-8", errors="replace")
        if len(encoded) > TOOL_OUTPUT_FILE_MAX_BYTES:
            capped = encoded[:TOOL_OUTPUT_FILE_MAX_BYTES]
            capped += (
                f"\n\n... [file capped at "
                f"{TOOL_OUTPUT_FILE_MAX_BYTES} bytes]"
            ).encode("utf-8")
        else:
            capped = encoded

        try:
            full_path.write_bytes(capped)
        except OSError as exc:
            log.warning("tool_output.save_failed", error=str(exc))
            return f"<save failed: {exc}>"

        return str(full_path)

    @staticmethod
    def cleanup_tool_outputs(workspace_path: str | None = None) -> None:
        """Delete tool output files older than the retention period (7 days).

        R6: 清理机制 —— 在 main.py 的 lifespan 启动阶段对每个项目工作区调用
        本方法（见 main.py "tool_outputs_cleaned"）。用文件 mtime 判断创建时间，
        删除超过 TOOL_OUTPUT_RETENTION_DAYS（7 天）的临时文件。文件名中的时间戳
        仅用于可读性，实际保留期判断以 mtime 为准（对齐 Elixir/TS 7 天保留策略）。
        """
        base_dir = workspace_path or os.getcwd()
        out_dir = Path(base_dir) / ".hiveweave" / "tool_outputs"
        if not out_dir.exists():
            return

        now = time.time()
        retention_s = TOOL_OUTPUT_RETENTION_DAYS * 86400

        for entry in out_dir.iterdir():
            try:
                mtime = entry.stat().st_mtime
                if now - mtime > retention_s:
                    entry.unlink()
            except OSError:
                continue

    # ── Helpers ──────────────────────────────────────────

    @staticmethod
    def _error(message: str) -> dict[str, Any]:
        """Build an error result dict."""
        return {"success": False, "output": "", "error": message}

    # ── High-level orchestration tool implementations ────

    async def _get_project_id(self, agent_id: str) -> str | None:
        """Resolve agent_id → project_id via Meta DB."""
        return await meta_db.get_agent_project_id(agent_id)

    async def _resolve_agent_id(self, project_id: str, name_or_id: str) -> str | None:
        """Resolve agent name/short_id/UUID to a real agent_id within a project.

        Priority: UUID exact → short_id → UUID prefix → name → role.
        Returns the agent_id (UUID) or None if not found.
        """
        if not name_or_id:
            return None
        inp = name_or_id.strip()

        # 1. Try resolve_agent (handles UUID, short_id, UUID prefix)
        agent = await self._org.resolve_agent(inp)
        if agent and agent.get("project_id") == project_id:
            return agent["id"]

        # 2. Try name / role match within the project
        all_agents = await self._org.list_agents(project_id)
        for a in all_agents:
            if a.get("name", "").lower() == inp.lower():
                return a["id"]
        for a in all_agents:
            if a.get("role", "").lower() == inp.lower():
                return a["id"]

        return None
