"""dispatch_facts — 派单时自动核验快照（40 轮 verifiedFacts L2）。

派单时由平台自动采集**廉价可机核事实**（git 状态/文件系统快照），追加到
任务卡，让执行者开局即知现场。与派单方手写的 verifiedFacts 互补：
- 本模块 = 平台自动核验（机器探测，带 @ 派单时点标记）
- verifiedFacts = 派单方人工核验（语义事实，信任派单方）

全部 best-effort：任何探测失败静默跳过，绝不阻塞派单。
"""

from __future__ import annotations

import time
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

_MAX_FACTS_PER_GROUP = 4
_GIT_TIMEOUT_S = 10


def _git(args: list[str], cwd: str,
         project_root: str | None = None) -> tuple[bool, str]:
    """Run a git command, return (ok, stripped stdout). **接信任锚**（#19）。

    ⚠ 以前这里自带一个裸 `hidden_run(["git", ...])`（唯一漏斗 ✅，但**未接锚**）
    ⇒ 平台 git 会去读 agent 可写的 `<wt>/.git` 指针与 `commondir`
    （实测形态见 `git_anchor` 模块 docstring 的三条路径）。锚的派生与
    「拒因 ⇒ 不跑」的 fail-closed 纪律必须只有一份 ⇒ 改走 `git_cmd._git_sync`
    （它是 `_git` 的同步孪生体，两者只差 spawn 原语）。

    `project_root` 由调用方显式传主仓路径（worktree 那几个调用点）。**不传时保持
    None**，让锚自己上溯 —— ⚠ **绝不回落到 `or cwd`**：cwd 可能是 worktree，
    把它当项目根会让 `resolve_anchor` 派生一个不可能存在的 gitdir ⇒
    `AnchorRefusal` ⇒ 拒跑 ⇒ **静默降级**（审计必修 2 实测）。派生不出时
    `git_anchor` 的第三档是"不钉锚继续跑"（loud warning），那才是既有行为。
    """
    from hiveweave.services.git_worktree.git_cmd import _git_sync

    return _git_sync(args, cwd, timeout=_GIT_TIMEOUT_S,
                     project_root=project_root)


def _main_git_facts(main_ws: str) -> list[str]:
    facts: list[str] = []
    ok, head = _git(["rev-parse", "--short", "HEAD"], main_ws)
    if not ok:
        return facts
    ok, subject = _git(["log", "-1", "--format=%s"], main_ws)
    subject = subject[:70] if ok else ""
    facts.append(f"MAIN HEAD: {head} {subject}".rstrip())
    ok, dirty = _git(["status", "--porcelain"], main_ws)
    if ok:
        n = len([l for l in dirty.splitlines() if l.strip()])
        facts.append(f"MAIN 未提交改动: {n} 处")
    return facts


def _worktree_git_facts(main_ws: str, wt: str, label: str) -> list[str]:
    facts: list[str] = []
    # ⚠ cwd 是 worktree、项目根是 main_ws ⇒ 两者必须分开传，否则锚会把
    #   worktree 当项目根派生（#19 的落点之一）。
    ok, branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], wt,
                      project_root=main_ws)
    if not ok:
        return facts
    ok_a, ahead = _git(["rev-list", "--count", f"main..{branch}"], main_ws,
                       project_root=main_ws)
    ok_b, behind = _git(["rev-list", "--count", f"{branch}..main"], main_ws,
                        project_root=main_ws)
    if ok_a and ok_b:
        facts.append(
            f"worktree {label}（{branch}）：领先 main {ahead} / 落后 {behind}"
        )
    return facts


def _deliverable_file_facts(main_ws: str) -> list[str]:
    facts: list[str] = []
    common = ["index.html", "package.json", "README.md"]
    for name in common:
        p = Path(main_ws) / name
        if p.is_file():
            st = p.stat()
            age_min = max(0, int((time.time() - st.st_mtime) / 60))
            facts.append(
                f"交付物 {name}: {st.st_size} bytes（{age_min} 分钟前修改）"
            )
    return facts


def collect_dispatch_facts(
    main_ws: str | None,
    target_worktree: str | None = None,
    target_label: str | None = None,
) -> list[str]:
    """采集派单时点的自动核验事实（同步，快，全部 best-effort）。

    返回事实文本列表（可直接进任务卡的「平台自动核验快照」块）。
    """
    facts: list[str] = []
    if main_ws and Path(main_ws).is_dir():
        facts += _main_git_facts(main_ws)
        facts += _deliverable_file_facts(main_ws)
    if target_worktree and Path(target_worktree).is_dir():
        label = target_label or "目标工位"
        ok, branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], target_worktree,
                          project_root=main_ws)
        if ok:
            facts += _worktree_git_facts(main_ws, target_worktree, label) if main_ws else []
            ok_u, untracked = _git(
                ["status", "--porcelain"], target_worktree,
                project_root=main_ws,
            )
            if ok_u:
                n = len([l for l in untracked.splitlines() if l.strip()])
                if n:
                    facts.append(f"worktree {label} 未提交/未跟踪文件: {n} 处")
    return facts[:_MAX_FACTS_PER_GROUP * 3]


def format_facts_block(facts: list[str], *, auto: bool = False) -> str:
    """渲染事实块（追加到任务描述）。空列表 → 空串。"""
    if not facts:
        return ""
    header = (
        "## 平台自动核验快照（派单时点，机器探测）"
        if auto
        else "## 已核事实（派单方核验，可直接采信；与你的观察冲突时先复核再行动）"
    )
    return header + "\n" + "\n".join(f"- {f}" for f in facts)


def collect_and_format(
    main_ws: str | None,
    target_worktree: str | None = None,
    target_label: str | None = None,
) -> str:
    """便捷入口：采集 + 渲染自动核验块。失败返回空串。"""
    try:
        return format_facts_block(
            collect_dispatch_facts(main_ws, target_worktree, target_label),
            auto=True,
        )
    except Exception as e:
        log.debug("dispatch_facts_collect_failed", error=str(e))
        return ""
