"""P0-3 Stage 1：沙箱拒绝的**成因**（DeniedBy）—— 枚举 + 分层分类器 + 文案由成因驱动。

病灶（实测 55 库 / 53 行 `[沙箱提示]` / 33 可判定）：**19 行（57.6%）**的被拒路径
**就在提示自己印出的授权树之内**（全部落在 `.hiveweave\\reports|worktrees` 这类
平台 PROTECTED 面）—— 文案却一律说「目标在授权树之外，请 message_user 申请豁免」，
把 agent 指向一个**不存在的**边界问题（申请也没用）。

判据全部是**状态判据**（枚举值 / 结果字段 / 两处常量同一性），不做整句文案断言
（唯一涉及文案的两条断言用 `in` 判「某半句在不在」，且**必须**配合 `denied_by` 字段
一起看 —— 本仓铁律：文本判据不可单独作为验收）。
"""

from __future__ import annotations

import pytest

from hiveweave.services.acl_sandbox import service as S
from hiveweave.tools.fact_positions import (
    _rejection_dialect,
    classify_denied_by,
    extract_denied_paths,
    is_acl_rejection,
)
from hiveweave.tools.result import DENIED_BY_KINDS

ROOT = r"D:\PC_AI\Project\HiveTestProject\TEST_DSH_65"
DENIED = "Out-File: Access to the path '{}' is denied."
OUTSIDE = r"D:\tmp\check_design_console.txt"


# ── ① 枚举：闭合 4 格（§1 验收 4）────────────────────────────


def test_denied_by_kinds_is_exactly_four():
    assert set(DENIED_BY_KINDS) == {
        "outside_boundary",
        "sealed_git",
        "no_write_sid",
        "unknown_acl",
    }


# ── ② 分层分类器：状态层 → 证据层 → 不足不猜 ────────────────


@pytest.mark.parametrize(
    "name,stderr,exit_code,expected",
    [
        ("树外", DENIED.format(OUTSIDE), 1, "outside_boundary"),
        ("树内 src", DENIED.format(ROOT + r"\src\a.txt"), 1, "no_write_sid"),
        # ⭐ 实测样本形态：被拒路径是平台 PROTECTED 面，却被告知「越界」
        ("树内 .hiveweave/reports", DENIED.format(ROOT + r"\.hiveweave\reports\x.md"), 1, "no_write_sid"),
        ("树内 .hiveweave/worktrees", DENIED.format(ROOT + r"\.hiveweave\worktrees\A227\tests"), 1, "no_write_sid"),
        ("抽不到路径", "Access is denied.", 1, "unknown_acl"),
        ("内外混着", DENIED.format(OUTSIDE) + "\n" + DENIED.format(ROOT + r"\b.txt"), 1, "unknown_acl"),
    ],
)
def test_classify_denied_by(name, stderr, exit_code, expected):
    assert classify_denied_by(stderr, exit_code, boundary_root=ROOT) == expected, name


def test_non_rejection_returns_none():
    """**不是**拒绝 ⇒ None（不许硬贴成因）：方言未命中 / exit=0 / 空 stderr。"""
    assert classify_denied_by("Get-Content: Cannot find path 'X'.", 1, boundary_root=ROOT) is None
    assert classify_denied_by(DENIED.format(OUTSIDE), 0, boundary_root=ROOT) is None
    assert classify_denied_by("", 1, boundary_root=ROOT) is None
    assert classify_denied_by(DENIED.format(OUTSIDE), None, boundary_root=ROOT) is None


def test_sealed_git_only_when_explicitly_passed():
    """`sealed_git` 只在**显式传入封条集合**时判定（Stage 1 边界，写进 docstring）。"""
    p = ROOT + r"\.git\config"
    sealed = ["seal:" + p, "deny-dc-all:" + ROOT + r"\.git"]
    assert classify_denied_by(DENIED.format(p), 1, boundary_root=ROOT, sealed=sealed) == "sealed_git"
    # 不传 sealed ⇒ 落回「树内」而不是猜成封条
    assert classify_denied_by(DENIED.format(p), 1, boundary_root=ROOT) == "no_write_sid"


def test_missing_boundary_is_not_a_guess():
    """没给 boundary ⇒ 无从判内外 ⇒ unknown_acl（不猜成越界）。"""
    assert classify_denied_by(DENIED.format(OUTSIDE), 1) == "unknown_acl"


def test_extract_denied_paths_handles_quotes():
    assert extract_denied_paths(DENIED.format(OUTSIDE)) == [OUTSIDE]
    assert extract_denied_paths('Access to the path "D:\\tmp\\b.txt" is denied.') == ["D:\\tmp\\b.txt"]
    assert extract_denied_paths("no path here") == []


