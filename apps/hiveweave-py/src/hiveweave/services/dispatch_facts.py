"""dispatch_facts — 派单时自动核验快照（40 轮 verifiedFacts L2）。

派单时由平台自动采集**廉价可机核事实**（git 状态/文件系统快照），追加到
任务卡，让执行者开局即知现场。与派单方手写的 verifiedFacts 互补：
- 本模块 = 平台自动核验（机器探测，带 @ 派单时点标记）
- verifiedFacts = 派单方人工核验（语义事实，信任派单方）

全部 best-effort：任何探测失败静默跳过，绝不阻塞派单。
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

_MAX_FACTS_PER_GROUP = 4
_GIT_TIMEOUT_S = 10


def _git(args: list[str], cwd: str) -> tuple[bool, str]:
    """Run a git command, return (ok, stripped stdout)."""
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            timeout=_GIT_TIMEOUT_S,
        )
        ok = r.returncode == 0
        out = (r.stdout or b"").decode("utf-8", errors="replace").strip()
        return ok, out
    except Exception:
        return False, ""


def _main_git_facts(main_ws: str) -> list[str]:
    facts: list[str] = []
    ok, head = _git(["rev-parse", "--short", "HEAD"], main_ws)
    if not ok:
        return facts
    ok, subject = _git(["log", "-1", "--format=%s"], main_ws)
    subject = subject[:70] if ok else ""
    facts.append(f"MAIN HEAD: {head} {subject}".rstrip())
    ok, dirty = _git(["status", "--porcelain"], main_ws)
    if ok:
        n = len([l for l in dirty.splitlines() if l.strip()])
        facts.append(f"MAIN 未提交改动: {n} 处")
    return facts


def _worktree_git_facts(main_ws: str, wt: str, label: str) -> list[str]:
    facts: list[str] = []
    ok, branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], wt)
    if not ok:
        return facts
    ok_a, ahead = _git(["rev-list", "--count", f"main..{branch}"], main_ws)
    ok_b, behind = _git(["rev-list", "--count", f"{branch}..main"], main_ws)
    if ok_a and ok_b:
        facts.append(
            f"worktree {label}（{branch}）：领先 main {ahead} / 落后 {behind}"
        )
    return facts


def _deliverable_file_facts(main_ws: str) -> list[str]:
    facts: list[str] = []
    common = ["index.html", "package.json", "README.md"]
    for name in common:
        p = Path(main_ws) / name
        if p.is_file():
            st = p.stat()
            age_min = max(0, int((time.time() - st.st_mtime) / 60))
            facts.append(
                f"交付物 {name}: {st.st_size} bytes（{age_min} 分钟前修改）"
            )
    return facts


def collect_dispatch_facts(
    main_ws: str | None,
    target_worktree: str | None = None,
    target_label: str | None = None,
) -> list[str]:
    """采集派单时点的自动核验事实（同步，快，全部 best-effort）。

    返回事实文本列表（可直接进任务卡的「平台自动核验快照」块）。
    """
    facts: list[str] = []
    if main_ws and Path(main_ws).is_dir():
        facts += _main_git_facts(main_ws)
        facts += _deliverable_file_facts(main_ws)
    if target_worktree and Path(target_worktree).is_dir():
        label = target_label or "目标工位"
        ok, branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], target_worktree)
        if ok:
            facts += _worktree_git_facts(main_ws, target_worktree, label) if main_ws else []
            ok_u, untracked = _git(
                ["status", "--porcelain"], target_worktree
            )
            if ok_u:
                n = len([l for l in untracked.splitlines() if l.strip()])
                if n:
                    facts.append(f"worktree {label} 未提交/未跟踪文件: {n} 处")
    return facts[:_MAX_FACTS_PER_GROUP * 3]


def format_facts_block(facts: list[str], *, auto: bool = False) -> str:
    """渲染事实块（追加到任务描述）。空列表 → 空串。"""
    if not facts:
        return ""
    header = (
        "## 平台自动核验快照（派单时点，机器探测）"
        if auto
        else "## 已核事实（派单方核验，可直接采信；与你的观察冲突时先复核再行动）"
    )
    return header + "\n" + "\n".join(f"- {f}" for f in facts)


def collect_and_format(
    main_ws: str | None,
    target_worktree: str | None = None,
    target_label: str | None = None,
) -> str:
    """便捷入口：采集 + 渲染自动核验块。失败返回空串。"""
    try:
        return format_facts_block(
            collect_dispatch_facts(main_ws, target_worktree, target_label),
            auto=True,
        )
    except Exception as e:
        log.debug("dispatch_facts_collect_failed", error=str(e))
        return ""
