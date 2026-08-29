"""Worktree vs MAIN labels on receipts, PIN, and System 2."""

from __future__ import annotations

from pathlib import Path

import pytest

from hiveweave.prompts.context import build_context_prompt
from hiveweave.prompts.identity import build_identity_prompt
from hiveweave.tools.bash import _cwd_style_hint
from hiveweave.tools.file import list_files, read_file, write_file
from hiveweave.tools.patch import apply_patch
from hiveweave.util.tree_label import tree_relpath, tree_tag, write_tree_suffix


@pytest.fixture
def layout(tmp_path: Path) -> dict[str, Path]:
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    wt = project / ".hiveweave" / "worktrees" / "A136"
    (wt / "src").mkdir(parents=True)
    return {"project": project, "wt": wt}


def test_tree_tag_main_and_worktree(layout: dict[str, Path]) -> None:
    assert tree_tag(str(layout["project"])) == "MAIN"
    assert tree_tag(str(layout["wt"])) == "worktree A136"
    assert tree_tag(str(layout["wt"] / "src")) == "worktree A136"
    assert tree_relpath(str(layout["wt"])) == ".hiveweave/worktrees/A136"
    assert tree_relpath(str(layout["project"])) is None
    assert write_tree_suffix(str(layout["project"])) == " [MAIN]"
    assert "not MAIN until merge" in write_tree_suffix(str(layout["wt"]))
    assert "A136" in write_tree_suffix(str(layout["wt"]))


@pytest.mark.asyncio
async def test_write_file_tags_worktree(layout: dict[str, Path]) -> None:
    result = await write_file(
        file_path="docs/spec.md",
        content="hello",
        workspace_path=str(layout["wt"]),
    )
    assert result["success"] is True
    out = result["output"]
    assert "Wrote docs/spec.md" in out
    assert "[worktree A136, not MAIN until merge]" in out
    assert "D:" not in out
    assert str(layout["wt"]) not in out


@pytest.mark.asyncio
async def test_write_file_tags_main(layout: dict[str, Path]) -> None:
    result = await write_file(
        file_path="docs/spec.md",
        content="hello",
        workspace_path=str(layout["project"]),
    )
    assert result["success"] is True
    assert result["output"].endswith(" [MAIN]")


@pytest.mark.asyncio
async def test_apply_patch_tags_worktree(layout: dict[str, Path]) -> None:
    result = await apply_patch(
        patches=[{"op": "add", "filePath": "src/a.txt", "content": "x"}],
        workspace_path=str(layout["wt"]),
    )
    assert result["success"] is True
    assert "[worktree A136, not MAIN until merge]" in result["output"]
    assert not result["output"].startswith("ERROR")


@pytest.mark.asyncio
async def test_read_file_miss_does_not_point_at_worktrees(
    layout: dict[str, Path],
) -> None:
    result = await read_file(
        file_path="docs/spec.md",
        offset=0,
        limit=10,
        workspace_path=str(layout["wt"]),
        project_root=str(layout["project"]),
    )
    assert result["success"] is False
    err = result["error"] or ""
    assert "File not found" in err
    assert "MAIN" in err
    assert "../docs" not in err
    assert "git_worktree_list" not in err
    assert "worktrees" not in err.lower()


@pytest.mark.asyncio
async def test_list_files_header_worktree(layout: dict[str, Path]) -> None:
    result = await list_files(
        path="",
        workspace_path=str(layout["wt"]),
        project_root=str(layout["project"]),
    )
    assert result["success"] is True
    assert result["output"].startswith("Listing: worktree A136\n")


@pytest.mark.asyncio
async def test_list_files_header_main(layout: dict[str, Path]) -> None:
    result = await list_files(
        path="",
        workspace_path=str(layout["project"]),
        project_root=str(layout["project"]),
    )
    assert result["success"] is True
    assert result["output"].startswith("Listing: MAIN\n")


@pytest.mark.asyncio
async def test_list_files_shared_miss_hints_write_path(
    layout: dict[str, Path],
) -> None:
    """P1-1 回归：worktree 里探 `.hiveweave/shared/` 不存在（空目录不物化）
    必须分支化提示——教 write→checkpoint→merge 与可见时机，而不是用通用
    READ_MISS_HINT 把它说成"不在树内 / 去 MAIN docs"（曾两次误报通道不存在）。"""
    result = await list_files(
        path=str(layout["wt"] / ".hiveweave" / "shared"),
        workspace_path=str(layout["wt"]),
        project_root=str(layout["project"]),
    )
    assert result["success"] is False
    err = result["error"] or ""
    assert "Directory not found" in err
    assert "write_file to .hiveweave/shared/<file>" in err
    assert "checkpoint" in err
    assert "Not in this tree" not in err


