"""Tests for LessonService (co-learning) + trigger.context.build recall hook.

Covers: save quality gates, recall scoring (tags/content), keyword extraction,
commit_turn(done_slice + extensions.lessons) archiving, hook injection into
build_trigger_context.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services.lessons import LessonService
from hiveweave.tools.base import get_tool_def

PROJECT_ID = "test-lessons"


def async_mock(result):
    """Build an async function returning ``result`` (for patch new=)."""
    async def _fn(*args, **kwargs):
        return result
    return _fn


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        async def fake_get_agent_project_id(aid: str):
            return PROJECT_ID if aid in ("dev-a", "dev-b") else None

        with (
            patch("hiveweave.db.meta.get_project_workspace",
                  fake_get_project_workspace),
            patch("hiveweave.db.meta.get_agent_project_id",
                  fake_get_agent_project_id),
        ):
            yield {"workspace_path": workspace_path}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
        project_db._agent_cache.clear()


@pytest.fixture(autouse=True)
def _clear_module_cache():
    from hiveweave.services.lessons import _cache
    _cache.clear()
    yield
    _cache.clear()


# ── save_lesson quality gates ───────────────────────────────


@pytest.mark.asyncio
async def test_save_lesson_ok(env):
    mid = await LessonService().save_lesson(
        PROJECT_ID, "dev-a",
        "worktree 合并前必须 git_worktree_merge 同步",
        tags=["worktree", "merge"],
        root_cause="直接 merge 导致未同步提交丢失",
        fix="先 merge 再 rollback",
    )
    assert mid is not None

    lessons = await LessonService().recall_lessons(PROJECT_ID, ["worktree"])
    assert len(lessons) == 1
    assert "worktree" in lessons[0]["content"]


@pytest.mark.asyncio
async def test_save_lesson_rejects_empty(env):
    assert await LessonService().save_lesson(PROJECT_ID, "dev-a", "") is None
    assert await LessonService().save_lesson(
        PROJECT_ID, "dev-a", "   ") is None


@pytest.mark.asyncio
async def test_save_lesson_rejects_fluff_without_value(env):
    # 无根因无修复 → 质量门拒绝
    assert await LessonService().save_lesson(
        PROJECT_ID, "dev-a", "今天干活很顺利") is None


@pytest.mark.asyncio
async def test_save_lesson_rejects_too_long(env):
    long = "x" * 700
    assert await LessonService().save_lesson(
        PROJECT_ID, "dev-a", long, root_cause="rc", fix="fx") is None


# ── recall scoring ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_recall_tags_overlap_scores_higher(env):
    svc = LessonService()
    await svc.save_lesson(
        PROJECT_ID, "dev-a", "worktree merge 前必须同步",
        tags=["worktree", "merge"], root_cause="r", fix="f")
    await svc.save_lesson(
        PROJECT_ID, "dev-a", "Python 依赖必须用 uv sync",
        tags=["python", "uv"], root_cause="r", fix="f")

    hits = await svc.recall_lessons(PROJECT_ID, ["worktree", "merge"])
    assert len(hits) == 1
    assert "worktree" in hits[0]["content"]


@pytest.mark.asyncio
async def test_recall_no_keywords_returns_recent(env):
    svc = LessonService()
    await svc.save_lesson(
        PROJECT_ID, "dev-a", "first", tags=["a"], root_cause="r", fix="f")
    await svc.save_lesson(
        PROJECT_ID, "dev-b", "second", tags=["b"], root_cause="r", fix="f")

    hits = await svc.recall_lessons(PROJECT_ID, None)
    assert len(hits) == 2
    assert hits[0]["content"] == "second"  # 最近在前


@pytest.mark.asyncio
async def test_recall_limit_top3(env):
    svc = LessonService()
    for i in range(5):
        await svc.save_lesson(
            PROJECT_ID, "dev-a", f"lesson-{i}",
            tags=["shared"], root_cause="r", fix="f")

    hits = await svc.recall_lessons(PROJECT_ID, ["shared"])
    assert len(hits) == 3


@pytest.mark.asyncio
async def test_recall_content_substring_match(env):
    svc = LessonService()
    await svc.save_lesson(
        PROJECT_ID, "dev-a", "先调查后修复：遇到 flaky test 不注释",
        tags=[], root_cause="r", fix="f")

    hits = await svc.recall_lessons(PROJECT_ID, ["flaky"])
    assert len(hits) == 1
    assert "flaky" in hits[0]["content"]


# ── keyword extraction ──────────────────────────────────────


def test_extract_keywords_basic():
    kws = LessonService.extract_keywords(
        "Implement the game store module with TypeScript types")
    assert "game" in kws
    assert "store" in kws
    assert "typescript" in kws
    assert "the" not in kws  # stopword


def test_extract_keywords_empty():
    assert LessonService.extract_keywords("") == []
    assert LessonService.extract_keywords(None) == []


def test_extract_keywords_caps_limit():
    kws = LessonService.extract_keywords(
        "alpha beta gamma delta epsilon zeta eta theta iota kappa", max_keywords=3)
    assert len(kws) == 3


# ── commit_turn archiving (co-learning wiring) ──────────────


@pytest.mark.asyncio
async def test_commit_turn_done_slice_archives_lessons(env):
    from hiveweave.services.lessons import _cache

    async def fake_project_id(aid: str):
        return PROJECT_ID

    with (
        patch("hiveweave.db.meta.get_agent_project_id", fake_project_id),
        patch("hiveweave.services.turn_session.set_pending_turn_result"),
    ):
        from hiveweave.tools.turn_tools import CommitTurnParams, commit_turn_tool

        result = await commit_turn_tool(
            CommitTurnParams(
                phase="done_slice",
                summary="完成 store 实现",
                extensions={
                    "lessons": [{
                        "lesson": "store 公共文件必须等 owner 实现后再改",
                        "tags": ["store"],
                        "root_cause": "提前改导致 merge 冲突",
                        "fix": "先 read_file 对齐已有签名",
                    }],
                },
            ),
            "dev-a",
            "",
        )
    assert result.success is True

    lessons = await LessonService().recall_lessons(PROJECT_ID, ["store"])
    assert len(lessons) == 1
    assert "merge 冲突" in lessons[0]["metadata"]["root_cause"]


@pytest.mark.asyncio
async def test_commit_turn_in_progress_does_not_archive(env):
    async def fake_project_id(aid: str):
        return PROJECT_ID

    with (
        patch("hiveweave.db.meta.get_agent_project_id", fake_project_id),
        patch("hiveweave.services.turn_session.set_pending_turn_result"),
    ):
        from hiveweave.tools.turn_tools import CommitTurnParams, commit_turn_tool

        result = await commit_turn_tool(
            CommitTurnParams(
                phase="in_progress",
                summary="继续干活",
                extensions={
                    "lessons": [{"lesson": "x", "tags": ["a"], "root_cause": "r"}],
                },
            ),
            "dev-a",
            "",
        )
    assert result.success is True
    assert await LessonService().recall_lessons(PROJECT_ID, ["a"]) == []


# ── FIFO cap / TTL / robustness ────────────────────────────


@pytest.mark.asyncio
async def test_save_lesson_fifo_cap_evicts_oldest(env):
    from hiveweave.db import project as project_db

    svc = LessonService()
    for i in range(105):
        await svc.save_lesson(
            PROJECT_ID, "dev-a", f"lesson-{i:03d}",
            tags=["t"], root_cause="r", fix="f")

    conn = await project_db.ensure_project_db(env["workspace_path"])
    cursor = await conn.execute(
        "SELECT COUNT(*) AS n FROM memories WHERE scope = 'lesson'")
    row = await cursor.fetchone()
    await cursor.close()
    assert int(row["n"]) == 100

    oldest = await svc.recall_lessons(PROJECT_ID, None, limit=100)
    contents = [l["content"] for l in oldest]
    assert "lesson-000" not in contents
    assert "lesson-104" in contents


@pytest.mark.asyncio
async def test_recall_cache_ttl_expires(env):
    from hiveweave.services import lessons as lessons_mod

    svc = LessonService()
    await svc.save_lesson(
        PROJECT_ID, "dev-a", "alpha", tags=["a"], root_cause="r", fix="f")
    key = (PROJECT_ID, "lesson")
    assert await svc.recall_lessons(PROJECT_ID, ["alpha"])  # 填充缓存
    data, expires = lessons_mod._cache[key]
    lessons_mod._cache[key] = (data, 0.0)  # 强制过期

    hits = await svc.recall_lessons(PROJECT_ID, ["alpha"])
    assert len(hits) == 1
    assert lessons_mod._cache.get(key) is not None  # 重新填充


@pytest.mark.asyncio
async def test_save_lesson_whitespace_only_root_cause_rejected(env):
    assert await LessonService().save_lesson(
        PROJECT_ID, "dev-a", "text", root_cause="   ", fix="f") is not None
    assert await LessonService().save_lesson(
        PROJECT_ID, "dev-a", "text", root_cause="   ", fix="   ") is None


@pytest.mark.asyncio
async def test_save_lesson_accepts_root_cause_only_or_fix_only(env):
    svc = LessonService()
    assert await svc.save_lesson(
        PROJECT_ID, "dev-a", "a", root_cause="rc", fix=None) is not None
    assert await svc.save_lesson(
        PROJECT_ID, "dev-a", "b", root_cause=None, fix="fx") is not None


@pytest.mark.asyncio
async def test_save_lesson_tags_coerced_to_strings(env):
    svc = LessonService()
    mid = await svc.save_lesson(
        PROJECT_ID, "dev-a", "text", tags="worktree",
        root_cause="r", fix="f")
    assert mid is not None
    hits = await svc.recall_lessons(PROJECT_ID, ["worktree"])
    assert len(hits) == 1


@pytest.mark.asyncio
async def test_commit_turn_skips_malformed_lessons(env):
    async def fake_project_id(aid: str):
        return PROJECT_ID

    with (
        patch("hiveweave.db.meta.get_agent_project_id", fake_project_id),
        patch("hiveweave.services.turn_session.set_pending_turn_result"),
    ):
        from hiveweave.tools.turn_tools import CommitTurnParams, commit_turn_tool

        result = await commit_turn_tool(
            CommitTurnParams(
                phase="done_slice",
                summary="完成",
                extensions={
                    "lessons": [
                        "not-a-dict",
                        {"lesson": 123, "tags": ["a"]},   # 非 str lesson
                        {"lesson": "valid one", "tags": ["v"], "root_cause": "r"},
                    ],
                },
            ),
            "dev-a",
            "",
        )
    assert result.success is True
    hits = await LessonService().recall_lessons(PROJECT_ID, ["v"])
    assert len(hits) == 1
    assert hits[0]["content"] == "valid one"


@pytest.mark.asyncio
async def test_commit_turn_archives_on_soft_pass(env):
    """soft-pass 分支（首次 SOFT 违规）也必须归档 lessons，不得静默丢弃。"""
    async def fake_project_id(aid: str):
        return PROJECT_ID

    with (
        patch("hiveweave.db.meta.get_agent_project_id", fake_project_id),
        patch("hiveweave.services.turn_session.set_pending_turn_result"),
        patch("hiveweave.services.turn_exit.pre_check_exit_gates",
              new=async_mock(["SOME_SOFT_GATE"])),
        patch("hiveweave.tools.turn_tools.classify_commit_gate_soft_warn",
              return_value=(["SOME_SOFT_GATE"], [])),
    ):
        from hiveweave.tools.turn_tools import CommitTurnParams, commit_turn_tool

        result = await commit_turn_tool(
            CommitTurnParams(
                phase="done_slice",
                summary="完成",
                extensions={
                    "lessons": [{"lesson": "soft 也归档", "tags": ["soft"],
                                 "root_cause": "r"}],
                },
            ),
            "dev-a",
            "",
        )
    assert result.success is True
    hits = await LessonService().recall_lessons(PROJECT_ID, ["soft"])
    assert len(hits) == 1


@pytest.mark.asyncio
async def test_commit_turn_fail_open_when_db_broken(env):
    """workspace 查不到（DB 故障）时归档失败不得阻塞 turn exit。"""
    async def fake_project_id(aid: str):
        return None  # 查不到 → LessonService._conn 抛错

    with (
        patch("hiveweave.db.meta.get_agent_project_id", fake_project_id),
        patch("hiveweave.services.turn_session.set_pending_turn_result"),
    ):
        from hiveweave.tools.turn_tools import CommitTurnParams, commit_turn_tool

        result = await commit_turn_tool(
            CommitTurnParams(
                phase="done_slice",
                summary="完成",
                extensions={
                    "lessons": [{"lesson": "x", "root_cause": "r"}],
                },
            ),
            "dev-a",
            "",
        )
    assert result.success is True


@pytest.mark.asyncio
async def test_lesson_service_fail_open_on_evicted_workspace(env):
    from hiveweave.services.lessons import LessonService

    async def no_workspace(pid: str):
        return None

    with patch("hiveweave.db.meta.get_project_workspace", no_workspace):
        with pytest.raises(Exception):
            await LessonService().save_lesson(
                PROJECT_ID, "dev-a", "x", root_cause="r")


# ── trigger.context.build hook ──────────────────────────────


@pytest.mark.asyncio
async def test_hook_injects_lessons_block(env):
    from hiveweave.hooks.handlers.lessons import on_trigger_context_build

    await LessonService().save_lesson(
        PROJECT_ID, "dev-a",
        "worktree 合并前必须 git_worktree_merge 同步",
        tags=["worktree"], root_cause="r", fix="f")

    output: dict = {}
    await on_trigger_context_build(
        {
            "agent_id": "dev-b",
            "project_id": PROJECT_ID,
            "context": "## Pending Tasks\n任务：worktree 合并与回滚",
        },
        output,
    )
    block = output.get("lessons_block")
    assert block is not None
    assert "## Past Lessons" in block
    assert "worktree" in block


@pytest.mark.asyncio
async def test_hook_no_match_no_block(env):
    from hiveweave.hooks.handlers.lessons import on_trigger_context_build

    await LessonService().save_lesson(
        PROJECT_ID, "dev-a",
        "worktree 合并前必须同步",
        tags=["worktree"], root_cause="r", fix="f")

    output: dict = {}
    await on_trigger_context_build(
        {
            "agent_id": "dev-b",
            "project_id": PROJECT_ID,
            "context": "## Pending Tasks\n任务：写 README",
        },
        output,
    )
    assert output.get("lessons_block") is None


@pytest.mark.asyncio
async def test_build_trigger_context_wires_hook(env, monkeypatch):
    """端到端：build_trigger_context 组装后经 hook 注入 lessons block。"""
    from hiveweave.agents import trigger as trigger_module
    from hiveweave.services.lessons import _cache

    await LessonService().save_lesson(
        PROJECT_ID, "dev-a",
        "merge 前必须先同步 worktree",
        tags=["worktree"], root_cause="r", fix="f")

    async def fake_pending_handoffs(pid, aid):
        return [{
            "id": "h1", "from_agent_id": "dev-a", "to_agent_id": "dev-b",
            "summary": "帮我合并 worktree 分支", "status": "pending",
            "expect_report": False, "module_id": None,
        }]

    async def fake_accepted_handoffs(pid, aid):
        return []

    async def fake_pending_messages(aid):
        return []

    async def fake_undelivered_background(aid):
        return []

    async def fake_unreported(pid, aid):
        return []

    async def fake_mark_delivered(pid, ids):
        return None

    async def fake_agent_name(aid):
        return {"dev-a": "DevA", "dev-b": "DevB"}.get(aid, aid)

    monkeypatch.setattr(trigger_module, "_get_agent_manager", lambda: None)
    monkeypatch.setattr(
        trigger_module._handoff_service, "get_pending_handoffs",
        fake_pending_handoffs)
    monkeypatch.setattr(
        trigger_module._handoff_service, "get_accepted_handoffs",
        fake_accepted_handoffs)
    monkeypatch.setattr(
        trigger_module._handoff_service, "get_unreported_accepted_handoffs",
        fake_unreported)
    monkeypatch.setattr(
        trigger_module._handoff_service, "mark_delivered", fake_mark_delivered)
    monkeypatch.setattr(trigger_module._inbox_service,
                        "get_pending_messages", fake_pending_messages)
    monkeypatch.setattr(trigger_module._inbox_service,
                        "get_undelivered_background",
                        fake_undelivered_background)
    monkeypatch.setattr(trigger_module, "_agent_name", fake_agent_name)

    from hiveweave.hooks import TRIGGER_CONTEXT_BUILD, hooks
    from hiveweave.hooks.handlers.lessons import register as register_lessons
    register_lessons()
    try:
        result = await trigger_module.build_trigger_context(
            {"id": "dev-b", "project_id": PROJECT_ID, "name": "DevB"},
            "subordinate",
        )
    finally:
        hooks.clear(TRIGGER_CONTEXT_BUILD)
    assert result is not None
    context = result[0]
    assert "## Past Lessons" in context
    assert "worktree" in context


def test_commit_turn_schema_mentions_lessons():
    td = get_tool_def("commit_turn")
    assert td is not None
    schema = td.to_llm_schema() if hasattr(td, "to_llm_schema") else None
    if schema:
        desc = str(schema)
        assert "lessons" in desc