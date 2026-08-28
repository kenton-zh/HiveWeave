"""platform.* 探测项 — 纯内省，无子进程，永不超时。"""
from __future__ import annotations

import platform as _platform

from ..types import CapabilityLevel, CapabilityUnavailableError, ProbeResult

_LEVEL_FULL = CapabilityLevel.FULL


def probe_platform_os(**_) -> ProbeResult:
    """``platform.os`` — 操作系统族（Windows/Linux/Darwin）。"""
    system = _platform.system()
    if not system:
        raise CapabilityUnavailableError(
            "platform.system() returned an empty string",
            probe="platform.os",
            reason="introspection-failed",
        )
    return ProbeResult(
        name="platform.os",
        level=_LEVEL_FULL,
        detail=system,
        data={
            "system": system,
            "release": _platform.release(),
            "version": _platform.version(),
        },
    )


def probe_platform_arch(**_) -> ProbeResult:
    """``platform.arch`` — CPU 架构（AMD64/ARM64/x86_64/…）。"""
    machine = _platform.machine()
    if not machine:
        raise CapabilityUnavailableError(
            "platform.machine() returned an empty string",
            probe="platform.arch",
            reason="introspection-failed",
        )
    return ProbeResult(
        name="platform.arch",
        level=_LEVEL_FULL,
        detail=machine,
        data={"machine": machine, "python_bitness": _platform.architecture()[0]},
    )
