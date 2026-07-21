"""Built-in lifecycle hook handlers (platform priority 0–49)."""

from __future__ import annotations

_registered = False


def register_builtin_handlers() -> None:
    """Idempotent — safe to call from app lifespan."""
    global _registered
    if _registered:
        return
    from hiveweave.hooks.handlers import task_advance as _task_advance

    _task_advance.register()
    _registered = True
