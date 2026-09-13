"""#4 主因守卫（2026-09-13 TEST_DSH_55 报告 §3 第 4 条）。

现象：`_is_canonical_worktree_read` 只认 `.hiveweave/worktrees/A050/…` 形态的
路径；被 rehome / 改挂过的树带 `-b` / `-c` / `-d` 后缀（`constants.
_RELOCATION_SUFFIXES`），于是 `A051-b/docs/DESIGN.md` 被**判成非规范路径**，
跨树读被拒 —— 实测 16 步（离线复现的拒绝提示与生产逐字一致，见 issue-4 §三）。

修法：后缀**从常量生成**（`re.escape` 拼接），与 `constants` 同源，
避免两处硬编码各自漂移。
"""
from __future__ import annotations


def test_relocated_worktree_suffixes_are_canonical_reads():
    """`-b/-c/-d` 树必须算「规范跨树读路径」，且 suffix 清单与常量同源。

    阳性对照：把 `file.py` 的正则改回只认 `[A-Za-z]\\d{2,}(/.*)?$`（不容纳
    后缀）→ 本用例在第 1 个 suffix 处转红。
    """
    from hiveweave.services.git_worktree.constants import _RELOCATION_SUFFIXES
    from hiveweave.tools.file import _is_canonical_worktree_read

    # 既有行为不变
    assert _is_canonical_worktree_read(
        ".hiveweave/worktrees/A050/docs/DESIGN.md"
    )
    # 新行为：每个改挂后缀都要认（后缀来自常量 ⇒ 常量加了值这里自动覆盖）
    assert _RELOCATION_SUFFIXES, "suffix 清单不应为空"
    for suf in _RELOCATION_SUFFIXES:
        p = f".hiveweave/worktrees/A051{suf}/docs/DESIGN.md"
        assert _is_canonical_worktree_read(p), (
            f"{p} 应算规范跨树读路径 —— 否则该树的契约/证据读不到（§3 #4）"
        )


def test_relaxed_suffixes_do_not_leak_quarantine_or_normal_paths():
    """反向守卫：放宽后缀**不得**顺手放行隔离区与普通相对路径。

    这是本次改动最主要的风险面 —— `_quarantine` 是平台自管的隔离区，
    若被算作「规范读路径」就等于把隔离敞开了。
    """
    from hiveweave.tools.file import _is_canonical_worktree_read

    assert not _is_canonical_worktree_read(
        ".hiveweave/worktrees/_quarantine/x"
    ), "隔离区不得被放行"
    assert not _is_canonical_worktree_read("docs/DESIGN.md")
    assert not _is_canonical_worktree_read(".hiveweave/shared/x")
    assert not _is_canonical_worktree_read("")
