"""TEST_DSH_70 批2「备好没接线族」收口测试。

权威定案：docs/platform-issue-research/platform-issue-report-TEST_DSH_70-2026-09-25-merged-appendix.md
§2 P1-1 / P1-2 / P1-4 / P1-6 + §6 修复路线批2。

覆盖（每项一正一反）：
- P1-1 canonical task_id 对外统一：get_tasks(taskId=…) 单任务视图按
  身份变体集合匹配（32 位去横线 canonical ↔ 36 位带横线 tasks.id）；
  原始写法 0 行时才走变体轮，多命中仍报歧义拒绝。
- P1-6 平台级入参归一：services/param_shapes 单键群体形态
  ``{"item": […]}`` 解包（恰一键、值 str/list/tuple），多键 dict 禁展平
  （fail-closed 交 pydantic 报形状错）；commit_turn / submit_task /
  create_task 三处入参接线。
- P1-6 裸 except 落事件：lessons 归档失败/形状非法必须落 telemetry 事件
  + 结构化日志，不许静默（71 轮 memories scope='lesson' 0 行的病根）。
- P1-6 waitingOn 非法拒绝：空 ref / 非法 kind 在 commit_turn 入口显式
  拒绝并说明合法形态；wait_contract.replace_waits 兜底路径不再静默 continue。
- P1-2 defer 断路器键归一：去空白/标点 + 相似度兜底 —— 微调措辞不清零，
  真实换理由正常清零。
- P1-4 lockfile 诚实化 + .gitattributes EOL 钉住：模板不再宣称 post-merge
  自动重生成；-text 钉 lockfile/二进制/图纸；改名保留通道回执说明需手工
  npm/pnpm install。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from hiveweave.db import project as project_db
from hiveweave.services import task as task_module
from hiveweave.services.param_shapes import (
    coerce_list_param,
    shape_summary,
    unwrap_single_key_group,
)
from hiveweave.services.telemetry import telemetry
from hiveweave.services.turn_session import (
    DEFER_REASON_STREAK_LIMIT,
    clear_defer_reason_streak,
    defer_breaker_tripped,
    defer_reason_streak,
    normalize_defer_reason,
    record_defer_reason,
)
from hiveweave.tools.tasks.query import task_id_variant_match

# ── 共享 fixture（temp workspace + meta 补丁，沿用 test_p01 模式）───────

PROJECT_ID = "test-dsh70-b2"
AGENT_ID = "agent-dsh70-b2"


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        async def fake_get_agent_project_id(aid: str):
            return PROJECT_ID if aid == AGENT_ID else None

        task_module._migrated.clear()
        project_db._agent_cache.pop(AGENT_ID, None)

        with (
            patch(
                "hiveweave.db.meta.get_project_workspace",
                fake_get_project_workspace,
            ),
            patch(
                "hiveweave.db.meta.get_agent_project_id",
                fake_get_agent_project_id,
            ),
        ):
            yield {
                "project_id": PROJECT_ID,
                "workspace_path": workspace_path,
                "agent_id": AGENT_ID,
            }

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                # teardown best-effort：temp 目录/连接可能已被上一个用例关闭
                # —— 这里没有可恢复对象，吞掉是刻意的（测试目录随 tmp 销毁）。
                pass
        project_db._agent_cache.pop(AGENT_ID, None)


class _TelemetryCapture:
    """订阅 telemetry 事件流（测试内自清理，防泄漏进其他用例）。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self._handler = self._on_event

    def _on_event(self, name: str, payload: dict) -> None:
        self.events.append((name, payload or {}))

    def __enter__(self) -> "_TelemetryCapture":
        telemetry.add_handler(self._handler)
        return self

    def __exit__(self, *exc) -> None:
        try:
            telemetry._handlers.remove(self._handler)
        except ValueError:
            # handler 已被其他清理路径移除（重复 exit）——无副作用，可吞。
            pass

    def names(self) -> list[str]:
        return [n for n, _ in self.events]


# ── §0 共享 helper：单键群体形态解包（P1-6 基座）───────────────────────


def test_unwrap_single_key_group_positive():
    assert unwrap_single_key_group({"item": ["a", "b"]}) == ["a", "b"]
    assert unwrap_single_key_group({"item": "x"}) == "x"
    assert unwrap_single_key_group({"item": ("a",)}) == ("a",)
    # known_keys 不计入键数（acceptance 声明 dict 同口径）
    assert unwrap_single_key_group(
        {"attestation_ids": ["x"], "item": ["y"]},
        known_keys=("attestation_ids",),
    ) == ["y"]


