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

def _git_sync(args: list[str], cwd: str, timeout: float = GIT_TIMEOUT,
              project_root: str | None = None) -> tuple[bool, str]:
    """`_git` 的**同步**孪生体 —— 锚的接线只此一份，两个入口共用（#19）。

    为什么必须有 sync 面：`services/dispatch_facts.py` 那条"派单时采集 git 快照"
    的调用链**整条是同步的**（`collect_and_format` → `_main_git_facts`），把它改成
    async 会牵动派单路径。以前它自带一个 `_git`（裸 `hidden_run(["git", ...])`）
    ⇒ **未接信任锚**：平台 git 会去读 agent 可写的 `<wt>/.git` 与 `commondir`。

    为什么放在这里而不是各调用点自己写：锚的派生（`anchor_for_git`）与
    「拒因 ⇒ 不跑」的 fail-closed 纪律必须**只有一份**；本函数与 `_git` 的差别
    仅在 spawn 原语（`hidden_run` vs `hidden_exec`），注释写明以免日后各自演化。

    与 `_git` 同语义：**stderr 合并进 stdout**（必须显式给 `stdout`/`stderr`
    两个 PIPE 常量 —— `hidden_run` 内部默认把两者分开；
    审计实测：不合并时同一次失败 `_git` 给 `fatal: not a git repository…`
    而本函数给空串 ⇒ **失败原因全丢**，正是本仓定义的一等缺陷）。
    锚拒绝 ⇒ 返回 ``(False, 拒因)``；`AnchorUnderivable`（派生不出、无篡改证据）
    ⇒ 按 `git_anchor` 的第三档**不钉锚继续跑**（不是拒跑 —— 这一点原来写错了，
    审计 D4 订正）。
    """
    anchor, refusal = anchor_for_git(cwd, project_root)
    if refusal is not None:
        return False, refusal
    kwargs: dict = {}
    if anchor is not None:
        args = [*anchor.args, *args]
        kwargs["env"] = {**os.environ, **anchor.env}
    try:
        from hiveweave.util.win_subprocess import (
            PIPE,
            STDOUT,
            TimeoutExpired,
            hidden_run,
        )

        r = hidden_run(
            ["git", *args],
            cwd=cwd,
            # ⚠ stderr 必须并进 stdout（与 `_git` 的 `stderr=STDOUT` 对齐）：
            # 不合并时"失败原因"整段丢失 —— 而本函数是 `dispatch_facts` 的**唯一**
            # 入口，那些失败会静默变成空串（审计 §1 实测）。
            # ⚠ **不能用 `capture_output=True`**：它与 `stderr=STDOUT` 互斥，
            # `subprocess` 会直接抛 "stdout and stderr arguments may not be used
            # with capture_output" ⇒ 本函数**100% 失败**（定向回归实测抓到，
            # 比审计发现的那条更重）。故显式给两个 PIPE 常量。
            stdout=PIPE,
            stderr=STDOUT,
            timeout=timeout,
            **kwargs,
        )
    except FileNotFoundError:
        return False, "git not found on PATH"
    except TimeoutExpired:
        # 与 `_git` 对齐的文案（`_git` 那条由 `asyncio.wait_for` 分支给）
        return False, f"git {' '.join(args[:2])} timed out after {timeout}s"
    except Exception as exc:  # noqa: BLE001 — 见下：失败不抛，但回执必须带原因
        # 与 `_git` 的差别（如实登记）：`_git` 只接 `FileNotFoundError`（超时另分支），
        # 本函数是同步原语 ⇒ 超时与其它异常都在这里收口（超时已单列）。
        return False, f"git failed: {exc}"
    output = (r.stdout or b"").decode("utf-8", errors="replace").strip()
    return (r.returncode == 0), output


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


async def merge_in_progress(
    worktree_path: str, project_root: str | None = None
) -> bool:
    """该工作树是否处于**半合并态**（merge 起了但没结束）。

    判据 = **git 自己的状态** ``MERGE_HEAD`` 在不在（`git merge` 产生冲突时写下它，
    ``--abort`` 与成功收尾都会清掉它），**不是文案**、不是"index 里有 UU"那种
    近似 —— 后者在 `add` 过之后会消失。

    为什么需要这个判据（2026-09-16，③）：
    平台原本**从不制造**半合并态 —— 两个方向（`git_worktree_merge` 进 MAIN、
    `git_worktree_sync` 进 worktree）失败即 `merge --abort`。新增
    ``mode=materialize_conflict`` 之后，半合并态第一次成为**合法、可达、且由
    agent 主动要求**的状态（把冲突就地留给自己手工解）⇒ 所有"消费这棵树"的
    地方都必须能识别它。最危险的一处是 checkpoint 的 `add -A + commit`：
    它会把冲突标记当正常改动**提交成一次正常提交**（`git add` 一条未解决路径
    即视为已解决），于是半成品被当成已完成的代码。
    """
    ok, out = await _git(
        ["rev-parse", "--verify", "--quiet", "MERGE_HEAD"],
        worktree_path, project_root=project_root,
    )
    # ⚠ 判据要求**正面证据**（解析出 sha），不是只看退出码：`--verify` 成功时
    # 一定吐 sha；而"退出码 0 + 空输出"只可能来自**打桩的假 git**（全量回归实测：
    # `test_worktree_relocate_binding` 的 `fake_git` 对任何命令都 `return True, ""`
    # ⇒ 只看退出码会把正常树误判成半合并态、checkpoint 被误拒）。
    # 真实 git 不存在 MERGE_HEAD 时是 rc≠0 ⇒ 两种形态都判对。
    return bool(ok and (out or "").strip())


async def unmerged_paths(
    worktree_path: str, project_root: str | None = None
) -> list[str]:
    """未解决（unmerged）路径清单 —— 半合并态下给 agent/回执的**可执行事实**。

    ⚠ 必须在任何 ``merge --abort`` **之前**读（abort 之后 index 就干净了，
    这让"内容冲突"会被误报成"非内容冲突的 merge 失败"，见 service_sync 的注释）。
    """
    ok, out = await _git(
        ["diff", "--name-only", "--diff-filter=U"],
        worktree_path, project_root=project_root,
    )
    if not ok:
        return []
    return [
        f.strip().replace("\\", "/")
        for f in (out or "").splitlines() if f.strip()
    ]


async def _target_tip_short(workspace_path: str, target_branch: str) -> str | None:
    """目标分支当前 tip 的短 hash（F13b 幂等重入回执用）。best-effort。"""
    ok, out = await _git(
        ["rev-parse", "--short", target_branch], workspace_path
    )
    if ok and out:
        return out.strip()[:12]
    return None
