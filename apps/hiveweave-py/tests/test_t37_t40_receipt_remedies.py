"""T37 + T40 拒收回执处方回归。

T37 契约墙：delivery contract 未填齐的 submit 拒收回执必须附一份**按本任务
实例化**的可抄 deliveryContract JSON——已有 test_run 凭证时 test 字段填真 id
（与 auto-attach 同源判据，照抄即过同一张门）；无凭证时退占位符 + 跑测指引；
summary 尽量复用本次提交里已合法的摘要（只缺 test 的场景照抄整份 JSON 即过）。

T40 竞态处方：review_task 撞 running 态（提交仍在落地）时，回执保留事实，
追加 wait_on 挂起处方（拒绝语义不变，仍 failed；legacy 模式给 commit_turn
等价写法）。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import task as task_module
from hiveweave.services import delivery_contract as dcv
from hiveweave.services.attestation import attestation_service
from hiveweave.services.task import TaskService
from hiveweave.tools.tasks.submit import (
    SubmitTaskParams,
    _DC_SUMMARY_EXAMPLE,
    _submit_preflight,
)

PROJECT_ID = "t37-t40-proj"
COORD = "coord-t37"
EXEC = "exec-t37"
REVIEWER = "reviewer-t37"
ASSIGNEE = "assignee-t37"
CREATOR = "creator-t37"


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_ws(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        task_module._migrated.clear()
        with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
            yield {"project_id": PROJECT_ID, "workspace": workspace_path}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


async def _mk_task(env, *, contract=None):
    ts = TaskService()
    tid = await ts.create_task(
        env["project_id"],
        "impl signup module",
        "impl signup",
        creator_id=COORD,
        assignee_id=EXEC,
        policy_id="coordinator_review",
        contract_json=contract,
    )
    task = await ts.get_task(env["project_id"], tid)
    return ts, tid, task


def _dc_contract() -> dict:
    # 契约 id 只需稳定 8 位串（dc-<prefix>），与任务 id 无关
    return dcv.build_default_contract("t37abcd")


@contextmanager
def _no_worktree():
    with patch(
        "hiveweave.services.worktree_review.agent_worktree_path",
        AsyncMock(return_value=None),
    ):
        yield


async def _preflight(env, task, params_overrides: dict | None = None):
    base = {
        "task_id": task["id"],
        "summary": "impl done",
        "tests_passed": True,
    }
    if params_overrides:
        base.update(params_overrides)
    params = SubmitTaskParams(**base)
    with _no_worktree():
        return await _submit_preflight(
            env["project_id"], EXEC, task["id"], task, params
        )


def _dc_issue(res: dict) -> dict:
    for i in res["issues"]:
        if i.get("code") == "delivery_contract_incomplete":
            return i
    raise AssertionError(
        f"no delivery_contract_incomplete issue: {res['issues']}"
    )


def _extract_template_json(message: str) -> dict:
    """从回执 message 里抽出 COPY-PASTE 的 deliveryContract JSON。

    message 尾部还有 not_applicable 等含花括号的示例文本 ⇒ 用 raw_decode
    取 marker 之后第一个完整 JSON 对象，不能按首尾花括号切片。
    """
    marker = "deliveryContract 参数重交"
    idx = message.index(marker)
    tail = message[idx + len(marker):]
    start = tail.index("{")
    obj, _ = json.JSONDecoder().raw_decode(tail[start:])
    return obj


# ── T37：可抄模板 ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_t37_receipt_with_real_test_run_credential_fills_real_id(env):
    """有 test_run 凭证 ⇒ 回执含可抄 JSON 且 test 字段=真凭证 id。"""
    _, tid, task = await _mk_task(env, contract=_dc_contract())
    att_id = await attestation_service.create(
        env["project_id"], agent_id=EXEC, kind="test_run",
        task_id=tid, command_or_url="pytest -q", exit_code=0, stdout="ok",
    )
    res = await _preflight(env, task)  # 完全没填 deliveryContract
    issue = _dc_issue(res)
    assert "COPY-PASTE" in issue["message"]
    assert "原样作为 submit_task 的 deliveryContract 参数重交" in issue["message"]
    obj = _extract_template_json(issue["message"])
    assert obj["test"] == f"test_run:{att_id}"  # 实时填充的真 id
    # P0-4：summary 示例必须是**可照抄**的真实示例——不得落在
    # _PLACEHOLDER_LEADS 占位前缀上（旧值 "<实现摘要…>" 照抄必被二次拒收）。
    assert obj["summary"] == _DC_SUMMARY_EXAMPLE
    assert not obj["summary"].startswith("<")


@pytest.mark.asyncio
async def test_t37_copy_paste_json_passes_gate_verbatim(env):
    """金路径（S19 复现形态）：摘要合法 + test 填错 + 库里有真凭证 ⇒

    回执 JSON 原样照抄（summary 复用原摘要、test 填真 id）重交即过 dc 门。
    """
    _, tid, task = await _mk_task(env, contract=_dc_contract())
    att_id = await attestation_service.create(
        env["project_id"], agent_id=EXEC, kind="test_run",
        task_id=tid, command_or_url="pytest -q", exit_code=0, stdout="ok",
    )
    res = await _preflight(
        env, task,
        {"delivery_contract": {"summary": "impl done", "test": "run ok"}},
    )
    obj = _extract_template_json(_dc_issue(res)["message"])
    assert obj == {"summary": "impl done", "test": f"test_run:{att_id}"}
    # 照抄重交：同一份 JSON 作为 deliveryContract 参数 ⇒ 门放行
    res2 = await _preflight(env, task, {"delivery_contract": obj})
    assert "delivery_contract_incomplete" not in {
        i["code"] for i in res2["issues"]
    }


@pytest.mark.asyncio
async def test_p0_4_summary_example_copy_passes_gate_verbatim(env):
    """P0-4 测试③：处方示例**逐字照抄** ⇒ 不被占位符检测误伤。

    修前示例 summary 以 "<" 开头（落在 _PLACEHOLDER_LEADS 前缀上），照抄
    必被同一张门二次拒收——处方自相矛盾。现在：只缺 summary 的提交拿到
    回执模板后原样重交（summary=示例值、test=真凭证 id），dc 门放行。
    """
    _, tid, task = await _mk_task(env, contract=_dc_contract())
    att_id = await attestation_service.create(
        env["project_id"], agent_id=EXEC, kind="test_run",
        task_id=tid, command_or_url="pytest -q", exit_code=0, stdout="ok",
    )
    # 顶层 summary 与回执 summary 都缺失（最坏现场）⇒ 模板示例值登场
    res = await _preflight(
        env, task,
        {"summary": None, "delivery_contract": {"test": f"test_run:{att_id}"}},
    )
    obj = _extract_template_json(_dc_issue(res)["message"])
    assert obj["summary"] == _DC_SUMMARY_EXAMPLE
    assert obj["test"] == f"test_run:{att_id}"
    # 逐字照抄重交 ⇒ dc 门放行（占位符检测不误伤示例值）
    res2 = await _preflight(env, task, {"summary": None, "delivery_contract": obj})
    assert "delivery_contract_incomplete" not in {
        i["code"] for i in res2["issues"]
    }, res2["issues"]


@pytest.mark.asyncio
async def test_t37_receipt_without_credential_uses_placeholder(env):
    """无凭证 ⇒ 占位符版本 + 跑测指引（不编造 id）。"""
    _, tid, task = await _mk_task(env, contract=_dc_contract())
    res = await _preflight(env, task)
    obj = _extract_template_json(_dc_issue(res)["message"])
    assert obj["test"] == "test_run:<本任务的attestationId>"
    assert "真跑" in _dc_issue(res)["message"]  # 跑测指引在注明里


# ── T40：running 态拒绝附 wait_on 处方 ──────────────────────


def test_t40_running_race_receipt_shape():
    """helper 单元：事实保留 + 处方带完整任务 id（两处引用）+ legacy 兜底。"""
    from hiveweave.tools.tasks.review import _running_race_receipt

    tid = "0123456789abcdef0123456789abcdef"
    task = {"id": tid, "submitted_at": None}
    receipt = _running_race_receipt(task, tid)
    # ① 事实保留
    assert "likely still landing" in receipt
    # ② wait_on 处方（带完整任务 id）+ 唤醒语义
    assert f'wait_on(refs=[{{"kind": "task", "ref": "{tid}"}}])' in receipt
    assert "唤醒" in receipt
    # legacy 模式（默认）等价写法
    assert "commit_turn(phase='waiting'" in receipt
    assert receipt.count(tid) >= 2
    # 回执不隐含时长承诺（对齐 _race_fact_bit 纪律）
    assert "~" not in receipt


@pytest.mark.asyncio
async def test_t40_running_review_rejection_prescribes_wait_on(
    env, monkeypatch
):
    """工具路径：running 态 rework 仍 failed，回执含 wait_on 处方与任务 id。"""
    from hiveweave.tools.tasks.review import review_task_tool, ReviewTaskParams

    ts = TaskService()
    pid = env["project_id"]
    tid = await ts.create_task(
        project_id=pid,
        title="t40 race task",
        description="d",
        creator_id=CREATOR,
        assignee_id=ASSIGNEE,
    )
    await ts.claim_task(pid, tid, ASSIGNEE)
    await ts.start_task(pid, tid)  # → running（提交未落地/仍在落地）
    assert (await ts.get_task(pid, tid))["status"] == "running"

    async def _fake_pid(agent_id: str):
        return pid

    monkeypatch.setattr("hiveweave.tools.helpers.get_project_id", _fake_pid)

    async def _fake_agent_project(agent_id: str):
        return (
            pid if agent_id in (ASSIGNEE, REVIEWER, CREATOR) else None
        )

    monkeypatch.setattr(
        "hiveweave.db.meta.get_agent_project_id", _fake_agent_project
    )

    # running 分支有 2.5s 落库等待——测试里压缩为让出一次事件循环
    real_sleep = asyncio.sleep

    async def _fast_sleep(delay, *a, **kw):
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    result = await review_task_tool(
        ReviewTaskParams(
            taskId=tid, decision="rework", feedback="src/main.py:3 wrong"
        ),
        agent_id=REVIEWER,
        workspace=env["workspace"],
    )
    # ③ 拒绝语义不变：仍 failed
    assert result.success is False
    err = result.error or ""
    assert "likely still landing" in err
    assert f'wait_on(refs=[{{"kind": "task", "ref": "{tid}"}}])' in err
    assert "唤醒" in err
    assert "commit_turn(phase='waiting'" in err
