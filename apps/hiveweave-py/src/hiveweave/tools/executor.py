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

log = structlog.get_logger(__name__)

# ── Constants (契约 02) ────────────────────────────────────

TOOL_OUTPUT_MAX_LINES = 2000
TOOL_OUTPUT_MAX_BYTES = 50_000
TOOL_OUTPUT_RETENTION_DAYS = 7
TOOL_OUTPUT_FILE_MAX_BYTES = 10 * 1024 * 1024  # 10MB
PREVIEW_HEAD_LINES = 20  # kept for callers/tests; preview built via token_utils
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
#
# Description style (DSH contract, not a lecture): what it does; invariants;
# failure markers; output shape; next action on the long path; what not to do.
# Cross-call workflow lives in role prompts. Hive wakes via
# [BASH|SUBAGENT DONE/FAILED] — do not document job_output.

TOOL_PARAM_SCHEMAS: dict[str, dict] = {
    "bash": {
        "description": (
            "Execute a shell command and return stdout/stderr. Each call runs "
            "in a fresh shell: cwd, variables, and functions do not persist — "
            "do not `cd` expecting the next call to stay there (cwd is your "
            "workspace). Check `Exit code: N` on every result before moving "
            "on. Long output is truncated to head+tail; the full text is "
            "saved and the path is reported. Windows: Git Bash (`bash -c`) "
            "first, else cmd — not PowerShell. Prefer `tail -n N`, "
            "`uv run python` (bare `python` may be missing). Never invent "
            "`/workspace` or strip backslashes (`D:PC_AI...` is invalid). "
            "Set `background=true` for long scripts/tests: the call returns "
            "immediately with `waiting_on` (job id `bg-bash-…`). Then "
            "`commit_turn(phase=waiting)` using that list; do not poll. "
            "Woken with `[BASH DONE]` / `[BASH FAILED]`. No command timeout "
            "until done, `job_kill`, or project stop. Do not use "
            "`background=true` for `vite` / `npm run dev` / `uvicorn`. "
            "Long-running servers are auto-registered (tracked, killable); "
            "prefer `start_dev_server`. Do not append `&` on a foreground "
            "command. Default false keeps stdout in this turn."
        ),
        "properties": {
            "command": {
                "type": "string",
                "aliases": ["cmd", "run"],
                "description": "The bash command to execute.",
            },
            "timeout": {"type": "integer", "aliases": ["timeout_ms", "timeoutMs"],
                        "description": "Foreground only (5s–10min). Ignored when background=true. Default: 120000 (2 min). Max: 600000 (10 min). Values 1-600 are treated as seconds (e.g. 30 = 30s). The executor kills the command on expiry."},
            "background": {
                "type": "boolean",
                "aliases": ["bg"],
                "description": (
                    "Run off the org turn and return a job id immediately. "
                    "No timeout. Then commit_turn(waiting) with waiting_on; "
                    "woken with [BASH DONE]/[BASH FAILED]. Stop with "
                    "job_kill. Default false."
                ),
            },
            "taskId": {
                "type": "string",
                "aliases": ["task_id"],
                "description": (
                    "Optional task id to bind test_run attestation "
                    "(reviewers: pass the task under review)."
                ),
            },
        },
        "required": ["command"],
    },
    "browse": {
        "description": (
            "Drive Chromium via agent-browser CLI. Typical: "
            "goto URL → snapshot -i → click @eN → screenshot path → "
            "assert_visual. Screenshot pixels inject into the next turn; "
            "a PNG path is not UI evidence. Prefer after "
            "start_dev_server / lookup_dev_server."
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
    "assert_visual": {
        "description": (
            "Record a pixel-grounded UI assertion AFTER browse(screenshot). "
            "Describe what you SEE in the injected image (labels, layout, errors), "
            "then verdict=pass|fail. Creates visual_check attestation required for "
            "ui_browser_e2e submit. Path-only / 'looks fine' is rejected."
        ),
        "properties": {
            "screenshotPath": {
                "type": "string",
                "aliases": ["screenshot_path", "path"],
                "description": "PNG/JPEG path from browse screenshot.",
            },
            "observed": {
                "type": "string",
                "description": (
                    "What you see in the image pixels (>=40 chars). "
                    "Not the file path."
                ),
            },
            "verdict": {
                "type": "string",
                "enum": ["pass", "fail"],
                "description": "pass if criterion met; else fail.",
            },
            "criteria": {
                "type": "string",
                "description": "Optional acceptance criterion this check covers.",
            },
            "taskId": {
                "type": "string",
                "aliases": ["task_id"],
                "description": "Optional task id to bind attestation.",
            },
        },
        "required": ["screenshotPath", "observed", "verdict"],
    },
    "look_at_image": {
        "description": (
            "Ask the configured vision model about a workspace image. "
            "One-shot text answer; does not inject pixels into chat. "
            "To inspect another agent's screenshot, pass attestation_id "
            "from their browse_e2e / visual_check row instead of a path "
            "under your own worktree."
        ),
        "properties": {
            "image_path": {
                "type": "string",
                "aliases": ["path", "file", "screenshot", "image"],
                "description": (
                    "Image path relative to workspace or absolute under it. "
                    "Optional if attestation_id is set."
                ),
            },
            "attestation_id": {
                "type": "string",
                "aliases": ["attestationId", "att_id"],
                "description": (
                    "tool_attestations id whose artifact_hashes.screenshot_path "
                    "points at the PNG (cross-worktree). Prefer this over "
                    "copying another agent's absolute path."
                ),
            },
            "prompt": {
                "type": "string",
                "aliases": ["question", "query", "instruction"],
                "description": (
                    "What the vision model should look for and how to answer."
                ),
            },
        },
        "required": ["prompt"],
    },
    "generate_image": {
        "description": (
            "Generate a PNG via Seedream (text-to-image) under the workspace. "
            "Requires SOURCE_WRITE."
        ),
        "properties": {
            "prompt": {
                "type": "string",
                "aliases": ["text", "description"],
                "description": "Text prompt describing the image to generate.",
            },
            "size": {
                "type": "string",
                "description": 'Output size: "2K", "4K", or WxH. Default 2K.',
            },
            "output_path": {
                "type": "string",
                "aliases": ["path", "file", "save_as"],
                "description": (
                    "Optional workspace-relative save path "
                    "(default .hiveweave/generated/...)."
                ),
            },
            "watermark": {
                "type": "boolean",
                "description": "Add AI watermark (default false).",
            },
        },
        "required": ["prompt"],
    },
    "game_run_case": {
        "description": (
            "H5/canvas game harness runner. After browse(goto) to the game: "
            "probe → list → run(caseId). run() executes window.__HW_TEST__, "
            "screenshots canvas, returns codePass + visionCriteria; then "
            "assert_visual. No harness → observe-only (no gameplay pass)."
        ),
        "properties": {
            "action": {
                "type": "string",
                "enum": ["probe", "list", "run"],
                "description": "probe | list | run",
            },
            "caseId": {
                "type": "string",
                "aliases": ["case_id", "id"],
                "description": "Required for action=run.",
            },
            "screenshotPath": {
                "type": "string",
                "aliases": ["screenshot_path"],
                "description": "Screenshot output path after run.",
            },
            "screenshotSelector": {
                "type": "string",
                "aliases": ["selector"],
                "description": "CSS selector (default canvas).",
            },
            "timeoutSec": {
                "type": "integer",
                "aliases": ["timeout_sec", "timeout"],
                "description": "Per-step timeout seconds (default 90).",
            },
            "taskId": {
                "type": "string",
                "aliases": ["task_id"],
                "description": "Optional task id for attestation.",
            },
        },
        "required": ["action"],
    },
    "run_command": {
        "description": (
            "Execute a command and return stdout/stderr, like bash but with "
            "an explicit cwd. Check `Exit code: N`. Prefer bash unless you "
            "need a working directory other than the workspace root."
        ),
        "properties": {
            "command": {"type": "string", "aliases": ["cmd", "run"]},
            "cwd": {"type": "string", "description": "Working directory (relative to workspace). Default: workspace root."},
            "timeout": {"type": "integer", "aliases": ["timeout_ms", "timeoutMs"],
                        "description": "Timeout in milliseconds. Default: 120000 (2 min). Max: 600000 (10 min). Values 1-600 are treated as seconds."},
        },
        "required": ["command"],
    },
    "read_file": {
        "description": (
            "Read a UTF-8 text file and return line-numbered content. "
            "Use offset/limit for a slice; do not dump a huge file in one call."
        ),
        "properties": {
            "filePath": {
                "type": "string",
                "aliases": ["path", "file_path", "file"],
                "description": "Path to read (relative to workspace).",
            },
            "offset": {"type": "integer", "aliases": ["startLine"],
                "description": "Starting line number (0-based, default: 0)."},
            "limit": {"type": "integer", "aliases": ["maxLines", "lineLimit"],
                "description": "Max lines to return (default: 2000)."},
        },
        "required": ["filePath"],
    },
    "write_file": {
        "description": (
            "Create or fully replace a UTF-8 text file. Prefer edit_file / "
            "apply_patch for a small change to an existing file."
        ),
        "properties": {
            "filePath": {
                "type": "string",
                "aliases": ["path", "file_path", "file"],
                "description": "Path to write (relative to workspace).",
            },
            "content": {
                "type": "string",
                "aliases": ["data", "text", "body"],
                "description": "Full UTF-8 text to write.",
            },
        },
        "required": ["filePath", "content"],
    },
    "list_files": {
        "description": (
            "List file and directory names at a path. recursive=true walks "
            "up to maxdepth 3 (clamped). Does not search contents — use "
            "grep; for a filename glob use search_files."
        ),
        "properties": {
            "dirPath": {"type": "string", "aliases": ["path", "directory", "dir"]},
            "recursive": {"type": "boolean", "description": "If true, list recursively. Default: false."},
            "maxdepth": {"type": "integer", "description": "Max depth when recursive (1-3). Default: 1. Values above 3 are clamped to 3."},
        },
        "required": [],
    },
    "grep": {
        "description": (
            "Search file contents with a regex. Returns matching paths and "
            "lines. Use read_file on a match for surrounding context. For "
            "filename/glob search use search_files — not bash grep."
        ),
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
        "description": (
            "Find files whose names match a glob. Returns matching paths. "
            "For content search use grep."
        ),
        "properties": {
            "pattern": {"type": "string", "aliases": ["glob", "query", "search", "name"]},
            "directory": {"type": "string", "aliases": ["path", "dir"]},
        },
        "required": ["pattern"],
    },
    "edit_file": {
        "description": (
            "Edit an existing UTF-8 text file by replacing literal text. "
            "old_string must match exactly (whitespace included). When "
            "replace_all is false (default), it must appear exactly once — "
            "include enough context to make it unique. Empty new_string "
            "deletes the match. Read the file first."
        ),
        "properties": {
            "filePath": {
                "type": "string",
                "aliases": ["path", "file_path", "file"],
                "description": "Path to edit (relative to workspace).",
            },
            "old_string": {
                "type": "string",
                "aliases": ["oldString", "old_str", "search", "find"],
                "description": "Literal text to replace. Must match exactly.",
            },
            "new_string": {
                "type": "string",
                "aliases": ["newString", "new_str", "replace", "replacement"],
                "description": "Literal replacement. Empty string deletes the match.",
            },
            "replace_all": {
                "type": "boolean",
                "aliases": ["replaceAll"],
                "description": "Replace all matches. Default false: old_string must appear exactly once.",
            },
        },
        "required": ["filePath", "old_string", "new_string"],
    },
    "apply_patch": {
        "description": (
            "Apply file patch operations (add/update/delete). Prefer this "
            "or edit_file for a small change; write_file fully replaces a file."
        ),
        "properties": {
            "patches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "op": {"type": "string", "description": "Operation: 'add' (create), 'update' (replace), or 'delete'"},
                        "filePath": {"type": "string", "description": "Path to the file (relative to workspace)"},
                        "oldString": {"type": "string", "description": "For update: literal text to find. Must match exactly."},
                        "newString": {"type": "string", "description": "For update: literal replacement. Empty string deletes the match."},
                        "content": {"type": "string", "description": "For add: full file content"},
                    },
                },
                "description": "Array of patch operations",
            },
        },
        "required": [],
    },
    "websearch": {
        "description": (
            "Search the public web. Returns title, URL, and snippet."
        ),
        "properties": {
            "query": {"type": "string", "aliases": ["search", "q", "term"]},
            "numResults": {"type": "integer", "aliases": ["num_results", "limit", "count"],
                "description": "Number of results (1-8, default: 5)."},
        },
        "required": ["query"],
    },
    "calculate": {
        "description": (
            "Evaluate a math expression exactly. Do not compute non-trivial "
            "arithmetic by hand. '^' is power. Functions: sqrt sin cos tan "
            "log exp …; constants: pi e tau inf."
        ),
        "properties": {
            "expression": {"type": "string", "aliases": ["expr", "formula", "math", "calc"]},
        },
        "required": ["expression"],
    },
    "question": {
        "description": (
            "Ask the human a question; optional choices. Blocks until they "
            "answer. Assistant text is not delivered — only this tool is."
        ),
        "properties": {
            "question": {"type": "string", "aliases": ["message", "content", "query", "text"]},
            "options": {"type": "array", "aliases": ["choices"]},
        },
        "required": ["question"],
    },
    "todowrite": {
        "description": (
            "Update this turn's personal todo list; mark items completed "
            "when done. Not the Task Ledger — durable notes go in "
            "write_work_log."
        ),
        "properties": {
            "todos": {"type": "array", "aliases": ["tasks", "items", "list"]},
        },
        "required": ["todos"],
    },
    "spawn_subagent": {
        "description": (
            "Delegate a self-contained task to a subagent in its own context "
            "(it does not see this conversation). It works in YOUR worktree "
            "with YOUR permissions and returns its result, not intermediate "
            "steps. This call returns immediately with waiting_on — then "
            "commit_turn(phase=waiting) using that list; do not poll. Woken "
            "with [SUBAGENT DONE] / [SUBAGENT FAILED]. Give a complete "
            "standalone prompt. subagent_type is REQUIRED: readonly | audit "
            "| write. Concurrent writes to the same files will collide. "
            "Do not nest this work inside the current LLM turn."
        ),
        "type": "object",
        "properties": {
            "subagent_type": {
                "type": "string",
                "enum": ["readonly", "audit", "write"],
                "aliases": ["type", "kind"],
                "description": (
                    "REQUIRED. readonly = read-only scout; audit = run "
                    "tests/browse + submit (no attestation); write = edit "
                    "code + git_worktree (parent must have SOURCE_WRITE)."
                ),
            },
            "prompt": {
                "type": "string",
                "description": (
                    "The complete, self-contained task. The child does not "
                    "share this conversation, so include files, goals, and "
                    "acceptance criteria."
                ),
                "aliases": ["task", "instructions", "work"],
            },
            "description": {
                "type": "string",
                "description": "Short (3-5 word) label for the waiting context.",
                "aliases": ["desc", "title"],
            },
            "timeout_s": {
                "type": "integer",
                "minimum": 0,
                "maximum": 480,
                "description": (
                    "Optional hard deadline in seconds. Omit or 0: no wall "
                    "clock (job_kill / stream idle / commit_turn). Does not "
                    "extend the parent turn."
                ),
            },
        },
        "required": ["subagent_type", "prompt"],
    },
    "job_kill": {
        "description": (
            "Request cancellation of a live background bash or "
            "spawn_subagent job by job id (bg-bash-… / bg-sub-…). Returns "
            "immediately; the job settles as killed once its work actually "
            "stops. Woken with [BASH FAILED] / [SUBAGENT FAILED] if a wait "
            "was armed."
        ),
        "type": "object",
        "properties": {
            "jobId": {
                "type": "string",
                "aliases": ["job_id", "id"],
                "description": (
                    "Job id returned when the background work started."
                ),
            },
        },
        "required": ["jobId"],
    },
    "send_message": {
        "description": (
            "Send a tool-visible message to agents (or recipient 'user'). "
            "Assistant text is private. Prefer ask_agent (needs reply) or "
            "notify_agent (FYI). To close an ask, pass replyTo=the original "
            "reply_contract_id. End the turn with commit_turn."
        ),
        "properties": {
            "recipients": {"type": "array", "aliases": ["recipient", "to", "targets"]},
            "message": {"type": "string", "aliases": ["content", "body", "text"]},
            "expectReport": {
                "type": "boolean",
                "aliases": ["expect_report"],
                "description": (
                    "True when the recipient must reply. Not inferred from "
                    "message wording — set this or use ask_agent."
                ),
            },
            "priority": {"type": "string", "aliases": ["level"]},
            "replyTo": {
                "type": "string",
                "aliases": ["reply_to", "replyContractId"],
                "description": (
                    "Original message's reply_contract_id. Required to close "
                    "an ask; do not pass a tool-result message id."
                ),
            },
        },
        "required": ["recipients", "message"],
    },
    "ask_agent": {
        "description": (
            "Ask agents and require a reply. Prefer over "
            "send_message(expectReport=true). When answering an existing ask, "
            "pass replyTo=that message's reply_contract_id (not the "
            "tool-result message id) or a new obligation is created."
        ),
        "properties": {
            "recipients": {"type": "array", "aliases": ["recipient", "to", "targets", "target"]},
            "message": {"type": "string", "aliases": ["content", "body", "text"]},
            "priority": {"type": "string", "aliases": ["level"]},
            "replyTo": {
                "type": "string",
                "aliases": ["reply_to", "replyContractId"],
                "description": (
                    "Original message's reply_contract_id. Required to close "
                    "an ask; do not pass a tool-result message id."
                ),
            },
        },
        "required": ["recipients", "message"],
    },
    "notify_agent": {
        "description": (
            "FYI notify — does not require a reply. When answering an "
            "existing ask, pass replyTo=that reply_contract_id to close it "
            "without opening a new one."
        ),
        "properties": {
            "recipients": {"type": "array", "aliases": ["recipient", "to", "targets", "target"]},
            "message": {"type": "string", "aliases": ["content", "body", "text"]},
            "priority": {"type": "string", "aliases": ["level"]},
            "replyTo": {
                "type": "string",
                "aliases": ["reply_to", "replyContractId"],
                "description": (
                    "Original message's reply_contract_id. Closes an ask "
                    "without creating a new reply obligation."
                ),
            },
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
            "Declare this turn cannot advance actionable tasks. Stops "
            "[TASK ADVANCE] nudges until the next wake. Requires a concrete "
            "reason. Does not replace commit_turn."
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
        "description": (
            "Hire and deploy an agent. Returns the new agent id. role is a "
            "display title (executors: module+job, not bare 前端工程师). "
            "permissionType=coordinator|executor sets authority. Skills: "
            "\"#N\" from list_available_skills or a full slug — not raw "
            "tech names."
        ),
        "properties": {
            "name": {"type": "string"},
            "role": {"type": "string", "description": "Chinese job title (display label — does NOT set permission; use permissionType). For executors MUST include owned module, e.g. 签到排行榜工程师 / 认证API工程师 — NOT bare 前端工程师."},
            "permissionType": {
                "type": "string",
                "enum": ["coordinator", "executor"],
                "description": "coordinator = manages subordinates (dispatch_task/review_task); executor = hands-on work (claim_task/submit_task). Pass through the requester's value.",
            },
            "goal": {"type": "string"},
            "systemPrompt": {
                "type": "string",
                "aliases": ["system_prompt", "backstory"],
                "description": "2-4 sentence character narrative (also accepted as backstory).",
            },
            "skills": {"type": "array", "items": {"type": "string"}, "description": "Skills to bind. Tool skills: use \"#N\" to reference skills from list_available_skills by number (e.g. \"#1\"). Discipline skills: use full slug from matching table (e.g. \"self-review\"). NOT raw tech names like 'React 18'."},
            "parentId": {"type": "string", "aliases": ["parent_id", "parent", "parentAgentId", "parent_agent_id"]},
            "templateId": {"type": "string", "aliases": ["template_id"]},
        },
        "required": ["name", "role"],
    },
    "read_charter": {
        "description": "Read the organization charter (mission, rules).",
        "properties": {},
        "required": [],
    },
    "save_charter": {
        "description": "Create or replace the organization charter.",
        "properties": {
            "content": {"type": "string", "aliases": ["charter", "body", "text"]},
            "title": {"type": "string", "aliases": ["name"]},
        },
        "required": ["content"],
    },
    "read_goals": {
        "description": "Read current organization goals.",
        "properties": {},
        "required": [],
    },
    "update_goals": {
        "description": "Update organization goals. At least one field must be set.",
        "properties": {
            "objective": {"type": "string"},
            "focus": {"type": "string"},
            "keyResults": {"type": "array", "aliases": ["key_results"]},
            "userInvolvement": {"type": "string", "aliases": ["user_involvement"]},
        },
        "required": [],
    },
    "read_memory": {
        "description": (
            "Read stored memory. Pass moduleId for one entry; omit to list "
            "all. One entry per moduleId (latest write wins)."
        ),
        "properties": {
            "moduleId": {"type": "string", "aliases": ["module_id", "id", "key"]},
        },
        "required": [],
    },
    "write_memory": {
        "description": (
            "Upsert memory under moduleId (same id overwrites). To keep "
            "history, use a distinct moduleId per entry."
        ),
        "properties": {
            "content": {"type": "string", "aliases": ["data", "body", "text", "memory"]},
            "moduleId": {"type": "string", "aliases": ["module_id", "id", "key"]},
            "tags": {"type": "array", "items": {"type": "string"}, "aliases": []},
        },
        "required": ["content"],
    },
    "list_available_skills": {
        "description": (
            "List marketplace skills (built-in + external + skills.sh). "
            "Pass search to filter. Returns numbered rows (#1, #2); pass "
            "\"#N\" or the full slug to hire_agent."
        ),
        "properties": {
            "search": {"type": "string", "description": "Optional keyword to filter skills (e.g. 'react', 'testing', 'planning'). Case-insensitive."},
        },
        "required": [],
    },
    "read_skill": {
        "description": "Read a skill's SKILL.md by name or slug.",
        "properties": {
            "skill": {"type": "string", "aliases": ["name", "slug", "id"]},
        },
        "required": ["skill"],
    },
    "read_roster": {
        "description": "List agents with role and department.",
        "properties": {},
        "required": [],
    },
    "update_roster": {
        "description": "Update one agent's roster fields (position, department, status).",
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
        "description": "Show the reporting tree.",
        "properties": {},
        "required": [],
    },
    "list_subordinates": {
        "description": "List your direct reports.",
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
        "description": (
            "Schedule a game-time alarm (optional repeat). Does not unblock "
            "a blocked task — that needs wakeAt on update_task_status."
        ),
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
        "description": (
            "Read work logs. Pass agentId for one agent; omit to read your "
            "subordinates. Each row is type + summary."
        ),
        "properties": {
            "agentId": {"type": "string", "aliases": ["agent_id", "target"]},
            "limit": {"type": "integer", "aliases": ["count", "max"]},
        },
        "required": [],
    },
    "run_code_review": {
        "description": "LLM review of files for quality, correctness, and style. Returns findings — not a test_run attestation.",
        "properties": {
            "filePaths": {"type": "array", "items": {"type": "string"},
                "aliases": ["files", "target", "path", "file", "module"]},
            "testFiles": {"type": "array", "items": {"type": "string"},
                "aliases": ["test_files"]},
        },
        "required": ["filePaths"],
    },
    "run_security_audit": {
        "description": "LLM review of files for security issues. Returns findings — not a test_run attestation.",
        "properties": {
            "filePaths": {"type": "array", "items": {"type": "string"},
                "aliases": ["files", "target", "path", "file", "module"]},
            "testFiles": {"type": "array", "items": {"type": "string"},
                "aliases": ["test_files"]},
        },
        "required": ["filePaths"],
    },
    "run_tests": {
        "description": "Run the project's tests for the given files. For review evidence prefer bash(..., taskId=...).",
        "properties": {
            "filePaths": {"type": "array", "items": {"type": "string"},
                "aliases": ["files", "target", "path", "file", "module", "testPath"]},
            "testFiles": {"type": "array", "items": {"type": "string"},
                "aliases": ["test_files"]},
        },
        "required": ["filePaths"],
    },
    "run_perf_audit": {
        "description": "LLM review of files for performance issues. Returns suggestions — not a test_run attestation.",
        "properties": {
            "filePaths": {"type": "array", "items": {"type": "string"},
                "aliases": ["files", "target", "path", "file", "module"]},
            "testFiles": {"type": "array", "items": {"type": "string"},
                "aliases": ["test_files"]},
        },
        "required": ["filePaths"],
    },
    "run_full_review": {
        "description": "Run code, security, test, and performance reviews together. Returns findings — not a test_run attestation.",
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
        "description": "Archive/fire an agent. Cannot be undone. Prefer transfer_agent or bind_skill first.",
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
        "description": "Bind a skill slug (or \"#N\" from list_available_skills) to an agent.",
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
        "description": (
            "Message ALL direct reports at once (recipient is ignored). "
            "FYI only — for a reply use ask_agent; for assigned work use "
            "dispatch_task."
        ),
        "properties": {
            "message": {"type": "string", "aliases": ["content", "body", "text"]},
            "recipient": {
                "type": "string",
                "aliases": ["to", "target", "agentId", "agent_id"],
                "description": "Ignored. Always broadcasts to every direct report.",
            },
        },
        "required": ["message"],
    },
    "message_superior": {
        "description": (
            "Message your parent (FYI). For finishing assigned work use "
            "submit_task. For a required reply use ask_agent."
        ),
        "properties": {
            "message": {"type": "string", "aliases": ["content", "body", "text"]},
        },
        "required": ["message"],
    },
    "message_peer": {
        "description": "Message one same-level peer (FYI). For a required reply use ask_agent.",
        "properties": {
            "recipient": {"type": "string", "aliases": ["to", "target", "agentId", "agent_id"]},
            "message": {"type": "string", "aliases": ["content", "body", "text"]},
        },
        "required": ["recipient", "message"],
    },
    "message_team": {
        "description": "Broadcast to every agent in the project except you (FYI).",
        "properties": {
            "message": {"type": "string", "aliases": ["content", "body", "text"]},
        },
        "required": ["message"],
    },
    "dispatch_task": {
        "description": (
            "Assign work now: ledger entry AND inbox wake. Pass taskId to "
            "reuse a create_task draft. create_task alone does not wake anyone. "
            "Same-assignee duplicates cannot be forced."
        ),
        "properties": {
            "target": {"type": "string", "aliases": ["toAgentId", "to_agent_id", "recipient", "agentId", "subordinate", "agent_id"]},
            "task": {"type": "string", "aliases": ["description", "message", "content", "summary", "desc"]},
            "expectReport": {"type": "boolean", "aliases": ["expect_report"]},
            "taskId": {"type": "string", "aliases": ["task_id", "existing_task_id", "existingTaskId"],
                "description": "Optional: reuse an existing task instead of creating a new one"},
            "force": {
                "type": "boolean",
                "description": "Create despite a cross-assignee/structured dup. Same-assignee dup cannot be forced.",
            },
            "parentTaskId": {"type": "string", "aliases": ["parent_task_id"]},
            "expectedModules": {
                "type": "array",
                "items": {"type": "string"},
                "aliases": ["expected_modules"],
            },
            "artifactRefs": {
                "type": "array",
                "items": {"type": "string"},
                "aliases": ["artifact_refs", "required_paths"],
                "description": "Paths the assignee must be able to read (checked in their worktree).",
            },
        },
        "required": ["target", "task"],
    },
    "review": {
        "description": "Run a local review suite (code_review|security_audit|test_review|perf_audit) on filePaths. Returns findings — not a test_run attestation.",
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
        "description": "DEPRECATED. Use review_task(taskId, decision=...).",
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
        "description": "Append a durable record of work just done. todowrite is only this-turn planning.",
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
        "description": (
            "Merge a worktree branch into main and remove the worktree. "
            "Pass taskId to hit hw/<shortId>/t-<taskId[:8]>. On conflict: "
            "abort — rework the executor to rebase main in their worktree. "
            "On success: spawn VERIFY only for tasks this merge covers."
        ),
        "properties": {
            "branchName": {"type": "string", "aliases": ["branch_name", "branch", "name"]},
            "targetBranch": {"type": "string", "aliases": ["target_branch", "target"]},
            "taskId": {
                "type": "string",
                "aliases": ["task_id"],
                "description": "Optional. Tries hw/<shortId>/t-<taskId[:8]> first.",
            },
            "dryRun": {"type": "boolean", "aliases": ["dry_run", "preflight"],
                "description": "Preflight only: list missing items. No merge, no teardown. Default false."},
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
        "description": (
            "Fetch a URL and extract readable text. Optional prompt to "
            "answer from the page. SSRF-blocked."
        ),
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
        "description": (
            "Write a Task Ledger row. Does not inbox/wake anyone. Unassigned "
            "→ created; with assigneeId → claimed (assign=claim), except "
            "VERIFY stays created until the serial lock is free. To wake, "
            "call dispatch_task (pass taskId to reuse). Prefer "
            "dispatch_task(taskId=…) to transfer an existing task."
        ),
        "properties": {
            "title": {"type": "string", "aliases": ["name", "summary"]},
            "description": {"type": "string", "aliases": ["detail", "body"]},
            "priority": {"type": "integer", "aliases": ["level"],
                "description": "1=high, 2=normal (default), 3=low."},
            "dueAt": {"type": "integer", "aliases": ["due_at", "deadline"],
                "description": "Optional due time as epoch milliseconds."},
            "assigneeId": {"type": "string", "aliases": ["assignee_id", "assignee"]},
            "acceptanceCriteria": {"type": "array", "items": {"type": "string"},
                "aliases": ["acceptance_criteria"]},
            "parentTaskId": {"type": "string", "aliases": ["parent_task_id", "parent"]},
            "dependsOn": {"type": "array", "items": {"type": "string"},
                "aliases": ["depends_on"]},
            "expectedModules": {"type": "array", "items": {"type": "string"},
                "aliases": ["expected_modules"]},
            "tags": {"type": "array", "items": {"type": "string"}},
            "contractJson": {
                "type": "object",
                "aliases": ["contract_json", "contract"],
                "description": "Optional slice contract (ready gate + machine acceptance).",
            },
            "force": {
                "type": "boolean",
                "description": "Create despite a cross-assignee/structured dup. Same-assignee dup cannot be forced.",
            },
        },
        "required": ["title", "description"],
    },
    "claim_task": {
        "description": "Claim a created/unassigned task (created → claimed). Sets you as assignee.",
        "properties": {
            "taskId": {"type": "string", "aliases": ["task_id", "id"]},
        },
        "required": ["taskId"],
    },
    "update_task_status": {
        "description": (
            "Set status to running (start or unblock) or blocked. "
            "blocked requires dependsOnTaskIds and/or wakeAt — a block "
            "with neither is rejected. Status omitted defaults to running. "
            "blockedReason is a note only, never an unblock path."
        ),
        "properties": {
            "taskId": {"type": "string", "aliases": ["task_id", "id"]},
            "status": {"type": "string",
                "description": "Target status: 'running' or 'blocked'. Defaults to 'running'.",
                "enum": ["running", "blocked"]},
            "blockedReason": {"type": "string",
                "aliases": ["blocked_reason", "reason"],
                "description": "Required when blocked. Human-readable note only — auto-unblock is declared via dependsOnTaskIds / wakeAt, never inferred from this text."},
            "dependsOnTaskIds": {"type": "array",
                "items": {"type": "string"},
                "aliases": ["depends_on_task_ids", "dependsOnTaskId",
                            "depends_on_task_id", "dependsOn"],
                "description": "Blocker task ids (auto-unblock when all approved/closed). Passing dependsOnTaskIds or wakeAt is REQUIRED when blocking."},
            "waitKind": {"type": "string",
                "aliases": ["wait_kind"],
                "enum": ["dependency", "timer", "user", "external"],
                "description": "Structured wait kind. Inferred from dependsOnTaskIds (dependency) or wakeAt (timer) when omitted. Never inferred from blockedReason text."},
            "wakeAt": {"type": "string",
                "aliases": ["wake_at"],
                "description": "Deadline for timer waits: ISO-8601 datetime (naive = UTC) or epoch milliseconds. Auto-unblocks at this time."},
        },
        "required": ["taskId"],
    },
    "update_progress": {
        "description": "Set task progress 0-100. Does not change status.",
        "properties": {
            "taskId": {"type": "string", "aliases": ["task_id", "id"]},
            "progress": {"type": "integer", "aliases": ["percent"],
                "description": "Progress percentage (0-100)."},
        },
        "required": ["taskId", "progress"],
    },
    "submit_task": {
        "description": (
            "Submit for review (running → submitted). testsPassed must be "
            "true after real tests. Pass attestationIds from bash/browse. "
            "dryRun=true lists missing items without submitting."
        ),
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
            "dryRun": {"type": "boolean", "aliases": ["dry_run", "preflight"],
                "description": "Preflight only: list missing items. No submit. Default false."},
            "failuresAcknowledged": {
                "type": "array",
                "aliases": ["failures_acknowledged", "acknowledgedFailures"],
                "description": "VERIFY only: when tests report failures, list {test, reason} per case. Free-text excuses are rejected.",
            },
            "coreInteractionExecuted": {
                "type": "boolean",
                "aliases": ["core_interaction_executed"],
                "description": "UI VERIFY: true if core canvas/DOM interaction ran. Prefer unset — platform accepts a core_interaction browse_e2e attestation.",
            },
            "commitHash": {
                "type": "string",
                "aliases": ["commit_hash"],
                "description": "Optional MAIN commit hash for VERIFY evidence.",
            },
            "envSnapshot": {
                "type": "string",
                "aliases": ["env_snapshot"],
                "description": "Optional environment snapshot for VERIFY evidence.",
            },
        },
        "required": ["summary", "testsPassed"],
    },
    "review_task": {
        "description": (
            "Review a submitted task: decision=approve|rework. approve needs "
            "fresh test_run (or docs_only attest_doc_review) bound to this "
            "taskId — not bare testsPassed. Run tests yourself with "
            "bash(..., taskId=this) first; if rejected for missing evidence, "
            "do not retry approve. Does not spawn VERIFY — call "
            "git_worktree_merge next. waive_attestation: another agent must "
            "approve (VERIFY waive is CEO-only)."
        ),
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
        "description": (
            "List Task Ledger rows. Optional status or assigneeId. "
            "Excludes archived."
        ),
        "properties": {
            "status": {"type": "string"},
            "assigneeId": {"type": "string",
                "aliases": ["assignee_id", "assignee"]},
        },
        "required": [],
    },
    "attest_doc_review": {
        "description": (
            "Machine-check document files (exist; optional minLines). "
            "Creates a doc_review attestation. source=auto prefers your "
            "worktree, else project main. Returns attestationId for "
            "submit_task / review_task. Tag docs_only so approve needs this "
            "kind, not test_run."
        ),
        "properties": {
            "taskId": {"type": "string", "aliases": ["task_id"]},
            "files": {
                "type": "array",
                "description": "Each {path, minLines?}. Paths relative to the chosen root.",
            },
            "source": {
                "type": "string",
                "enum": ["auto", "worktree", "main"],
                "aliases": ["workspaceSource", "workspace"],
                "description": "Where to read files. Default auto.",
            },
        },
        "required": ["files"],
    },
    "cancel_task": {
        "description": (
            "Archive a mistaken or obsolete task (coordinator). It leaves "
            "lists and obligations. Prefer unclaim+dispatch when the task "
            "is still needed."
        ),
        "properties": {
            "taskId": {"type": "string", "aliases": ["task_id", "id"]},
            "reason": {
                "type": "string",
                "aliases": ["feedback", "comment", "description", "message", "why", "note"],
                "description": "Why this task is cancelled (audit).",
            },
        },
        "required": ["taskId", "reason"],
    },
    "unclaim_task": {
        "description": (
            "Release a claimed task back to created and clear assignee "
            "(coordinator). Then dispatch_task to the right agent."
        ),
        "properties": {
            "taskId": {"type": "string", "aliases": ["task_id", "id"]},
        },
        "required": ["taskId"],
    },
    "reassign_task": {
        "description": (
            "Transfer assignee and obligation (coordinator/CEO). Messages "
            "alone create no obligation. Wakes the new assignee. A queued "
            "VERIFY stays queued until MAIN is free."
        ),
        "properties": {
            "taskId": {"type": "string", "aliases": ["task_id", "id"]},
            "assigneeId": {
                "type": "string",
                "aliases": ["assignee_id", "to"],
                "description": "New assignee (id, short_id, or 花名).",
            },
            "reason": {"type": "string"},
        },
        "required": ["taskId", "assigneeId"],
    },
    "waive_attestation": {
        "description": (
            "Last-resort waiver of the attestation gate (coordinator/CEO). "
            "Requires evidenceAttestationId of a real test_run / browse_e2e / "
            "visual_check / doc_review. Max 2 per task. The waiving agent "
            "cannot later approve. Prefer attest_doc_review for docs_only."
        ),
        "properties": {
            "taskId": {"type": "string", "aliases": ["task_id", "id"]},
            "reason": {"type": "string"},
            "evidenceAttestationId": {
                "type": "string",
                "aliases": ["evidence_attestation_id"],
                "description": "Id of a real execution attestation. Pure read_file is not enough.",
            },
        },
        "required": ["taskId", "reason", "evidenceAttestationId"],
    },
    "waive_merge": {
        "description": (
            "Last-resort waiver of merge-before-close (coordinator/CEO). "
            "Prefer git_worktree_merge. After this, close may proceed; "
            "evidence records merge_waived."
        ),
        "properties": {
            "taskId": {"type": "string", "aliases": ["task_id", "id"]},
            "reason": {"type": "string", "description": "Auditable reason (min 20 chars)."},
        },
        "required": ["taskId", "reason"],
    },
    "check_agent_status": {
        "description": (
            "Read-only: busy/idle, disposition, unread_wake. Call this "
            "before claiming someone is busy or nagging a silence. "
            "Pass agentId (花名/short_id/UUID); omit to list the project."
        ),
        "properties": {
            "agentId": {
                "type": "string",
                "aliases": ["agent_id", "name", "target"],
                "description": "花名, short_id, or UUID. Not a role title. Omit to list all.",
            },
        },
        "required": [],
    },
    "check_agent_progress": {
        "description": (
            "CEO read-only snapshot: disposition, open obligations, last "
            "output. Does not send messages or wake anyone."
        ),
        "properties": {
            "agentId": {
                "type": "string",
                "aliases": ["agent_id", "name", "target"],
                "description": "花名, short_id, or UUID.",
            },
        },
        "required": ["agentId"],
    },
    "get_platform_state": {
        "description": (
            "Read-only platform ground truth (gates, ledger, org, runtime) "
            "tagged verified/claimed/unknown. Trust this over peer chat "
            "when they conflict."
        ),
        "properties": {},
        "required": [],
    },
    "git_worktree_create": {
        "description": (
            "Always rejected for agents. Worktrees are created on "
            "hire/dispatch. After approve, git_worktree_merge."
        ),
        "properties": {
            "branchName": {
                "type": "string",
                "aliases": ["branch_name", "branch", "name", "taskName", "task_name", "task"],
            },
            "baseBranch": {
                "type": "string",
                "aliases": ["base_branch", "base"],
            },
        },
        "required": ["branchName"],
    },
    "start_dev_server": {
        "description": (
            "Start the project Vite/dev server on a non-reserved port "
            "(never 4000/5173/4173). Registers pid/cwd/port. Prefer this "
            "over bare npm run dev / vite. Extra env belongs in "
            ".hiveweave/env.sh or an inline VAR=x prefix."
        ),
        "properties": {
            "command": {
                "type": "string",
                "description": "Optional override. Default: npx vite --host 0.0.0.0 --port <P> --strictPort.",
            },
            "preferredPort": {
                "type": "integer",
                "aliases": ["preferred_port"],
                "description": "Preferred project port. Must not be 4000/5173/4173.",
            },
            "cwd": {
                "type": "string",
                "description": "Working directory relative to workspace. Default: workspace root.",
            },
        },
        "required": [],
    },
    "lookup_dev_server": {
        "description": (
            "List this project's registered dev servers. Pass preferredPort "
            "to filter one port; omit to list all."
        ),
        "properties": {
            "preferredPort": {
                "type": "integer",
                "aliases": ["preferred_port", "port"],
            },
        },
        "required": [],
    },
    "message_user": {
        "description": (
            "Send a message to the human operator. Assistant text is not "
            "delivered — only this tool is. CEO终验 uses this."
        ),
        "properties": {
            "message": {"type": "string", "aliases": ["content", "body", "text"]},
            "priority": {"type": "string", "aliases": ["level"]},
        },
        "required": ["message"],
    },
    "request_code_audit": {
        "description": (
            "One-shot LLM audit of your worktree diff. Required before "
            "submit_task when cumulative code edits exceed 20 lines. "
            "Returns VERDICT PASS/ISSUES; ISSUES do not block submit. "
            "Runs on YOUR model (one extra LLM call)."
        ),
        "properties": {
            "taskId": {
                "type": "string",
                "aliases": ["task_id", "id"],
                "description": "Optional. Omit to audit the current running task / whole worktree.",
            },
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


async def _emit_tool_execute_after(
    agent_id: str,
    tool_name: str,
    tool_args: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """Fire TOOL_EXECUTE_AFTER lifecycle hooks — fail-open, never affects result.

    Emitted here (the single funnel both the registered pipeline and the
    legacy dispatch results flow through) because agent_id is in scope on
    both paths. Pre-execution failures (args/permission/ask) never emit —
    no tool actually ran.
    """
    try:
        from hiveweave.hooks import TOOL_EXECUTE_AFTER, hooks
        from hiveweave.hooks.handlers import register_builtin_handlers

        register_builtin_handlers()
        await hooks.run(
            TOOL_EXECUTE_AFTER,
            {
                "agent_id": agent_id,
                "tool_name": tool_name,
                "params": tool_args,
                "success": bool(result.get("success")),
                "output": str(result.get("output") or "")[:4000],
            },
            {},
        )
    except Exception as e:  # noqa: BLE001
        log.debug("tool_execute_after_hook_failed", error=str(e))


# ── ToolExecutor ───────────────────────────────────────────

class ToolExecutor:
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
            await _emit_tool_execute_after(
                agent_id, name, tool_args, registered_result
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
        deny_reason: str | None = None
        try:
            if hasattr(self.permission, "evaluate_detailed"):
                decision, deny_reason = await self.permission.evaluate_detailed(
                    agent_id, name, tool_args
                )
            else:
                decision = await self.permission.evaluate(
                    agent_id, name, tool_args
                )
        except Exception as exc:  # noqa: BLE001
            log.error("permission.evaluate_failed", error=str(exc))
            return self._error(f"Error: Permission check failed: {exc}")

        if decision == "deny":
            # 如实提示：硬门 / 用户 deny / 工具表 原因 + 角色指引
            from hiveweave.services.policy import infer_role_family
            from hiveweave.tools.pipeline import build_deny_hint

            agent_info = await meta_db.get_agent_by_id(agent_id)
            family = infer_role_family(agent_info or {})
            deny_result = self._error(build_deny_hint(name, family, deny_reason))
            # H3: 权限拒绝是平台护栏，不是模型空转 —— 标记 blocked 供 stall 分流
            deny_result["blocked"] = True
            return deny_result

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
                # H3: 平台拒绝（审批超时）≠ 模型空转 —— 与注册路径（pipeline）
                # 对齐标记 blocked 供 stall 分流。
                deny_result = self._error(
                    "Permission request timed out (120s). "
                    "The user may be away."
                )
                deny_result["blocked"] = True
                return deny_result
            except PermissionRejected as exc:
                deny_result = self._error(f"Permission rejected: {exc}")
                deny_result["blocked"] = True
                return deny_result
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

        await _emit_tool_execute_after(agent_id, name, tool_args, result)

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
          - preview: head/tail with per-line + total char dual-cap
            (truncation is last-resort; tools must shrink upstream)
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

        from hiveweave.conversation.token_utils import build_tool_output_preview

        return build_tool_output_preview(output, file_path)

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
