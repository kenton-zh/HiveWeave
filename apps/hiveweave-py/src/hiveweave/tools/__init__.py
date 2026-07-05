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
    get_pending_questions,
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
from hiveweave.tools.todowrite import execute_todowrite, get_todos
from hiveweave.tools.websearch import execute_websearch

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
    "get_pending_questions",
    # TodoWrite
    "execute_todowrite",
    "get_todos",
]
