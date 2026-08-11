"""Worktree orphan reconcile (P0)."""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

import structlog

from .constants import (
    WORKTREE_DIR,
    _IN_FLIGHT_AFTER_MERGE_STATUSES,
    _PROTECT_TASK_STATUSES,
    _TASK_BRANCH_RE,
)
from .git_cmd import _current_branch, _git, _resolve_base_branch
from .naming import compute_branch_name
from .paths import _has_git, _worktree_path

log = structlog.get_logger(__name__)

# hw/<short_id>/<rest> — used to detect agent-gone orphan branches (S8).
_HW_SID_BRANCH_RE = re.compile(r"^hw/([^/]+)/")

# 2026-08-11 A023 事故：husk rmtree 失败（Windows 文件锁 Device busy）静默
# 吞掉 → 损坏目录永久残留。跨 reconcile 轮次计数，连续失败告警（可观测）。
_husk_remove_failures: dict[str, int] = {}
_HUSK_RETRY_MAX = 3


def _rmtree_husk(
    child: Path,
    report: dict,
    workspace_path: str,
    *,
    source: str,
    recheck_git: bool = True,
) -> None:
    """Husk 目录删除：先停内部进程 → rmtree → 失败计数重试 + 告警。

    ``shutil.rmtree(ignore_errors=True)`` 在 Windows 文件锁下静默失败；
    失败目录跨轮次重试（下次 reconcile 再试），连续 ``_HUSK_RETRY_MAX``
    次仍失败则 log.error（持久 husk 需要人工介入）。

    ``recheck_git``（并发审计 F1）：仅对**调用时无 .git 的 husk**场景开启
    —— stop 进程的窗口里另一协程可能已完成 git worktree add（.git 刚写
    入），二次 stat 避免把刚复活的活树当 husk 删。orphan 目录（调用时
    有 .git 的未注册树）不适用（本来就有 .git，无复活误判窗口）。
    """
    try:
        from hiveweave.services.process_registry import (
            stop_processes_for_worktree,
        )

        stop_processes_for_worktree(str(child))
    except Exception:
        pass
    if recheck_git and (child / ".git").exists():
        log.info(
            "git_worktree.reconcile_husk_revived_skip",
            workspace=workspace_path,
            dir=str(child),
        )
        return
    shutil.rmtree(child, ignore_errors=True)
    key = os.path.normcase(str(child))
    if not child.exists():
        _husk_remove_failures.pop(key, None)
        report["removed_dirs"] = report.get("removed_dirs", 0) + 1
        log.info(
            "git_worktree.reconcile_husk_removed",
            workspace=workspace_path,
            dir=str(child),
            source=source,
        )
    else:
        n = _husk_remove_failures.get(key, 0) + 1
        _husk_remove_failures[key] = n
        report.setdefault("errors", []).append(
            f"failed to remove husk dir: {child} (attempt {n}/{_HUSK_RETRY_MAX})"
        )
        if n == _HUSK_RETRY_MAX:
            log.error(
                "git_worktree.reconcile_husk_persistent",
                workspace=workspace_path,
                dir=str(child),
                attempts=n,
                hint="Directory locked by a process (Device busy). Kill the "
                "holding process or move the directory aside manually.",
            )

def _parse_worktree_porcelain(raw: str) -> list[dict]:
    """解析 ``git worktree list --porcelain`` → [{path, head, branch}]"""
    entries: list[dict] = []
    cur: dict | None = None
    for line in raw.splitlines():
        if line.startswith("worktree "):
            cur = {"path": line[len("worktree "):].strip().strip('"'),
                   "head": "", "branch": ""}
            entries.append(cur)
        elif cur is not None and line.startswith("HEAD "):
            cur["head"] = line[len("HEAD "):].strip()
        elif cur is not None and line.startswith("branch "):
            ref = line[len("branch "):].strip()
            if ref.startswith("refs/heads/"):
                ref = ref[len("refs/heads/"):]
            cur["branch"] = ref
    return entries

async def _agent_id_for_short_id(
    workspace_path: str, short_id: str
) -> str | None:
    """Resolve agent UUID from short_id via project DB (best-effort)."""
    conn = await _project_db_if_exists(workspace_path)
    if not conn:
        return None
    try:
        cur = await conn.execute(
            "SELECT id FROM agents WHERE short_id = ? LIMIT 1",
            [short_id],
        )
        row = await cur.fetchone()
        await cur.close()
        if row:
            return row["id"] if "id" in row.keys() else row[0]
    except Exception:
        pass
    return None


