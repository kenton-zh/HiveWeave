"""Git subprocess helpers."""
from __future__ import annotations

import asyncio
import os

from .constants import GIT_TIMEOUT
from .git_anchor import anchor_for_git


async def _git(args: list[str], cwd: str, timeout: float = GIT_TIMEOUT,
              project_root: str | None = None) -> tuple[bool, str]:
    """Run a git command, return (success, output).

    stderr merged into stdout (mirrors Elixir stderr_to_stdout: true).

    **信任锚（2026-09-15）**：gitdir / common dir 由平台**派生并钉住**，不让 git 去读
    agent 可写的 `<wt>/.git` 指针与 `commondir` —— 否则 agent 可让平台的 git 读它写好的
    config，从而执行动态键名驱动（`filter.<n>.clean` 等，`GIT_CONFIG_*` 覆盖不到）。
    实测见 `git_anchor.py` 模块 docstring；派生失败 ⇒ 拒绝执行（不静默回落）。

    `project_root`：**cwd 是 worktree 时请务必传**（调用方手里通常就有
    `workspace_path`）—— 那是与布局无关的可靠派生源；不传则退化为结构上溯，
    上溯不到（如 worktree 与主仓是兄弟目录）会**拒绝执行**（fail-closed）。
    """
    anchor, refusal = anchor_for_git(cwd, project_root)
    if refusal is not None:
        return False, refusal
    kwargs: dict = {}
    if anchor is not None:
        args = [*anchor.args, *args]
        # env 走漏斗：`hidden_exec` 会把 GIT_CONFIG_* 加固**追加**进同一个 env。
        kwargs["env"] = {**os.environ, **anchor.env}
    try:
        from hiveweave.util.win_subprocess import hidden_exec

        proc = await hidden_exec(
            "git",
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **kwargs,
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
