"""Heal A004 stale non-git husk + clear worktree_error."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from hiveweave.services.git_worktree import _force_clear_path

WS = Path(r"D:\PC_AI\Project\TEST12")
PATH = WS / ".hiveweave" / "worktrees" / "A004"
PDB = WS / ".hiveweave" / "data.db"


def main() -> None:
    print("before exists", PATH.exists(), "git", (PATH / ".git").exists())
    if PATH.exists() and not (PATH / ".git").exists():
        ok = _force_clear_path(str(PATH))
        print("force_clear", ok, "exists_now", PATH.exists())
    c = sqlite3.connect(str(PDB))
    c.execute(
        "UPDATE agents SET worktree_error=NULL WHERE short_id='A004'"
    )
    c.commit()
    print(
        "db",
        c.execute(
            "SELECT short_id, worktree_error FROM agents WHERE short_id='A004'"
        ).fetchone(),
    )
    c.close()


if __name__ == "__main__":
    main()
