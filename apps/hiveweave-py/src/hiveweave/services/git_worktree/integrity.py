"""E8 复盘：merge 后整体性检查槽 —— 软件实例（静默吞错模式扫描）。

复盘致命链三：并行合并压力下 ``main.py`` 堆积 38 处 try/except 兜底，
admin 路由导入失败被静默吞掉后挂空 router，启动零报错 → F1（/_admin
全线 404）。核心不变式：**合成的整体不允许从未被整体性地看过一眼就放行**。

本模块只提供「检查槽 + 回执字段」；检查内容是领域配置。软件实例 = 本文件：

- 静默降级模式扫描（except 收敛块内的空 APIRouter / 空兜底 / 仅 pass）。
- FAIL → merge 回执带 blocking_issues；auto-submit 时 evidence 前置
  verdict=FAIL，由既有 E2 强制路由转到 rework（消费方是代码不是提示词）。

领域扩展位：``INTEGRITY_CHECKERS`` 按仓库类型注册——小说 = 并稿一致性 /
伏笔回收（LLM 审校任务承担）；视频 = 拉通播放抽检。本轮只交付软件实例。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

_SILENT_EXCEPT_RE = re.compile(
    r"^\s*except(\s+\(?(Exception|BaseException)\)?)?\s*:"
)
_PASS_ONLY_RE = re.compile(r"^\s*pass\s*(#.*)?$")
_FALLBACK_ASSIGN_RE = re.compile(
    r"^\s*[\w.]+\s*=\s*(APIRouter\s*\(\s*\)|\[\s*\]|\{\s*\}|None)\s*(#.*)?$"
)
_EXPLICIT_MARKERS = ("log.", "logger.", "raise ")

# 收敛窗口：except 块内向后最多看多少行（F1 现场 except 后 2-3 行即兜底）
_EXCEPT_BLOCK_LOOKAHEAD = 6


@dataclass
class IntegrityReport:
    """整体性检查回执。``passed=False`` ⇒ ``issues`` 即 blocking 清单。"""

    checks: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.issues


def scan_silent_swallow(text: str, path_label: str) -> list[str]:
    """扫描单文件：``except`` 收敛块内的静默降级兜底。

    判定口径（对齐 F1 指纹）：
    - except 块内含 ``x = APIRouter()`` / 空 list/dict / ``None`` 兜底 → 红牌
      （启动零报错，合成整体被换成一个空壳）。
    - except 块内仅 ``pass``、无日志/上抛 → 红牌（静默吞错）。
    - except 块内含 ``log.*`` / ``logger.*`` / ``raise`` → 显式降级，放行。
    """
    issues: list[str] = []
    lines = (text or "").splitlines()
    n = len(lines)
    i = 0
    while i < n:
        line = lines[i]
        if not _SILENT_EXCEPT_RE.match(line):
            i += 1
            continue
        base_indent = len(line) - len(line.lstrip())
        block_found: list[str] = []
        explicit = False
        j = i + 1
        while j < n and j <= i + _EXCEPT_BLOCK_LOOKAHEAD:
            bl = lines[j]
            if not bl.strip():
                j += 1
                continue
            indent = len(bl) - len(bl.lstrip())
            if indent <= base_indent:
                break  # dedent，except 块结束
            if _FALLBACK_ASSIGN_RE.match(bl):
                block_found.append(
                    f"{path_label}:{j + 1} 静默降级兜底（except 内挂空 "
                    "APIRouter/空值）；请 fail-fast 或显式降级日志"
                )
                break
            if _PASS_ONLY_RE.match(bl):
                block_found.append(
                    f"{path_label}:{j + 1} 静默吞错（except 内仅 pass）"
                )
            if any(mk in bl for mk in _EXPLICIT_MARKERS):
                explicit = True
                break
            j += 1
        if block_found and not explicit:
            issues.extend(block_found)
        i += 1
    return issues


async def _changed_python_files(workspace_path: str) -> list[str]:
    """merge 落 MAIN 引入的 .py 文件（复用处：merge success 后 HEAD 即 merge commit）。"""
    from hiveweave.services.git_worktree.git_cmd import _git as _run_git

    rels: list[str] = []
    try:
        ok, out = await _run_git(
            ["diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
            workspace_path,
        )
    except Exception as e:
        log.debug("integrity_diff_tree_failed", error=str(e))
        ok, out = False, ""
    if ok and out:
        rels = [
            ln.strip()
            for ln in out.splitlines()
            if ln.strip().endswith(".py")
        ]
    if not rels:
        # 兜底：差集扫描（no-op merge / 已合入场景）
        try:
            ok2, out2 = await _run_git(
                ["diff", "--name-only", "main...HEAD"], workspace_path
            )
        except Exception:
            ok2, out2 = False, ""
        if ok2 and out2:
            rels = [
                ln.strip()
                for ln in out2.splitlines()
                if ln.strip().endswith(".py")
            ]
    return rels


async def run_integrity_checks(
    workspace_path: str | None, branch: str | None = None
) -> IntegrityReport:
    """merge 后整体性检查（软件实例：静默吞错扫描）。

    纯静态、同步 IO、毫秒级；失败只进回执，绝不中断 merge 本身。
    领域扩展位在这里：后续按仓库类型选择 checker 集合。
    """
    report = IntegrityReport(checks=["software: silent-swallow scan"])
    if not workspace_path:
        report.issues.append("integrity scan skipped: no workspace")
        return report
    files = await _changed_python_files(workspace_path)
    for rel in files:
        p = Path(workspace_path) / rel
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        report.issues.extend(scan_silent_swallow(text, rel))
    if report.issues:
        log.warning(
            "git_worktree.integrity_fail",
            branch=branch,
            scanned=len(files),
            issues=report.issues,
        )
    else:
        log.info(
            "git_worktree.integrity_ok",
            branch=branch,
            scanned=len(files),
        )
    return report