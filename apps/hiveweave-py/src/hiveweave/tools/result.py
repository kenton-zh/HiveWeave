"""Unified tool result type.

Replaces ad-hoc dict returns with a dataclass that enforces the
{success, output, error} contract at construction time.

Usage:
    return ToolResult.ok("File written")
    return ToolResult.ok("Task created", task_id=task_id)
    return ToolResult.err("File not found")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolResult:
    """Unified tool return value.

    ``success``, ``output``, and ``error`` are always present.
    Extra structured fields (e.g. ``task_id``, ``alarm_id``) go in
    ``extra`` and are merged into the dict by :meth:`to_dict`.
    """

    success: bool
    output: str = ""
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    # ── constructors ─────────────────────────────────────

    @classmethod
    def ok(cls, output: str = "", **extra: Any) -> "ToolResult":
        """Build a success result with optional structured fields."""
        return cls(success=True, output=output, error=None, extra=extra)

    @classmethod
    def err(cls, message: str) -> "ToolResult":
        """Build an error result. ``success`` is always ``False``."""
        return cls(success=False, output="", error=message)

    # ── serialization ────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Convert to the legacy dict format expected by the rest of the system."""
        d: dict[str, Any] = {
            "success": self.success,
            "output": self.output,
            "error": self.error,
        }
        d.update(self.extra)
        return d

    def __repr__(self) -> str:
        if self.success:
            return f"ToolResult(ok, output={self.output[:60]!r}...)"
        return f"ToolResult(err, error={self.error!r})"