def test_unwrap_single_key_group_negative():
    # 多键歧义禁展平
    assert unwrap_single_key_group({"a": [1], "b": [2]}) is None
    # 内层非 str/list/tuple 保持 fail-closed
    assert unwrap_single_key_group({"item": 1}) is None
    assert unwrap_single_key_group({"item": {"kind": "task"}}) is None
    assert unwrap_single_key_group("not-a-dict") is None
    assert unwrap_single_key_group(None) is None


def test_coerce_list_param_platform_contract():
    assert coerce_list_param({"item": ["a", "b"]}) == ["a", "b"]
    assert coerce_list_param('["a", "b"]') == ["a", "b"]
    assert coerce_list_param("solo") == ["solo"]
    assert coerce_list_param((1, 2)) == [1, 2]
    assert coerce_list_param(None) is None
    # 多键 dict 原样交回（pydantic 报形状错，不是静默丢）
    multi = {"a": [1], "b": [2]}
    assert coerce_list_param(multi) is multi
    assert shape_summary(multi) == "dict(keys=a,b)"


# ── §1 P1-1 canonical task_id 对外统一 ────────────────────────────────


def test_task_id_variant_match_canonical_forms():
    dashed = "12345678-1234-1234-1234-123456789abc"
    canonical = "12345678123412341234123456789abc"  # 去横线 32 位
    assert task_id_variant_match(dashed, canonical)
    assert task_id_variant_match(canonical, dashed)
    # ≥8 字符前缀（resolve_task_id 同纪律）
    assert task_id_variant_match(dashed, "123456781234")
    assert task_id_variant_match(dashed, "12345678-1234")


def test_task_id_variant_match_negative():
    dashed = "12345678-1234-1234-1234-123456789abc"
    # 不同 id 不匹配
    assert not task_id_variant_match(dashed, "8765432143214321432143214321cba")
    # 短前缀（<8 归一字符）不匹配 —— 防歧义爆炸
    assert not task_id_variant_match(dashed, "1234567")
    assert not task_id_variant_match(dashed, "")
    assert not task_id_variant_match("", "12345678")


@pytest.mark.asyncio
async def test_get_tasks_single_view_finds_by_canonical_id(env):
    """P1-1 正向：attestation 回执里的 32 位去横线 id 查 get_tasks 必命中。"""
    from hiveweave.services.task import TaskService
    from hiveweave.tools.task_tools import GetTasksParams, get_tasks_tool

    svc = TaskService()
    tid = await svc.create_task(
        project_id=env["project_id"],
        title="canonical id view",
        description="d",
        creator_id=env["agent_id"],
    )
    canonical = tid.replace("-", "").lower()

    with patch(
        "hiveweave.tools.helpers.get_project_id",
        AsyncMock(return_value=env["project_id"]),
    ):
        result = await get_tasks_tool(
            GetTasksParams(taskId=canonical), env["agent_id"], ""
        )
    assert result.success, result.output or result.error
    out = result.output or ""
    assert "canonical id view" in out, out
    assert "No task matching" not in out
    # 单任务视图返回的就是带横线权威 id
    assert [str(t.get("id")) for t in (result.extra.get("tasks") or [])] == [tid]


@pytest.mark.asyncio
async def test_get_tasks_single_view_unknown_id_zero_rows(env):
    """P1-1 反向：真不存在的 id 仍 0 行（变体匹配不制造假阳性）。"""
    from hiveweave.services.task import TaskService
    from hiveweave.tools.task_tools import GetTasksParams, get_tasks_tool

    svc = TaskService()
    await svc.create_task(
        project_id=env["project_id"],
        title="other task",
        description="d",
        creator_id=env["agent_id"],
    )
    bogus = "ffffffff-ffff-ffff-ffff-ffffffffffff"

    with patch(
        "hiveweave.tools.helpers.get_project_id",
        AsyncMock(return_value=env["project_id"]),
    ):
        result = await get_tasks_tool(
            GetTasksParams(taskId=bogus), env["agent_id"], ""
        )
    assert result.success
    assert "No task matching" in (result.output or "")
    assert result.extra.get("tasks") == []


# ── §2 P1-6 平台级入参归一接线（commit_turn / submit_task / create_task）──


