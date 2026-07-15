"""TurnResult — mandatory return value of every agent turn (function ABI).

Every agent turn is treated like a function call: it MUST return a TurnResult
before the runtime allows idle. Control-plane ``phase`` stays small and stable;
data-plane ``result`` / ``extensions`` grow over time.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

TURN_RESULT_SCHEMA_VERSION = 1

TurnPhase = Literal["in_progress", "waiting", "blocked", "done_slice"]

VALID_PHASES: frozenset[str] = frozenset(
    {"in_progress", "waiting", "blocked", "done_slice"}
)

WaitingKind = Literal["agent", "task", "user", "timer", "external"]


class WaitingOnItem(BaseModel):
    """One machine-readable wait target."""

    kind: WaitingKind
    ref: str = Field(
        description="Agent 花名/short_id, task id, 'user', alarm id, or opaque ref"
    )
    note: str | None = Field(default=None, description="Optional short note")


class TurnResult(BaseModel):
    """Structured return value committed at end of turn."""

    schema_version: int = Field(default=TURN_RESULT_SCHEMA_VERSION)
    phase: TurnPhase
    summary: str = Field(min_length=1, description="1-2 sentences: what this turn did")
    waiting_on: list[WaitingOnItem] = Field(default_factory=list)
    result: dict[str, Any] = Field(
        default_factory=dict,
        description="Data plane: replies, tasks, artifacts, decisions, …",
    )
    extensions: dict[str, Any] = Field(
        default_factory=dict,
        description="Forward-compatible extension slot",
    )

    @field_validator("summary")
    @classmethod
    def _strip_summary(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("summary must be non-empty")
        return s

    def to_persist_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def parse_turn_result(raw: dict[str, Any] | None) -> TurnResult:
    """Parse/validate a TurnResult from tool args or stored JSON."""
    if not raw or not isinstance(raw, dict):
        raise ValueError("TurnResult payload required")
    data = dict(raw)
    # Accept waitingOn camelCase
    if "waiting_on" not in data and "waitingOn" in data:
        data["waiting_on"] = data.pop("waitingOn")
    if "schema_version" not in data and "schemaVersion" in data:
        data["schema_version"] = data.pop("schemaVersion")
    return TurnResult.model_validate(data)


def validate_phase_fields(tr: TurnResult) -> list[str]:
    """Return violation codes for phase/field consistency (no I/O)."""
    violations: list[str] = []
    if tr.phase in ("waiting", "blocked") and not tr.waiting_on:
        violations.append(
            "WAITING_ON_REQUIRED"
            if tr.phase == "waiting"
            else "BLOCKED_WAITING_ON_REQUIRED"
        )
    return violations
