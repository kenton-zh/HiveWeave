"""MAIN → worktree 同步（``git_worktree_sync`` 工具的服务端核心）。

对称补齐 worktree→MAIN 方向（service_merge / merge_support）已有的护栏：

- **dirty worktree 语义跟随 git_worktree_merge 既有约定**：merge_by_branch
  在 rebase 前对 worktree 自动 checkpoint（``add -A`` + ``diff --cached
  --quiet`` 判空 + ``commit -m pre-merge-checkpoint``）——本模块照抄同一
  策略，不发明新语义。
- **untracked 冲突隔离（sync 方向）**：MAIN incoming commits 将新增/修改
  的路径若与 worktree 里的 untracked 文件同路径 → 搬移到 MAIN 侧
  ``.hiveweave/merge-quarantine/<stamp>/``（与
  merge_support.quarantine_untracked_on_target 同一隔离区，未丢弃可恢复）。
  事件随回执透出，工具层据此发 inbox 通知（隔离不静默）。
- **merge-tree 预演两道门（冲突左移）**：
  第一道在 checkpoint/隔离之前（纯已提交态、零副作用拒绝）；第二道在
  checkpoint 之后、真 merge 之前（覆盖「worktree 未提交改动 vs MAIN 同
  路径修改」——第一道看不见未提交态，checkpoint 落成提交后才可预演）。
  第一道拒绝零副作用；第二道拒绝可能留下 pre-merge-checkpoint 提交
  （回执明示 checkpoint hash 与处方）；隔离搬移在 stuck 拒绝路径会按原
  路径搬回（搬回失败的文件保留在隔离区且回执列明）。
  基础设施失败（git 过旧/超时）fail-open 放行，与 merge 方向一致。
- **并发串行化**：按 ``workspace::short_id`` 复用 service_create 的
  ``_create_locks`` 模式加 asyncio.Lock 包住预演→隔离→checkpoint→merge
  段 —— 两个并发 sync（或 sync×merge）打同一 worktree 时 git 的
  index.lock 报错会被误归为 merge_failed。

merge 方向的 --no-ff 幂等约定（F13b）不适用本方向：sync 不删分支、不存在
「已合入分支再次 merge」的幂等判据问题，fast-forward 即裸 ``git merge
main`` 的自然语义。
"""
from __future__ import annotations

import asyncio
import shutil
import time as _time
from pathlib import Path

import structlog

from .conflict_predict import (
    _merge_tree,
    _parse_conflict_files,
    predict_merge_conflicts,
)
from .ensure import worktree_commits_behind_main
from .git_cmd import _current_branch, _git, _resolve_base_branch
from .merge_support import parse_untracked_overwrite

log = structlog.get_logger(__name__)

# P2-1: 同一 worktree 的 sync 串行化（service_create._create_locks 同款）。
# key = f"{workspace-resolved}::{short_id}"。
_sync_locks: dict[str, asyncio.Lock] = {}
_sync_locks_guard = asyncio.Lock()


async def _worktree_sync_lock(
    workspace_path: str, short_id: str
) -> asyncio.Lock:
    key = f"{Path(workspace_path).resolve()}::{short_id}"
    async with _sync_locks_guard:
        lock = _sync_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _sync_locks[key] = lock
    return lock


async def _head_short_async(worktree_path: str) -> str:
    """worktree 当前 HEAD 短 hash（best-effort，失败返回空串）。"""
    ok, out = await _git(["rev-parse", "--short", "HEAD"], worktree_path)
    return out.strip() if ok and out else ""


async def _incoming_paths_from_main(
    worktree_path: str, base: str
) -> list[str]:
    """MAIN 侧 incoming commits 将新增/修改的文件（三点 diff：merge-base..base）。

    两点 diff（``HEAD..base``）会把 worktree 侧自删的文件也算进来，方向
    不对；三点 diff 只看 base 一侧自分叉以来的变化，才是「合进来会碰的」。
    """
    ok, out = await _git(
        [
            "-c", "core.quotepath=false",
            "diff", "--name-only", f"HEAD...{base}",
        ],
        worktree_path,
    )
    if not ok:
        return []
    return [
        ln.strip().replace("\\", "/")
        for ln in (out or "").splitlines()
        if ln.strip()
    ]


