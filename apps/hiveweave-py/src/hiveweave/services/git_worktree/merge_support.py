"""Merge failure classification and untracked quarantine."""
from __future__ import annotations

import shutil
from pathlib import Path

import structlog

from .constants import (
    CHECKPOINT_PREFIX,
    TRACKED_WS_DIRS,
    _UNTRACKED_FILE_LINE_RE,
    _UNTRACKED_OVERWRITE_RE,
    is_generated_path,
)
from .git_cmd import _git
from .porcelain import _in_tracked_ws_dir, _porcelain_tracked_dirty_paths

log = structlog.get_logger(__name__)

# ── 批E#3 任务4：平台机制自造的非再生脏（三机制死锁拆解）─────────────
# 死锁链（constants.py REGENERABLE_PATTERNS 注释 + 审计卡 P1-12）：
#   ① checkpoint 的 ``git add -A`` 把可再生产物提交进分支 ⇒ merge 落到 MAIN
#      后 MAIN **跟踪**它们 ⇒ 引擎一重生成 MAIN 就永久脏；
#   ② merge 门禁判 non_regen 硬拒（REGENERABLE_PATTERNS 只覆盖 4 类）；
#   ③ 门禁处方让 agent 自己跑 ``checkout HEAD --`` ⇒ 受限进程撞 .git 封条。
# 修法：③的平台化 —— 凡 HEAD 里某路径的内容**最后**由平台 checkpoint 提交
# 写入（即 HEAD 基线本身就是平台快照，不是团队甄别过的源码），其上的
# 未提交改动就由**平台侧**（非受限进程，走信任锚 _git）代为恢复。恢复前
# 工作树副本先**复制**进 merge-quarantine —— untracked 计入 dirty 是设计
# 目的，本清理只碰 tracked 脏、且绝不静默丢内容（AI_MEMORY 纪律）。
#
# ⚠ P1（独立审计 2026-09-26）：「HEAD 末次写入者是 checkpoint」只证明
# **HEAD 基线**的来源，**不证明**工作树未提交增量的来源 —— user_suspect
# 本来就把这批路径标了疑似人工编辑。故代清绝不静默：warning 日志列路径 +
# 隔离目录内写显式声明文件（该目录是 merge 期间新建 ⇒ service_merge 的
# ``_attach_quarantine_events`` 会把它自动附进**成功与失败两条 merge 回执**
# 的 ``[QUARANTINE]`` 行与 urgent ``[MERGE QUARANTINE]`` 收件箱，且
# merge-quarantine 对 read_file 只读放行 —— 声明可达三通道）。
#
# 上游裁决：亦无（ACL 特有，上游无此机制可抄）。
_CHECKPOINT_COMMIT_SUBJECTS = (CHECKPOINT_PREFIX, "pre-merge-checkpoint:")
_PLATFORM_DIRT_QUERY_CAP = 20
#: 隔离目录内的显式声明文件名（文件名本身进回执文件清单 = 声明可见）。
_RESTORE_DECLARATION_FILENAME = "_PLATFORM-RESTORED-SUSPECTED-HUMAN-EDITS.txt"


async def _checkpoint_committed_dirt(
    workspace_path: str, paths: list[str]
) -> list[str]:
    """``paths`` 中「HEAD 内容最后由平台 checkpoint 提交写入」的子集。

    判据：``git log -1 --format=%s -- <path>`` 的主题行以 checkpoint 前缀
    开头。HEAD 基线既是平台快照，把它恢复回来不会丢团队甄别过的内容
    （快照之后的未提交增量先隔离再恢复，见
    :func:`_restore_platform_created_dirt`）。
    """
    hit: list[str] = []
    for rel in paths[:_PLATFORM_DIRT_QUERY_CAP]:
        ok, out = await _git(
            ["log", "-1", "--format=%s", "--", rel], workspace_path
        )
        subj = (out or "").strip()
        if ok and subj.startswith(_CHECKPOINT_COMMIT_SUBJECTS):
            hit.append(rel)
    return hit


