"""GitWorktreeService create / checkpoint mixin."""
from __future__ import annotations

import asyncio
import os
import shutil
import time
from pathlib import Path
from typing import Any, List, TYPE_CHECKING

import structlog

from .constants import (
    CHECKPOINT_PREFIX,
    GENERATED_FILES,
    GIT_TIMEOUT,
    GITIGNORE_GENERATED_ENTRIES,
    QUARANTINE_DIR,
    TRACKED_WS_DIRS,
    WORKTREE_DIR,
    _RELOCATION_SUFFIXES,
    _WT_LIST_RE,
    _create_locks,
    _create_locks_guard,
    is_regenerable_path,
)
from .conflict_markers import _reject_if_markers_landed, scan_conflict_markers
from .git_cmd import _current_branch, _git, _resolve_base_branch
from .merge_support import (
    _auto_checkpoint_dirty_target,
    _merge_failure_result,
    parse_untracked_overwrite,
    quarantine_untracked_on_target,
)
from .naming import _branch_name, _slugify, compute_branch_name
from .paths import (
    _force_clear_path,
    _has_git,
    _is_bound_worktree_basename,
    _worktree_binding_under_project,
    _worktree_path,
)
from .porcelain import (
    _porcelain_non_hiveweave_dirty,
    _porcelain_tracked_dirty,
    _target_worktree_is_dirty,
)

log = structlog.get_logger(__name__)
from .reconcile import _log_worktree_rebuild_event


class CreateMixin:
    """ensure_git_repo / create / checkpoint."""

    if TYPE_CHECKING:
        # Provided by the MergeMixin composed into GitWorktreeService.
        # Declared here so mypy can resolve the cross-mixin reference.
        _resolve_effective_worktree_path: Any

    async def ensure_git_repo(self, workspace_path: str) -> dict:
        """Ensure workspace is a git repo. Auto-init + master→main if needed.

        初始化时自动 commit 现有项目文件到 main 分支，这样 worktree
        创建时能继承完整代码。.gitignore 排除 node_modules/.hiveweave 私有区。

        Returns ``{success, initialized}`` or ``{success: False, message}``.
        """
        if _has_git(workspace_path):
            # Existing repo — still patch .gitignore idempotently (TEST6 P1-A),
            # and migrate legacy ignore rules (workspace tracking) if found.
            await self._migrate_legacy_hiveweave_ignore(workspace_path)
            await self._ensure_gitignore_entries(workspace_path)
            return {"success": True, "initialized": False}

        ok, _ = await _git(["--version"], workspace_path)
        if not ok:
            return {"success": False, "message": "Git is not installed or not on PATH."}

        ok, _ = await _git(["init"], workspace_path)
        if not ok:
            return {"success": False, "message": "Failed to initialize git repository."}

        # Rename master → main (ignore failure — may already be main/trunk)
        await _git(["branch", "-m", "master", "main"], workspace_path)

        # Ensure git identity (needed for commits)
        await _git(["config", "user.email", "hiveweave@agent.local"], workspace_path)
        await _git(["config", "user.name", "HiveWeave Agent"], workspace_path)

        # 创建 .gitignore — 排除不应进入 worktree 的文件
        # (node_modules 每个 worktree 独立安装; .hiveweave 是平台私有目录;
        #  *.db 是数据库; dist/build 是构建产物; .env 是密钥)
        gitignore_path = Path(workspace_path) / ".gitignore"
        if not gitignore_path.exists():
            gitignore_content = """\
# HiveWeave 平台目录: 私有资产不入库 (worktrees/tool_outputs/logs/data.db 等)。
# shared/reports/drafts/handoffs 四目录为 agent 共享工作区, 反选入库 —
# 契约跨 worktree 可见可合并 (merge 可见冲突, 见 .gitattributes)。
.hiveweave/*
!.hiveweave/shared/
!.hiveweave/reports/
!.hiveweave/drafts/
!.hiveweave/handoffs/

# 依赖 (每个 worktree 独立安装)
node_modules/
.venv/
venv/

# 数据库
*.db
*.db-shm
*.db-wal

# 构建产物
dist/
build/
.next/
.nuxt/
.turbo/

# 密钥
.env
.env.*
!.env.example

# 缓存
__pycache__/
*.pyc
.cache/
coverage/

# 生成物 (tsc/vite/vitest 可再生输出 — TEST6 P1)
*.tsbuildinfo
test_output*.json
test-results/
playwright-report/

# 平台审计产物 (B-2: .audit 首轮审计遗留 untracked 噪音 + 不被 Remove-Item -Recurse 撞权限超时)
.audit/

# IDE
.idea/
.vscode/
"""
            gitignore_path.write_text(gitignore_content, encoding="utf-8")
        else:
            await self._ensure_gitignore_entries(workspace_path)

