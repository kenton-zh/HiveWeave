"""Hire hard invariants — unique 花名/role, no executor under CEO, span max."""

from __future__ import annotations

from hiveweave.services.org_invariants import MAX_DIRECT_REPORTS, validate_hire


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


def test_rejects_span_overflow():
    agents = _agents()
    for i in range(MAX_DIRECT_REPORTS):
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
    assert err is not None
    assert f"max {MAX_DIRECT_REPORTS}" in err


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
