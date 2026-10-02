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
    """修法②后 adding_coordinator 不再短路，但仍在指导值内 → 静默。"""
    from hiveweave.services.org_invariants import span_advisory

    assert (
        span_advisory(
            agents=_with_kids(2), parent_id="arch-1", adding_coordinator=True
        )
        is None
    )


def test_span_advisory_coordinator_add_evaluates_subtree():
    """修法②：加中层那一刻恰是评估「加得够不够」的时刻——辖下总人数
    仍超指导值时照常提示，文案为「加了这个中层后…仍有 N 人」。"""
    from hiveweave.services.org_invariants import span_advisory

    note = span_advisory(
        agents=_with_kids(7), parent_id="arch-1", adding_coordinator=True
    )
    assert note is not None
    assert "SPAN ADVISORY" in note
    assert "辖下" in note
    assert "仍有" in note  # adding_coordinator 场景的专属措辞
    assert "直属" not in note  # 直属判据对加中层仍跳过


def test_span_advisory_subtree_fires_when_direct_within_guidance():
    """修法①：直属 ≤7 但辖下（含 indirect）超 7 —— subtree 判据单独触发。

    形态：arch-1 直属 = eng-1 + 5 叶（+1 招聘 = 7，直属判据沉默），
    eng-1 名下再挂 3 叶 → arch-1 辖下 9 人，+1 招聘 = 10 → 触发。
    """
    from hiveweave.services.org_invariants import span_advisory

    agents = _with_kids(5)  # eng-1 + 5 叶都挂 arch-1（直属 6）
    for i in range(3):
        agents.append(
            {
                "id": f"grand-{i}",
                "name": f"隔层花{i}",
                "role": f"子模块{i}工程师",
                "permission_type": "executor",
                "parent_id": "eng-1",
                "status": "active",
                "short_id": f"G{i}",
            }
        )
    note = span_advisory(agents=agents, parent_id="arch-1")
    assert note is not None
    assert "辖下（含间接下属）将达 10 人" in note
    assert "的直属将达" not in note  # 直属 7 ≤ 7，直属判据不重复发声


def test_span_advisory_flat_overflow_single_note():
    """扁平组织直属超编时直属与 subtree 是同一事实，只发一条不重复刷屏。"""
    from hiveweave.services.org_invariants import span_advisory

    note = span_advisory(agents=_with_kids(7), parent_id="arch-1")
    assert note is not None
    assert note.count("SPAN ADVISORY") == 1
    assert "直属将达 9" in note


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


# ── 45 轮 C 案③：里程碑 VERIFY 创建时的 QA 深度提示 ────────────────


def test_qa_depth_advisory_no_leaf_qa():
    from hiveweave.services.org_invariants import qa_depth_advisory

    agents = [
        {"id": "ceo", "status": "active", "role": "ceo"},
        {"id": "lead", "status": "active", "role": "qa_lead"},
    ]
    tasks = [{"title": "VERIFY: m1", "status": "created", "kind": "verify"}]
    note = qa_depth_advisory(agents=agents, tasks=tasks)
    assert note is not None
    assert "no active leaf QA" in note


def test_qa_depth_advisory_single_qa_multiple_verifies():
    from hiveweave.services.org_invariants import qa_depth_advisory

    agents = [
        {"id": "q1", "status": "active", "role": "test_engineer"},
    ]
    tasks = [
        {"title": "VERIFY: m1", "status": "created", "kind": "verify"},
        {"title": "VERIFY: m2", "status": "running", "kind": "verify"},
    ]
    note = qa_depth_advisory(agents=agents, tasks=tasks)
    assert note is not None
    assert "only 1" in note


def test_qa_depth_advisory_sufficient_depth_none():
    from hiveweave.services.org_invariants import qa_depth_advisory

    agents = [
        {"id": "q1", "status": "active", "role": "test_engineer"},
        {"id": "q2", "status": "active", "role": "test_engineer"},
    ]
    tasks = [{"title": "VERIFY: m1", "status": "created", "kind": "verify"}]
    assert qa_depth_advisory(agents=agents, tasks=tasks) is None
    assert qa_depth_advisory(agents=agents, tasks=None) is None


# ── B3 ⑤：org_shape_signals（1 业务中层 + ≥6 叶子形态）─────────────


def _coord(
    id: str, name: str, parent: str | None, short: str, role: str = "技术负责人"
) -> dict:
    return {
        "id": id,
        "name": name,
        "role": role,
        "permission_type": "coordinator",
        "parent_id": parent,
        "status": "active",
        "short_id": short,
    }


