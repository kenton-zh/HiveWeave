"""Guard: pass-path game_run_case must expose structured metrics to the model.

report TEST_DSH_54 Layer 6 #5（2026-09-12）: QA had to bypass the tool with
``browse eval __HW_TEST__.run(caseId)`` because the *pass* path only put
scalars (``simulatedMs``) into the model-visible text while ``metrics`` /
``detail`` stayed in ``extra`` and got stripped by the provider whitelist.
The fail path meanwhile embedded the whole result JSON (verbose).

These tests lock the fix: ``detail`` + ``metrics`` are surfaced verbatim on
BOTH paths, with the SAME format, so the pass/fail asymmetry is gone.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import hiveweave.tools.game_qa_tools  # noqa: F401
from hiveweave.tools.game_qa_tools import GameRunCaseParams, game_run_case_tool


def _patch_stack(run_result: dict, tmp_path: Path):
    run_payload = json.dumps(run_result)

    async def fake_exec(argv, workspace, timeout_sec=60, agent_id=None):
        head = argv[0] if argv else ""
        if head == "js":
            return 0, run_payload, ""
        if head == "screenshot":
            out = argv[-1]
            p = Path(workspace) / out
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
            return 0, f"saved {out}", ""
        return 1, "", f"unexpected {argv}"

    return (
        patch(
            "hiveweave.tools.game_qa_tools.resolve_browse_bin",
            return_value=Path("fake-browse"),
        ),
        patch(
            "hiveweave.tools.game_qa_tools.browse_exec",
            new=AsyncMock(side_effect=fake_exec),
        ),
        patch(
            "hiveweave.tools.game_qa_tools.issue_browse_e2e_attestation",
            new=AsyncMock(return_value=""),
        ),
    )


async def _run_case(run_result: dict, tmp_path: Path):
    p1, p2, p3 = _patch_stack(run_result, tmp_path)
    with p1, p2, p3:
        return await game_run_case_tool(
            GameRunCaseParams(action="run", caseId=run_result.get("id", "c1")),
            agent_id="a1",
            workspace=str(tmp_path),
        )


def test_pass_path_exposes_metrics_and_detail(tmp_path: Path):
    """PASS 用例：metrics/detail 必须出现在模型可见文本里（逐字）。"""
    metrics = {"slopeMax": 0.123, "lanes": [1, 2, 3], "ok": True}
    result = {
        "id": "ac3-pad",
        "codePass": True,
        "codeErrors": [],
        "detail": "flatness within tolerance",
        "visionCriteria": "castle platform flat",
        "screenshotHint": "canvas",
        "simulatedMs": 1000,
        "metrics": metrics,
    }
    r = asyncio.run(_run_case(result, tmp_path))
    assert r.success
    assert "CASE codePass=true" in r.output
    assert f"detail={result['detail']}" in r.output
    assert f"metrics={json.dumps(metrics, ensure_ascii=False)}" in r.output
    # 关键内容必须真的可读（防「字段名在、值被吞」）
    assert "slopeMax" in r.output and "0.123" in r.output


def test_fail_path_exposes_metrics_same_format(tmp_path: Path):
    """FAIL 用例走同一格式：不再整段 dump result，但 metrics/detail 仍透出。"""
    metrics = {"slopeMax": 0.9}
    result = {
        "id": "ac3-grade",
        "codePass": False,
        "codeErrors": ["rampMaxJump 0.72 > 0.05"],
        "detail": "ramp too steep",
        "visionCriteria": "ramp slope gentle",
        "simulatedMs": 500,
        "metrics": metrics,
    }
    r = asyncio.run(_run_case(result, tmp_path))
    assert r.success
    assert "CASE FAIL (code gate)" in r.output
    assert f"detail={result['detail']}" in r.output
    assert f"metrics={json.dumps(metrics, ensure_ascii=False)}" in r.output
    # 对称性：fail 不再嵌整段 result JSON（消除「失败话多」）
    assert f"result={json.dumps(result, ensure_ascii=False)}" not in r.output


def test_no_metrics_key_emits_no_metrics_line(tmp_path: Path):
    """反向对照：结果里**没有** metrics 键时，绝不打印 ``metrics=``。

    唯一判据选择：该 result 的所有字符串值（id/detail/visionCriteria/
    codeErrors）都不含子串 ``metrics``，输出里的 ``metrics=`` 只可能来自
    ``_structured_harness_lines`` 的无条件输出 —— 因此本断言不会被他串短路。
    同时断言 ``CASE codePass=true`` 命中了正确分支，排除「空输出恒绿」。
    """
    result = {
        "id": "plain-case",
        "codePass": True,
        "codeErrors": [],
        "detail": "nothing to measure",
        "visionCriteria": "looks fine",
        "screenshotHint": "canvas",
        "simulatedMs": 250,
        # 故意不提供 metrics 键
    }
    r = asyncio.run(_run_case(result, tmp_path))
    assert r.success
    assert "CASE codePass=true" in r.output  # 分支确已执行
    assert "metrics=" not in r.output
