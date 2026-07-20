"""Lifecycle hooks — OpenCode-style (input, output) mutation chains.

See docs/spec/lifecycle-hooks.md and docs/adr/005-lifecycle-hooks.md.
Distinct from realtime StatusEventBus (UI fan-out).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from dataclasses import dataclass, field
from typing import Any, Literal

import structlog

log = structlog.get_logger(__name__)

FailPolicy = Literal["open", "closed"]

HookHandler = Callable[
    [Mapping[str, Any], MutableMapping[str, Any]],
    Awaitable[None],
]


class HookClosedError(Exception):
    """Raised when a fail=closed handler aborts the hook chain.

    Callers must not treat this as fail-open enrichment noise.
    """

    def __init__(
        self,
        message: str,
        *,
        point: str,
        handler: str,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.point = point
        self.handler = handler
        self.cause = cause


@dataclass(order=True)
class _Registration:
    priority: int
    seq: int
    name: str = field(compare=False)
    handler: HookHandler = field(compare=False)
    fail: FailPolicy = field(compare=False, default="open")
    timeout_s: float | None = field(compare=False, default=None)


class HookRegistry:
    """Process-local sequential hook runner."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[_Registration]] = {}
        self._seq = 0

    def register(
        self,
        point: str,
        handler: HookHandler,
        *,
        priority: int = 100,
        fail: FailPolicy = "open",
        timeout_s: float | None = None,
        name: str | None = None,
    ) -> Callable[[], None]:
        """Register a handler. Returns an unregister callback."""
        point = (point or "").strip()
        if not point:
            raise ValueError("hook point must be non-empty")
        self._seq += 1
        reg = _Registration(
            priority=priority,
            seq=self._seq,
            name=name or getattr(handler, "__name__", "handler"),
            handler=handler,
            fail=fail,
            timeout_s=timeout_s,
        )
        bucket = self._handlers.setdefault(point, [])
        bucket.append(reg)
        bucket.sort()
        log.debug(
            "hook_registered",
            point=point,
            name=reg.name,
            priority=priority,
            fail=fail,
        )

        def _unregister() -> None:
            lst = self._handlers.get(point) or []
            self._handlers[point] = [r for r in lst if r.seq != reg.seq]

        return _unregister

    def on(
        self,
        point: str,
        *,
        priority: int = 100,
        fail: FailPolicy = "open",
        timeout_s: float | None = None,
        name: str | None = None,
    ) -> Callable[[HookHandler], HookHandler]:
        """Decorator form of register."""

        def deco(fn: HookHandler) -> HookHandler:
            self.register(
                point,
                fn,
                priority=priority,
                fail=fail,
                timeout_s=timeout_s,
                name=name or fn.__name__,
            )
            return fn

        return deco

    def clear(self, point: str | None = None) -> None:
        """Clear handlers (tests)."""
        if point is None:
            self._handlers.clear()
        else:
            self._handlers.pop(point, None)

    def list_handlers(self, point: str) -> list[str]:
        return [r.name for r in self._handlers.get(point, [])]

    async def run(
        self,
        point: str,
        input: Mapping[str, Any],
        output: MutableMapping[str, Any],
    ) -> MutableMapping[str, Any]:
        """Run all handlers for ``point`` in priority order.

        Mutates ``output`` in place and returns it.
        """
        regs = list(self._handlers.get(point) or [])
        if not regs:
            return output
        t0 = time.monotonic()
        for reg in regs:
            try:
                if reg.timeout_s is not None and reg.timeout_s > 0:
                    await asyncio.wait_for(
                        reg.handler(input, output),
                        timeout=reg.timeout_s,
                    )
                else:
                    await reg.handler(input, output)
            except Exception as e:
                if reg.fail == "closed":
                    log.error(
                        "hook_failed_closed",
                        point=point,
                        name=reg.name,
                        error=str(e),
                    )
                    raise HookClosedError(
                        f"hook {point}/{reg.name} aborted: {e}",
                        point=point,
                        handler=reg.name,
                        cause=e,
                    ) from e
                log.warning(
                    "hook_failed_open",
                    point=point,
                    name=reg.name,
                    error=str(e),
                )
        log.debug(
            "hook_run_done",
            point=point,
            handlers=len(regs),
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
        return output


# Process singleton — import as ``from hiveweave.hooks import hooks``
hooks = HookRegistry()
