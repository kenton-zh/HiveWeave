"""Git worktree service — isolated worktrees per agent (contract 09).

契约 09: Git Worktree
- 每个叶子 agent 分配隔离 worktree: <workspace>/.hiveweave/worktrees/<shortId>/
- 分支命名: hw/<shortId>/<task-slug>
- Coordinator 全权管理生命周期 (coordinator-only)
- 7 个操作: create / list / checkpoint / merge / rollback / delete / info
- merge 用 --no-edit (非 ff-only), 成功后自动删除 worktree+分支
- rollback 前先 checkpoint 存档 (安全加固, 源码未做)
- git 命令 30s 超时
- slugify 保留 CJK (\\u4e00-\\u9fff), 空串→"task"
- base_branch 三级回退: origin/<base> → <base> → master
"""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

WORKTREE_DIR = ".hiveweave/worktrees"
CHECKPOINT_PREFIX = "checkpoint:"
GIT_TIMEOUT = 30.0
SLUG_MAX_LEN = 40

# slugify regexes (契约 09: 保留 CJK \u4e00-\u9fff)
_SLUG_SPACE = re.compile(r"[\s/\\]+")
_SLUG_INVALID = re.compile(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]+")
_SLUG_TRIM = re.compile(r"^-+|-+$")

# Match: "<path>  <hash> [<branch>]" from `git worktree list`
_WT_LIST_RE = re.compile(r"^(.+?)\s+([a-f0-9]+)\s*(?:\[(.+?)\])?$")


def _slugify(name: str) -> str:
    """Slugify a task name (契约 09 slugify 规则).

    1. 空格/正反斜杠 → "-"
    2. 删除非 [a-zA-Z0-9_-] 和 CJK 以外字符
    3. 截断至 40 字符
    4. 去除首尾连字符
    5. 空串 → "task"
    """
    s = _SLUG_SPACE.sub("-", name)
    s = _SLUG_INVALID.sub("", s)
    s = s[:SLUG_MAX_LEN]
    s = _SLUG_TRIM.sub("", s)
    return s or "task"


def _branch_name(short_id: str, task_name: str) -> str:
    return f"hw/{short_id}/{_slugify(task_name)}"


def _worktree_path(workspace_path: str, short_id: str) -> str:
    return str(Path(workspace_path) / WORKTREE_DIR / short_id)


def _has_git(path: str) -> bool:
    return (Path(path) / ".git").exists()


async def _git(args: list[str], cwd: str, timeout: float = GIT_TIMEOUT) -> tuple[bool, str]:
    """Run a git command, return (success, output).

    stderr merged into stdout (mirrors Elixir stderr_to_stdout: true).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        return False, "git not found on PATH"

    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        cmd_preview = " ".join(args[:2])
        return False, f"git {cmd_preview} timed out after {timeout}s"

    output = stdout.decode("utf-8", errors="replace").strip() if stdout else ""
    if proc.returncode == 0:
        return True, output
    return False, output


class GitWorktreeService:
    """GitWorktreeService — isolated worktrees per agent, managed by coordinators.

    契约 09: coordinator-only. 7 operations: create / list / checkpoint /
    merge / rollback / delete / info. Each returns a dict with ``success``
    plus operation-specific fields (and ``message`` on error).
    """

    # ── helpers ──────────────────────────────────────────────

    async def ensure_git_repo(self, workspace_path: str) -> dict:
        """Ensure workspace is a git repo. Auto-init + master→main if needed.

        初始化时自动 commit 现有项目文件到 main 分支，这样 worktree
        创建时能继承完整代码。.gitignore 排除 node_modules/.hiveweave 等。

        Returns ``{success, initialized}`` or ``{success: False, message}``.
        """
        if _has_git(workspace_path):
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
        # (node_modules 每个 worktree 独立安装; .hiveweave 是系统目录;
        #  *.db 是数据库; dist/build 是构建产物; .env 是密钥)
        gitignore_path = Path(workspace_path) / ".gitignore"
        if not gitignore_path.exists():
            gitignore_content = """\
