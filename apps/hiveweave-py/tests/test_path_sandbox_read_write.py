"""Read/write path sandbox: write confined to workspace, read allows project root."""

from __future__ import annotations

from pathlib import Path

import pytest

from hiveweave.tools.file import (
    _resolve_safe,
    infer_project_root,
    resolve_for_read,
    write_file,
    read_file,
)


@pytest.fixture
def project_layout(tmp_path: Path) -> dict[str, Path]:
    """project/
         src/main.txt
         .hiveweave/worktrees/A001/src/own.txt
         .hiveweave/worktrees/A002/src/peer.txt
    """
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "main.txt").write_text("main-content", encoding="utf-8")

    wt_a = project / ".hiveweave" / "worktrees" / "A001"
    wt_b = project / ".hiveweave" / "worktrees" / "A002"
    (wt_a / "src").mkdir(parents=True)
    (wt_b / "src").mkdir(parents=True)
    (wt_a / "src" / "own.txt").write_text("own-content", encoding="utf-8")
    (wt_b / "src" / "peer.txt").write_text("peer-content", encoding="utf-8")

    return {"project": project, "wt_a": wt_a, "wt_b": wt_b}


def test_infer_project_root_from_worktree(project_layout: dict[str, Path]) -> None:
    assert infer_project_root(str(project_layout["wt_a"])) == str(
        project_layout["project"].resolve()
    )
    assert infer_project_root(str(project_layout["project"])) == str(
        project_layout["project"].resolve()
    )


def test_write_stays_in_own_worktree(project_layout: dict[str, Path]) -> None:
    wt = str(project_layout["wt_a"])
    # relative write OK
    assert _resolve_safe(wt, "src/new.txt") is not None
    # escape to peer / main denied
    assert _resolve_safe(wt, "../A002/src/peer.txt") is None
    assert _resolve_safe(wt, "../../../src/main.txt") is None


def test_read_allows_project_via_relative(project_layout: dict[str, Path]) -> None:
    wt = str(project_layout["wt_a"])
    root = str(project_layout["project"])

    own = resolve_for_read(wt, "src/own.txt", root)
    assert own is not None
    assert own.endswith("own.txt")

    peer = resolve_for_read(wt, "../A002/src/peer.txt", root)
    assert peer is not None
    assert Path(peer).read_text(encoding="utf-8") == "peer-content"

    main = resolve_for_read(wt, "../../../src/main.txt", root)
    assert main is not None
    assert Path(main).read_text(encoding="utf-8") == "main-content"

    # escape project denied
    assert resolve_for_read(wt, "../../../../outside.txt", root) is None


@pytest.mark.asyncio
async def test_read_file_tool_cross_worktree(project_layout: dict[str, Path]) -> None:
    wt = str(project_layout["wt_a"])
    root = str(project_layout["project"])

    result = await read_file(
        file_path="../A002/src/peer.txt",
        offset=0,
        limit=50,
        workspace_path=wt,
        project_root=root,
    )
    assert result["success"] is True
    assert "peer-content" in result["output"]


@pytest.mark.asyncio
async def test_write_file_rejects_outside_worktree(
    project_layout: dict[str, Path],
) -> None:
    wt = str(project_layout["wt_a"])
    result = await write_file(
        file_path="../A002/hacked.txt",
        content="nope",
        workspace_path=wt,
    )
    assert result["success"] is False
    assert "Sandbox violation" in (result["error"] or "")
