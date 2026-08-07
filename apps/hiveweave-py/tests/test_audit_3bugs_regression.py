"""Regression tests for the 3 platform bugs found in the shoulabridge audit.

1. submit_task attestation short-id non-normalization — agents pass the
   8-char prefix shown in get_tasks; gates stored/compared it verbatim so
   every exact-match against the full UUID failed (submit gate 49% failure).
2. review_task evidence gate misattribution — the hint reported "mismatch"
   while the same row was MATCH, and short-id rows never satisfied the gate.
3. browse `evaluate`/`js` expression wrapping crash — the old code fed a
   tempfile *path string* to `js`, so `evaluate 1+1` returned the path.
   Now gstack-style argv is mapped to agent-browser (`_map_ab_argv`):
   inline expressions become `eval <src>`, existing files get their
   content read, oversized snippets go base64 / stdin.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


FULL_UUID = "1a2b3c4d-5e6f-7890-abcd-ef1234567890"
SHORT_PREFIX = FULL_UUID[:8]  # the 8-char UI short id agents actually pass
DASHED = FULL_UUID  # canonical form
NODASH = FULL_UUID.replace("-", "")  # same task, dashes stripped


# ── 1. submit_task short-id normalization ────────────────────────────────
def test_norm_task_ref_ignores_dashes_and_case():
    from hiveweave.services.attestation import _norm_task_ref

    assert _norm_task_ref(FULL_UUID) == _norm_task_ref(NODASH)
    assert _norm_task_ref(SHORT_PREFIX) == SHORT_PREFIX.lower()
    assert _norm_task_ref(NODASH) == FULL_UUID.lower().replace("-", "")


@pytest.mark.asyncio
async def test_canonical_task_id_resolves_short_prefix_to_full_uuid():
    from hiveweave.services.attestation import canonical_task_id

    with patch(
        "hiveweave.services.task.TaskService",
    ) as TS:
        TS.return_value.require_task_id = AsyncMock(return_value=FULL_UUID)
        resolved = await canonical_task_id("proj", SHORT_PREFIX)
    # canonical form is dash-stripped lowercase — equals the full id's norm
    assert resolved == FULL_UUID.lower().replace("-", "")


@pytest.mark.asyncio
async def test_task_ids_equal_treats_short_and_full_as_same():
    from hiveweave.services.attestation import _task_ids_equal

    with patch(
        "hiveweave.services.task.TaskService",
    ) as TS:
        TS.return_value.require_task_id = AsyncMock(return_value=FULL_UUID)
        eq = await _task_ids_equal("proj", SHORT_PREFIX, FULL_UUID)
    assert eq is True


# ── 2. review evidence gate hint no longer lies about MATCH ──────────────
def test_format_mismatch_hint_labels_shortid_as_match():
    from hiveweave.services.attestation import format_attestation_mismatch_hint

    held = [
        {
            "id": "att-1",
            "kind": "test_run",
            # legacy row: task_id stored as the 8-char prefix
            "task_id": SHORT_PREFIX,
        }
    ]
    text = format_attestation_mismatch_hint(
        held, target_task_id=FULL_UUID
    )
    assert "MATCH" in text
    assert "mismatch" not in text


def test_format_mismatch_hint_labels_unrelated_task_as_mismatch():
    from hiveweave.services.attestation import format_attestation_mismatch_hint

    held = [{"id": "att-2", "kind": "test_run", "task_id": FULL_UUID}]
    text = format_attestation_mismatch_hint(
        held, target_task_id="9f8e7d6c-5b4a-3210-fedc-ba9876543210"
    )
    assert "mismatch" in text


# ── 3. browse evaluate/js inline expression routing ──────────────────────
# New contract (agent-browser): _map_ab_argv translates gstack-style browse
# argv to the agent-browser CLI. js/eval/evaluate become `eval <src>` — a
# path arg that names an existing file has its content read in, small
# snippets go direct, medium ones base64 (-b), oversized ones via --stdin.
def test_browse_inline_js_maps_to_eval():
    from hiveweave.tools.browse_tools import _map_ab_argv

    argv, stdin = _map_ab_argv(["js", "1+1"], "")
    assert argv == ["eval", "1+1"]
    assert stdin is None


def test_browse_evaluate_maps_to_eval():
    from hiveweave.tools.browse_tools import _map_ab_argv

    argv, stdin = _map_ab_argv(["evaluate", "document.title"], "")
    assert argv == ["eval", "document.title"]
    assert stdin is None


def test_browse_eval_file_content_loaded(tmp_path):
    """`eval <path>` to an existing file must run its content, not the path."""
    from hiveweave.tools.browse_tools import _map_ab_argv

    f = tmp_path / "snippet.js"
    f.write_text("42", encoding="utf-8")
    argv, stdin = _map_ab_argv(["eval", str(f)], str(tmp_path))
    assert argv == ["eval", "42"]
    assert stdin is None


def test_browse_js_existing_file_reads_content(tmp_path):
    """Same file-content semantics under the `js` head."""
    from hiveweave.tools.browse_tools import _map_ab_argv

    f = tmp_path / "snippet.js"
    f.write_text("42", encoding="utf-8")
    argv, stdin = _map_ab_argv(["js", str(f)], str(tmp_path))
    assert argv == ["eval", "42"]
    assert stdin is None


def test_browse_large_js_uses_base64():
    """>1024 chars avoids shell/argv escaping via base64 (-b)."""
    import base64

    from hiveweave.tools.browse_tools import _map_ab_argv

    big = "(" + "1;" * 3000 + ")"  # > _EVAL_DIRECT_MAX, <= _EVAL_B64_MAX
    argv, stdin = _map_ab_argv(["js", big], "")
    assert argv[0] == "eval"
    assert argv[1] == "-b"
    assert base64.b64decode(argv[2]).decode("utf-8") == big
    assert stdin is None


def test_browse_huge_js_uses_stdin():
    """Oversized snippets (>24000) avoid Windows argv limits via --stdin."""
    from hiveweave.tools.browse_tools import _map_ab_argv

    huge = "x" * 30000
    argv, stdin = _map_ab_argv(["js", huge], "")
    assert argv == ["eval", "--stdin"]
    assert stdin == huge


def test_browse_goto_maps_to_open():
    from hiveweave.tools.browse_tools import _map_ab_argv

    argv, stdin = _map_ab_argv(["goto", "http://127.0.0.1:3000"], "")
    assert argv == ["open", "http://127.0.0.1:3000"]
    assert stdin is None


def test_browse_screenshot_selector_to_positional():
    from hiveweave.tools.browse_tools import _map_ab_argv

    argv, stdin = _map_ab_argv(
        ["screenshot", "--selector", "canvas", "evidence/x.png"], ""
    )
    assert argv == ["screenshot", "canvas", "evidence/x.png"]
    assert stdin is None


def test_browse_passthrough_command_untouched():
    from hiveweave.tools.browse_tools import _map_ab_argv

    argv, stdin = _map_ab_argv(["console"], "")
    assert argv == ["console"]
    assert stdin is None


# ── check_verify_baseline canonical query key ─────────────────────────────
# Regression from the short-id normalization audit: `create()` now stores
# task_id in canonical (dash-stripped) form, but check_verify_baseline queried
# with the raw dotted task id → the query never matched → the VERIFY baseline
# gate was silently disabled. This test uses a REAL per-project DB so the
# canonical match is actually exercised (the legacy tests mock _conn and
# masked the bug).


@pytest.mark.asyncio
async def test_check_verify_baseline_canonical_query_key(tmp_path):
    import tempfile
    from pathlib import Path
    from unittest.mock import patch

    import hiveweave.services.attestation as att_module
    from hiveweave.services.attestation import (
        attestation_service,
        check_verify_baseline,
    )
    from hiveweave.db import project as project_db

    project_id = "test-verify-baseline-canonical"
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == project_id else None

        att_module._migrated.discard(project_id)

        with patch("hiveweave.db.meta.get_project_workspace",
                   fake_get_project_workspace):
            # Task id as a dotted UUID (what the task ledger carries).
            task_id = "9a8b7c6d-5e4f-3210-fedc-ba9876543210"
            # Store an attestation via create() — stored canonical (dash-stripped).
            await attestation_service.create(
                project_id,
                agent_id="agent-x",
                kind="test_run",
                task_id=task_id,
                command_or_url="pytest",
                workspace=workspace_path,
                stdout="ok",
                exit_code=0,
            )
            task = {
                "id": task_id,
                "title": "VERIFY: game",
                "evidence": {"target_merge_commit": "99999999999999999999"},
            }
            # Make the attestation commit stale so a found row yields an error
            # (non-None) — if the canonical query key misses (no rows), the
            # function returns None and the test fails.
            with (
                patch(
                    "hiveweave.services.worktree_review.project_main_workspace",
                    AsyncMock(return_value="/proj"),
                ),
                patch(
                    "hiveweave.services.git_worktree._git",
                    AsyncMock(
                        side_effect=[
                            (True, "aaaaaaaaaaaaaaaaaaaa\n"),  # main HEAD
                            (False, ""),  # attestation commit NOT ancestor
                        ]
                    ),
                ),
            ):
                err = await check_verify_baseline(project_id, task)

            assert err is not None, (
                "check_verify_baseline must find the canonical-stored attestation; "
                "returned None means the query key missed the row"
            )
            assert "stale" in err.lower() or "baseline" in err.lower()

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


# ── 2nd-audit C1: waive evidence binding normalization ────────────────────
# The waive_attestation binding check compared `ev.task_id` (canonical,
# dash-stripped after the fix) against the raw agent-passed ref with `!=` —
# both the dashed UUID and the 8-char prefix were REJECTED, sealing the
# escape hatch shut. Fixed to compare via _task_ids_equal. Also covers the
# canonical_task_id fail-open invariants (2nd-audit R4 test gaps).


@pytest.mark.asyncio
async def test_canonical_task_id_failopen_invariants():
    """Non-UUID refs pass through untouched — never dash-stripped, no DB call."""
    from hiveweave.services.attestation import canonical_task_id

    # Non-hex synthetic id: raw returned unchanged (no DB round-trip).
    assert await canonical_task_id("proj", "task-1") == "task-1"
    # Dashed non-UUID must NOT be corrupted into "tinvalidate3".
    assert await canonical_task_id("proj", "t-invalidate-3") == "t-invalidate-3"
    # Empty / None stay None.
    assert await canonical_task_id("proj", None) is None
    assert await canonical_task_id("proj", "  ") is None
    # Full UUID canonicalized without DB.
    assert (
        await canonical_task_id("proj", FULL_UUID)
        == FULL_UUID.lower().replace("-", "")
    )


@pytest.mark.asyncio
async def test_canonical_task_id_failopen_on_resolve_error():
    """require_task_id raising (task gone / ambiguous) → raw ref, never raises."""
    from hiveweave.services.attestation import canonical_task_id

    with patch("hiveweave.services.task.TaskService") as TS:
        TS.return_value.require_task_id = AsyncMock(
            side_effect=ValueError("ambiguous task ref")
        )
        ref = "1a2b3c4d"  # 8-hex short prefix → reaches the DB branch
        assert await canonical_task_id("proj", ref) == ref


@pytest.mark.asyncio
async def test_waive_binding_accepts_short_prefix_and_dashed(tmp_path):
    """waive_attestation evidence binding must accept both id forms agents use.

    2nd-audit C1 regression: evidence rows store canonical (dash-stripped)
    task_id; the binding check must match a waive call passing the 8-char
    short prefix AND one passing the dashed full UUID.
    """
    import tempfile
    from pathlib import Path

    import hiveweave.services.attestation as att_module
    from hiveweave.db import project as project_db
    from hiveweave.services import task as task_module
    from hiveweave.services.attestation import attestation_service
    from hiveweave.services.task import TaskService

    project_id = "test-waive-binding-norm"
    coord_id = "coord-wbn"
    exec_id = "exec-wbn"

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == project_id else None

        async def fake_get_agent_project_id(aid: str):
            return project_id if aid in (coord_id, exec_id) else None

        _FAKE_AGENTS = {
            coord_id: {"id": coord_id, "name": "协调", "short_id": "C009",
                       "parent_id": None, "permission_type": "coordinator",
                       "role": "架构师", "status": "active"},
            exec_id: {"id": exec_id, "name": "执行", "short_id": "E009",
                      "parent_id": coord_id, "permission_type": "executor",
                      "role": "engineer", "status": "active"},
        }

        async def fake_get_agent_by_id(aid: str):
            return _FAKE_AGENTS.get(aid)

        att_module._migrated.discard(project_id)
        task_module._migrated.discard(project_id)
        project_db._agent_cache.pop(coord_id, None)
        project_db._agent_cache.pop(exec_id, None)

        with (
            patch("hiveweave.db.meta.get_project_workspace",
                  fake_get_project_workspace),
            patch("hiveweave.db.meta.get_agent_project_id",
                  fake_get_agent_project_id),
            patch("hiveweave.db.meta.get_agent_by_id", fake_get_agent_by_id),
            patch(
                "hiveweave.tools.helpers.get_project_id",
                AsyncMock(return_value=project_id),
            ),
        ):
            svc = TaskService()
            # Two tasks: one waived via short prefix, one via dashed UUID.
            tid_a = await svc.create_task(
                project_id=project_id, title="A", description="d",
                creator_id=coord_id,
            )
            tid_b = await svc.create_task(
                project_id=project_id, title="B", description="d",
                creator_id=coord_id,
            )
            await svc.claim_task(project_id, tid_a, exec_id)
            await svc.claim_task(project_id, tid_b, exec_id)

            # Evidence attestations bound with the dashed form → stored
            # canonical (dash-stripped) by the normalization fix.
            ev_a = await attestation_service.create(
                project_id, agent_id=exec_id, kind="test_run",
                task_id=tid_a, command_or_url="pytest",
                workspace=workspace_path, stdout="ok", exit_code=0,
            )
            ev_b = await attestation_service.create(
                project_id, agent_id=exec_id, kind="test_run",
                task_id=tid_b, command_or_url="pytest",
                workspace=workspace_path, stdout="ok", exit_code=0,
            )

            from hiveweave.tools.tasks.waive import (
                WaiveAttestationParams,
                waive_attestation_tool,
            )

            # (1) waive via 8-char short prefix — the form agents copy from
            # get_tasks. Binding check must NOT reject.
            r1 = await waive_attestation_tool(
                WaiveAttestationParams(
                    taskId=tid_a[:8],
                    reason="short-prefix waive regression test evidence",
                    evidenceAttestationId=ev_a,
                ),
                coord_id,
                workspace_path,
            )
            assert r1.success, f"short-prefix waive rejected: {r1.error or r1.output}"

            # (2) waive via dashed full UUID — the other form agents use.
            r2 = await waive_attestation_tool(
                WaiveAttestationParams(
                    taskId=tid_b,
                    reason="dashed-uuid waive regression test evidence",
                    evidenceAttestationId=ev_b,
                ),
                coord_id,
                workspace_path,
            )
            assert r2.success, f"dashed-uuid waive rejected: {r2.error or r2.output}"

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
        project_db._agent_cache.pop(coord_id, None)
        project_db._agent_cache.pop(exec_id, None)