async def _restore_platform_created_dirt(
    workspace_path: str, paths: list[str], *, branch: str
) -> dict | None:
    """平台侧恢复平台自造脏：先隔离工作树副本，再 ``checkout HEAD --``。

    Returns a rejection dict on failure, None on success（调用方原样返回）。
    """
    quarantined, quarantine_dest = await _quarantine_dirty_copies(
        workspace_path, paths
    )
    # P1（独立审计）：这批路径同属 user_suspect（疑似人工/外部编辑）——
    # 代清必须 red flag：warning 列路径；显式声明已写进隔离目录的
    # ``_PLATFORM-RESTORED-SUSPECTED-HUMAN-EDITS.txt``，随 merge 回执的
    # ``[QUARANTINE]`` 行与 urgent 收件箱可见（成功路径同样附）。
    if quarantined:
        log.warning(
            "git_worktree.platform_restore_suspected_human_edits",
            paths=paths[:10],
            quarantine_dir=quarantine_dest,
            note=(
                "以下路径 HEAD 基线来自平台 checkpoint，其未提交改动疑似"
                "人工/外部编辑——平台已代清恢复到 HEAD；工作树副本保留在"
                "隔离目录，merge 回执与收件箱均附声明。"
            ),
        )
    ok_restore, restore_out = await _git(
        ["checkout", "HEAD", "--"] + paths, workspace_path
    )
    if not ok_restore:
        return {
            "success": False,
            "reason": "main_dirty",
            "message": (
                "MAIN had platform-checkpoint dirt but the platform-side "
                f"restore failed: {(restore_out or '')[:300]}"
            ),
            "remedy": (
                "重试一次 merge；仍失败请报告平台（附上面 git 输出）——"
                "这是平台侧清理通道的故障，不要自己在受限 shell 里跑 git "
                "checkout（会撞 .git 封条）。"
            ),
            "branch": branch,
        }
    log.info(
        "git_worktree.platform_created_dirt_restored",
        count=len(paths),
        restored=paths[:10],
        quarantined=len(quarantined),
    )
    return None


async def _quarantine_dirty_copies(
    workspace_path: str, files: list[str]
) -> tuple[list[str], str | None]:
    """把待恢复路径的**工作树副本**复制进 merge-quarantine（best-effort）。

    复制（不移动）：checkout 失败时原文件仍在；quarantine 清单照常经
    ``list_pending_quarantine_dirs`` 进平台状态、经 ``_attach_quarantine_events``
    进 merge 回执。内容零丢失（AI_MEMORY 纪律：不静默删树丢码）。

    P1（独立审计）：复制成功时在隔离目录写**显式声明文件**
    （``_RESTORE_DECLARATION_FILENAME``）——「以下路径疑似人工编辑、已代清、
    副本在本目录」，随回执与收件箱可见。Returns ``(copied, dest_str_or_None)``。
    """
    import time as _time

    root = Path(workspace_path)
    stamp = _time.strftime("%Y%m%d-%H%M%S")
    dest_root = root / ".hiveweave" / "merge-quarantine" / f"{stamp}-pre-restore"
    copied: list[str] = []
    for rel in files:
        src = root / rel
        if not src.is_file():
            continue
        dest = dest_root / rel
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dest))
            copied.append(rel.replace("\\", "/"))
        except OSError as e:
            log.warning(
                "git_worktree.pre_restore_quarantine_failed",
                path=rel,
                error=str(e),
            )
    if copied:
        declaration = (
            "PLATFORM RESTORE DECLARATION / 平台代清显式声明\n"
            "以下路径疑似人工编辑（疑似人工/外部编辑，user_suspect），"
            "平台已代清恢复到 HEAD（已代清）。你未提交的工作树改动**未丢弃**"
            "——副本在本目录（.hiveweave/merge-quarantine/ 的 "
            f"{dest_root.name}/，只读可取回）：\n"
            + "\n".join(f"- {rel}" for rel in copied[:20])
            + "\n恢复指引：确认改动是你要的 → 取回副本重新提交；"
            "确认是误编辑 → 忽略本目录即可。"
        )
        try:
            (dest_root / _RESTORE_DECLARATION_FILENAME).write_text(
                declaration, encoding="utf-8"
            )
        except OSError as e:
            log.warning(
                "git_worktree.pre_restore_declaration_write_failed",
                error=str(e),
            )
        log.info(
            "git_worktree.pre_restore_quarantined",
            count=len(copied),
            dest=str(dest_root),
            files=copied[:12],
        )
        return copied, str(dest_root)
    return copied, None


