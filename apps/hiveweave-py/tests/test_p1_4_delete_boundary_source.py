"""P1-4：删除落点守卫必须能以**执行侧授权树**为边界（ACL 与守卫同一来源）。

病灶（§10.6 更正②③）：ACL 的写 SID 由调用方 `workspace_path` 派生
（`acl_sandbox/policy.py:337`），而删除落点守卫**只从 agent 身份**派生
（`agent_worktree_path`）⇒ **同一事实被判了两次**。`pwsh_main` 场景 ACL 授的是 MAIN
（`entry.py` 传 `workspace_path=MAIN`），守卫却按**自有 worktree** 判 ⇒ 在 MAIN 里
删自己刚写的文件被拒（实测 DB：`pwsh_main/confined` 37 行）。

判据（状态判据，四条）：
- **AC2 四格矩阵**：{目标∈MAIN, 目标∈WT} × {显式边界=MAIN, 缺省(→WT)}，与「执行目录语义」一致；
- **AC3 唯一来源**：显式 `boundary_root=realpath(X)` 的结论 == 直接以 `realpath(X)` 为边界
  调 `resolve_delete_landing` 的结论；
- **缺省不动**：不传 `boundary_root` ⇒ 一字不动地保留旧行为（目标∈MAIN 仍 deny）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hiveweave.services import command_guard as cg


@pytest.fixture
def ctx(monkeypatch, tmp_path: Path):
    main = tmp_path / "MAIN"
    wt = tmp_path / "WT"
    main.mkdir()
    wt.mkdir()

    async def get_agent(_aid):
        return {"project_id": "p1"}

    async def worktree_of(_aid):
        return str(wt)

    async def project_root(_pid):
        return str(main)

    async def extra_dirs(_boundary):
        return ()

    monkeypatch.setattr("hiveweave.db.meta.get_agent_by_id", get_agent)
    monkeypatch.setattr(
        "hiveweave.services.worktree_review.agent_worktree_path", worktree_of
    )
    monkeypatch.setattr(
        "hiveweave.services.acl_sandbox.integration.resolve_project_root",
        project_root,
    )
    monkeypatch.setattr(
        "hiveweave.services.acl_sandbox.integration.fetch_additional_writable_dirs",
        extra_dirs,
    )
    return main, wt


async def _verdict(cmd: str, boundary: str | None):
    return await cg.resolve_delete_landing_for_agent(
        cmd, agent_id="a1", cwd=boundary, boundary_root=boundary
    )


@pytest.mark.asyncio
async def test_ac2_matrix_explicit_boundary_is_main(ctx):
    main, wt = ctx
    # 目标∈MAIN + 显式边界=MAIN ⇒ allow（这是 P1-4 要修的格）
    assert await _verdict(f"rm -rf {main / 'a.txt'}", str(main)) is None
    # 目标∈WT + 显式边界=MAIN ⇒ deny（真的越界）
    v = await _verdict(f"rm -rf {wt / 'b.txt'}", str(main))
    assert v is not None and v.blocked and v.rule == "__delete_out_of_boundary__"


@pytest.mark.asyncio
async def test_ac2_matrix_default_boundary_stays_worktree(ctx):
    main, wt = ctx
    # 缺省（不传 boundary_root）⇒ 按 agent 身份 = WT
    assert await _verdict(f"rm -rf {wt / 'b.txt'}", None) is None
    v = await _verdict(f"rm -rf {main / 'a.txt'}", None)
    assert v is not None and v.blocked, "缺省行为必须一字不动（目标∈MAIN 仍 deny）"


@pytest.mark.asyncio
async def test_ac3_single_source_with_explicit_boundary(ctx):
    """显式 `boundary_root=realpath(X)` 的结论 == 直接以 realpath(X) 为边界的结论。"""
    import os

    main, _wt = ctx
    cmd = f"rm -rf {main / 'a.txt'}"
    via_agent = await _verdict(cmd, str(main))
    direct = cg.resolve_delete_landing(
        cmd,
        boundary_root=os.path.realpath(str(main)),
        temp_dir=str(main / ".hiveweave" / "sandbox-temp" / "a1"),
        extra_dirs=(),
        cwd=os.path.realpath(str(main)),
    )
    assert (via_agent is None) == (direct is None)


@pytest.mark.asyncio
async def test_relative_escape_still_denied_under_explicit_boundary(ctx):
    """AC3（第二半）：`../outside` 在显式边界下**仍必须 deny**。"""
    main, _wt = ctx
    v = await cg.resolve_delete_landing_for_agent(
        "rm -rf ../outside", agent_id="a1", cwd=str(main), boundary_root=str(main)
    )
    assert v is not None and v.blocked, "显式边界不得把相对越界放行"
