"""Worktree review gate + merge-scope helpers."""

from __future__ import annotations

from pathlib import Path

from hiveweave.services.worktree_review import (
    MERGE_CONFLICT_HINT,
    compare_worktree_to_main,
    hint_missing_file_locations,
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


def test_compare_identical_to_main_allows_already_merged(tmp_path: Path):
    """BUG-9: all claimed files already on MAIN → allow approve (close ledger)."""
    main = tmp_path / "main"
    wt = tmp_path / "wt"
    main.mkdir()
    wt.mkdir()
    (main / "a.js").write_text("same", encoding="utf-8")
    (wt / "a.js").write_text("same", encoding="utf-8")
    deny, meta = compare_worktree_to_main(
        main_ws=str(main), worktree_ws=str(wt), files_changed=["a.js"]
    )
    assert deny is None
    assert meta.get("alreadyOnMain") is True
    assert meta["identicalToMain"] == ["a.js"]


def test_compare_partial_identical_strips_and_allows(tmp_path: Path):
    """TEST21 M1: mixed identical + diverged → strip identical, approve diverged."""
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
    assert deny is None
    assert meta["divergedFiles"] == ["a.js"]
    assert meta["identicalToMain"] == ["b.js"]
    assert meta.get("confirmedOnMain") == ["b.js"]
    assert meta.get("strippedIdentical") is True


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


def test_hint_missing_file_locations_finds_leaf(tmp_path: Path):
    root = tmp_path / "main"
    (root / ".hiveweave" / "reports").mkdir(parents=True)
    (root / ".hiveweave" / "reports" / "A018-moduleC-verification.md").write_text(
        "x", encoding="utf-8"
    )
    hints = hint_missing_file_locations(
        [str(root)], ["A018-moduleC-verification.md"]
    )
    assert len(hints) == 1
    assert "A018-moduleC-verification.md" in hints[0]
    assert ".hiveweave" in hints[0]


def test_hint_missing_file_locations_no_match_empty(tmp_path: Path):
    root = tmp_path / "main"
    root.mkdir()
    assert hint_missing_file_locations([str(root)], ["nope.txt"]) == []


def test_hint_missing_file_locations_multi_root_no_overwrite(tmp_path: Path):
    wtree = tmp_path / "wtree"
    main = tmp_path / "main"
    (wtree / "src").mkdir(parents=True)
    (main / "src").mkdir(parents=True)
    (main / "src" / "target.md").write_text("x", encoding="utf-8")
    hints = hint_missing_file_locations(
        [str(wtree), str(main)], ["target.md"]
    )
    assert len(hints) == 1
    assert "target.md" in hints[0]
    assert str(wtree) not in hints[0]


def test_hint_missing_file_locations_prunes_big_dirs(tmp_path: Path):
    root = tmp_path / "main"
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "src").mkdir(parents=True)
    (root / "node_modules" / "pkg" / "wanted.md").write_text(
        "x", encoding="utf-8"
    )
    (root / "src" / "wanted.md").write_text("x", encoding="utf-8")
    hints = hint_missing_file_locations([str(root)], ["wanted.md"])
    assert len(hints) == 1
    assert "src" in hints[0]
    assert "node_modules" not in hints[0]


def test_hint_missing_file_locations_caps_hints(tmp_path: Path):
    root = tmp_path / "main"
    root.mkdir()
    for i in range(6):
        (root / f"f{i}.md").write_text("x", encoding="utf-8")
    hints = hint_missing_file_locations(
        [str(root)], [f"f{i}.md" for i in range(6)]
    )
    assert len(hints) <= 4


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


def test_select_tasks_ambiguous_returns_all_newest_first():
    # 语义变更（worktree_review.select_tasks_for_merged_work docstring）：
    # 同 assignee、无文件交集可歧义消解时，返回该 assignee 全部 approved
    # 任务（updated_at 降序），不再 [:1] 静默丢弃兄弟任务 —— 同一次
    # worktree merge 覆盖他在分支上的所有已批准工作。
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
    assert [t["id"] for t in selected] == ["new", "old"]