def test_commit_turn_params_unwrap_item_group():
    from hiveweave.tools.turn_tools import CommitTurnParams

    p = CommitTurnParams.model_validate(
        {
            "phase": "waiting",
            "summary": "s",
            "waitingOn": {
                "item": [
                    {"kind": "task", "ref": "t-1"},
                    {"kind": "agent", "ref": "A469"},
                ]
            },
        }
    )
    assert p.waiting_on == [
        {"kind": "task", "ref": "t-1"},
        {"kind": "agent", "ref": "A469"},
    ]


def test_commit_turn_params_single_item_dict_still_one_item():
    """合法单条等待 {kind, ref}（两键）不受解包影响。"""
    from hiveweave.tools.turn_tools import CommitTurnParams

    p = CommitTurnParams.model_validate(
        {
            "phase": "waiting",
            "summary": "s",
            "waitingOn": {"kind": "task", "ref": "t-1"},
        }
    )
    assert p.waiting_on == [{"kind": "task", "ref": "t-1"}]


def test_submit_task_params_unwrap_item_groups():
    from hiveweave.tools.tasks.submit import SubmitTaskParams

    p = SubmitTaskParams.model_validate(
        {
            "taskId": "t-1",
            "summary": "s",
            "filesChanged": {"item": ["a.py", "b.py"]},
            "attestationIds": {"item": ["att-1"]},
            "failuresAcknowledged": {"item": [{"test": "x", "reason": "y"}]},
            "blockingIssues": {"item": ["b1"]},
        }
    )
    assert p.files_changed == ["a.py", "b.py"]
    assert p.attestation_ids == ["att-1"]
    assert p.failures_acknowledged == [{"test": "x", "reason": "y"}]
    assert p.blocking_issues == ["b1"]


def test_submit_task_params_multi_key_dict_not_flattened():
    """多键 dict 禁展平：显式形状错（fail-closed），不是静默丢数据。"""
    from hiveweave.tools.tasks.submit import SubmitTaskParams

    with pytest.raises(ValidationError):
        SubmitTaskParams.model_validate(
            {
                "taskId": "t-1",
                "summary": "s",
                "filesChanged": {"added": ["a.py"], "removed": ["b.py"]},
            }
        )


def test_create_task_params_unwrap_item_group():
    from hiveweave.tools.tasks.create import CreateTaskParams

    p = CreateTaskParams.model_validate(
        {
            "title": "T",
            "description": "d",
            "submitGate": "docs",
            "acceptanceCriteria": {"item": ["crit-1", "crit-2"]},
            "dependsOn": {"item": ["t-0"]},
            "tags": {"item": ["plane:web"]},
        }
    )
    assert p.acceptance_criteria == ["crit-1", "crit-2"]
    assert p.depends_on == ["t-0"]
    assert p.tags == ["plane:web"]


# ── §3 P1-6 裸 except 落事件（lessons 归档）──────────────────────────


class _FakeTr:
    def __init__(self, lessons) -> None:
        self.phase = "done_slice"
        self.summary = "did things"
        self.extensions = {"lessons": lessons}


@pytest.mark.asyncio
async def test_lessons_item_group_archived(env):
    """正向：``{"item": […]}`` 群体形态 lessons 不再被 isinstance 静默丢弃。"""
    from hiveweave.tools.turn_tools import _archive_turn_lessons

    tr = _FakeTr(
        {"item": [{"lesson": "L1", "root_cause": "R", "fix": "F", "tags": ["t"]}]}
    )
    save = AsyncMock(return_value="id")
    with patch(
        "hiveweave.services.lessons.LessonService.save_lesson", save
    ):
        await _archive_turn_lessons(env["agent_id"], tr, None)
    assert save.await_count == 1
    kwargs = save.await_args.kwargs
    assert kwargs["lesson"] == "L1"
    assert kwargs["root_cause"] == "R"


@pytest.mark.asyncio
async def test_lessons_unsupported_shape_emits_event(env):
    """反向：非法形状（多键 dict）不许静默丢 —— telemetry 事件 + 不归档。"""
    from hiveweave.tools.turn_tools import _archive_turn_lessons

    tr = _FakeTr({"a": [1], "b": [2]})  # 多键 dict：不展平、不归档
    save = AsyncMock(return_value="id")
    with (
        patch("hiveweave.services.lessons.LessonService.save_lesson", save),
        _TelemetryCapture() as cap,
    ):
        await _archive_turn_lessons(env["agent_id"], tr, None)
    assert save.await_count == 0
    assert "turn_lessons_unsupported_shape" in cap.names()