async def classify_main_dirt(workspace_path: str) -> dict:
    """只读判定 main 脏状态（不清理、不提交）。

    Returns ``{"dirty_paths": [...], "hard_blockers": [...],
    "user_suspect": [...]}`` —
    ``hard_blockers`` = tracked 脏路径中「非可再生且不在 tracked ws 目录」
    的集合（merge 会硬拒的集合）；``user_suspect`` = 其中**疑似人工/外部
    编辑**的路径（P1-4 author 事实位：Agent 产出应经 worktree+merge，MAIN
    上遗留的未提交 tracked 改动 = 人类编辑/外部进程嫌疑 —— 与平台托管目录
    .hiveweave/* 及可再生产物互斥后取 hard_blockers）。供 merge 预检
    （dry-run）与 ``restore_regenerable_dirt_or_reject`` 共享同一判定。
    """
    ok_st, st_out = await _git(
        ["-c", "core.quotepath=false", "status", "--porcelain", "-z"],
        workspace_path,
    )
    dirty_paths = _porcelain_tracked_dirty_paths(st_out) if ok_st else []
    hard = [
        p for p in dirty_paths
        if not is_generated_path(p) and not _in_tracked_ws_dir(p)
    ]
    return {
        "dirty_paths": dirty_paths,
        "hard_blockers": hard,
        "user_suspect": list(hard),  # P1-4 author 事实位
    }