async def _log_worktree_rebuild_event(
    workspace_path: str,
    short_id: str,
    *,
    reason: str,
    original: str,
    path: str,
) -> None:
    """Fire-and-forget audit when stale path fallback/reuse occurs (TEST21 M11)."""
    agent_id = await _agent_id_for_short_id(workspace_path, short_id)
    if not agent_id:
        return
    head = ""
    ok_h, head_out = await _git(["rev-parse", "HEAD"], path if _has_git(path) else workspace_path)
    if ok_h and head_out:
        head = head_out.strip()
    project_id = ""
    try:
        from hiveweave.services.agent_router import agent_router

        project_id = agent_router.get_project_id(agent_id) or ""
    except Exception:
        pass
    try:
        from hiveweave.services.event_audit import event_audit

        await event_audit.log(
            agent_id,
            project_id,
            "worktree_rebuild",
            {
                "reason": reason,
                "original": original,
                "path": path,
                "head": head,
            },
        )
    except Exception as e:
        log.debug("worktree_rebuild_audit_failed", error=str(e))


async def _project_db_if_exists(workspace_path: str):
    """项目 DB 存在才连接 — 对账绝不为了查任务表而新建 DB。"""
    db_file = Path(workspace_path) / ".hiveweave" / "data.db"
    if not db_file.exists():
        return None
    try:
        from hiveweave.db.project import ensure_project_db

        return await ensure_project_db(workspace_path)
    except Exception as e:
        log.warning("git_worktree.reconcile_db_failed",
                    workspace=workspace_path, error=str(e))
        return None


async def _open_project_db_raw(workspace_path: str):
    """Open existing project DB without migrations (protect lookups).

    Used when ensure_project_db fails on partial schemas — still need to
    read agents/tasks so reconcile does not wipe active worktrees.
    """
    db_file = Path(workspace_path) / ".hiveweave" / "data.db"
    if not db_file.exists():
        return None
    try:
        import aiosqlite

        conn = await aiosqlite.connect(str(db_file))
        conn.row_factory = aiosqlite.Row
        return conn
    except Exception as e:
        log.warning(
            "git_worktree.raw_db_open_failed",
            workspace=workspace_path,
            error=str(e),
        )
        return None


async def _task_branch_candidate(conn, prefix: str) -> tuple[bool, str]:
    """t-<taskid8> 分支的回收候选判定 (查项目 DB tasks 表)。

    任务 closed / archived / 不存在 → (True, reason); 仍有活跃任务 →
    (False, "")。同一 8 位前缀命中多个任务时全部终态才算候选 (保守,
    不误删); 查询失败同样保守跳过。
    """
    try:
        cur = await conn.execute(
            "SELECT status, is_archived FROM tasks "
            "WHERE substr(lower(id), 1, 8) = ?",
            [prefix.lower()],
        )
        rows = await cur.fetchall()
        await cur.close()
    except Exception as e:
        log.warning("git_worktree.reconcile_task_query_failed",
                    prefix=prefix, error=str(e))
        return False, ""
    if not rows:
        return True, "task_missing_unmerged"
    done = all(
        r["status"] == "closed" or bool(r["is_archived"]) for r in rows
    )
    return (True, "task_closed_unmerged") if done else (False, "")


# Protect reconcile from deleting dirs still needed for in-flight / pending-merge work
_PROTECT_TASK_STATUSES = frozenset({
    "created", "claimed", "running", "blocked", "submitted",
    "reviewing", "rework", "verifying", "approved",
})
# After a successful merge, approved work is done — retain worktree only if
# assignee still has truly in-flight tasks (not the just-merged approved ones).
_IN_FLIGHT_AFTER_MERGE_STATUSES = frozenset({
    "created", "claimed", "running", "blocked", "submitted",
    "reviewing", "rework", "verifying",
})

# TEST6 evening P1-4: ancestor-of-main orphan_agent_gone → quarantine after TTL
_ORPHAN_ANCESTOR_TTL_S = 7 * 86400


async def _active_agent_short_ids(
    workspace_path: str,
) -> tuple[set[str], bool]:
    """Non-archived agent short_ids (+ workspace basenames) for orphan scan.

    Returns ``(sids, orphan_scan_enabled)``. Orphan scan is enabled only when
    the agents table is readable AND has at least one row (active or archived).
    An empty agents table (test fixtures / brand-new project) must NOT mark
    every ``hw/*`` branch as agent-gone.

    TEST6 audit S8: ``hw/<sid>/*`` branches whose sid is absent from active
    sids (when scan enabled) are agent-gone orphans.
    """
    sids: set[str] = set()
    conn = await _project_db_if_exists(workspace_path)
    close_raw = False
    if conn is None:
        conn = await _open_project_db_raw(workspace_path)
        close_raw = conn is not None
    if conn is None:
        return sids, False
    try:
        cur = await conn.execute(
            "SELECT short_id, workspace_path, status FROM agents"
        )
        rows = await cur.fetchall()
        await cur.close()
        if not rows:
            return sids, False
        for r in rows:
            sid = ""
            wp = ""
            status = "active"
            if hasattr(r, "keys"):
                keys = r.keys()
                sid = (r["short_id"] or "").strip() if "short_id" in keys else ""
                wp = (
                    (r["workspace_path"] or "").strip()
                    if "workspace_path" in keys
                    else ""
                )
                status = (
                    (r["status"] or "active") if "status" in keys else "active"
                )
            else:
                sid = (r[0] or "").strip() if r else ""
                wp = (r[1] or "").strip() if r and len(r) > 1 else ""
                status = (r[2] or "active") if r and len(r) > 2 else "active"
            if (status or "").lower() == "archived":
                continue
            if sid:
                sids.add(sid)
            if wp:
                base = Path(wp).name
                if base:
                    sids.add(base)
        return sids, True
    except Exception as e:
        log.warning(
            "git_worktree.reconcile_active_sids_failed",
            workspace=workspace_path,
            error=str(e),
        )
        return sids, False
    finally:
        if close_raw and conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