# ── ③ 方言表唯一源（两处不许各自演化）───────────────────────


def test_dialect_single_source():
    """`fact_positions` 惰性引用的方言表 == `service.REJECTION_DIALECT`（兜底不漂移）。"""
    assert set(_rejection_dialect()) == set(S.REJECTION_DIALECT)


def test_is_rejection_delegates_and_keeps_contract():
    """对外语义不变（既有测试 pin 了 `is_rejection("Access is denied", 1) is True`）。"""
    assert S.is_rejection("Access is denied", 1) is True
    assert S.is_rejection("Access is denied", 0) is False
    assert S.is_rejection("", 1) is False
    assert is_acl_rejection("Access to the path 'x' is denied", 3) is True


# ── ④ 接线：结果带 denied_by，文案按成因选（旧文案只在 outside 时出现）──


def _hint_for(stderr: str, *, agent_id: str) -> dict:
    S._hint_counts.pop(agent_id, None)
    res = S._maybe_append_rejection_hint(
        agent_id, ROOT, {"stderr": stderr, "exit_code": 1, "stdout": ""}
    )
    return res


def test_hint_is_cause_driven_not_always_outside():
    """⭐ 这组就是 57.6% 假越界的回归网：树内路径**不得**再收到「之外」文案。"""
    inside = _hint_for(DENIED.format(ROOT + r"\.hiveweave\reports\A236-m\notes.md"), agent_id="p03-inside")
    assert inside["denied_by"] == "no_write_sid"
    assert "在授权树（" in inside["stderr"] and "之外" not in inside["stderr"]
    assert "这不是越界" in inside["stderr"]

    outside = _hint_for(DENIED.format(OUTSIDE), agent_id="p03-outside")
    assert outside["denied_by"] == "outside_boundary"
    assert "在授权树（" in outside["stderr"] and "之外" in outside["stderr"]


def test_unknown_cause_gets_no_outside_claim():
    res = _hint_for("Access is denied.", agent_id="p03-unknown")
    assert res["denied_by"] == "unknown_acl"
    assert "之外" not in res["stderr"]


def test_non_rejection_not_stamped():
    res = _hint_for("Get-Content: Cannot find path 'X'.", agent_id="p03-none")
    assert "denied_by" not in res
    assert "[沙箱提示]" not in res["stderr"]


def test_hint_still_rate_limited_but_stamp_always_set():
    """限频只作用于**文案**；`denied_by` 每次都要落（否则统计口径会缺行）。"""
    aid = "p03-rate"
    S._hint_counts.pop(aid, None)
    seen = [
        S._maybe_append_rejection_hint(
            aid, ROOT, {"stderr": DENIED.format(OUTSIDE), "exit_code": 1}
        )
        for _ in range(3)
    ]
    assert all(r.get("denied_by") == "outside_boundary" for r in seen)
    hits = [("[沙箱提示]" in r["stderr"]) for r in seen]
    assert hits[0] is True and hits.count(True) == 1, hits


# ── ⑤ 审计 A1/A2/A5 的回归钉（随 fix commit 入库）────────────


def test_relative_path_is_not_judged_outside():
    """A1①：相对路径无从比边界 ⇒ 不得断言越界（曾误判 outside_boundary）。"""
    assert classify_denied_by(
        "Access to the path 'src\\a.txt' is denied.", 1, boundary_root=ROOT
    ) == "unknown_acl"


def test_denial_must_not_borrow_paths_from_other_lines():
    """A1②：拒绝语句不得与**另一行**的无关引号串拼成「那个文件被拒」。"""
    stderr = "Set-Content : Access is denied.\n+ Copy-Item 'D:\\tmp\\a.txt'\n"
    assert classify_denied_by(stderr, 1, boundary_root=ROOT) == "unknown_acl"


def test_sealed_accepts_bare_path_too():
    """A2：`sealed` 传**裸路径**也要生效（曾因 split(":",1) 被砍成 `\\x`）。"""
    path = ROOT + "\\.git\\config"
    assert (
        classify_denied_by(DENIED.format(path), 1, boundary_root=ROOT, sealed=[path])
        == "sealed_git"
    )
    assert (
        classify_denied_by(
            DENIED.format(path), 1, boundary_root=ROOT, sealed=["seal:" + path]
        )
        == "sealed_git"
    )


def test_drive_root_boundary_prefix_matches():
    """A5：边界是盘符根时，树内路径不得被判 out（曾因尾分隔符永不匹配）。"""
    assert (
        classify_denied_by(DENIED.format("D:\\a\\x.txt"), 1, boundary_root="D:\\")
        == "no_write_sid"
    )
