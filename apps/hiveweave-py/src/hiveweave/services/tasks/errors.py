"""Task service errors."""
from __future__ import annotations

class MergeRequiredError(ValueError):
    """Close blocked: code task still has unmerged / undelivered worktree output.

    Callers (approve auto-close, VERIFY parent close) must surface this as a
    hard gate — never swallow and close anyway (TEST20 P0-A / N1).
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        task_id: str | None = None,
        commits_ahead: int | None = None,
        dirty_count: int = 0,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.task_id = task_id
        self.commits_ahead = commits_ahead
        self.dirty_count = dirty_count

