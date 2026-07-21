"""Tool executor layer (contract 02).

Exports:
    ToolExecutor — main dispatcher with permission gating + output truncation
    ToolResult — result dict type
    Tool functions for direct invocation (used by ToolExecutor internally):
        execute_bash, execute_run_command
        read_file, write_file, list_files
        apply_patch
        execute_grep
        execute_websearch
        execute_review (run_code_review / run_security_audit /
                        run_test_review / run_perf_audit / run_full_review)
        execute_question
        execute_todowrite
"""

from hiveweave.tools.bash import (
    execute_bash,
    execute_run_command,
    check_self_destructive,
)
from hiveweave.tools.executor import ToolExecutor, ToolResult
from hiveweave.tools.file import read_file, write_file, list_files
from hiveweave.tools.grep import execute_grep
from hiveweave.tools.patch import apply_patch
from hiveweave.tools.question import (
    execute_question,
    resolve_question,
)
from hiveweave.tools.review import (
    execute_review,
    run_code_review,
    run_security_audit,
    run_test_review,
    run_perf_audit,
    run_full_review,
    ReviewLLMCallback,
)
from hiveweave.tools.todowrite import execute_todowrite
from hiveweave.tools.websearch import execute_websearch

# Import tool registration modules to trigger @tool decorators
import hiveweave.tools.file_mgmt  # noqa: F401 — registers delete_file, move_file, etc.
import hiveweave.tools.orchestration_tools  # noqa: F401 — registers messaging, charter, memory, alarm
import hiveweave.tools.org_tools  # noqa: F401 — registers hire_agent, dismiss_agent, skills, etc.
import hiveweave.tools.task_tools  # noqa: F401 — registers dispatch/submit/waive/cancel/…
import hiveweave.tools.misc_tools  # noqa: F401 — registers git_worktree, legacy tasks, webfetch, etc.
import hiveweave.tools.turn_tools  # noqa: F401 — commit_turn, defer_task_advance, ask/notify
import hiveweave.tools.dev_server_tools  # noqa: F401 — start_dev_server / lookup_dev_server
import hiveweave.tools.browse_tools  # noqa: F401 — browse (gstack Chromium CLI)
from hiveweave.tools.base import _TOOL_REGISTRY, list_tool_names  # noqa: F401
from hiveweave.tools.result import ToolResult as ToolResultDataclass  # noqa: F401
from hiveweave.tools.pipeline import ToolContext  # noqa: F401

__all__ = [
    # Executor
    "ToolExecutor",
    "ToolResult",
    # Bash
    "execute_bash",
    "execute_run_command",
    "check_self_destructive",
    # File
    "read_file",
    "write_file",
    "list_files",
    # Patch
    "apply_patch",
    # Grep
    "execute_grep",
    # Websearch
    "execute_websearch",
    # Review
    "execute_review",
    "run_code_review",
    "run_security_audit",
    "run_test_review",
    "run_perf_audit",
    "run_full_review",
    "ReviewLLMCallback",
    # Question
    "execute_question",
    "resolve_question",
    # TodoWrite
    "execute_todowrite",
]
