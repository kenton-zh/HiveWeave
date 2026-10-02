"""T17：审计器退化输出 sanity 校验 + 缓存失效路径。

病灶（S16 霜叶 88.8KB diff 实测）：审计 LLM 偶发输出思维链/开场白 ⇒
「审计行数: 0 / 问题数: 100 / 第 1 条 finding = 开场白 / unparsed_ratio=1.0」
的退化结论被判 ISSUES 落地，还按 diff_hash 永久缓存（同 diff 重审 6 次原样
回放）⇒ submit 门结构性无解。

覆盖：
- 纯函数规则边界（少 findings 不拦——severity-failsafe / T2 合法单 finding
  形态回归；env 可调；kill-switch）；
- 三形态退化输出（0 行+假 findings / 思维链泄漏 / unparsed 超阈）各一例
  ⇒ llm_failed（走软失败自动重试）且不写审计缓存、不发凭证；
- 正常 ISSUES ⇒ 照常落地（凭证 exit_code=1）+ 照常入缓存；
- 缓存失效路径：退化缓存行在读点被判「不信」⇒ 强制重跑，重跑成功后覆盖
  坏行（同 diff 下次审计不再回放坏结论）。

DB 环境 fixture 照 test_code_audit_severity_failsafe.py（复用
test_audit_epic_fixes 的 env / 打桩）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import structlog.testing

from hiveweave.services.attestation import attestation_service, hash_stdout

# `env` 是 fixture，导入到本模块即生效；其余是打桩与常量。
from tests.test_audit_epic_fixes import (  # noqa: F401
    AGENT_ID,
    DIFF_TEXT,
    PROJECT_ID,
    _fake_git,
    _run_audit_patches,
    env as audit_env,  # —— fixture（改名避免与测试参数同名触发 F811）
)


# ── 公共工具 ─────────────────────────────────────────────────────


def _findings(n: int, template: str = "src/f{i}.py:{i} [high] issue {i}") -> list[str]:
    """n 条**格式合规**的 finding 行（带 [high]，非思维链）。"""
    return [template.format(i=i + 1) for i in range(n)]


async def _run_audit(llm_text: str, *, ledger_lines: int = 0):
    """真凭证服务 + 真缓存表的 run_code_audit（审计缓存写点被留痕）。

    返回 (result, cache_store_calls, last_attempt)。打桩面与 epic 用例一致
    （worktree/git/报告文件），audit_cache_store 用 spy 包裹照常落库 ⇒ 既可
    断言「写没写」，又不影响后续缓存命中路径。last_attempt 在收尾台账重置
    **前**抓取（reset_ledger 会一并清掉失败尝试记录）。
    """
    from hiveweave.services import code_audit as ca

    ca.reset_ledger(AGENT_ID)
    if ledger_lines:
        ca.record_change(AGENT_ID, ledger_lines)
    store_calls: list[dict] = []
    real_store = attestation_service.audit_cache_store

    async def _store_spy(project_id, **kwargs):
        store_calls.append(kwargs)
        return await real_store(project_id, **kwargs)

    p_wt, p_git, p_save = _run_audit_patches()
    with (
        p_wt,
        p_git,
        p_save,
        patch.object(attestation_service, "audit_cache_store", new=_store_spy),
    ):
        result = await ca.run_code_audit(
            PROJECT_ID, AGENT_ID, task_id="task-t17",
            call_llm=AsyncMock(return_value=llm_text),
        )
    last_attempt = ca.get_last_audit_attempt(AGENT_ID)
    ca.reset_ledger(AGENT_ID)
    return result, store_calls, last_attempt


async def _current_diff_hash() -> str:
    """与 run_code_audit 同源计算当前打桩 diff 的哈希（缓存播种用）。"""
    from hiveweave.services.code_audit import collect_worktree_diff

    with patch("hiveweave.services.git_worktree._git", new=_fake_git()):
        res = await collect_worktree_diff(r"C:\fake\wt")
    return hash_stdout(res.text)


# ── 1. 纯函数：detect_audit_output_degeneracy 规则边界 ────────────


def test_pass_verdict_never_degenerate():
    """PASS 永不判退化：首行合规即强信号（T2 的 PASS+叙述行形态）。"""
    from hiveweave.services.code_audit import detect_audit_output_degeneracy

    assert detect_audit_output_degeneracy(
        "PASS", ["Let me explain why this passes.", "all good within first 50KB"]
    ) == []


def test_single_unparsed_issue_with_zero_lines_not_flagged():
    """回归护栏：单条 unparsed + 台账 0 **不**判退化。

    这正是 severity-failsafe（#12）与 T2 的合法形态——少 findings 已有
    fail-safe 逐条兜（视为 high 拦门），sanity 不得再把它打成 llm_failed。
    """
    from hiveweave.services.code_audit import detect_audit_output_degeneracy

    assert detect_audit_output_degeneracy(
        "ISSUES", ["src/a.py:1 【high】未校验用户输入"], lines_audited=0
    ) == []


def test_rule1_zero_lines_with_fake_findings():
    """规则①：审计行数=0 却报出一批「假 findings」⇒ 矛盾签名（S16 形态）。"""
    from hiveweave.services.code_audit import detect_audit_output_degeneracy

    rules = detect_audit_output_degeneracy(
        "ISSUES", _findings(12), lines_audited=0
    )
    assert rules == ["zero_lines_with_issues"]
    # 台账正常时同输出不拦（规则①只在 0 行时评估）
    assert detect_audit_output_degeneracy(
        "ISSUES", _findings(12), lines_audited=25
    ) == []
    # 缓存行（lines_audited=None）不评估规则①
    assert detect_audit_output_degeneracy("ISSUES", _findings(12)) == []


def test_rule2_first_finding_cot_opening():
    """规则②：第 1 条 finding 是开场白 ⇒ 退化（S16 的直接签名）。"""
    from hiveweave.services.code_audit import detect_audit_output_degeneracy

    issues = ["Let me analyze this diff carefully."] + _findings(10)
    assert detect_audit_output_degeneracy("ISSUES", issues) == [
        "first_finding_cot_opening"
    ]
    # 开场白不在首条、且只混入 1 行 ⇒ 不整份作废（容忍度设计）
    mid = _findings(10)
    mid.insert(3, "Sure, here is my analysis of the changes.")
    assert detect_audit_output_degeneracy("ISSUES", mid) == []


def test_rule2_cot_flood():
    """规则②洪水：≥2 条思维链行 ⇒ 整份是思维链，不是 findings。"""
    from hiveweave.services.code_audit import detect_audit_output_degeneracy

    issues = [
        "src/f1.py:1 [high] issue",
        "I'll start by examining each file.",
        "Let me check the next part too.",
    ]
    assert detect_audit_output_degeneracy("ISSUES", issues) == ["cot_flood"]


def test_rule3_unparsed_flood_over_threshold():
    """规则③：条数足额 + unparsed 占比 ≥0.9 ⇒ 输出不在契约格式里。"""
    from hiveweave.services.code_audit import detect_audit_output_degeneracy

    flood = [f"src/a.py:{i + 1} looks incorrect in this hunk" for i in range(12)]
    assert detect_audit_output_degeneracy("ISSUES", flood, lines_audited=25) == [
        "unparsed_ratio_high"
    ]
    # 占比低于阈值（12 条里 2 条带档位）⇒ 不拦（unparsed 行仍有 fail-safe 兜）
    mixed = flood[:10] + _findings(2)
    assert detect_audit_output_degeneracy("ISSUES", mixed, lines_audited=25) == []


def test_env_overrides_thresholds(monkeypatch):
    """校验参数 env 可调（阈值/门槛/总开关）。"""
    from hiveweave.services.code_audit import detect_audit_output_degeneracy

    # min_findings：默认 10 → 6 条假 findings 不拦；调到 5 → 拦
    assert detect_audit_output_degeneracy(
        "ISSUES", _findings(6), lines_audited=0
    ) == []
    monkeypatch.setenv("HIVEWEAVE_AUDIT_DEGEN_MIN_FINDINGS", "5")
    assert detect_audit_output_degeneracy(
        "ISSUES", _findings(6), lines_audited=0
    ) == ["zero_lines_with_issues"]
    monkeypatch.delenv("HIVEWEAVE_AUDIT_DEGEN_MIN_FINDINGS")

    # unparsed 阈值：默认 0.9 → 6/10 不拦；调到 0.5 → 拦
    mixed = [f"src/a.py:{i + 1} vague remark" for i in range(6)] + _findings(4)
    assert detect_audit_output_degeneracy("ISSUES", mixed, lines_audited=25) == []
    monkeypatch.setenv("HIVEWEAVE_AUDIT_DEGEN_UNPARSED_RATIO", "0.5")
    assert detect_audit_output_degeneracy(
        "ISSUES", mixed, lines_audited=25
    ) == ["unparsed_ratio_high"]
    monkeypatch.delenv("HIVEWEAVE_AUDIT_DEGEN_UNPARSED_RATIO")

    # kill-switch：整体关闭 ⇒ 恒不拦
    monkeypatch.setenv("HIVEWEAVE_AUDIT_SANITY_ENABLED", "0")
    assert detect_audit_output_degeneracy(
        "ISSUES", ["Let me analyze this diff carefully."], lines_audited=0
    ) == []


# ── 2. 端到端：三形态退化输出 ⇒ llm_failed 且不写缓存 ─────────────


@pytest.mark.asyncio
async def test_e2e_zero_lines_fake_findings_llm_failed_not_cached(audit_env):  # noqa: F811
    """形态①（0 行 + 假 findings）⇒ llm_failed、自动重试入队、无凭证、不缓存。"""
    llm_text = "VERDICT: ISSUES\n" + "\n".join(
        f"- {line}" for line in _findings(12)
    )
    result, store_calls, last_attempt = await _run_audit(llm_text, ledger_lines=0)

    assert result["audited"] is False
    assert result["reason"] == "llm_failed"
    assert result["degenerate_rules"] == ["zero_lines_with_issues"]
    assert "attestation_id" not in result, "退化结论不得发凭证"
    assert store_calls == [], "退化结论不得写入审计缓存"
    # 走既有软失败路径：失败尝试被记录 + 自动重试已入队
    assert (last_attempt or {}).get("reason") == "llm_failed"
    assert result.get("retry_queued") is True
    assert result.get("retry_exhausted") is False


@pytest.mark.asyncio
async def test_e2e_cot_leakage_llm_failed_not_cached(audit_env):  # noqa: F811
    """形态②（思维链泄漏）⇒ llm_failed 且留 audit_degenerate_detected 结构化日志。"""
    llm_text = (
        "VERDICT: ISSUES\n"
        "Let me analyze this diff carefully.\n"
        "I'll start by examining each file.\n"
        "Then I will summarize what needs fixing.\n"
    )
    with structlog.testing.capture_logs() as logs:
        result, store_calls, _ = await _run_audit(llm_text, ledger_lines=50)

    assert result["audited"] is False
    assert result["reason"] == "llm_failed"
    assert result["degenerate_rules"] == [
        "first_finding_cot_opening",
        "cot_flood",
    ]
    assert store_calls == []
    events = [e for e in logs if e.get("event") == "audit_degenerate_detected"]
    assert events, f"缺结构化日志：{[e.get('event') for e in logs]}"
    assert events[0]["source"] == "fresh"
    assert "first_finding_cot_opening" in events[0]["rules"]


@pytest.mark.asyncio
async def test_e2e_unparsed_flood_llm_failed_not_cached(audit_env):  # noqa: F811
    """形态③（unparsed 超阈洪水）⇒ llm_failed 且不写缓存。"""
    llm_text = "VERDICT: ISSUES\n" + "\n".join(
        f"src/a.py:{i + 1} looks incorrect in this hunk" for i in range(12)
    )
    result, store_calls, _ = await _run_audit(llm_text, ledger_lines=50)

    assert result["audited"] is False
    assert result["reason"] == "llm_failed"
    assert result["degenerate_rules"] == ["unparsed_ratio_high"]
    assert store_calls == []


# ── 3. 正常 ISSUES：照常落地 + 照常缓存（不回退既有语义） ──────────


@pytest.mark.asyncio
async def test_normal_issues_still_lands_and_caches(audit_env):  # noqa: F811
    """正常 ISSUES 不受 sanity 影响：凭证 exit_code=1 + 缓存照写 + 命中回放。"""
    llm_text = (
        "VERDICT: ISSUES\n"
        "- x.py:12 [high] crash on empty input\n"
        "- y.py:3 [low] style\n"
    )
    first, store_calls, _ = await _run_audit(llm_text, ledger_lines=30)

    assert first["audited"] is True
    assert first["verdict"] == "ISSUES"
    assert first["issues_count"] == 2
    assert len(store_calls) == 1, "正常 ISSUES 必须照常入缓存"
    assert store_calls[0]["verdict"] == "ISSUES"
    assert int(store_calls[0]["exit_code"]) == 1
    assert len(store_calls[0]["top_issues"]) == 2

    # 同 diff 第二次 → 缓存命中回放（不烧 LLM）
    second, store_calls2, _ = await _run_audit("SHOULD NOT BE CALLED", ledger_lines=7)
    assert second["audited"] is True
    assert second["verdict"] == "ISSUES"
    assert second["issues_count"] == 2
    assert second["cached_from_attestation_id"] == first["attestation_id"]
    assert store_calls2 == [], "缓存命中不重写缓存"


# ── 4. 缓存失效路径：退化缓存行「不信」⇒ 强制重跑 + 覆盖坏行 ──────


@pytest.mark.asyncio
async def test_degenerate_cache_row_distrusted_and_rerun_overwrites(audit_env):  # noqa: F811
    """S16 病灶闭环：退化缓存行不再被回放——本次重跑成功且坏行被覆盖。"""
    bad_issues = ["Let me analyze this diff carefully."] + [
        "The implementation might have issues worth checking further."
        for _ in range(9)
    ]
    dh = await _current_diff_hash()
    await attestation_service.audit_cache_store(
        PROJECT_ID,
        agent_id=AGENT_ID,
        diff_hash=dh,
        verdict="ISSUES",
        exit_code=1,
        attestation_id="att-bad-src",
        top_issues=bad_issues,
        source_attestation_id="att-bad-src",
    )

    good_llm = AsyncMock(
        return_value="VERDICT: ISSUES\n- x.py:12 [high] real defect\n"
    )
    from hiveweave.services import code_audit as ca
    from hiveweave.services.code_audit import record_change, reset_ledger, run_code_audit

    reset_ledger(AGENT_ID)
    record_change(AGENT_ID, 30)  # 台账正常 ⇒ 规则①不参与，纯验读点不信
    p_wt, p_git, p_save = _run_audit_patches()
    with p_wt, p_git, p_save, structlog.testing.capture_logs() as logs:
        result = await run_code_audit(
            PROJECT_ID, AGENT_ID, task_id="task-t17", call_llm=good_llm
        )
    reset_ledger(AGENT_ID)

    assert result["audited"] is True, "缓存被不信后必须真重跑"
    assert result["verdict"] == "ISSUES"
    assert result.get("cached_from_attestation_id") is None, "不是回放"
    assert good_llm.await_count == 1, "「不信」必须烧一次真实 LLM"
    degen = [
        e for e in logs
        if e.get("event") == "audit_degenerate_detected"
        and e.get("source") == "audit_cache"
    ]
    assert degen, f"缺缓存读点退化日志：{[e.get('event') for e in logs]}"
    assert "first_finding_cot_opening" in degen[0]["rules"]

    # 坏行已被重跑结论覆盖（INSERT OR REPLACE）
    row = await attestation_service.audit_cache_lookup(
        PROJECT_ID, agent_id=AGENT_ID, diff_hash=dh
    )
    assert row is not None
    assert row["attestation_id"] == result["attestation_id"]
    assert "Let me" not in str(row["top_issues"])

    # 之后的同 diff 审计回到正常缓存命中（回放新结论，不再打退化日志）
    replay_llm = AsyncMock(return_value="SHOULD NOT BE CALLED")
    ca.reset_ledger(AGENT_ID)
    ca.record_change(AGENT_ID, 5)
    p_wt2, p_git2, p_save2 = _run_audit_patches()
    with p_wt2, p_git2, p_save2, structlog.testing.capture_logs() as logs2:
        replay = await ca.run_code_audit(
            PROJECT_ID, AGENT_ID, task_id="task-t17", call_llm=replay_llm
        )
    ca.reset_ledger(AGENT_ID)
    assert replay["audited"] is True
    assert replay["cached_from_attestation_id"] == result["attestation_id"]
    assert not [
        e for e in logs2 if e.get("event") == "audit_degenerate_detected"
    ]
