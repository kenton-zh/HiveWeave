"""Unit tests for game_run_case / harness JSON parsing."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import hiveweave.tools.browse_tools  # noqa: F401
import hiveweave.tools.game_qa_tools  # noqa: F401

from hiveweave.services.permission import READONLY_TOOLS
from hiveweave.services.policy import TOOL_CAPABILITY, Capability
from hiveweave.tools.base import get_tool_def
from hiveweave.tools.executor import TOOL_PARAM_SCHEMAS
from hiveweave.tools.game_qa_tools import (
    GameRunCaseParams,
    _parse_json_blob,
    _safe_filename,
    game_run_case_tool,
)


def test_game_run_case_registered_and_permitted():
    assert "game_run_case" in READONLY_TOOLS
    assert "game_run_case" in TOOL_PARAM_SCHEMAS
    assert get_tool_def("game_run_case") is not None
    caps = TOOL_CAPABILITY["game_run_case"]
    assert Capability.BROWSE in caps
    assert Capability.BROWSER_ACCEPTANCE in caps


def test_parse_json_blob_variants():
    assert _parse_json_blob('{"hw": true, "cases": ["a"]}')["hw"] is True
    wrapped = '--- BEGIN UNTRUSTED EXTERNAL CONTENT ---\n{"a":1}\n--- END UNTRUSTED EXTERNAL CONTENT ---'
    assert _parse_json_blob(wrapped)["a"] == 1
    assert _parse_json_blob('"{\\"x\\": 2}"')["x"] == 2
    assert _parse_json_blob("noise {\"ok\": true} trailing")["ok"] is True
    assert _parse_json_blob("") is None


def test_safe_filename():
    assert _safe_filename("jump_cross_gap") == "jump_cross_gap"
    assert "/" not in _safe_filename("../evil id!!")


def test_probe_and_run_with_mocked_browse(tmp_path: Path):
    async def _run():
        probe_payload = json.dumps(
            {
                "hw": True,
                "version": "1.0",
                "cases": ["jump_cross_gap"],
                "render_game_to_text": "function",
                "advanceTime": "function",
            }
        )
        run_payload = json.dumps(
            {
                "id": "jump_cross_gap",
                "codePass": True,
                "codeErrors": [],
                "visionCriteria": "Player on right platform",
                "screenshotHint": "canvas",
                "simulatedMs": 1000,
            }
        )

        async def fake_exec(argv, workspace, timeout_sec=60):
            head = argv[0] if argv else ""
            if head == "js":
                expr = argv[1] if len(argv) > 1 else ""
                if ".run('" in expr:
                    return 0, run_payload, ""
                if "{cases: window.__HW_TEST__.list()}" in expr:
                    return 0, json.dumps({"cases": ["jump_cross_gap"]}), ""
                return 0, probe_payload, ""
            if head == "screenshot":
                out = argv[-1]
                p = Path(workspace) / out
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
                return 0, f"saved {out}", ""
            return 1, "", f"unexpected {argv}"

        with (
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
                new=AsyncMock(return_value="\n[attestation_id=t1 kind=browse_e2e]"),
            ),
        ):
            probe = await game_run_case_tool(
                GameRunCaseParams(action="probe"),
                agent_id="a1",
                workspace=str(tmp_path),
            )
            assert probe.success
            assert "tier=instrumented" in probe.output
            assert probe.extra.get("tier") == "instrumented"

            listed = await game_run_case_tool(
                GameRunCaseParams(action="list"),
                agent_id="a1",
                workspace=str(tmp_path),
            )
            assert listed.success
            assert listed.extra.get("cases") == ["jump_cross_gap"]

            ran = await game_run_case_tool(
                GameRunCaseParams(action="run", caseId="jump_cross_gap"),
                agent_id="a1",
                workspace=str(tmp_path),
            )
            assert ran.success
            assert ran.extra.get("code_pass") is True
            assert "assert_visual" in ran.output
            assert ran.extra.get("gate") == "pending_vision"

            failed = await game_run_case_tool(
                GameRunCaseParams(action="run"),
                agent_id="a1",
                workspace=str(tmp_path),
            )
            assert not failed.success

    asyncio.run(_run())


def test_probe_observe_only(tmp_path: Path):
    async def _run():
        payload = json.dumps(
            {
                "hw": False,
                "version": None,
                "cases": [],
                "render_game_to_text": "undefined",
                "advanceTime": "undefined",
            }
        )

        with (
            patch(
                "hiveweave.tools.game_qa_tools.resolve_browse_bin",
                return_value=Path("fake-browse"),
            ),
            patch(
                "hiveweave.tools.game_qa_tools.browse_exec",
                new=AsyncMock(return_value=(0, payload, "")),
            ),
            patch(
                "hiveweave.tools.game_qa_tools.issue_browse_e2e_attestation",
                new=AsyncMock(return_value=""),
            ),
        ):
            probe = await game_run_case_tool(
                GameRunCaseParams(action="probe"),
                agent_id="a1",
                workspace=str(tmp_path),
            )
            assert probe.success
            assert probe.extra.get("tier") == "observe-only"
            assert "gameplay pass" in probe.output.lower() or "observe-only" in probe.output

    asyncio.run(_run())
