r"""P2-2 跟进（审计 P1-1）：项目根 `shared/` 必须被**物化**，否则"谁都能写网盘"不成立。

## 病灶（独立审计拟真形态实测）

`_materialize_shared_dir` 原先**只被 worktree 路径调用**（`service_create.py` 的 6 处
调用点全在创建/检查 worktree 的路径上）⇒ **存量/收养项目**里
`<proj>/.hiveweave/shared/` 可能根本不存在。而：

- `_ensure_standing_grants` 对**缺失目录刻意跳过**（fail-soft，有守卫
  `test_shared_absent_skip_then_backfill` 钉着）；
- MAIN 边界（CEO/HR）**没有** `.hiveweave` 写权 ⇒ **自己也建不了**。

⇒ 拟真实测：MAIN 边界 `mkdir .hiveweave\shared` → **exit=1**，
`echo > .hiveweave\shared\x.md` → exit=1。
即"谁都能写"只在**目录已存在**时成立 —— 而两个既有夹具（探针 §18 与
`test_acl_sandbox_shared.py` 的 `project` fixture）**都预建了该目录**，把这格掩盖了。

## 本文件守什么

`ensure_git_repo` 是**存量（早退分支）与新建（init 分支）的唯一入口** ⇒ 在它**分支之前**
物化即一次覆盖两条路。判据是**看盘**：

- ① 空目录 `shared/` 存在（否则 MAIN 边界授予被 skip）；
- ② `.keep.md` 存在（空目录 git 不跟踪 ⇒ 没有它就无法随 checkout 到 worktree）；
- ③ **两条分支都要**：先 init（新建形态），再删掉 `shared/` 重跑（存量形态）。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from hiveweave.services.git_worktree import GitWorktreeService


async def test_ensure_git_repo_materializes_netdisk_on_both_branches(
    tmp_path: Path,
) -> None:
    """① 新建分支物化；② 存量（`_has_git` 为真、走 early return）分支也物化。"""
    ws = tmp_path / "proj"
    ws.mkdir()
    svc = GitWorktreeService()
    shared = ws / ".hiveweave" / "shared"

    r1 = await svc.ensure_git_repo(str(ws))
    assert r1.get("success") is True, r1
    assert r1.get("initialized") is True, "前置：第一次应走 init 分支"
    assert shared.is_dir(), "项目根网盘必须被物化（否则 MAIN 边界写不进去）"
    assert (shared / ".keep.md").is_file(), (
        "空目录 git 不跟踪 ⇒ 必须有 .keep.md，否则 shared 不会随 checkout 到 worktree"
    )

    # ② 模拟**存量/收养**项目：有 `.git` 但没有 `shared/`
    shutil.rmtree(shared)
    assert not shared.exists(), "前置：先把它删掉，才是在测存量分支"
    r2 = await svc.ensure_git_repo(str(ws))
    assert r2.get("success") is True, r2
    assert r2.get("initialized") is False, "前置：第二次应走存量早退分支"
    assert shared.is_dir(), "存量/收养分支同样必须物化（这才是审计实测的缺口）"

    # 幂等：再跑一次不炸，`.keep.md` 不被改写（best-effort 不该抖动既有内容）
    keep = shared / ".keep.md"
    before = keep.read_text(encoding="utf-8")
    r3 = await svc.ensure_git_repo(str(ws))
    assert r3.get("success") is True
    assert keep.read_text(encoding="utf-8") == before
