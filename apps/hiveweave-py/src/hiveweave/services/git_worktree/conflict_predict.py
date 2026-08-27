"""只读合并冲突预演 — submit 硬门 / checkpoint 提示共用的预测核心。

机制: ``git merge-tree --write-tree <base> <branch>`` (Git 2.38+) 纯内存
三方合并预演, 不碰工作区/索引/锁。GitHub/GitLab PR 可合并检测同款。

fail-open / fail-closed 边界:
- 基础设施失败(无分支/无 base/git 过旧/超时)一律 unknown → 调用方放行;
- 只有 merge-tree 明确报冲突(exit 1)才 conflict → 调用方拦截。
唯一阻塞条件 = 真实冲突, 杜绝误伤。
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

import structlog

from .constants import GIT_TIMEOUT
from .git_cmd import _current_branch, _git, _resolve_base_branch

log = structlog.get_logger(__name__)

# 进程级缓存: 首次探测 merge-tree 不可用后置 False, 之后永久跳过(省子进程)
_merge_tree_supported: bool | None = None

# merge-tree 冲突行形态:
#   CONFLICT (content): Merge conflict in <path>
#   CONFLICT (modify/delete): <path> deleted in <ref> and modified in <ref>...
#   CONFLICT (rename/rename): <path> renamed to <path> in <ref>...
_CONFLICT_LINE_RE = re.compile(r"^CONFLICT \([^)]+\): (.+)$", re.MULTILINE)
# 老版本 git (<2.38) 对 --write-tree 报错的特征。收紧为精确前缀——裸
# "usage:" 过宽, fatal 消息若碰巧含该词会把特性永久降级(误伤放大)。
_DEGRADED_MARKERS = ("unknown option", "usage: git merge-tree")


@dataclass
class ConflictPrediction:
    """冲突预演结果。

    status:
        up_to_date — 无分叉或一方零提交, 数学上不可能冲突
        clean       — 双方都有新提交, merge-tree 预演无冲突
        conflict    — merge-tree 明确报冲突 (conflicts 为冲突文件清单)
        unknown     — 无法预测 (基础设施失败, 调用方应放行)
    """

    status: str
    behind: int = 0                    # base 领先 branch 的提交数
    ahead: int = 0                     # branch 领先 base 的提交数
    conflicts: list[str] = field(default_factory=list)
    degraded: bool = False             # True = 本机 git 不支持 merge-tree


def _parse_conflict_files(output: str) -> list[str]:
    """从 merge-tree 输出提取冲突文件清单(去重保序)。"""
    files: list[str] = []
    for m in _CONFLICT_LINE_RE.finditer(output):
        rest = m.group(1).strip()
        if rest.startswith("Merge conflict in "):
            path = rest[len("Merge conflict in "):].strip()
        elif " deleted in " in rest:
            path = rest.split(" deleted in ")[0].strip()
        elif " renamed to " in rest:
            path = rest.split(" renamed to ")[0].strip()
        else:
            continue  # 未知形态不猜路径 — 留空由调用方按"冲突存在"处理
        if path and path not in files:
            files.append(path)
    return files


async def _merge_tree(base: str, branch: str, cwd: str) -> tuple[int, str]:
    """git merge-tree --write-tree 带退出码 (exit 1 = 冲突)。

    ``_git`` 只返回 bool, 无法区分 exit 1(冲突)与 exit 128(fatal) —
    fail-closed 判定必须看退出码, 这里单独跑子进程。
    """
    try:
        from hiveweave.util.win_subprocess import windows_no_window_kwargs

        proc = await asyncio.create_subprocess_exec(
            "git", "merge-tree", "--write-tree", base, branch,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **windows_no_window_kwargs(),
        )
    except FileNotFoundError:
        return -1, "git not found on PATH"
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=GIT_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -2, "merge-tree timed out"
    output = stdout.decode("utf-8", errors="replace").strip() if stdout else ""
    return (proc.returncode if proc.returncode is not None else -3), output


async def predict_merge_conflicts(worktree_path: str) -> ConflictPrediction:
    """只读预演 worktree 检出分支与 base(main/master) 的合并冲突。

    预演分支 = worktree 实际检出分支(与 ``_resolve_agent_branch``
    "事实优先"一致), 预测的就是 merge 将要合的东西。
    """
    global _merge_tree_supported

    branch = await _current_branch(worktree_path)
    if not branch:
        return ConflictPrediction(status="unknown")
    base = await _resolve_base_branch(worktree_path)
    if not base:
        return ConflictPrediction(status="unknown")

    async def _count(rev_range: str) -> int:
        """计数; 失败返回 -1 哨兵(与真实 0 区分, 防误报 up_to_date)。"""
        ok, out = await _git(["rev-list", "--count", rev_range], worktree_path)
        if not ok:
            return -1
        try:
            return int((out or "0").strip() or "0")
        except ValueError:
            return -1

    behind = await _count(f"{branch}..{base}")
    ahead = await _count(f"{base}..{branch}")
    if behind < 0 or ahead < 0:
        return ConflictPrediction(status="unknown")
    # 无东西可合 / main 未动 — 均不可能冲突, 廉价剪枝
    if ahead == 0 or behind == 0:
        return ConflictPrediction(status="up_to_date", behind=behind, ahead=ahead)

    if _merge_tree_supported is False:
        return ConflictPrediction(
            status="unknown", behind=behind, ahead=ahead, degraded=True,
        )

    rc, out = await _merge_tree(base, branch, worktree_path)
    if rc == 0:
        return ConflictPrediction(status="clean", behind=behind, ahead=ahead)
    if rc == 1:
        # exit 1 = 冲突。解析不出文件清单仍按冲突拦(fail-closed)。
        return ConflictPrediction(
            status="conflict", behind=behind, ahead=ahead,
            conflicts=_parse_conflict_files(out),
        )
    low = (out or "").lower()
    if any(marker in low for marker in _DEGRADED_MARKERS):
        # 老版本 git (<2.38) 不识别 --write-tree — 进程级降级, 之后不再尝试
        _merge_tree_supported = False
        log.warning(
            "conflict_predict_merge_tree_unsupported", output=out[:120],
        )
        return ConflictPrediction(
            status="unknown", behind=behind, ahead=ahead, degraded=True,
        )
    # 超时 / fatal / 仓库损坏 — fail-open 放行
    log.warning(
        "conflict_predict_merge_tree_failed",
        branch=branch, base=base, rc=rc, output=(out or "")[:120],
    )
    return ConflictPrediction(status="unknown", behind=behind, ahead=ahead)