async def _protected_worktree_short_ids(workspace_path: str) -> set[str]:
    """Directory basenames reconcile must not rmtree.

    TEST4: protect only assignees with non-terminal / pending-merge tasks.
    Active executors with all tasks closed must be eligible for cleanup —
    otherwise worktree dirs linger forever after merge+close.

    P1-5: protect by ``basename(workspace_path)`` (the actual directory the
    agent uses) rather than ``short_id``. When an agent's worktree is
    recreated under a new directory (e.g. A024 → A024-b after a stale
    cleanup), the old orphan directory must NOT be protected by the
    short_id match — only the live directory is.
    """
    protected: set[str] = set()
    conn = await _project_db_if_exists(workspace_path)
    close_raw = False
    if conn is None:
        conn = await _open_project_db_raw(workspace_path)
        close_raw = conn is not None
    if conn is None:
        return protected
    try:
        # Assignees with non-terminal tasks — protect their actual workspace dir
        placeholders = ", ".join("?" * len(_PROTECT_TASK_STATUSES))
        cur = await conn.execute(
            f"SELECT DISTINCT a.workspace_path FROM tasks t "
            f"JOIN agents a ON a.id = t.assignee_id "
            f"WHERE COALESCE(t.is_archived, 0) = 0 "
            f"AND t.status IN ({placeholders}) "
            f"AND a.workspace_path IS NOT NULL AND TRIM(a.workspace_path) != ''",
            list(_PROTECT_TASK_STATUSES),
        )
        rows = await cur.fetchall()
        await cur.close()
        for r in rows:
            wp = (r["workspace_path"] or "").strip()
            if wp:
                # Protect the basename of the actual workspace directory
                basename = Path(wp).name
                if basename:
                    protected.add(basename)
    except Exception as e:
        log.warning(
            "git_worktree.reconcile_protected_lookup_failed",
            workspace=workspace_path,
            error=str(e),
        )
    finally:
        if close_raw and conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
    return protected


async def _try_reattach_worktree(
    workspace_path: str, short_id: str, path: str
) -> bool:
    """Best-effort: re-register an existing dir into git worktree list."""
    branch = compute_branch_name(short_id)
    fwd = path.replace("\\", "/")
    # Prefer branch already checked out in the dir
    if _has_git(path):
        current = await _current_branch(path)
        if current:
            branch = current
    ok, out = await _git(
        ["worktree", "add", "--force", fwd, branch], workspace_path
    )
    if ok:
        return True
    # Branch may not exist — try creating from HEAD
    ok2, out2 = await _git(
        ["worktree", "add", "-b", branch, fwd, "HEAD"], workspace_path
    )
    if ok2:
        return True
    log.debug(
        "git_worktree.reattach_failed",
        short_id=short_id,
        path=path,
        error=f"{out}; {out2}",
    )
    return False


async def _assignee_has_open_tasks(workspace_path: str, short_id: str) -> bool:
    """True if agent still has in-flight tasks after a merge (not mere approved).

    ``approved`` is excluded: that is the state of work just merged. Including
    it would permanently skip worktree cleanup (TEST3 self-check).
    """
    conn = await _project_db_if_exists(workspace_path)
    close_raw = False
    if conn is None:
        conn = await _open_project_db_raw(workspace_path)
        close_raw = conn is not None
    if conn is None:
        return False
    try:
        placeholders = ", ".join("?" * len(_IN_FLIGHT_AFTER_MERGE_STATUSES))
        cur = await conn.execute(
            f"SELECT 1 FROM tasks t "
            f"JOIN agents a ON a.id = t.assignee_id "
            f"WHERE a.short_id = ? AND COALESCE(t.is_archived, 0) = 0 "
            f"AND t.status IN ({placeholders}) LIMIT 1",
            [short_id, *list(_IN_FLIGHT_AFTER_MERGE_STATUSES)],
        )
        row = await cur.fetchone()
        await cur.close()
        return row is not None
    except Exception as e:
        log.warning(
            "git_worktree.open_tasks_lookup_failed",
            short_id=short_id,
            error=str(e),
        )
        # Fail closed: retain worktree rather than delete mid-flight
        return True
    finally:
        if close_raw and conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


