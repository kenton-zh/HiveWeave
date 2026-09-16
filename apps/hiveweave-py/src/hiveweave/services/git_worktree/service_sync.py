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

## ③（2026-09-16）：半合并态第一次成为合法状态，因此必须被全链识别

此前平台**从不制造**半合并态（两个方向失败即 ``merge --abort``），所以
"worktree 正处于 merge 中"这个状态在本仓**零处理**（``MERGE_HEAD`` 全仓
0 命中）。``mode=materialize_conflict`` 把它变成**agent 主动要求**的状态，
于是新增两件事：

1. 本模块：``merge``/``materialize`` 在已处于 merge 态时**硬拒**（``reason=
   merge_in_progress``）并给两条出路；``abort`` 是显式退出入口（幂等，
   ``no_merge_in_progress`` 是成功而不是错误）。
2. ``service_create.checkpoint``：半合并态下**拒绝 checkpoint**（``add -A``
   一条未解决路径即视为"已解决"，会把冲突标记提交成正常提交）。

判据一律是 ``git rev-parse --verify --quiet MERGE_HEAD``（状态），不是
"index 里有 UU"、更不是文案。
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
from .git_identity import agent_identity_args
from .git_cmd import (
    _current_branch,
    _git,
    _resolve_base_branch,
    merge_in_progress,
    unmerged_paths,
)
from .merge_support import parse_untracked_overwrite

log = structlog.get_logger(__name__)

# ── ③（2026-09-16）：三个显式模式 ────────────────────────────────────
# 为什么要有模式而不是"再多一条禁令"：`不要裸 git merge main` 原先只活在
# prompts/工具描述里（`services/policy.py` 无任何规则拒它）—— 按 DSH 的宪法
# `packages/AGENTS.md:14`「Enforce a decision in the operation that makes it.
# … prompt filtering … are not enforcement」，**那根本不算约束**。真实需求
# 是"把冲突制造出来、在本地手工解"，那就把这件事做成**操作**：
#   merge（默认）= 现状（冲突左移拒绝，不留半成品）
#   materialize_conflict = 跳过两道预检、真跑 merge、**冲突留在树里**
#   abort = 从半合并态退出（`merge --abort` 的显式入口）
SYNC_MODE_MERGE = "merge"
SYNC_MODE_MATERIALIZE = "materialize_conflict"
SYNC_MODE_ABORT = "abort"
SYNC_MODES = frozenset({SYNC_MODE_MERGE, SYNC_MODE_MATERIALIZE, SYNC_MODE_ABORT})


