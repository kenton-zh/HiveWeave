"""TEST_DSH_64 #4/#7/#10 文案域回归（组4：提示词口径与边界文案）。

覆盖：
- #4：四处「shared 契约指向 MAIN docs/」的旧口径全部订正为
  `.hiveweave/shared/`（跨树可读）+「普通 repo 文档 merge 后可见」；
  READ_MISS_HINT 额外带行动指路；dispatch_pin footer 嵌入 MAIN shared
  实存契约清单（mtime 倒序、上限 10、IO 失败静默跳过）。
- #7②：steer/mid-turn 预览截断显式标记（不落库，标记自含出处）。
- #10①：OUT_OF_BOUNDARY_HINT 增补合并时序指路。

clip_with_pointer 单元与 get_tasks 单任务视图见
test_truncation_pointer_dsh64.py / test_get_tasks_single_task_view.py。
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock

import pytest

from hiveweave.prompts.context import build_context_prompt
from hiveweave.prompts.identity import build_identity_prompt
from hiveweave.services.git_worktree import pin_dispatch_message_to_worktree
from hiveweave.util import path_guard
from hiveweave.util.tree_label import READ_MISS_HINT

# TEST_DSH_64 #4 统一口径的关键片段（四处共用）
_CANONICAL = "cross-tree readable"
_CANONICAL_MERGE = "regular repo docs arrive via git merge"


# ── #4① READ_MISS_HINT ────────────────────────────────────────


def test_read_miss_hint_points_to_shared_not_docs():
    """报错回执必须教对路：shared 契约读 `.hiveweave/shared/`，不再指向 docs/。"""
    assert ".hiveweave/shared/" in READ_MISS_HINT
    assert _CANONICAL in READ_MISS_HINT
    assert _CANONICAL_MERGE in READ_MISS_HINT
    # 行动指路（回执文案专属）：同名文件先查 shared/
    assert "Check `.hiveweave/shared/` for the same file" in READ_MISS_HINT
    # 不得再把 shared 契约指向 docs/
    assert "MAIN docs/" not in READ_MISS_HINT
    assert "Shared contracts are MAIN docs/" not in READ_MISS_HINT
    # 既有钉点保持（test_read_multi_tree_lookup / test_shared_cross_tree_read）
    assert "MAIN" in READ_MISS_HINT
    assert "确实不存在" not in READ_MISS_HINT
    assert "hiveweave/reports" not in READ_MISS_HINT


# ── #4② context.py workspace 块 ───────────────────────────────


def test_workspace_block_points_to_shared_not_docs():
    out = build_context_prompt(
        "id", None, None,
        workspace_path="D:/proj/.hiveweave/worktrees/A136",
    )
    assert "Workspace: worktree A136" in out
    assert "Shared contract files: read from `.hiveweave/shared/`" in out
    assert _CANONICAL in out
    assert _CANONICAL_MERGE in out
    assert "MAIN docs/" not in out

    main = build_context_prompt(
        "id", None, None, workspace_path="D:/proj",
    )
    assert "Workspace: MAIN (project root)" in main  # MAIN 行为不变


# ── #4③ identity.py ──────────────────────────────────────────


def test_identity_system_dir_points_to_shared_not_docs():
    text = build_identity_prompt(
        role="developer", role_type="executor", backstory="", name="Robert",
    )
    assert "Shared contract files: read from `.hiveweave/shared/`" in text
    # _normalize_cjk_punct 会把 em dash 归一成 "-"，故只断 dash 前的短语
    assert ".hiveweave/shared/` (cross-tree readable" in text
    assert "repo docs arrive via git merge" in text
    assert "Shared contracts teammates read live on MAIN" not in text
    # 机制总览 §5 的混合口径（MAIN docs/ 与 shared 团队共享）一并订正
    assert "MAIN `docs/` 与 `.hiveweave/shared/` 团队共享" not in text


# ── #4④ dispatch_pin footer + MAIN shared 契约清单 ───────────


def _make_layout(tmp_path, shared_files: int = 0):
    root = tmp_path / "proj"
    wt = root / ".hiveweave" / "worktrees" / "A005"
    wt.mkdir(parents=True, exist_ok=True)
    if shared_files:
        shared = root / ".hiveweave" / "shared"
        shared.mkdir(parents=True, exist_ok=True)
        for i in range(shared_files):
            (shared / f"c{i:02d}.md").write_text("x", encoding="utf-8")
            old = (shared / f"c{i:02d}.md").stat().st_mtime - i * 60
            os.utime(shared / f"c{i:02d}.md", (old, old))
    return root, wt


def test_dispatch_pin_footer_shared_wording(tmp_path):
    _root, wt = _make_layout(tmp_path)
    msg = pin_dispatch_message_to_worktree(
        "do it", short_id="A005", worktree_path=str(wt),
    )
    assert "Reads: shared contract files from `.hiveweave/shared/`" in msg
    assert _CANONICAL in msg
    assert _CANONICAL_MERGE in msg
    assert "MAIN docs/" not in msg
    # 写侧指引保持不变
    assert "write_file to .hiveweave/shared/<file>" in msg


def test_dispatch_pin_embeds_main_shared_contract_listing(tmp_path):
    _root, wt = _make_layout(tmp_path)
    shared = wt.parents[1] / "shared"
    shared.mkdir(parents=True, exist_ok=True)
    (shared / "a-old.md").write_text("old", encoding="utf-8")
    (shared / "b-new.md").write_text("new", encoding="utf-8")
    old = (shared / "a-old.md").stat().st_mtime - 600
    os.utime(shared / "a-old.md", (old, old))

    msg = pin_dispatch_message_to_worktree(
        "do it", short_id="A005", worktree_path=str(wt),
    )
    assert "Shared contracts currently on MAIN" in msg
    assert ".hiveweave/shared/a-old.md" in msg
    assert ".hiveweave/shared/b-new.md" in msg
    # mtime 倒序：新文件排前
    assert msg.index("b-new.md") < msg.index("a-old.md")


def test_dispatch_pin_listing_caps_at_ten(tmp_path):
    _root, wt = _make_layout(tmp_path, shared_files=12)
    shared = wt.parents[1] / "shared"
    msg = pin_dispatch_message_to_worktree(
        "do it", short_id="A005", worktree_path=str(wt),
    )
    listed = [ln for ln in msg.splitlines() if ln.strip().startswith("- .hiveweave/shared/")]
    assert len(listed) == 10


def test_dispatch_pin_listing_silent_when_shared_missing(tmp_path):
    _root, wt = _make_layout(tmp_path)  # 无 shared 目录
    msg = pin_dispatch_message_to_worktree(
        "do it", short_id="A005", worktree_path=str(wt),
    )
    assert "Shared contracts currently on MAIN" not in msg
    assert "[WORKTREE PIN]" in msg  # footer 本体不受影响


# ── #7② steer 截断显式标记 ───────────────────────────────────


class _FakeAgent:
    def __init__(self):
        self.status = type("S", (), {"value": "processing"})()
        self._steer_q = asyncio.Queue()
        self.steer = AsyncMock(return_value={"steer": True})


def _msg(content: str, from_id: str = "boss") -> dict:
    return {"wake": 1, "from_agent_id": from_id,
            "message_type": "normal", "message": content}


async def _steer_once(monkeypatch, pending: list[dict]) -> str:
    from hiveweave.agents import trigger as trig

    svc = AsyncMock()
    svc.get_pending_messages = AsyncMock(return_value=list(pending))
    svc.get_undelivered_background = AsyncMock(return_value=[])
    monkeypatch.setattr(trig, "_inbox_service", svc)
    monkeypatch.setattr(trig, "_steer_inbox_last", {})
    agent = _FakeAgent()
    ok = await trig._try_steer_busy_inbox(agent, "a1")
    assert ok is True
    return agent.steer.await_args.args[0]


@pytest.mark.asyncio
async def test_steer_body_truncation_appends_marker(monkeypatch):
    long_body = "前" * 300 + "尾部锚点TAILXYZ"
    text = await _steer_once(monkeypatch, [_msg(long_body)])
    assert "…[truncated — full message in your inbox]" in text
    assert "尾部锚点TAILXYZ" not in text  # 240 之后真被截掉
    assert "前" * 240 in text  # 保留前缀


@pytest.mark.asyncio
async def test_steer_overall_truncation_appends_marker_and_keeps_budget(monkeypatch):
    pending = [_msg("x" * 300, from_id=f"a{i}") for i in range(5)]
    from hiveweave.agents import trigger as trig

    text = await _steer_once(monkeypatch, pending)
    assert "…[truncated — full message in your inbox]" in text
    assert len(text) <= trig._STEER_TOTAL_LIMIT


@pytest.mark.asyncio
async def test_steer_short_body_no_marker(monkeypatch):
    text = await _steer_once(monkeypatch, [_msg("把 E2 复测一下")])
    assert "truncated" not in text
    assert "把 E2 复测一下" in text


# ── #10① OUT_OF_BOUNDARY_HINT 时序指路 ───────────────────────


def test_out_of_boundary_hint_has_merge_timing_guidance():
    hint = path_guard.OUT_OF_BOUNDARY_HINT
    # 既有钉点（test_path_guard_boundary.py）
    assert "boundary_root" in hint
    # 新增时序指路
    assert "MAIN 验证" in hint
    assert "git_worktree_merge(dryRun=true)" in hint
    assert "assignee 代跑" in hint