def _row_is_verify_task(title: str | None, tags_raw: Any) -> bool:
    """True for VERIFY children (title prefix only — TEST19 教训: agent 自由
    tag "verify" 不得触发 VERIFY 特殊路径)."""
    del tags_raw
    return isinstance(title, str) and title.startswith("VERIFY:")


async def _assignee_needs_write_worktree(
    workspace_path: str, short_id: str
) -> bool:
    """True if agent has in-flight *code-write* tasks (excludes VERIFY).

    TEST6 evening P1-2/P1-3: VERIFY is main-checkout work. Counting VERIFY
    as "needs write worktree" caused personal trees to be recreated after
    GC, and QA/coordinators verified stale branch tips instead of main.
    """
    conn = await _project_db_if_exists(workspace_path)
    close_raw = False
    if conn is None:
        conn = await _open_project_db_raw(workspace_path)
        close_raw = conn is not None
    if conn is None:
        return False
    try:
        placeholders = ", ".join("?" * len(_IN_FLIGHT_AFTER_MERGE_STATUSES))
        cur = await conn.execute(
            f"SELECT t.title, t.tags FROM tasks t "
            f"JOIN agents a ON a.id = t.assignee_id "
            f"WHERE a.short_id = ? AND COALESCE(t.is_archived, 0) = 0 "
            f"AND t.status IN ({placeholders})",
            [short_id, *list(_IN_FLIGHT_AFTER_MERGE_STATUSES)],
        )
        rows = await cur.fetchall()
        await cur.close()
        for row in rows or []:
            title = row[0] if not hasattr(row, "keys") else row["title"]
            tags = row[1] if not hasattr(row, "keys") else row["tags"]
            if not _row_is_verify_task(title, tags):
                return True
        return False
    except Exception as e:
        log.warning(
            "git_worktree.write_tasks_lookup_failed",
            short_id=short_id,
            error=str(e),
        )
        # Fail closed: allow recreate rather than block real write work
        return True
    finally:
        if close_raw and conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


