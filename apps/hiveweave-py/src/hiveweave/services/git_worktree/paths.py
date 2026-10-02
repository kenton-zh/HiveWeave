"""Worktree path helpers."""
from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path

import structlog

from .constants import WORKTREE_DIR, _RELOCATION_SUFFIXES

log = structlog.get_logger(__name__)

# F13（平台修复计划 2026-08-30）：嵌套假路径清扫 ——
# ``…\worktrees\A298\.hiveweave\worktrees\A299\…`` 这类「已在 worktree
# 内却再次相对拼接 .hiveweave/worktrees/*」的路径（r4：A298→A299 双拼）。
_WORKTREE_SEG_RE = re.compile(
    re.escape(WORKTREE_DIR.replace("/", "\\/")) + r"[\\/]+", re.IGNORECASE
)


def normalize_worktree_root(path: str) -> str:
    """把已处于 worktree 内的路径归一化到**项目根**的 worktree 字典。

    输入如 ``<proj>/.hiveweave/worktrees/A298/.hiveweave/worktrees/A299``：
    取第一个 ``.hiveweave/worktrees/`` 段之前的项目根，清掉后续所有
    嵌套段，得到 ``<proj>/.hiveweave/worktrees/A299``。禁止在已位于
    worktree 内的 cwd 上再次相对拼接（r4 根因）。
    """
    if not path:
        return path
    p = str(path)
    if "." not in p.lower():  # 极快路径：无 .hiveweave 段则原样
        return p
    parts = p.replace("/", "\\").split("\\")
    # 找到第一个 `.hiveweave\\worktrees` 段：从项目根重建
    for i, part in enumerate(parts):
        if part.lower() == ".hiveweave" and i + 1 < len(parts):
            if parts[i + 1].lower() == "worktrees":
                joined = "\\".join(parts[: i + 2])
                # 取后续第一个「非 .hiveweave 段」作为 worktree 名起点
                # （A298\.hiveweave\worktrees\A299 → 保留 A299）
                tail = parts[i + 2:]
                kept = [x for x in tail if x and x.lower() != ".hiveweave"][:1]
                if kept:
                    return Path(joined) / kept[0]
                return Path(joined)
    return p


def _worktree_path(workspace_path: str, short_id: str) -> str:
    # F13：先归一化 —— 若 workspace_path 已在 worktree 内（嵌套拼接的
    # 双份 .hiveweave/worktrees），剥回项目根再拼接，杜绝 A298→A299 假路径。
    root = normalize_worktree_root(workspace_path)
    return str(Path(root) / WORKTREE_DIR / short_id)


def _is_bound_worktree_basename(basename: str, short_id: str) -> bool:
    """True if dirname is this agent's canonical tree or an explicit -b/-c/-d relocate.

    Exact short_id OR ``short_id + '-b'|'-c'|'-d'`` only — never substring
    matching (that revived the A003-b-masquerades-as-A003 split).
    """
    if not basename or not short_id:
        return False
    if basename == short_id:
        return True
    return basename in {f"{short_id}{s}" for s in _RELOCATION_SUFFIXES}


def _worktree_binding_under_project(db_path: str, workspace_path: str) -> bool:
    """True if ``db_path`` resolves under this project's ``.hiveweave/worktrees/``.

    Hard sandbox for prefer-DB / heal: rejects cross-project short_id collisions
    and hand-edited absolute paths that escape the project worktree root.
    """
    if not db_path or not workspace_path:
        return False
    try:
        root = (Path(workspace_path) / WORKTREE_DIR).resolve()
        resolved = Path(db_path).resolve()
        resolved.relative_to(root)
        return True
    except (ValueError, OSError):
        return False


def _has_git(path: str) -> bool:
    return (Path(path) / ".git").exists()


def _project_root_guess(path: Path) -> str:
    """从 worktree 路径反推项目根（解锁用，best-effort）。

    规范形 ``<proj>/.hiveweave/worktrees/<sid>`` → 取 ``.hiveweave`` 祖先的
    父目录。反推不到（畸形路径）时退回路径自身 —— ``unlock_git_lockdown``
    对无 ``.git`` 目录的路径是安全 no-op。
    """
    for anc in path.parents:
        if anc.name.lower() == ".hiveweave":
            return str(anc.parent)
    return str(path)


def _force_clear_path(path: str) -> bool:
    """Remove a path so ``git worktree add`` can reuse it.

    Windows often leaves a non-git husk (e.g. only ``node_modules``) that
    cannot be fully deleted while files are locked — then ``worktree add``
    fails with "already exists" and we stamp a false ``worktree_error``.
    Rename-aside lets add proceed.

    P1-6（2026-10-02 实测：SandboxUnavailable / worktree_rebuild / cwd 不存在
    连环）两条硬规矩：

    1. **AGENTS.md Git/ACL 锁死契约**：本路径删的是 ``.git`` 附近路径
       （worktree 内含被封条的 ``.git`` gitdir 指针）⇒ rmtree 前必须先
       ``unlock_git_lockdown()``，否则轻则 PermissionError、重则半删出
       「gitdir 指针空壳」→ 该 agent 工具面全灭（沙箱校验 fail-closed）。
       解锁 best-effort：失败降级继续但记 warning（真实判据是随后的删除状态）。
    2. **去掉 ``ignore_errors=True`` 的静默语义**：显式捕获异常 + 删后验证。
       Windows 文件锁（agent 进程还活着）删不掉/移不走是合法路径 ⇒ 走
       「rename-aside 也失败 ⇒ 返回 False」，让上层（service_create 的
       repair / relocation 分支）报错，绝不静默宣称已清。
    """
    p = Path(path)
    if not p.exists():
        return True
    try:
        from hiveweave.services.acl_sandbox.service import unlock_git_lockdown

        unlock_git_lockdown(_project_root_guess(p))
    except Exception as exc:  # noqa: BLE001 — 解锁失败不挡清理，留痕即可
        log.warning(
            "git_worktree.force_clear_unlock_failed",
            path=path,
            root=_project_root_guess(p),
            error=str(exc),
        )
    try:
        shutil.rmtree(path)
    except OSError as exc:
        log.warning(
            "git_worktree.force_clear_rmtree_failed",
            path=path,
            error=str(exc),
        )
    if not p.exists():
        return True
    try:
        dest = p.parent / f".stale-{p.name}-{int(time.time() * 1000)}"
        p.rename(dest)
    except OSError as exc:
        # rename-aside 也失败 ⇒ 上层报错（repair / relocation），不静默。
        log.error("git_worktree.force_clear_failed", path=path, error=str(exc))
        return False
    try:
        shutil.rmtree(dest)
    except OSError as exc:
        log.warning(
            "git_worktree.force_clear_aside_rmtree_failed",
            aside=str(dest),
            error=str(exc),
        )
    log.info(
        "git_worktree.stale_path_moved_aside",
        path=path,
        aside=str(dest),
        aside_gone=not dest.exists(),
    )
    return not Path(path).exists()
