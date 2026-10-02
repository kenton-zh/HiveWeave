"""T2：50KB diff 静默截断 → 非阻塞标注（回执明示 + 凭证 PARTIAL COVERAGE）。

覆盖三处同源事实位（判定与门禁语义不变，只是覆盖范围诚实化）：
  1. ``collect_worktree_diff`` → ``WorktreeDiff.truncated``（>50KB / ≤50KB /
     untracked 预算耗尽）；
  2. 审计回执（PASS 与 ISSUES 都）带截断明示；≤50KB 不误标；
  3. 凭证 ``command_or_url`` 追加 PARTIAL COVERAGE 标注（正常路径 +
     缓存复用路径）+ ``run_steps.truncated`` 现成列透传位。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

HEAD_SHA = "a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0"

_SMALL_DIFF = "--- a/x.py\n+++ b/x.py\n+ok\n"
_OVER_50KB_DIFF = "--- a/big.py\n+++ b/big.py\n" + ("+x = 1\n" * 12000)


def _fake_git(diff_text: str):
    """Async fake for ``hiveweave.services.git_worktree._git``（base=main）。"""

    async def fake_git(args: list[str], cwd: str, timeout: float = 30.0):
        table = {
            ("rev-parse", "--verify", "refs/heads/main"): (True, ""),
            ("diff", "main...HEAD"): (True, diff_text),
            ("diff", "HEAD"): (True, ""),
            ("ls-files", "--others", "--exclude-standard"): (True, ""),
            ("rev-parse", "HEAD"): (True, HEAD_SHA),
        }
        return table.get(tuple(args), (False, ""))

    return fake_git


def _attestation_service(att_id: str = "att-1"):
    svc = MagicMock()
    svc.create = AsyncMock(return_value=att_id)
    return svc


# ── 1. collect_worktree_diff 截断标志 ───────────────────────────


@pytest.mark.asyncio
async def test_collect_over_50kb_flags_truncated():
    from hiveweave.services.code_audit import (
        _DIFF_CAP_BYTES,
        WorktreeDiff,
        collect_worktree_diff,
    )

    assert len(_OVER_50KB_DIFF.encode()) > _DIFF_CAP_BYTES
    with patch("hiveweave.services.git_worktree._git", new=_fake_git(_OVER_50KB_DIFF)):
        res = await collect_worktree_diff(r"C:\fake\wt")

    assert isinstance(res, WorktreeDiff)
    assert res.truncated is True
    assert "[diff truncated at 50KB]" in res.text
    # 帽语义不变：总输出仍 ≤ 50KB
    assert len(res.text.encode("utf-8", errors="replace")) <= _DIFF_CAP_BYTES


@pytest.mark.asyncio
async def test_collect_small_diff_not_flagged():
    from hiveweave.services.code_audit import collect_worktree_diff

    with patch("hiveweave.services.git_worktree._git", new=_fake_git(_SMALL_DIFF)):
        res = await collect_worktree_diff(r"C:\fake\wt")

    assert res.truncated is False
    assert "[diff truncated at 50KB]" not in res.text


@pytest.mark.asyncio
async def test_collect_untracked_budget_exhaustion_flags_truncated(tmp_path):
    """untracked 预算耗尽（后续文件整段跳过）⇒ 覆盖不全，必须标注。"""
    from hiveweave.services.code_audit import collect_worktree_diff

    (tmp_path / "a.txt").write_text("a" * 50000, encoding="utf-8")
    (tmp_path / "b.txt").write_text("b" * 20000, encoding="utf-8")
    (tmp_path / "c.txt").write_text("c", encoding="utf-8")

    async def fake_git(args: list[str], cwd: str, timeout: float = 30.0):
        table = {
            ("diff", "HEAD"): (True, _SMALL_DIFF),
            ("ls-files", "--others", "--exclude-standard"): (True, "a.txt\nb.txt\nc.txt"),
            ("rev-parse", "HEAD"): (True, HEAD_SHA),
        }
        return table.get(tuple(args), (False, ""))

    with patch("hiveweave.services.git_worktree._git", new=fake_git):
        res = await collect_worktree_diff(str(tmp_path))

    assert res.truncated is True
    assert "== untracked a.txt ==" in res.text
    assert "== untracked c.txt ==" not in res.text, "预算耗尽后 c.txt 被跳过"


# ── 2+3. run_code_audit 全链路：回执明示 + 凭证标注 ──────────────


async def _run_audit(llm_text: str):
    from hiveweave.services import code_audit as ca

    att_svc = _attestation_service()
    ca.reset_ledger("agent-t2")
    with (
        patch(
            "hiveweave.services.worktree_review.agent_worktree_path",
            new_callable=AsyncMock,
            return_value=r"C:\fake\wt",
        ),
        patch("hiveweave.services.git_worktree._git", new=_fake_git(_OVER_50KB_DIFF)),
        patch("hiveweave.services.attestation.attestation_service", att_svc),
        patch(
            "hiveweave.tools.executor.ToolExecutor._save_tool_output_file",
            return_value=r"C:\fake\wt\report.txt",
        ),
    ):
        result = await ca.run_code_audit(
            "proj", "agent-t2", call_llm=AsyncMock(return_value=llm_text)
        )
    ca.reset_ledger("agent-t2")
    return result, att_svc


@pytest.mark.asyncio
async def test_truncated_pass_receipt_and_attestation_annotated():
    """>50KB diff + PASS：回执明示 + 凭证 PARTIAL COVERAGE + 透传位。"""
    from hiveweave.services.code_audit import AUDIT_PARTIAL_COVERAGE_MARKER
    from hiveweave.tools.code_audit import _format_verdict

    result, att_svc = await _run_audit("VERDICT: PASS\nall good within first 50KB\n")

    assert result["audited"] is True
    assert result["verdict"] == "PASS"
    assert result["diff_truncated"] is True

    kw = att_svc.create.await_args.kwargs
    assert "[verdict=PASS]" in str(kw.get("command_or_url"))
    assert AUDIT_PARTIAL_COVERAGE_MARKER in str(kw.get("command_or_url"))
    assert kw["exit_code"] == 0  # 判定语义不变：截断不改 PASS

    receipt = _format_verdict(result)
    out = receipt.output or ""
    assert "仅覆盖前 50KB" in out
    assert "后续改动未审计" in out
    assert receipt.to_dict().get("truncated") is True  # run_steps 现成列透传


@pytest.mark.asyncio
async def test_truncated_issues_receipt_and_attestation_annotated():
    """>50KB diff + ISSUES：回执同样明示，凭证同样标注。"""
    from hiveweave.services.code_audit import AUDIT_PARTIAL_COVERAGE_MARKER
    from hiveweave.tools.code_audit import _format_verdict

    llm_text = "VERDICT: ISSUES\n- big.py:2 [high] overflow past cap\n"
    result, att_svc = await _run_audit(llm_text)

    assert result["audited"] is True
    assert result["verdict"] == "ISSUES"
    assert result["diff_truncated"] is True

    kw = att_svc.create.await_args.kwargs
    assert "[verdict=ISSUES]" in str(kw.get("command_or_url"))
    assert AUDIT_PARTIAL_COVERAGE_MARKER in str(kw.get("command_or_url"))
    assert kw["exit_code"] == 1

    out = _format_verdict(result).output or ""
    assert "仅覆盖前 50KB" in out


@pytest.mark.asyncio
async def test_small_diff_no_partial_annotation():
    """≤50KB：不误标——回执无明示、凭证无标注、透传位缺省。"""
    from hiveweave.services import code_audit as ca
    from hiveweave.tools.code_audit import _format_verdict

    att_svc = _attestation_service()
    ca.reset_ledger("agent-t2")
    with (
        patch(
            "hiveweave.services.worktree_review.agent_worktree_path",
            new_callable=AsyncMock,
            return_value=r"C:\fake\wt",
        ),
        patch("hiveweave.services.git_worktree._git", new=_fake_git(_SMALL_DIFF)),
        patch("hiveweave.services.attestation.attestation_service", att_svc),
        patch(
            "hiveweave.tools.executor.ToolExecutor._save_tool_output_file",
            return_value=r"C:\fake\wt\report.txt",
        ),
    ):
        result = await ca.run_code_audit(
            "proj", "agent-t2", call_llm=AsyncMock(return_value="VERDICT: PASS\nok\n")
        )
    ca.reset_ledger("agent-t2")

    assert result["audited"] is True
    assert result.get("diff_truncated") is False

    kw = att_svc.create.await_args.kwargs
    assert "PARTIAL COVERAGE" not in str(kw.get("command_or_url"))

    receipt = _format_verdict(result)
    out = receipt.output or ""
    assert "仅覆盖前 50KB" not in out
    assert "truncated" not in receipt.to_dict()  # 未截断不传键 ⇒ 列保持 NULL


# ── 缓存复用路径：截断 diff 的缓存结论同样标注 ────────────────────


class _CacheAttestationSvc:
    """audit_cache_lookup 命中的假凭证服务（其余出口走 AsyncMock 兜底）。"""

    def __init__(self, cached: dict | None):
        self.cached = cached
        self.created: list[dict] = []

    async def audit_cache_lookup(self, project_id, **kwargs):
        return self.cached

    async def audit_cache_store(self, project_id, **kwargs):
        pass

    async def create(self, project_id, **kwargs):
        self.created.append(kwargs)
        return "att-cache"

    def __getattr__(self, name):
        return AsyncMock()


@pytest.mark.asyncio
async def test_cached_reuse_of_truncated_diff_annotates_attestation():
    """缓存复用同一截断 diff：新凭证同样带 PARTIAL COVERAGE。"""
    from hiveweave.services import code_audit as ca
    from hiveweave.services.code_audit import AUDIT_PARTIAL_COVERAGE_MARKER
    from hiveweave.tools.code_audit import _format_verdict

    svc = _CacheAttestationSvc(
        cached={"exit_code": 0, "verdict": "PASS", "top_issues": "[]"}
    )
    ca.reset_ledger("agent-t2")
    with (
        patch(
            "hiveweave.services.worktree_review.agent_worktree_path",
            new_callable=AsyncMock,
            return_value=r"C:\fake\wt",
        ),
        patch("hiveweave.services.git_worktree._git", new=_fake_git(_OVER_50KB_DIFF)),
        patch("hiveweave.services.attestation.attestation_service", svc),
    ):
        result = await ca.run_code_audit("proj", "agent-t2")

    ca.reset_ledger("agent-t2")

    assert result["audited"] is True
    assert result["verdict"] == "PASS"
    assert result.get("cached_diff_hash")
    assert result["diff_truncated"] is True

    kw = svc.created[0]
    assert "[verdict=PASS] cached-reuse" in str(kw.get("command_or_url"))
    assert AUDIT_PARTIAL_COVERAGE_MARKER in str(kw.get("command_or_url"))

    out = _format_verdict(result).output or ""
    assert "仅覆盖前 50KB" in out