async def restore_regenerable_dirt_or_reject(
    workspace_path: str, *, branch: str = "", short_id: str = ""
) -> dict | None:
    """TEST6 P1-C shared dirty-target gate for merge() and merge_by_branch().

    All-regenerable dirt (tsbuildinfo / test_output*.json left by tsc /
    vitest runs on the main checkout) is auto-cleaned and merge proceeds —
    previously this hard-reject cost a cross-agent git-stash round-trip
    (TEST6 23:21-23:23) and left residue behind. Any non-regenerable
    tracked dirt → hard reject (unchanged contract: no auto-commit spam
    on main history) — **except** 平台机制自造的非再生脏（批E#3 任务4：
    HEAD 基线本身就是 checkpoint 快照的路径），平台侧代清后放行，见
    :func:`_restore_platform_created_dirt`。这批路径同属 ``user_suspect``
    （疑似人工编辑）⇒ 代清**不静默**：warning 日志列路径 + 隔离目录内的
    ``_PLATFORM-RESTORED-SUSPECTED-HUMAN-EDITS.txt`` 声明文件随 merge 回执
    （``[QUARANTINE]`` 行）与 urgent 收件箱可见。

    Restore is split by presence in HEAD: paths known to HEAD are restored
    via ``checkout HEAD --``; staged-new regenerable files (an agent's
    manual ``git add`` — never committed, so HEAD has no pathspec) are
    de-staged with ``rm --cached`` instead (file stays on disk, and F1's
    info/exclude keeps it out of status afterwards). Discarded content is
    regenerable by definition, so the loss is acceptable by design.

    Returns a rejection dict the caller returns verbatim, or None when the
    target is clean / was cleaned and the merge may proceed.
    """
    dirt = await classify_main_dirt(workspace_path)
    dirty_paths = dirt["dirty_paths"]
    if not dirty_paths:
        return None

    # ── 批E#3 任务4：平台自造非再生脏，平台侧代清（merge 主路打通）──────
    # 与 preflight_merge 共用同一份分类（classify_main_dirt）——预检报的
    # hard_blockers 与本门禁拒的集合从此一致。清理限定在「平台机制自造且
    # 已可识别」（HEAD 内容最后写入者是 checkpoint 提交），不扩大化。
    hard_blockers = dirt["hard_blockers"]
    if hard_blockers:
        platform_created = await _checkpoint_committed_dirt(
            workspace_path, hard_blockers
        )
        if platform_created:
            fail = await _restore_platform_created_dirt(
                workspace_path, platform_created, branch=branch
            )
            if fail is not None:
                return fail
            # 恢复后重取状态：剩余硬阻塞才进下面的硬拒。
            dirt = await classify_main_dirt(workspace_path)
            dirty_paths = dirt["dirty_paths"]
            if not dirty_paths:
                return None

    non_regen = [p for p in dirty_paths if not is_generated_path(p)]
    if non_regen and not all(_in_tracked_ws_dir(p) for p in non_regen):
        # 按路径类别分流处方（fixlist #7）：生成物与源码脏的**正确动作不同**。
        # 原来笼统一句 "clean or commit" 会让团队把引擎缓存 commit 进库 ——
        # 51 号实测的形态（`.godot/global_script_class_cache.cfg` 被拒 2 次）。
        regen_dirty = [p for p in dirty_paths if is_generated_path(p)]
        parts: list[str] = []
        parts.append(
            "SOURCE changes — commit them on a side branch (or discard if "
            "unintended): " + ", ".join(non_regen[:8])
        )
        if regen_dirty:
            parts.append(
                "REGENERABLE artifacts — do NOT commit; reset them instead, "
                "the next build/test run rebuilds them: "
                + ", ".join(regen_dirty[:8])
            )
        return {
            "success": False,
            "reason": "main_dirty",
            "message": (
                "MAIN has uncommitted changes; auto-save to main history is "
                "disabled. " + " | ".join(parts)
            ),
            # 批E#3 任务3：拒绝必带处方。平台自造脏已由平台侧代清（上方），
            # 走到这里的必是**疑似人工/外部编辑**（P1-4）——正确动作是
            # 请用户确认，不是让受限 agent 自己跑 git（撞封条）。
            "remedy": (
                "这批改动疑似人工/外部编辑：通知用户确认后再处理"
                "（用户认可的工作让它先提交，误编辑由用户丢弃）——不要 "
                "stash/checkout 吞掉，也不要把引擎缓存 commit 进库；"
                "处理完重新发起 merge。"
            ),
            "branch": branch,
        }
    ok_lt, lt_out = await _git(
        ["ls-tree", "-r", "--name-only", "HEAD", "--"] + dirty_paths,
        workspace_path,
    )
    in_head = {ln.strip() for ln in (lt_out or "").splitlines() if ln.strip()}
    if not ok_lt:
        in_head = set()
    # Workspace docs never get auto-restored or de-staged here — those are
    # the agent's live contract edits and are committed atomically below.
    non_ws = [p for p in dirty_paths if not _in_tracked_ws_dir(p)]
    in_head_paths = [p for p in non_ws if p in in_head]
    staged_new = [p for p in non_ws if p not in in_head]
    if staged_new:
        ok_rm, rm_out = await _git(
            # P1-6 问题 B（2026-09-21，git 层实测）：缺 `-f --ignore-unmatch` 时，
            # 只要路径里有**一个不存在**的项，整条 `git rm` 就失败（rc=128：
            # `fatal: pathspec '...' did not match any files`）—— 连已存在的项也没去掉。
            # 实测：加 `-f --ignore-unmatch` 后同一组输入 rc=0。
            ["rm", "--cached", "-f", "--ignore-unmatch", "--quiet", "--"] + staged_new, workspace_path
        )
        if not ok_rm:
            return {
                "success": False,
                "reason": "main_dirty",
                "message": (
                    "MAIN has regenerable-only dirt but de-staging "
                    f"staged-new files failed: {(rm_out or '')[:300]}"
                ),
                "remedy": (
                    "重试一次 merge；仍失败请报告平台（附上面 git 输出）——"
                    "这是平台侧清理通道的故障。"
                ),
                "branch": branch,
            }
    if in_head_paths:
        ok_restore, restore_out = await _git(
            ["checkout", "HEAD", "--"] + in_head_paths, workspace_path
        )
        if not ok_restore:
            return {
                "success": False,
                "reason": "main_dirty",
                "message": (
                    "MAIN has regenerable-only dirt but auto-restore "
                    f"failed: {(restore_out or '')[:300]}"
                ),
                "remedy": (
                    "重试一次 merge；仍失败请报告平台（附上面 git 输出）——"
                    "这是平台侧清理通道的故障，不要自己在受限 shell 里跑 "
                    "git checkout（会撞 .git 封条）。"
                ),
                "branch": branch,
            }
    log.info(
        "git_worktree.merge_regenerable_dirt_restored",
        short_id=short_id,
        restored=in_head_paths[:10],
        destaged=staged_new[:10],
    )
    # Tracked workspace docs (shared/reports/drafts/handoffs) edited directly
    # on main are legitimate contract updates — auto-commit them before
    # merging when they are the ONLY non-regenerable dirt (audit P-R: legacy
    # sync-copy is gone; main-side doc edits must not hard-reject). Any
    # unrelated staged content is un-staged first so the commit is strictly
    # the workspace docs.
    if any(_in_tracked_ws_dir(p) for p in dirty_paths):
        await _git(["reset", "--quiet"], workspace_path)
        # Codespell: pathspec for a missing dir errors out — stage only dirs
        # that exist (tracked ws dirs are created lazily by agents).
        ws_dirs = [
            d for d in TRACKED_WS_DIRS
            if (Path(workspace_path) / d).exists()
        ]
        ok_add, add_out = await _git(
            ["add", "--"] + ws_dirs, workspace_path
        )
        if not ok_add:
            return {
                "success": False,
                "reason": "main_dirty",
                "message": (
                    "MAIN has workspace-doc dirt but auto-staging failed: "
                    f"{(add_out or '')[:300]}"
                ),
                "remedy": (
                    "重试一次 merge；仍失败请报告平台（附上面 git 输出）。"
                ),
                "branch": branch,
            }
        ok_ci, ci_out = await _git(
            # Identity via -c: adopted legacy repos may lack user.name/email
            # (audit P2: same口径 as the ignore-migration maintenance commit).
            ["-c", "user.name=HiveWeave Agent",
             "-c", "user.email=hiveweave@agent.local",
             "commit", "-m", "checkpoint: shared workspace docs (main)"],
            workspace_path,
        )
        if not ok_ci:
            return {
                "success": False,
                "reason": "main_dirty",
                "message": (
                    "MAIN has workspace-doc dirt but auto-commit failed: "
                    f"{(ci_out or '')[:300]}"
                ),
                "remedy": (
                    "重试一次 merge；仍失败请报告平台（附上面 git 输出）。"
                ),
                "branch": branch,
            }
        log.info(
            "git_worktree.workspace_docs_autocommitted",
            short_id=short_id,
            committed=[p for p in dirty_paths if _in_tracked_ws_dir(p)][:10],
        )
    return None


