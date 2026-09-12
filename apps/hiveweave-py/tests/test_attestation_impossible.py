"""attestation_impossible — 报告 Layer 6「歪招典藏」第 4 行回归测试。

原始判据（逐字）：CEO 用 ``waive_attestation(reasonKind=tool_failure)`` 越过
``code_audit`` 凭证门（任务已 merge 后 ``request_code_audit`` 因 diff 为空拒发
PASS）。缺口：**「凭证在物理上无法签发」与「质量不够所以豁免」是两件事，却
共用同一个豁免入口**，且豁免一旦发生不留结构性事实位。

修复（本文件钉住的行为）：
1. ``detect_attestation_impossible`` 把「物理无法签发」判成结构化事实
   （reason=tool_limited），且 fail-closed（无 worktree / git 不可判 / 有前科
   → None，保持原门禁）；
2. submit 门禁**自己消化**该事实：命中时把该 kind 从本轮必需清单剔除，默认
   路径不再需要人手动豁免；
3. 事实位落 ``tool_attestations.kind='attestation_impossible'`` + 同步
   ``tasks.evidence``，可事后审计；
4. 人工豁免路径（``waive_attestation``）保留不动。
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import attestation as att_module
from hiveweave.services import task as task_module
from hiveweave.services.attestation import (
    ATTESTATION_IMPOSSIBLE_EVIDENCE_KEY,
    ATTESTATION_IMPOSSIBLE_KIND,
    ATTESTATION_IMPOSSIBLE_REASON_TOOL_LIMITED,
    CODE_AUDIT_SOFT_FAIL_KIND,
    attestation_service,
    detect_attestation_impossible,
    drop_impossible_attestation_kinds,
    evidence_has_attestation_impossible,
    get_attestation_impossible,
    get_code_audit_soft_fail,
    record_attestation_impossible,
    record_code_audit_soft_fail,
    resolve_impossible_kind,
    resolve_soft_fail_kind,
)
from hiveweave.tools.tasks.submit import SubmitTaskParams, _submit_preflight

PROJECT_ID = "test-attestation-impossible"
AGENT_ID = "agent-impossible"
COORD_ID = "coord-impossible"
TASK_ID = "11111111-1111-1111-1111-111111111111"


# ── harness ─────────────────────────────────────────────────


def _run_git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _init_repo(path: str, *, branch: str = "main") -> None:
    """真实 git 仓库（detect 的判据是 git 事实，mock 会把判据替换掉）。"""
    _run_git(path, "init")
    _run_git(path, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
    _run_git(path, "config", "user.email", "t@example.com")
    _run_git(path, "config", "user.name", "tester")
    _run_git(path, "config", "core.autocrlf", "false")
    (Path(path) / "README.md").write_text("hello\n", encoding="utf-8")
    _run_git(path, "add", "-A")
    _run_git(path, "commit", "-m", "init")


def _commit_divergence(path: str, rel: str, body: str = "x\n") -> None:
    """在 main 之上造一个分支提交（相对 MAIN 有 diff）。"""
    _run_git(path, "checkout", "-b", "feature")
    f = Path(path) / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body, encoding="utf-8")
    _run_git(path, "add", "-A")
    _run_git(path, "commit", "-m", "feature work")


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())
        # git worktree 放在 workspace 的**子目录**：项目库落在
        # <workspace>/.hiveweave/data.db，若把库目录本身当 worktree，
        # untracked 的库文件会让 `git status --porcelain` 永远非空。
        repo_path = str((Path(tmpdir) / "repo").resolve())
        Path(repo_path).mkdir(parents=True, exist_ok=True)
        _init_repo(repo_path)

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        att_module._migrated.clear()
        from hiveweave.services import code_audit as code_audit_mod

        code_audit_mod.reset_ledger(AGENT_ID)

        with patch(
            "hiveweave.db.meta.get_project_workspace",
            fake_get_project_workspace,
        ):
            yield {"workspace_path": workspace_path, "repo_path": repo_path}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


def _task(**over) -> dict:
    task = {
        "id": TASK_ID,
        "title": "写树代码任务",
        "description": "",
        "tags": ["code_audit", "test_run"],
        "evidence": {},
        "policy_id": "code_audit_unit",
        "assignee_id": AGENT_ID,
        "creator_id": "creator-1",
        "status": "running",
    }
    task.update(over)
    return task


# ── detect_attestation_impossible（单元）────────────────────


@pytest.mark.asyncio
async def test_detect_none_when_code_audit_not_required(env):
    """不需要 code_audit 的 policy 不产生事实位（唯一判据：needed）。"""
    assert (
        await detect_attestation_impossible(
            PROJECT_ID, AGENT_ID, TASK_ID, frozenset({"test_run"})
        )
        is None
    )


@pytest.mark.asyncio
async def test_detect_fires_on_no_auditable_diff(env):
    """分支相对 MAIN 无 diff（已 merge）⇒ 结构上发不出 PASS ⇒ 事实位。"""
    with patch(
        "hiveweave.services.worktree_review.agent_worktree_path",
        AsyncMock(return_value=env["repo_path"]),
    ):
        info = await detect_attestation_impossible(
            PROJECT_ID, AGENT_ID, TASK_ID, frozenset({"code_audit"})
        )
    assert info is not None
    assert info["reason"] == ATTESTATION_IMPOSSIBLE_REASON_TOOL_LIMITED
    assert info["need"] == "code_audit"
    assert info["detail"] == "no_auditable_diff"


@pytest.mark.asyncio
async def test_detect_none_when_diff_present(env):
    """反向对照：唯一差别是「有 diff」——不得产生事实位。"""
    _commit_divergence(env["repo_path"], "src/a.py")
    with patch(
        "hiveweave.services.worktree_review.agent_worktree_path",
        AsyncMock(return_value=env["repo_path"]),
    ):
        info = await detect_attestation_impossible(
            PROJECT_ID, AGENT_ID, TASK_ID, frozenset({"code_audit"})
        )
    assert info is None


@pytest.mark.asyncio
async def test_detect_none_when_worktree_missing(env):
    """无法定位 worktree → 不可证 → fail-closed（保持原门禁 + 人工豁免）。"""
    with patch(
        "hiveweave.services.worktree_review.agent_worktree_path",
        AsyncMock(return_value=None),
    ):
        assert (
            await detect_attestation_impossible(
                PROJECT_ID, AGENT_ID, TASK_ID, frozenset({"code_audit"})
            )
            is None
        )


@pytest.mark.asyncio
async def test_detect_none_when_base_branch_unresolvable(env):
    """基准分支不可解析（git 不可判）→ None，不得凭「空输出」自行放行。"""
    with (
        patch(
            "hiveweave.services.worktree_review.agent_worktree_path",
            AsyncMock(return_value=env["repo_path"]),
        ),
        patch(
            "hiveweave.services.git_worktree._resolve_base_branch",
            AsyncMock(return_value=None),
        ),
    ):
        assert (
            await detect_attestation_impossible(
                PROJECT_ID, AGENT_ID, TASK_ID, frozenset({"code_audit"})
            )
            is None
        )


@pytest.mark.asyncio
async def test_detect_none_when_prior_issues(env):
    """同任务上一轮 code_audit=ISSUES（回滚前科）→ 不得自动消化。"""
    await attestation_service.create(
        PROJECT_ID,
        agent_id=AGENT_ID,
        kind="code_audit",
        task_id=TASK_ID,
        exit_code=1,
        command_or_url="[verdict=ISSUES] high=2",
        stdout="VERDICT: ISSUES",
    )
    with patch(
        "hiveweave.services.worktree_review.agent_worktree_path",
        AsyncMock(return_value=env["repo_path"]),
    ):
        assert (
            await detect_attestation_impossible(
                PROJECT_ID, AGENT_ID, TASK_ID, frozenset({"code_audit"})
            )
            is None
        )


@pytest.mark.asyncio
async def test_detect_none_when_prior_pass(env):
    """已有 PASS 凭证 → 门禁本就不缺该 kind → 不记伪事实。"""
    await attestation_service.create(
        PROJECT_ID,
        agent_id=AGENT_ID,
        kind="code_audit",
        task_id=TASK_ID,
        exit_code=0,
        command_or_url="[verdict=PASS]",
        stdout="VERDICT: PASS",
    )
    with patch(
        "hiveweave.services.worktree_review.agent_worktree_path",
        AsyncMock(return_value=env["repo_path"]),
    ):
        assert (
            await detect_attestation_impossible(
                PROJECT_ID, AGENT_ID, TASK_ID, frozenset({"code_audit"})
            )
            is None
        )


@pytest.mark.asyncio
async def test_detect_none_when_prior_lookup_raises(env):
    """前科守卫 fail-closed（审计 ②-1 修复）：前科查询异常 ⇒ 整体 return None。

    修复前该异常被吞成 ``prior=None``（被当成「无前科」）⇒ 会继续产生事实位
    放行，与 docstring 自称的 fail-closed 相反。唯一判据是「前科不可判」。
    所有其它条件都满足（worktree 存在且无 diff），所以只有这一条能拦住放行。
    """
    with (
        patch(
            "hiveweave.services.worktree_review.agent_worktree_path",
            AsyncMock(return_value=env["repo_path"]),
        ),
        patch(
            "hiveweave.services.attestation.find_latest_attestation_by_kind",
            AsyncMock(side_effect=RuntimeError("prior store down")),
        ),
    ):
        assert (
            await detect_attestation_impossible(
                PROJECT_ID, AGENT_ID, TASK_ID, frozenset({"code_audit"})
            )
            is None
        )


# ── 门禁自己消化（集成：_submit_preflight）──────────────────


@pytest.mark.asyncio
async def test_submit_gate_self_consumes_impossible_code_audit(env):
    """命中事实位时门禁不再要求 code_audit，默认路径无需人手动豁免。

    唯一判据：其余前置条件全部满足（真实 test_run 凭证 + 干净且已 merge 的
    worktree），唯一的缺口就是「结构上发不出的 code_audit」。
    """
    repos = env["repo_path"]
    tr_id = await attestation_service.create(
        PROJECT_ID,
        agent_id=AGENT_ID,
        kind="test_run",
        task_id=TASK_ID,
        command_or_url="uv run pytest -q",
        exit_code=0,
        workspace=repos,
        stdout="3 passed",
        commit_hash="deadbeef",
    )
    params = SubmitTaskParams(
        summary="交付完成", testsPassed=True, taskId=TASK_ID,
        attestationIds=[tr_id],
    )
    with patch(
        "hiveweave.services.worktree_review.agent_worktree_path",
        AsyncMock(return_value=repos),
    ):
        pf = await _submit_preflight(
            PROJECT_ID, AGENT_ID, TASK_ID, _task(), params
        )
    assert pf["ok"] is True, pf["issues"]
    assert pf["impossible"] is not None
    assert pf["impossible"]["reason"] == ATTESTATION_IMPOSSIBLE_REASON_TOOL_LIMITED


@pytest.mark.asyncio
async def test_submit_gate_still_blocks_when_diff_present(env):
    """反向对照：有 diff 时唯一的缺口是「少跑了一次 request_code_audit」，
    门禁必须照旧拦下（本事实位不得把该拦的放行）。"""
    _commit_divergence(env["repo_path"], "src/a.py")
    repos = env["repo_path"]
    tr_id = await attestation_service.create(
        PROJECT_ID,
        agent_id=AGENT_ID,
        kind="test_run",
        task_id=TASK_ID,
        command_or_url="uv run pytest -q",
        exit_code=0,
        workspace=repos,
        stdout="3 passed",
        commit_hash="deadbeef",
    )
    params = SubmitTaskParams(
        summary="交付完成", testsPassed=True, taskId=TASK_ID,
        attestationIds=[tr_id],
    )
    with patch(
        "hiveweave.services.worktree_review.agent_worktree_path",
        AsyncMock(return_value=repos),
    ):
        pf = await _submit_preflight(
            PROJECT_ID, AGENT_ID, TASK_ID, _task(), params
        )
    assert pf["impossible"] is None
    assert pf["ok"] is False
    codes = {i["code"] for i in pf["issues"]}
    assert "attestation" in codes, pf["issues"]


# ── 事实位可观测 / 可审计 + 消费 API ────────────────────────


@pytest.mark.asyncio
async def test_record_and_read_impossible_fact(env):
    """事实位落库为 tool_attestations 行，可读回、可 SQL 审计。"""
    att_id = await record_attestation_impossible(
        PROJECT_ID,
        agent_id=AGENT_ID,
        task_id=TASK_ID,
        info={
            "reason": ATTESTATION_IMPOSSIBLE_REASON_TOOL_LIMITED,
            "need": "code_audit",
            "detail": "no_auditable_diff",
            "detected_at": 1234567890,
        },
    )
    assert att_id

    row = await attestation_service.get(PROJECT_ID, att_id)
    assert row is not None
    assert row["kind"] == ATTESTATION_IMPOSSIBLE_KIND
    assert f"[reason={ATTESTATION_IMPOSSIBLE_REASON_TOOL_LIMITED}]" in (
        row["command_or_url"] or ""
    )
    assert row["exit_code"] is None  # 不是执行结论，不得被当通过凭证

    fact = await get_attestation_impossible(PROJECT_ID, TASK_ID)
    assert fact is not None
    assert fact["id"] == att_id
    assert fact["agent_id"] == AGENT_ID


def test_evidence_stamp_and_drop_kind():
    """evidence 事实位可判读，并被消费 API 用于剔除对应必需 kind。"""
    evidence = {
        ATTESTATION_IMPOSSIBLE_EVIDENCE_KEY: {
            "reason": ATTESTATION_IMPOSSIBLE_REASON_TOOL_LIMITED,
            "need": "code_audit",
            "detail": "no_auditable_diff",
            "consumed_by": "submit_gate",
        }
    }
    assert evidence_has_attestation_impossible(evidence) is True
    needed, dropped = drop_impossible_attestation_kinds(
        frozenset({"code_audit", "test_run"}), evidence
    )
    assert dropped is True
    assert needed == frozenset({"test_run"})

    # 无事实位 / need 不在必需清单 → 原样返回（不得误剔）
    assert drop_impossible_attestation_kinds(
        frozenset({"test_run"}), {}
    ) == (frozenset({"test_run"}), False)
    assert drop_impossible_attestation_kinds(
        frozenset({"test_run"}), evidence
    ) == (frozenset({"test_run"}), False)


# ── 批准侧接线（approve / HTTP 门禁自己消化同一事实位）─────────
#
# 报告 Layer 6 第 4 行接线缺口：submit 侧已自消化，但 reviewer approve 仍走
# ``drop_code_audit_kind_if_soft``。
#
# **安全修复（本组用例钉住）**：事实判据此前做在 ``evidence`` 上，而 HTTP 路径
# 的 evidence 是客户端原文 ⇒ 任何调用方自带
# ``{"attestation_impossible":{"need":"code_audit"}}`` 即可跳 code_audit 门。
# 现改为漏斗内部**服务端复核**（``resolve_impossible_kind`` 读
# tool_attestations 平台落库事实行）：库里没有事实行 ⇒ 无论 evidence 写什么
# 都照旧拦。以下守卫 + 阳性对照分别钉住两个方向。


def _impossible_evidence(*, need: str = "code_audit") -> dict:
    return {
        ATTESTATION_IMPOSSIBLE_EVIDENCE_KEY: {
            "reason": ATTESTATION_IMPOSSIBLE_REASON_TOOL_LIMITED,
            "need": need,
            "detail": "no_auditable_diff",
            "consumed_by": "submit_gate",
        }
    }


async def _record_impossible_fact(*, need: str = "code_audit") -> str:
    return await record_attestation_impossible(
        PROJECT_ID,
        agent_id=AGENT_ID,
        task_id=TASK_ID,
        info={
            "reason": ATTESTATION_IMPOSSIBLE_REASON_TOOL_LIMITED,
            "need": need,
            "detail": "no_auditable_diff",
            "detected_at": 1234567890,
        },
    )


# ── 核心回归守卫：伪造 evidence 不能解锁 ──────────────────────


@pytest.mark.asyncio
async def test_forged_impossible_evidence_does_not_drop_kind(env):
    """核心回归守卫：evidence 伪造事实位、但库里**没有**平台事实行 ⇒
    ``drop_code_audit_kind_if_soft`` 不剔除 code_audit。

    这正是修复前的绕过路径（客户端自带事实位即跳门）。唯一判据是服务端复核
    的结果，evidence 只是陪衬。
    """
    from hiveweave.services.code_audit import drop_code_audit_kind_if_soft

    needed, dropped = await drop_code_audit_kind_if_soft(
        frozenset({"code_audit", "test_run"}),
        PROJECT_ID,
        task_id=TASK_ID,
        evidence=_impossible_evidence(),
    )
    assert dropped is False
    assert needed == frozenset({"code_audit", "test_run"})


@pytest.mark.asyncio
async def test_resolve_impossible_kind_none_without_fact(env):
    """无事实行 ⇒ resolve 返回 None（fail-closed：查不到就不放行）。"""
    assert await resolve_impossible_kind(PROJECT_ID, TASK_ID) is None


@pytest.mark.asyncio
async def test_http_gate_rejects_forged_impossible_evidence(env):
    """HTTP 门禁（api/tasks.py::_gate_attestation_for_task）：evidence 伪造
    事实位、但库里无事实行 ⇒ 仍然拦下 code_audit。这是本修复的核心证据。"""
    from fastapi import HTTPException

    from hiveweave.api.tasks import _gate_attestation_for_task

    task = _task(policy_id="code_audit", tags=["code_audit"])
    with pytest.raises(HTTPException) as ei:
        await _gate_attestation_for_task(PROJECT_ID, task, _impossible_evidence())
    assert ei.value.status_code == 400
    assert "code_audit" in str(ei.value.detail)


# ── 阳性对照：平台落库事实行在场时确实剔除该 kind ─────────────


@pytest.mark.asyncio
async def test_verified_impossible_fact_drops_kind(env):
    """阳性对照：库里确有平台落库事实行（need=code_audit）⇒ 才剔除该 kind。

    与上面伪造守卫的唯一差别就是「库里有事实行」，证明剔除确实由服务端复核
    触发，而非 evidence 或形同虚设的门禁。
    """
    from hiveweave.services.code_audit import drop_code_audit_kind_if_soft

    await _record_impossible_fact()
    needed, dropped = await drop_code_audit_kind_if_soft(
        frozenset({"code_audit", "test_run"}),
        PROJECT_ID,
        task_id=TASK_ID,
        evidence=None,
    )
    assert dropped is True
    assert needed == frozenset({"test_run"})


@pytest.mark.asyncio
async def test_resolve_impossible_kind_parses_need(env):
    """事实行在场 ⇒ resolve 从 command_or_url 解析出 need。"""
    await _record_impossible_fact()
    assert await resolve_impossible_kind(PROJECT_ID, TASK_ID) == "code_audit"


@pytest.mark.asyncio
async def test_http_gate_passes_on_verified_impossible_fact(env):
    """阳性对照：库里确有平台事实行 ⇒ HTTP 门禁不再二次拦截 code_audit
    （submit 已自消化的任务在 approve 侧无需再 waive 一次）。"""
    from fastapi import HTTPException

    from hiveweave.api.tasks import _gate_attestation_for_task

    await _record_impossible_fact()
    task = _task(policy_id="code_audit", tags=["code_audit"])
    try:
        await _gate_attestation_for_task(PROJECT_ID, task, {})
    except HTTPException as exc:  # pragma: no cover — 复核失效时才会走到这里
        pytest.fail(f"approve gate re-blocked on verified impossible kind: {exc.detail}")


# ── 接线前的原语义保持（无事实行、无 soft-fail ⇒ 一点都不放行）────


@pytest.mark.asyncio
async def test_drop_code_audit_kind_if_soft_unchanged_without_fact(env):
    """无事实行 / 无 soft-fail 盖章 ⇒ 原样返回（不得误放行）。"""
    from hiveweave.services.code_audit import drop_code_audit_kind_if_soft

    assert await drop_code_audit_kind_if_soft(
        frozenset({"code_audit", "test_run"}), PROJECT_ID, evidence={}
    ) == (frozenset({"code_audit", "test_run"}), False)
    assert await drop_code_audit_kind_if_soft(
        frozenset({"code_audit", "test_run"}), PROJECT_ID, evidence=None
    ) == (frozenset({"code_audit", "test_run"}), False)
    # evidence 事实位的 need 不在必需清单 → 也不得误剔。
    assert await drop_code_audit_kind_if_soft(
        frozenset({"code_audit"}),
        PROJECT_ID,
        evidence=_impossible_evidence(need="test_run"),
    ) == (frozenset({"code_audit"}), False)


@pytest.mark.asyncio
async def test_http_approve_gate_still_blocks_without_fact(env):
    """反向对照（阳性对照基线）：同一任务、同一 policy，仅去掉事实位 ——
    approve 门禁必须照旧拦截 code_audit。证明「放行」确实来自服务端事实行，
    而不是门禁本就形同虚设。"""
    from fastapi import HTTPException

    from hiveweave.api.tasks import _gate_attestation_for_task

    task = _task(policy_id="code_audit", tags=["code_audit"])
    with pytest.raises(HTTPException) as ei:
        await _gate_attestation_for_task(PROJECT_ID, task, {})
    assert ei.value.status_code == 400
    assert "code_audit" in str(ei.value.detail)


# ══════════════════════════════════════════════════════════════════════
# F1（另一半）：code_audit_soft_fail 持久事实位 —— 伪造即拦 + 阳性对照
# ══════════════════════════════════════════════════════════════════════
#
# 修复前 ``drop_code_audit_kind_if_soft`` 的 soft-fail 分支读客户端
# ``evidence["code_audit_soft_fail"]``（llm_failed / no_model / no_callback 三值
# 全中）⇒ 任何调用方自带该盖章即可零 attestation 过门。现与
# attestation_impossible 同构：平台落库事实行 + 服务端复核。
#
# 注意：不能复用进程内 ``get_last_audit_attempt``（``code_audit._last_attempt``
# 是内存态，重启即失），拿它当 approve 侧判据会把「重启前合法软失败」的任务
# 误拦 —— 所以平台事实必须落 ``tool_attestations``。


def _soft_fail_evidence(*, reason: str = "llm_failed") -> dict:
    return {"code_audit_soft_fail": {"reason": reason, "task_id": TASK_ID}}


@pytest.mark.asyncio
async def test_record_and_read_soft_fail_fact(env):
    """软失败事实位落库为 tool_attestations 行，可读回、可 SQL 审计。"""
    att_id = await record_code_audit_soft_fail(
        PROJECT_ID, agent_id=AGENT_ID, task_id=TASK_ID, reason="llm_failed",
    )
    assert att_id
    row = await attestation_service.get(PROJECT_ID, att_id)
    assert row is not None
    assert row["kind"] == CODE_AUDIT_SOFT_FAIL_KIND
    assert "[reason=llm_failed]" in (row["command_or_url"] or "")
    assert "[need=code_audit]" in (row["command_or_url"] or "")
    assert row["exit_code"] is None  # 不是执行结论，不得被当通过凭证

    fact = await get_code_audit_soft_fail(PROJECT_ID, TASK_ID)
    assert fact is not None and fact["id"] == att_id
    assert await resolve_soft_fail_kind(PROJECT_ID, TASK_ID) == "code_audit"


@pytest.mark.asyncio
async def test_forged_soft_fail_evidence_does_not_drop_kind(env):
    """核心回归守卫（F1）：evidence 伪造软失败盖章、但库里**没有**平台持久
    事实行 ⇒ ``drop_code_audit_kind_if_soft`` 不剔除 code_audit。"""
    from hiveweave.services.code_audit import drop_code_audit_kind_if_soft

    needed, dropped = await drop_code_audit_kind_if_soft(
        frozenset({"code_audit", "test_run"}),
        PROJECT_ID,
        task_id=TASK_ID,
        evidence=_soft_fail_evidence(),
    )
    assert dropped is False
    assert needed == frozenset({"code_audit", "test_run"})


@pytest.mark.asyncio
async def test_http_gate_rejects_forged_soft_fail_evidence(env):
    """核心回归守卫（HTTP/approve 门）：伪造盖章 + 库里无事实行 ⇒ 仍拦。"""
    from fastapi import HTTPException

    from hiveweave.api.tasks import _gate_attestation_for_task

    task = _task(policy_id="code_audit", tags=["code_audit"])
    with pytest.raises(HTTPException) as ei:
        await _gate_attestation_for_task(PROJECT_ID, task, _soft_fail_evidence())
    assert ei.value.status_code == 400
    assert "code_audit" in str(ei.value.detail)


@pytest.mark.asyncio
async def test_verified_soft_fail_fact_drops_kind(env):
    """阳性对照（F1）：库里确有平台签发的软失败事实行（need=code_audit）⇒ 才
    剔除该 kind。与伪造守卫的唯一差别 = 「库里有事实行」。"""
    from hiveweave.services.code_audit import drop_code_audit_kind_if_soft

    await record_code_audit_soft_fail(
        PROJECT_ID, agent_id=AGENT_ID, task_id=TASK_ID, reason="llm_failed",
    )
    needed, dropped = await drop_code_audit_kind_if_soft(
        frozenset({"code_audit", "test_run"}),
        PROJECT_ID,
        task_id=TASK_ID,
        evidence=None,
    )
    assert dropped is True
    assert needed == frozenset({"test_run"})


@pytest.mark.asyncio
async def test_http_gate_passes_on_verified_soft_fail_fact(env):
    """阳性对照（F1）：库里确有平台事实行 ⇒ HTTP 门禁不再二次拦截 code_audit。"""
    from fastapi import HTTPException

    from hiveweave.api.tasks import _gate_attestation_for_task

    await record_code_audit_soft_fail(
        PROJECT_ID, agent_id=AGENT_ID, task_id=TASK_ID, reason="llm_failed",
    )
    task = _task(policy_id="code_audit", tags=["code_audit"])
    try:
        await _gate_attestation_for_task(PROJECT_ID, task, {})
    except HTTPException as exc:  # pragma: no cover — 复核失效时才会走到这里
        pytest.fail(f"approve gate re-blocked on verified soft-fail fact: {exc.detail}")


# ── F3：剔除的 kind 固定为 code_audit（不采信事实行里的 need 值）──────────


@pytest.mark.asyncio
async def test_soft_fail_fact_need_mismatch_does_not_drop_kind(env):
    """F3 守卫：事实行 need≠code_audit（此处 test_run）⇒ 不据行内容剔 kind。"""
    from hiveweave.services.code_audit import drop_code_audit_kind_if_soft

    await attestation_service.create(
        PROJECT_ID,
        agent_id=AGENT_ID,
        kind=CODE_AUDIT_SOFT_FAIL_KIND,
        task_id=TASK_ID,
        command_or_url="[reason=llm_failed] [need=test_run]",
        stdout="{}",
        exit_code=None,
    )
    needed, dropped = await drop_code_audit_kind_if_soft(
        frozenset({"code_audit", "test_run"}),
        PROJECT_ID,
        task_id=TASK_ID,
        evidence=None,
    )
    assert dropped is False
    assert needed == frozenset({"code_audit", "test_run"})


@pytest.mark.asyncio
async def test_impossible_fact_need_mismatch_does_not_drop_kind(env):
    """F3 守卫（impossible 分支同判据）：need≠code_audit ⇒ 不剔任何 kind。

    修复前 ``impossible_need in needed`` 会按行里的 need 剔除 test_run。
    """
    from hiveweave.services.code_audit import drop_code_audit_kind_if_soft

    await attestation_service.create(
        PROJECT_ID,
        agent_id=AGENT_ID,
        kind=ATTESTATION_IMPOSSIBLE_KIND,
        task_id=TASK_ID,
        command_or_url="[reason=tool_limited] [need=test_run]",
        stdout="{}",
        exit_code=None,
    )
    needed, dropped = await drop_code_audit_kind_if_soft(
        frozenset({"code_audit", "test_run"}), PROJECT_ID, task_id=TASK_ID,
    )
    assert dropped is False
    assert needed == frozenset({"code_audit", "test_run"})


# ══════════════════════════════════════════════════════════════════════
# F2：返修（rework）必须失效「平台门禁放宽事实位」
# ══════════════════════════════════════════════════════════════════════
#
# 审计实测：先以「无 diff」正当落一条 attestation_impossible 事实行 → 任务被
# 返修、补了真代码 → approve 时 ``resolve_impossible_kind`` 仍读到旧行 ⇒
# code_audit 继续免检（全仓没有任何代码失效这个事实行）。
#
# 修复：返修汇聚点（``services.tasks.review._force_rework``）已调用的
# ``invalidate_valid_waivers`` 现同时退役该任务的事实行（``expires_at=now``，
# 行保留可审计）。挂在那个既有钩子上是为了覆盖 tool / HTTP / verify_merge 等
# 全部返修入口。以下用例用**真实返修**（``TaskService.review_task``）验证。


@pytest.fixture
async def rework_env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        async def fake_get_agent_project_id(aid: str):
            return PROJECT_ID if aid in (COORD_ID, AGENT_ID) else None

        _FAKE_AGENTS = {
            COORD_ID: {
                "id": COORD_ID,
                "name": "协调员",
                "short_id": "C001",
                "parent_id": None,
                "permission_type": "coordinator",
                "role": "架构师",
                "status": "active",
            },
            AGENT_ID: {
                "id": AGENT_ID,
                "name": "执行者",
                "short_id": "E001",
                "parent_id": COORD_ID,
                "permission_type": "executor",
                "role": "engineer",
                "status": "active",
            },
        }

        async def fake_get_agent_by_id(aid: str):
            return _FAKE_AGENTS.get(aid)

        att_module._migrated.clear()
        task_module._migrated.clear()
        project_db._agent_cache.pop(COORD_ID, None)
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
            patch("hiveweave.db.meta.get_agent_by_id", fake_get_agent_by_id),
        ):
            yield {"project_id": PROJECT_ID, "workspace_path": workspace_path}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
        project_db._agent_cache.pop(COORD_ID, None)
        project_db._agent_cache.pop(AGENT_ID, None)


async def _submitted_task(svc) -> str:
    tid = await svc.create_task(
        project_id=PROJECT_ID, title="T", description="d", creator_id=COORD_ID,
    )
    await svc.claim_task(PROJECT_ID, tid, AGENT_ID)
    await svc.start_task(PROJECT_ID, tid)
    await svc.submit_task(PROJECT_ID, tid, {"files": ["a.py"]})
    await svc.start_review(PROJECT_ID, tid)
    return tid


async def _rework(svc, tid: str) -> None:
    # 2026-09-11 处方门禁下沉：无路径的返修必须声明结构化处方类别。
    await svc.review_task(
        PROJECT_ID, tid, "rework",
        feedback="fix", prescription_kind="missing-evidence",
    )


@pytest.mark.asyncio
async def test_rework_invalidates_impossible_fact(rework_env):
    """F2 守卫：落事实行 → 真实返修 ⇒ ``resolve_impossible_kind`` 不再返回该
    need（门禁重新拦 code_audit）。"""
    from hiveweave.services.code_audit import (
        CODE_AUDIT_KIND,
        drop_code_audit_kind_if_soft,
    )
    from hiveweave.services.task import TaskService

    svc = TaskService()
    tid = await _submitted_task(svc)
    await record_attestation_impossible(
        PROJECT_ID,
        agent_id=AGENT_ID,
        task_id=tid,
        info={
            "reason": ATTESTATION_IMPOSSIBLE_REASON_TOOL_LIMITED,
            "need": "code_audit",
            "detail": "no_auditable_diff",
            "detected_at": 1234567890,
        },
    )
    # 返修前：事实行在案 ⇒ 门禁会免检 code_audit
    assert await resolve_impossible_kind(PROJECT_ID, tid) == "code_audit"

    await _rework(svc, tid)

    # 返修后：事实行已退役 ⇒ 不再返回该 need，门禁重新拦
    assert await resolve_impossible_kind(PROJECT_ID, tid) is None
    needed, dropped = await drop_code_audit_kind_if_soft(
        frozenset({CODE_AUDIT_KIND}), PROJECT_ID, task_id=tid,
    )
    assert dropped is False
    assert CODE_AUDIT_KIND in (needed or frozenset())


@pytest.mark.asyncio
async def test_rework_invalidates_soft_fail_fact(rework_env):
    """F2 守卫（另一半）：软失败事实行同样活不过返修。"""
    from hiveweave.services.task import TaskService

    svc = TaskService()
    tid = await _submitted_task(svc)
    await record_code_audit_soft_fail(
        PROJECT_ID, agent_id=AGENT_ID, task_id=tid, reason="llm_failed",
    )
    assert await resolve_soft_fail_kind(PROJECT_ID, tid) == "code_audit"

    await _rework(svc, tid)

    assert await resolve_soft_fail_kind(PROJECT_ID, tid) is None


@pytest.mark.asyncio
async def test_rework_retires_fact_rows_not_deletes(rework_env):
    """F2 可观测性：退役 = ``UPDATE expires_at``（行保留供审计），不是 DELETE。"""
    from hiveweave.services.task import TaskService

    svc = TaskService()
    tid = await _submitted_task(svc)
    att_id = await record_attestation_impossible(
        PROJECT_ID,
        agent_id=AGENT_ID,
        task_id=tid,
        info={
            "reason": ATTESTATION_IMPOSSIBLE_REASON_TOOL_LIMITED,
            "need": "code_audit",
            "detail": "no_auditable_diff",
        },
    )

    await _rework(svc, tid)

    row = await attestation_service.get(PROJECT_ID, att_id)
    assert row is not None  # 行仍在（可审计）
    assert row["expires_at"] is not None
    # 但已过期 ⇒ 消费侧读不到
    assert await get_attestation_impossible(PROJECT_ID, tid) is None
