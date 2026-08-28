"""Task ledger package — public API + patch-compatible re-exports.

Behavior-preserving mechanical split of the former monolithic
``task.py``. External callers keep using the shim::

    from hiveweave.services.task import TaskService
    patch("hiveweave.services.task._query", ...)

This package module also supports direct imports and propagates
setattr on patched symbols into consumer submodule globals.
"""
from __future__ import annotations

import sys
import types

from .constants import _MISSING_COLUMNS, _PROGRESS_FLOORS, _TRANSITIONS
from .db import (
    _conn,
    _ensure_schema,
    _execute,
    _execute_tx,
    _migrated,
    _query,
)
from .errors import MergeRequiredError
from .events import TaskEventService
from .policy import (
    format_submit_expectations,
    resolve_task_policy,
    submit_expectations,
)
from .service import TaskService
from .verify import VerificationCaseService

_PATCH_CONSUMERS = (
    "hiveweave.services.tasks.db",
    "hiveweave.services.tasks.progress",
    "hiveweave.services.tasks.transitions",
    "hiveweave.services.tasks.crud",
    "hiveweave.services.tasks.claim",
    "hiveweave.services.tasks.lifecycle",
    "hiveweave.services.tasks.submit",
    "hiveweave.services.tasks.review",
    "hiveweave.services.tasks.close",
    "hiveweave.services.tasks.verify",
    "hiveweave.services.tasks.obligations",
    "hiveweave.services.tasks.events",
    "hiveweave.services.tasks.service",
    # Shim module — keep in sync when package attrs are patched
    "hiveweave.services.task",
)

_PATCH_NAMES = frozenset({
    "_conn",
    "_query",
    "_execute",
    "_execute_tx",
    "_ensure_schema",
    "_migrated",
    "_TRANSITIONS",
    "_PROGRESS_FLOORS",
    "_MISSING_COLUMNS",
    "MergeRequiredError",
    "resolve_task_policy",
    "submit_expectations",
    "format_submit_expectations",
    "TaskService",
    "TaskEventService",
    "VerificationCaseService",
})


class _TasksPackage(types.ModuleType):
    """Propagate setattr on patched symbols into consumer globals."""

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        if name not in _PATCH_NAMES:
            return
        for modname in _PATCH_CONSUMERS:
            mod = sys.modules.get(modname)
            if mod is not None and name in mod.__dict__:
                object.__setattr__(mod, name, value)


_mod = sys.modules[__name__]
_mod.__class__ = _TasksPackage

__all__ = [
    "TaskService",
    "TaskEventService",
    "VerificationCaseService",
    "MergeRequiredError",
    "resolve_task_policy",
    "_conn",
    "_query",
    "_execute",
    "_execute_tx",
    "_ensure_schema",
    "_migrated",
    "_TRANSITIONS",
    "_PROGRESS_FLOORS",
    "_MISSING_COLUMNS",
]
