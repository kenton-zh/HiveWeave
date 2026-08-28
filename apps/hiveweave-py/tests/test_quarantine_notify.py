"""T2.5：隔离即通知 —— 隔离副作用必须可发现（P0-2 静默隔离）。

工具层 [MERGE QUARANTINE] inbox（TEST_DSH_32 P8）已有；这里钉住补充面：
1. ``list_pending_quarantine_dirs`` 清点（get_platform_state 计数的来源）；
2. ``UNCOMMITTED_WORKTREE`` 收工拦截文案附 ``quarantine_ref``；
3. ``build_platform_state`` 透出 ``merge_quarantine.pending`` 条目。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.services.git_worktree.merge_support import (
    list_pending_quarantine_dirs,
)
from hiveweave.services.turn_exit import (
    _format_uncommitted_worktree_label,
    _worktree_hint_details,
    _attach_quarantine_info,
)


def _make_quarantine(tmp_path: Path, stamps: dict[str, int]) -> None:
    for stamp, n_files in stamps.items():
        d = tmp_path / ".hiveweave" / "merge-quarantine" / stamp
        d.mkdir(parents=True, exist_ok=True)
        for i in range(n_files):
            (d / f"f{i}.json").write_text("{}", encoding="utf-8")


def test_list_pending_quarantine_dirs_newest_first(tmp_path: Path):
    _make_quarantine(tmp_path, {"20260827-100000": 3, "20260828-090000": 2})
    out = list_pending_quarantine_dirs(str(tmp_path))
    assert [e["stamp"] for e in out] == ["20260828-090000", "20260827-100000"]
    assert out[0]["file_count"] == 2
    assert out[0]["path"].endswith("20260828-090000")


def test_list_pending_quarantine_dirs_empty_when_absent(tmp_path: Path):
    assert list_pending_quarantine_dirs(str(tmp_path)) == []


def test_uncommitted_label_carries_quarantine_ref():
    agent = "agent-q1"
    _worktree_hint_details[agent] = {
        "dirty": True,
        "files": ["src/a.ts"],
        "path": "/ws/.hiveweave/worktrees/A001",
        "git_error": None,
        "quarantine": [
            {"stamp": "20260828-090000", "path": "/ws/.hiveweave/merge-quarantine/20260828-090000",
             "file_count": 15},
        ],
    }
    try:
        label = _format_uncommitted_worktree_label(agent)
    finally:
        _worktree_hint_details.pop(agent, None)
    assert "quarantine_ref=" in label
    assert "20260828-090000" in label
    assert "15 files" in label
    assert "recoverable" in label


def test_uncommitted_label_without_quarantine_unchanged():
    agent = "agent-q2"
    _worktree_hint_details[agent] = {
        "dirty": True,
        "files": [],
        "path": "/ws/.hiveweave/worktrees/A002",
        "git_error": None,
    }
    try:
        label = _format_uncommitted_worktree_label(agent)
    finally:
        _worktree_hint_details.pop(agent, None)
    assert "quarantine_ref" not in label
    assert "path=" in label


def test_attach_quarantine_info_populates_details(tmp_path: Path):
    _make_quarantine(tmp_path, {"20260828-120000": 4})
    details: dict = {}
    _attach_quarantine_info(details, str(tmp_path))
    assert details["quarantine"][0]["file_count"] == 4
    # 空 ws / 无目录 → 不炸、值为空清单
    details2: dict = {}
    _attach_quarantine_info(details2, None)
    _attach_quarantine_info(details2, str(tmp_path / "nope"))
    assert details2.get("quarantine") in (None, [])


async def test_platform_state_has_quarantine_entry(tmp_path: Path):
    """build_platform_state 透出 merge_quarantine.pending（verified）。"""
    _make_quarantine(tmp_path, {"20260828-130000": 7})
    from hiveweave.services.platform_state import build_platform_state

    async def fake_ws(pid: str):
        return str(tmp_path)

    with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
        snapshot = await build_platform_state(
            agent_id="agent-q3", project_id="proj-q3"
        )
    epi = snapshot.get("epistemology") or {}
    keys = [
        e.get("key")
        for e in (epi.get("verified") or []) + (epi.get("unknown") or [])
    ]
    assert "merge_quarantine.pending" in keys
    rows = epi.get("verified") or []
    entry = next(
        e for e in rows if e["key"] == "merge_quarantine.pending"
    )
    assert entry["value"]["count"] == 1
    assert entry["value"]["total_files"] == 7
