"""Git worktree service — isolated worktrees per agent (contract 09).

契约 09: Git Worktree
- 每个叶子 agent 分配隔离 worktree: <workspace>/.hiveweave/worktrees/<shortId>/
- 分支命名 (P0 稳定化): 有 task_id → hw/<shortId>/t-<task_id前8位小写>,
  无 task_id → hw/<shortId>/work; 与任务描述文本无关, 重算不增生。
  legacy slug 分支 (hw/<sid>/<task-slug>) 仅兼容保留 (merge/对账可处理)
- Coordinator 全权管理生命周期 (coordinator-only)
- 7 个操作: create / list / checkpoint / merge / rollback / delete / info
- merge 用 --no-edit (非 ff-only), 成功后自动删除 worktree+分支 (branch -d)
- delete 安全链 (P0): worktree remove → --force → rmtree 兜底;
  分支默认 git branch -d (git 自拒未合并), 仅 discard=True 才 CAS 强删
- reconcile_worktrees (P0): 注册表/磁盘/任务表三方核对的孤儿回收
- rollback 前先 checkpoint 存档 (安全加固, 源码未做)
- git 命令 30s 超时
- slugify 保留 CJK (\\u4e00-\\u9fff), 空串→"task"
- base_branch 三级回退: origin/<base> → <base> → master
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path
from typing import List

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

# git merge/checkout: untracked files would be overwritten
_UNTRACKED_OVERWRITE_RE = re.compile(
    r"(?:untracked working tree files would be overwritten|"
    r"The following untracked working tree files would be overwritten)"
    r"[\s\S]*?(?:Please move or remove them|Aborting)",
    re.IGNORECASE,
)
_UNTRACKED_FILE_LINE_RE = re.compile(r"^\t(.+)$", re.MULTILINE)


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
    """LEGACY slug 命名 (P0 之前) — 仅为兼容存量分支保留。

    新代码一律用 compute_branch_name(); 本函数只在解析/清理
    老 slug 分支 (hw/<sid>/<task-slug>) 时作兜底。
    """
    return f"hw/{short_id}/{_slugify(task_name)}"


def compute_branch_name(short_id: str, task_id: str | None = None) -> str:
    """稳定分支命名 (P0) — 从 task_id 派生, 与任务描述文本无关。

    - 有 task_id → ``hw/<shortId>/t-<task_id 前 8 位小写>``
      (同一任务重算必同名, 根治 description[:40] 每次重算导致的分支增生)
    - 无 task_id → ``hw/<shortId>/work`` (每个 agent 一条稳定工作分支)
    """
    tid = (task_id or "").strip().lower()
    if tid:
        return f"hw/{short_id}/t-{tid[:8]}"
    return f"hw/{short_id}/work"


def _worktree_path(workspace_path: str, short_id: str) -> str:
    return str(Path(workspace_path) / WORKTREE_DIR / short_id)


def _has_git(path: str) -> bool:
    return (Path(path) / ".git").exists()


# ── 冲突标记扫描 (merge 成功后 main 树残留检测) ─────────────
# 行首锚定 <<<<<<< / >>>>>>> (标准 git conflict marker, 7 字符)。
# 故意不含 ^={7} — 一行等号同时是 setext 标题下划线, 误报率高。
_CONFLICT_MARKER_RE = re.compile(r"^(?:<{7}|>{7})", re.MULTILINE)

# 扫描时跳过的目录: 系统目录 / 依赖 / 构建产物 (口径与 ensure_git_repo
# 生成的 .gitignore 一致, 另含 worktree 宿主目录 .hiveweave)
_MARKER_SCAN_SKIP_DIRS = frozenset({
    ".git", ".hiveweave", "node_modules", "dist", "build",
    ".next", ".nuxt", ".turbo", ".venv", "venv", "__pycache__",
    ".cache", "coverage", ".idea", ".vscode",
})

_MARKER_SCAN_MAX_BYTES = 1_000_000  # 大文件跳过 (大概率是产物/压缩包)
_MARKER_SCAN_MAX_HITS = 50          # 报告上限, 防止异常输出刷屏


def scan_conflict_markers(root: str) -> list[str]:
    """Scan *root* for unresolved git conflict markers (merge 后残留检测).

    行首锚定 ``<<<<<<<`` / ``>>>>>>>``。只扫文本文件 — 跳过
    .git/.hiveweave/node_modules/dist/build 等目录、含 NUL 字节的二进制
    文件、以及 >1MB 的大文件。返回 POSIX 风格相对路径的排序列表。
    """
    root_path = Path(root)
    if not root_path.is_dir():
        return []
    hits: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root_path):
        dirnames[:] = [d for d in dirnames if d not in _MARKER_SCAN_SKIP_DIRS]
        for name in filenames:
            if len(hits) >= _MARKER_SCAN_MAX_HITS:
                return sorted(hits)
            fpath = Path(dirpath) / name
            try:
                if fpath.stat().st_size > _MARKER_SCAN_MAX_BYTES:
                    continue
                raw = fpath.read_bytes()
            except OSError:
                continue
            if b"\x00" in raw[:8192]:
                continue  # 二进制文件不扫
            if _CONFLICT_MARKER_RE.search(raw.decode("utf-8", errors="replace")):
                hits.append(fpath.relative_to(root_path).as_posix())
    return sorted(hits)


async def _git(args: list[str], cwd: str, timeout: float = GIT_TIMEOUT) -> tuple[bool, str]:
    """Run a git command, return (success, output).

    stderr merged into stdout (mirrors Elixir stderr_to_stdout: true).
    """
    try:
        from hiveweave.util.win_subprocess import windows_no_window_kwargs

        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **windows_no_window_kwargs(),
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
        "conflicts": [],
        "branch": branch,
        "files": branch_files,
        "short_id": short_id,
    }


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

    async def create(self, workspace_path: str, short_id: str,
                     task_name: str | None = None,
                     base_branch: str = "main", *,
                     task_id: str | None = None) -> dict:
        """Allocate an isolated worktree + branch for a subordinate agent.

        task_name: DEPRECATED — 保留兼容旧调用方, 不再参与分支命名
        (P0 命名稳定化, 见 compute_branch_name)。

        Returns ``{success, path, branch}`` or ``{success: False, message}``.
        """
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
            return {"success": True, "path": path,
                    "branch": actual or branch,
                    "message": "worktree already exists"}

        # Stale cleanup. Two failure modes we must handle before add:
        # 1) Path exists but is not a valid worktree (partial delete) →
        #    `worktree add` fails with "'<path>' already exists".
        # 2) Path is gone but git still has a registered worktree entry →
        #    add fails with "is a missing but registered worktree" until prune.
        # Always prune when the target is not a valid worktree.
        if Path(path).exists():
            shutil.rmtree(path, ignore_errors=True)
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
                return {"success": True, "path": path, "branch": branch}
            last_error = out
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

    async def _resolve_agent_branch(self, workspace_path: str, short_id: str,
                                    task_name: str | None,
                                    task_id: str | None) -> str:
        """解析 agent 分支名 — 事实优先, 入参兜底。

        1. worktree 还在 → 实际检出分支 (legacy slug / t- 新名通吃,
           根治 task_name 重算与首次命名脱钩导致的 merge 解析问题)
        2. 有 task_id → 稳定命名 t-<id8>
        3. 只有 task_name → legacy slug 命名 (向后兼容旧调用方)
        """
        path = _worktree_path(workspace_path, short_id)
        if _has_git(path):
            actual = await _current_branch(path)
            if actual:
                return actual
        if task_id:
            return compute_branch_name(short_id, task_id)
        return _branch_name(short_id, task_name or "task")

    async def merge(self, workspace_path: str, short_id: str,
                    task_name: str | None = None,
                    target_branch: str = "main", *,
                    task_id: str | None = None) -> dict:
        """Merge agent branch into target (git merge --no-edit), then cleanup.

        契约 09 RECONCILE: 用 --no-edit (非 ff-only), 成功后自动 remove worktree+分支.
        冲突时 abort, worktree 保留 — executor 在自己的 worktree 里合 main 解冲突.

        task_name: DEPRECATED — 不再用于命名; 分支以 worktree 实际检出
        为准 (legacy slug 分支通吃), 仅作无 worktree 时的 legacy 兜底。

        Returns ``{success, merged, hash, branch, files?}`` or
        ``{success: False, message, conflicts?, branch, files?}``.
        """
        branch = await self._resolve_agent_branch(
            workspace_path, short_id, task_name, task_id
        )

        ok, _ = await _git(["checkout", target_branch], workspace_path)
        if not ok:
            return {"success": False,
                    "message": f"Failed to checkout {target_branch}",
                    "branch": branch}

        # Capture files covered by this branch before merge mutates history
        ok_f, files_out = await _git(
            ["diff", "--name-only", f"{target_branch}...{branch}"],
            workspace_path,
        )
        branch_files = [
            f.strip() for f in (files_out or "").split("\n") if f.strip()
        ] if ok_f else []

        ok, merge_out = await _git(["merge", branch, "--no-edit"], workspace_path)
        if not ok:
            fail = await _merge_failure_result(
                workspace_path=workspace_path,
                branch=branch,
                target_branch=target_branch,
                merge_out=merge_out or "",
                branch_files=branch_files,
                short_id=short_id,
                auto_quarantine=True,
            )
            if fail is None:
                # Quarantined untracked — retry once
                ok, merge_out = await _git(
                    ["merge", branch, "--no-edit"], workspace_path
                )
                if not ok:
                    fail = await _merge_failure_result(
                        workspace_path=workspace_path,
                        branch=branch,
                        target_branch=target_branch,
                        merge_out=merge_out or "",
                        branch_files=branch_files,
                        short_id=short_id,
                        auto_quarantine=False,
                    )
                    assert fail is not None
                    return fail
            else:
                return fail

        ok, head = await _git(["rev-parse", "--short", "HEAD"], workspace_path)

        # Auto-remove worktree + branch on success (契约 09 RECONCILE)
        # 连带删分支走 delete 的 branch -d 安全链 — 已合并必然成功
        await self.delete(workspace_path, short_id, branch=branch)

        # merge 成功 ≠ main 干净 — 残留冲突标记会随提交一并落地。
        # 扫描目标树并随结果返回, 由调用方 (git_worktree_merge tool) 路由清理。
        marker_files = scan_conflict_markers(workspace_path)
        if marker_files:
            log.warning("git_worktree.merge_markers_found",
                        short_id=short_id, target=target_branch,
                        files=marker_files[:10])

        already = "already up to date" in (merge_out or "").lower()
        log.info("git_worktree.merge", short_id=short_id,
                 target=target_branch, hash=head if ok else "",
                 already_up_to_date=already)
        result = {
            "success": True,
            "merged": True,
            "hash": head if ok else "",
            "branch": branch,
            "files": branch_files,
            "short_id": short_id,
        }
        if already:
            result["already_up_to_date"] = True
            result["message"] = (
                f"Branch {branch} already on {target_branch} "
                f"(no new commits) — treated as merged."
            )
        if marker_files:
            result["conflict_markers"] = marker_files
        return result

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

        # Capture files covered by this branch before merge
        ok_f, files_out = await _git(
            ["diff", "--name-only", f"{target_branch}...{branch}"],
            workspace_path,
        )
        branch_files = [
            f.strip() for f in (files_out or "").split("\n") if f.strip()
        ] if ok_f else []

        # Step 2: Merge with target_branch
        ok, merge_out = await _git(["merge", branch, "--no-edit"], workspace_path)

        if not ok:
            # Step 3a: Untracked on MAIN — quarantine + retry (NOT executor rework)
            untracked = parse_untracked_overwrite(merge_out or "")
            if untracked:
                await _git(["merge", "--abort"], workspace_path)
                moved = await quarantine_untracked_on_target(
                    workspace_path, untracked
                )
                if moved:
                    ok, merge_out = await _git(
                        ["merge", branch, "--no-edit"], workspace_path
                    )
                if not ok:
                    still = parse_untracked_overwrite(merge_out or "")
                    if still or not moved:
                        # Still untracked (or quarantine moved nothing) —
                        # do NOT fall through as content-conflict / fake rework.
                        await _git(["merge", "--abort"], workspace_path)
                        from hiveweave.services.worktree_review import (
                            format_untracked_on_target_message,
                        )

                        return {
                            "success": False,
                            "reason": "untracked_on_target",
                            "message": format_untracked_on_target_message(
                                branch=branch,
                                target=target_branch,
                                untracked=still or untracked,
                            ),
                            "untracked": still or untracked,
                            "conflicts": [],
                            "branch": branch,
                            "files": branch_files,
                            "short_id": short_id,
                        }
                    # Quarantine worked but retry failed for another reason
                    # (e.g. real content conflict) — fall through to 3b.

            if not ok:
                # Step 3b: Content conflict — try semantic merge for package.json
                ok_diff, diff_out = await _git(
                    ["diff", "--name-only", "--diff-filter=U"], workspace_path
                )
                conflict_files = [
                    f.strip()
                    for f in (diff_out or "").split("\n")
                    if f.strip()
                ]

                resolved = []
                pkg_path = _Path(workspace_path) / "package.json"
                if "package.json" in conflict_files and pkg_path.exists():
                    try:
                        ok_ours, ours_raw = await _git(
                            ["show", f"{target_branch}:package.json"],
                            workspace_path,
                        )
                        ok_theirs, theirs_raw = await _git(
                            ["show", f"{branch}:package.json"], workspace_path
                        )
                        if ok_ours and ok_theirs:
                            ours = _json.loads(ours_raw)
                            theirs = _json.loads(theirs_raw)
                            for dep_key in (
                                "dependencies",
                                "devDependencies",
                                "peerDependencies",
                                "scripts",
                            ):
                                if dep_key in ours or dep_key in theirs:
                                    merged_deps = ours.get(dep_key, {})
                                    merged_deps.update(theirs.get(dep_key, {}))
                                    ours[dep_key] = merged_deps
                            pkg_path.write_text(
                                _json.dumps(
                                    ours, indent=2, ensure_ascii=False
                                )
                                + "\n",
                                encoding="utf-8",
                            )
                            await _git(["add", "package.json"], workspace_path)
                            resolved.append("package.json")
                    except Exception as e:
                        log.warning(
                            "git_worktree.package_merge_failed",
                            branch=branch,
                            error=str(e),
                        )

                if resolved:
                    ok_commit, _ = await _git(
                        ["commit", "--no-edit"], workspace_path
                    )
                    if ok_commit:
                        ok = True
                        log.info(
                            "git_worktree.semantic_merge_resolved",
                            branch=branch,
                            resolved_files=resolved,
                        )

                if not ok:
                    fail2 = await _merge_failure_result(
                        workspace_path=workspace_path,
                        branch=branch,
                        target_branch=target_branch,
                        merge_out=merge_out or "",
                        branch_files=branch_files,
                        short_id=short_id,
                        auto_quarantine=False,
                    )
                    assert fail2 is not None
                    if conflict_files and fail2.get("reason") != "untracked_on_target":
                        from hiveweave.services.worktree_review import (
                            format_merge_conflict_message,
                        )

                        fail2 = {
                            **fail2,
                            "reason": "merge_conflict",
                            "conflicts": conflict_files,
                            "message": format_merge_conflict_message(
                                branch=branch,
                                target=target_branch,
                                conflicts=conflict_files,
                            ),
                        }
                    return fail2

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

        ok_head, head = await _git(
            ["rev-parse", "--short", "HEAD"], workspace_path
        )

        # Step 5: Auto-remove worktree + branch on success
        # 显式传已合并的分支全名 — delete 走 branch -d 安全链, 必然成功
        if short_id:
            try:
                await self.delete(workspace_path, short_id, branch=branch)
            except Exception:
                pass  # worktree 可能已不存在

        # merge 成功 ≠ main 干净 — 扫描残留冲突标记, 交给调用方路由清理
        marker_files = scan_conflict_markers(workspace_path)
        if marker_files:
            log.warning("git_worktree.merge_markers_found",
                        branch=branch, target=target_branch,
                        files=marker_files[:10])

        already = "already up to date" in (merge_out or "").lower()
        log.info("git_worktree.merge_by_branch", branch=branch,
                 target=target_branch, hash=head if ok_head else "",
                 warnings=verification_errors, already_up_to_date=already)
        result = {
            "success": True,
            "merged": True,
            "hash": head if ok_head else "",
            "branch": branch,
            "files": branch_files,
            "short_id": short_id,
        }
        if already:
            result["already_up_to_date"] = True
            result["message"] = (
                f"Branch {branch} already on {target_branch} "
                f"(no new commits) — treated as merged."
            )
        if verification_errors:
            result["warnings"] = verification_errors
        if marker_files:
            result["conflict_markers"] = marker_files
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
                     task_name: str | None = None, *,
                     task_id: str | None = None,
                     branch: str | None = None,
                     discard: bool = False) -> dict:
        """Discard agent's worktree (rejected/obsolete work) — 删除安全链 (P0).

        Orca 式生命周期语义:
        ① ``git worktree remove`` — 先不带 --force, 失败再 --force,
           仍失败 rmtree 兜底 + prune;
        ② 分支默认 ``git branch -d`` — git 自己拒删未合并分支; 拒删时
           **不强删**, preserved_branch={branch, head, reason} 透出;
        ③ 仅显式 ``discard=True`` (确认丢弃被拒工作的场景) 才 CAS 强删:
           ``update-ref -d refs/heads/<b> <expectedHead>`` — 分支在
           rev-parse 之后移动则 CAS 失败, 放弃并透出。

        task_name: DEPRECATED — 仅为旧调用方保留, 只作 legacy slug 分支
        的兜底解析, 不参与新命名。

        Always returns ``{success: True, removed: True, branch,
        preserved_branch}`` (best-effort).
        """
        path = _worktree_path(workspace_path, short_id)
        fwd_path = path.replace("\\", "/")

        # 分支解析必须在 worktree 删除之前 — 检出分支信息随目录一起消失
        target = branch
        if not target and _has_git(path):
            target = await _current_branch(path)
        if not target and task_id:
            target = compute_branch_name(short_id, task_id)
        if not target and task_name:
            target = _branch_name(short_id, task_name)  # legacy slug 兼容
        if not target:
            target = compute_branch_name(short_id)  # 稳定 /work 名兜底

        # ① worktree 移除链: remove → remove --force → rmtree + prune
        ok, _ = await _git(["worktree", "remove", fwd_path], workspace_path)
        if not ok:
            ok, _ = await _git(
                ["worktree", "remove", fwd_path, "--force"], workspace_path
            )
        if not ok:
            # Worktree may not be registered — delete directory manually
            shutil.rmtree(path, ignore_errors=True)
            await _git(["worktree", "prune"], workspace_path)

        # ②/③ 分支处置 (分支不存在时 _dispose_branch 直接返回 None)
        preserved = await self._dispose_branch(workspace_path, target, discard)

        log.info("git_worktree.delete", short_id=short_id, branch=target,
                 preserved=preserved is not None, discard=discard)
        return {
            "success": True,
            "removed": True,
            "branch": target,
            "preserved_branch": preserved,
        }

    async def _dispose_branch(self, workspace_path: str, branch: str,
                              discard: bool) -> dict | None:
        """分支处置: 默认 -d 安全删, discard=True 走 CAS 强删。

        返回 None = 分支已删除/不存在; 否则 preserved_branch dict 透出。
        """
        if not await self._branch_exists(workspace_path, branch):
            return None
        if discard:
            return await self._discard_branch(workspace_path, branch)

        ok, out = await _git(["branch", "-d", branch], workspace_path)
        if ok:
            return None
        # git 拒删 (未完全合并/被占用) — 不强删, 透出给调用方决策
        ok_h, head = await _git(["rev-parse", "--short", branch], workspace_path)
        preserved: dict = {
            "branch": branch,
            "head": head.strip() if ok_h else "",
            "reason": "unmerged",
        }
        detail = (out or "").splitlines()
        if detail:
            preserved["detail"] = detail[0]
        log.warning("git_worktree.branch_preserved", branch=branch,
                    reason=detail[0] if detail else "branch -d refused")
        return preserved

    async def _discard_branch(self, workspace_path: str,
                              branch: str) -> dict | None:
        """CAS 强删: ``update-ref -d refs/heads/<b> <expectedHead>``。

        先 rev-parse 拿 expected head; CAS 失败说明分支已移动 —
        绝不盲删, 放弃并透出。
        """
        ok_h, head = await _git(["rev-parse", branch], workspace_path)
        if not ok_h or not head.strip():
            return None  # 分支已不存在
        expected = head.strip()
        ok, out = await _git(
            ["update-ref", "-d", f"refs/heads/{branch}", expected],
            workspace_path,
        )
        if ok:
            log.info("git_worktree.branch_discarded", branch=branch,
                     head=expected[:7])
            return None
        log.warning("git_worktree.discard_cas_failed", branch=branch,
                    expected=expected[:7], error=(out or "")[:200])
        return {
            "branch": branch,
            "head": expected[:7],
            "reason": "cas_failed",
            "detail": (out.splitlines()[0] if out else "ref moved"),
        }

    async def _branch_exists(self, workspace_path: str, branch: str) -> bool:
        # --format 不带 * / + 前缀 (检出标记), 精确匹配整行即可
        ok, out = await _git(
            ["branch", "--list", branch, "--format=%(refname:short)"],
            workspace_path,
        )
        return bool(
            ok and any(ln.strip() == branch
                       for ln in out.splitlines() if ln.strip())
        )

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

    async def _checkpoint_list(self, path: str) -> List[dict]:
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


# ── 孤儿回收对账 (P0) ──────────────────────────────────────

# 新稳定命名 hw/<sid>/t-<taskid8> 的解析正则; 非 t- 后缀即 legacy slug 分支
_TASK_BRANCH_RE = re.compile(r"^hw/[^/]+/t-(.{8})$")


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


async def _resolve_base_branch(workspace_path: str) -> str | None:
    """merged 判定的基准分支: main → master 二级回退。"""
    for name in ("main", "master"):
        ok, _ = await _git(
            ["rev-parse", "--verify", f"refs/heads/{name}"], workspace_path
        )
        if ok:
            return name
    return None


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
    preserved_branches, errors}``.
    """
    report: dict = {
        "pruned": 0,
        "removed_dirs": 0,
        "deleted_branches": [],
        "preserved_branches": [],
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

    # ② 磁盘 → 注册表: 未注册的孤儿目录 → rmtree
    if wt_root.is_dir():
        for child in sorted(wt_root.iterdir()):
            if not child.is_dir():
                continue
            if os.path.normcase(str(child)) in registered:
                continue
            shutil.rmtree(child, ignore_errors=True)
            if child.exists():
                report["errors"].append(
                    f"failed to remove orphan dir: {child}")
            else:
                report["removed_dirs"] += 1
                log.info("git_worktree.reconcile_dir_removed",
                         workspace=workspace_path, dir=str(child))

    # ③ 分支对账: t-<taskid8> 查任务表, legacy slug 直接候选;
    #    merged → -d 删除, 未合并 → preserved 报告
    # (--format 输出不带 * / + 检出前缀, 可精确匹配)
    checked_out = {e["branch"] for e in entries if e.get("branch")}
    base = await _resolve_base_branch(workspace_path)
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
        m = _TASK_BRANCH_RE.match(b)
        reason = "legacy_unmerged"
        if m:
            if conn is None:
                # 无项目 DB → 按"任务不存在"处理 (契约: 不存在 → 候选)
                candidate, reason = True, "task_missing_unmerged"
            else:
                candidate, reason = await _task_branch_candidate(
                    conn, m.group(1))
            if not candidate:
                continue  # 任务仍活跃 — 不动
        if not base:
            continue  # 无法判定 merged — 不动 (errors 已记)
        if b in merged_set:
            ok_d, out_d = await _git(["branch", "-d", b], workspace_path)
            if ok_d:
                report["deleted_branches"].append(b)
                log.info("git_worktree.reconcile_branch_deleted",
                         workspace=workspace_path, branch=b)
            else:
                report["errors"].append(f"branch -d {b} failed: {out_d}")
        else:
            ok_h, head = await _git(
                ["rev-parse", "--short", b], workspace_path)
            report["preserved_branches"].append({
                "branch": b,
                "head": head.strip() if ok_h else "",
                "reason": reason,
            })
            log.warning("git_worktree.reconcile_branch_preserved",
                        workspace=workspace_path, branch=b, reason=reason)

    log.info("git_worktree.reconcile", workspace=workspace_path,
             pruned=report["pruned"], removed_dirs=report["removed_dirs"],
             deleted=len(report["deleted_branches"]),
             preserved=len(report["preserved_branches"]),
             errors=len(report["errors"]))
    return report


async def ensure_executor_worktree(
    project_id: str,
    agent_id: str,
    *,
    task_name: str | None = None,
    task_id: str | None = None,
) -> dict:
    """Ensure an executor has a live worktree and ``agents.workspace_path``.

    Refuses coordinators/HR — they must not own write worktrees.
    Idempotent if a valid worktree is already bound.

    task_name: DEPRECATED — 保留兼容旧调用方, 不再参与分支命名;
    task_id 驱动 P0 稳定命名 (hw/<sid>/t-<id8>)。

    Returns ``{success, path, short_id, branch?}`` or ``{success: False, message}``.
    """
    from hiveweave.db import meta as meta_db
    from hiveweave.services.org import OrgService

    org = OrgService()
    agent = await org.resolve_agent(agent_id)
    if not agent:
        return {"success": False, "message": f"Agent not found: {agent_id}"}

    perm = (agent.get("permission_type") or "").lower()
    if perm != "executor":
        return {
            "success": False,
            "message": (
                f"Refusing worktree for {agent.get('short_id')} "
                f"(permission_type={perm or 'unknown'}). "
                "Only executors get write worktrees — coordinators review/merge only."
            ),
        }

    short_id = (agent.get("short_id") or "").strip()
    if not short_id:
        return {"success": False, "message": "Agent has no short_id"}

    ws = await meta_db.get_project_workspace(project_id)
    if not ws or not (Path(ws) / ".git").exists():
        return {"success": False, "message": "Project has no git workspace"}

    cur = (agent.get("workspace_path") or "").strip()
    if cur and Path(cur).is_dir() and (Path(cur) / ".git").exists():
        # Accept only if path is under this agent's short_id worktree dir
        norm = cur.replace("\\", "/")
        needle = f"/worktrees/{short_id}"
        if needle in norm or norm.rstrip("/").endswith(f"/worktrees/{short_id}"):
            # 幂等: 透出实际检出的分支, 不按入参重算 (P0 幂等脱钩修复)
            actual = await _current_branch(cur)
            return {
                "success": True,
                "path": cur,
                "short_id": short_id,
                "branch": actual,
                "message": "worktree already bound",
            }
        # Path exists but not this agent's tree — recreate under correct short_id
        log.warning(
            "worktree_path_mismatch",
            agent_id=agent_id,
            short_id=short_id,
            workspace_path=cur,
        )

    gwt = GitWorktreeService()
    name = task_name or agent.get("role") or "task"
    result = await gwt.create(ws, short_id, str(name), task_id=task_id)
    if not result.get("success") or not result.get("path"):
        err = result.get("message") or "worktree create failed"
        try:
            await org.update_agent(agent_id, {"worktree_error": err})
        except Exception:
            pass
        return {"success": False, "message": err, "short_id": short_id}

    path = result["path"]
    try:
        await org.update_agent(
            agent_id,
            {"workspace_path": path, "worktree_error": None},
        )
    except Exception as e:
        log.warning("worktree_bind_failed", agent_id=agent_id, error=str(e))

    log.info(
        "executor_worktree_ensured",
        agent_id=agent_id,
        short_id=short_id,
        path=path,
    )
    return {
        "success": True,
        "path": path,
        "short_id": short_id,
        "branch": result.get("branch"),
    }


def pin_dispatch_message_to_worktree(
    description: str,
    *,
    short_id: str,
    worktree_path: str,
) -> str:
    """Rewrite wrong worktree paths and append a mandatory WORKTREE PIN footer."""
    import re

    text = description or ""
    sid = (short_id or "").strip()
    if not sid:
        return text

    def _repl(m: re.Match[str]) -> str:
        other = m.group(1)
        if other.upper() == sid.upper():
            return m.group(0)
        return f".hiveweave/worktrees/{sid}"

    text = re.sub(
        r"\.hiveweave[/\\]+worktrees[/\\]+(A\d+)",
        _repl,
        text,
        flags=re.IGNORECASE,
    )
    # Avoid pointing at bare project-root file edits without worktree context
    footer = (
        f"\n\n[WORKTREE PIN] You MUST edit only under your worktree ({sid}):\n"
        f"  {worktree_path}\n"
        f"Do NOT edit project root/main or other agents' worktrees "
        f"(e.g. A001/CEO). After submit, coordinator merges with "
        f"git_worktree_merge(branchName='{sid}')."
    )
    if "[WORKTREE PIN]" not in text:
        text = text.rstrip() + footer
    return text


async def heal_project_executor_worktrees(project_id: str) -> dict:
    """Ensure every active executor has a valid worktree before agents start.

    Prunes stale metadata, recreates missing worktrees, updates agents.workspace_path.
    """
    from hiveweave.db import meta as meta_db
    from hiveweave.db import project as project_db

    ws = await meta_db.get_project_workspace(project_id)
    if not ws or not (Path(ws) / ".git").exists():
        return {"recovered": 0, "failed": 0, "skipped": True}

    await _git(["worktree", "prune"], ws)
    conn = await project_db.get_project_db_by_project_id(project_id)
    if conn is None:
        return {"recovered": 0, "failed": 0, "skipped": True}

    cur = await conn.execute(
        "SELECT id, name, role, short_id, workspace_path, permission_type "
        "FROM agents WHERE project_id=? AND status='active' "
        "AND permission_type='executor'",
        [project_id],
    )
    agents = await cur.fetchall()
    await cur.close()

    recovered = 0
    failed = 0
    for a in agents:
        result = await ensure_executor_worktree(
            project_id,
            a["id"],
            task_name=a["role"] or "developer",
        )
        if result.get("success"):
            if result.get("message") != "worktree already bound":
                recovered += 1
        else:
            failed += 1
    return {"recovered": recovered, "failed": failed, "skipped": False}
