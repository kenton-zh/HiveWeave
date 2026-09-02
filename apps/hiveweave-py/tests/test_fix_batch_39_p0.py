"""批次 39-处置1 单元测试：P0-2 merge 零差异 no-op + P0-3 审计回滚标注。

P0-2（39 审计）：零差异 merge（branch 已合入 main，branch_files 与 diff-tree -m
双空）此前被 fail-closed 报「Merge aborted」——但 merge commit 已落地，出现
「重试→No-op→再重试」×3（178K tok）。修复：报失败前查 is-ancestor，已合入 →
no-op 成功（merged_already 事实位）；未合入 → 保留 fail-closed。

P0-3（39 审计）：空 diff 的 auto-PASS 会把同任务上一轮 ISSUES 结论 4min 内翻转
成 PASS（新凭证顶掉旧凭证）。修复：上一轮 ISSUES 在案时改发「回滚标注」
（verdict=ROLLED_BACK，不发 PASS 凭证）；无 ISSUES 前科保留原 auto-PASS。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.services.git_worktree import conflict_markers as cm


# ── P0-2：_reject_if_markers_landed 零差异 no-op ─────────────────────────


def _git_stub(diff_tree_out: str, ancestor_ok: bool):
    """_git 打桩：diff-tree 返回 diff_tree_out；is-ancestor 按 ancestor_ok。"""

    calls: list[list] = []

    async def fake_git(args, workspace_path):
        calls.append(list(args))
        if args[:2] == ["diff-tree"]:
            return True, diff_tree_out
        if args[:2] == ["merge-base", "--is-ancestor"]:
            return (True, "") if ancestor_ok else (False, "")
        return False, ""

    fake_git.calls = calls
    return fake_git


@pytest.mark.asyncio
async def test_zero_diff_already_merged_returns_noop_success():
    """零差异 + 分支已是 target 祖先 → no-op 成功（不再是 Merge aborted）。"""
    with patch.object(cm, "_git", new=_git_stub("", ancestor_ok=True)), \
         patch.object(cm, "scan_conflict_markers", new=lambda *a, **k: []):
        result = await cm._reject_if_markers_landed(
            "ws", short_id="a343", branch="hw/x/t-2c7298cc",
            target_branch="main", branch_files=[],
        )
    assert result is not None  # 不再返回 None（None = 无需拒收，但这里显式给 no-op 成功）
    assert result["success"] is True
    assert result["already_up_to_date"] is True
    assert result["merged_already"] is True
    assert result["reason"] == "no_op_zero_diff"


@pytest.mark.asyncio
async def test_zero_diff_not_merged_keeps_fail_closed():
    """零差异但分支未合入（异常态）→ 保留 fail-closed abort。"""
    with patch.object(cm, "_git", new=_git_stub("", ancestor_ok=False)):
        result = await cm._reject_if_markers_landed(
            "ws", short_id="a343", branch="hw/x/t-2c7298cc",
            target_branch="main", branch_files=[],
        )
    assert result is not None
    assert result["success"] is False
    assert result["reason"] == "conflict_scan_unscoped"


@pytest.mark.asyncio
async def test_branch_files_present_still_scans_markers():
    """branch_files 非空 → 原扫描路径不受影响（无 marker 返回 None 放行）。"""
    with patch.object(cm, "_git", new=_git_stub("", ancestor_ok=False)) as git_stub, \
         patch.object(cm, "scan_conflict_markers", new=lambda *a, **k: []):
        result = await cm._reject_if_markers_landed(
            "ws", short_id="a343", branch="hw/x/t-2c7298cc",
            target_branch="main", branch_files=["app/main.py"],
        )
    assert result is None  # None = 无 marker，放行
    # 分支文件非空时不应走 diff-tree / is-ancestor 分支
    git_stub.calls == []


# ── P0-3：code_audit 空 diff 回滚标注 ────────────────────────────────────


def _audit_env(tmp_path, prior_exit_code: int | None, prior_task: str | None):
    """打桩 run_code_audit 的环境：worktree 存在、diff 为空、attestation 可查。"""
    ws = tmp_path / "wt"
    ws.mkdir()

    prior_row = (
        {"exit_code": prior_exit_code, "task_id": prior_task}
        if prior_exit_code is not None
        else None
    )

    async def fake_find_latest(project_id, *, agent_id, kind, max_age_ms=None):
        return prior_row

    async def fake_collect(worktree):
        return ""  # 空 diff

    created: list[dict] = []

    class _FakeAttestation:
        async def create(self, project_id, **kwargs):
            created.append(kwargs)
            return "att-1"

        def __getattr__(self, name):
            return AsyncMock()

    async def fake_create(project_id, **kwargs):
        created.append(kwargs)
        return "att-1"

    return ws, fake_find_latest, fake_collect, _FakeAttestation(), created


@pytest.mark.asyncio
async def test_empty_diff_after_issues_returns_rolled_back(tmp_path, monkeypatch):
    """上一轮 ISSUES（exit 1）在案 → 空 diff 改发 ROLLED_BACK，不发 PASS 凭证。"""
    from hiveweave.services import code_audit as ca

    tid = "35851a01-abcd-4321-9876-1234567890ab"
    ws, fake_find, fake_collect, fake_att, created = _audit_env(
        tmp_path, prior_exit_code=1, prior_task=tid
    )
    monkeypatch.setattr(
        "hiveweave.services.worktree_review.agent_worktree_path",
        AsyncMock(return_value=str(ws)),
    )
    monkeypatch.setattr(ca, "collect_worktree_diff", fake_collect)
    monkeypatch.setattr(
        "hiveweave.services.attestation.find_latest_attestation_by_kind",
        fake_find,
    )
    monkeypatch.setattr(
        "hiveweave.services.attestation.attestation_service", fake_att
    )
    monkeypatch.setattr(ca, "reset_ledger", lambda agent_id: None)

    result = await ca.run_code_audit(
        "proj", "agent-1", task_id=tid, call_llm=None, oneshot_llm=None
    )
    assert result["verdict"] == "ROLLED_BACK"
    assert result["auto_pass_reason"] == "empty_diff_after_issues"
    assert created == [], "不得发新 PASS 凭证（旧 ISSUES 凭证继续生效）"
    assert "回滚" in result["message"]


@pytest.mark.asyncio
async def test_empty_diff_without_prior_issues_keeps_auto_pass(tmp_path, monkeypatch):
    """无 ISSUES 前科 → 保留 TEST_DSH_32 P7 的 auto-PASS（优化不回退）。"""
    from hiveweave.services import code_audit as ca

    tid = "35851a01-abcd-4321-9876-1234567890ab"
    ws, fake_find, fake_collect, fake_att, created = _audit_env(
        tmp_path, prior_exit_code=None, prior_task=None
    )
    monkeypatch.setattr(
        "hiveweave.services.worktree_review.agent_worktree_path",
        AsyncMock(return_value=str(ws)),
    )
    monkeypatch.setattr(ca, "collect_worktree_diff", fake_collect)
    monkeypatch.setattr(
        "hiveweave.services.attestation.find_latest_attestation_by_kind",
        fake_find,
    )
    monkeypatch.setattr(
        "hiveweave.services.attestation.attestation_service", fake_att
    )
    monkeypatch.setattr(ca, "reset_ledger", lambda agent_id: None)

    result = await ca.run_code_audit(
        "proj", "agent-1", task_id=tid, call_llm=None, oneshot_llm=None
    )
    assert result["verdict"] == "PASS"
    assert result["auto_pass_reason"] == "empty_diff"
    assert len(created) == 1


@pytest.mark.asyncio
async def test_rolled_back_ignores_other_task_issues(tmp_path, monkeypatch):
    """别的前科不算——上一轮 ISSUES 必须是同一任务（task ref 匹配）。"""
    from hiveweave.services import code_audit as ca

    tid = "35851a01-abcd-4321-9876-1234567890ab"
    ws, fake_find, fake_collect, fake_att, created = _audit_env(
        tmp_path, prior_exit_code=1, prior_task="99999999-aaaa-bbbb-cccc-dddddddddddd"
    )
    monkeypatch.setattr(
        "hiveweave.services.worktree_review.agent_worktree_path",
        AsyncMock(return_value=str(ws)),
    )
    monkeypatch.setattr(ca, "collect_worktree_diff", fake_collect)
    monkeypatch.setattr(
        "hiveweave.services.attestation.find_latest_attestation_by_kind",
        fake_find,
    )
    monkeypatch.setattr(
        "hiveweave.services.attestation.attestation_service", fake_att
    )
    monkeypatch.setattr(ca, "reset_ledger", lambda agent_id: None)

    result = await ca.run_code_audit(
        "proj", "agent-1", task_id=tid, call_llm=None, oneshot_llm=None
    )
    assert result["verdict"] == "PASS"  # 别的任务的 ISSUES 不触发回滚标注
