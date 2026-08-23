"""拒绝方言 + 提示限频单测（spec §5.6 + §12.1）。跨平台。"""

from __future__ import annotations

from hiveweave.services.acl_sandbox.service import (
    REJECTION_DIALECT,
    _HINT_EVERY_N_ROUNDS,
    _hint_counts,
    _maybe_append_rejection_hint,
    is_rejection,
)


def test_dialect_hit_on_denied() -> None:
    assert is_rejection("Access is denied", 1) is True
    assert is_rejection("Access to the path 'D:\\x' is denied", 1) is True
    assert is_rejection("Permission denied", 1) is True


def test_dialect_miss() -> None:
    assert is_rejection("command not found", 1) is False
    assert is_rejection("Access is denied", 0) is False   # 零退出不算
    assert is_rejection("Access is denied", None) is False
    assert is_rejection("", 1) is False                    # 空 stderr 不算


def test_dialect_matches_constant() -> None:
    assert "Access is denied" in REJECTION_DIALECT
    assert "Access to the path" in REJECTION_DIALECT
    assert "Permission denied" in REJECTION_DIALECT


def test_hint_rate_limited_every_n_rounds(monkeypatch) -> None:
    """限频：每 N 轮 hit 才追加一次提示（per agent）。"""
    fresh: dict[str, int] = {}
    monkeypatch.setattr("hiveweave.services.acl_sandbox.service._hint_counts", fresh)

    base = {"exit_code": 1, "stderr": "Access is denied"}
    for i in range(1, _HINT_EVERY_N_ROUNDS * 2 + 1):
        r = _maybe_append_rejection_hint("A001", "worktree-A", dict(base))
        if i == 1 or (i - 1) % _HINT_EVERY_N_ROUNDS == 0:
            assert "[沙箱提示]" in r["stderr"], f"round {i} 应追加"
        else:
            assert "[沙箱提示]" not in r["stderr"], f"round {i} 不应追加"


def test_hint_not_appended_on_non_rejection(monkeypatch) -> None:
    fresh: dict[str, int] = {}
    monkeypatch.setattr("hiveweave.services.acl_sandbox.service._hint_counts", fresh)
    r = _maybe_append_rejection_hint(
        "A001", "worktree-A", {"exit_code": 0, "stderr": "ok"})
    assert "[沙箱提示]" not in r["stderr"]


def test_hint_boundary_name_rendered(monkeypatch) -> None:
    fresh: dict[str, int] = {}
    monkeypatch.setattr("hiveweave.services.acl_sandbox.service._hint_counts", fresh)
    r = _maybe_append_rejection_hint(
        "A001", "project-root",
        {"exit_code": 1, "stderr": "Permission denied"})
    assert "worktree" in r["stderr"] or "project" in r["stderr"]
