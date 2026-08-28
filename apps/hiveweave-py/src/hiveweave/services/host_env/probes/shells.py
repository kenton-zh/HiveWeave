"""shell.* 探测项 — 真跑一次命令，不看文件存在（DSH：spawnSync + status===0）。

- ``shell.pwsh``：PowerShell 7+。版本 ≥7.6 → full（翻译层建议按 7.6 实测
  写的，计划 T3.2 前置）；跑得动但版本 <7.6 或读不出 → partial；跑不动 →
  unavailable。
- ``shell.git_bash``：Git for Windows 的 MSYS bash。``bash --version`` 且
  输出含 ``msys`` → full；有 bash 但不是 MSYS（WSL/Cygwin，方言完全不同，
  不能当 Git Bash 用）→ unavailable；Linux 上的本命 bash 不适用本探测 →
  unavailable（是「不适用」，不是「不知道」）。
"""
from __future__ import annotations

import shutil

from ..runner import run_command
from ..types import CapabilityLevel, CapabilityUnavailableError, ProbeResult

#: 翻译层建议按此版本实测（plan T3.2：版本 ≥7.6 给 full）。
PWSH_MIN_FULL_VERSION = (7, 6)


def _parse_version_tuple(text: str) -> tuple[int, ...] | None:
    parts: list[int] = []
    for chunk in text.strip().split("."):
        if not chunk.isdigit():
            return None
        parts.append(int(chunk))
    return tuple(parts) if parts else None


def probe_shell_pwsh(*, timeout_s: float = 5.0, **_) -> ProbeResult:
    """``shell.pwsh`` — 真跑 ``pwsh -NoProfile`` 取版本号。"""
    proc = run_command(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command",
         "$PSVersionTable.PSVersion.ToString()"],
        timeout_s=timeout_s,
    )
    version_raw = (proc.stdout or "").strip()
    if proc.returncode != 0:
        raise CapabilityUnavailableError(
            f"pwsh ran but exited {proc.returncode}: "
            f"{(proc.stderr or proc.stdout or '').strip()[:200]}",
            probe="shell.pwsh",
            reason="pwsh-exit-nonzero",
        )
    version = _parse_version_tuple(version_raw)
    path = shutil.which("pwsh") or ""
    data = {"version": version_raw, "path": path}
    if version is None:
        # 跑得动但版本读不出 —— 能用，但「≥7.6」无从证明 → partial（保守）。
        return ProbeResult(
            name="shell.pwsh",
            level=CapabilityLevel.PARTIAL,
            detail=f"pwsh runnable but version unreadable: {version_raw!r}",
            data=data,
        )
    if version >= PWSH_MIN_FULL_VERSION:
        return ProbeResult(
            name="shell.pwsh",
            level=CapabilityLevel.FULL,
            detail=f"pwsh {version_raw} >= {'.'.join(map(str, PWSH_MIN_FULL_VERSION))}",
            data=data,
        )
    return ProbeResult(
        name="shell.pwsh",
        level=CapabilityLevel.PARTIAL,
        detail=f"pwsh {version_raw} < {'.'.join(map(str, PWSH_MIN_FULL_VERSION))} "
               "(translation-layer hints were measured on 7.6)",
        data=data,
    )


def probe_shell_git_bash(*, timeout_s: float = 5.0, **_) -> ProbeResult:
    """``shell.git_bash`` — ``bash --version`` 且输出带 MSYS 标记才算 Git Bash。"""
    proc = run_command(["bash", "--version"], timeout_s=timeout_s)
    first_line = (proc.stdout or "").strip().splitlines()
    version_text = first_line[0] if first_line else ""
    if proc.returncode != 0:
        raise CapabilityUnavailableError(
            f"bash --version exited {proc.returncode}",
            probe="shell.git_bash",
            reason="bash-exit-nonzero",
        )
    if "msys" not in (proc.stdout or "").lower():
        # WSL/Cygwin/Linux bash —— 存在但不是 Git Bash，方言契约不成立。
        raise CapabilityUnavailableError(
            f"bash found but not Git Bash (no msys tag): {version_text[:120]}",
            probe="shell.git_bash",
            reason="not-msys-bash",
        )
    return ProbeResult(
        name="shell.git_bash",
        level=CapabilityLevel.FULL,
        detail=version_text[:160],
        data={"version": version_text, "path": shutil.which("bash") or ""},
    )