@pytest.mark.asyncio
async def test_lessons_archive_failure_emits_event(env):
    """反向：归档抛错不阻断 turn exit（fail-open），但必须落事件。"""
    from hiveweave.tools.turn_tools import _archive_turn_lessons

    tr = _FakeTr([{"lesson": "L1"}])
    save = AsyncMock(side_effect=RuntimeError("db down"))
    with (
        patch("hiveweave.services.lessons.LessonService.save_lesson", save),
        _TelemetryCapture() as cap,
    ):
        # 不抛 = fail-open 保留
        await _archive_turn_lessons(env["agent_id"], tr, None)
    assert "turn_lessons_archive_failed" in cap.names()


# ── §4 P1-6 waitingOn 非法拒绝 ───────────────────────────────────────


@pytest.mark.asyncio
async def test_commit_turn_rejects_empty_ref_wait():
    """空 ref 等待在 commit_turn 入口显式拒绝并说明合法形态（不再静默跳过）。"""
    from hiveweave.tools.turn_tools import CommitTurnParams, commit_turn_tool

    params = CommitTurnParams.model_validate(
        {
            "phase": "waiting",
            "summary": "s",
            "waitingOn": [{"kind": "agent", "ref": "   "}],
        }
    )
    with patch(
        "hiveweave.db.meta.get_agent_project_id", AsyncMock(return_value=None)
    ):
        result = await commit_turn_tool(params, "agent-x", "", ctx=None)
    assert not result.success
    msg = result.error or ""
    assert "illegal waiting_on" in msg
    # 回执说明合法形态
    assert "kind:" in msg and "ref" in msg
    assert "empty ref can never be registered" in msg


@pytest.mark.asyncio
async def test_commit_turn_rejects_illegal_kind_wait():
    from hiveweave.tools.turn_tools import CommitTurnParams, commit_turn_tool

    params = CommitTurnParams.model_validate(
        {
            "phase": "blocked",
            "summary": "s",
            "waitingOn": [{"kind": "whatever", "ref": "x"}],
        }
    )
    with patch(
        "hiveweave.db.meta.get_agent_project_id", AsyncMock(return_value=None)
    ):
        result = await commit_turn_tool(params, "agent-x", "", ctx=None)
    assert not result.success
    assert "not a legal kind" in (result.error or "")


@pytest.mark.asyncio
async def test_commit_turn_accepts_valid_wait():
    from hiveweave.tools.turn_tools import CommitTurnParams, commit_turn_tool

    params = CommitTurnParams.model_validate(
        {
            "phase": "waiting",
            "summary": "s",
            "waitingOn": [{"kind": "task", "ref": "t-1"}],
        }
    )
    with patch(
        "hiveweave.db.meta.get_agent_project_id", AsyncMock(return_value=None)
    ):
        result = await commit_turn_tool(params, "agent-x", "", ctx=None)
    assert result.success, result.error


@pytest.mark.asyncio
async def test_replace_waits_registers_valid_and_skips_empty_ref(env):
    """wait_contract 兜底路径：合法等待照常登记；空 ref 条目不再静默 continue。"""
    from hiveweave.services.wait_contract import wait_contract_service

    created = await wait_contract_service.replace_waits(
        env["project_id"],
        env["agent_id"],
        [{"kind": "agent", "ref": "coordinator-1"}],
        phase="waiting",
    )
    assert len(created) == 1
    assert created[0]["ref"] == "coordinator-1"

    # 反向：空 ref 条目被跳过（不登记），但只有它时不产生任何行
    with _TelemetryCapture() as _cap:
        created2 = await wait_contract_service.replace_waits(
            env["project_id"],
            env["agent_id"],
            [{"kind": "agent", "ref": ""}],
            phase="waiting",
        )
    assert created2 == []


# ── §5 P1-2 defer 断路器键归一 ───────────────────────────────────────

_REASON_A = "任务还没approved，等平台定时收口，先不推进了。"
# 微调措辞：换标点（全角→半角）、加「还」、去句号 —— 旧 key（前 80 字符）会归零
_REASON_A_REWORDED = "任务还没有approved,等平台定时收口,先不推进"
# 真实换理由
_REASON_B = "等CEO审批本次发布。"
_REASON_C = "等HR审批本次入职流程。"


