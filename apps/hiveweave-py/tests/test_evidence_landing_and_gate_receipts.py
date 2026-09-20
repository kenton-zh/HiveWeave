"""TEST_DSH_64 修复回归：证据落地判据 + 门禁回执事实 + 合并提醒成对清理。

## 背景（64 现场三个 P0/P1）

1. **#1 证据滞留（P0）**：64 收官时终验证据 4 份 md 只 checkpoint 到
   ``hw/A140/work`` 从未合 MAIN，``.hiveweave/reports/73c67a58/`` 只剩
   2 张 untracked PNG；``_delivery_blockers`` 三条旧判据零触文件系统 ⇒
   ship 照常触发。现补第 4 判据 ``EVIDENCE_NOT_LANDED``：已 approved/
   closed 的 VERIFY 任务在 ``evidence.files_changed`` 里声称的平台自管
   路径（``.hiveweave/reports/`` / ``.hiveweave/shared/``）必须真实存在
   于 **MAIN 工作区磁盘**。口径 =「磁盘存在」而非「git 已跟踪」——
   browse_main 直写的截图是合法 untracked，按 git 跟踪判会误伤。
2. **#6 closed 不短路**：身份门排在状态检查之前，closed 任务上 assignee
   先撞身份门带不出「任务已关闭」的状态事实。现终态短路**放在身份门
   之前**（挨着 archived 检查），回 ``ok``（不进失败签名池，设计意图）。
3. **#8 [MERGE PENDING] 永不清除**：approve 时 ``_inject_merge_pending_wake``
   发出，merge 成功路径从不清除 ⇒ owner 收件箱堆假账（64 现场潮汐 4 个
   清账回合实测 17 LLM + 17 tool）。现 merge 成功后按任务 id/分支名指纹
   supersede（不变式：通知发出与清理必须成对）。
4. 附带：VERIFY approve 早退回执补 worktree 领先事实（ahead>0 时明示
   「分支领先 main N 提交，请 merge 或显式 waive_merge」）；reconcile
   stranded 扫描补 ``hw/<sid>/work`` 兜底（只报告不重开，见
   test_stranded_candidates_work_branch_fallback）。
"""

from __future__ import annotations

import contextlib
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services.git_worktree import reconcile as recon_mod
from hiveweave.services.tasks.verify import VERIFY_KIND
from hiveweave.tools import misc_tools as mt
from hiveweave.tools.tasks.review import review_task_tool, ReviewTaskParams

PROJECT_ID = "test-evidence-landing"
WS_AGENT = "delivery-evidence-ceo"      # env 路由前缀 delivery-*（见 fixture）
CREATOR = "evidence-creator-1"
EXEC = "evidence-exec-1"
REVIEWER = "evidence-reviewer-1"
CALLER_X = "merge-caller-x"


# ── 夹具：真实 per-project DB + meta 路由 mock（照抄
#    tests/test_delivery_state_gate.py 的 Windows-safe 形态：先关缓存
#    连接再删目录，打开的文件句柄会挡住 tempdir 删除） ────────────────


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        async def fake_get_agent_project_id(aid: str):
            if aid.startswith("delivery-") or aid in (
                CREATOR, EXEC, REVIEWER, CALLER_X,
            ):
                return PROJECT_ID
            return None

        project_db._agent_cache.pop(WS_AGENT, None)

        with patch("hiveweave.db.meta.get_project_workspace",
                   fake_get_project_workspace), \
             patch("hiveweave.db.meta.get_agent_project_id",
                   fake_get_agent_project_id):
            yield {"project_id": PROJECT_ID, "workspace": workspace_path}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
        for aid in (WS_AGENT, CREATOR, EXEC, REVIEWER, CALLER_X):
            project_db._agent_cache.pop(aid, None)


async def _conn(env):
    return await project_db.get_project_db_by_project_id(env["project_id"])


async def _insert_task(
    env,
    task_id: str,
    *,
    status: str = "closed",
    kind: str | None = VERIFY_KIND,
    evidence: dict | None = None,
    updated_at: int | None = None,
    assignee_id: str = EXEC,
) -> None:
    """直接插任务行（不走状态机 —— 本文件只测判据，不测流转）。"""
    conn = await _conn(env)
    now_ms = int(time.time() * 1000)
    await conn.execute(
        "INSERT INTO tasks (id, project_id, title, creator_id, assignee_id, "
        "status, kind, evidence, created_at, updated_at, closed_at, "
        "is_archived) VALUES (?,?,?,?,?,?,?,?,?,?,?,0)",
        [
            task_id, env["project_id"], "VERIFY: t", CREATOR, assignee_id,
            status, kind,
            json.dumps(evidence or {}),
            updated_at if updated_at is not None else now_ms,
            updated_at if updated_at is not None else now_ms,
            now_ms,
        ],
    )
    await conn.commit()


