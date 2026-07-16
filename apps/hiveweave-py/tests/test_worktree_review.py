"""Worktree review gate + merge-scope helpers."""

from __future__ import annotations

from pathlib import Path

from hiveweave.services.worktree_review import (
    MERGE_CONFLICT_HINT,
    compare_worktree_to_main,
    select_tasks_for_merged_work,
)


def test_compare_diverged_allows(tmp_path: Path):
    main = tmp_path / "main"
    wt = tmp_path / "wt"
    main.mkdir()
    wt.mkdir()
    (main / "a.js").write_text("old", encoding="utf-8")
    (wt / "a.js").write_text("new", encoding="utf-8")
    deny, meta = compare_worktree_to_main(
        main_ws=str(main), worktree_ws=str(wt), files_changed=["a.js"]
    )
    assert deny is None
    assert meta["divergedFiles"] == ["a.js"]


def test_compare_empty_files_blocks(tmp_path: Path):
    main = tmp_path / "main"
    wt = tmp_path / "wt"
    main.mkdir()
    wt.mkdir()
    deny, _ = compare_worktree_to_main(
        main_ws=str(main), worktree_ws=str(wt), files_changed=[]
    )
    assert deny is not None
    assert "files_changed is empty" in deny


def test_compare_identical_to_main_blocks(tmp_path: Path):
    main = tmp_path / "main"
    wt = tmp_path / "wt"
    main.mkdir()
    wt.mkdir()
    (main / "a.js").write_text("same", encoding="utf-8")
    (wt / "a.js").write_text("same", encoding="utf-8")
    deny, meta = compare_worktree_to_main(
        main_ws=str(main), worktree_ws=str(wt), files_changed=["a.js"]
    )
    assert deny is not None
    assert "identical to MAIN" in deny
    assert meta["identicalToMain"] == ["a.js"]


def test_compare_partial_identical_blocks(tmp_path: Path):
    main = tmp_path / "main"
    wt = tmp_path / "wt"
    main.mkdir()
    wt.mkdir()
    (main / "a.js").write_text("old", encoding="utf-8")
    (wt / "a.js").write_text("new", encoding="utf-8")
    (main / "b.js").write_text("same", encoding="utf-8")
    (wt / "b.js").write_text("same", encoding="utf-8")
    deny, meta = compare_worktree_to_main(
        main_ws=str(main),
        worktree_ws=str(wt),
        files_changed=["a.js", "b.js"],
    )
    assert deny is not None
    assert "b.js" in meta["identicalToMain"]


def test_compare_missing_in_worktree_blocks(tmp_path: Path):
    main = tmp_path / "main"
    wt = tmp_path / "wt"
    main.mkdir()
    wt.mkdir()
    (main / "a.js").write_text("x", encoding="utf-8")
    deny, _ = compare_worktree_to_main(
        main_ws=str(main), worktree_ws=str(wt), files_changed=["a.js"]
    )
    assert deny is not None
    assert "missing in assignee worktree" in deny


def test_merge_conflict_hint_is_executor_owned():
    assert "EXECUTOR FIXES IN WORKTREE" in MERGE_CONFLICT_HINT
    assert "aborted" in MERGE_CONFLICT_HINT.lower()
    assert "edit_file" not in MERGE_CONFLICT_HINT.lower()


def test_select_tasks_single_approved():
    tasks = [
        {
            "id": "t1",
            "assignee_id": "e1",
            "status": "approved",
            "tags": [],
            "updated_at": 1,
            "evidence": {"files_changed": ["a.js"]},
        },
        {
            "id": "t2",
            "assignee_id": "e1",
            "status": "running",
            "tags": [],
            "updated_at": 2,
        },
    ]
    selected = select_tasks_for_merged_work(
        tasks, assignee_id="e1", merged_files=["a.js"]
    )
    assert [t["id"] for t in selected] == ["t1"]


def test_select_tasks_intersects_files_not_all_approved():
    tasks = [
        {
            "id": "old",
            "assignee_id": "e1",
            "status": "approved",
            "tags": [],
            "updated_at": 10,
            "evidence": {"files_changed": ["legacy.js"]},
        },
        {
            "id": "new",
            "assignee_id": "e1",
            "status": "approved",
            "tags": [],
            "updated_at": 20,
            "evidence": {"files_changed": ["feature.js"]},
        },
    ]
    selected = select_tasks_for_merged_work(
        tasks, assignee_id="e1", merged_files=["feature.js"]
    )
    assert [t["id"] for t in selected] == ["new"]


def test_select_tasks_ambiguous_falls_back_to_newest():
    tasks = [
        {
            "id": "old",
            "assignee_id": "e1",
            "status": "approved",
            "tags": [],
            "updated_at": 10,
            "evidence": {},
        },
        {
            "id": "new",
            "assignee_id": "e1",
            "status": "approved",
            "tags": [],
            "updated_at": 99,
            "evidence": {},
        },
    ]
    selected = select_tasks_for_merged_work(
        tasks, assignee_id="e1", merged_files=["x.js"]
    )
    assert [t["id"] for t in selected] == ["new"]