# HiveWeave 系统目录 (worktree 不继承)
.hiveweave/

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

# IDE
.idea/
.vscode/
"""
            gitignore_path.write_text(gitignore_content, encoding="utf-8")

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

    def get_worktree_path(self, workspace_path: str, short_id: str) -> str | None:
        """Get the worktree path for an agent, or None if not found."""
        path = _worktree_path(workspace_path, short_id)
        return path if _has_git(path) else None

    # ── 1. CREATE ────────────────────────────────────────────

    async def create(self, workspace_path: str, short_id: str, task_name: str,
                     base_branch: str = "main") -> dict:
        """Allocate an isolated worktree + branch for a subordinate agent.

        Returns ``{success, path, branch}`` or ``{success: False, message}``.
        """
        repo = await self.ensure_git_repo(workspace_path)
        if not repo["success"]:
            return repo

        wt_root = Path(workspace_path) / WORKTREE_DIR
        wt_root.mkdir(parents=True, exist_ok=True)

        path = _worktree_path(workspace_path, short_id)
        branch = _branch_name(short_id, task_name)

        # Already exists and valid — idempotent
        if _has_git(path):
            return {"success": True, "path": path, "branch": branch,
                    "message": "worktree already exists"}

        # Stale directory cleanup: if the worktree directory exists but .git
        # is missing (e.g., partial deletion), git worktree add will fail with
        # "'<path>' already exists". Remove the stale directory and prune.
        if Path(path).exists():
            import shutil as _shutil
            _shutil.rmtree(path, ignore_errors=True)
            await _git(["worktree", "prune"], workspace_path)
            # Also delete stale branch ref so -B doesn't conflict
            await _git(["branch", "-D", branch], workspace_path)

        # 3-level fallback: origin/<base> → <base> → HEAD
        # HEAD 作为最终兜底（当前分支），避免在只有 main 的仓库上尝试不存在的 master
        fwd_path = path.replace("\\", "/")
        attempts = [
            ["worktree", "add", fwd_path, "-B", branch, f"origin/{base_branch}"],
            ["worktree", "add", fwd_path, "-B", branch, base_branch],
            ["worktree", "add", fwd_path, "-B", branch, "HEAD"],
        ]
        for args in attempts:
            ok, out = await _git(args, workspace_path)
            if ok:
                log.info("git_worktree.create", short_id=short_id,
                         branch=branch, base=base_branch)
                return {"success": True, "path": path, "branch": branch}
            last_error = out

        log.error("git_worktree.create_failed", short_id=short_id,
                  path=path, branch=branch, error=last_error)
        return {"success": False, "message": f"Failed to create worktree: {last_error}"}

    # ── 2. CHECKPOINT ────────────────────────────────────────

    async def checkpoint(self, workspace_path: str, short_id: str,
                         message: str) -> dict:
        """Snapshot current state (git add -A + commit). No empty commits.

        Returns ``{success, hash, count}`` or ``{success: False, message}``.
        """
        path = _worktree_path(workspace_path, short_id)
        if not Path(path).is_dir():
            return {"success": False,
                    "message": f"Worktree for {short_id} does not exist."}

        ok, _ = await _git(["add", "-A"], path)
        if not ok:
            return {"success": False, "message": "Failed to stage files"}

        # No changes → return current HEAD, count=0
        ok, status = await _git(["status", "--porcelain"], path)
        if ok and status == "":
            ok2, head = await _git(["rev-parse", "--short", "HEAD"], path)
            return {"success": True, "hash": head if ok2 else "",
                    "count": 0, "message": "no changes to commit"}

        commit_msg = f"{CHECKPOINT_PREFIX} {message}"
        ok, _ = await _git(["commit", "-m", commit_msg], path)
        if not ok:
            return {"success": False, "message": "Failed to create checkpoint commit"}

        ok, head = await _git(["rev-parse", "--short", "HEAD"], path)
        count = await self._count_checkpoints(path)
        log.info("git_worktree.checkpoint", short_id=short_id,
                 hash=head if ok else "", count=count)
        return {"success": True, "hash": head if ok else "", "count": count}

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

    async def merge(self, workspace_path: str, short_id: str, task_name: str,
                    target_branch: str = "main") -> dict:
        """Merge agent branch into target (git merge --no-edit), then cleanup.

        契约 09 RECONCILE: 用 --no-edit (非 ff-only), 成功后自动 remove worktree+分支.
        冲突时 abort, worktree 保留.

        Returns ``{success, merged, hash}`` or ``{success: False, message}``.
        """
        branch = _branch_name(short_id, task_name)

        ok, _ = await _git(["checkout", target_branch], workspace_path)
        if not ok:
            return {"success": False,
                    "message": f"Failed to checkout {target_branch}"}

        ok, _ = await _git(["merge", branch, "--no-edit"], workspace_path)
        if not ok:
            # Abort merge — worktree+branch preserved for retry/rollback
            await _git(["merge", "--abort"], workspace_path)
            return {"success": False,
                    "message": f"Merge conflict for {short_id} into "
                               f"{target_branch}. Resolve manually or rollback."}

        ok, head = await _git(["rev-parse", "--short", "HEAD"], workspace_path)

        # Auto-remove worktree + branch on success (契约 09 RECONCILE)
        await self.delete(workspace_path, short_id, task_name)

        log.info("git_worktree.merge", short_id=short_id,
                 target=target_branch, hash=head if ok else "")
        return {"success": True, "merged": True, "hash": head if ok else ""}

    async def merge_by_branch(self, workspace_path: str, branch: str,
                              target_branch: str = "main") -> dict:
        """Merge a specific branch by full name (Bug G fix + Bug L enhancement).

        Enhanced merge flow:
        1. Rebase worktree branch onto latest target_branch (reduces conflicts)
        2. Attempt git merge
        3. On conflict: try semantic merge for package.json, report conflict files
        4. Post-merge verification: check key files exist
        5. Auto-remove worktree on success

        Returns ``{success, merged, hash, message?, conflicts?}`` or
        ``{success: False, message, conflicts?}``.
        """
        import json as _json
        from pathlib import Path as _Path

        # Step 0: Fetch latest target_branch
        ok, _ = await _git(["checkout", target_branch], workspace_path)
        if not ok:
            return {"success": False,
                    "message": f"Failed to checkout {target_branch}"}

        # Step 1: Rebase worktree branch onto target_branch to minimize conflicts
        parts = branch.split("/", 2)
        short_id = parts[1] if len(parts) >= 2 else ""
        wt_path = _worktree_path(workspace_path, short_id) if short_id else ""

        if wt_path and _Path(wt_path).is_dir():
            # Checkpoint worktree state before rebase
            await _git(["add", "-A"], wt_path)
            await _git(["commit", "-m", "pre-merge-checkpoint", "--allow-empty"],
                       wt_path)
            # Rebase onto target_branch
            ok_reb, reb_out = await _git(
                ["rebase", target_branch], wt_path)
            if not ok_reb:
                # Rebase conflict — abort rebase, continue with 3-way merge
                await _git(["rebase", "--abort"], wt_path)
                log.warning("git_worktree.rebase_failed",
                            branch=branch, output=reb_out[:200])

        # Step 2: Merge with target_branch
        ok, merge_out = await _git(["merge", branch, "--no-edit"], workspace_path)

        if not ok:
            # Step 3: Conflict handling — identify conflicted files
            ok_diff, diff_out = await _git(
                ["diff", "--name-only", "--diff-filter=U"], workspace_path)
            conflict_files = [f.strip() for f in diff_out.split("\n") if f.strip()]

            # Try semantic merge for package.json
            resolved = []
            pkg_path = _Path(workspace_path) / "package.json"
            if "package.json" in conflict_files and pkg_path.exists():
                try:
                    # Get the three versions
                    ok_ours, ours_raw = await _git(
                        ["show", f"{target_branch}:package.json"], workspace_path)
                    ok_theirs, theirs_raw = await _git(
                        ["show", f"{branch}:package.json"], workspace_path)
                    if ok_ours and ok_theirs:
                        ours = _json.loads(ours_raw)
                        theirs = _json.loads(theirs_raw)
                        # Merge dependencies semantically
                        for dep_key in ("dependencies", "devDependencies",
                                        "peerDependencies", "scripts"):
                            if dep_key in ours or dep_key in theirs:
                                merged_deps = ours.get(dep_key, {})
                                merged_deps.update(theirs.get(dep_key, {}))
                                ours[dep_key] = merged_deps
                        pkg_path.write_text(
                            _json.dumps(ours, indent=2, ensure_ascii=False) + "\n",
                            encoding="utf-8")
                        await _git(["add", "package.json"], workspace_path)
                        resolved.append("package.json")
                except Exception as e:
                    log.warning("git_worktree.package_merge_failed",
                                branch=branch, error=str(e))

            if resolved:
                # Re-attempt commit after resolving conflicts
                ok_commit, _ = await _git(
                    ["commit", "--no-edit"], workspace_path)
                if ok_commit:
                    ok = True  # merge succeeded after resolution
                    log.info("git_worktree.semantic_merge_resolved",
                             branch=branch, resolved_files=resolved)

            if not ok:
                # Report conflict files to agent
                await _git(["merge", "--abort"], workspace_path)
                return {"success": False,
                        "message": f"Merge conflict for {branch} into "
                                   f"{target_branch}. Conflicted files: "
                                   f"{', '.join(conflict_files[:10])}",
                        "conflicts": conflict_files}

        # Step 4: Post-merge verification
        verification_errors = []
        pkg_path = _Path(workspace_path) / "package.json"
        if pkg_path.exists():
            try:
                pkg = _json.loads(pkg_path.read_text(encoding="utf-8"))
                if not pkg.get("scripts"):
                    verification_errors.append("package.json missing scripts")
                if not pkg.get("dependencies") and not pkg.get("devDependencies"):
                    verification_errors.append("package.json missing dependencies")
            except Exception:
                verification_errors.append("package.json is invalid JSON")

        if verification_errors:
            log.warning("git_worktree.merge_verification_failed",
                        branch=branch, errors=verification_errors)
            # Don't rollback — warn but allow (agent can fix)

        ok, head = await _git(["rev-parse", "--short", "HEAD"], workspace_path)

        # Step 5: Auto-remove worktree + branch on success
        if short_id:
            try:
                await self.delete(workspace_path, short_id,
                                  parts[2] if len(parts) > 2 else "task")
            except Exception:
                pass  # worktree 可能已不存在

        log.info("git_worktree.merge_by_branch", branch=branch,
                 target=target_branch, hash=head if ok else "",
                 warnings=verification_errors)
        result = {"success": True, "merged": True, "hash": head if ok else ""}
        if verification_errors:
            result["warnings"] = verification_errors
        return result

    # ── 4. ROLLBACK ─────────────────────────────────────────

    async def rollback(self, workspace_path: str, short_id: str,
                       commit_hash: str | None = None) -> dict:
        """Reset worktree to a previous checkpoint (or latest checkpoint).

        契约 09 安全加固: rollback 前先 checkpoint 存档当前状态 (源码未做).

        Returns ``{success, hash, message}`` or ``{success: False, message}``.
        """
        path = _worktree_path(workspace_path, short_id)
        if not Path(path).is_dir():
            return {"success": False,
                    "message": f"Worktree for {short_id} does not exist."}

        # Safety: snapshot current state before discarding (契约 09 安全加固)
        await self.checkpoint(workspace_path, short_id, "pre-rollback-snapshot")

        target = commit_hash
        if not target:
            ok, h = await _git(
                ["log", "--format=%H", f"--grep={CHECKPOINT_PREFIX}", "-1"],
                path,
            )
            target = h if (ok and h) else None

        if not target:
            return {"success": False,
                    "message": f"No checkpoints found for {short_id}."}

        ok, _ = await _git(["reset", "--hard", target], path)
        if not ok:
            return {"success": False,
                    "message": f"Rollback failed for {short_id}"}

        ok, head = await _git(["rev-parse", "--short", "HEAD"], path)
        ok2, msg = await _git(["log", "-1", "--format=%s"], path)
        log.info("git_worktree.rollback", short_id=short_id,
                 hash=head if ok else "", target=target)
        return {"success": True,
                "hash": head if ok else "",
                "message": msg if ok2 else ""}

    # ── 5. DELETE (remove) ──────────────────────────────────

    async def delete(self, workspace_path: str, short_id: str,
                     task_name: str | None = None) -> dict:
        """Discard agent's worktree (rejected/obsolete work).

        Always returns ``{success: True, removed: True}`` (best-effort).
        """
        path = _worktree_path(workspace_path, short_id)
        fwd_path = path.replace("\\", "/")

        ok, _ = await _git(
            ["worktree", "remove", fwd_path, "--force"], workspace_path
        )
        if not ok:
            # Worktree may not be registered — delete directory manually
            shutil.rmtree(path, ignore_errors=True)

        if task_name:
            branch = _branch_name(short_id, task_name)
            await _git(["branch", "-D", branch], workspace_path)

        log.info("git_worktree.delete", short_id=short_id)
        return {"success": True, "removed": True}

    # ── 6. LIST ─────────────────────────────────────────────

    async def list(self, workspace_path: str) -> dict:
        """List all HiveWeave-managed worktrees.

        Returns ``{success, entries: [...]}``. Filters to only those under
        ``.hiveweave/worktrees/``.
        """
        ok, raw = await _git(["worktree", "list"], workspace_path)
        if not ok:
            return {"success": True, "entries": []}

        entries: list[dict] = []
        for line in raw.split("\n"):
            line = line.strip()
            if not line:
                continue
            m = _WT_LIST_RE.match(line)
            if not m:
                continue
            wt_path = m.group(1).strip()
            wt_fwd = wt_path.replace("\\", "/")
            if WORKTREE_DIR in wt_fwd:
                entries.append({
                    "short_id": Path(wt_path).name,
                    "path": wt_path,
                    "branch": (m.group(3) or "").strip(),
                    "head": m.group(2)[:7],
                    "active": Path(wt_path).exists(),
                })
        return {"success": True, "entries": entries}

    # ── 7. INFO (status) ────────────────────────────────────

    async def info(self, workspace_path: str, short_id: str) -> dict:
        """Detailed status of one agent's worktree.

        Returns ``{success, status: {...} | None}``.
        """
        path = _worktree_path(workspace_path, short_id)
        if not Path(path).is_dir():
            return {"success": True, "status": None}

        ok, head = await _git(["rev-parse", "--short", "HEAD"], path)
        if not ok:
            return {"success": True, "status": None}

        ok2, branch = await _git(["rev-parse", "--abbrev-ref", "HEAD"], path)
        ok3, st = await _git(["status", "--porcelain"], path)
        has_uncommitted = bool(st) if ok3 else True

        checkpoints = await self._checkpoint_list(path)

        return {"success": True, "status": {
            "short_id": short_id,
            "branch": branch if ok2 else "",
            "active": True,
            "has_uncommitted": has_uncommitted,
            "head": head,
            "checkpoints": checkpoints,
        }}

    async def _checkpoint_list(self, path: str) -> list[dict]:
        """Get recent checkpoints (limit 20) with hash/date/message."""
        ok, raw = await _git(
            ["log", "--format=%h|%ad|%s", "--date=short",
             f"--grep={CHECKPOINT_PREFIX}", "-20"],
            path,
        )
        if not ok or not raw:
            return []

        entries: list[dict] = []
        for line in raw.split("\n"):
            parts = line.split("|", 2)
            if len(parts) != 3:
                continue
            h, date, msg = parts
            # Strip the "checkpoint: " prefix from the displayed message
            if msg.startswith(f"{CHECKPOINT_PREFIX} "):
                msg = msg[len(CHECKPOINT_PREFIX) + 1:]
            entries.append({"hash": h, "date": date, "message": msg})
        return entries
