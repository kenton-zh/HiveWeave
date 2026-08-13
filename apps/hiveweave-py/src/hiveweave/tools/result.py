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
    ``blocked`` marks platform-guard refusals (permission / sandbox /
    security rules): the platform refused to execute — not a model
    mistake — so stall detection must not treat it as model spinning (H3).
    """

    success: bool
    output: str = ""
    error: str | None = None
    blocked: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    # ── constructors ─────────────────────────────────────

    @classmethod
    def ok(cls, output: str = "", **extra: Any) -> "ToolResult":
        """Build a success result with optional structured fields."""
        return cls(success=True, output=output, error=None, extra=extra)

    @classmethod
    def err(cls, message: str, **extra: Any) -> "ToolResult":
        """Build an error result. ``success`` is always ``False``.

        Optional structured fields (e.g. ``gates``, ``actions``) go in
        ``extra`` and are merged into the dict by :meth:`to_dict` — they
        are observable for telemetry/future consumers (TEST19 ④).
        """
        return cls(success=False, output="", error=message, extra=extra)

    @classmethod
    def blocked_err(cls, message: str, **extra: Any) -> "ToolResult":
        """Build a platform-guard refusal result (permission/sandbox/security).

        ``success`` is always ``False`` and text semantics match :meth:`err`;
        ``blocked=True`` tells stall detection this was a platform refusal,
        not a model mistake (H3). Named ``blocked_err`` (not ``blocked``)
        to avoid colliding with the ``blocked`` dataclass field.
        """
        return cls(
            success=False, output="", error=message, extra=extra, blocked=True
        )

    # ── serialization ────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Convert to the legacy dict format expected by the rest of the system."""
        d: dict[str, Any] = {
            "success": self.success,
            "output": self.output,
            "error": self.error,
        }
        d.update(self.extra)
        # extra 不得覆盖 blocked（平台护栏标记不是工具自由字段）
        d["blocked"] = self.blocked
        return d

    def __repr__(self) -> str:
        if self.success:
            return f"ToolResult(ok, output={self.output[:60]!r}...)"
        if self.blocked:
            return f"ToolResult(blocked, error={self.error!r})"
        return f"ToolResult(err, error={self.error!r})"