def materialize_prescription(base: str, branch: str) -> str:
    """半合并态的**两条出路**（唯一一份文案 —— 3–4 处拒绝共用它）。

    形状照 DSH 的 `GoalBlockReason {code, message}`：判据留 `reason`（稳定
    code，下游与审计按它分支/统计），人读的处方在这一份里。
    """
    return (
        f"Your worktree ({branch}) is mid-merge with {base}: git left the "
        "conflict in place for you to resolve by hand. Two exits — "
        "(1) resolve: edit the conflicted file(s), remove every conflict "
        "marker, `git add <files>`, then commit; "
        "(2) abandon: call git_worktree_sync with mode=abort to return the "
        "worktree to its pre-merge HEAD. Do NOT leave it unresolved: "
        "checkpoint refuses while a merge is in progress (it would commit "
        "the conflict markers as if they were finished work)."
    )

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
    mode: str = SYNC_MODE_MERGE,
) -> dict:
    """把 MAIN（默认基分支）的新提交同步进 agent 的 worktree。

    参数风格对齐同包 ``service_merge.merge(workspace_path, short_id, ...)``。
    调用方（工具层）负责 caller↔target 越权门 —— 本函数只认 short_id。

    ``mode``（③ 2026-09-16）：
    - ``merge``（默认）= 冲突左移（两道 merge-tree 预检），拒绝时**不留半成品**；
    - ``materialize_conflict`` = **跳过两道预检**、真跑 merge，把冲突**留在树里**
      交给 agent 手工解（回执给未解决路径清单 + 两条出路）。untracked 撞车的
      隔离护栏与 dirty 自动 checkpoint **照旧**（前者防丢文件、后者是 merge
      能跑起来的前提）；
    - ``abort`` = 从半合并态退出（``git merge --abort`` 的显式入口，幂等）。

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
        {"success": True,  "merged": False, "state": "conflict_materialized",
         "conflicted": [路径...], "conflicts": [路径...], ...}   # mode=materialize
        {"success": True,  "merged": False, "state": "aborted" |
         "no_merge_in_progress", ...}                            # mode=abort
        {"success": False, "reason": ..., "message", "conflicts": [...],
         "quarantined": [...], ...}
    """
    from .service_merge import MergeMixin

    # 未知 mode 一律 fail-loud：静默回落到 merge 会让"我要制造冲突"变成
    # 一次被拒的普通 sync，而 agent 从回执上看不出自己参数写错了。
    if mode not in SYNC_MODES:
        return {
            "success": False,
            "merged": False,
            "reason": "bad_mode",
            "message": (
                f"Unknown sync mode {mode!r}. Use one of: "
                f"{', '.join(sorted(SYNC_MODES))}."
            ),
            "branch": "", "base": "", "behind_before": 0, "new_head": "",
            "quarantined": [], "conflicts": [],
        }

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
        # ── 1.5 半合并态（③）：`merge`/`materialize` 在"已经处于 merge 态"时
        #    必须**硬拒并给两条出路**，不能往下走 —— 否则 `git merge` 自己会报
        #    "You have not concluded your merge"，被归成 merge_failed（文案指向
        #    不存在的"非内容冲突"，agent 只能盲试）；`abort` 则是明确入口。
        mid = await merge_in_progress(wt_path, workspace_path)
        if mode == SYNC_MODE_ABORT:
            if not mid:
                return {
                    "success": True,
                    "merged": False,
                    "state": "no_merge_in_progress",
                    "reason": "no_merge_in_progress",
                    "message": (
                        f"Worktree {short_id} ({branch}) is not mid-merge — "
                        "there is nothing to abort. HEAD unchanged."
                    ),
                    "branch": branch, "base": base, "behind_before": 0,
                    "new_head": await _head_short_async(wt_path),
                    "conflicted": [], "quarantined": [], "conflicts": [],
                }
            ok_ab, ab_out = await _git(["merge", "--abort"], wt_path)
            # ⚠ 判据只看**状态**（merge 态是否真的消失），**不 OR 退出码**：
            # `git merge --abort` 会返回非 0 而 MERGE_HEAD 已被清掉（"已经没
            # 有可 abort 的东西"这类），OR 上去就把成功报成 still-mid-merge
            # （假失败，agent 重试永远撞同一句）。`ok_ab` 只用于给原因文本。
            if await merge_in_progress(wt_path, workspace_path):
                return _reject(
                    "abort_refused",
                    "Could not abort the in-progress merge: "
                    f"{(ab_out or '').strip()[:400] or 'git reported nothing'}"
                    f" (rc={ok_ab}). The worktree is still mid-merge — move or "
                    "commit the blocking files, then retry mode=abort.",
                    branch=branch,
                    behind=await worktree_commits_behind_main(
                        workspace_path, wt_path
                    ),
                    head=await _head_short_async(wt_path),
                )
            return {
                "success": True,
                "merged": False,
                "state": "aborted",
                "reason": "aborted",
                "message": (
                    f"Aborted the in-progress merge in {short_id} "
                    f"({branch}). HEAD is back to "
                    f"{await _head_short_async(wt_path)} and the worktree "
                    "index is clean."
                ),
                "branch": branch, "base": base,
                "behind_before": await worktree_commits_behind_main(
                    workspace_path, wt_path
                ),
                "new_head": await _head_short_async(wt_path),
                "conflicted": [], "quarantined": [], "conflicts": [],
            }
        if mid:
            return _reject(
                "merge_in_progress",
                "Sync refused: this worktree is already mid-merge. "
                + materialize_prescription(base, branch),
                branch=branch,
                behind=await worktree_commits_behind_main(
                    workspace_path, wt_path
                ),
                conflicts=await unmerged_paths(wt_path, workspace_path),
                head=await _head_short_async(wt_path),
                extra={"state": "merge_in_progress"},
            )

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
        #    零副作用（只看已提交态；未提交改动 vs MAIN 的冲突由第二道接）。
        #    ⚠ materialize 模式**刻意跳过**：它就是来要这个冲突的，"提前拒绝"
        #    等于把需求本身拒掉（真正的护栏是回执给清单 + checkpoint 的半合并
        #    态闸门，不是"不许产生冲突"）。──
        conflicted, conflict_files = (
            (False, [])
            if mode == SYNC_MODE_MATERIALIZE
            else await _predict_sync_conflicts(
                wt_path, branch, base, project_root=workspace_path
            )
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
                "branch, then call git_worktree_sync again. To resolve the "
                "conflict by hand instead, call git_worktree_sync with "
                "mode=materialize_conflict — it merges and leaves the "
                "conflict in your tree.",
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
                [*await agent_identity_args(short_id),
                 "commit", "-m", "pre-merge-checkpoint"],
                wt_path,
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
        #    ⚠ materialize 模式同样跳过（理由同第一道）。──
        conflicted, conflict_files = (
            (False, [])
            if mode == SYNC_MODE_MATERIALIZE
            else await _predict_sync_conflicts(
                wt_path, branch, base, project_root=workspace_path
            )
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
                    f"with MAIN, then call git_worktree_sync again. To "
                    f"resolve them by hand, call git_worktree_sync with "
                    f"mode=materialize_conflict."
                    if checkpoint_commit
                    else "Resolve the conflicted file(s) on your branch, "
                         "then call git_worktree_sync again (or use "
                         "mode=materialize_conflict to resolve by hand)."
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
            # ⚠ **顺序即判据**：状态必须在任何 `merge --abort` **之前**读。
            #   旧实现先无条件 abort，再读 `--diff-filter=U` ⇒ index 已干净，
            #   清单恒空 ⇒ 真正的内容冲突被归到 `merge_failed`（"not a content
            #   conflict"，把 agent 引向错误方向），而 `merge_conflict` 分支几乎
            #   不可达。本批把读取提前（错误标签同时被修正）。
            mid = await merge_in_progress(wt_path, workspace_path)
            unmerged = await unmerged_paths(wt_path, workspace_path) if mid else []
            untracked = parse_untracked_overwrite(merge_out or "")

            async def _materialized(files: list[str]) -> dict:
                """把"冲突已留在树里"的回执收成一个构造点（**两处调用**：
                首次 merge 与 untracked 隔离后的重试 —— 两处条件必须同源，
                否则「该留的冲突被 abort 掉」这种缺口只会在一侧被修）。"""
                log.info(
                    "git_worktree.sync_conflict_materialized",
                    short_id=short_id, branch=branch, base=base,
                    files=len(files),
                )
                return {
                    "success": True,
                    "merged": False,
                    "state": "conflict_materialized",
                    "reason": "conflict_materialized",
                    "message": (
                        f"Merging {base} into {branch} produced content "
                        f"conflicts in {len(files)} file(s) — left in your "
                        f"worktree for you to resolve: "
                        f"{', '.join(files[:12])}"
                        + (f" (checkpoint {checkpoint_commit} was created "
                           f"first)" if checkpoint_commit else "")
                        + ". " + materialize_prescription(base, branch)
                    ),
                    "branch": branch,
                    "base": base,
                    "behind_before": behind,
                    "new_head": await _head_short_async(wt_path),
                    "conflicted": files,
                    "conflicts": files,
                    "quarantined": quarantined,
                    **({"post_checkpoint": True,
                        "checkpoint": checkpoint_commit}
                       if checkpoint_commit else {}),
                }

            if mid and mode == SYNC_MODE_MATERIALIZE:
                # materialize：**刻意不 abort** —— 冲突留在树里。success=True
                # 因为操作**完成了它被要求的事**（`merged=False` + `state`
                # 表达"合并没结束"，不是失败）。
                return await _materialized(unmerged)
            # 与 merge 方向同款护栏：untracked 撞车 → 隔离后重试一次
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
                    # ⚠ **重试后必须重算状态**：第一次失败是 untracked 撞车
                    # （git 根本没进 merge 态 ⇒ 上面两个判据都是空的），重试才
                    # 可能撞上内容冲突。复用第一次的 `unmerged`（恒空）会让
                    # materialize **把该留的冲突 abort 掉**，并把它误标成
                    # merge_failed（"not a content conflict"）—— 正是本批要修的
                    # 那个错误标签，换条路又回来了（审计实测）。
                    mid = await merge_in_progress(wt_path, workspace_path)
                    unmerged = (
                        await unmerged_paths(wt_path, workspace_path)
                        if mid else []
                    )
                    if mid and mode == SYNC_MODE_MATERIALIZE:
                        return await _materialized(unmerged)
            if not ok_merge:
                await _git(["merge", "--abort"], wt_path)  # 幂等：无 merge 态无害
                if unmerged:
                    return _reject(
                        "merge_conflict",
                        f"Merging {base} into {branch} hit content "
                        f"conflicts: {', '.join(unmerged[:12])}. The "
                        "merge was aborted — resolve the conflicted file(s) "
                        "in your worktree, checkpoint, then retry "
                        "git_worktree_sync (or use mode=materialize_conflict "
                        "to get the conflict left in your tree).",
                        branch=branch,
                        behind=behind,
                        conflicts=unmerged,
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