async def _abort_landed_merge(workspace_path: str) -> bool:
    """Undo a bad merge on target — reset to ORIG_HEAD or merge --abort."""
    ok_reset, _ = await _git(["reset", "--hard", "ORIG_HEAD"], workspace_path)
    if ok_reset:
        return True
    ok_abort, _ = await _git(["merge", "--abort"], workspace_path)
    return ok_abort


async def _auto_checkpoint_dirty_target(
    workspace_path: str, target_branch: str
) -> bool:
    """If target (usually main) has local changes, commit a pre-merge checkpoint.

    TEST11 evening P3-1: dirty main caused "not a content conflict" merge
    failures. Previously we only advised; now we checkpoint automatically.
    Returns True if a checkpoint commit was created.
    """
    ok_st, st_out = await _git(["status", "--porcelain"], workspace_path)
    if not ok_st or not (st_out or "").strip():
        return False
    await _git(["add", "-A"], workspace_path)
    ok_c, out_c = await _git(
        [
            "commit",
            "-m",
            f"pre-merge-checkpoint: auto-save dirty {target_branch}",
            "--allow-empty",
        ],
        workspace_path,
    )
    if ok_c:
        log.info(
            "git_worktree.pre_merge_main_checkpoint",
            target=target_branch,
            dirty_preview=(st_out or "")[:200],
        )
        return True
    log.warning(
        "git_worktree.pre_merge_checkpoint_failed",
        target=target_branch,
        output=(out_c or "")[:200],
    )
    return False


