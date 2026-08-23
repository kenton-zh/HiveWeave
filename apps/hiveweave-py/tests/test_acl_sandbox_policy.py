"""policy 单测（spec §5.5/§5.6）：边界源解析 + SID 组装。跨平台。"""

from __future__ import annotations

import pytest

from hiveweave.services.acl_sandbox.policy import (
    ENTRY_BOUNDARY,
    SANDBOX_TEMP_REL,
    build_write_sids,
    resolve_policy,
    resolve_temp_dir,
    resolve_temp_sid,
)
from hiveweave.services.acl_sandbox.sid import (
    cache_sid,
    extra_sid,
    git_sid,
    temp_sid,
    worktree_sid,
)


def test_entry_boundary_covers_all_six_rows() -> None:
    """§5.7 六入口全部登记（bash 两行共享 bash 键）。"""
    assert ENTRY_BOUNDARY == {
        "bash": "boundary",
        "bash_main": "project_root",
        "run_command": "boundary",
        "dev_server": "boundary",
        "alarm": "project_root",
    }


def test_build_write_sids_structure(tmp_path) -> None:
    """组装顺序/构成：boundary + cache(项目根) + git(项目根) + temp（§3 一览）。"""
    boundary = str(tmp_path / "worktree-A")
    project = str(tmp_path)
    t = temp_sid(str(tmp_path / "t"))
    sids = build_write_sids(boundary, project, t)
    assert sids == [
        worktree_sid(boundary),   # 边界 SID 派生自 worktree
        cache_sid(project),       # cache/git SID 派生自**项目根**（§4.8/§8）
        git_sid(project),
        t,
    ]


def test_build_write_sids_project_root_separate_from_boundary(tmp_path) -> None:
    """executor 形态：边界=worktree，git/cache SID 却必须来自项目根。

    §4.8/§8：同项目全 agent 共享同一 git/cache 能力 —— 若从 worktree 派生，
    每个 worktree 一套 SID，项目级 `.git`/`.hiveweave-cache` 就没人对得上。
    """
    project = str(tmp_path)
    wt_a = str(tmp_path / ".hiveweave" / "worktrees" / "A")
    wt_b = str(tmp_path / ".hiveweave" / "worktrees" / "B")
    t = temp_sid(str(tmp_path / "t"))
    a = build_write_sids(wt_a, project, t)
    b = build_write_sids(wt_b, project, t)
    # 边界 SID 不同（A/B worktree），但 cache/git SID 相同（同一项目根）
    assert a[0] != b[0]
    assert a[1] == b[1]  # cache SID 同
    assert a[2] == b[2]  # git SID 同


def test_build_write_sids_extra_dirs(tmp_path) -> None:
    """附加可写目录（§5.5b②）追加 extra SID，域前缀独立。"""
    boundary = str(tmp_path)
    project = str(tmp_path)
    extra = str(tmp_path / "ext")
    t = temp_sid(str(tmp_path / "t"))
    sids = build_write_sids(boundary, project, t, extra_dirs=(extra,))
    assert sids[-1] == extra_sid(extra)
    # extra SID 与其余域不撞
    assert len(set(sids)) == 5


def test_resolve_temp_dir_nesting(tmp_path) -> None:
    """§4.12：私有 temp 一律在 workspace 内，不落 %TEMP%。"""
    root = str(tmp_path)
    assert resolve_temp_dir(root, "A001") == str(
        tmp_path / SANDBOX_TEMP_REL / "A001"
    )


def test_resolve_temp_sid(tmp_path) -> None:
    root = str(tmp_path)
    tdir = resolve_temp_dir(root, "A001")
    assert resolve_temp_sid(tdir) == temp_sid(tdir)


def test_resolve_policy_full(tmp_path) -> None:
    p = resolve_policy(workspace_path=str(tmp_path), agent_id="A001")
    assert p.boundary_root == str(tmp_path)
    assert p.project_root == str(tmp_path)  # 缺省 project_workspace_path → 回退边界
    assert p.temp_dir == resolve_temp_dir(str(tmp_path), "A001")
    assert p.temp_sid == p.write_sids[-1]
    assert len(p.write_sids) == 4


def test_resolve_policy_project_root_override(tmp_path) -> None:
    """显式传入项目根：cache_dir 与 cache/git SID 走项目根。"""
    project = str(tmp_path)
    wt = str(tmp_path / ".hiveweave" / "worktrees" / "A")
    p = resolve_policy(workspace_path=wt, agent_id="A001",
                       project_workspace_path=project)
    assert p.boundary_root == wt
    assert p.project_root == project
    assert p.cache_dir == str(tmp_path / ".hiveweave-cache")
    assert p.write_sids[1] == cache_sid(project)
    assert p.write_sids[2] == git_sid(project)


def test_resolve_policy_unknown_entry_fail_closed(tmp_path) -> None:
    with pytest.raises(ValueError):
        resolve_policy(workspace_path=str(tmp_path), agent_id="A001", entry="bogus")