async def _insert_inbox(env, row_id: str, to_id: str, message: str) -> None:
    conn = await _conn(env)
    await conn.execute(
        "INSERT INTO inbox (id, from_agent_id, to_agent_id, message, read, "
        "created_at) VALUES (?,?,?,?,0,1)",
        [row_id, "system", to_id, message],
    )
    await conn.commit()


async def _inbox_read(env, row_id: str) -> int:
    conn = await _conn(env)
    cur = await conn.execute("SELECT read FROM inbox WHERE id = ?", [row_id])
    row = await cur.fetchone()
    await cur.close()
    return int(row["read"]) if row else -1


# ── 1. EVIDENCE_NOT_LANDED 判据 ──────────────────────────────


async def test_blocker_reports_missing_evidence_file(env):
    """① VERIFY 任务声称的 reports 路径不在 MAIN 磁盘 ⇒ blocked + 政策码。"""
    tid = "evid-missing-0001"
    await _insert_task(
        env, tid,
        evidence={"files_changed": [".hiveweave/reports/73c67a58/final.md"]},
    )

    blockers = await mt._delivery_blockers(WS_AGENT)
    codes = [b["code"] for b in blockers]
    assert "EVIDENCE_NOT_LANDED" in codes, blockers
    bit = next(b for b in blockers if b["code"] == "EVIDENCE_NOT_LANDED")
    # message 列缺失文件 + 所属任务 id + 出路（merge 或共享空间补齐）
    assert ".hiveweave/reports/73c67a58/final.md" in bit["message"]
    assert tid[:8] in bit["message"]
    assert "merge 产出分支" in bit["message"]


async def test_blocker_tolerates_untracked_file_on_main_disk(env):
    """② 文件在 MAIN 磁盘存在但 untracked ⇒ 不触发（口径=磁盘存在）。

    复刻 64 现场：browse_main 直写的截图从不过 git，若按「git 已跟踪」
    判会误伤合法 untracked 证据。这里连 git 仓都不建 —— 磁盘存在即放行。
    """
    tid = "evid-untrack-0001"
    await _insert_task(
        env, tid,
        evidence={"files_changed": [".hiveweave/reports/73c67a58/shot.png"]},
    )
    reports_dir = (
        Path(env["workspace"]) / ".hiveweave" / "reports" / "73c67a58"
    )
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "shot.png").write_text("fake png", encoding="utf-8")

    blockers = await mt._delivery_blockers(WS_AGENT)
    assert "EVIDENCE_NOT_LANDED" not in [b["code"] for b in blockers], blockers


async def test_blocker_ignores_non_platform_and_non_verify(env):
    """③ 无平台前缀 / 非 VERIFY 任务 / 超 7 天 ⇒ 不触发。"""
    # 带缺失的业务代码路径（不归本判据管——走 merge 门）
    await _insert_task(
        env, "evid-bizpath-001",
        evidence={"files_changed": ["src/app/never_landed.py"]},
    )
    # 普通（kind=NULL）任务带缺失 reports 路径 —— 只扫 VERIFY 类
    await _insert_task(
        env, "evid-plain-00001", kind=None,
        evidence={"files_changed": [".hiveweave/reports/x/gone.md"]},
    )
    # VERIFY 但证据超 7 天（updated_at 窗口护栏）
    week_ago = int(time.time() * 1000) - 8 * 86400 * 1000
    await _insert_task(
        env, "evid-stale-0001", updated_at=week_ago,
        evidence={"files_changed": [".hiveweave/reports/y/gone.md"]},
    )

    blockers = await mt._delivery_blockers(WS_AGENT)
    assert "EVIDENCE_NOT_LANDED" not in [b["code"] for b in blockers], blockers


# ── 2. VERIFY approve 早退回执的领先事实 ─────────────────────