def test_defer_breaker_survives_reworded_reason():
    clear_defer_reason_streak("ag-b2-1")
    try:
        assert record_defer_reason("ag-b2-1", _REASON_A) == 1
        # 标点/空白/大小写差异被稳定摘要吸收
        assert normalize_defer_reason(_REASON_A) == normalize_defer_reason(
            _REASON_A.upper()
        )
        assert record_defer_reason("ag-b2-1", _REASON_A_REWORDED) == 2
        assert record_defer_reason("ag-b2-1", _REASON_A) == 3
        assert defer_breaker_tripped("ag-b2-1")
        assert DEFER_REASON_STREAK_LIMIT == 3
    finally:
        clear_defer_reason_streak("ag-b2-1")


def test_defer_breaker_resets_on_genuinely_new_reason():
    clear_defer_reason_streak("ag-b2-2")
    try:
        record_defer_reason("ag-b2-2", _REASON_A)
        record_defer_reason("ag-b2-2", _REASON_A_REWORDED)
        # 真实换理由 → streak 重开（相似度显著低于阈值）
        assert record_defer_reason("ag-b2-2", _REASON_B) == 1
        assert not defer_breaker_tripped("ag-b2-2")
        # 超短理由不做相似度兜底：「等CEO审批」vs「等HR审批」是不同等待
        assert record_defer_reason("ag-b2-2", _REASON_C) == 1
        assert defer_reason_streak("ag-b2-2") == 1
    finally:
        clear_defer_reason_streak("ag-b2-2")


# ── §6 P1-4 lockfile 诚实化 + .gitattributes EOL 钉住 ────────────────


def _init_repo_and_read_gitattributes() -> str:
    from hiveweave.services.git_worktree.service_create import CreateMixin

    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp) / "proj"
        os.makedirs(ws)
        result = _run_async(CreateMixin().ensure_git_repo(str(ws)))
        assert result.get("success"), result
        return (ws / ".gitattributes").read_text(encoding="utf-8")


def _run_async(coro):
    import asyncio

    return asyncio.get_event_loop().run_until_complete(coro)


def test_gitattributes_template_pins_eol():
    """P1-4 正向：模板钉住 lockfile/二进制/图纸的 -text，防 autocrlf 污染。"""
    content = _init_repo_and_read_gitattributes()
    # lockfiles（与 GENERATED_FILES 判定源同集）
    for lock in (
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "poetry.lock",
    ):
        assert f"{lock} -text" in content, lock
    # 二进制
    assert "*.png -text" in content
    assert "*.pdf -text" in content
    assert "*.woff2 -text" in content
    # 图纸类
    assert "*.drawio -text" in content
    assert "*.excalidraw -text" in content
    # union 合并规则保留
    assert "package-lock.json merge=union" in content
    # 文本源码不全锁（最小防污染面）
    assert "*.py -text" not in content
    assert "*.ts -text" not in content


def test_gitattributes_template_no_fake_regenerate_claim():
    """P1-4 反向：模板/注释不再宣称 post-merge 自动重生成 lockfile。"""
    content = _init_repo_and_read_gitattributes()
    assert "fixes semantics" not in content
    assert "Post-merge regeneration" not in content
    # 诚实声明在场：钉的权威是 blob，重生成靠本机 npm/pnpm install
    assert "does NOT auto-regenerate" in content
    assert "pinned authority" in content


def test_service_create_source_has_no_fake_postmerge_claim():
    """P1-4 收口：agent 可见回执与源码注释里不得残留「post-merge 重生成」宣称。"""
    import inspect

    from hiveweave.services.git_worktree import service_create as sc

    src = inspect.getsource(sc)
    assert "regenerated post-merge" not in src
    assert "post-merge regenerate eliminates" not in src
    assert "should be regenerated post-merge" not in src


def test_lockfile_rename_note_wired():
    """P1-4：改名保留通道回执说明 lockfile 需手工重生成（后端不起 node）。"""
    from hiveweave.services.git_worktree.service_create import (
        _lockfile_rename_note,
    )

    note = _lockfile_rename_note(
        ["pkg/package-lock.json", "x.tsbuildinfo"], "E42"
    )
    assert "package-lock.json" in note
    assert "E42" in note  # 改名保留做法完整（short_id 前缀示例在场）
    assert "does NOT regenerate" in note
    assert "npm install" in note
    # 非 lockfile 剥离不打扰
    assert _lockfile_rename_note(["x.tsbuildinfo", "test_output-1.json"], "E42") == ""
