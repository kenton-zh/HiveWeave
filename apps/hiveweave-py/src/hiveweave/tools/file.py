"""File tool — read_file / write_file / list_files with sandbox.

契约 02: 工具执行器 — file 子模块
- read_file: 行号格式输出，支持 offset+limit，二进制检测拒绝
- write_file: 自动创建父目录，覆盖写入
- list_files: 列出目录条目，标 [DIR]/[FILE] + 大小
- 路径沙箱（读写分离）:
  - 写：必须落在 agent 自己的 workspace（executor = worktree）
  - 读：相对路径仍相对 workspace 解析，但允许落到整个项目根内
    （可读 main / 同伴 worktree / shared，不可逃出项目）
- 敏感文件保护：拒绝 .env / *.pem / id_rsa / credentials 等（写入和读取）
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
from pathlib import Path
from typing import Any

import structlog

from hiveweave.util import path_guard
from hiveweave.util.tree_label import (
    READ_MISS_HINT,
    listing_header,
    tree_tag,
    write_tree_suffix,
)

log = structlog.get_logger(__name__)

# ── 文件版本戳（45 轮 P1「拒绝无记忆」③；2026-09-12 换三段短路）─────
# 记录本进程内最近一次成功访问后的版本戳；patch 的 update op 写盘前比对——
# 文件在最近访问后被外部（git 同步/其他 agent/平台工具）改过 → 陈旧视图
# 早拒逼重读。进程内即可：陈旧窗口是 turn 级的。
# 键是进程级路径（非 per-agent）：A 读 → B 写 → A edit 的场景 B 的写已
# 刷新戳，A 仍会漏判——已知的精度换简（audit P3-1），edit 本身对新读内容
# 锚定不会写坏。
#
# **2026-09-12：`(mtime_ns, size)` 两元组有约 66-75% 的漏检率**（不是 flake）。
# 实测「连续无间改写 100 次」的漏检数：56 / 66 / 72 / 74 / 75（多次独立复测）。
# **单点 56% 是最低的那个样本，别当常数引用** —— 引用时给区间。
# 机理：本机文件系统 mtime 刻度约 1ms，两次连续写几乎必然落在同一刻度、
# 同长度 ⇒ 元组**逐字节相同** ⇒ 陈旧检测形同虚设
# （`sha256` 在同批数据上漏检 **0/200**，对照成立）。
# 量级结论：**约三分之二的同刻度改写会漏** —— 确定性失效，不是偶发。
#
# 修法 = 三段**短路**（`_version_token`）：先比 (mtime_ns, size)，**只在前
# 两段相同时才算内容摘要**。这样「明显变了」的常见情形仍是 O(1) stat，
# 只在「看起来没变」时才付 hash 代价 —— 而那恰好就是原来会漏检的分支。
# ⚠ 注意这只对 check 侧成立：**record 侧是无条件 hash**（见该函数 docstring，
# 登记时必须立好基准，否则首次 check 无尺可用）。
#
# 为什么不照 DSH `fsio.ts:74-76 versionOf` 用 `ctimeNs`：**Windows 上
# `st_ctime_ns` 是创建时间**（`st_birthtime` 别名），实测同长度/变长度改写
# 后**都不变** —— 加了等于加个常量，是「改了但无效」的假修复。DSH 的
# `versionOf` 是 POSIX 视角（其主体跑 Linux/macOS）。**借判据不借字段**
# （MEMORY.md 纪律 #13）。
_version_lock = threading.Lock()
_VERSION_CACHE_CAP = 4096
# 值 = (mtime_ns, size, digest)；digest 仅在"前两段相同"时才算，否则为 ""。
_version_cache: dict[str, tuple[int, int, str]] = {}
_HASH_CHUNK = 1 << 20  # 1MiB


def _content_digest(path: str | Path) -> str:
    """文件内容摘要；读失败返回 ""（best-effort，不因它引入新故障面）。

    ⚠ **已知边界（审计 P2，2026-09-12）**：`""` 同时表示「算不出来」与
    「这就是算出来的值」。若**两端都**读失败（登记时读不到 + 检查时读不到），
    空 == 空 ⇒ 判为"没变"（假阴性）。影响面：只在文件**同时**不可读时发生，
    而那种情况下 `patch` 后续的 read 也必然失败并给出真实错误 ⇒ 不会
    静默写坏数据。**不为它增加哨兵值**：真正的读取失败会以更明确的错误
    在下一环暴露，加一个 `_DIGEST_FAILED` 哨兵只会让"摘要不可用"这个
    本已罕见的形态多一套分支。若将来出现"可读→不可读"的翻转场景再收口。
    """
    try:
        h = hashlib.blake2b(digest_size=8)
        with open(path, "rb") as fh:
            while chunk := fh.read(_HASH_CHUNK):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def _stat_segments(path: str | Path) -> tuple[int, int]:
    st = Path(path).stat()
    return st.st_mtime_ns, st.st_size


def record_file_version(path: str | Path) -> None:
    """登记一次成功访问后的文件版本（stat 失败静默跳过）。

    **记录时必须带内容摘要** —— 登记侧是"我看到了什么"，此处若懒惰不算
    hash，后面比对就失去了判据（两端都要有同一把尺）。

    ⚠ **为什么不惰性**（2026-09-12 实测否掉了一个诱人的优化）：曾把这里
    改成只存 `(mtime, size, "")`、由 `check_file_version` 就地在同刻度分支
    补齐。代价实测确认存在（10MiB 文件每次 read_file 多花约 11.4ms），
    **但它换来一个真回归**：record 后同刻度同长度改写的**首次** check
    拿不到基准摘要 ⇒ 返回 `None`（放行）—— 正是本改动要消灭的那个漏检。
    即：惰性把漏检从"每次"降级为"同刻度改写后的第一次"，而"第一次"恰好
    就是陈旧检测唯一需要在场的时刻。**判据必须在登记时就立好**，
    否则第一道 gate 是空的。

    ⚠ **为什么也不给大文件免 hash**（审计 P1 提议，2026-09-12 实测否掉）：
    审计指出 record 侧无条件 hash 是真热点（实测 1MiB≈1.6ms、10MiB≈11.4ms、
    50MiB≈57ms，随大小线性）。试过"超过阈值不 hash、存 `!too-big` 哨兵、
    前两段相同即判已变"。**实测证明这是错的**：大文件只要没被改过，
    每次 check 都会报 `FS_NOT_OBSERVED` ⇒ **永远逼重读**（回归：
    未改动的 3MiB 文件连续两次 check 都报 stale）。把"偶尔多花 57ms"
    换成"大文件彻底不可用"，方向反了。⇒ 保留无条件 hash：**O(n) 的读
    是正确性的代价，且它总要先被 read 一遍，hash 与之同量级**。
    真正的热点若将来出现，应从"谁在反复 read 同一大文件"入手，
    而不是削弱判据。
    """
    try:
        mtime_ns, size = _stat_segments(path)
    except OSError:
        return
    key = str(Path(path).resolve())
    cur = (mtime_ns, size, _content_digest(path))
    with _version_lock:
        if len(_version_cache) >= _VERSION_CACHE_CAP and key not in _version_cache:
            # 插入序淘汰最旧一半（dict 保插入序；audit P2-2 防无界增长）
            for k in list(_version_cache)[: _VERSION_CACHE_CAP // 2]:
                del _version_cache[k]
        _version_cache[key] = cur


def check_file_version(path: str | Path) -> str | None:
    """返回「已变」的**动作指引**；无历史记录或未变化返回 None。

    #10（2026-09-11，用户拍板「只给动作，别叠加证据」）：**停止打印版本证据**。

    原实现返回 `f"size {known[1]}B → {cur[1]}B"` —— 但版本元组是
    `(st_mtime_ns, st_size)`，只印 size ⇒ **同长度内容改动时打印
    `size 1077B → 1077B`，两个相等的数字**。那不是证据，是**伪造证据**：
    它声称"文件变了"却给出一个看不出变化的量，模型既无法据此判断、又会
    得到一个自相矛盾的信号（DSH `fs-observation-policy/src/index.ts:61-88`
    的设计立场正是**版本对模型不透明**、失败只抛 typed code、**绝不打印
    `(mtime,size)`**）。

    ⇒ 现在只回**动作**（重读后再改），不伪装成"我给你看了差异"。
    差异维度（mtime / hash）对**平台诊断**仍有用，但它属于日志，
    **不属于给模型的回执** —— 两处混用正是原 bug 的来源。

    三段短路（2026-09-12）：先比 `(mtime_ns, size)`；**只有前两段相同
    才算内容摘要**。理由见上方版本戳注释 —— 两元组在同刻度同长度改写时
    漏检约三分之二（实测 56-75%），必须靠内容兜底；而"明显变了"仍走 O(1) stat。

    ⚠ 摘要必须由 `record_file_version` 在**登记时就写好**：本分支不做
    惰性补齐（曾试过，会让同刻度改写的**首次** check 无基准可用而放行 ——
    恰是漏检最需要被拦住的那一次）。详见 `record_file_version` docstring。
    """
    try:
        mtime_ns, size = _stat_segments(path)
    except OSError:
        return None
    key = str(Path(path).resolve())
    with _version_lock:
        known = _version_cache.get(key)
    if known is None:
        return None
    # 前两段（廉价、O(1)）：不同即"确实变了"，无需读内容
    if (known[0], known[1]) != (mtime_ns, size):
        return "FS_NOT_OBSERVED"
    # 前两段相同 → 可能是"真的没变"，也可能是**同刻度同长度改写**。
    # 只有这里才付内容摘要的代价（原来会漏检的正是这个分支）。
    if known[2] != _content_digest(path):
        return "FS_NOT_OBSERVED"
    return None


def clear_file_versions_for_tests() -> None:
    with _version_lock:
        _version_cache.clear()

# ── Constants ───────────────────────────────────────────────

MAX_READ_LINES = 2000
MAX_LINE_LENGTH = 2000
BINARY_PROBE_SIZE = 8192
MAX_LIST_FILES = 1000
MAX_READ_BYTES = 10 * 1024 * 1024  # 10MB — 大文件读取上限（R5），防止 OOM

# Directories to skip in list_files (common build/dep dirs + HiveWeave system dir)
IGNORED_DIRS = frozenset({
    "node_modules", ".git", ".svn", ".hg", "__pycache__",
    "dist", "build", "target", ".next", ".nuxt", ".turbo",
    ".cache", "coverage", ".idea", ".vscode",
    ".hiveweave",  # HiveWeave 系统目录 — agent 不应遍历
})

# HiveWeave system directory — agents must never touch it
HIVEWEAVE_DIR = ".hiveweave"

# Sensitive file patterns — blocked from read/write
SENSITIVE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^\.env(\..+)?$", re.I),
    re.compile(r"^id_rsa(\.pub)?$", re.I),
    re.compile(r"^id_ed25519(\.pub)?$", re.I),
    re.compile(r"^.*\.pem$", re.I),
    re.compile(r"^.*\.p12$", re.I),
    re.compile(r"^.*\.pfx$", re.I),
    re.compile(r"^credentials(\.json)?$", re.I),
    re.compile(r"^.*\.key$", re.I),
    re.compile(r"^\.htpasswd$", re.I),
    re.compile(r"^shadow$", re.I),
    re.compile(r"^.*\.keystore$", re.I),
    re.compile(r"^token(\.json)?$", re.I),
    re.compile(r"^secrets?(\.json|\.ya?ml)?$", re.I),
    re.compile(r"^\.npmrc$", re.I),
    re.compile(r"^\.pypirc$", re.I),
    re.compile(r"^netrc$", re.I),
    re.compile(r"^\.aws[\\/]credentials$", re.I),
]


# ── Path security ──────────────────────────────────────────

# Tools that may resolve paths against the project root (read-only).
READ_PATH_TOOLS: frozenset[str] = frozenset({
    "read_file",
    "list_files",
    "grep",
    "search_files",
})

# Git Bash / MSYS2 drive paths: /d/PC_AI/... → D:/PC_AI/...
# Agents copy these from bash output into read_file/write_file; Path() on
# Windows does not map them to the real drive, so sandbox checks false-deny.
_MSYS_DRIVE = re.compile(r"^/([a-zA-Z])(/|$)")
_CANONICAL_WT_READ = re.compile(
    r"^\.hiveweave/worktrees/[A-Za-z]\d{2,}(/.*)?$",
    re.IGNORECASE,
)


def normalize_input_path(p: str) -> str:
    """Normalize agent-supplied paths (MSYS2 / Git Bash → Windows-friendly).

    ``/d/PC_AI/Project/X`` → ``D:/PC_AI/Project/X``. Backslashes become
    forward slashes for consistent prefix checks. Relative paths unchanged.
    """
    if not p:
        return p
    s = p.replace("\\", "/")
    if _MSYS_DRIVE.match(s):
        return _MSYS_DRIVE.sub(
            lambda m: m.group(1).upper() + ":/", s, count=1
        )
    return s


def _is_canonical_worktree_read(file_path: str) -> bool:
    """``.hiveweave/worktrees/<sid>/…`` — review path, resolve from project root."""
    s = (file_path or "").replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    return bool(_CANONICAL_WT_READ.match(s))


def infer_project_root(workspace_path: str) -> str:
    """Derive the project root from an agent workspace.

    Executor worktrees live at ``<project>/.hiveweave/worktrees/<short_id>``.
    Coordinator/HR workspaces are already the project root.
    """
    p = Path(workspace_path).resolve()
    parts = p.parts
    for i in range(len(parts) - 1):
        if parts[i] == ".hiveweave" and parts[i + 1] == "worktrees":
            if i == 0:
                return str(p)
            return str(Path(parts[0]).joinpath(*parts[1:i]))
    return str(p)


async def fetch_additional_read_dirs(project_root: str) -> list[str]:
    """P1 (§5.5b①)：项目级配置的外部只读目录（projects.additional_read_dirs）。

    大小写不敏感匹配 workspace_path（Windows）。无配置/查询失败 → 空列表
    （fail-open 到现状行为，不阻断读）。
    """
    import json as _json

    try:
        from hiveweave.db import meta as meta_db

        rows = await meta_db.query(
            "SELECT workspace_path, additional_read_dirs FROM projects"
        )
        target = os.path.normcase(os.path.normpath(project_root))
        for r in rows:
            ws = r["workspace_path"] or ""
            if os.path.normcase(os.path.normpath(ws)) != target:
                continue
            raw = r["additional_read_dirs"] or "[]"
            try:
                parsed = _json.loads(raw)
            except (ValueError, TypeError):
                return []
            return [str(d) for d in parsed if str(d).strip()]
    except Exception:
        return []
    return []


def _double_worktree_prefix(workspace_path: str, full_path: str) -> str | None:
    """Detect a self-nested worktree prefix (ghost tree) in ``full_path``.

    **#6（2026-09-12）薄封装** —— 实现已抽到
    :func:`hiveweave.util.path_guard.double_worktree_prefix`，shell 侧
    （``tools/bash.py``）与 file 侧**共用同一判定、同一处方**。保留本名
    仅为兼容既有调用点与测试（``tests/test_file_self_nested_prefix.py``），
    避免一次性重命名扩散到无关文件。行为与抽取前**逐字一致**。

    详细判定说明见 :mod:`hiveweave.util.path_guard` 的模块 docstring。
    """
    return path_guard.double_worktree_prefix(workspace_path, full_path)


def _resolve_safe_detail(
    workspace_path: str, file_path: str
) -> tuple[str | None, str | None]:
    """Like _resolve_safe, but reports *why* resolution failed.

    hint is non-None only when the path repeats the worktree prefix (ghost
    nest) — callers surface it as a clear error instead of a generic sandbox
    violation.
    """
    if not file_path:
        return None, None
    file_path = normalize_input_path(file_path)
    try:
        ws = Path(workspace_path).resolve()
        # Disallow absolute paths that point outside workspace
        candidate = Path(file_path)
        if candidate.is_absolute():
            try:
                # Resolve absolute first so D:/… and /d/… (normalized) compare
                abs_cand = candidate.resolve()
                rel = abs_cand.relative_to(ws)
                full = ws / rel
            except ValueError:
                return None, None
        else:
            full = (ws / file_path).resolve()
        # Re-check via relative_to
        if full != ws:
            try:
                full.relative_to(ws)
            except ValueError:
                return None, None
        # 报错优先于 _check_hiveweave_dir 放行：双重 worktree 前缀 → 拒绝（M4）
        if _double_worktree_prefix(str(ws), str(full)) is not None:
            return None, (
                f"疑似重复 worktree 前缀路径：{file_path}"
                "（应为相对 worktree 或项目根的路径）"
            )
        return str(full), None
    except (OSError, ValueError):
        return None, None


def _resolve_safe(workspace_path: str, file_path: str) -> str | None:
    """Resolve file_path against workspace and ensure it stays inside (WRITE sandbox).

    Returns the absolute path string, or None if the path escapes the sandbox
    or repeats the worktree prefix (ghost-nest path, M4).
    """
    full, hint = _resolve_safe_detail(workspace_path, file_path)
    if hint is not None:
        return None
    return full


def strip_dot_slash_prefix(p: str) -> str:
    """剥前导 ``./`` 序列，**不碰**其他 '.' 段。

    用逐段切片而非 ``str.lstrip("./")`` —— 后者把参数当**字符集**，会把
    ``.hiveweave/reports/x`` 剥成 ``hiveweave/reports/x``，使一切以点开头的
    路径判定恒不命中。本仓已有三处同族修正
    （``services/policy.py:475`` / ``services/worktree_review.py:93`` /
    ``tools/tasks/submit.py:707``），此处是第四处（2026-09-12 审计实测：
    ``_is_platform_reports_read`` 因该 bug **恒返回 False**，reports 重定向
    从未生效）。
    """
    s = (p or "").replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    return s


def _is_platform_reports_read(file_path: str) -> bool:
    """`.hiveweave/reports/**` 是平台自管共享产物（契约/取证/截图）。

    **判据来源**：``services/git_worktree/service_create.py:99-105`` ——
    ``.hiveweave/{shared,reports,drafts,handoffs}`` 四目录反选入库、
    **跨 worktree 可见可合并**；``:171-175`` 给 reports 定的是「默认文本合并、
    预期多方写」。写侧因此有**单一权威落点**（MAIN 的 ``.hiveweave/reports/``）
    —— 只有落在同一棵树上，其 merge 策略才成立。

    ⇒ 本判定命中后解析改走项目根（即 MAIN）。**这不是"硬重定向"，是共享
    契约的落点**（fixplan §10.2）。

    40 轮实测背景：旧解析只拼 worktree 前缀 → 永远 File not found（4 人 3
    通道 76 次读取 0 成功，225min 契约税）。
    """
    p = strip_dot_slash_prefix(file_path)
    return p == ".hiveweave/reports" or p.startswith(".hiveweave/reports/")


def _resolve_for_read_detail(
    write_workspace: str,
    file_path: str,
    project_root: str | None = None,
    extra_read_dirs: list[str] | None = None,
) -> tuple[str | None, str | None]:
    """Like resolve_for_read, but reports *why* resolution failed.

    hint is non-None only when the path repeats the worktree prefix (ghost
    nest) — callers surface it as a clear error instead of a generic sandbox
    violation.

    P1 (§5.5b①)：可读范围 = 项目根 ∪ ``extra_read_dirs``（外部只读参考）。
    """
    root = Path(project_root or infer_project_root(write_workspace)).resolve()
    write_ws = Path(write_workspace).resolve()
    bases = [root]
    for d in extra_read_dirs or []:
        if not d:
            continue
        try:
            bases.append(Path(d).resolve())
        except OSError:
            continue
    if not file_path:
        return str(write_ws), None
    file_path = normalize_input_path(file_path)
    try:
        candidate = Path(file_path)
        if candidate.is_absolute():
            full = candidate.resolve()
        elif _is_canonical_worktree_read(file_path) or _is_platform_reports_read(file_path):
            # Mid-level review path is project-relative. From a builder
            # worktree cwd, joining it would ghost-nest and get rejected.
            # 40 轮 P0-1：.hiveweave/reports/** 同理——平台自管产物在 MAIN，
            # 从 worktree cwd 相对读取必须走项目根。
            full = (root / file_path.replace("\\", "/")).resolve()
        else:
            full = (write_ws / file_path).resolve()
        if not _inside_any(full, bases):
            raise ValueError("outside allowed read scope")
        # 报错优先于 _check_hiveweave_dir 放行：双重 worktree 前缀 → 拒绝（M4）
        if _double_worktree_prefix(str(write_ws), str(full)) is not None:
            return None, (
                f"疑似重复 worktree 前缀路径：{file_path}"
                "（应为相对 worktree 或项目根的路径）"
            )
        return str(full), None
    except (OSError, ValueError):
        return None, None


def _inside_any(candidate: Path, bases: list[Path]) -> bool:
    """``candidate`` 落在任一 ``base`` 内（含 base 自身）。"""
    for base in bases:
        if candidate == base:
            return True
        try:
            candidate.relative_to(base)
            return True
        except ValueError:
            continue
    return False


def resolve_for_read(
    write_workspace: str,
    file_path: str,
    project_root: str | None = None,
    extra_read_dirs: list[str] | None = None,
) -> str | None:
    """Resolve a path for READ access.

    Relative paths are resolved against the agent's write workspace (cwd),
    but the final path may land anywhere under the project root. Paths that
    start with ``.hiveweave/worktrees/<shortId>/`` are resolved from the
    project root so mid-level review works from a builder worktree cwd.
    Do not teach ``../`` as MAIN — from a worktree that is the sibling
    worktrees directory.

    P1 (§5.5b①)：``extra_read_dirs`` 追加为可读范围（外部只读参考）。

    Returns None if the path escapes the project or repeats the worktree
    prefix (ghost-nest path, M4).
    """
    full, hint = _resolve_for_read_detail(
        write_workspace, file_path, project_root, extra_read_dirs
    )
    if hint is not None:
        return None
    return full


def _check_hiveweave_dir(
    abs_path: str, workspace_path: str, *, write: bool = False
) -> bool:
    """Return True if the path targets protected .hiveweave internals.

    保护策略（分层）:
    - `.hiveweave` 根目录下的直接文件（data.db, env.sh, *.db-* 等）→ 保护
    - `.hiveweave/tool_outputs/` → 保护（系统管理的工具输出）
    - `.hiveweave/shared/` → 放行（团队共享空间，所有 agent 可读可写）
    - `.hiveweave/reports/`, `.hiveweave/drafts/`, `.hiveweave/worktrees/` → 放行（agent 工作文件）
    - `.hiveweave/handoffs/` → 放行（解散交接文档，供上级 read_file 读取；审计 2026-08-05 深度审计 P0）
    - `.hiveweave/merge-quarantine/` → **只读放行**（见下）
    - 其他 `.hiveweave/<subdir>/` → 保护（未知子目录默认保护）

    ``write=False``（默认）表示这是一次**读**检查；``write=True`` 表示写。

    merge-quarantine 只读放行的判据（report TEST_DSH_54 #5，v2 收窄版）：
    平台自己在 `services/platform_state.py` 的 T2.5 把 merge-quarantine 当
    **只读诊断源**接进了平台状态（统计"待处理 quarantine"并回报给 Agent），
    却因该目录未入白名单而不让 Agent 读里面到底是什么 —— 实测 18/18 次
    拒绝全部指向它，Agent 只能靠猜。反向也重要：隔离区由平台自管（
    git_worktree 把阻塞 merge 的 untracked 文件搬进去），**agent 不得改写**，
    所以是"读放行、写仍保护"，而不是把它整个并入 allowed_subdirs。
    """
    try:
        ws = Path(workspace_path).resolve()
        hw_root = ws / HIVEWEAVE_DIR
        target = Path(abs_path).resolve()
        try:
            target.relative_to(hw_root)
        except ValueError:
            return False  # Not in .hiveweave — allowed

        # 放行的 agent 工作子目录（shared = 团队共享空间；sandbox-temp = 沙箱
        # TEMP 指向的私有 scratch——平台自己教 agent 往里写，护栏不能再封死它，
        # s3-clone_07 报告 P0-1：7 Agent 撞 11 次）
        allowed_subdirs = {"shared", "reports", "drafts", "worktrees", "handoffs",
                           "sandbox-temp"}
        for sub in allowed_subdirs:
            try:
                target.relative_to(hw_root / sub)
                return False  # 在允许的工作子目录内
            except ValueError:
                pass

        # 只读放行的平台自管子目录 —— 读可以（诊断），写不行（平台自管）。
        if not write:
            readonly_subdirs = {"merge-quarantine"}
            for sub in readonly_subdirs:
                try:
                    target.relative_to(hw_root / sub)
                    return False  # 只读放行
                except ValueError:
                    pass

        # tool_outputs/ 保护
        try:
            target.relative_to(hw_root / "tool_outputs")
            return True
        except ValueError:
            pass

        # .hiveweave 根目录下的直接文件或其他未知子目录 → 保护
        return True
    except (OSError, ValueError):
        return False


def _is_sensitive(file_path: str) -> bool:
    """Return True if the file path matches any sensitive file pattern.

    Uses two complementary checks:
    1. Basename-anchored patterns (exact match on filename)
    2. Full-path substring patterns from security.py (broader, catches paths like .ssh/id_rsa)
    """
    base = Path(file_path).name
    if any(p.search(base) for p in SENSITIVE_PATTERNS):
        return True
    # Also check via security.py for path-level patterns (.ssh/, .aws/, etc.)
    from hiveweave.tools.security import is_sensitive_path
    return is_sensitive_path(file_path)


def _is_binary(abs_path: str) -> bool:
    """Probe the file for null bytes (binary detection)."""
    try:
        with open(abs_path, "rb") as fh:
            chunk = fh.read(BINARY_PROBE_SIZE)
        return b"\x00" in chunk
    except OSError:
        return False


# ── #5 读侧多树查找（判据来源：我们自己的四目录共享模型）────────────
# 出处：services/git_worktree/service_create.py:99-105（四目录反选入库、
# 跨 worktree 可见可合并）+ :171-175（reports = 默认文本合并、预期多方写）；
# services/acl_sandbox/policy.py:54（boundary_root：executor=worktree）。
# ⇒ 我们的**共享是有意的，隔离也是有的**：写侧单一权威落点（MAIN），
#   读侧必须能跨越 per-agent worktree 找到它，且**回执要说明在哪棵树命中**
#   —— 多树语境下「这条读取落在哪个树」是归因的必要条件（fixplan §10.5）。
_REPORTS_ID_RE = re.compile(r"\.hiveweave/reports/([^/]+)")


def _reports_read_scope(
    rel: str, root: str, write_workspace: str
) -> list[tuple[str, str]]:
    """reports 相对路径的候选树：(树标签, 该树内的绝对路径)。

    ``rel`` 是**已剥前导 ``./`` 的相对路径**（如
    ``.hiveweave/reports/<id>/x.png``）。

    顺序 = fixplan §10.2 / ``fixplan:351`` 的读侧顺序：**MAIN（请求者/共享
    权威落点）→ 本树 → 兄弟树**。MAIN 排第一不是随手排的 —— 共享产物的
    权威落点就在 MAIN（``service_create.py:99-105``，写侧单一权威落点），
    所以"可能写了它的树"里 MAIN 的可能性最高；本树（请求者）次之；兄弟树
    是 assignee 的近似上界（``.hiveweave/worktrees/<id>`` 是同一项目下的
    命名空间，``dispatch_pin.py:7,34``）。与 vision 侧
    ``_multi_tree_bases`` 的顺序**完全一致**（两处各写一套会漂移）。
    查不查得到都会在回执里说明，不做断言。
    """
    if not rel:
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _add(base: str) -> None:
        if not base:
            return
        try:
            full = os.path.realpath(os.path.join(base, rel))
        except (OSError, ValueError):
            return
        key = os.path.normcase(full)
        if key in seen:
            return
        seen.add(key)
        out.append((tree_tag(full), full))

    ws = os.path.realpath(write_workspace) if write_workspace else ""
    # MAIN 优先（共享产物的权威落点，fixplan:351）；project_root 未给时
    # root 即项目根，去重会自然退化为单棵。
    _add(os.path.realpath(root))
    _add(ws)
    # 兄弟 worktree（`.hiveweave/worktrees/*`，跳过 _quarantine 兜底目录）
    try:
        wt_root = os.path.join(
            os.path.realpath(root), ".hiveweave", "worktrees",
        )
        for name in sorted(os.listdir(wt_root)):
            if name.startswith("_"):
                continue
            _add(os.path.join(wt_root, name))
    except OSError:
        pass
    return out


def _reports_evidence_hint(file_path: str, root: str, workspace_path: str = "") -> str:
    """reports 路径未命中时，逐树说明「查了哪些树、哪棵树有该 id 目录」。

    46/11 #4 原实现只查项目根一棵树就下结论（``no reports directory for id``）；
    #5（2026-09-12）改为**候选树逐查 + 回执点名命中树**：
    - 任一候选树里有 ``reports/<id>/`` 且非空 → 列出该树里已有哪些文件
      （「证据目录已有但你要的文件未生成」）；
    - 候选树里都没有该 id 目录 → 只说「查过哪些树、均未见到该 id 目录」，
      **不断言"确实不存在"**（L17/L20 同族病：单点查空就下全局结论）。

    非 reports 路径返回空串。
    """
    norm = strip_dot_slash_prefix(file_path)
    m = _REPORTS_ID_RE.search(norm)
    if not m:
        return ""
    eid = m.group(1)
    candidates = _reports_read_scope(norm, root, workspace_path or root)
    if not candidates:
        return f" [no reports directory for id '{eid}' in the searched trees]"
    present: list[str] = []
    for tag, base in candidates:
        # base 是 `<树根>/.hiveweave/reports/<id>/<file>` ⇒ 上溯两级才是
        # `<树根>/.hiveweave/reports`，再拼 `<id>` 才是该 id 的目录。
        d = Path(base).parent.parent / eid
        try:
            if not d.is_dir():
                continue
            entries = [e.name for e in d.iterdir() if not e.name.startswith(".")]
        except OSError:
            continue
        shown = ", ".join(entries[:5]) + ("…" if len(entries) > 5 else "")
        present.append(f"{tag}: {shown}" if entries else f"{tag}: (empty)")
    searched = ", ".join(tag for tag, _ in candidates)
    if present:
        return (
            f" [reports/{eid} found in {len(present)} of the searched trees —"
            f" {'; '.join(present)} — your file is not generated yet]"
        )
    return (
        f" [searched {len(candidates)} tree(s) for reports/{eid}: {searched}"
        f" — no reports directory for this id in any of them"
        f" (this is not proof the evidence does not exist)]"
    )


def _find_shadowed_read(
    file_path: str,
    root: str,
    workspace_path: str,
    primary_full: str,
) -> str | None:
    """本树命中失败后，在候选树里找一个**存在**的同名文件。

    #5（2026-09-12）读侧多树查找：只对**共享产物路径**生效
    （``.hiveweave/reports/**`` —— 四目录共享设计里唯一"平台宿主写、叶子读"
    的通道），不改变普通项目文件的解析（那仍受 per-agent 写隔离约束）。
    命中返回该文件绝对路径，否则 None（调用方维持原 miss 路径与文案）。
    """
    if not _is_platform_reports_read(file_path):
        return None
    rel = strip_dot_slash_prefix(file_path)
    primary_key = os.path.normcase(os.path.realpath(primary_full))
    for _tag, cand in _reports_read_scope(rel, root, workspace_path):
        if os.path.normcase(cand) == primary_key:
            continue
        try:
            if os.path.isfile(cand):
                return cand
        except OSError:
            continue
    return None


def _resolve_reports_across_trees(
    file_path: str,
    root: str,
    workspace_path: str,
    primary_full: str,
) -> tuple[str, str | None] | None:
    """共享 reports 产物的跨树解析：返回 ``(绝对路径, 命中树标签)``。

    候选序 = **MAIN（权威落点）→ 本树 → 兄弟 worktree**（fixplan:351 的
    「MAIN → 请求者树 → assignee 树」；实际顺序由 ``_reports_read_scope``
    单一权威给出，本函数只消费，不另立一套）。
    「本树」= **写侧授权树**（``workspace_path``，即 ``boundary_root``，
    见 ``acl_sandbox/policy.py:54``），**不是** ``_resolve_for_read_detail``
    预解析出来的路径 —— 后者对 reports 走的就是项目根，拿它当基准会把
    "MAIN 命中"误判成"本树命中"从而丢掉归因标签（实测踩过）。

    本树命中返回 ``(path, None)``（无跨树归因需求，不加回执噪音）；
    跨树命中返回 ``(path, 树标签)`` —— **必须点名在哪棵树**，多树语境下
    这是归因的必要条件（fixplan §10.5）。全部未命中返回 None。
    """
    rel = strip_dot_slash_prefix(file_path)
    if not rel:
        return None
    ws_key = os.path.normcase(os.path.realpath(workspace_path)) if workspace_path else ""
    for _tag, cand in _reports_read_scope(rel, root, workspace_path):
        try:
            if not os.path.isfile(cand):
                continue
        except OSError:
            continue
        # 命中就在本树（cand 落在 workspace_path 内）→ 无跨树标签
        if ws_key and _path_within(cand, ws_key):
            return cand, None
        return cand, tree_tag(cand)
    return None


def _path_within(candidate: str, base_key: str) -> bool:
    """``candidate``（已 normcase+realpath 语义）是否落在 ``base_key`` 内。"""
    ck = os.path.normcase(candidate)
    if ck == base_key:
        return True
    try:
        return os.path.commonpath([ck, base_key]).startswith(base_key)
    except ValueError:
        return False


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    if size < 1_048_576:
        return f"{size / 1024:.1f}KB"
    return f"{size / 1_048_576:.1f}MB"


# ── Public tool functions ──────────────────────────────────

async def read_file(
    file_path: str,
    offset: int,
    limit: int,
    workspace_path: str,
    project_root: str | None = None,
    extra_read_dirs: list[str] | None = None,
    *,
    agent_id: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Read a file with line numbers. Refuses binary files.

    Returns {success, output, error} where output is line-numbered text.
    Reads may resolve anywhere under the project root (∪ extra_read_dirs, P1);
    writes stay sandboxed to workspace_path (see write_file).
    """
    if not file_path:
        return {"success": False, "output": "",
                "error": "Error: filePath is required"}

    root = project_root or infer_project_root(workspace_path)
    full, hint = _resolve_for_read_detail(
        workspace_path, file_path, root, extra_read_dirs
    )
    if hint is not None:
        # L6（2026-09-11）：hint 只可能是「幽灵 worktree 前缀路径」——那是
        # **模型自己写错了路径**，平台无责。此前用 blocked=True 表达，
        # 而 blocked 的语义是「平台拒绝执行 —— not a model mistake」
        # （result.py 的 docstring）⇒ agent 收到「不是你的 bug」信号后
        # 在同一 run 里原地重撞（52_B 实测）。
        # 改判 bad_args：归因落到调用方，stall 走 tool_failed。
        return ToolResult.err(f"Error: {hint}", fact="bad_args").to_dict()
    if full is None:
        return {"success": False, "output": "",
                "error": f'Error: Sandbox violation — "{file_path}" '
                         "outside project"}

    if _check_hiveweave_dir(full, root):
        return {"success": False, "output": "",
                "error": 'Error: Access denied: ".hiveweave" is the '
                         "HiveWeave system directory."}

    if _is_sensitive(file_path):
        return {"success": False, "output": "",
                "error": f'Error: Access denied: "{file_path}" matches a '
                         "sensitive file pattern."}

    p = Path(full)
    # #5：共享产物（reports/**）的解析**先走跨树候选序**（MAIN → 本树 →
    # 兄弟树，见 _reports_read_scope；fixplan:351），因为写侧权威落点是
    # MAIN；单一解析点无法表达"哪棵树的"。
    # 非共享路径维持原解析（隔离不受影响）。
    read_tree_tag: str | None = None
    if _is_platform_reports_read(file_path):
        got = _resolve_reports_across_trees(
            file_path, root or "", workspace_path, full,
        )
        if got is None:
            from hiveweave.services import fs_errors

            reports_hint = _reports_evidence_hint(
                file_path, root or "", workspace_path,
            )
            fs_errors.observed_absent(
                full, agent_id=agent_id, project_id=project_id
            )
            return {"success": False, "output": "",
                    "error": f"Error: File not found: {file_path}."
                             f"{reports_hint}{READ_MISS_HINT}",
                    fs_errors.ERROR_CODE_KEY: fs_errors.NOT_FOUND}
        full, read_tree_tag = got
        p = Path(full)
    elif not p.exists():
        # FS 错误码分类学 + 观察到缺席事件（编排层区分「漏步」vs「不可读」；
        # 带 project_id 的事实才会进 L3 总线触发按事实唤醒——审计 P1-2）
        from hiveweave.services import fs_errors

        fs_errors.observed_absent(
            full, agent_id=agent_id, project_id=project_id
        )
        return {"success": False, "output": "",
                "error": f"Error: File not found: {file_path}."
                         f"{READ_MISS_HINT}",
                fs_errors.ERROR_CODE_KEY: fs_errors.NOT_FOUND}
    if p.is_dir():
        from hiveweave.services import fs_errors

        return {"success": False, "output": "",
                "error": f"Error: Path is a directory, not a file: {file_path}",
                fs_errors.ERROR_CODE_KEY: fs_errors.IS_A_DIRECTORY}

    if _is_binary(full):
        size = p.stat().st_size
        return {"success": False, "output": "",
                "error": f"Error: Cannot display binary file ({size} bytes). "
                         "Use list_files to see metadata instead."}

    # R5: 大文件保护 — 超过 MAX_READ_BYTES 只读首块，防止一次性 read_text 导致 OOM
    size = p.stat().st_size
    truncated_note = ""
    try:
        if size > MAX_READ_BYTES:
            with open(full, "rb") as fh:
                raw = fh.read(MAX_READ_BYTES)
            content = raw.decode("utf-8", errors="replace")
            truncated_note = (
                f"\n\n⚠️ 文件较大（{_format_size(size)}），仅读取前 "
                f"{_format_size(MAX_READ_BYTES)}。请使用 offset 参数读取后续部分。"
            )
        else:
            content = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        from hiveweave.services import fs_errors

        return {"success": False, "output": "",
                "error": f"Error: {type(exc).__name__}: {exc}",
                fs_errors.ERROR_CODE_KEY: fs_errors.classify_oserror(exc)}

    # Split keeping line semantics
    lines = content.split("\n")
    total = len(lines)

    start = max(int(offset or 0), 0)
    lim = int(limit or MAX_READ_LINES)
    if lim <= 0:
        lim = MAX_READ_LINES
    end = min(start + lim, total)

    selected = lines[start:end]

    formatted_lines: list[str] = []
    for idx, line in enumerate(selected, start=start):
        # Truncate overly long lines
        if len(line) > MAX_LINE_LENGTH:
            line = line[:MAX_LINE_LENGTH] + " ... [line truncated]"
        formatted_lines.append(f"{idx + 1}: {line}")

    body = "\n".join(formatted_lines)
    suffix = f"\n\n(Showing lines {start + 1}-{end} of {total})"
    if truncated_note:
        suffix = truncated_note + suffix
    if read_tree_tag:
        # #5：跨树命中要说明**在哪棵树**（多树归因必要条件；共享产物落 MAIN
        # 是四目录共享设计的正常形态，不是"硬重定向"）
        suffix += (
            f"\n\n[read from {read_tree_tag}"
            " — shared .hiveweave/reports/ is written to MAIN by design"
            " (remote worktrees are visible to all agents; see git_worktree"
            " shared 4-dir contract)]"
        )
    record_file_version(full)
    return {"success": True, "output": body + suffix, "error": None}


async def write_file(
    file_path: str,
    content: str,
    workspace_path: str,
) -> dict[str, Any]:
    """Write a file (overwrite). Auto-creates parent directories."""
    if not file_path:
        return {"success": False, "output": "",
                "error": "Error: filePath is required"}
    if content is None:
        return {"success": False, "output": "",
                "error": "Error: content is required"}

    full, hint = _resolve_safe_detail(workspace_path, file_path)
    if hint is not None:
        # L6：幽灵 worktree 前缀 = 模型写错路径（见 read_file 处的长注释）。
        return ToolResult.err(f"Error: {hint}", fact="bad_args").to_dict()
    if full is None:
        return {"success": False, "output": "",
                "error": f'Error: Sandbox violation — "{file_path}" '
                         "outside workspace"}

    if _check_hiveweave_dir(full, workspace_path, write=True):
        return {"success": False, "output": "",
                "error": 'Error: Access denied: ".hiveweave" is the '
                         "HiveWeave system directory."}

    if _is_sensitive(file_path):
        return {"success": False, "output": "",
                "error": f'Error: Access denied: "{file_path}" matches a '
                         "sensitive file pattern."}

    p = Path(full)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        # Normalize to LF — prevents CRLF/LF mismatch breaking grep
        normalized = content.replace("\r\n", "\n").replace("\r", "\n")
        p.write_text(normalized, encoding="utf-8", newline="")
    except OSError as exc:
        return {"success": False, "output": "",
                "error": f"Error: {type(exc).__name__}: {exc}"}

    size = len(content.encode("utf-8"))
    log.info("file.write", path=file_path, bytes=size)
    record_file_version(p)
    return {"success": True,
            "output": (
                f"Wrote {file_path} ({size} bytes)"
                f"{write_tree_suffix(workspace_path)}"
            ),
            "error": None}


async def list_files(
    path: str,
    workspace_path: str,
    recursive: bool = False,
    maxdepth: int = 1,
    include_ignored: bool = False,
    project_root: str | None = None,
    extra_read_dirs: list[str] | None = None,
) -> dict[str, Any]:
    """List directory contents with [DIR]/[FILE] tags and sizes.

    BUG-019 修复：支持 recursive + maxdepth 参数，让 CEO 一次看多层目录，
    避免反复调 list_files 探索不同目录导致首次 chat 30s+。

    include_ignored: 列出被 IGNORED_DIRS 屏蔽的 .hiveweave 子目录——
    coordinator 审查 worktree 代码时需要（默认 .hiveweave 整目录被跳过）。

    Reads may list anywhere under the project root; empty path lists the
    agent's own write workspace.
    """
    ws = workspace_path or "."
    root = project_root or infer_project_root(workspace_path)
    depth = max(1, min(maxdepth, 3)) if recursive else 1

    if path:
        full, hint = _resolve_for_read_detail(
            workspace_path, path, root, extra_read_dirs
        )
        if hint is not None:
            # L6：幽灵 worktree 前缀 = 模型写错路径（见 read_file 处注释）。
            return ToolResult.err(f"Error: {hint}", fact="bad_args").to_dict()
        if full is None:
            return {"success": False, "output": "",
                    "error": "Error: Sandbox violation - "
                             "path must be within project"}
        # .hiveweave 系统目录保护 — 对 protected 区域返回错误，
        # 但允许列出 .hiveweave 根目录（只显示 agent 可用子目录）
        if Path(full).name == HIVEWEAVE_DIR and \
           _check_hiveweave_dir(full, root):
            # Listing .hiveweave root — show only accessible subdirectories
            hw_root = Path(full)
            allowed_subs = ["shared", "worktrees", "reports", "drafts"]
            hw_lines: list[str] = []
            for sub in allowed_subs:
                sub_path = hw_root / sub
                if sub_path.exists() and sub_path.is_dir():
                    hw_lines.append(f"[DIR]  {sub}/")
            msg = "Accessible agent work directories in .hiveweave/:" if hw_lines \
                  else "No agent work directories found in .hiveweave/."
            return {
                "success": True,
                "output": msg + ("\n" + "\n".join(hw_lines) if hw_lines else ""),
                "error": None,
            }
        if _check_hiveweave_dir(full, root):
            return {"success": False, "output": "",
                    "error": "Error: This part of `.hiveweave/` is a protected "
                             "system area. Accessible subdirectories: "
                             "`.hiveweave/shared/`, `.hiveweave/worktrees/`, "
                             "`.hiveweave/reports/`, `.hiveweave/drafts/`."}
    else:
        full = str(Path(ws).resolve())

    p = Path(full)
    # worktrees 内的列表自动放开 ignore 过滤（审查场景）
    try:
        rel = p.resolve().relative_to(Path(root).resolve())
        if len(rel.parts) >= 2 and rel.parts[0] == HIVEWEAVE_DIR \
           and rel.parts[1] == "worktrees":
            include_ignored = True
    except ValueError:
        pass
    if not p.exists():
        # P1-1: 对 .hiveweave/shared 的探路失败要分支化提示 —— 共享区在
        # worktree 里"空目录不物化"，报"不在树内"会误导 agent 判定通道
        # 不存在（platform-issue-report P1-1：两次把它当"真不在树内"）。
        shared_hint = READ_MISS_HINT
        try:
            _rel = p.resolve().relative_to(Path(root).resolve()).parts
            # 匹配 rel 路径中任意位置的 `.hiveweave` 紧随 `shared` 段对：
            # 同时覆盖 MAIN（.hiveweave/shared/...）与叶子 worktree 嵌套
            # （.hiveweave/worktrees/<sid>/.hiveweave/shared/...）两种布局。
            if any(
                _rel[i] == HIVEWEAVE_DIR and i + 1 < len(_rel) and _rel[i + 1] == "shared"
                for i in range(len(_rel) - 1)
            ):
                shared_hint = (
                    " The .hiveweave/shared/ chain may not be materialized "
                    "in your worktree yet (empty shared/ is not tracked). "
                    "Write: write_file to .hiveweave/shared/<file> → "
                    "checkpoint → merge; members see it after their next "
                    "worktree merge."
                )
        except Exception as exc:
            # 生成 shared 提示失败**不改变**结论（目录本就不存在）：提示是
            # 附加教学，不是判定依据。吞掉并留痕，让排障时能看到为什么没提示。
            log.debug("file.shared_hint_failed", path=str(path), err=str(exc))
        return {"success": False, "output": "",
                "error": f"Error: Directory not found: {path}."
                         f"{shared_hint}"}
    if not p.is_dir():
        return {"success": False, "output": "",
                "error": f"Error: Not a directory: {path}"}

    lines: list[str] = []
    count = 0
    # include_ignored（审查 worktree 场景）：放开 .hiveweave 目录过滤
    ignored = IGNORED_DIRS - {HIVEWEAVE_DIR} if include_ignored else IGNORED_DIRS

    def _walk(d: Path, prefix: str, current_depth: int):
        nonlocal count
        try:
            entries = sorted(d.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        except OSError as exc:
            lines.append(f"{prefix}(error: {exc})")
            return
        for entry in entries:
            if count >= MAX_LIST_FILES:
                if not any("truncated" in l for l in lines[-1:]):
                    lines.append(f"... (truncated at {MAX_LIST_FILES} entries)")
                return
            if entry.is_dir() and entry.name in ignored:
                continue
            try:
                rel = entry.relative_to(p)
                indent = "  " * (current_depth - 1)
                if entry.is_dir():
                    lines.append(f"{indent}[DIR]  {rel}/")
                    count += 1
                    if current_depth < depth:
                        _walk(entry, prefix, current_depth + 1)
                elif entry.is_file():
                    size = entry.stat().st_size
                    lines.append(f"{indent}[FILE] {rel} ({_format_size(size)})")
                    count += 1
                else:
                    lines.append(f"{indent}[???]  {rel}")
                    count += 1
            except OSError:
                continue

    _walk(p, "", 1)

    body = "\n".join(lines) if lines else "(empty directory)"
    return {
        "success": True,
        "output": listing_header(full) + "\n" + body,
        "error": None,
    }


# ── Pydantic models + @tool registration (Phase 2 migration) ──────

from pydantic import BaseModel, Field, ConfigDict

from .base import tool
from .result import ToolResult


class ReadFileParams(BaseModel):
    """Parameters for read_file tool."""
    model_config = ConfigDict(populate_by_name=True)

    file_path: str = Field(
        alias="filePath",
        description="Path to the file to read (relative to workspace).",
        json_schema_extra={"aliases": ["path", "file_path", "file"]},
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="Starting line number (0-based, default: 0).",
    )
    limit: int = Field(
        default=2000,
        ge=1,
        description="Max lines to read (default: 2000).",
    )


class WriteFileParams(BaseModel):
    """Parameters for write_file tool."""
    model_config = ConfigDict(populate_by_name=True)

    file_path: str = Field(
        alias="filePath",
        description="Path to the file to write (relative to workspace).",
        json_schema_extra={"aliases": ["path", "file_path", "file"]},
    )
    content: str = Field(
        description="Full file content to write. Overwrites existing file.",
    )


class ListFilesParams(BaseModel):
    """Parameters for list_files tool."""
    model_config = ConfigDict(populate_by_name=True)

    dir_path: str | None = Field(
        default=None,
        alias="dirPath",
        description="Directory path to list (relative to workspace). Default: workspace root.",
        json_schema_extra={"aliases": ["path", "directory", "dir", "dir_path"]},
    )
    recursive: bool = Field(
        default=False,
        description="If true, list recursively. Default: false.",
    )
    maxdepth: int = Field(
        default=1,
        ge=1,
        le=3,
        description="Max depth when recursive (1-3). Default: 1. Values above 3 "
        "are clamped to 3 — deeper recursion is not supported.",
    )
    include_ignored: bool = Field(
        default=False,
        description="Also list .hiveweave subdirectories (e.g. worktrees when "
        "reviewing executor code). Default: false.",
        json_schema_extra={"aliases": ["include_ignored", "no_ignore"]},
    )


@tool(
    "read_file",
    "Reads file contents with line numbers. Relative paths resolve from your "
    "workspace. Reviewers read unmerged code at .hiveweave/worktrees/<shortId>/. "
    "Do not use ../ for MAIN docs. Writes stay confined to your workspace.",
    requires_workspace=True,
    security_level="file_op",
)
async def read_file_tool(params: ReadFileParams, agent_id: str, workspace: str) -> ToolResult:
    """Read a file with line numbers. Refuses binary files."""
    # P1 (§5.5b①)：外部只读参考目录（仅读工具生效，写仍锁 workspace）
    extra = await fetch_additional_read_dirs(infer_project_root(workspace))
    result = await read_file(
        file_path=params.file_path,
        offset=params.offset,
        limit=params.limit,
        workspace_path=workspace,
        extra_read_dirs=extra,
        agent_id=agent_id,
        project_id=await _project_id_for_workspace(workspace),
    )
    if result.get("success"):
        return ToolResult.ok(result["output"])
    # 复审 P2-2：包装器必须透传 blocked（护栏拒绝语义），与 bash_tool 对齐
    if result.get("blocked"):
        return ToolResult.blocked_err(result.get("error", "Unknown error"))
    # FS 错误码分类学（46/11 #4）：稳定错误码随回执透传（extra 合并）
    # L6：fact 同为内核判定的事实位，必须一起透传（否则幽灵路径的
    # bad_args 归因在包装层丢失，退回「无位」）。
    err_extra: dict = {}
    code = result.get("error_code")
    if code:
        err_extra["error_code"] = code
    return ToolResult.err(
        result.get("error", "Unknown error"),
        fact=result.get("fact"),
        **err_extra,
    )


async def _project_id_for_workspace(workspace: str) -> str | None:
    """workspace → project_id（read 缺席事实的归属；审计 P1-2）。

    仅在 read miss 路径消费——查询频度低，直接全表 normcase 比对即可，
    不引缓存。fail-open None（事实退化为平台级，不落库不唤醒）。"""
    try:
        from hiveweave.db import meta as meta_db

        want = os.path.normcase(os.path.normpath(workspace))
        rows = await meta_db.query("SELECT id, workspace_path FROM projects")
        for r in rows:
            ws = r["workspace_path"] or ""
            if ws and os.path.normcase(os.path.normpath(ws)) == want:
                return str(r["id"])
    except Exception as exc:
        # 查不到 project_id 属**预期降级**（meta 库未初始化 / 无匹配行）：
        # 调用方把 None 解释为"非 MAIN 树"，不影响读路径正确性。留痕备排障。
        log.debug("file.main_project_id_unresolved",
                  workspace=workspace, err=str(exc))
    return None


@tool(
    "write_file",
    "Writes content to a file (overwrite) inside your own workspace only. "
    "Cannot write outside your worktree / workspace. Auto-creates parent dirs.",
    requires_workspace=True,
    security_level="file_op",
)
async def write_file_tool(params: WriteFileParams, agent_id: str, workspace: str) -> ToolResult:
    """Write a file (overwrite). Auto-creates parent directories."""
    # 写路径闸（46/11 #6）：同路径并发写（父/子代理共享 worktree）advisory
    # 冲突，advice never a block；try/finally 配对防异常泄漏持有。
    from hiveweave.tools import write_gate

    if not write_gate.try_acquire(params.file_path, workspace):
        return ToolResult.err(write_gate.conflict_message(params.file_path))
    try:
        result = await write_file(
            file_path=params.file_path,
            content=params.content,
            workspace_path=workspace,
        )
    finally:
        write_gate.release(params.file_path, workspace)
    if result.get("success"):
        return ToolResult.ok(result["output"])
    # 复审 P2-2：包装器必须透传 blocked（护栏拒绝语义），与 bash_tool 对齐
    if result.get("blocked"):
        return ToolResult.blocked_err(result.get("error", "Unknown error"))
    # L6：fact 一并透传（幽灵路径的 bad_args 归因不得在包装层丢失）
    return ToolResult.err(
        result.get("error", "Unknown error"), fact=result.get("fact")
    )


@tool(
    "list_files",
    "Lists files and directories. Empty path lists your workspace. Reviewers "
    "list unmerged code at .hiveweave/worktrees/<shortId>/. Do not use ../ "
    "for MAIN. When recursive=true, maxdepth controls how many levels to "
    "descend — capped at 3 (values above 3 are clamped to 3).",
    requires_workspace=True,
    security_level="file_op",
)
async def list_files_tool(params: ListFilesParams, agent_id: str, workspace: str) -> ToolResult:
    """List directory contents with [DIR]/[FILE] tags and sizes."""
    # P1 (§5.5b①)：外部只读参考目录（仅读工具生效）
    extra = await fetch_additional_read_dirs(infer_project_root(workspace))
    result = await list_files(
        path=params.dir_path or "",
        workspace_path=workspace,
        recursive=params.recursive,
        maxdepth=params.maxdepth,
        include_ignored=params.include_ignored,
        extra_read_dirs=extra,
    )
    if result.get("success"):
        return ToolResult.ok(result["output"])
    # 复审 P2-2：包装器必须透传 blocked（护栏拒绝语义），与 bash_tool 对齐
    if result.get("blocked"):
        return ToolResult.blocked_err(result.get("error", "Unknown error"))
    # L6：fact 一并透传（幽灵路径的 bad_args 归因不得在包装层丢失）
    return ToolResult.err(
        result.get("error", "Unknown error"), fact=result.get("fact")
    )
