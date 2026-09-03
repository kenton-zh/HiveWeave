"""Short MAIN vs worktree labels for receipts and System 2.

Kept free of tools/prompts imports so identity/context can use it
without loading the tool registry.
"""
from __future__ import annotations

from pathlib import Path


def tree_tag(path: str) -> str:
    """Short label: ``MAIN`` or ``worktree A136`` (dirname, including relocate)."""
    if not path:
        return "MAIN"
    try:
        parts = Path(path).resolve().parts
    except (OSError, ValueError):
        return "MAIN"
    for i in range(len(parts) - 1):
        if parts[i] == ".hiveweave" and parts[i + 1] == "worktrees":
            if i + 2 < len(parts):
                return f"worktree {parts[i + 2]}"
            return "worktree"
    return "MAIN"


def tree_relpath(path: str) -> str | None:
    """``.hiveweave/worktrees/<id>`` for a worktree path; None on MAIN."""
    tag = tree_tag(path)
    if not tag.startswith("worktree "):
        return None
    return f".hiveweave/worktrees/{tag.split(' ', 1)[1]}"


def write_tree_suffix(workspace_path: str) -> str:
    """One short suffix for write/edit/patch success receipts."""
    tag = tree_tag(workspace_path)
    if tag == "MAIN":
        return " [MAIN]"
    return f" [{tag}, not MAIN until merge]"


def listing_header(listed_path: str) -> str:
    return f"Listing: {tree_tag(listed_path)}"


def cwd_display(cwd: str, relative: str | None = None) -> str:
    """Agent-visible cwd: tree tag + relative name, never a D:\\ dump."""
    if not (cwd or "").strip():
        return "[cwd unknown]"
    tag = tree_tag(cwd)
    if relative and relative not in {".", ""}:
        loc = relative.replace("\\", "/")
    else:
        loc = tree_relpath(cwd) or "project root"
    return f"[{tag} {loc}]"


# Leaf/QA miss: do not mention git_worktree_list, peer trees, or ../docs
# (from a worktree, ../ is the sibling-worktrees dir, not MAIN).
READ_MISS_HINT = (
    " Not in this tree. Shared contracts are MAIN docs/ after merge "
    "(empty MAIN is OK). Do not search other agents' trees."
    " 平台自管共享产物（.hiveweave/reports/**）会自动从 MAIN 读取——"
    "若仍报缺失，说明该产物确实不存在或已随取消任务归档。"
)
