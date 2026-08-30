"""Git subprocess helpers."""
from __future__ import annotations

import asyncio

from .constants import GIT_TIMEOUT

async def _git(args: list[str], cwd: str, timeout: float = GIT_TIMEOUT) -> tuple[bool, str]:
    """Run a git command, return (success, output).

    stderr merged into stdout (mirrors Elixir stderr_to_stdout: true).
    """
    try:
        from hiveweave.util.win_subprocess import windows_no_window_kwargs

        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **windows_no_window_kwargs(),
        )
    except FileNotFoundError:
        return False, "git not found on PATH"

    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        cmd_preview = " ".join(args[:2])
        return False, f"git {cmd_preview} timed out after {timeout}s"

    output = stdout.decode("utf-8", errors="replace").strip() if stdout else ""
    if proc.returncode == 0:
        return True, output
    return False, output

async def _current_branch(worktree_path: str) -> str | None:
    """worktree 实际检出的分支 (``git -C <path> rev-parse --abbrev-ref HEAD``)。

    幂等/解析的唯一事实来源: 路径还在, 就以检出分支为准, 不按入参
    重算 (重算名与检出分支可能脱钩)。detached HEAD 返回 None。
    """
    ok, out = await _git(["rev-parse", "--abbrev-ref", "HEAD"], worktree_path)
    if ok and out and out.strip() != "HEAD":
        return out.strip()
    return None

async def _resolve_base_branch(workspace_path: str) -> str | None:
    """merged 判定的基准分支: main → master 二级回退。"""
    for name in ("main", "master"):
        ok, _ = await _git(
            ["rev-parse", "--verify", f"refs/heads/{name}"], workspace_path
        )
        if ok:
            return name
    return None


async def _target_tip_short(workspace_path: str, target_branch: str) -> str | None:
    """目标分支当前 tip 的短 hash（F13b 幂等重入回执用）。best-effort。"""
    ok, out = await _git(
        ["rev-parse", "--short", target_branch], workspace_path
    )
    if ok and out:
        return out.strip()[:12]
    return None
