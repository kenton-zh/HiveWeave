"""toolchain.* 探测项（LAZY — 只影响诊断展示，首访真跑并缓存）。"""
from __future__ import annotations

import shutil

from ..runner import run_command
from ..types import CapabilityLevel, CapabilityUnavailableError, ProbeResult


def _version_probe(
    name: str, argv: list[str], *, timeout_s: float
) -> ProbeResult:
    """git/node/npm 共用的版本探测模板：exit 0 → full，带版本号。"""
    proc = run_command(argv, timeout_s=timeout_s)
    version = (proc.stdout or "").strip().splitlines()
    version_text = version[0] if version else ""
    if proc.returncode != 0:
        raise CapabilityUnavailableError(
            f"{argv[0]} --version exited {proc.returncode}: "
            f"{(proc.stderr or '').strip()[:200]}",
            probe=name,
            reason="tool-exit-nonzero",
        )
    return ProbeResult(
        name=name,
        level=CapabilityLevel.FULL,
        detail=version_text,
        data={"version": version_text, "path": shutil.which(argv[0]) or ""},
    )


def probe_toolchain_git(*, timeout_s: float = 5.0, **_) -> ProbeResult:
    return _version_probe("toolchain.git", ["git", "--version"], timeout_s=timeout_s)


def probe_toolchain_node(*, timeout_s: float = 5.0, **_) -> ProbeResult:
    return _version_probe("toolchain.node", ["node", "--version"], timeout_s=timeout_s)


def probe_toolchain_npm(*, timeout_s: float = 5.0, **_) -> ProbeResult:
    return _version_probe("toolchain.npm", ["npm", "--version"], timeout_s=timeout_s)