# P1-1: .gitattributes — lockfile union merge strategy.
        # package-lock.json conflicts are 100% predictable (every executor
        # runs npm install); union + post-merge regenerate eliminates rework.
        gitattributes_path = Path(workspace_path) / ".gitattributes"
        if not gitattributes_path.exists():
            gitattributes_content = """\
# HiveWeave P1-1: generated files use union merge (no content conflicts).
# Post-merge regeneration (npm install / pnpm install) fixes semantics.
package-lock.json merge=union
pnpm-lock.yaml merge=union
yarn.lock merge=union

# HiveWeave workspace shared docs: shared/ 契约用 binary (双方改动即冲突,
# 拒绝静默拼接成自相矛盾的规格); drafts/handoffs 是 append-only 日志,
# union 合并. reports/ 是 agent 产物, 走默认文本合并.
# `**` 让嵌套子目录 (shared/sub/contract.md) 也命中 — 单 `*` 只匹配直接子级.
.hiveweave/shared/**/*.md merge=binary
.hiveweave/drafts/**/*.md merge=union
.hiveweave/handoffs/**/*.md merge=union
"""
            gitattributes_path.write_text(gitattributes_content, encoding="utf-8")

        # 把现有项目文件 commit 到 main 分支
        await _git(["add", "-A"], workspace_path)
        ok, out = await _git(
            ["commit", "-m", "initial: project files imported by HiveWeave"],
            workspace_path,
        )
        if not ok:
            # 没有文件可 commit (空目录) — 用空提交兜底
            ok, _ = await _git(
                ["commit", "--allow-empty", "-m", "root: initialized by HiveWeave"],
                workspace_path,
            )
            if not ok:
                return {"success": False, "message": "Failed to create initial commit."}

        log.info("git_worktree.init_repo", workspace=workspace_path)
        return {"success": True, "initialized": True}

    async def _migrate_legacy_hiveweave_ignore(self, workspace_path: str) -> None:
        """Upgrade legacy ignore rules so shared workspace dirs become tracked.

        旧模板忽略整个 ``.hiveweave/`` (审计前), 新模板反选 shared/reports/
        drafts/handoffs 四目录入库。存量仓库的 `.gitignore` 是 tracked 文件,
        直接改会 dirty main 且触发 merge dirty gate; 所以迁移必须落成一次
        git 提交: 检查 → 重写 → ``git add`` + ``git commit`` (维护提交)。
        幂等: 无旧规则时 no-op。失败不致命 — 只记日志, 下次启动重试。
        """
        if not _has_git(workspace_path):
            return
        gi_path = Path(workspace_path) / ".gitignore"
        if not gi_path.exists():
            gi_path = Path(workspace_path) / ".git" / "info" / "exclude"
            if not gi_path.exists():
                return
        try:
            content = gi_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            log.warning("git_worktree.ignore_migrate_read_failed",
                        workspace=workspace_path, error=str(e))
            return
        lines = content.splitlines()
        has_legacy = any(ln.strip() == ".hiveweave/" for ln in lines)
        has_new = any(ln.strip() == ".hiveweave/*" for ln in lines) and any(
            ln.strip().startswith("!.hiveweave/") for ln in lines
        )
        if not has_legacy or has_new:
            return
        # Step 1: check nothing under .hiveweave is already tracked (lifting
        # the ignore must not silently fold pre-existing tracked helper files).
        ok_ls, ls_out = await _git(
            ["ls-files", ".hiveweave"], workspace_path
        )
        if ok_ls and (ls_out or "").strip():
            log.warning(
                "git_worktree.ignore_migrate_aborted_tracked_files",
                workspace=workspace_path,
                count=len([l for l in ls_out.splitlines() if l.strip()]),
            )
            return
        # P1-4：用户/外部 dirty 避让 —— 维护提交会与未保存的人工编辑赛跑
        # （report 实测：平台 05:55:47 自动提交 82d84a6 与用户手工编辑争同一
        # .gitignore，Agent stash 吞掉用户工作）。检测到 MAIN tracked dirty
        # 时跳过本次迁移，留到工作区干净后再试（幂等 no-op，下次启动重试）。
        ok_mig_st, mig_st_out = await _git(
            ["-c", "core.quotepath=false", "status", "--porcelain", "-z"],
            workspace_path,
        )
        if ok_mig_st:
            from .porcelain import _porcelain_tracked_dirty_paths

            migrate_dirty = _porcelain_tracked_dirty_paths(mig_st_out)
            # 只对 .gitignore/.gitattributes 自身 dirty 避让（迁移正写入的文件）；
            # 其它无关 dirty 不推迟迁移 —— 迁移提交 pathspec 只碰这两个文件，
            # 不会 sweep 用户其它未保存编辑。
            if migrate_dirty and any(
                p in (".gitignore", ".gitattributes") for p in migrate_dirty
            ):
                log.warning(
                    "git_worktree.ignore_migrate_deferred_user_dirty",
                    workspace=workspace_path,
                    paths=migrate_dirty[:5],
                    reason="human/external edits to the files being migrated",
                )
                return
        new_block = (
            "# HiveWeave 平台目录: 私有资产不入库 (worktrees/tool_outputs/)\n"
            "# logs/data.db 等)。shared/reports/drafts/handoffs 四目录为 agent\n"
            "# 共享工作区, 反选入库 — 契约跨 worktree 可见可合并。\n"
            ".hiveweave/*\n"
            "!.hiveweave/shared/\n"
            "!.hiveweave/reports/\n"
            "!.hiveweave/drafts/\n"
            "!.hiveweave/handoffs/\n"
        )
        out: list[str] = []
        for ln in lines:
            if ln.strip() == ".hiveweave/":
                out.extend(new_block.rstrip("\n").splitlines())
            else:
                out.append(ln)
        # .gitattributes may be absent in legacy repos — build the shared-docs
        # merge block and fold it into the same maintenance commit. Originals
        # are captured up front: the failed-add/failed-commit rollback must
        # restore them from memory (see _rollback_migration_files).
        attr_path = Path(workspace_path) / ".gitattributes"
        attr_block = (
            "\n# HiveWeave workspace shared docs: contracts use binary merge "
            "(conflicts visible, no silent splicing).\n"
            ".hiveweave/shared/**/*.md merge=binary\n"
            ".hiveweave/drafts/**/*.md merge=union\n"
            ".hiveweave/handoffs/**/*.md merge=union\n"
        )
        attr_existed = attr_path.exists()
        attr_original: str | None = None
        patched_attr = False
        attr_lines: list[str] = []
        try:
            if attr_existed:
                attr_original = attr_path.read_text(
                    encoding="utf-8", errors="replace"
                )
                attr_lines = attr_original.splitlines()
                if not any(
                    ".hiveweave/shared" in ln and "merge=binary" in ln
                    for ln in attr_lines
                ):
                    attr_lines.extend(attr_block.rstrip("\n").splitlines())
                    patched_attr = True
            else:
                attr_lines = attr_block.rstrip("\n").splitlines()
                patched_attr = True
        except OSError as e:
            log.warning("git_worktree.ignore_migrate_attr_read_failed",
                        workspace=workspace_path, error=str(e))
        try:
            gi_path.write_text("\n".join(out) + "\n", encoding="utf-8")
            if patched_attr:
                attr_path.write_text("\n".join(attr_lines) + "\n", encoding="utf-8")
            # Identity first: legacy repos may lack user.name/email — the
            # commit must not fail on that (audit P1: failure left .gitignore
            # rewritten-but-uncommitted and migration permanently stuck).
            id_args = ["-c", "user.name=HiveWeave Agent",
                       "-c", "user.email=hiveweave@agent.local"]
            # Pathspec commit: only the files we actually touched go into the
            # maintenance commit — never sweep unrelated dirty working files.
            # (.git/info/exclude fallback is untracked and needs no commit.)
            add_paths: list[str] = []
            if gi_path.name == ".gitignore":
                add_paths.append(".gitignore")
            if patched_attr:
                add_paths.append(".gitattributes")
            if not add_paths:
                log.info("git_worktree.ignore_migrate_nothing_committable",
                         workspace=workspace_path)
                return
            ok_add, add_out = await _git(
                id_args + ["add", "--"] + add_paths, workspace_path,
            )
            if not ok_add:
                await self._rollback_migration_files(
                    workspace_path, gi_path, content, attr_path,
                    attr_existed, attr_original,
                )
                log.warning(
                    "git_worktree.ignore_migrate_add_failed_rolled_back",
                    workspace=workspace_path, out=(add_out or "")[:200])
                return
            commit_ok, commit_out = await _git(
                id_args + ["commit", "-m",
                           "maintenance: track hiveweave shared workspace dirs"],
                workspace_path,
            )
            if commit_ok:
                log.info("git_worktree.ignore_migrated",
                         workspace=workspace_path)
                await self._quarantine_legacy_shared_wt_copies(workspace_path)
            elif "nothing to commit" in (commit_out or ""):
                pass
            else:
                # Roll back disk + index so the next startup retries —
                # otherwise the rewritten .gitignore is never committed and
                # main becomes permanently dirty (audit P1: the old single
                # `checkout HEAD -- <both>` pathspec failed wholesale when
                # .gitattributes was brand-new, restoring nothing).
                await self._rollback_migration_files(
                    workspace_path, gi_path, content, attr_path,
                    attr_existed, attr_original,
                )
                log.warning(
                    "git_worktree.ignore_migrate_commit_failed_rolled_back",
                    workspace=workspace_path, out=commit_out[:200])
        except OSError as e:
            log.warning("git_worktree.ignore_migrate_failed",
                        workspace=workspace_path, error=str(e))

    async def _rollback_migration_files(
        self,
        workspace_path: str,
        gi_path: Path,
        gi_original: str,
        attr_path: Path,
        attr_existed: bool,
        attr_original: str | None,
    ) -> None:
        """Restore .gitignore/.gitattributes (+ index) after a failed migration.

        P1-2 (audit): a single ``checkout HEAD -- <both>`` fails wholesale
        when .gitattributes is new (not in HEAD) — pathspec error, NOTHING
        restored, main left permanently dirty, and the migration no-ops on
        every future startup (the rewritten rules no longer contain the
        legacy ``.hiveweave/`` marker). Restore each file from the
        pre-migration in-memory original instead: files absent from HEAD are
        simply deleted; the ``.git/info/exclude`` fallback is rewritten (it
        is not a git path at all). Anything the failed add/commit staged is
        un-staged per path so the index does not linger dirty.
        """
        for rel in (".gitignore", ".gitattributes"):
            if not (Path(workspace_path) / rel).exists():
                continue
            ok_ls, ls_out = await _git(["ls-files", "--", rel], workspace_path)
            if ok_ls and (ls_out or "").strip():
                await _git(["reset", "--quiet", "HEAD", "--", rel],
                           workspace_path)
        try:
            gi_path.write_text(gi_original, encoding="utf-8")
        except OSError as e:
            log.warning("git_worktree.ignore_migrate_rollback_gi_failed",
                        workspace=workspace_path, error=str(e))
        try:
            if attr_existed and attr_original is not None:
                attr_path.write_text(attr_original, encoding="utf-8")
            elif attr_existed:
                # Original unreadable (OSError earlier) — fall back to HEAD.
                ok_e, _ = await _git(
                    ["cat-file", "-e", "HEAD:.gitattributes"], workspace_path
                )
                if ok_e:
                    await _git(
                        ["checkout", "HEAD", "--", ".gitattributes"],
                        workspace_path,
                    )
            else:
                attr_path.unlink(missing_ok=True)
        except OSError as e:
            log.warning("git_worktree.ignore_migrate_rollback_attr_failed",
                        workspace=workspace_path, error=str(e))

    async def _quarantine_legacy_shared_wt_copies(self, workspace_path: str) -> None:
        """Move pre-migration `.hiveweave/shared` copies inside EXISTING
        worktrees into the merge-quarantine dir.

        Before PR-A, every worktree got a plain file copy of shared/ (the old
        sync — untracked, never committed). After migration these untracked
        copies would make ``git merge main`` fail on the agent side*
        (untracked working tree files would be overwritten) with no
        quarantine path — deadlock. Preserve them, don't delete: content may
        be an agent's uncommitted contract edit.
        """
        if not _has_git(workspace_path):
            return
        ok_wt, wt_out = await _git(
            ["worktree", "list", "--porcelain"], workspace_path
        )
        if not ok_wt:
            return  # fall back to graceful no-op; merge-side gate still safe
        main_resolved = str(Path(workspace_path).resolve())
        wt_paths: list[str] = []
        for fld in (wt_out or "").split("\n\n"):
            lines = fld.splitlines()
            path_line = next((ln for ln in lines if ln.startswith("worktree ")),
                             None)
            if not path_line:
                continue
            wt = path_line[len("worktree "):].strip()
            if str(Path(wt).resolve()).lower() == main_resolved.lower():
                continue  # main checkout, not a linked worktree
            wt_paths.append(wt)
        for wt in wt_paths:
            await self._quarantine_legacy_shared_in_one(wt)

    async def _quarantine_legacy_shared_in_one(self, wt: str) -> None:
        """Move untracked legacy shared copies out of a single worktree.

        Only files that are NOT tracked in the worktree branch move — a
        tracked file is either the checkout of the real contract or the
        agent's committed edit; both are fine where they are. Untracked
        copies are moved (preserved, not deleted) under
        ``.hiveweave/merge-quarantine/legacy-sync-<stamp>/`` so the next
        ``git merge main`` does not fail with untracked overwrite.

        Detection uses ``git ls-files --others`` WITHOUT ``--exclude-standard``:
        legacy worktree branches still carry the OLD ``.gitignore`` that
        ignores all of ``.hiveweave/``, so the sync copies are IGNORED files
        — ``status --porcelain`` never lists them and the scan was a silent
        no-op (audit P1: the copies survived, then ``git merge main``
        silently overwrote them on fast-forward AND 3-way merge, losing the
        very agent contract edits this guard exists to preserve).
        ``ls-files --others`` lists ignored untracked files too, and never
        emits directory records.
        """
        hw = Path(wt) / ".hiveweave"
        if not hw.is_dir():
            return
        for tracked in TRACKED_WS_DIRS:
            if not (Path(wt) / tracked).is_dir():
                continue
            ok_ls, ls_out = await _git(
                ["ls-files", "-z", "--others", "--", tracked], wt,
            )
            if not ok_ls:
                continue
            untracked = [p for p in (ls_out or "").split("\x00") if p.strip()]
            if not untracked:
                continue
            stamp = f"legacy-sync-{int(time.time())}"
            qroot = Path(wt) / ".hiveweave" / "merge-quarantine" / stamp
            moved = 0
            for rel in untracked:
                src = Path(wt) / rel.replace("\\", "/")
                if not src.is_file():
                    continue
                dst = qroot / rel
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src), str(dst))
                    moved += 1
                except OSError as e:
                    log.warning(
                        "git_worktree.legacy_shared_quarantine_failed",
                        wt=wt, rel=rel, error=str(e))
            if moved:
                log.info(
                    "git_worktree.legacy_shared_quarantined",
                    wt=wt, sub=tracked, moved=moved, stamp=stamp,
                    files=[p for p in untracked if p.strip()][:10],
                )

    async def _ensure_gitignore_entries(self, workspace_path: str) -> None:
        """TEST6 P1-A: add GITIGNORE_GENERATED_ENTRIES to the repo-local
        exclude file (``.git/info/exclude``), idempotently.

        Deliberately NOT the tracked ``.gitignore``: patching a tracked file
        would itself dirty main and trip the merge dirty gate. info/exclude
        is untracked, shared by all worktrees, and has identical semantics.
        """
        exclude = Path(workspace_path) / ".git" / "info" / "exclude"
        try:
            existing = ""
            if exclude.exists():
                existing = exclude.read_text(
                    encoding="utf-8", errors="replace"
                )
            existing_lines = {
                ln.strip() for ln in existing.splitlines() if ln.strip()
            }
            missing = [
                e for e in GITIGNORE_GENERATED_ENTRIES
                if e not in existing_lines
            ]
            if not missing:
                return
            with open(exclude, "a", encoding="utf-8") as f:
                f.write(
                    "\n# HiveWeave generated artifacts (auto-appended)\n"
                    + "\n".join(missing)
                    + "\n"
                )
            log.info(
                "git_worktree.exclude_patched",
                workspace=workspace_path,
                added=missing,
            )
        except OSError as e:
            log.warning(
                "git_worktree.exclude_patch_failed",
                workspace=workspace_path,
                error=str(e),
            )

    def get_worktree_path(self, workspace_path: str, short_id: str) -> str | None:
        """Get the worktree path for an agent, or None if not found."""
        path = _worktree_path(workspace_path, short_id)
        return path if _has_git(path) else None

    # ── 1. CREATE ────────────────────────────────────────────

    async def create(self, workspace_path: str, short_id: str,
                     task_name: str | None = None,
                     base_branch: str = "main", *,
                     task_id: str | None = None) -> dict:
        """Allocate an isolated worktree + branch for a subordinate agent.

        task_name: DEPRECATED — 保留兼容旧调用方, 不再参与分支命名
        (P0 命名稳定化, 见 compute_branch_name)。

        Returns ``{success, path, branch}`` or ``{success: False, message}``.
        """
        lock_key = f"{Path(workspace_path).resolve()}::{short_id}"
        async with _create_locks_guard:
            lock = _create_locks.get(lock_key)
            if lock is None:
                lock = asyncio.Lock()
                _create_locks[lock_key] = lock
        async with lock:
            result = await self._create_unlocked(
                workspace_path, short_id, task_name, base_branch, task_id=task_id
            )
        # P1 (§7.1)：worktree standing ACE 后台铺设（幂等；沙箱 off 时 no-op）。
        # 首命令不再背传播成本。删→同路径重建走 verify-then-skip 兜底。
        if result.get("success") and result.get("path"):
            # Per-agent git 身份（同步写，先于 agent 首个 checkpoint）。
            await self._apply_agent_git_identity(result["path"], short_id)
            self._schedule_sandbox_grant(workspace_path, result["path"], short_id)
        return result

    async def _apply_agent_git_identity(
        self, worktree_path: str, short_id: str
    ) -> None:
        """Per-agent git 身份（用户需求：每个 agent 在 git 内有自己的名字）。

        worktree-local config 覆盖该 agent 的 checkpoint commit 与自发
        bash commit——fast-forward merge 后 main 的 git log 直接显示
        作者花名（「判断带出处」不变式的 git 侧落地）。

        - user.name  = agent 花名（resolve_agent 查不到时 fallback
          ``HiveWeave Agent <short_id>``，仍可与其它 agent 区分）
        - user.email = ``<short_id>@agents.hiveweave.local``（合成、
          ASCII、可从 email 反查 agent）
        - 前置：repo 级 ``extensions.worktreeConfig=true``（幂等开启）
          必须先于 ``--worktree`` 写入，顺序不可颠倒。
        - fail-quiet：身份写失败不阻塞 worktree 创建，回退 repo 级
          统一身份（HiveWeave Agent）。
        """
        try:
            ok, _ = await _git(
                ["config", "extensions.worktreeConfig", "true"],
                worktree_path,
            )
            if not ok:
                log.warning(
                    "git_worktree.agent_identity_worktree_config_off",
                    short_id=short_id,
                )
                return
            name: str | None = None
            try:
                from hiveweave.services.org import OrgService

                agent = await OrgService().resolve_agent(short_id)
                raw = (agent or {}).get("name")
                # 脏数据防御：非 str 或空白一律走 fallback（审计 P1-2）
                name = raw if isinstance(raw, str) and raw.strip() else None
            except Exception:
                name = None
            git_name = name or f"HiveWeave Agent {short_id}"
            email = f"{short_id}@agents.hiveweave.local"
            ok_n, _ = await _git(
                ["config", "--worktree", "user.name", git_name],
                worktree_path,
            )
            ok_e, _ = await _git(
                ["config", "--worktree", "user.email", email],
                worktree_path,
            )
            if not (ok_n and ok_e):
                # 审计 P1-1：--worktree 写失败时确保 repo 级至少有 fallback
                # 身份（存量/收养仓库可能完全没有 user.name）——否则
                # checkpoint 会以 "Please tell me who you are" 硬失败。
                # 只在缺失时补写，不覆盖既有身份。
                ok_chk, cur = await _git(
                    ["config", "user.name"], worktree_path
                )
                if not ok_chk or not (cur or "").strip():
                    await _git(
                        ["config", "user.name", git_name], worktree_path
                    )
                    await _git(
                        ["config", "user.email", email], worktree_path
                    )
                log.warning(
                    "git_worktree.agent_identity_worktree_write_failed",
                    short_id=short_id,
                    repo_level_fallback=True,
                )
            else:
                log.info(
                    "git_worktree.agent_identity_applied",
                    short_id=short_id,
                    git_name=git_name,
                )
        except Exception as e:
            log.warning(
                "git_worktree.agent_identity_failed",
                short_id=short_id,
                error=str(e),
            )

    def _schedule_sandbox_grant(
        self, project_root: str, worktree_path: str, short_id: str
    ) -> None:
        """后台铺 worktree 根的能力 SID ACE（§4.4/§7.1，fail-quiet）。"""
        try:
            from hiveweave.services.acl_sandbox.service import ensure_standing_grants

            async def _bg() -> None:
                try:
                    await ensure_standing_grants(
                        workspace_path=worktree_path,
                        project_workspace_path=project_root,
                        agent_id=short_id,
                    )
                except Exception as e:  # 铺授失败只告警，verify-then-skip 兜底
                    log.warning(
                        "acl_sandbox_worktree_grant_failed",
                        worktree=worktree_path, error=str(e),
                    )

            asyncio.create_task(_bg())
        except Exception as e:
            log.warning(
                "acl_sandbox_worktree_grant_spawn_failed",
                worktree=worktree_path, error=str(e),
            )

    async def _materialize_shared_dir(self, path: str) -> None:
        """P1-1: 物化 ``.hiveweave/shared/`` —— 空目录 git 不跟踪，worktree
        checkout 里不会物化，agent list_files 探路误报"不在树内"（report
        P1-1：两次被当"真不在树内"）。放一个 ``.keep.md`` 让 git 可跟踪：
        首个 checkpoint 后该目录在全部 worktree 常驻。best-effort 不阻塞创建。
        """
        try:
            from .constants import SHARED_DIR

            _sd = Path(path) / SHARED_DIR
            _sd.mkdir(parents=True, exist_ok=True)
            _keep = _sd / ".keep.md"
            if not _keep.exists():
                _keep.write_text(
                    "# HiveWeave shared workspace\n\n"
                    "Team-visible channel (read/write). Write: write_file to "
                    "this dir → checkpoint → merge; members see it after "
                    "their next worktree merge. Never put secrets here.\n",
                    encoding="utf-8",
                )
        except Exception:
            pass  # best-effort: don't fail worktree creation

    async def _create_unlocked(
        self,
        workspace_path: str,
        short_id: str,
        task_name: str | None = None,
        base_branch: str = "main",
        *,
        task_id: str | None = None,
    ) -> dict:
        repo = await self.ensure_git_repo(workspace_path)
        if not repo["success"]:
            return repo

        wt_root = Path(workspace_path) / WORKTREE_DIR
        wt_root.mkdir(parents=True, exist_ok=True)

        path = _worktree_path(workspace_path, short_id)
        branch = compute_branch_name(short_id, task_id)

        # Already exists and valid — idempotent.
        # P0 幂等脱钩修复: 返回 worktree 实际检出的分支, 不按当前入参
        # 新算 (新算名与检出分支可能不同: task_id 变化 / legacy slug 分支)。
        if _has_git(path):
            actual = await _current_branch(path)
            await self._materialize_shared_dir(path)
            return {
                "success": True,
                "path": path,
                "branch": actual or branch,
                "message": (
                    "worktree already exists. "
                    "Evidence files: prefix with short_id "
                    f"(e.g. {short_id}-verify.txt), never bare shared names."
                ),
                "cleared_error": True,
            }

        # Stale cleanup. Two failure modes we must handle before add:
        # 1) Path exists but is not a valid worktree (partial delete) →
        #    `worktree add` fails with "'<path>' already exists".
        # 2) Path is gone but git still has a registered worktree entry →
        #    add fails with "is a missing but registered worktree" until prune.
        # Always prune when the target is not a valid worktree.
        relocated = False  # P0-3: flag for caller to notify agent
        if Path(path).exists():
            # P0-3: stop dev servers that may lock files (WinError 32 root cause)
            try:
                from hiveweave.services.process_registry import (
                    stop_processes_for_worktree,
                )

                stop_processes_for_worktree(path)
            except Exception:
                pass

            if not _force_clear_path(path):
                # P0-3: try git worktree repair + second clear attempt
                # (correct action for stale metadata, per postmortem §P0-3)
                await _git(["worktree", "repair", path], workspace_path)
                if not _force_clear_path(path):
                    # Last resort: alternate directory name (disk truly corrupted).
                    # P0-3: this is NOT silent — relocated flag notifies agent.
                    original_path = path
                    for suffix in _RELOCATION_SUFFIXES:
                        alt = path + suffix
                        # L5: if alt already exists as a valid worktree, reuse it
                        # (idempotent — prevents -c/-d proliferation when -b is healthy)
                        if _has_git(alt):
                            log.info(
                                "git_worktree.stale_path_reuse_existing",
                                original=path,
                                reuse=alt,
                            )
                            path = alt
                            relocated = True
                            await _log_worktree_rebuild_event(
                                workspace_path,
                                short_id,
                                reason="stale_path_reuse_existing",
                                original=original_path,
                                path=alt,
                            )
                            break
                        if not Path(alt).exists():
                            log.warning(
                                "git_worktree.stale_path_fallback",
                                original=path,
                                fallback=alt,
                            )
                            path = alt
                            relocated = True
                            await _log_worktree_rebuild_event(
                                workspace_path,
                                short_id,
                                reason="stale_path_fallback",
                                original=original_path,
                                path=alt,
                            )
                            break
                    else:
                        return {
                            "success": False,
                            "message": (
                                f"Failed to create worktree: stale path locked "
                                f"and could not be cleared: {path}"
                            ),
                        }
        await _git(["worktree", "prune"], workspace_path)

        fwd_path = path.replace("\\", "/")

        # If the agent branch already exists (worktree dir deleted but branch
        # kept), attach to it — do NOT -B reset, or we wipe executor commits.
        ok_list, branch_list = await _git(
            ["branch", "--list", branch], workspace_path
        )
        branch_exists = bool(
            ok_list and any(
                ln.strip().lstrip("* ").strip() == branch
                for ln in branch_list.splitlines()
                if ln.strip()
            )
        )
        if branch_exists:
            ok, out = await _git(
                ["worktree", "add", fwd_path, branch], workspace_path
            )
            if ok:
                log.info("git_worktree.create", short_id=short_id,
                         branch=branch, base="existing-branch")
                await self._materialize_shared_dir(path)
                return {
                    "success": True,
                    "path": path,
                    "branch": branch,
                    "message": (
                        f"Worktree ready. Name evidence files with {short_id}- "
                        f"prefix to avoid merge collisions."
                    ),
                }
            last_error = out
            # Path-exists race: clear husk and retry attach once
            err_l = (out or "").lower()
            if "already exists" in err_l:
                _force_clear_path(path)
                await _git(["worktree", "prune"], workspace_path)
                ok2, out2 = await _git(
                    ["worktree", "add", fwd_path, branch], workspace_path
                )
                if ok2:
                    log.info(
                        "git_worktree.create_retry_after_path_exists",
                        short_id=short_id,
                        branch=branch,
                    )
                    await self._materialize_shared_dir(path)
                    return {
                        "success": True,
                        "path": path,
                        "branch": branch,
                        "message": (
                            f"Worktree ready. Name evidence files with "
                            f"{short_id}- prefix to avoid merge collisions."
                        ),
                    }
                last_error = out2 or out
            # Fall through: branch may be checked out elsewhere; try -B paths
        else:
            last_error = ""

        # 3-level fallback: origin/<base> → <base> → HEAD
        # HEAD 作为最终兜底（当前分支），避免在只有 main 的仓库上尝试不存在的 master
        # Use -b (create) when branch was absent; -B only as last resort after
        # attach failed (e.g. branch locked by another worktree).
        flag = "-B" if branch_exists else "-b"
        attempts = [
            ["worktree", "add", fwd_path, flag, branch, f"origin/{base_branch}"],
            ["worktree", "add", fwd_path, flag, branch, base_branch],
            ["worktree", "add", fwd_path, flag, branch, "HEAD"],
        ]
        for args in attempts:
            ok, out = await _git(args, workspace_path)
            if ok:
                log.info("git_worktree.create", short_id=short_id,
                         branch=branch, base=base_branch)
                await self._materialize_shared_dir(path)
                return {
                    "success": True,
                    "path": path,
                    "branch": branch,
                    "message": (
                        f"Worktree ready. Name evidence files with {short_id}- "
                        f"prefix to avoid merge collisions."
                    ),
                }
            last_error = out
            # branch_exists detection can miss (format/race); -b then fails with
            # "a branch named X already exists" — or path husk left → clear + attach.
            err_l = (out or "").lower()
            if "already exists" in err_l:
                _force_clear_path(path)
                await _git(["worktree", "prune"], workspace_path)
                ok_att, out_att = await _git(
                    ["worktree", "add", fwd_path, branch], workspace_path
                )
                if ok_att:
                    log.info(
                        "git_worktree.create_attached_after_exists",
                        short_id=short_id,
                        branch=branch,
                    )
                    await self._materialize_shared_dir(path)
                    return {
                        "success": True,
                        "path": path,
                        "branch": branch,
                        "message": (
                            f"Worktree ready (attached existing branch). "
                            f"Name evidence files with {short_id}- prefix."
                        ),
                    }
                last_error = out_att or out

        # Final heal: another path may have created a valid tree during races
        if _has_git(path):
            actual = await _current_branch(path)
            await self._materialize_shared_dir(path)
            log.info(
                "git_worktree.create_healed_existing",
                short_id=short_id,
                branch=actual or branch,
                prior_error=last_error,
            )
            return {
                "success": True,
                "path": path,
                "branch": actual or branch,
                "message": "worktree healthy after create race",
                "cleared_error": True,
            }

        log.error("git_worktree.create_failed", short_id=short_id,
                  path=path, branch=branch, error=last_error)
        return {"success": False, "message": f"Failed to create worktree: {last_error}"}
    # ── 2. CHECKPOINT ────────────────────────────────────────

    async def checkpoint(self, workspace_path: str, short_id: str,
                         message: str) -> dict:
        """Snapshot current state (git add -A + commit). No empty commits.

        Returns ``{success, hash, count}`` or ``{success: False, message}``.
        """
        path = await self._resolve_effective_worktree_path(
            workspace_path, short_id
        )
        if not Path(path).is_dir():
            return {"success": False,
                    "message": f"Worktree for {short_id} does not exist."}

        ok, add_out = await _git(["add", "-A"], path)
        if not ok:
            # P1-2: 失败必须透传 git 原始输出（此前丢 stderr 只回"Failed to
            # stage files"，agent 只能盲试 —— platform-issue-report P1-2 的
            # 19.1 分钟自救马拉松根因）。
            add_detail = (add_out or "").strip()
            detail = f" | git: {add_detail[:400]}" if add_detail else ""
            return {
                "success": False,
                "message": f"Failed to stage files{detail}",
            }

        # P1-1: strip GENERATED_FILES from staging (lockfiles cause predictable
        # merge conflicts; they should be regenerated post-merge, not committed).
        # TEST6 P1-B: regenerable artifacts (tsbuildinfo / test_output*.json)
        # are stripped too — and de-tracked when already tracked, so they stop
        # dirtying every future checkout (merge-blocking main dirt).
        regen_stripped: list[str] = []
        gen_stripped: list[str] = []
        # P1-2: 平台私有时运行时目录（.hiveweave/* 中非 TRACKED_WS_DIRS）绝不
        # 提交 —— npm 缓存等 untracked 运行时目录曾导致 git add -A 失败，
        # 触发 agent 19 分钟自救马拉松（platform-issue-report P1-2）。
        runtime_stripped: list[str] = []
        _keep_prefixes = tuple(f"{d}/" for d in TRACKED_WS_DIRS)
        try:
            ok_st, staged_out = await _git(
                ["diff", "--cached", "--name-only"], path
            )
            if ok_st and staged_out:
                for ln in staged_out.splitlines():
                    fname = ln.strip()
                    if not fname:
                        continue
                    # Match by basename (lockfile at any depth)
                    basename = fname.rsplit("/", 1)[-1] if "/" in fname else fname
                    if basename in GENERATED_FILES:
                        gen_stripped.append(fname)
                    elif is_regenerable_path(fname):
                        regen_stripped.append(fname)
                    elif fname.startswith(".hiveweave/") and not any(
                        fname.startswith(kp) for kp in _keep_prefixes
                    ):
                        runtime_stripped.append(fname)
                if gen_stripped:
                    await _git(
                        ["reset", "HEAD", "--"] + gen_stripped, path
                    )
                    log.info(
                        "checkpoint_generated_files_stripped",
                        short_id=short_id,
                        files=gen_stripped[:10],
                    )
                if regen_stripped:
                    # De-track: stage the removal so the merge lands
                    # "untracked + gitignored" on main, not just unstaged.
                    await _git(
                        ["rm", "--cached", "--quiet", "--"] + regen_stripped,
                        path,
                    )
                    log.info(
                        "checkpoint_regenerable_detracked",
                        short_id=short_id,
                        files=regen_stripped[:10],
                    )
                if runtime_stripped:
                    ok_rst, _ = await _git(
                        ["reset", "HEAD", "--"] + runtime_stripped, path
                    )
                    if not ok_rst:
                        # 兜底：reset 失败则 runtime 文件仍会进提交 —— 摘出
                        # 清单避免回执"声称已排除"而实际已提交（口径失真）；
                        # runtime_note 在函数后续 `if runtime_stripped:` 一并置空。
                        log.warning(
                            "checkpoint_platform_dirs_reset_failed",
                            short_id=short_id,
                            files=runtime_stripped[:10],
                        )
                        runtime_stripped = []
                    else:
                        log.info(
                            "checkpoint_platform_dirs_stripped",
                            short_id=short_id,
                            files=runtime_stripped[:10],
                        )
        except Exception:
            pass  # best-effort: don't fail checkpoint on strip

        # T1.2: 剥离说明进返回 message（此前只写 log.info，Agent 看不到）。
        # 措辞对齐 dirty 门禁新口径（T1.1）：生成物不再计入 dirty。
        generated_note = ""
        if gen_stripped:
            generated_note = (
                f" NOTE: {len(gen_stripped)} generated file(s) stripped by "
                f"policy (regenerated post-merge, never committed; the dirty "
                f"gate no longer counts them): "
                f"{', '.join(gen_stripped[:5])}"
                f"{'...' if len(gen_stripped) > 5 else ''}."
            )

        # Regenerable files get their own note — the generic ignored-files
        # warning below advises `git add -f`, which for this class just gets
        # stripped again next checkpoint (audit: dead-loop advice).
        regen_note = ""
        if regen_stripped:
            regen_note = (
                f" NOTE: {len(regen_stripped)} regenerable artifact(s) "
                f"de-tracked by design (never committed): "
                f"{', '.join(regen_stripped[:5])}"
                f"{'...' if len(regen_stripped) > 5 else ''}. To keep one "
                f"as evidence, rename it with your short_id prefix (e.g. "
                f"{short_id}-evidence.txt) and checkpoint again."
            )

        # P1-2: 平台运行时目录剥离也进返回 message（agent 可见，避免改
        # .gitignore 自救 —— report 歪招典藏①）。
        runtime_note = ""
        if runtime_stripped:
            runtime_note = (
                f" NOTE: {len(runtime_stripped)} platform runtime path(s) "
                f"under .hiveweave/ excluded from this commit (platform "
                f"private; never commit them): "
                f"{', '.join(runtime_stripped[:5])}"
                f"{'...' if len(runtime_stripped) > 5 else ''}."
            )

        # P1 fix(TEST10): 检测被 .gitignore 屏蔽的产物文件
        # 如果 worktree 中有文件被 ignore，checkpoint 不会包含它们，
        # merge 后产物会静默丢失。主动警告 agent。
        ignored_warning = ""
        try:
            # 列出所有未跟踪+被忽略的文件
            ok_ign, ign_out = await _git(
                ["status", "--porcelain", "--ignored", "-u"], path
            )
            if ok_ign and ign_out:
                ignored_files = [
                    ln[3:].strip() for ln in ign_out.split("\n")
                    if ln.startswith("!!")
                ]
                # 只关注可能是产物的文件（排除 .pyc/__pycache__/.hiveweave 等）
                # 再生文件走上面的 regen_note（`git add -f` 建议对它们是死循环）
                _NOISE = (".pyc", "__pycache__", ".hiveweave/", "node_modules/",
                          ".venv/", ".git/")
                product_ignored = [
                    f for f in ignored_files
                    if not any(n in f for n in _NOISE)
                    and not is_regenerable_path(f)
                ]
                if product_ignored:
                    ignored_warning = (
                        f" WARNING: {len(product_ignored)} file(s) are "
                        f".gitignore'd and will NOT be committed: "
                        f"{', '.join(product_ignored[:5])}"
                        f"{'...' if len(product_ignored) > 5 else ''}. "
                        f"Fix .gitignore or use `git add -f` to force-include."
                    )
                    log.warning(
                        "checkpoint_ignored_files",
                        short_id=short_id,
                        files=product_ignored[:10],
                    )
        except Exception:
            pass  # best-effort: don't fail checkpoint on ignore check

        # No changes → return current HEAD, count=0
        ok, status = await _git(["status", "--porcelain"], path)
        if ok and status == "":
            ok2, head = await _git(["rev-parse", "--short", "HEAD"], path)
            conflict_warning = await self._conflict_warning(path)
            return {"success": True, "hash": head if ok2 else "",
                    "count": 0,
                    "message": "no changes to commit" + ignored_warning
                               + generated_note + regen_note + conflict_warning}

        # T1.2/P0-1: 剥离后暂存区为空 = 剩余变更全是生成物/被忽略 —— 这是
        # 「按策略无事可提交」，不是失败。此前落到 git commit「nothing to
        # commit」→「Failed to create checkpoint commit」无原因失败（死锁
        # 的 checkpoint 侧）。
        ok_cached, cached_out = await _git(
            ["diff", "--cached", "--name-only"], path
        )
        if ok_cached and not (cached_out or "").strip():
            ok2, head = await _git(["rev-parse", "--short", "HEAD"], path)
            conflict_warning = await self._conflict_warning(path)
            return {
                "success": True,
                "hash": head if ok2 else "",
                "count": 0,
                "message": (
                    "no committable changes"
                    + ignored_warning + generated_note + regen_note
                    + runtime_note + conflict_warning
                ).strip(),
            }

        commit_msg = f"{CHECKPOINT_PREFIX} {message}"
        ok, commit_out = await _git(["commit", "-m", commit_msg], path)
        if not ok:
            # T1.2: 失败带 git commit 的 stderr/stdout（此前无原因失败），
            # 剥离清单非空时附上，便于区分「没东西可提交」与「真失败」。
            fail_detail = (commit_out or "").strip()
            stripped_all = gen_stripped + regen_stripped
            if stripped_all:
                stripped_note = (
                    "stripped-by-policy: " + ", ".join(stripped_all[:10])
                )
                fail_detail = (
                    f"{fail_detail} | {stripped_note}" if fail_detail
                    else stripped_note
                )
            return {
                "success": False,
                "message": (
                    "Failed to create checkpoint commit"
                    + (f": {fail_detail}" if fail_detail else "")
                ),
            }

        # 冲突预警在 commit 之后算: 本次存档新引入的变更参与预演(审计 P2-1),
        # 否则新写出的冲突要滞后一轮才暴露。
        conflict_warning = await self._conflict_warning(path)
        ok, head = await _git(["rev-parse", "--short", "HEAD"], path)
        count = await self._count_checkpoints(path)
        log.info("git_worktree.checkpoint", short_id=short_id,
                 hash=head if ok else "", count=count)
        return {"success": True, "hash": head if ok else "", "count": count,
                "message": (ignored_warning + generated_note + regen_note
                            + runtime_note + conflict_warning) or None}

    async def _conflict_warning(self, path: str) -> str:
        """checkpoint 回执的冲突预警文案(只提示, 绝不拦截——checkpoint 语义
        = 过程存档/回滚保险, 预警 fail-quiet)。"""
        try:
            from .conflict_predict import predict_merge_conflicts

            pred = await predict_merge_conflicts(path)
            if pred.status == "conflict":
                return (
                    f" WARNING: main 已领先 {pred.behind} 个提交, 且合并时将"
                    f"冲突: "
                    + (", ".join(pred.conflicts[:5])
                       if pred.conflicts else "(文件清单解析失败)")
                    + ("…" if len(pred.conflicts) > 5 else "")
                    + "。建议尽快在你的 worktree 执行 `git rebase main`"
                      "解决冲突后再继续。"
                )
            if pred.degraded and pred.behind > 0 and pred.ahead > 0:
                return (
                    f" NOTE: main 已领先 {pred.behind} 个提交(本机 git 过旧,"
                    f"无法预演冲突)。建议 `git rebase main` 后再继续。"
                )
        except Exception:
            pass  # fail-quiet: 预警绝不影响存档
        return ""

    async def _count_checkpoints(self, path: str) -> int:
        """Count checkpoint commits in the last 7 days."""
        ok, log_out = await _git(
            ["log", "--oneline", f"--grep={CHECKPOINT_PREFIX}",
             "--since=7 days ago"],
            path,
        )
        if ok and log_out:
            return len([ln for ln in log_out.split("\n") if ln.strip()])
        return 1

    # ── 3. MERGE ─────────────────────────────────────────────
