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
    assert "Writes: this tree only" in msg
    assert "MAIN docs/" in msg
    assert "Do NOT edit project root" not in msg
    assert "Review unmerged" not in msg
    assert "git_worktree_merge" not in msg
    assert "<assignee>" not in msg


def test_pin_keeps_assignee_own_path():
    msg = pin_dispatch_message_to_worktree(
        "Fix .hiveweave/worktrees/A005/src/x.js",
        short_id="A005",
        worktree_path="/wt/A005",
    )
    assert ".hiveweave/worktrees/A005/src/x.js" in msg
    assert "[WORKTREE PIN]" in msg
    assert "Writes: this tree only" in msg
    assert "Review unmerged" not in msg
    assert "git_worktree_merge" not in msg
    assert "<assignee>" not in msg


def test_pin_strips_absolute_worktree_prefix():
    msg = pin_dispatch_message_to_worktree(
        r"Read D:\proj\.hiveweave\worktrees\A005\src\x.js and "
        r"D:\proj\.hiveweave\worktrees\A001\y.js",
        short_id="A005",
        worktree_path=r"D:\proj\.hiveweave\worktrees\A005",
    )
    body = msg.split("[WORKTREE PIN]", 1)[0]
    assert r"D:" not in body
    assert ".hiveweave/worktrees/A005/src/x.js" in body
    assert ".hiveweave/worktrees/A005/y.js" in body
    assert "A001" not in body
    assert "Review unmerged" not in msg
    assert "<assignee>" not in msg


def test_pin_keeps_cjk_text_adjacent_to_path():
    """中文/反引号紧贴路径时不得吞字（审计 P1：`请在.xxxx` 丢「请在」）。"""
    msg = pin_dispatch_message_to_worktree(
        "请在.hiveweave/worktrees/A001/src/app.tsx 中实现登录",
        short_id="A005",
        worktree_path="/wt/A005",
    )
    body = msg.split("[WORKTREE PIN]", 1)[0]
    assert "请在" in body
    assert ".hiveweave/worktrees/A005/src/app.tsx" in body

    msg2 = pin_dispatch_message_to_worktree(
        "修改 `path=.hiveweave/worktrees/A001/x.js` 即可",
        short_id="A005",
        worktree_path="/wt/A005",
    )
    body2 = msg2.split("[WORKTREE PIN]", 1)[0]
    assert "`path=" in body2
    assert body2.count("`") == 2
    assert ".hiveweave/worktrees/A005/x.js" in body2


def test_pin_matches_five_digit_short_id_whole():
    """5 位 sid（generate_short_id 无上限）整段匹配，不截断成畸形路径。"""
    msg = pin_dispatch_message_to_worktree(
        "参考 .hiveweave/worktrees/A10001/src/a.ts 的旧实现",
        short_id="A005",
        worktree_path="/wt/A005",
    )
    body = msg.split("[WORKTREE PIN]", 1)[0]
    assert ".hiveweave/worktrees/A005/src/a.ts" in body
    assert "A10001" not in body
    assert "A0051" not in body  # 截断残留（A1000 被换 + 尾巴 1）不得出现


def test_coordinator_tools_exclude_worktree_create():
    assert "git_worktree_create" not in COORDINATOR_TOOLS
    assert "git_worktree_create" not in COORDINATOR_ONLY_TOOLS
    assert "git_worktree_merge" in COORDINATOR_ONLY_TOOLS