async def _current_branch(worktree_path: str) -> str | None:
    """worktree 实际检出的分支 (``git -C <path> rev-parse --abbrev-ref HEAD``)。

    幂等/解析的唯一事实来源: 路径还在, 就以检出分支为准, 不按入参
    重算 (重算名与检出分支可能脱钩)。detached HEAD 返回 None。
    """
    ok, out = await _git(["rev-parse", "--abbrev-ref", "HEAD"], worktree_path)
    if ok and out and out.strip() != "HEAD":
        return out.strip()
    return None


def parse_untracked_overwrite(git_output: str) -> list[str]:
    """Extract paths from 'untracked working tree files would be overwritten'."""
    if not git_output or not _UNTRACKED_OVERWRITE_RE.search(git_output):
        return []
    files: list[str] = []
    for m in _UNTRACKED_FILE_LINE_RE.finditer(git_output):
        path = m.group(1).strip().replace("\\", "/")
        if path and path not in files:
            files.append(path)
    return files


def list_pending_quarantine_dirs(
    workspace_path: str, limit: int = 3, max_files: int = 500
) -> list[dict]:
    """T2.5: 清点待处理的 merge-quarantine 目录（只读， Newest first）。

    供 ``get_platform_state`` 挂待处理隔离计数与 UNCOMMITTED_WORKTREE
    提示附 ``quarantine_ref`` —— 隔离副作用必须可发现（P0-2），不能只存在
    于文件系统里。返回 ``[{stamp, path, file_count}]``；单目录文件数封顶
    ``max_files``（审计 P2-2：隔离区可能搬进整棵 node_modules，每次 turn
    exit 全量遍历不可接受），超过即停止计数（值为下限）。
    """
    qroot = Path(workspace_path) / ".hiveweave" / "merge-quarantine"
    if not qroot.is_dir():
        return []
    out: list[dict] = []
    try:
        stamps = sorted(
            (d for d in qroot.iterdir() if d.is_dir()),
            key=lambda d: d.name,
            reverse=True,  # stamp 名即时间戳， newest first
        )
    except OSError:
        return []
    for d in stamps[: max(0, int(limit))]:
        n = 0
        truncated = False
        try:
            for _ in d.rglob("*"):
                if not _.is_file():
                    continue
                n += 1
                if n >= max_files:
                    truncated = True
                    break
        except OSError:
            pass
        out.append({
            "stamp": d.name,
            "path": str(d),
            "file_count": n,
            **({"file_count_truncated": True} if truncated else {}),
        })
    return out