def _verify_approve_patches(env, ahead):
    """VERIFY approve 工具链路的前序门禁 mock（只留待测的回执分支）。"""
    wt_path = str(
        Path(env["workspace"]) / ".hiveweave" / "worktrees" / "A140"
    )
    return (
        patch("hiveweave.services.worktree_review.review_worktree_gate",
              new=AsyncMock(return_value=(None, {}))),
        patch("hiveweave.services.worktree_review.check_evidence_verifiable",
              new=AsyncMock(return_value=None)),
        patch("hiveweave.services.attestation.required_attestation_kinds",
              new=MagicMock(return_value=[])),
        patch("hiveweave.services.attestation.reviewer_required_kinds",
              new=MagicMock(return_value=[])),
        patch("hiveweave.services.attestation.has_valid_waiver",
              new=AsyncMock(return_value=False)),
        patch("hiveweave.services.attestation.check_verify_baseline",
              new=AsyncMock(return_value=None)),
        patch("hiveweave.services.code_audit.drop_code_audit_kind_if_soft",
              new=AsyncMock(return_value=([], False))),
        patch("hiveweave.services.worktree_review.agent_worktree_path",
              new=AsyncMock(return_value=wt_path)),
        patch("hiveweave.services.worktree_review.project_main_workspace",
              new=AsyncMock(return_value=env["workspace"])),
        patch("hiveweave.services.worktree_review.worktree_commits_ahead",
              new=AsyncMock(return_value=ahead)),
        patch("hiveweave.services.org.OrgService.resolve_agent",
              new=AsyncMock(return_value={"id": EXEC, "short_id": "A140"})),
        patch("hiveweave.services.inbox.InboxService.send_message",
              new=AsyncMock()),
        patch("hiveweave.agents.trigger.trigger_subordinate", new=AsyncMock()),
        patch("hiveweave.tools.helpers.get_project_id",
              new=AsyncMock(return_value=env["project_id"])),
    )


async def _mk_reviewing_verify_task(env) -> str:
    """建 VERIFY 任务并推进到 reviewing（照抄
    tests/test_idle_architecture_p0.py::test_verify_approve_closes_parent
    的推进序列）。"""
    from hiveweave.services.task import TaskService

    ts = TaskService()
    pid = env["project_id"]
    tid = await ts.create_task(
        pid, "VERIFY: 64 终验", "verify",
        creator_id=CREATOR, assignee_id=EXEC,
        source="system", kind=VERIFY_KIND,
    )
    await ts.claim_task(pid, tid, EXEC)
    await ts.start_task(pid, tid)
    await ts.submit_task(
        pid, tid,
        evidence={"verdict": "PASS", "tests_passed": True, "test_output": "ok"},
    )
    await ts.start_review(pid, tid, reviewer_id=REVIEWER)
    return tid


async def test_verify_approve_receipt_states_branch_ahead(env):
    """④a ahead>0 ⇒ 回执改述领先事实 + merge/waive 出路（approve 仍成功）。"""
    tid = await _mk_reviewing_verify_task(env)

    with contextlib.ExitStack() as stack:
        for cm in _verify_approve_patches(env, ahead=4):
            stack.enter_context(cm)
        result = await review_task_tool(
            ReviewTaskParams(task_id=tid, decision="approve"),
            REVIEWER, env["workspace"],
        )

    assert result.success is True, result.error
    assert "hw/A140/work 领先 main 4 提交" in result.output
    assert "waive_merge" in result.output
    assert "证据滞留分支" in result.output
    # 误导性旧文案不得与领先事实并存
    assert "No git_worktree_merge needed" not in result.output
    # approve 本身仍成功（信息性回执）：VERIFY approve 同秒自动关闭是
    # 既有平台行为（64 取证 task_events 14:29:55.703 approved → .719 closed）
    from hiveweave.services.task import TaskService

    task = await TaskService().get_task(env["project_id"], tid)
    assert task["status"] == "closed"


async def test_verify_approve_receipt_zero_ahead_keeps_original(env):
    """④b ahead=0 ⇒ 保持原文案（不画蛇添足）。"""
    tid = await _mk_reviewing_verify_task(env)

    with contextlib.ExitStack() as stack:
        for cm in _verify_approve_patches(env, ahead=0):
            stack.enter_context(cm)
        result = await review_task_tool(
            ReviewTaskParams(task_id=tid, decision="approve"),
            REVIEWER, env["workspace"],
        )

    assert result.success is True, result.error
    assert "No git_worktree_merge needed" in result.output
    assert "领先" not in result.output


# ── 3. closed 终态短路（身份门之前） ─────────────────────────


async def test_closed_task_short_circuits_before_identity_gate(env):
    """⑤ closed 任务上 assignee（会撞自审身份门的人）approve ⇒ ok + 已关闭。

    短路必须在身份门之前：否则状态事实「已关闭」被「Self-review is
    forbidden」挡住，64 现场的过期重试就被老文案引去补身份资格。
    """
    conn = await _conn(env)
    tid = "closed-short-0001"
    now_ms = int(time.time() * 1000)
    await conn.execute(
        "INSERT INTO tasks (id, project_id, title, creator_id, assignee_id, "
        "status, evidence, created_at, updated_at, closed_at, is_archived) "
        "VALUES (?,?,?,?,?,'closed','{}',?,?,?,0)",
        [tid, env["project_id"], "已关闭任务", CREATOR, EXEC,
         now_ms, now_ms, now_ms],
    )
    await conn.commit()

    with patch("hiveweave.tools.helpers.get_project_id",
               new=AsyncMock(return_value=env["project_id"])):
        result = await review_task_tool(
            ReviewTaskParams(
                task_id=tid, decision="approve", feedback="迟到的重试"
            ),
            EXEC, env["workspace"],  # caller == assignee：旧代码先撞自审门
        )

    assert result.success is True, (result.error, result.output)
    assert "关闭" in result.output
    assert "未做任何状态变更" in result.output
    # 身份门文案不得出现 —— 证明短路排在身份门之前
    assert "Self-review is forbidden" not in (result.error or "")
    assert "Self-review is forbidden" not in result.output


