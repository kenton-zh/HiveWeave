"""批25 修复2（D 桶处方收紧，T37 式 top3）：拒收回执补处方，不动门语义。

a) wait_on「不当挂起」类拒 —— 处方补「等任务完成用 kind=task；等他人
   先 ask/question 发问（kind=user 等用户答复）」；
b) merge 门拒 nothing_to_merge —— 补下一步精确处方（未提交先
   git_worktree_checkpoint 再重试 merge）。注：「未批准先 merge」自检门
   实际在 ``tools/misc_tools.py::_check_self_merge_gate``（批24 域），
   其文案已带 review_task(approve) 处方，本批不动；conflict 回执经
   ``format_merge_conflict_message``（MERGE_CONFLICT_HINT）已带完整
   rework 路径，本批不重复加；
c) 跨级沟通拒（org_span）—— 已带改道路径的保持，补「正确通道一句话」。
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

PROJECT_ID = "test-b25-project"
AGENT_ID = "test-b25-agent"


@pytest.fixture
def auto_mode(monkeypatch):
    from hiveweave.config import settings

    monkeypatch.setattr(settings, "turn_exit_mode", "auto")
    return "auto"


@pytest.fixture
async def env():
    """per-project DB 环境（test_t16_turn_exit_auto 同款夹具形态）。"""
    from hiveweave.db import project as project_db
    from hiveweave.db.project import ensure_project_db
    from hiveweave.services import wait_contract as wait_contract_module

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        wait_contract_module._migrated.clear()
        project_db._agent_cache[AGENT_ID] = workspace_path
        await ensure_project_db(workspace_path)

        with patch(
            "hiveweave.db.meta.get_project_workspace",
            fake_get_project_workspace,
        ):
            yield {"project_id": PROJECT_ID}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
            project_db._agent_cache.pop(AGENT_ID, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


def _ctx():
    return SimpleNamespace(project_id=PROJECT_ID)


# ── a) wait_on 不当挂起拒收：处方点名正确姿势 ───────────────────────


async def test_wait_on_rejection_remedy_names_task_and_ask(auto_mode, env):
    """不当挂起拒收回执的处方必须含：等任务完成用 kind=task；等他人先
    ask/question（kind=user 等用户答复）。门语义不变（仍整批拒绝）。"""
    from hiveweave.services.wait_contract import wait_contract_service
    from hiveweave.tools.wait_tools import WaitOnParams, wait_on_tool

    r = await wait_on_tool(
        WaitOnParams(refs=[{"kind": "task", "ref": "ghost-task"}]),
        AGENT_ID,
        "",
        ctx=_ctx(),
    )
    assert r.success is False
    assert "ghost-task" in (r.error or "")
    assert "不当挂起" in (r.error or "")
    remedy = str(r.extra.get("remedy") or "")
    assert "kind=task" in remedy, remedy
    assert "ask" in remedy or "question" in remedy, remedy
    assert "kind=user" in remedy, remedy
    # 门语义不变：不得落账
    waits = await wait_contract_service.list_active(PROJECT_ID, AGENT_ID)
    assert waits == []


# ── c) 跨级沟通拒：改道路径保持 + 正确通道一句话 ────────────────────


def _fake_org_service(sender: dict, target: dict):
    class _FakeOrg:
        async def get_agent(self, agent_id: str):
            return sender if agent_id == "sender-1" else target

    return _FakeOrg()


async def test_cross_level_message_rejection_keeps_path_and_names_channel():
    """跨级沟通拒：既有改道路径（上级转发/升级/transfer）保持，
    并补「正确通道」一句话（直属下属/同级/直属上级可直接消息/ask）。"""
    from hiveweave.services.org_span import validate_message_span

    sender = {"id": "sender-1", "parent_id": "mid-1", "name": "coordinator"}
    # 目标既非直属下属、非上级、也非同级（不同父）
    target = {"id": "leaf-9", "parent_id": "other-mid", "name": "Leaf"}
    err = await validate_message_span(
        "sender-1", "leaf-9", org_service=_fake_org_service(sender, target)
    )
    assert err is not None
    assert "拒绝跨级沟通" in err
    # 改道路径保持
    assert "直属上级转发" in err
    assert "transfer" in err
    # 新增正确通道一句话
    assert "正确通道" in err
    assert "直属上级" in err
    assert "ask" in err


async def test_cross_level_dispatch_rejection_no_channel_duplication():
    """跨级派活拒：B5 处方（先派直属中层）保持；不重复加沟通通道句
    （channel_hint 只对非派活动作生效）。"""
    from hiveweave.services.org_span import validate_dispatch_span

    sender = {"id": "sender-1", "parent_id": "mid-1", "name": "coordinator"}
    target = {"id": "leaf-9", "parent_id": "other-mid", "name": "Leaf"}
    err = await validate_dispatch_span(
        "sender-1", "leaf-9", org_service=_fake_org_service(sender, target)
    )
    assert err is not None
    assert "拒绝跨级派活" in err
    assert "先向你的直属下属" in err  # B5 处方保持
    assert "正确通道" not in err  # 不与 B5 处方重复


# ── b) merge 门拒 nothing_to_merge：下一步精确处方 ──────────────────


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return (r.stdout or "").strip()


def _init_repo(root: Path) -> None:
    _git(root, "init")
    _git(root, "config", "user.email", "t@t.com")
    _git(root, "config", "user.name", "t")
    (root / "README.md").write_text("hi\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "init")
    _git(root, "branch", "-M", "main")


@pytest.mark.asyncio
async def test_merge_nothing_to_merge_receipt_has_next_step(tmp_path: Path):
    """outcome=nothing_to_merge 回执补下一步：未提交先 checkpoint 再重试
    merge；既有事实陈述与 outcome token 语义不动（T22-① 保持）。"""
    from hiveweave.services.git_worktree import GitWorktreeService

    main = tmp_path / "repo"
    main.mkdir()
    _init_repo(main)
    _git(main, "branch", "hw/A005/work")  # 与 main 同 tip → ahead=0

    gwt = GitWorktreeService()
    result = await gwt.merge_by_branch(
        str(main), "hw/A005/work", target_branch="main"
    )
    assert result.get("success") is True, result
    assert result.get("already_up_to_date") is True
    msg = str(result.get("message") or "")
    assert msg.startswith("outcome=nothing_to_merge:"), msg
    # T22-① 语义保持：没有发生任何合并，禁称 COMPLETE
    assert "COMPLETE" not in msg, msg
    assert "没有发生任何合并" in msg, msg
    # 批25 新增：下一步精确处方
    assert "git_worktree_checkpoint" in msg, msg
    assert "重试 merge" in msg, msg
