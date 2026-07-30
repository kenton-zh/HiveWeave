"""Pin dispatch messages to agent worktrees."""
from __future__ import annotations

import re

def pin_dispatch_message_to_worktree(
    description: str,
    *,
    short_id: str,
    worktree_path: str,
) -> str:
    """Rewrite wrong worktree paths and append a mandatory WORKTREE PIN footer."""
    import re

    text = description or ""
    sid = (short_id or "").strip()
    if not sid:
        return text

    def _repl(m: re.Match[str]) -> str:
        other = m.group(1)
        if other.upper() == sid.upper():
            return m.group(0)
        return f".hiveweave/worktrees/{sid}"

    text = re.sub(
        r"\.hiveweave[/\\]+worktrees[/\\]+(A\d+)",
        _repl,
        text,
        flags=re.IGNORECASE,
    )
    # Avoid pointing at bare project-root file edits without worktree context
    footer = (
        f"\n\n[WORKTREE PIN] You MUST edit only under your worktree ({sid}):\n"
        f"  {worktree_path}\n"
        f"Do NOT edit project root/main or other agents' worktrees "
        f"(e.g. A001/CEO). After submit, coordinator merges with "
        f"git_worktree_merge(branchName='{sid}')."
    )
    if "[WORKTREE PIN]" not in text:
        text = text.rstrip() + footer
    return text