# ── 4. merge 成功 ⇒ [MERGE PENDING] 成对清理 ─────────────────


async def test_merge_success_supersedes_merge_pending_inbox(env):
    """⑥ merge 成功后：本任务 [MERGE PENDING] 被清，其它任务/前缀不受累。"""
    from hiveweave.services.task import TaskService

    ts = TaskService()
    pid = env["project_id"]
    tid = await ts.create_task(
        pid, "滞留证据的里程碑", "d", creator_id=CREATOR, assignee_id=EXEC
    )
    other_tid = await ts.create_task(
        pid, "别的待合并任务", "d", creator_id=CREATOR, assignee_id=EXEC
    )

    owner = CREATOR  # wake 接收者 = merge owner（creator）
    await _insert_inbox(
        env, "mp-hit", owner,
        f"[MERGE PENDING] Task '滞留证据的里程碑' ({tid}) is approved and "
        f"needs git_worktree_merge(branchName='hw/A140/work'). "
        f"YOU (task creator/coordinator, merge owner) must merge. "
        f"reason=approved_needs_merge",
    )
    await _insert_inbox(
        env, "mp-other", owner,
        f"[MERGE PENDING] Task '别的待合并任务' ({other_tid}) is approved "
        f"and needs git_worktree_merge(branchName='hw/ZZZ/work').",
    )
    await _insert_inbox(env, "wd-other", owner, "[TASK WATCHDOG] 别的催办")

    # caller ≠ owner（第三方代合并）：owner 要靠 DB 反查补齐
    await mt._supersede_merge_pending_after_merge(
        pid, CALLER_X,
        task_id=None,
        branches=[f"hw/OWN1/t-{tid[:8]}", "hw/A140/work"],
    )

    assert await _inbox_read(env, "mp-hit") == 1, "本任务待合并提醒应被清"
    assert await _inbox_read(env, "mp-other") == 0, "其它任务的提醒不得误清"
    assert await _inbox_read(env, "wd-other") == 0, "非合并前缀不得误清"


# ── 5. reconcile stranded 扫描的 hw/<sid>/work 兜底 ──────────


async def test_stranded_candidates_work_branch_fallback(monkeypatch):
    """死注释补实现：无合并事实的任务追加 hw/<sid>/work 候选（64 现场形态
    t-<8> 命名不存在）；有合并事实的任务**不**追加（只报告不重开的拍板
    2026-07-28 Sage W1，语义不变）。"""

    async def fake_git(args, cwd=None):
        if args[:2] == ["branch", "--list"]:
            spec = args[2] if len(args) > 2 else ""
            if spec == "hw/%/t-abcdef12":
                return True, ""  # t-<8> 命名不存在（64 现场）
            if spec == "hw/A140/work":
                return True, "hw/A140/work\nhw/A140/t-other"
            return True, ""
        return True, ""

    monkeypatch.setattr(recon_mod, "_git", fake_git)
    sids = {EXEC: "A140"}
    common = dict(
        tid="abcdef12-ab-cd-ef-gh",
        assignee=EXEC,
        sid_by_agent=sids,
        workspace_path="/tmp/ws",
    )

    no_merge = await recon_mod._stranded_scan_candidates(has_merge=False,
                                                         **common)
    assert no_merge == ["hw/A140/work"], no_merge

    with_merge = await recon_mod._stranded_scan_candidates(has_merge=True,
                                                           **common)
    assert with_merge == [], "有合并事实的任务不得追加 work 分支候选"

    # assignee short_id 解析不到 / git 失败 ⇒ 无候选（不猜）
    no_sid = await recon_mod._stranded_scan_candidates(
        has_merge=False, tid=common["tid"], assignee="ghost",
        sid_by_agent=sids, workspace_path="/tmp/ws",
    )
    assert no_sid == []

    async def broken_git(args, cwd=None):
        return False, "git error"

    monkeypatch.setattr(recon_mod, "_git", broken_git)
    failed = await recon_mod._stranded_scan_candidates(has_merge=False,
                                                       **common)
    assert failed == []
