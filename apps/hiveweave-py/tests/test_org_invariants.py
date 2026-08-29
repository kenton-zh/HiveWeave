"""Hire hard invariants — unique 花名/role, no executor under CEO.

Headcount has NO hard cap: span of control (直属 ≤5-7) is prompt-level
guidance only; hiring a 9th direct report must not be blocked.
"""

from __future__ import annotations

from hiveweave.services.org_invariants import validate_hire


def _agents() -> list[dict]:
    return [
        {
            "id": "ceo-1",
            "name": "归零",
            "role": "ceo",
            "permission_type": "coordinator",
            "parent_id": None,
            "status": "active",
            "short_id": "A1",
        },
        {
            "id": "arch-1",
            "name": "知远",
            "role": "frontend-architect",
            "permission_type": "coordinator",
            "parent_id": "ceo-1",
            "status": "active",
            "short_id": "A2",
        },
        {
            "id": "eng-1",
            "name": "墨白",
            "role": "签到排行榜工程师",
            "permission_type": "executor",
            "parent_id": "arch-1",
            "status": "active",
            "short_id": "A3",
        },
        {
            "id": "eng-old",
            "name": "旧人",
            "role": "旧模块工程师",
            "permission_type": "executor",
            "parent_id": "arch-1",
            "status": "archived",
            "short_id": "A0",
        },
    ]


def test_allows_valid_executor_hire():
    err = validate_hire(
        agents=_agents(),
        name="青禾",
        role="认证API工程师",
        permission_type="executor",
        parent_id="arch-1",
    )
    assert err is None


def test_rejects_duplicate_active_name():
    err = validate_hire(
        agents=_agents(),
        name="墨白",
        role="另一模块工程师",
        permission_type="executor",
        parent_id="arch-1",
    )
    assert err is not None
    assert "already named" in err


def test_allows_reusing_archived_name():
    err = validate_hire(
        agents=_agents(),
        name="旧人",
        role="新模块工程师",
        permission_type="executor",
        parent_id="arch-1",
    )
    assert err is None


def test_rejects_duplicate_executor_role():
    err = validate_hire(
        agents=_agents(),
        name="青禾",
        role="签到排行榜工程师",
        permission_type="executor",
        parent_id="arch-1",
    )
    assert err is not None
    assert "already owns role" in err


def test_rejects_bare_executor_role():
    err = validate_hire(
        agents=_agents(),
        name="青禾",
        role="前端工程师",
        permission_type="executor",
        parent_id="arch-1",
    )
    assert err is not None
    assert "too generic" in err


def test_rejects_executor_under_ceo():
    err = validate_hire(
        agents=_agents(),
        name="青禾",
        role="认证API工程师",
        permission_type="executor",
        parent_id="ceo-1",
    )
    assert err is not None
    assert "cannot report directly to CEO" in err


def test_allows_span_beyond_seven():
    """No headcount hard cap: an 8th+ direct report is allowed (TEST_DSH_36)."""
    agents = _agents()
    for i in range(9):
        agents.append(
            {
                "id": f"kid-{i}",
                "name": f"花{i}",
                "role": f"模块{i}工程师",
                "permission_type": "executor",
                "parent_id": "arch-1",
                "status": "active",
                "short_id": f"K{i}",
            }
        )
    err = validate_hire(
        agents=agents,
        name="溢编",
        role="额外模块工程师",
        permission_type="executor",
        parent_id="arch-1",
    )
    assert err is None


def test_rejects_reserved_flower_name():
    err = validate_hire(
        agents=_agents(),
        name="归零",
        role="杂务协调",
        permission_type="coordinator",
        parent_id="ceo-1",
    )
    assert err is not None
    assert "reserved" in err


def test_rejects_archived_parent():
    agents = _agents()
    agents.append(
        {
            "id": "dead-boss",
            "name": "已走",
            "role": "ex-architect",
            "permission_type": "coordinator",
            "parent_id": "ceo-1",
            "status": "archived",
            "short_id": "DX",
        }
    )
    err = validate_hire(
        agents=agents,
        name="青禾",
        role="认证API工程师",
        permission_type="executor",
        parent_id="dead-boss",
    )
    assert err is not None
    assert "archived" in err


def test_rejects_transfer_executor_to_ceo():
    from hiveweave.services.org_invariants import validate_transfer

    err = validate_transfer(
        agents=_agents(),
        agent_id="eng-1",
        new_parent_id="ceo-1",
    )
    assert err is not None
    assert "cannot report directly to CEO" in err


def test_allows_transfer_executor_to_coordinator():
    from hiveweave.services.org_invariants import validate_transfer

    agents = _agents() + [
        {
            "id": "arch-2",
            "name": "云帆",
            "role": "游戏技术负责人",
            "permission_type": "coordinator",
            "parent_id": "ceo-1",
            "status": "active",
            "short_id": "A9",
        }
    ]
    err = validate_transfer(
        agents=agents,
        agent_id="eng-1",
        new_parent_id="arch-2",
    )
    assert err is None


def test_rejects_transfer_executor_to_root():
    from hiveweave.services.org_invariants import validate_transfer

    err = validate_transfer(
        agents=_agents(),
        agent_id="eng-1",
        new_parent_id=None,
    )
    assert err is not None
    assert "cannot be root" in err.lower() or "coordinator" in err.lower()


# ── span_advisory: non-blocking layering hint ──────────────


def _with_kids(n: int) -> list[dict]:
    agents = _agents()
    for i in range(n):
        agents.append(
            {
                "id": f"kid-{i}",
                "name": f"花{i}",
                "role": f"模块{i}工程师",
                "permission_type": "executor",
                "parent_id": "arch-1",
                "status": "active",
                "short_id": f"K{i}",
            }
        )
    return agents


def test_span_advisory_silent_within_guidance():
    from hiveweave.services.org_invariants import span_advisory

    # eng-1 + 5 kids = 6 direct reports; +1 hire = 7 → within guidance
    assert span_advisory(agents=_with_kids(5), parent_id="arch-1") is None


def test_span_advisory_fires_beyond_guidance():
    from hiveweave.services.org_invariants import span_advisory

    note = span_advisory(agents=_with_kids(7), parent_id="arch-1")
    assert note is not None
    assert "SPAN ADVISORY" in note
    assert "coordinator" in note
    assert "9" in note  # 8 existing + the incoming hire


def test_span_advisory_skips_coordinator_add():
    from hiveweave.services.org_invariants import span_advisory

    # Adding a coordinator IS the layering remedy — never nudged
    assert (
        span_advisory(
            agents=_with_kids(7), parent_id="arch-1", adding_coordinator=True
        )
        is None
    )


def test_span_advisory_transfer_exclude_keeps_count_stable():
    from hiveweave.services.org_invariants import span_advisory

    # Same-parent no-op transfer: 7 total, mover excluded then +1 → 7 → silent
    assert (
        span_advisory(
            agents=_with_kids(6), parent_id="arch-1", exclude_id="eng-1"
        )
        is None
    )
    # 8 total: mover excluded then +1 → 8 → fires
    assert (
        span_advisory(
            agents=_with_kids(7), parent_id="arch-1", exclude_id="eng-1"
        )
        is not None
    )
