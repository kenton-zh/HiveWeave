"""Explicit attestation_ids reuse matrix + unique-prefix get (UUID fixtures)."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.services.attestation import (
    AmbiguousAttestationId,
    AttestationService,
    check_attestation_reuse_binding,
)

TASK_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
TASK_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
AGENT_1 = "11111111-1111-4111-8111-111111111111"
AGENT_2 = "22222222-2222-4222-8222-222222222222"
ATT_ID = "407ec944-1111-4111-8111-111111111111"
HEAD = "abc123def4567890abc123def4567890abc123de"
OTHER = "ffffffffffffffffffffffffffffffffffffffff"


def _row(**kwargs) -> dict:
    now = int(time.time() * 1000)
    base = {
        "id": ATT_ID,
        "agent_id": AGENT_1,
        "task_id": TASK_A.replace("-", ""),
        "kind": "test_run",
        "exit_code": 0,
        "stdout_hash": "deadbeefdeadbeef",
        "commit_hash": HEAD,
        "created_at": now,
        "expires_at": now + 24 * 60 * 60 * 1000,
    }
    base.update(kwargs)
    return base


@pytest.mark.asyncio
async def test_same_agent_other_task_head_commit_ok():
    svc = AttestationService()
    row = _row(task_id=TASK_A.replace("-", ""), commit_hash=HEAD)
    with (
        patch.object(svc, "ensure_schema", new_callable=AsyncMock),
        patch.object(svc, "get", new_callable=AsyncMock, return_value=row),
        patch(
            "hiveweave.services.attestation._commit_is_worktree_head_or_ancestor",
            new_callable=AsyncMock,
            return_value=True,
        ) as git_mock,
    ):
        ok, err = await svc.verify_ids(
            "proj",
            [ATT_ID],
            expected_agent_id=AGENT_1,
            task_id=TASK_B,
        )
    assert ok, err
    git_mock.assert_awaited()


@pytest.mark.asyncio
async def test_same_agent_other_task_commit_not_ancestor_fails():
    svc = AttestationService()
    row = _row(commit_hash=OTHER)
    with (
        patch.object(svc, "ensure_schema", new_callable=AsyncMock),
        patch.object(svc, "get", new_callable=AsyncMock, return_value=row),
        patch(
            "hiveweave.services.attestation._commit_is_worktree_head_or_ancestor",
            new_callable=AsyncMock,
            return_value=False,
        ),
    ):
        ok, err = await svc.verify_ids(
            "proj",
            [ATT_ID],
            expected_agent_id=AGENT_1,
            task_id=TASK_B,
        )
    assert ok is False
    assert "commit" in err.lower() or "ancestor" in err.lower()


@pytest.mark.asyncio
async def test_other_agent_same_task_ok():
    svc = AttestationService()
    row = _row(agent_id=AGENT_2, task_id=TASK_A.replace("-", ""))
    with (
        patch.object(svc, "ensure_schema", new_callable=AsyncMock),
        patch.object(svc, "get", new_callable=AsyncMock, return_value=row),
        patch(
            "hiveweave.services.attestation._commit_is_worktree_head_or_ancestor",
            new_callable=AsyncMock,
            return_value=False,
        ) as git_mock,
    ):
        ok, err = await svc.verify_ids(
            "proj",
            [ATT_ID],
            expected_agent_id=AGENT_1,
            task_id=TASK_A,
        )
    assert ok, err
    git_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_other_agent_other_task_fails():
    svc = AttestationService()
    row = _row(agent_id=AGENT_2, task_id=TASK_A.replace("-", ""))
    with (
        patch.object(svc, "ensure_schema", new_callable=AsyncMock),
        patch.object(svc, "get", new_callable=AsyncMock, return_value=row),
    ):
        ok, err = await svc.verify_ids(
            "proj",
            [ATT_ID],
            expected_agent_id=AGENT_1,
            task_id=TASK_B,
        )
    assert ok is False
    assert "mismatch" in err.lower()


@pytest.mark.asyncio
async def test_doc_review_missing_hash_same_agent_other_task_skips_commit():
    svc = AttestationService()
    row = _row(
        kind="doc_review",
        commit_hash=None,
        task_id=TASK_A.replace("-", ""),
    )
    with (
        patch.object(svc, "ensure_schema", new_callable=AsyncMock),
        patch.object(svc, "get", new_callable=AsyncMock, return_value=row),
        patch(
            "hiveweave.services.attestation._commit_is_worktree_head_or_ancestor",
            new_callable=AsyncMock,
            return_value=False,
        ) as git_mock,
    ):
        ok, err = await svc.verify_ids(
            "proj",
            [ATT_ID],
            expected_agent_id=AGENT_1,
            task_id=TASK_B,
        )
    assert ok, err
    git_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_binding_helper_waive_other_agent_same_task():
    row = _row(agent_id=AGENT_2, task_id=TASK_A.replace("-", ""))
    ok, err = await check_attestation_reuse_binding(
        "proj",
        row,
        expected_task_id=TASK_A,
        expected_agent_id=AGENT_1,
    )
    assert ok, err


@pytest.mark.asyncio
async def test_get_unique_prefix_resolves_uuid():
    svc = AttestationService()
    full = dict(_row(id=ATT_ID))

    class _Cur:
        def __init__(self, rows):
            self._rows = rows

        async def fetchone(self):
            return self._rows[0] if self._rows else None

        async def fetchall(self):
            return self._rows

        async def close(self):
            return None

    class _Conn:
        def __init__(self):
            self.calls: list[tuple] = []

        async def execute(self, sql, params=None):
            self.calls.append((sql, params))
            sql_l = sql.lower()
            if "like" in sql_l:
                return _Cur([full])
            return _Cur([])

    conn = _Conn()
    with (
        patch.object(svc, "ensure_schema", new_callable=AsyncMock),
        patch(
            "hiveweave.services.attestation._conn",
            new_callable=AsyncMock,
            return_value=conn,
        ),
    ):
        got = await svc.get("proj", "407ec944")
    assert got is not None
    assert got["id"] == ATT_ID


@pytest.mark.asyncio
async def test_get_ambiguous_prefix_raises():
    svc = AttestationService()
    r1 = dict(_row(id=ATT_ID))
    r2 = dict(_row(id="407ec944-2222-4222-8222-222222222222"))

    class _Cur:
        def __init__(self, rows):
            self._rows = rows

        async def fetchone(self):
            return self._rows[0] if self._rows else None

        async def fetchall(self):
            return self._rows

        async def close(self):
            return None

    class _Conn:
        async def execute(self, sql, params=None):
            if "LIKE" in sql or "like" in sql.lower():
                return _Cur([r1, r2])
            return _Cur([])

    with (
        patch.object(svc, "ensure_schema", new_callable=AsyncMock),
        patch(
            "hiveweave.services.attestation._conn",
            new_callable=AsyncMock,
            return_value=_Conn(),
        ),
    ):
        with pytest.raises(AmbiguousAttestationId, match="ambiguous"):
            await svc.get("proj", "407ec944")


@pytest.mark.asyncio
async def test_commit_prefix_does_not_skip_ancestor_check():
    """Unrelated commits can share a 7+ hex prefix; only merge-base decides."""
    from hiveweave.services.attestation import _commit_is_worktree_head_or_ancestor

    calls: list[list[str]] = []

    async def fake_git(args, cwd):
        calls.append(list(args))
        if args[:2] == ["rev-parse", "HEAD"]:
            return True, HEAD + "\n"
        if args[:2] == ["merge-base", "--is-ancestor"]:
            return False, ""
        return False, ""

    with (
        patch(
            "hiveweave.services.attestation._agent_worktree_cwd",
            new_callable=AsyncMock,
            return_value="/tmp/wt",
        ),
        patch("hiveweave.services.git_worktree._git", new=fake_git),
    ):
        ok = await _commit_is_worktree_head_or_ancestor("proj", AGENT_1, HEAD[:7])
    assert ok is False
    assert any(c[:2] == ["merge-base", "--is-ancestor"] for c in calls)


@pytest.mark.asyncio
async def test_commit_exact_head_skips_merge_base():
    from hiveweave.services.attestation import _commit_is_worktree_head_or_ancestor

    calls: list[list[str]] = []

    async def fake_git(args, cwd):
        calls.append(list(args))
        if args[:2] == ["rev-parse", "HEAD"]:
            return True, HEAD + "\n"
        raise AssertionError(f"unexpected git {args}")

    with (
        patch(
            "hiveweave.services.attestation._agent_worktree_cwd",
            new_callable=AsyncMock,
            return_value="/tmp/wt",
        ),
        patch("hiveweave.services.git_worktree._git", new=fake_git),
    ):
        ok = await _commit_is_worktree_head_or_ancestor("proj", AGENT_1, HEAD)
    assert ok is True
    assert not any(c[:2] == ["merge-base", "--is-ancestor"] for c in calls)