async def _worktree_untracked_collisions(
    worktree_path: str, paths: list[str]
) -> list[str]:
    """paths 中在 worktree 里是「untracked 且文件存在」的子集。

    tracked 文件交给 git 三方合并自己处理；只有 untracked 实体文件会被
    merge 的 "untracked working tree files would be overwritten" 硬中止。
    """
    collisions: list[str] = []
    for rel in paths:
        src = Path(worktree_path) / rel
        if not src.is_file():
            continue  # 目录 / 不存在 — 不挡 merge
        ok_ls, ls_out = await _git(["ls-files", "--", rel], worktree_path)
        if ok_ls and (ls_out or "").strip():
            continue  # worktree index 已跟踪 — git 自己能合
        collisions.append(rel)
    return collisions


async def _quarantine_untracked_in_worktree(
    workspace_path: str, worktree_path: str, files: list[str]
) -> dict | None:
    """把挡路的 worktree untracked 文件搬进 MAIN 侧 merge-quarantine。

    隔离区与 merge 方向共用 ``.hiveweave/merge-quarantine/<stamp>/``
    （list_pending_quarantine_dirs / get_platform_state 由此可见）。
    全部搬移失败返回 None；返回 ``{stamp, dest, files}`` 事件供回执与
    inbox 通知使用。
    """
    stamp = _time.strftime("%Y%m%d-%H%M%S")
    dest_root = (
        Path(workspace_path) / ".hiveweave" / "merge-quarantine" / stamp
    )
    moved: list[str] = []
    for rel in files:
        src = Path(worktree_path) / rel
        if not src.exists():
            continue
        dest = dest_root / rel
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
            moved.append(rel.replace("\\", "/"))
        except OSError as e:
            log.warning(
                "git_worktree.sync_quarantine_failed", path=rel, error=str(e)
            )
    if not moved:
        return None
    log.info(
        "git_worktree.sync_quarantined_untracked",
        count=len(moved),
        dest=str(dest_root),
        files=moved[:12],
    )
    return {"stamp": stamp, "dest": str(dest_root), "files": moved}


async def _restore_quarantined_files(
    event: dict, worktree_path: str
) -> tuple[list[str], list[str]]:
    """P2-2: 把已搬进隔离区的文件按原路径搬回 worktree（stuck 拒绝路径回滚）。

    Returns ``(restored, kept)`` —— kept = 搬回失败、仍留在隔离区的文件
    （回执必须列明，不许静默吞）。
    """
    dest_root = Path(event.get("dest") or "")
    restored: list[str] = []
    kept: list[str] = []
    for rel in event.get("files") or []:
        src = dest_root / rel
        dst = Path(worktree_path) / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            restored.append(rel)
        except OSError as e:
            log.warning(
                "git_worktree.sync_quarantine_restore_failed",
                path=rel,
                error=str(e),
            )
            kept.append(rel)
    if not kept and dest_root.is_dir():
        # 全部搬回 — 清掉空 stamp 目录（best-effort；根目录非空时无害失败）
        try:
            dest_root.rmdir()
            dest_root.parent.rmdir()
        except OSError:
            pass
    return restored, kept


async def _predict_sync_conflicts(
    worktree_path: str, branch: str, base: str,
    project_root: str | None = None,
) -> tuple[bool, list[str]]:
    """merge-tree 预演 MAIN→worktree 内容冲突。返回 ``(conflicted, files)``。

    默认基分支走 predict_merge_conflicts（含降级/失败分类）；显式传入
    其它 base 时直接跑 _merge_tree。基础设施失败一律 fail-open 放行
    （与 merge 方向口径一致：只有明确报冲突才拦）。
    """
    default_base = await _resolve_base_branch(worktree_path)
    if (default_base or "main") == base:
        pred = await predict_merge_conflicts(
            worktree_path, project_root=project_root
        )
        if pred.status == "conflict":
            return True, list(pred.conflicts)
        return False, []
    rc, out = await _merge_tree(base, branch, worktree_path, project_root)
    if rc == 1:
        return True, _parse_conflict_files(out)
    return False, []


