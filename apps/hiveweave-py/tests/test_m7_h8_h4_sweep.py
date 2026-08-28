"""M7 + H8 + H4 sweep — task-advance seeding, commit_turn hard rule, obligation hygiene."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── M7: CEO/HR seed skills include task-advance ─────────────────────────────


@pytest.mark.asyncio
async def test_seed_arrays_include_task_advance():
    """_seed_default_agents 必须给 CEO/HR 绑定 task-advance（nudge 文案指向
    read_skill("task-advance")，未绑定 = 死链）。"""
    from hiveweave.api.projects import _seed_default_agents
    from hiveweave.services.model import ModelService
    from hiveweave.services.org import OrgService

    mgmt_row = {"id": "uuid-mgmt", "name": "官方DS", "model_id": "m"}
    exec_row = {"id": "uuid-exec", "name": "ARK Coding", "model_id": "e"}

    async def fake_resolve(*, tier, skip_model_ids=None, prefer_latest=False):
        return mgmt_row if tier == "management" else exec_row

    created: list[dict] = []

    async def fake_create(attrs, bootstrap=False):
        created.append(attrs)
        return {"id": f"id-{attrs['role']}", "model_id": attrs.get("model_id")}

    fake_cursor = AsyncMock()
    fake_cursor.fetchall = AsyncMock(return_value=[])
    fake_conn = AsyncMock()
    fake_conn.execute = AsyncMock(return_value=fake_cursor)
    fake_conn.commit = AsyncMock()

    with (
        patch(
            "hiveweave.api.projects.project_db.get_project_db_by_project_id",
            new=AsyncMock(return_value=fake_conn),
        ),
        patch.object(OrgService, "list_agents", new=AsyncMock(return_value=[])),
        patch.object(ModelService, "resolve_model", side_effect=fake_resolve),
        patch.object(ModelService, "list_active", new=AsyncMock(return_value=[])),
        patch.object(OrgService, "create_agent", side_effect=fake_create),
    ):
        await _seed_default_agents("proj-1")

    assert created, "seed 应至少创建默认 agent"
    by_role = {a["role"]: a for a in created}
    for role in ("ceo", "hr"):
        assert role in by_role, f"缺 {role} 种子: {by_role}"
        assert "task-advance" in by_role[role]["skills"], (
            f"{role} 种子 skills 缺 task-advance: {by_role[role]['skills']}"
        )


# ── P2-⑪: seed CEO/HR 继承项目级 language（与 hire 路径同口径）────────────


@pytest.mark.asyncio
async def test_seed_agents_inherit_project_language():
    """CEO/HR 的 language 必须读 project_meta.language。

    实证：project_meta.language='zh'，但 seed 出来的 A267(CEO)/A268(HR) 是
    'en'（schema 默认），而 hire 出来的 A269–A274 是 'zh' —— 同项目语言分裂。
    """
    from hiveweave.api.projects import _seed_default_agents
    from hiveweave.services.model import ModelService
    from hiveweave.services.org import OrgService

    created: list[dict] = []

    async def fake_create(attrs, bootstrap=False):
        created.append(attrs)
        return {"id": f"id-{attrs['role']}"}

    stale_cursor = AsyncMock()
    stale_cursor.fetchall = AsyncMock(return_value=[])
    lang_cursor = AsyncMock()
    lang_cursor.fetchone = AsyncMock(return_value={"language": "zh"})

    fake_conn = AsyncMock()
    fake_conn.execute = AsyncMock(side_effect=[stale_cursor, lang_cursor])
    fake_conn.commit = AsyncMock()

    with (
        patch(
            "hiveweave.api.projects.project_db.get_project_db_by_project_id",
            new=AsyncMock(return_value=fake_conn),
        ),
        patch.object(OrgService, "list_agents", new=AsyncMock(return_value=[])),
        patch.object(
            ModelService, "resolve_model", new=AsyncMock(return_value=None)
        ),
        patch.object(ModelService, "list_active", new=AsyncMock(return_value=[])),
        patch.object(OrgService, "create_agent", side_effect=fake_create),
    ):
        await _seed_default_agents("proj-1")

    assert created, "seed 应至少创建默认 agent"
    by_role = {a["role"]: a for a in created}
    for role in ("ceo", "hr"):
        assert by_role[role]["language"] == "zh", (
            f"{role} 未继承项目 language: {by_role[role].get('language')}"
        )


@pytest.mark.asyncio
async def test_seed_language_falls_back_to_zh_when_meta_missing():
    """读不到 project_meta 时回落 'zh' —— 与 hire 路径一致，不落 'en'。"""
    from hiveweave.api.projects import _resolve_seed_language

    with patch(
        "hiveweave.api.projects.project_db.get_project_db_by_project_id",
        new=AsyncMock(side_effect=RuntimeError("no db")),
    ):
        assert await _resolve_seed_language("proj-x") == "zh"


# ── H8: commit_turn hard rule present in identity + executor prompts ────────


def test_identity_prompt_has_commit_turn_hard_rule():
    from hiveweave.prompts.identity import build_identity_prompt

    prompt = build_identity_prompt("developer", "executor", "背景", name="n")
    assert "HARD RULE" in prompt
    assert "every turn MUST `commit_turn`" in prompt
    assert "first turn included, no exception" in prompt
    assert "TURN EXIT BLOCKED" in prompt


def test_executor_script_mentions_commit_turn():
    from hiveweave.prompts.executor import build_executor_script

    script = build_executor_script("developer", "n")
    assert "commit_turn" in script
    assert "TURN EXIT BLOCKED" in script


# ── H4: obligation hygiene ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scan_overdue_skips_review_before_submit():
    """行为契约：review 义务在任务进入 submitted/reviewing 前绝不升级。

    不钉死 _REVIEW_PARKED_DEADLINE_MS 的具体数值——占位 deadline 可调
    （原 change-detector 断言已移除）：submit 激活无条件重置 deadline，
    pre-submit 期 scan_overdue 必须跳过（claimed/running 由 task stall 覆盖）。
    """
    from hiveweave.services.obligation import ObligationLedger

    overdue = [
        {
            "id": "ob1",
            "owner_agent_id": "reviewer",
            "obligation_type": "review",
            "task_id": "task-running",
            "escalation_count": 0,
            "escalated_at": 0,
            "deadline": 1,
        }
    ]
    notify = AsyncMock()
    with (
        patch("hiveweave.services.obligation._query", AsyncMock(return_value=overdue)),
        patch("hiveweave.services.obligation._execute", AsyncMock()),
        patch.object(
            ObligationLedger, "_task_status", AsyncMock(return_value="running")
        ),
        patch.object(
            ObligationLedger,
            "_find_escalation_target",
            AsyncMock(return_value="ceo"),
        ),
        patch.object(ObligationLedger, "_notify_escalation", notify),
    ):
        out = await ObligationLedger().scan_overdue("p1")

    assert out == []
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_scan_overdue_warns_when_task_status_missing():
    """_task_status 返回 None（lookup miss）→ 打 warning 且不升级（fail-open）。"""
    from hiveweave.services.obligation import ObligationLedger

    overdue = [
        {
            "id": "ob1",
            "owner_agent_id": "reviewer",
            "obligation_type": "review",
            "task_id": "task-missing",
            "escalation_count": 0,
            "escalated_at": 0,
            "deadline": 1,
        }
    ]
    notify = AsyncMock()
    fake_log = MagicMock()
    with (
        patch("hiveweave.services.obligation._query", AsyncMock(return_value=overdue)),
        patch("hiveweave.services.obligation._execute", AsyncMock()),
        patch.object(
            ObligationLedger, "_task_status", AsyncMock(return_value=None)
        ),
        patch.object(
            ObligationLedger,
            "_find_escalation_target",
            AsyncMock(return_value="ceo"),
        ),
        patch.object(ObligationLedger, "_notify_escalation", notify),
        patch("hiveweave.services.obligation.log", fake_log),
    ):
        out = await ObligationLedger().scan_overdue("p1")

    assert out == []
    notify.assert_not_awaited()
    fake_log.warning.assert_called()
    assert fake_log.warning.call_args.args[0] == (
        "obligation.review_escalate_task_missing"
    )