async def reconcile_worktrees(workspace_path: str) -> dict:
    """孤儿回收对账 (P0) — 注册表 / 磁盘 / 任务表三方核对。

    ① ``git worktree list --porcelain`` 注册表逐个 stat, 目录消失
       → ``git worktree prune``;
    ② 反向: ``.hiveweave/worktrees/`` 下目录不在注册表 → rmtree;
    ③ 枚举 ``hw/*/*`` 分支: ``t-<taskid8>`` 查项目 DB tasks 表
       (任务 closed/archived/不存在 → 候选; DB 不可用按"任务不存在"
       处理, 查询失败则保守跳过), legacy slug 分支同样候选;
       ``git branch --merged main`` 判定, 已合并 → ``branch -d`` 删除,
       未合并 → preserved_branches 报告 (**绝不强删**)。
       活跃 worktree 检出的分支不算孤儿, 跳过。

    Returns ``{pruned, removed_dirs, deleted_branches,
    preserved_branches, orphan_agent_branches, errors}``.
    """
    report: dict = {
        "pruned": 0,
        "removed_dirs": 0,
        "deleted_branches": [],
        "preserved_branches": [],
        "ttl_deleted_branches": [],
        "quarantined_branches": [],
        "orphan_agent_branches": [],
        "errors": [],
    }
    wt_root = Path(workspace_path) / WORKTREE_DIR

    ok, raw = await _git(["worktree", "list", "--porcelain"], workspace_path)
    if not ok:
        report["errors"].append(f"git worktree list failed: {raw}")
        log.error("git_worktree.reconcile_list_failed",
                  workspace=workspace_path, error=raw)
        return report
    entries = _parse_worktree_porcelain(raw)

    # ① 注册表 → 磁盘: 目录消失的注册项 → prune
    registered = {os.path.normcase(str(Path(e["path"]))) for e in entries}
    stale = sum(
        1 for e in entries
        if WORKTREE_DIR in e["path"].replace("\\", "/")
        and not Path(e["path"]).exists()
    )
    if stale:
        ok_p, out_p = await _git(["worktree", "prune"], workspace_path)
        if ok_p:
            report["pruned"] = stale
            # prune 后注册表已变 — 丢掉缺失项, 保证后续 checked_out 准确
            entries = [e for e in entries if Path(e["path"]).exists()]
            log.info("git_worktree.reconcile_pruned",
                     workspace=workspace_path, pruned=stale)
        else:
            report["errors"].append(f"git worktree prune failed: {out_p}")

    # ①.5 TEST16 P0-2: husk detection — registered + dir exists but no .git
    # (e.g. node_modules-only shell after failed stale cleanup).
    husks = [
        e for e in entries
        if WORKTREE_DIR in e["path"].replace("\\", "/")
        and Path(e["path"]).is_dir()
        and not (Path(e["path"]) / ".git").exists()
    ]
    if husks:
        report["husk_dirs"] = [e["path"] for e in husks]
        log.warning(
            "git_worktree.reconcile_husks_detected",
            workspace=workspace_path,
            husks=[e["path"] for e in husks],
        )
        # Prune husks from registry so they don't block future creates
        ok_h, _ = await _git(["worktree", "prune"], workspace_path)
        if ok_h:
            entries = [e for e in entries if e not in husks]
            report["pruned"] += len(husks)

    # ② 磁盘 → 注册表: 未注册的孤儿目录 → rmtree
    # TEST3: never rmtree dirs that still belong to active executors / open tasks
    # (git registry desync must not erase in-flight work). Prefer reattach.
    # 2026-08-11 A023 事故：active agent 的 **husk**（无 .git，非活树）此前被
    # protected 保护跳过 → 永久残留 → merge/checkpoint 反复解析到损坏目录。
    # husk 直接清（不丢任何工作：工作要么已提交要么在 .git 里）；活树仍保护。
    protected_sids = await _protected_worktree_short_ids(workspace_path)
    if wt_root.is_dir():
        for child in sorted(wt_root.iterdir()):
            if not child.is_dir():
                continue
            if os.path.normcase(str(child)) in registered:
                continue
            sid = child.name
            if sid in protected_sids:
                if not (child / ".git").exists():
                    # 2026-08-11 A023 事故 + 并发审计 F1：active agent 的 husk
                    # 不能直接杀进程+rmtree —— 快照陈旧 + .git stat 瞬时失败
                    # 的窗口下可能误杀刚并发 add 出来的活树。repair-first：
                    # 先 _try_reattach_worktree（git worktree add --force 会把
                    # 空目录修复成活树，不丢任何东西），失败才删除。
                    reattached = await _try_reattach_worktree(
                        workspace_path, sid, str(child)
                    )
                    if reattached:
                        report.setdefault("reattached_dirs", []).append(sid)
                        log.info(
                            "git_worktree.reconcile_protected_husk_reattached",
                            workspace=workspace_path,
                            dir=str(child),
                            short_id=sid,
                        )
                        continue
                    _rmtree_husk(child, report, workspace_path, source="protected_husk")
                    continue
                reattached = await _try_reattach_worktree(
                    workspace_path, sid, str(child)
                )
                if reattached:
                    report.setdefault("reattached_dirs", []).append(sid)
                    log.info(
                        "git_worktree.reconcile_dir_reattached",
                        workspace=workspace_path,
                        dir=str(child),
                        short_id=sid,
                    )
                else:
                    report.setdefault("skipped_active_dirs", []).append(sid)
                    log.info(
                        "reconcile_dir_skipped_active_agent",
                        workspace=workspace_path,
                        dir=str(child),
                        short_id=sid,
                    )
                continue
            _rmtree_husk(
                child, report, workspace_path, source="orphan_dir",
                recheck_git=False,  # 有 .git 的未注册目录：无 husk 复活误判窗口
            )

    # ②.5 P0-3: clean .stale-* rename-aside residue + process-aware husk removal
    if wt_root.is_dir():
        for child in sorted(wt_root.iterdir()):
            if not child.is_dir():
                continue
            name = child.name
            # .stale-* dirs are rename-aside artifacts from _force_clear_path
            if name.startswith(".stale-"):
                shutil.rmtree(child, ignore_errors=True)
                if not child.exists():
                    report["removed_dirs"] += 1
                    log.info(
                        "git_worktree.reconcile_stale_dir_removed",
                        workspace=workspace_path, dir=str(child),
                    )
                continue
            # Husk: dir exists, no .git, not registered
            # → stop processes inside then remove (P0-3 orphan cleanup)
            # 2026-08-11 A023 事故：此前 `name not in protected_sids` 排除
            # active agent 的 husk —— 无 .git 的目录不是活树，保护反而让
            # 损坏目录永久残留。husk 一律清。
            if (
                not (child / ".git").exists()
                and os.path.normcase(str(child)) not in registered
                and not name.startswith("_")  # skip _quarantine etc.
            ):
                _rmtree_husk(child, report, workspace_path, source="husk")

    # ③ 分支对账: t-<taskid8> 查任务表, legacy slug 直接候选;
    #    merged → -d 删除, 未合并 → preserved 报告
    #    TEST6 S8: hw/<sid>/* 且 agents 表无对应 short_id → orphan_agent_gone
    # (--format 输出不带 * / + 检出前缀, 可精确匹配)
    checked_out = {e["branch"] for e in entries if e.get("branch")}
    base = await _resolve_base_branch(workspace_path)
    active_sids, agents_db_ok = await _active_agent_short_ids(workspace_path)
    ok_b, branches_raw = await _git(
        ["branch", "--list", "hw/*/*", "--format=%(refname:short)"],
        workspace_path)
    branches = (
        [ln.strip() for ln in branches_raw.splitlines() if ln.strip()]
        if ok_b else []
    )
    merged_set: set[str] = set()
    if base:
        ok_m, merged_raw = await _git(
            ["branch", "--list", "hw/*/*", "--merged", base,
             "--format=%(refname:short)"],
            workspace_path,
        )
        if ok_m:
            merged_set = {ln.strip()
                          for ln in merged_raw.splitlines() if ln.strip()}
    elif branches:
        report["errors"].append("no main/master branch — skipped branch GC")

    conn = await _project_db_if_exists(workspace_path)
    for b in branches:
        if b in checked_out:
            continue  # 活跃 worktree 占用 — 非孤儿
        m_sid = _HW_SID_BRANCH_RE.match(b)
        branch_sid = m_sid.group(1) if m_sid else ""
        # Only flag agent-gone when agents table was readable.
        agent_gone = (
            bool(branch_sid)
            and agents_db_ok
            and branch_sid not in active_sids
        )

        m = _TASK_BRANCH_RE.match(b)
        reason = "legacy_unmerged"
        # Task liveness always wins: open tasks are never GC'd, even if the
        # assignee row is already archived (dismiss mid-flight).
        if m:
            if conn is None:
                candidate, reason = True, "task_missing_unmerged"
            else:
                candidate, reason = await _task_branch_candidate(
                    conn, m.group(1))
            if not candidate:
                continue  # 任务仍活跃 — 不动
            # Keep task_* reason as primary; agent_gone is annotated separately
            # on the report entry (orphan_agent_branches).
        elif agent_gone:
            reason = "orphan_agent_gone"
        if not base:
            continue  # 无法判定 merged — 不动 (errors 已记)
        if b in merged_set:
            ok_d, out_d = await _git(["branch", "-d", b], workspace_path)
            if ok_d:
                report["deleted_branches"].append(b)
                log.info("git_worktree.reconcile_branch_deleted",
                         workspace=workspace_path, branch=b,
                         reason=reason)
            else:
                report["errors"].append(f"branch -d {b} failed: {out_d}")
        else:
            # TEST21 M11: TTL GC for stale resubmit/hotfix suffix branches (>7d)
            _TTL_BRANCH_RE = re.compile(
                r"(?:/resubmit|/hotfix|/-b$|/-c$|/-d$|resubmit|hotfix)",
                re.IGNORECASE,
            )
            ttl_deleted = False
            # Never TTL-force-delete orphan_agent_gone tips — quarantine report only
            if (not agent_gone) and _TTL_BRANCH_RE.search(b):
                ok_ct, ct_out = await _git(
                    ["log", "-1", "--format=%ct", b], workspace_path
                )
                if ok_ct and (ct_out or "").strip().isdigit():
                    age_s = time.time() - int(ct_out.strip())
                    if age_s > 7 * 86400:
                        ok_d, out_d = await _git(
                            ["branch", "-D", b], workspace_path
                        )
                        if ok_d:
                            report["ttl_deleted_branches"].append(b)
                            log.info(
                                "git_worktree.reconcile_branch_ttl_deleted",
                                workspace=workspace_path,
                                branch=b,
                            )
                            ttl_deleted = True
                        else:
                            report["errors"].append(
                                f"branch -D {b} (ttl) failed: {out_d}"
                            )
            if ttl_deleted:
                continue
            ok_h, head = await _git(
                ["rev-parse", "--short", b], workspace_path)
            # S8: tip is-ancestor of main (False = highest-priority recovery)
            tip_is_ancestor = False
            ok_anc, _ = await _git(
                ["merge-base", "--is-ancestor", b, base], workspace_path
            )
            tip_is_ancestor = bool(ok_anc)
            entry = {
                "branch": b,
                "head": head.strip() if ok_h else "",
                "reason": reason,
                "short_id": branch_sid or None,
                "tip_is_ancestor_of_main": tip_is_ancestor,
                "agent_gone": agent_gone,
                "priority": (
                    "high" if (agent_gone and not tip_is_ancestor) else "normal"
                ),
            }
            # TEST6 evening P1-4: ancestor orphan_agent_gone past TTL → quarantine
            if agent_gone and tip_is_ancestor:
                ok_ct, ct_out = await _git(
                    ["log", "-1", "--format=%ct", b], workspace_path
                )
                if ok_ct and (ct_out or "").strip().isdigit():
                    age_s = time.time() - int(ct_out.strip())
                    if age_s > _ORPHAN_ANCESTOR_TTL_S:
                        q = await quarantine_orphan_branch(workspace_path, b)
                        if q.get("success"):
                            report.setdefault(
                                "quarantined_branches", []
                            ).append(
                                {
                                    **entry,
                                    "quarantine_ref": q.get("quarantine_ref"),
                                }
                            )
                            log.info(
                                "git_worktree.reconcile_orphan_quarantined",
                                workspace=workspace_path,
                                branch=b,
                                age_days=round(age_s / 86400, 1),
                            )
                            continue
            report["preserved_branches"].append(entry)
            if agent_gone:
                # Prefer explicit orphan reason in the orphan list for triage
                orphan_entry = dict(entry)
                if orphan_entry.get("reason") != "orphan_agent_gone":
                    orphan_entry["task_reason"] = orphan_entry["reason"]
                    orphan_entry["reason"] = "orphan_agent_gone"
                report["orphan_agent_branches"].append(orphan_entry)
            log.warning(
                "git_worktree.reconcile_branch_preserved",
                workspace=workspace_path,
                branch=b,
                reason=reason,
                tip_is_ancestor_of_main=tip_is_ancestor,
            )

    # ④ P0-1 startup reconciliation: closed tasks with branch tip ∉ main
    # → reopen merge obligation (catches stranded commits like 5510049)
    if conn is not None and base:
        try:
            import json as _json

            # Resolve project_id from workspace (needed for ObligationLedger)
            _recon_project_id: str | None = None
            try:
                from hiveweave.db import meta as _meta

                _rows = await _meta.query(
                    "SELECT id, workspace_path FROM projects"
                )
                for _p in _rows or []:
                    _pw = (_p["workspace_path"] or "").replace("\\", "/")
                    if _pw and os.path.normcase(_pw) == os.path.normcase(
                        workspace_path
                    ):
                        _recon_project_id = str(_p["id"] or "")
                        break
            except Exception:
                pass

            # aiosqlite.Row (sqlite3.Row) has no .get() — convert to dict.
            # Bug: 'sqlite3.Row' object has no attribute 'get' (audit 2026-07-28).
            cur = await conn.execute(
                "SELECT id, assignee_id, evidence, creator_id FROM tasks "
                "WHERE status = 'closed' AND closed_at IS NOT NULL "
                "ORDER BY closed_at DESC LIMIT 30"
            )
            _raw_rows = await cur.fetchall()
            await cur.close()
            rows = [dict(r) for r in _raw_rows]
            stranded: list[str] = []
            for row in rows:
                ev_raw = row.get("evidence") or "{}"
                try:
                    ev = _json.loads(ev_raw) if isinstance(ev_raw, str) else ev_raw
                except Exception:
                    ev = {}
                if not isinstance(ev, dict):
                    continue
                # Tasks that claim a merge happened → reopen merge obligation
                # if the tip is stranded. Tasks WITHOUT merge facts (e.g. VERIFY
                # reports committed to a branch) are still scanned for visibility
                # — their deliverables can strand invisibly (audit 2026-07-28:
                # Sage W1 VERIFY report 21d1697 stranded on hw/A015/work while
                # task was closed). We report those but don't reopen a merge
                # obligation (VERIFY reports aren't always meant to merge).
                has_merge = any(
                    ev.get(k) for k in (
                        "merged_by", "mergedBy", "merge_commit",
                        "merge_commit_hash", "mergeCommit",
                    )
                )
                # Resolve branch for this task's assignee
                assignee = row.get("assignee_id") or ""
                tid = str(row.get("id") or "")
                if not assignee or not tid:
                    continue
                # Try to find a branch matching this task
                task_branch = f"hw/%/t-{tid[:8]}"
                ok_tb, tb_out = await _git(
                    ["branch", "--list", task_branch, "--format=%(refname:short)"],
                    workspace_path,
                )
                candidate_branches = (
                    [ln.strip() for ln in (tb_out or "").splitlines() if ln.strip()]
                    if ok_tb else []
                )
                # Also check hw/<sid>/work fallback
                for cb in candidate_branches:
                    if cb in merged_set:
                        continue  # already in main — fine
                    ok_anc, _ = await _git(
                        ["merge-base", "--is-ancestor", cb, base],
                        workspace_path,
                    )
                    if not ok_anc:
                        stranded.append(tid)
                        log.warning(
                            "git_worktree.reconcile_stranded_closed_task",
                            workspace=workspace_path,
                            task_id=tid,
                            branch=cb,
                            has_merge_fact=has_merge,
                        )
                        # Reopen merge obligation ONLY when the task claimed a
                        # merge (avoids spurious obligations for docs-only or
                        # pure-VERIFY tasks).
                        if has_merge:
                            try:
                                from hiveweave.services.obligation import (
                                    ObligationLedger,
                                )

                                creator = row.get("creator_id") or assignee
                                if _recon_project_id:
                                    await ObligationLedger().create(
                                        _recon_project_id,
                                        str(creator),
                                        "merge",
                                        task_id=tid,
                                        context={
                                            "reason": "reconcile_stranded_tip",
                                            "source": "startup_reconcile",
                                            "branch": cb,
                                        },
                                    )
                            except Exception:
                                pass
                        break
            if stranded:
                report["stranded_closed_tasks"] = stranded
        except Exception as recon_err:
            report["errors"].append(
                f"stranded task reconciliation failed: {recon_err}"
            )

    # ⑤ TEST6 S8: notify project CEO about high-priority orphan tips (fail-open)
    orphans = report.get("orphan_agent_branches") or []
    high = [o for o in orphans if o.get("priority") == "high"]
    if high:
        try:
            await _notify_orphan_branches(workspace_path, high)
        except Exception as e:
            report["errors"].append(f"orphan notify failed: {e}")

    log.info("git_worktree.reconcile", workspace=workspace_path,
             pruned=report["pruned"], removed_dirs=report["removed_dirs"],
             deleted=len(report["deleted_branches"]),
             preserved=len(report["preserved_branches"]),
             orphan_agent=len(report.get("orphan_agent_branches") or []),
             errors=len(report["errors"]))
    return report