async def quarantine_untracked_on_target(
    workspace_path: str, files: list[str]
) -> list[str]:
    """Move untracked files that block merge into ``.hiveweave/merge-quarantine/``.

    Returns list of successfully quarantined relative paths.
    """
    import time as _time

    root = Path(workspace_path)
    stamp = _time.strftime("%Y%m%d-%H%M%S")
    dest_root = root / ".hiveweave" / "merge-quarantine" / stamp
    moved: list[str] = []
    for rel in files:
        src = root / rel
        if not src.exists():
            continue
        # Only quarantine untracked / not in index
        ok_ls, ls_out = await _git(["ls-files", "--", rel], workspace_path)
        if ok_ls and (ls_out or "").strip():
            continue  # tracked — leave alone
        dest = dest_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(src), str(dest))
            moved.append(rel.replace("\\", "/"))
        except OSError as e:
            log.warning(
                "git_worktree.quarantine_failed",
                path=rel,
                error=str(e),
            )
    if moved:
        log.info(
            "git_worktree.quarantined_untracked",
            count=len(moved),
            dest=str(dest_root),
            files=moved[:12],
        )
    return moved


async def _merge_failure_result(
    *,
    workspace_path: str,
    branch: str,
    target_branch: str,
    merge_out: str,
    branch_files: list[str],
    short_id: str = "",
    auto_quarantine: bool = True,
) -> dict | None:
    """Classify merge failure. May quarantine untracked and return None to retry.

    Returns a failure dict, or ``None`` when caller should retry merge once
    after auto-quarantine.
    """
    untracked = parse_untracked_overwrite(merge_out)
    if untracked:
        # Abort any in-progress merge so main is clean
        await _git(["merge", "--abort"], workspace_path)
        if auto_quarantine:
            moved = await quarantine_untracked_on_target(
                workspace_path, untracked
            )
            if moved:
                return None  # signal retry
        from hiveweave.services.worktree_review import (
            format_untracked_on_target_message,
        )

        return {
            "success": False,
            "reason": "untracked_on_target",
            "message": format_untracked_on_target_message(
                branch=branch,
                target=target_branch,
                untracked=untracked,
            ),
            "remedy": (
                "平台已把这些 untracked 文件搬进 .hiveweave/merge-quarantine/"
                "（可恢复，位置见回执/quarantine_ref）—— 直接重试 merge 即可；"
                "不要手工去 MAIN 删文件。"
            ),
            "untracked": untracked,
            "conflicts": [],
            "branch": branch,
            "files": branch_files,
            "short_id": short_id,
        }

    ok_diff, diff_out = await _git(
        ["diff", "--name-only", "--diff-filter=U"], workspace_path
    )
    conflict_files = [
        f.strip() for f in (diff_out or "").split("\n") if f.strip()
    ] if ok_diff else []
    await _git(["merge", "--abort"], workspace_path)

    from hiveweave.services.worktree_review import format_merge_conflict_message

    if conflict_files:
        return {
            "success": False,
            "reason": "merge_conflict",
            "message": format_merge_conflict_message(
                branch=branch,
                target=target_branch,
                conflicts=conflict_files,
            ),
            "remedy": (
                "派回 assignee：在**他的 worktree**里 git_worktree_sync "
                "同步 main → 就地解冲突 → commit → checkpoint；平台已 "
                "abort 本次 merge，不要在 MAIN 上解。"
            ),
            "conflicts": conflict_files,
            "branch": branch,
            "files": branch_files,
            "short_id": short_id,
        }

    # Not a content conflict — surface raw git output; do NOT fake conflicts
    # from branch_files (that caused "same commit" false conflict loops).
    return {
        "success": False,
        "reason": "merge_failed",
        "message": (
            f"Merge of {branch} into {target_branch} failed "
            f"(not a content conflict):\n{(merge_out or '')[:800]}\n\n"
            "Do NOT ask the executor to 'fix merge conflict in worktree' "
            "unless conflicted files are listed. Inspect main hygiene "
            "(untracked / local edits) and retry."
        ),
        "remedy": (
            "按回执列出的 MAIN hygiene 问题逐项处理后重试 merge；"
            "没有列冲突文件就不要按冲突处理（那会烧掉一轮返修）。"
        ),
        "conflicts": [],
        "branch": branch,
        "files": branch_files,
        "short_id": short_id,
    }
