"""Worktree pin on dispatch + coordinator create ban."""

from __future__ import annotations

from hiveweave.services.git_worktree import pin_dispatch_message_to_worktree
from hiveweave.services.permission import COORDINATOR_TOOLS, COORDINATOR_ONLY_TOOLS


def test_pin_rewrites_wrong_short_id_paths():
    msg = pin_dispatch_message_to_worktree(
        "Edit .hiveweave/worktrees/A001/vite.config.js and GameMain.js",
        short_id="A005",
        worktree_path=r"D:\proj\.hiveweave\worktrees\A005",
    )
    assert ".hiveweave/worktrees/A005/vite.config.js" in msg
    assert "A001/vite" not in msg.replace("A001/CEO", "")
    assert "[WORKTREE PIN]" in msg
    assert "A005" in msg
    assert r"D:\proj\.hiveweave\worktrees\A005" in msg


def test_pin_keeps_assignee_own_path():
    msg = pin_dispatch_message_to_worktree(
        "Fix .hiveweave/worktrees/A005/src/x.js",
        short_id="A005",
        worktree_path="/wt/A005",
    )
    assert ".hiveweave/worktrees/A005/src/x.js" in msg
    assert "[WORKTREE PIN]" in msg


def test_coordinator_tools_exclude_worktree_create():
    assert "git_worktree_create" not in COORDINATOR_TOOLS
    assert "git_worktree_create" not in COORDINATOR_ONLY_TOOLS
    assert "git_worktree_merge" in COORDINATOR_ONLY_TOOLS