async def quarantine_orphan_branch(
    workspace_path: str, branch: str
) -> dict:
    """Move an orphan branch tip to refs/quarantine/* then delete the branch.

    Objects stay reachable via the quarantine ref — recoverable, not lost.
    TEST6 evening P1-4.
    """
    branch = (branch or "").strip()
    if not branch.startswith("hw/"):
        return {"success": False, "message": "only hw/* branches quarantined"}
    ok_h, tip = await _git(["rev-parse", branch], workspace_path)
    if not ok_h or not (tip or "").strip():
        return {"success": False, "message": f"cannot resolve {branch}"}
    tip = tip.strip()
    stamp = time.strftime("%Y%m%d%H%M%S")
    safe = branch.replace("/", "-")
    qref = f"refs/quarantine/{safe}-{stamp}"
    ok_u, out_u = await _git(
        ["update-ref", qref, tip], workspace_path
    )
    if not ok_u:
        return {
            "success": False,
            "message": f"update-ref failed: {out_u}",
        }
    ok_d, out_d = await _git(["branch", "-D", branch], workspace_path)
    if not ok_d:
        # Best-effort: leave quarantine ref even if branch delete failed
        return {
            "success": False,
            "quarantine_ref": qref,
            "tip": tip,
            "message": f"quarantine ref set but branch -D failed: {out_d}",
        }
    return {
        "success": True,
        "quarantine_ref": qref,
        "tip": tip,
        "branch": branch,
    }