def test_system2_workspace_lines(layout: dict[str, Path]) -> None:
    main = build_context_prompt(
        "id", None, None, workspace_path=str(layout["project"]),
    )
    assert "Workspace: MAIN (project root)" in main
    assert "team-visible" in main

    leaf = build_context_prompt(
        "id", None, None,
        workspace_path=str(layout["wt"]),
        role="签到排行榜工程师",
    )
    assert "Workspace: worktree A136 (.hiveweave/worktrees/A136)" in leaf
    assert "../docs" not in leaf
    assert "bash_main" not in leaf

    qa = build_context_prompt(
        "id", None, None,
        workspace_path=str(layout["wt"]),
        role="测试工程师",
    )
    assert "bash_main / browse_main" in qa

    qa_en = build_context_prompt(
        "id", None, None,
        workspace_path=str(layout["wt"]),
        role="qa engineer",
    )
    assert "bash_main / browse_main" in qa_en


def test_identity_keeps_evidence_paths_and_splits_worktrees() -> None:
    text = build_identity_prompt(
        role="developer",
        role_type="executor",
        backstory="",
        name="Robert",
    )
    assert "Official evidence location" in text
    assert ".hiveweave/reports/<task-shortId>/" in text
    assert "tool_outputs/" in text
    assert "unmerged checkout" in text
    assert "write sandbox" not in text
    assert "individual drafts, reports, and test outputs" not in text
    assert "Do NOT edit project root" not in text


def test_executor_playbook_does_not_ban_reading_main() -> None:
    text = build_identity_prompt(
        role="签到排行榜工程师",
        role_type="executor",
        backstory="",
        name="Robert",
        permission_type="executor",
    )
    assert "Writes: this tree only" in text
    assert "MAIN `docs/`" in text
    assert "git_worktree_list" not in text
    assert "Use .hiveweave/ ONLY for draft notes" not in text


def test_ceo_playbook_is_main_not_worktree() -> None:
    text = build_identity_prompt(
        role="CEO",
        role_type="coordinator",
        backstory="",
        name="归零",
        permission_type="coordinator",
    )
    assert "Workspace: MAIN (project root)" in text
    assert "You do not have a worktree" in text
    assert "Empty MAIN" in text
    assert "write sandbox" not in text
    assert "CEO and HR stay on MAIN" in text


def test_bash_cwd_hint_has_no_abs_dump(layout: dict[str, Path]) -> None:
    wt = _cwd_style_hint(str(layout["wt"]))
    assert wt.startswith("[worktree A136")
    assert ".hiveweave/worktrees/A136" in wt
    assert "D:" not in wt
    assert str(layout["wt"]) not in wt
    main = _cwd_style_hint(str(layout["project"]))
    assert main.startswith("[MAIN")
    assert "project root" in main
    assert "D:" not in main


def test_tool_schemas_do_not_teach_dotdot_as_main() -> None:
    import inspect

    from hiveweave.tools.executor import TOOL_PARAM_SCHEMAS
    from hiveweave.tools import file as file_mod

    read_desc = TOOL_PARAM_SCHEMAS["read_file"]["properties"]["filePath"]["description"]
    list_desc = TOOL_PARAM_SCHEMAS["list_files"]["properties"]["dirPath"]["description"]
    src = inspect.getsource(file_mod)
    assert "via ../" not in src
    assert "../ for main" not in src
    assert "../peerId" not in src
    assert "Do not use ../" in src
    assert ".hiveweave/worktrees/<shortId>" in read_desc
    assert "Do not use ../" in list_desc
    assert "Do not use ../" in read_desc


def test_git_worktree_list_line_never_dumps_abs(layout: dict[str, Path]) -> None:
    from hiveweave.util.tree_label import tree_relpath as rel

    wt_line = f"A136: hw/A136/work {rel(str(layout['wt'])) or 'MAIN'}"
    main_line = f"MAIN: main {rel(str(layout['project'])) or 'MAIN'}"
    assert wt_line == "A136: hw/A136/work .hiveweave/worktrees/A136"
    assert main_line == "MAIN: main MAIN"
    assert "D:" not in wt_line
    assert str(layout["project"]) not in main_line