async def sync_main_into_worktree(
    workspace_path: str,
    short_id: str,
    *,
    base_branch: str | None = None,
) -> dict:
    """把 MAIN（默认基分支）的新提交同步进 agent 的 worktree。

    参数风格对齐同包 ``service_merge.merge(workspace_path, short_id, ...)``。
    调用方（工具层）负责 caller↔target 越权门 —— 本函数只认 short_id。

    流程（预演→隔离→checkpoint→merge 整段按 ``workspace::short_id`` 串行）：
    定位/校验 worktree → behind 计数（0 → up_to_date 幂等回执）→ merge-tree
    预演第一道（纯已提交态，拒绝零副作用）→ untracked 冲突隔离（必须在
    add -A 之前 —— 否则 checkpoint 会把 untracked 文件收编成 tracked，把
    add/add 撞车洗成内容冲突；stuck 拒绝路径会把已搬文件搬回）→ dirty
    worktree 自动 checkpoint（merge 方向同一约定，回执带 checkpoint hash）→
    merge-tree 预演第二道（覆盖 dirty-vs-MAIN 同路径冲突）→ ``git merge
    <base>`` → 新 HEAD 回执。

    Returns::
        {"success": True,  "merged": True,  "new_head", "behind_before",
         "quarantined": [{stamp, dest, files}...], "conflicts": [],
         "branch", "base", "message"}
        {"success": True,  "merged": False, "reason": "up_to_date", ...}
        {"success": False, "reason": ..., "message", "conflicts": [...],
         "quarantined": [...], ...}
    """
    from .service_merge import MergeMixin

    base = (base_branch or "").strip() or (
        await _resolve_base_branch(workspace_path) or "main"
    )

    def _reject(
        reason: str, message: str, *, branch: str = "",
        behind: int = 0, conflicts: list[str] | None = None,
        head: str = "", quarantined: list[dict] | None = None,
        extra: dict | None = None,
    ) -> dict:
        out = {
            "success": False,
            "merged": False,
            "reason": reason,
            "message": message,
            "branch": branch,
            "base": base,
            "behind_before": behind,
            "new_head": head,
            "quarantined": list(quarantined or []),
            "conflicts": list(conflicts or []),
        }
        if extra:
            out.update(extra)
        return out

    # ── 1. 前置：定位 worktree 与 MAIN ──
    ok_base, _ = await _git(
        ["rev-parse", "--verify", f"refs/heads/{base}"], workspace_path
    )
    if not ok_base:
        return _reject(
            "no_base_branch",
            f"Base branch '{base}' does not exist in the project repo — "
            "nothing to sync from.",
        )

    wt_path = await MergeMixin._resolve_effective_worktree_path(
        workspace_path, short_id
    )
    if not Path(wt_path).is_dir() or not Path(wt_path, ".git").exists():
        return _reject(
            "no_worktree",
            f"No live worktree found for {short_id} "
            f"(expected under .hiveweave/worktrees/{short_id}/). "
            "Worktrees are provisioned automatically on hire/dispatch.",
        )
    branch = await _current_branch(wt_path)
    if not branch:
        return _reject(
            "worktree_detached",
            f"Worktree for {short_id} ({wt_path}) is in detached HEAD state "
            "(no branch checked out). Trigger worktree repair before sync.",
        )

    # ── P2-1: 预演→隔离→checkpoint→merge 全段串行（同一 worktree 的并发
    #    sync/sync×merge 会让 git index.lock 报错被误归 merge_failed）。
    #    behind/up-to-date 判定也放锁内 —— 并发下第一个 sync 合完后，
    #    第二个必须重新看到 behind==0 才能给出正确回执。
    lock = await _worktree_sync_lock(workspace_path, short_id)
    async with lock:
        # ── 2. behind/ahead：0 → 幂等「已最新」回执 ──
        behind = await worktree_commits_behind_main(workspace_path, wt_path)
        head_before = await _head_short_async(wt_path)
        if behind <= 0:
            return {
                "success": True,
                "merged": False,
                "reason": "up_to_date",
                "message": (
                    f"Worktree {short_id} ({branch}) is already up to date "
                    f"with {base} — nothing to sync. Do not call "
                    "git_worktree_sync again until MAIN moves forward."
                ),
                "branch": branch,
                "base": base,
                "behind_before": 0,
                "new_head": head_before,
                "quarantined": [],
                "conflicts": [],
            }

        # ── 3. merge-tree 预演第一道（checkpoint/隔离之前）：拒绝路径
        #    零副作用（只看已提交态；未提交改动 vs MAIN 的冲突由第二道接）──
        conflicted, conflict_files = await _predict_sync_conflicts(
            wt_path, branch, base, project_root=workspace_path
        )
        if conflicted:
            listing = (
                f": {', '.join(conflict_files[:12])}"
                if conflict_files
                else " (git did not list files — treat every divergence as hot)"
            )
            return _reject(
                "merge_conflict_predicted",
                f"Sync refused: merging {base} into {branch} would conflict"
                f"{listing}. Nothing was merged and nothing was changed — "
                "your worktree HEAD is untouched. Fix in your worktree "
                "first: commit or resolve the conflicted file(s) on your "
                "branch, then call git_worktree_sync again.",
                branch=branch,
                behind=behind,
                conflicts=conflict_files,
                head=await _head_short_async(wt_path),
            )

        # ── 4. untracked 冲突护栏（sync 方向）：搬移到隔离区（未丢弃可恢复）。
        #    必须先于 dirty checkpoint —— add -A 会把 untracked 撞车文件收编
        #    进 checkpoint 提交，护栏就永远打不中了。──
        quarantined: list[dict] = []
        incoming = await _incoming_paths_from_main(wt_path, base)
        collisions = await _worktree_untracked_collisions(wt_path, incoming)
        if collisions:
            event = await _quarantine_untracked_in_worktree(
                workspace_path, wt_path, collisions
            )
            if event:
                quarantined.append(event)
            moved_set = set(event["files"]) if event else set()
            stuck = [p for p in collisions if p not in moved_set]
            if stuck:
                # P2-2: 部分隔离失败 → 把已搬的搬回原路径再拒绝（不留半吊子）。
                restored: list[str] = []
                kept: list[str] = []
                if event:
                    restored, kept = await _restore_quarantined_files(
                        event, wt_path
                    )
                quarantined = (
                    [{**event, "files": kept}] if kept else []
                )
                kept_note = (
                    f" Files that could not be restored stay quarantined at "
                    f"{event['dest']}: {', '.join(kept[:12])}."
                    if kept
                    else " All previously moved files were restored to your "
                         "worktree — nothing was lost."
                )
                return _reject(
                    "untracked_overwrite",
                    "Sync refused: MAIN's incoming commits would overwrite "
                    f"untracked file(s) in your worktree and they could not "
                    f"be quarantined: {', '.join(stuck[:12])}. Move or commit "
                    "them manually, then retry git_worktree_sync."
                    + kept_note,
                    branch=branch,
                    behind=behind,
                    head=await _head_short_async(wt_path),
                    quarantined=quarantined,
                )

        # ── 5. dirty worktree：跟随 git_worktree_merge 既有约定（自动
        #    checkpoint，空变更跳过）。checkpoint 失败必须硬拒 —— staged
        #    改动会让 merge 直接中止（merge 方向合的是 MAIN 检出，无此
        #    约束，sync 有）。
        checkpoint_commit: str | None = None
        await _git(["add", "-A"], wt_path)
        ok_diff, _ = await _git(["diff", "--cached", "--quiet"], wt_path)
        if not ok_diff:  # exit 1 = 有暂存改动
            ok_ci, ci_out = await _git(
                ["commit", "-m", "pre-merge-checkpoint"], wt_path
            )
            if not ok_ci:
                return _reject(
                    "worktree_dirty",
                    "Your worktree has uncommitted changes and the "
                    "automatic pre-merge checkpoint failed: "
                    f"{(ci_out or '')[:300]}. Run git_worktree_checkpoint "
                    "(or commit) first, then retry git_worktree_sync.",
                    branch=branch,
                    behind=behind,
                    head=await _head_short_async(wt_path),
                    quarantined=quarantined,
                )
            checkpoint_commit = await _head_short_async(wt_path)
            log.info(
                "git_worktree.sync_pre_merge_checkpoint",
                short_id=short_id, branch=branch, checkpoint=checkpoint_commit,
            )

        # ── 6. merge-tree 预演第二道（checkpoint 之后、真 merge 之前）：
        #    第一道只看已提交态，「worktree 未提交改动 vs MAIN 同路径修改」
        #    的冲突在此（checkpoint 落成提交后）才可预判。拒绝路径可能
        #    留下 checkpoint 提交 —— 回执明示 hash 与处方，不算静默副作用。
        conflicted, conflict_files = await _predict_sync_conflicts(
            wt_path, branch, base, project_root=workspace_path
        )
        if conflicted:
            listing = (
                f": {', '.join(conflict_files[:12])}"
                if conflict_files
                else " (git did not list files — treat every divergence as hot)"
            )
            return _reject(
                "merge_conflict_predicted",
                f"Sync refused: merging {base} into {branch} would conflict"
                f"{listing}. Nothing was merged. "
                + (
                    f"Your uncommitted changes were auto-checkpointed "
                    f"(commit {checkpoint_commit}); resolve the conflicts "
                    f"with MAIN, then call git_worktree_sync again."
                    if checkpoint_commit
                    else "Resolve the conflicted file(s) on your branch, "
                         "then call git_worktree_sync again."
                ),
                branch=branch,
                behind=behind,
                conflicts=conflict_files,
                head=await _head_short_async(wt_path),
                quarantined=quarantined,
                extra=(
                    {"post_checkpoint": True, "checkpoint": checkpoint_commit}
                    if checkpoint_commit
                    else None
                ),
            )

        # ── 7. 执行：git merge <base>（fast-forward 即自然语义，无需 --no-ff）──
        ok_merge, merge_out = await _git(["merge", base, "--no-edit"], wt_path)
        if not ok_merge:
            # 与 merge 方向同款护栏：untracked 撞车 → 隔离后重试一次
            untracked = parse_untracked_overwrite(merge_out or "")
            await _git(["merge", "--abort"], wt_path)
            if untracked:
                event = await _quarantine_untracked_in_worktree(
                    workspace_path, wt_path, untracked
                )
                if event:
                    quarantined.append(event)
                    ok_merge, merge_out = await _git(
                        ["merge", base, "--no-edit"], wt_path
                    )
            if not ok_merge:
                ok_u, u_out = await _git(
                    ["diff", "--name-only", "--diff-filter=U"], wt_path
                )
                conflict_files = [
                    f.strip().replace("\\", "/")
                    for f in (u_out or "").splitlines()
                    if f.strip()
                ] if ok_u else []
                await _git(["merge", "--abort"], wt_path)  # 幂等：无 merge 态无害
                if conflict_files:
                    return _reject(
                        "merge_conflict",
                        f"Merging {base} into {branch} hit content "
                        f"conflicts: {', '.join(conflict_files[:12])}. The "
                        "merge was aborted — resolve the conflicted file(s) "
                        "in your worktree, checkpoint, then retry "
                        "git_worktree_sync.",
                        branch=branch,
                        behind=behind,
                        conflicts=conflict_files,
                        head=await _head_short_async(wt_path),
                        quarantined=quarantined,
                    )
                return _reject(
                    "merge_failed",
                    f"Merging {base} into {branch} failed (not a content "
                    f"conflict):\n{(merge_out or '')[:600]}",
                    branch=branch,
                    behind=behind,
                    head=await _head_short_async(wt_path),
                    quarantined=quarantined,
                )

        new_head = await _head_short_async(wt_path)
        log.info(
            "git_worktree.sync_main_into_worktree",
            short_id=short_id,
            branch=branch,
            base=base,
            behind_before=behind,
            new_head=new_head,
            quarantined=len(quarantined),
        )
        msg = (
            f"Synced {base} into worktree {short_id} ({branch}): "
            f"{behind} commit(s) behind before, new HEAD {new_head or '?'}."
        )
        if checkpoint_commit:
            msg += (
                f" Uncommitted changes were auto-checkpointed first "
                f"(commit {checkpoint_commit})."
            )
        if quarantined:
            qf = [f for e in quarantined for f in e["files"]]
            msg += (
                f" {len(qf)} untracked file(s) were moved to "
                f"{quarantined[0]['dest']} (not deleted, recoverable — see "
                "the [WORKTREE SYNC QUARANTINE] inbox notice)."
            )
        return {
            "success": True,
            "merged": True,
            "new_head": new_head,
            "behind_before": behind,
            "branch": branch,
            "base": base,
            "quarantined": quarantined,
            "conflicts": [],
            "message": msg,
        }