async def _notify_orphan_branches(
    workspace_path: str, orphans: list[dict]
) -> None:
    """Best-effort inbox to CEO about unmerged agent-gone branches."""
    from hiveweave.db import meta as meta_db

    project_id = None
    try:
        rows = await meta_db.query("SELECT id, workspace_path FROM projects")
        for p in rows or []:
            pw = (p.get("workspace_path") or "").replace("\\", "/")
            if pw and os.path.normcase(pw) == os.path.normcase(workspace_path):
                project_id = str(p.get("id") or "")
                break
    except Exception:
        return
    if not project_id:
        return

    from hiveweave.services.org import OrgService

    agents = await OrgService().list_agents(project_id)
    ceo = next(
        (
            a for a in (agents or [])
            if (a.get("role") or "").strip().lower() == "ceo"
            and (a.get("status") or "").lower() != "archived"
        ),
        None,
    )
    if not ceo:
        return
    lines = [
        "[ORPHAN BRANCHES] Unmerged hw/* tips whose agent is gone "
        "(dismiss/reset residue). tip_is_ancestor_of_main=false — "
        "recover or explicitly discard:",
    ]
    for o in orphans[:8]:
        lines.append(
            f"  - {o.get('branch')} head={o.get('head')} "
            f"sid={o.get('short_id')}"
        )
    try:
        from hiveweave.services.inbox import InboxService

        key = "orphan-branches-" + ",".join(
            sorted(str(o.get("branch") or "") for o in orphans[:5])
        )
        await InboxService().send_message(
            from_agent_id="system",
            to_agent_id=str(ceo["id"]),
            message="\n".join(lines),
            message_type="system",
            priority="normal",
            wake=False,
            idempotency_key=key[:120],
        )
    except Exception as e:
        log.warning(
            "git_worktree.orphan_notify_failed",
            project_id=project_id,
            error=str(e),
        )
