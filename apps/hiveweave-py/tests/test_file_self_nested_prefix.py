"""M4 regression: self-nested worktree prefix (ghost tree) rejection.

Agent workspace IS a worktree (``<project>/.hiveweave/worktrees/<id>``); a
path repeating that prefix — e.g. ``.hiveweave/worktrees/<id>/src/x.py`` —
previously passed `_check_hiveweave_dir`'s ``worktrees`` allow-list and
`write_file` silently ``mkdir(parents=True)`` a ghost nested tree
(slack-clone_03 A044 start_dev_server failure). Now blocked with a clear
error, before the allow-list is consulted.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hiveweave.tools.file import (
    _double_worktree_prefix,
    _resolve_safe,
    list_files,
    read_file,
    resolve_for_read,
    write_file,
)

M4_ERROR = "疑似重复 worktree 前缀路径"


@pytest.fixture
def worktree_layout(tmp_path: Path) -> dict[str, Path]:
    """project/
         .hiveweave/worktrees/A044/   (agent's own worktree)
         .hiveweave/worktrees/A045/   (sibling worktree)
    """
    project = tmp_path / "project"
    wt_a = project / ".hiveweave" / "worktrees" / "A044"
    wt_b = project / ".hiveweave" / "worktrees" / "A045"
    wt_a.mkdir(parents=True)
    (wt_b / "src").mkdir(parents=True)
    (wt_b / "src" / "peer.txt").write_text("peer-content", encoding="utf-8")
    return {"project": project, "wt_a": wt_a, "wt_b": wt_b}


# ── ① double-prefix write: clear error + no ghost dir ───────────────


@pytest.mark.asyncio
async def test_write_double_prefix_rejected_no_ghost(
    worktree_layout: dict[str, Path],
) -> None:
    wt = worktree_layout["wt_a"]
    result = await write_file(
        file_path=".hiveweave/worktrees/A044/scripts/start.sh",
        content="echo hi",
        workspace_path=str(wt),
    )
    assert result["success"] is False
    assert M4_ERROR in (result["error"] or "")
    assert ".hiveweave/worktrees/A044" in (result["error"] or "")
    ghost = wt / ".hiveweave" / "worktrees" / "A044"
    assert not ghost.exists(), "ghost nested tree must not be created"
    assert not (wt / ".hiveweave").exists()


@pytest.mark.asyncio
async def test_write_absolute_double_prefix_rejected(
    worktree_layout: dict[str, Path],
) -> None:
    wt = worktree_layout["wt_a"]
    ghost_target = wt / ".hiveweave" / "worktrees" / "A044" / "x.sh"
    result = await write_file(
        file_path=str(ghost_target),
        content="echo hi",
        workspace_path=str(wt),
    )
    assert result["success"] is False
    assert M4_ERROR in (result["error"] or "")
    assert not ghost_target.exists()


def test_resolve_safe_double_prefix_returns_none(
    worktree_layout: dict[str, Path],
) -> None:
    wt = worktree_layout["wt_a"]
    assert _resolve_safe(str(wt), ".hiveweave/worktrees/A044/x.py") is None


# ── ② normal write inside worktree unaffected ────────────────────────


@pytest.mark.asyncio
async def test_write_inside_worktree_unaffected(
    worktree_layout: dict[str, Path],
) -> None:
    wt = worktree_layout["wt_a"]
    result = await write_file(
        file_path="src/app.py",
        content="print('hi')",
        workspace_path=str(wt),
    )
    assert result["success"] is True
    assert (wt / "src" / "app.py").read_text(encoding="utf-8") == "print('hi')"


# ── ③ read sibling worktree unaffected ───────────────────────────────


@pytest.mark.asyncio
async def test_read_sibling_worktree_unaffected(
    worktree_layout: dict[str, Path],
) -> None:
    wt = worktree_layout["wt_a"]
    result = await read_file(
        file_path="../A045/src/peer.txt",
        offset=0,
        limit=50,
        workspace_path=str(wt),
        project_root=str(worktree_layout["project"]),
    )
    assert result["success"] is True
    assert "peer-content" in result["output"]


def test_resolve_for_read_sibling_unaffected(
    worktree_layout: dict[str, Path],
) -> None:
    wt = worktree_layout["wt_a"]
    peer = resolve_for_read(
        str(wt), "../A045/src/peer.txt", str(worktree_layout["project"])
    )
    assert peer is not None
    peer_path = Path(peer)
    assert peer_path.parts[-3:] == ("A045", "src", "peer.txt")


# ── read/list double-prefix also rejected with clear error ───────────


@pytest.mark.asyncio
async def test_read_double_prefix_rejected(
    worktree_layout: dict[str, Path],
) -> None:
    wt = worktree_layout["wt_a"]
    result = await read_file(
        file_path=".hiveweave/worktrees/A044/notes.md",
        offset=0,
        limit=50,
        workspace_path=str(wt),
        project_root=str(worktree_layout["project"]),
    )
    assert result["success"] is False
    assert M4_ERROR in (result["error"] or "")


@pytest.mark.asyncio
async def test_list_files_double_prefix_rejected(
    worktree_layout: dict[str, Path],
) -> None:
    wt = worktree_layout["wt_a"]
    result = await list_files(
        path=".hiveweave/worktrees/A044",
        workspace_path=str(wt),
        project_root=str(worktree_layout["project"]),
    )
    assert result["success"] is False
    assert M4_ERROR in (result["error"] or "")


# ── ④ worktree 内平台自管目录可读（复审 P1：误杀 tool_outputs 句柄）───


@pytest.mark.asyncio
async def test_worktree_tool_outputs_handle_readable(
    worktree_layout: dict[str, Path],
) -> None:
    """worktree workspace 内读自身 .hiveweave/tool_outputs 落盘句柄必须放行。

    复审 P1：workspace 在 worktree 内时「相对路径任何 .hiveweave 段皆幽灵」
    过宽——executor 把大输出落盘 <ws>/.hiveweave/tool_outputs/ 并回传句柄，
    见 .hiveweave 就拒切断了该契约。仅 .hiveweave/worktrees 段判幽灵。
    """
    wt = worktree_layout["wt_a"]
    out_dir = wt / ".hiveweave" / "tool_outputs"
    out_dir.mkdir(parents=True)
    (out_dir / "big.txt").write_text("line1\nline2\n", encoding="utf-8")

    result = await read_file(
        file_path=".hiveweave/tool_outputs/big.txt",
        offset=0,
        limit=50,
        workspace_path=str(wt),
        project_root=str(worktree_layout["project"]),
    )
    assert result["success"] is True
    assert "line1" in result["output"]


# ── detector unit tests ──────────────────────────────────────────────


def test_double_worktree_prefix_detector(
    worktree_layout: dict[str, Path],
) -> None:
    project = worktree_layout["project"]
    wt_a = worktree_layout["wt_a"]
    wt_b = worktree_layout["wt_b"]
    # own prefix repeated → flagged (returns the ghost-triggering segment)
    assert _double_worktree_prefix(
        str(wt_a), str(wt_a / ".hiveweave" / "worktrees" / "A044" / "x.py")
    ) == ".hiveweave"
    # 跨 id 幽灵（2026-08-13 审计 P1）：workspace 自身前缀后出现任意
    # .hiveweave 段即拒（不同 worktree id 各出现一次，同 id 计数漏判）
    assert _double_worktree_prefix(
        str(wt_a), str(wt_a / ".hiveweave" / "worktrees" / "A045" / "x.py")
    ) == ".hiveweave"
    # 多层交替跨 id（A044 树内 A045 再内 A046）同样拒
    deep = wt_a / ".hiveweave" / "worktrees" / "A045" \
        / ".hiveweave" / "worktrees" / "A046" / "x.py"
    assert _double_worktree_prefix(str(wt_a), str(deep)) == ".hiveweave"
    # normal own-worktree path → not flagged
    assert _double_worktree_prefix(str(wt_a), str(wt_a / "src" / "x.py")) is None
    # sibling worktree path → not flagged
    assert _double_worktree_prefix(str(wt_a), str(wt_b / "src" / "x.py")) is None
    # workspace root itself → not flagged
    assert _double_worktree_prefix(str(wt_a), str(wt_a)) is None
    # root workspace: doubled prefix under project-level worktrees → flagged
    doubled = project / ".hiveweave" / "worktrees" / "A044" / ".hiveweave" \
        / "worktrees" / "A044" / "x.py"
    assert _double_worktree_prefix(str(project), str(doubled)) == ".hiveweave"
    # root workspace 写单个合法 worktree 路径 → not flagged
    legit = project / ".hiveweave" / "worktrees" / "A044" / "src" / "x.py"
    assert _double_worktree_prefix(str(project), str(legit)) is None
    # 项目根 workspace 的 .hiveweave/shared 合法（平台自管共享目录）
    shared = project / ".hiveweave" / "shared" / "note.md"
    assert _double_worktree_prefix(str(project), str(shared)) is None
    # 大小写变体：casefold 命中（Windows 不敏感），.Hiveweave/worktrees 仍拒
    upper = wt_a / ".Hiveweave" / "worktrees" / "A044" / "x.py"
    assert _double_worktree_prefix(str(wt_a), str(upper)) == ".Hiveweave"
    # worktree workspace 内的平台自管目录（tool_outputs）合法——
    # 大输出落盘句柄契约依赖此路径可读（复审 P1）
    handle = wt_a / ".hiveweave" / "tool_outputs" / "big.txt"
    assert _double_worktree_prefix(str(wt_a), str(handle)) is None


@pytest.mark.asyncio
async def test_root_workspace_double_prefix_write_rejected(
    worktree_layout: dict[str, Path],
) -> None:
    """Coordinator (root workspace) writing into a nested ghost also blocked."""
    project = worktree_layout["project"]
    result = await write_file(
        file_path=".hiveweave/worktrees/A044/.hiveweave/worktrees/A044/x.py",
        content="x = 1",
        workspace_path=str(project),
    )
    assert result["success"] is False
    assert M4_ERROR in (result["error"] or "")
    assert not (project / ".hiveweave" / "worktrees" / "A044" / ".hiveweave").exists()


@pytest.mark.asyncio
async def test_worktree_workspace_cross_id_ghost_write_rejected(
    worktree_layout: dict[str, Path],
) -> None:
    """Worktree workspace 内写跨 id 幽灵（A044 内嵌 A045）必须被拒。

    2026-08-13 审计 P1：同 id 计数法漏判跨 id（各出现一次），实测可无限
    交替加深幽灵树。修复：相对路径再现 `.hiveweave/worktrees` 段即幽灵
    （复审放宽后仅 worktrees 段判幽灵，tool_outputs/shared 合法）。
    """
    wt_a = worktree_layout["wt_a"]
    result = await write_file(
        file_path=".hiveweave/worktrees/A045/src/x.py",
        content="x = 1",
        workspace_path=str(wt_a),
    )
    assert result["success"] is False
    assert M4_ERROR in (result["error"] or "")
    assert not (wt_a / ".hiveweave").exists()


@pytest.mark.asyncio
async def test_apply_patch_ghost_prefix_rejected(
    worktree_layout: dict[str, Path],
) -> None:
    from hiveweave.tools.patch import apply_patch

    wt = worktree_layout["wt_a"]
    result = await apply_patch(
        [{
            "op": "add",
            "filePath": ".hiveweave/worktrees/A044/x.py",
            "content": "x = 1",
        }],
        str(wt),
    )
    blob = (result.get("output") or "") + (result.get("error") or "")
    assert result["success"] is False
    assert M4_ERROR in blob
    assert not (wt / ".hiveweave").exists()


@pytest.mark.asyncio
async def test_pipeline_write_ghost_returns_blocked_hint(
    worktree_layout: dict[str, Path],
) -> None:
    import hiveweave.tools.file  # noqa: F401 — register write_file
    from hiveweave.tools.pipeline import execute_registered_tool

    class _Allow:
        async def evaluate_detailed(self, *a, **k):
            return "allow", None

    wt = worktree_layout["wt_a"]
    result = await execute_registered_tool(
        "write_file",
        {"filePath": ".hiveweave/worktrees/A044/x.py", "content": "x"},
        "agent-1",
        str(wt),
        _Allow(),
        None,
    )
    assert result is not None
    assert result["success"] is False
    assert result.get("blocked") is True
    assert M4_ERROR in (result["error"] or "")
