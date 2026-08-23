"""SID 派生单测（spec §4.3）。纯函数，跨平台可跑。"""

from __future__ import annotations

from pathlib import Path

from hiveweave.services.acl_sandbox.sid import (
    cache_sid,
    extra_sid,
    git_sid,
    project_root_sid,
    temp_sid,
    worktree_sid,
)


def test_worktree_and_project_root_share_derivation(tmp_path: Path) -> None:
    """worktree / 项目根同用空前缀 —— 路径即边界（§5.5 定则）。"""
    assert worktree_sid(str(tmp_path)) == project_root_sid(str(tmp_path))


def test_domain_separation_same_path(tmp_path: Path) -> None:
    """同一路径下五类域必须互不撞车（cache/git/temp/extra 各有前缀）。"""
    p = str(tmp_path)
    sids = {
        worktree_sid(p),
        cache_sid(p),
        git_sid(p),
        temp_sid(p),
        extra_sid(p),
    }
    assert len(sids) == 5, f"domain separation broken: {sids}"


def test_temp_extra_subauthority(tmp_path: Path) -> None:
    """temp SID 带 extra=(1,)（对齐 DSH）—— 与无 extra 的派生区分。"""
    a = temp_sid(str(tmp_path))
    b = temp_sid(str(tmp_path / "other"))
    assert a != b


def test_deterministic_same_input(tmp_path: Path) -> None:
    """同路径重复派生必须一致（standing ACE 幂等跳过的基础）。"""
    p = str(tmp_path)
    assert worktree_sid(p) == worktree_sid(p)
    assert cache_sid(p) == cache_sid(p)
    assert git_sid(p) == git_sid(p)


def test_separator_convergence(tmp_path: Path) -> None:
    """正反斜杠 + 尾斜杠在 realpath 下收敛为同一 SID。"""
    p = tmp_path / "sub" / "dir"
    p.mkdir(parents=True)
    a = worktree_sid(str(p))
    b = worktree_sid(str(p).replace("\\", "/"))
    c = worktree_sid(str(p) + "\\")
    assert a == b == c


def test_path_rename_generates_new_sid(tmp_path: Path) -> None:
    """路径改名 → 新 SID（旧 standing ACE 惰性残留无害，§7.4）。"""
    p1 = tmp_path / "alpha"
    p2 = tmp_path / "beta"
    p1.mkdir()
    p2.mkdir()
    assert worktree_sid(str(p1)) != worktree_sid(str(p2))


def test_cross_project_uniqueness(tmp_path: Path) -> None:
    """不同项目根 → 不同 SID（60-bit 碰撞 ~2⁻⁵⁴，项目间写隔离基础）。"""
    proj_a = tmp_path / "proj-a"
    proj_b = tmp_path / "proj-b"
    proj_a.mkdir()
    proj_b.mkdir()
    assert cache_sid(str(proj_a)) != cache_sid(str(proj_b))
    assert git_sid(str(proj_a)) != git_sid(str(proj_b))


def test_realpath_absorbs_dotdot(tmp_path: Path) -> None:
    """存在的路径经 .. 归一后与直写等价。"""
    real = tmp_path / "sub"
    real.mkdir()
    indirect = f"{tmp_path}{'\\..'}{'\\'}{tmp_path.name}\\sub"
    assert worktree_sid(str(real)) == worktree_sid(indirect)


def test_sid_format() -> None:
    """SID 必须落在 S-1-4-<a>-<b>（能力域）形态。"""
    s = worktree_sid(str(__file__))
    parts = s.split("-")
    assert parts[:3] == ["S", "1", "4"]
    assert len(parts) == 5
    assert all(p.isdigit() for p in parts[3:])