def _leaf(id: str, name: str, parent: str, short: str) -> dict:
    return {
        "id": id,
        "name": name,
        "role": f"模块{short}工程师",
        "permission_type": "executor",
        "parent_id": parent,
        "status": "active",
        "short_id": short,
    }


def test_org_shape_signals_single_coordinator_many_leaves():
    from hiveweave.services.org_invariants import org_shape_signals

    agents = [
        _coord("ceo", "归零", None, "A1", role="ceo"),
        _coord("mid", "知远", "ceo", "A2"),
    ] + [_leaf(f"e{i}", f"花{i}", "mid", f"E{i}") for i in range(6)]
    signals = org_shape_signals(agents=agents)
    assert len(signals) == 1
    assert "ORG SHAPE SIGNAL" in signals[0]
    assert "知远" in signals[0]
    assert "6 个叶子" in signals[0]
    assert "非阻塞" in signals[0]


def test_org_shape_signals_healthy_orgs_silent():
    from hiveweave.services.org_invariants import org_shape_signals

    # 两个中层分摊 6 叶 → 不触发
    two_mid = [
        _coord("ceo", "归零", None, "A1", role="ceo"),
        _coord("m1", "知远", "ceo", "A2"),
        _coord("m2", "云帆", "ceo", "A3"),
    ] + [_leaf(f"e{i}", f"花{i}", "m1" if i < 3 else "m2", f"E{i}") for i in range(6)]
    assert org_shape_signals(agents=two_mid) == []
    # 1 中层只有 5 叶 → 不触发
    small = [_coord("ceo", "归零", None, "A1", role="ceo"), _coord("m", "知远", "ceo", "A2")] + [
        _leaf(f"e{i}", f"花{i}", "m", f"E{i}") for i in range(5)
    ]
    assert org_shape_signals(agents=small) == []
    assert org_shape_signals(agents=None) == []


# ── B3 ③④：charter paradigm/faces → 最低中层数 + 非阻塞校验 ────────


def test_min_business_coordinators_mapping():
    from hiveweave.services.org_invariants import min_business_coordinators

    assert min_business_coordinators("solo") == 1
    assert min_business_coordinators("flat_squad") == 1
    assert min_business_coordinators("tech_lead") == 1
    assert min_business_coordinators("pipeline") == 1
    assert min_business_coordinators("pm_architect") == 2
    assert min_business_coordinators("pod") == 2  # 未声明 pod 数 → 下限 2
    assert min_business_coordinators("pod", pod_count=3) == 3
    assert min_business_coordinators("unknown_paradigm") is None
    assert min_business_coordinators("") is None


def test_charter_shape_advisory_fires_on_faces_mismatch():
    from hiveweave.services.org_invariants import charter_shape_advisory

    agents = [
        _coord("ceo", "归零", None, "A1", role="ceo"),
        _coord("m1", "知远", "ceo", "A2"),
    ]
    note = charter_shape_advisory(
        charter={"paradigm": "tech_lead", "faces": 4}, agents=agents
    )
    assert note is not None
    assert "4 个" in note
    assert "1 个业务中层" in note
    assert "合并理由" in note
    assert "非阻塞" in note


def test_charter_shape_advisory_silent_states():
    from hiveweave.services.org_invariants import charter_shape_advisory

    agents = [
        _coord("ceo", "归零", None, "A1", role="ceo"),
        _coord("m1", "知远", "ceo", "A2"),
        _coord("m2", "云帆", "ceo", "A3"),
        _coord("m3", "青苔", "ceo", "A4"),
        _coord("m4", "流火", "ceo", "A5"),
    ]
    # 中层数 ≥ faces → 沉默
    assert (
        charter_shape_advisory(
            charter={"paradigm": "tech_lead", "faces": 4}, agents=agents
        )
        is None
    )
    # paradigm 自身声明多中层（pm_architect → 2）→ 跳过
    assert (
        charter_shape_advisory(
            charter={"paradigm": "pm_architect", "faces": 4},
            agents=agents[:2],
        )
        is None
    )
    # faces ≤ 1 → 沉默
    assert (
        charter_shape_advisory(
            charter={"paradigm": "tech_lead", "faces": 1}, agents=agents[:2]
        )
        is None
    )
    # 旧 charter：无 paradigm/faces 字段 → 跳过校验（不拦旧数据）
    assert charter_shape_advisory(charter={"title": "old"}, agents=agents[:2]) is None
    assert charter_shape_advisory(charter=None, agents=agents) is None
    # 未知范式 → 跳过
    assert (
        charter_shape_advisory(
            charter={"paradigm": "death_star", "faces": 9}, agents=agents[:2]
        )
        is None
    )
