"""Worktree-aware review/merge helpers — avoid main-vs-worktree dual reality.

P0 contract (human-aligned):
- Review against assignee worktree, not main-only view.
- Merge conflicts: abort on main, executor rebases/merges main *in their worktree*.
- VERIFY is post-merge and scoped to the merged work, not every approved task.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.services.org import OrgService

log = structlog.get_logger(__name__)

# Strip accidental worktree prefixes from evidence.files_changed (TEST4).
# Matches: .hiveweave/worktrees/<sid>/, hiveweave/worktrees/<sid>/,
# and absolute paths containing those segments.
_WORKTREE_PREFIX_RE = re.compile(
    r"(?:^|/)\.?hiveweave/worktrees/[^/]+/",
    re.IGNORECASE,
)
_BARE_WORKTREE_PREFIX_RE = re.compile(
    r"(?:^|/)worktrees/[A-Za-z0-9_-]+/",
)


async def project_main_workspace(project_id: str) -> str | None:
    return await meta_db.get_project_workspace(project_id)


async def agent_worktree_path(agent_id: str) -> str | None:
    """Return agents.workspace_path if it looks like a live worktree dir."""
    row = await meta_db.get_agent_by_id(agent_id)
    if not row:
        try:
            a = await OrgService().resolve_agent(agent_id)
        except Exception:
            a = None
        row = a
    if not row:
        return None
    ws = (row.get("workspace_path") or "").strip()
    if ws:
        p = Path(ws)
        # TEST16 P0-2: require .git — aligned with agent.py lazy-create check.
        # Old: `any(p.iterdir())` passed node_modules husks as valid worktrees.
        if p.is_dir() and (p / ".git").exists():
            return str(p)
    # BUG-ORGWT（2026-08-05 feature-test 死锁根因）：DB workspace_path 为空/
    # 失效但规范位置 `.hiveweave/worktrees/<short_id>` 真实存在且 git 在册
    # 时，读路径兜底挂回。典型现场：HR 把 qa-family 角色（如 org测试工程师）
    # 误配 permission_type=coordinator —— hire 期建过树，但
    # `agent_gets_write_worktree`（coordinator 须 family==coordinator）不认，
    # heal/reconcile 跳过 → 树成孤儿，review 门禁 "no worktree path" 硬拒且
    # agent 侧无解法。此函数只用于 review/submit/dispatch 的只读审查定位，
    # 物理存在 + git 在册的树就是该 agent 的树，挂回严格优于硬拒。
    # 不授权任何写路径（写资格仍由 agent_gets_write_worktree 判定）。
    short_id = str(row.get("short_id") or "").strip()
    project_id = row.get("project_id")
    if short_id and project_id:
        main_ws = await project_main_workspace(str(project_id))
        if main_ws:
            canon = Path(main_ws) / ".hiveweave" / "worktrees" / short_id
            if canon.is_dir() and (canon / ".git").exists():
                log.info(
                    "worktree_review.orphan_worktree_rebound",
                    agent_id=agent_id,
                    short_id=short_id,
                    path=str(canon),
                )
                return str(canon)
    return None


def normalize_evidence_path(path: str | Any) -> str:
    """Normalize a claimed file path to repo-relative (main) form.

    Executors often pass worktree-relative or absolute worktree paths
    (e.g. ``.hiveweave/worktrees/A004/module_a.py``). Approve compares
    against the worktree checkout using repo-relative names.
    """
    s = str(path or "").replace("\\", "/").strip()
    if not s:
        return ""
    # Strip only "./" / leading "/", NOT every "." — lstrip("./") wrongly
    # turns ".editorconfig" into "editorconfig" (TEST5 approve miss).
    while s.startswith("./"):
        s = s[2:]
    s = s.lstrip("/")
    m = _WORKTREE_PREFIX_RE.search(s)
    if m:
        s = s[m.end() :]
    else:
        m2 = _BARE_WORKTREE_PREFIX_RE.search(s)
        if m2:
            s = s[m2.end() :]
    return s.lstrip("/")


def normalize_files_changed(files: list[Any] | None) -> list[str]:
    """Normalize + dedupe files_changed while preserving order."""
    out: list[str] = []
    seen: set[str] = set()
    for f in files or []:
        s = normalize_evidence_path(f)
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def hint_missing_file_locations(
    roots: list[str], missing_paths: list[str], max_hints: int = 4
) -> list[str]:
    """Leaf-name search under *roots* for *missing_paths*; return location hints.

    submit_task 的 files_changed 存在性校验拒绝时，agent 常因前缀/目录传错
    （TEST19：文件实际在 .hiveweave/reports/ 下，传了裸文件名）而困惑。
    按叶子名搜索找回真实位置，错误消息给出「found at …」提示。
    限量搜索（前 max_hints 个缺失路径、每路径最多 2 个命中）防性能问题；
    os.walk 原地剪掉 node_modules/.venv/.git/dist 等大目录（.hiveweave
    不剪——交付物常在其中）。
    """
    import os
    from pathlib import Path as _PH

    _SKIP_DIRS = {"node_modules", ".venv", "venv", ".git", "dist", "build"}

    def _walk(root: _PH, name: str) -> list[str]:
        hits: list[str] = []
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [
                    d for d in dirnames if d not in _SKIP_DIRS
                ]
                if name in filenames:
                    hits.append(str(_PH(dirpath) / name))
                    if len(hits) >= 2:
                        break
        except Exception:
            pass
        return hits

    hints: list[str] = []
    for fc in missing_paths[:max_hints]:
        found: list[str] = []
        for r in roots:
            found.extend(_walk(_PH(r), fc))
            if len(found) >= 2:
                break
        if found:
            # 显示相对对应 root 的路径（agent 提交时用的是相对路径）
            rel = found[0]
            for r in roots:
                try:
                    rel = str(_PH(rel).relative_to(_PH(r)))
                    break
                except Exception:
                    continue
            hints.append(f"'{fc}' found at {rel} — use that relative path")
    return hints


def _rel_paths(files: list[Any]) -> list[str]:
    return normalize_files_changed(list(files or []))


def _norm_set(files: list[Any] | None) -> set[str]:
    return set(_rel_paths(list(files or [])))


async def worktree_commits_ahead(
    main_ws: str, worktree_ws: str, *, target_branch: str = "main"
) -> int | None:
    """How many commits worktree HEAD is ahead of target branch tip.

    ``0`` → already on main (pure verification / already merged).
    ``None`` → could not determine (git error).
    """
    try:
        from hiveweave.services.git_worktree import _git

        ok_m, main_tip = await _git(
            ["rev-parse", target_branch], main_ws
        )
        if not ok_m or not (main_tip or "").strip():
            return None
        ok_w, wt_tip = await _git(["rev-parse", "HEAD"], worktree_ws)
        if not ok_w or not (wt_tip or "").strip():
            return None
        if main_tip.strip() == wt_tip.strip():
            return 0
        ok_c, count_out = await _git(
            ["rev-list", "--count", f"{main_tip.strip()}..HEAD"],
            worktree_ws,
        )
        if not ok_c:
            return None
        return int((count_out or "0").strip() or "0")
    except Exception as e:
        log.warning("worktree_commits_ahead_failed", error=str(e))
        return None


def _is_generated_untracked(path: str) -> bool:
    """True 当路径属于生成物（checkpoint 会剥离的那类）。

    生成物两套都算（T1.1 口径修正）：``GENERATED_FILES``（7 个 lockfile 名）
    + ``is_regenerable_path``（.tsbuildinfo / test_output*.json）。只排除
    这类路径，**不是排除全部 ``??``** —— untracked 新源码必须继续计入
    dirty，否则「零 commit + 纯 untracked 交付」会绕过 Rita escape 防线，
    且 ``org._in_progress_keep_status`` 会把 worktree 连同未提交源码删掉。
    """
    from hiveweave.services.git_worktree.constants import (
        GENERATED_FILES,
        is_regenerable_path,
    )

    norm = (path or "").strip().strip('"').replace("\\", "/")
    base = norm.rsplit("/", 1)[-1]
    return base in GENERATED_FILES or is_regenerable_path(norm)


#: porcelain XY 前缀 + 路径。``_git`` 对整段输出做了 strip()，首行的
#: 前导空格（X 位 = 未修改）可能被吃掉（`` M a`` → ``M a``），所以用
#: 容错正则而不是固定 [3:] 切片（审计修复过程中实测复现）。
_PORCELAIN_LINE_RE = re.compile(r"^[ MADRC?!UTXB]{0,2}\s+(.+)$")


def _porcelain_paths(ln: str) -> list[str]:
    """取 porcelain 行的路径。rename 行 ``R  old -> new`` 两侧都算。"""
    rest = (ln or "").strip()
    if not rest:
        return []
    m = _PORCELAIN_LINE_RE.match(rest)
    if not m:
        return []
    body = m.group(1).strip()
    if " -> " in body:
        old, new = body.split(" -> ", 1)
        return [old.strip().strip('"'), new.strip().strip('"')]
    return [body.strip().strip('"')]


async def worktree_dirty_counts(worktree_ws: str) -> dict[str, Any]:
    """Count dirty paths via ``git status --porcelain``.

    T1.1 口径（P0-1 审计后扩展到 tracked 侧）：``dirty_count`` = 可提交的
    变更数。**生成物**（``GENERATED_FILES`` / ``REGENERABLE_PATTERNS``，
    即 checkpoint 必然剥离、merge 后重新生成、永远不可能落进提交的那类）
    无论 untracked 还是 tracked-修改 都不计入 dirty —— 只计它们就是纯噪音，
    且会造成「checkpoint 报剥离、dirty 门禁又计数」的死锁闭环（tracked
    变体实测复现）。其余变更（tracked 修改 + 非生成物 untracked）正常计数，
    Rita-style 零 commit + untracked 源码交付仍被拦住。
    Returns ``{dirty_count, untracked_count, modified_count,
    generated_untracked, generated_paths}``.
    On git error, treats as dirty_count=1 so fail-closed callers stay safe.
    """
    empty_fail = {
        "dirty_count": 1,
        "untracked_count": 0,
        "modified_count": 1,
        "generated_untracked": 0,
        "generated_paths": [],
    }
    try:
        from hiveweave.services.git_worktree import _git

        ok, st = await _git(["status", "--porcelain"], worktree_ws)
        if not ok:
            return dict(empty_fail, generated_paths=[])
        lines = [ln for ln in (st or "").splitlines() if ln.strip()]
        untracked = 0
        modified = 0
        generated = 0
        generated_paths: list[str] = []
        dirty = 0
        for ln in lines:
            # porcelain v1: XY PATH — ?? = untracked, !! = ignored
            if ln.startswith("??") or ln.startswith("!!"):
                untracked += 1
                path = ln[2:].strip()
                if _is_generated_untracked(path):
                    generated += 1
                    generated_paths.append(path)
                else:
                    dirty += 1
            else:
                paths = _porcelain_paths(ln)
                if paths and all(_is_generated_untracked(p) for p in paths):
                    # tracked 生成物修改：checkpoint 必剥离（reset/checkout），
                    # 不可提交 → 与 untracked 生成物同口径，不计 dirty。
                    generated += 1
                    generated_paths.extend(paths)
                else:
                    modified += 1
                    dirty += 1
        return {
            "dirty_count": dirty,
            "untracked_count": untracked,
            "modified_count": modified,
            "generated_untracked": generated,
            "generated_paths": generated_paths,
        }
    except Exception as e:
        log.warning("worktree_dirty_counts_failed", error=str(e))
        return dict(empty_fail, generated_paths=[])


async def effective_delivery(
    main_ws: str,
    worktree_ws: str,
    *,
    target_branch: str = "main",
) -> dict[str, Any]:
    """Machine delivery fact for close/submit/approve gates (TEST20 N1).

    ``commits_ahead`` alone misses zero-commit + untracked delivery. Combine
    ahead + porcelain dirty so Rita-style empty closes cannot escape.
    """
    ahead = await worktree_commits_ahead(
        main_ws, worktree_ws, target_branch=target_branch
    )
    dirty = await worktree_dirty_counts(worktree_ws)
    ahead_n = int(ahead) if ahead is not None else 0
    has_output = (ahead is not None and ahead_n > 0) or dirty["dirty_count"] > 0
    return {
        "commits_ahead": ahead,
        "dirty_count": dirty["dirty_count"],
        "untracked_count": dirty["untracked_count"],
        "modified_count": dirty["modified_count"],
        "generated_untracked": dirty.get("generated_untracked", 0),
        "generated_paths": list(dirty.get("generated_paths", [])),
        "has_effective_output": has_output,
    }


def evidence_has_merge_fact(evidence: dict[str, Any] | None) -> bool:
    """True when evidence already records a real merge or explicit waive."""
    if not isinstance(evidence, dict):
        return False
    if evidence.get("merge_waived") is True or evidence.get("mergeWaived") is True:
        return True
    for key in (
        "merged_by",
        "mergedBy",
        "merge_commit",
        "merge_commit_hash",
        "mergeCommit",
        "mergeCommitHash",
    ):
        val = evidence.get(key)
        if val is not None and str(val).strip():
            return True
    return False


def evidence_merge_waived(evidence: dict[str, Any] | None) -> bool:
    if not isinstance(evidence, dict):
        return False
    return (
        evidence.get("merge_waived") is True
        or evidence.get("mergeWaived") is True
    )


def compare_worktree_to_main(
    *,
    main_ws: str,
    worktree_ws: str,
    files_changed: list[Any] | None,
    allow_empty_files: bool = False,
) -> tuple[str | None, dict[str, Any]]:
    """Return (deny_reason, meta) for review against assignee worktree.

    Deny when:
    - files_changed is empty AND allow_empty_files is False
      (no proof of what to review for a code-changing task)
    - any claimed file is missing in worktree
    - after stripping identical-to-main claims, nothing diverged remains
      (unless the whole claim set was already on main — BUG-9 auto-close)

    TEST21 M1: mixed identical + diverged no longer hard-denies the whole
    approve. Identical paths are stripped as ``confirmedOnMain``; only
    diverged files are required for review proof (completes BUG-9's other half).
    """
    meta: dict[str, Any] = {
        "mainWorkspace": main_ws,
        "worktreeWorkspace": worktree_ws,
        "checkedFiles": [],
        "divergedFiles": [],
        "identicalToMain": [],
        "missingInWorktree": [],
        "confirmedOnMain": [],
    }
    rels = _rel_paths(list(files_changed or []))
    if not rels:
        if allow_empty_files:
            meta["skipped"] = "empty_files_changed_allowed"
            return None, meta
        return (
            "Approve blocked: evidence.files_changed is empty. "
            "List the paths you reviewed in the assignee worktree "
            "(not main). Without that list there is no worktree proof. "
            "Pure verification / no-code tasks: submit with attestation "
            "and empty files_changed only when the worktree has 0 commits "
            "ahead of main (or tag the task VERIFY).",
            meta,
        )

    main_root = Path(main_ws)
    wt_root = Path(worktree_ws)
    for rel in rels[:40]:
        meta["checkedFiles"].append(rel)
        wt_f = wt_root / rel
        main_f = main_root / rel
        if not wt_f.is_file():
            meta["missingInWorktree"].append(rel)
            continue
        if not main_f.is_file():
            meta["divergedFiles"].append(rel)
            continue
        try:
            if wt_f.read_bytes() == main_f.read_bytes():
                meta["identicalToMain"].append(rel)
            else:
                meta["divergedFiles"].append(rel)
        except OSError:
            meta["missingInWorktree"].append(rel)

    if meta["missingInWorktree"]:
        return (
            "Approve blocked: claimed files_changed missing in assignee "
            f"worktree ({worktree_ws}). Review that worktree, not main. "
            f"Missing: {meta['missingInWorktree'][:8]}",
            meta,
        )
    # BUG-9: if every claimed file is already identical to MAIN, the work was
    # already merged (or landed on main another way). Allow approve so the
    # ledger can close instead of forcing cancel / dead tasks.
    if meta["identicalToMain"] and not meta["divergedFiles"]:
        meta["alreadyOnMain"] = True
        meta["autoCloseReason"] = "content_already_on_main"
        meta["confirmedOnMain"] = list(meta["identicalToMain"])
        return None, meta
    # TEST21 M1 / BUG-9 other half: mixed identical + diverged → strip
    # identical (confirmed already on main), review only diverged.
    if meta["identicalToMain"] and meta["divergedFiles"]:
        meta["confirmedOnMain"] = list(meta["identicalToMain"])
        meta["strippedIdentical"] = True
    if not meta["divergedFiles"]:
        return (
            "Approve blocked: no diverged files vs MAIN in assignee worktree.",
            meta,
        )
    return None, meta


def _is_no_code_evidence(evidence: dict[str, Any]) -> bool:
    """Explicit no-code / verification-only delivery flags on evidence."""
    for key in (
        "no_code_change",
        "noCodeChange",
        "verification_only",
        "verificationOnly",
    ):
        if evidence.get(key) is True:
            return True
    return False


async def autoderive_changed_files(
    main_ws: str,
    worktree_ws: str,
    *,
    target_branch: str = "main",
) -> list[str]:
    """TEST_DSH_32 P4：用 git 指纹自动生成变更清单。

    worktree 相对 main 的全部变更文件（committed diff + dirty + untracked），
    排除 ``.hiveweave/`` 内部路径。判定「改没改」看文件指纹，不逼人自填
    清单格子（verify-plan 空格仪式根因）。
    """
    try:
        from hiveweave.services.git_worktree import _git, _resolve_base_branch

        base = await _resolve_base_branch(main_ws) or target_branch
        files: list[str] = []
        ok_d, diff_out = await _git(
            ["diff", "--name-only", f"{base}...HEAD"], worktree_ws
        )
        if ok_d:
            files += [
                l.strip().replace("\\", "/")
                for l in (diff_out or "").splitlines()
                if l.strip()
            ]
        ok_s, st_out = await _git(
            ["status", "--porcelain"], worktree_ws
        )
        if ok_s:
            for line in (st_out or "").splitlines():
                if len(line) < 4:
                    continue
                path = line[3:].strip()
                # rename "old -> new" 取新路径
                if " -> " in path:
                    path = path.split(" -> ")[-1]
                path = path.strip('"').replace("\\", "/")
                if path:
                    files.append(path)
        seen: set[str] = set()
        out: list[str] = []
        for f in files:
            if f.startswith(".hiveweave") or f in seen:
                continue
            seen.add(f)
            out.append(f)
        return out[:40]
    except Exception as e:
        log.warning("autoderive_changed_files_failed", error=str(e))
        return []


async def review_worktree_gate(
    project_id: str,
    task: dict[str, Any],
    evidence: dict[str, Any],
) -> tuple[str | None, dict[str, Any]]:
    """Hard gate for approve: code tasks need worktree proof; verify/no-diff do not.

    Pure verification (VERIFY: title prefix, or 0 commits ahead of main with empty
    files_changed) must be approvable — otherwise CEO can only cancel.
    """
    from hiveweave.services.task import TaskService

    assignee = task.get("assignee_id")
    # TEST21 M2: evidence follows implementer worktree, not current assignee
    evidence_agent = task.get("implementer_id") or assignee
    main_ws = await project_main_workspace(project_id)
    if not main_ws:
        return None, {}
    if not evidence_agent:
        return None, {"mainWorkspace": main_ws}

    meta: dict[str, Any] = {
        "mainWorkspace": main_ws,
        "assigneeId": assignee,
        "implementerId": task.get("implementer_id"),
        "evidenceAgentId": evidence_agent,
    }

    # VERIFY child / tagged verify: delivery is attestation/script, not a diff
    if TaskService._is_verify_task(task):
        meta["skipped"] = "verify_task"
        return None, meta

    if _is_no_code_evidence(evidence):
        meta["skipped"] = "no_code_change_flag"
        return None, meta

    pinned_wt = (task.get("implementer_worktree") or "").strip() or None
    wt = pinned_wt
    if wt and not Path(wt).is_dir():
        wt = None
    if not wt:
        wt = await agent_worktree_path(str(evidence_agent))
    meta["worktreeWorkspace"] = wt
    if not wt:
        return (
            "Approve blocked: implementer/assignee has no worktree path. "
            "Executor worktrees are created on hire/dispatch — re-dispatch "
            "or wait for worktree heal, then review that tree (not main). "
            f"evidenceAgent={str(evidence_agent)[:8]}",
            meta,
        )

    files = evidence.get("files_changed") or evidence.get("filesChanged") or []
    ahead = await worktree_commits_ahead(main_ws, wt)
    meta["commitsAhead"] = ahead

    # Empty files_changed + worktree already on main → verification-only OK
    allow_empty = False
    if not _rel_paths(list(files or [])):
        if ahead == 0:
            allow_empty = True
            meta["skipped"] = "zero_commits_ahead"
        elif ahead is None:
            # Can't measure — still allow empty when attestation-backed
            # caller already passed attestation gate before this.
            aids = evidence.get("attestation_ids") or evidence.get(
                "attestationIds"
            )
            if isinstance(aids, list) and aids:
                allow_empty = True
                meta["skipped"] = "attestation_only_unknown_ahead"
        if not allow_empty:
            # TEST_DSH_32 P4（变更加密签名判定）：清单空但 worktree 有
            # 实际交付 → 用 git 指纹自动生成清单，不再打回原样重交。
            derived = await autoderive_changed_files(main_ws, wt)
            if derived:
                files = derived
                meta["files_changed_autoderived"] = derived
                log.info(
                    "evidence.files_changed_autoderived",
                    task_id=task.get("id"),
                    count=len(derived),
                )

    deny, cmp_meta = compare_worktree_to_main(
        main_ws=main_ws,
        worktree_ws=wt,
        files_changed=files,
        allow_empty_files=allow_empty,
    )
    meta.update(cmp_meta)
    # Prefer the more specific skip reason over compare's generic flag
    if allow_empty and ahead == 0:
        meta["skipped"] = "zero_commits_ahead"
    return deny, meta


# Human-aligned conflict ownership (NOT coordinator edit_file on aborted main)
MERGE_CONFLICT_HINT = (
    "[MERGE CONFLICT — EXECUTOR FIXES IN WORKTREE] "
    "Main merge was aborted (no conflict markers left on main). "
    "Coordinator: review_task(decision='rework') on the related task with the "
    "conflict file list — do NOT ask the executor to 'fix merge on main', "
    "and do NOT use bash/git CLI merge yourself. "
    "Executor: in YOUR worktree, merge or rebase main into your branch, "
    "resolve conflicts there, checkpoint, re-submit. "
    "Coordinator then retries git_worktree_merge. "
    "VERIFY is created only after a successful merge."
)

# Untracked on MAIN is a coordinator/main hygiene issue — NOT executor rework
UNTRACKED_ON_TARGET_HINT = (
    "[UNTRACKED ON MAIN — NOT A MERGE CONFLICT] "
    "Main has untracked files that would be overwritten by the merge. "
    "This is NOT an executor worktree problem — do NOT rework the assignee "
    "and do NOT ask them to 'fix it in the worktree'. "
    "Coordinator: quarantine/remove those untracked files on MAIN "
    "(or let git_worktree_merge auto-quarantine), then retry merge. "
    "If branch tip already equals main tip, merge is a no-op after cleanup."
)

# Back-compat alias for older imports / messages
COORDINATOR_MERGE_OWNERSHIP = MERGE_CONFLICT_HINT


def format_merge_conflict_message(
    *,
    branch: str,
    target: str,
    conflicts: list[str] | None,
) -> str:
    files = ", ".join((conflicts or [])[:12]) or "(unknown)"
    return (
        f"Merge conflict for {branch} into {target}. "
        f"Conflicted files: {files}.\n\n{MERGE_CONFLICT_HINT}"
    )


def format_untracked_on_target_message(
    *,
    branch: str,
    target: str,
    untracked: list[str] | None,
) -> str:
    files = ", ".join((untracked or [])[:12]) or "(unknown)"
    return (
        f"Merge blocked for {branch} into {target}: untracked files on "
        f"{target} would be overwritten: {files}.\n\n{UNTRACKED_ON_TARGET_HINT}"
    )


def select_tasks_for_merged_work(
    tasks: list[dict[str, Any]],
    *,
    assignee_id: str,
    merged_files: list[str] | None = None,
    statuses: tuple[str, ...] = ("approved", "verifying"),
) -> list[dict[str, Any]]:
    """Pick parent tasks that this merge actually covers.

    - One matching approved/verifying task → that one
    - Several → intersect evidence.files_changed with merged_files
    - Still ambiguous (same assignee, no file overlap) → **all** of them
      (same worktree merge covers that assignee's approved work; do not
      silently drop siblings with ``[:1]``)
    """
    from hiveweave.services.task import TaskService

    ts = TaskService()
    candidates: list[dict[str, Any]] = []
    for t in tasks:
        if ts._is_verify_task(t):
            continue
        if t.get("assignee_id") != assignee_id:
            continue
        if t.get("status") not in statuses:
            continue
        candidates.append(t)

    if not candidates:
        return []
    if len(candidates) == 1:
        return candidates

    merged = _norm_set(merged_files)
    if merged:
        matched: list[dict[str, Any]] = []
        for t in candidates:
            evidence = t.get("evidence") or {}
            if isinstance(evidence, str):
                import json

                try:
                    evidence = json.loads(evidence)
                except Exception:
                    evidence = {}
            if not isinstance(evidence, dict):
                evidence = {}
            claimed = _norm_set(
                evidence.get("files_changed") or evidence.get("filesChanged")
            )
            if claimed and claimed & merged:
                matched.append(t)
        if matched:
            return matched

    # Same assignee — this worktree merge covers all their approved tasks
    candidates.sort(key=lambda x: int(x.get("updated_at") or 0), reverse=True)
    return candidates


# ── Evidence verifiability (TEST11 evening P3 / action #9) ─────
# Structured only: files_changed existence + acceptance_criteria path tokens.
# Never scan free-text for intent keywords.

_KNOWN_FILE_EXTS = (
    ".py", ".ts", ".tsx", ".js", ".jsx", ".md", ".json", ".yml", ".yaml",
    ".toml", ".css", ".html", ".vue", ".go", ".rs", ".java", ".kt", ".sql",
    ".txt", ".sh", ".bat", ".ps1", ".cjs", ".mjs", ".scss", ".less",
)

# Path-like token: has a slash, or is a bare filename with a known extension.
# ASCII-only character class — \w matches CJK which causes false positives
# on Chinese acceptance criteria like "签到/排行榜" (TEST16 P0-1).
_PATH_TOKEN_RE = re.compile(
    r"(?:"
    r"(?:\.?/?[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+)"  # a/b or ./a/b
    r"|"
    r"(?:[A-Za-z0-9_.-]+\.(?:" + "|".join(e.lstrip(".") for e in _KNOWN_FILE_EXTS) + r"))"
    r")"
)


# Known project root prefixes — a slash-containing token is only treated as
# a path reference if it starts with one of these, or ends with a known ext.
_KNOWN_PATH_PREFIXES = (
    "src/", "apps/", "tests/", "test/", "docs/", "doc/", "lib/", "pkg/",
    "public/", "static/", "dist/", "build/", "scripts/", "config/",
    "components/", "pages/", "api/", "services/", "utils/", "data/",
    "evidence/", "assets/", "internal/", "cmd/", "spec/",
)


def _looks_like_real_path(token: str) -> bool:
    """Filter: only accept tokens that look like genuine file paths."""
    low = token.lower()
    if low.endswith(_KNOWN_FILE_EXTS):
        return True
    if any(low.startswith(pfx) for pfx in _KNOWN_PATH_PREFIXES):
        return True
    # Contains a dot-segment that looks like an extension in the last part
    last_seg = low.rsplit("/", 1)[-1] if "/" in low else low
    if "." in last_seg and len(last_seg.rsplit(".", 1)[-1]) <= 5:
        return True
    return False


def extract_acceptance_path_refs(criteria: Any) -> list[str]:
    """Extract filesystem path tokens from acceptance_criteria.

    Only structural path shapes — no natural-language intent guessing.
    """
    items: list[Any]
    if criteria is None:
        return []
    if isinstance(criteria, str):
        try:
            import json

            parsed = json.loads(criteria)
            items = parsed if isinstance(parsed, list) else [criteria]
        except Exception:
            items = [criteria]
    elif isinstance(criteria, list):
        items = criteria
    else:
        items = [criteria]

    found: list[str] = []
    seen: set[str] = set()
    for raw in items:
        text = str(raw or "").replace("\\", "/")
        if not text.strip():
            continue
        # Whole criterion is itself a path
        cand = text.strip().strip("`\"'")
        while cand.startswith("./"):
            cand = cand[2:]
        if ("/" in cand or cand.lower().endswith(_KNOWN_FILE_EXTS)) and " " not in cand:
            if _looks_like_real_path(cand):
                norm = normalize_evidence_path(cand)
                if norm and norm not in seen:
                    seen.add(norm)
                    found.append(norm)
            continue
        for m in _PATH_TOKEN_RE.finditer(text):
            token = m.group(0).strip().strip("`\"'")
            while token.startswith("./"):
                token = token[2:]
            if not _looks_like_real_path(token):
                continue
            norm = normalize_evidence_path(token)
            if norm and norm not in seen:
                seen.add(norm)
                found.append(norm)
    return found


def _resolve_evidence_roots(
    project_root: str | None,
    worktree: str | None,
) -> list[Path]:
    roots: list[Path] = []
    for r in (worktree, project_root):
        if not r:
            continue
        p = Path(r)
        if p.is_dir() and p not in roots:
            roots.append(p)
    return roots


def _path_exists_under(roots: list[Path], rel: str) -> bool:
    if not rel or not roots:
        return False
    rel_n = rel.replace("\\", "/").lstrip("/")
    for root in roots:
        cand = root / rel_n
        try:
            if cand.exists():
                return True
        except OSError:
            continue
        # Also try as absolute if rel somehow absolute after normalize
    abs_cand = Path(rel_n)
    if abs_cand.is_absolute():
        try:
            return abs_cand.exists()
        except OSError:
            return False
    return False


async def check_evidence_verifiable(
    project_id: str,
    task: dict[str, Any],
    evidence: dict[str, Any] | None,
) -> str | None:
    """Return deny reason if approve evidence is not structurally verifiable.

    Checks:
    1. Each ``evidence.files_changed`` path exists under assignee worktree
       or project root.
    2. Path tokens extracted from ``acceptance_criteria`` are either in
       ``files_changed`` or exist on disk.

    Skips VERIFY tasks and empty criteria+files (docs-only / no claims).
    """
    from hiveweave.services.task import TaskService

    if TaskService()._is_verify_task(task):
        return None

    ev = evidence if isinstance(evidence, dict) else {}
    claimed_raw = ev.get("files_changed") or ev.get("filesChanged") or []
    if not isinstance(claimed_raw, list):
        claimed_raw = []
    claimed = [
        normalize_evidence_path(p)
        for p in claimed_raw
        if normalize_evidence_path(p)
    ]
    path_refs = extract_acceptance_path_refs(task.get("acceptance_criteria"))

    if not claimed and not path_refs:
        return None  # nothing structural to verify

    project_root = await project_main_workspace(project_id)
    worktree = None
    evidence_agent = task.get("implementer_id") or task.get("assignee_id")
    pinned = (task.get("implementer_worktree") or "").strip() or None
    if pinned and Path(pinned).is_dir():
        worktree = pinned
    elif evidence_agent:
        worktree = await agent_worktree_path(str(evidence_agent))
    roots = _resolve_evidence_roots(project_root, worktree)

    missing_claimed = [
        p for p in claimed if not _path_exists_under(roots, p)
    ]
    uncovered_criteria: list[str] = []
    claimed_set = set(claimed)
    for pref in path_refs:
        if pref in claimed_set:
            continue
        # Prefix / basename soft match against claimed
        if any(
            c == pref or c.endswith("/" + pref) or pref.endswith("/" + c)
            for c in claimed_set
        ):
            continue
        if _path_exists_under(roots, pref):
            continue
        uncovered_criteria.append(pref)

    if not missing_claimed and not uncovered_criteria:
        return None

    # uncovered_criteria: path-like tokens from acceptance criteria text that
    # weren't found on disk.
    # P0-2 tiered enforcement (replaces TEST16 P0-1 blanket soft-pass):
    # - Milestone/SHIP tasks: HARD REJECT (this is the first root cause of
    #   CHANGELOG loss — approve with missing deliverables must be blocked).
    # - Other tasks: warning only (reviewer's judgment call, original intent).
    # Milestone detection uses STRUCTURED TAGS ONLY (no NL title/description
    # scraping — respects "language-agnostic, no text-guessing" hard rule).
    _MILESTONE_TAGS = frozenset({
        "milestone", "ship", "release", "e2e", "integration",
    })
    task_tags = task.get("tags") or []
    if isinstance(task_tags, str):
        try:
            import json as _json_mod
            task_tags = _json_mod.loads(task_tags)
        except Exception:
            task_tags = []
    tag_set = {str(t).lower() for t in task_tags} if isinstance(task_tags, list) else set()
    is_milestone = bool(tag_set & _MILESTONE_TAGS)

    if uncovered_criteria:
        if is_milestone:
            log.warning(
                "evidence.uncovered_criteria_hard_reject",
                task_id=task.get("id"),
                refs=uncovered_criteria[:8],
                tags=sorted(tag_set & _MILESTONE_TAGS),
            )
            return (
                "Cannot approve milestone task: acceptance criteria reference "
                "paths not found on disk or in files_changed: "
                + ", ".join(uncovered_criteria[:8])
                + ("…" if len(uncovered_criteria) > 8 else "")
                + ". Deliverables must exist before SHIP approve. "
                "Ask assignee to create the missing files and resubmit, "
                "or waive_attestation with CEO-level audit reason."
            )
        else:
            log.warning(
                "evidence.uncovered_criteria_warning",
                task_id=task.get("id"),
                refs=uncovered_criteria[:8],
            )

    # Only missing_claimed (agent explicitly claimed these files in
    # files_changed but they don't exist) is a hard deny.
    if not missing_claimed:
        return None

    parts: list[str] = [
        "Cannot approve: evidence not structurally verifiable."
    ]
    parts.append(
        "files_changed missing on disk: "
        + ", ".join(missing_claimed[:8])
        + ("…" if len(missing_claimed) > 8 else "")
    )
    parts.append(
        "Ask assignee to resubmit with existing paths, or rework."
    )
    return " ".join(parts)
