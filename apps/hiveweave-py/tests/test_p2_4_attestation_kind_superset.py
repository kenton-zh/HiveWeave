"""P2-4 交付契约 kind 门禁 —— 判据是**集合包含**，不是集合相等。

病灶（实测 28 项目 / 63 次，`690d743` 后仍 12/31）：`verify_ids` 对每个入参
id 要求 `kind in expected_kinds`，等价于要求「给定集合 ⊆ 所需集合」= 集合
**相等** ⇒ 「本已含所需 kind、只是夹带一个 surplus」被整体拒。把 27 条的入参
与实际 kind 对齐后：**20 条的批量里本已含所需 kind，仅因 surplus 被整体拒**。

修法：surplus **忽略**（记入结构化 `report["ignoredKinds"]`），
`expected_kinds ⊆ seen_kinds` 是唯一硬判。本文件钉住该语义：
- 判据层（`verify_ids`）：surplus 通过 / 缺 kind 仍拒 / surplus 的**其余校验也
  不再有机会把整次提交打回**（过期、换人、exit≠0）；
- 回执层（`_submit_preflight`）：结构化字段 `gate/expectedKinds/givenKinds/
  ignoredKinds` 落进 issue。
"""

from __future__ import annotations

import subprocess
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import attestation as att_module
from hiveweave.services.attestation import AttestationService, attestation_service
from hiveweave.tools.tasks.submit import SubmitTaskParams, _submit_preflight

PROJECT_ID = "test-p2-4-surplus-kind"
AGENT_ID = "agent-p24"
TASK_ID = "22222222-2222-2222-2222-222222222222"


# ── 判据层：直接用假行驱动 verify_ids（不碰 DB）──────────────


def _svc_with_rows(monkeypatch, rows: dict[str, dict]) -> AttestationService:
    svc = AttestationService()
    now = int(time.time() * 1000)

    async def fake_get(_pid, aid):
        return rows.get(str(aid))

    monkeypatch.setattr(svc, "get", fake_get)
    monkeypatch.setattr(svc, "ensure_schema", AsyncMock())
    return svc


def _row(kind: str, **over) -> dict:
    now = int(time.time() * 1000)
    base = {
        "id": "x",
        "kind": kind,
        "agent_id": AGENT_ID,
        "task_id": TASK_ID,
        "created_at": now,
        "expires_at": now + 3_600_000,
        "stdout_hash": "h",
        "exit_code": 0,
    }
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_surplus_kind_is_ignored_not_rejected(monkeypatch):
    """⭐ 验收 ③ 正向：`[test_run, browse_e2e]` + 只要 browse_e2e ⇒ 通过。"""
    svc = _svc_with_rows(monkeypatch, {
        "tr": _row("test_run"),
        "br": _row("browse_e2e"),
    })
    report: dict = {}
    ok, err = await svc.verify_ids(
        "p", ["tr", "br"], expected_kinds={"browse_e2e"}, report=report
    )
    assert ok, err
    assert report["expectedKinds"] == ["browse_e2e"]
    assert report["givenKinds"] == ["browse_e2e"]
    assert report["ignoredKinds"] == ["test_run"]


@pytest.mark.asyncio
async def test_missing_kind_still_rejected(monkeypatch):
    """⭐ 验收 ③ 反向：只给 test_run 却要 browse_e2e ⇒ 仍必须拒。"""
    svc = _svc_with_rows(monkeypatch, {"tr": _row("test_run")})
    report: dict = {}
    ok, err = await svc.verify_ids(
        "p", ["tr"], expected_kinds={"browse_e2e"}, report=report
    )
    assert not ok
    assert "Missing required attestation kind" in err
    assert report["givenKinds"] == []
    assert report["ignoredKinds"] == ["test_run"]


@pytest.mark.asyncio
async def test_surplus_expired_row_does_not_block(monkeypatch):
    """surplus 的**其余校验**也不该把提交打回（旧实现连过期 surplus 都会拒）。"""
    now = int(time.time() * 1000)
    svc = _svc_with_rows(monkeypatch, {
        "br": _row("browse_e2e"),
        "old": _row("test_run", expires_at=now - 1000),
    })
    ok, err = await svc.verify_ids(
        "p", ["br", "old"], expected_kinds={"browse_e2e"}
    )
    assert ok, err


@pytest.mark.asyncio
async def test_surplus_failed_exit_does_not_block(monkeypatch):
    """surplus 的 exit≠0 同样不该解锁或阻塞（它既不解锁也不该拒）。"""
    svc = _svc_with_rows(monkeypatch, {
        "br": _row("browse_e2e"),
        "bad": _row("visual_check", exit_code=1),
        "bad_audit": _row("code_audit", exit_code=1),
    })
    ok, err = await svc.verify_ids(
        "p", ["br", "bad", "bad_audit"], expected_kinds={"browse_e2e"}
    )
    assert ok, err


@pytest.mark.asyncio
async def test_required_failed_exit_still_blocks(monkeypatch):
    """反向对照：**所需** kind 的 exit≠0 仍必须拒（别把 surplus 的放宽用到这里）。"""
    svc = _svc_with_rows(monkeypatch, {"br": _row("browse_e2e", exit_code=1)})
    ok, err = await svc.verify_ids("p", ["br"], expected_kinds={"browse_e2e"})
    assert not ok
    assert "exit_code=1" in err


@pytest.mark.asyncio
async def test_every_policy_judge_matches_prescription_set(monkeypatch):
    """⭐ 说明书 ↔ 判据 的**行为**对齐（不是文案对齐）。

    对每个硬策略取它下发给 assignee 的 `attestation_kinds`（= 说明书），
    断言判据行为正好等价于「**包含**该集合」：
      ① 集合本身 ⇒ 通过；② 集合 + 一个 surplus ⇒ 通过；③ 去掉任一 ⇒ 拒。
    这样「说明书说 A、判据做 B」的漂移会被打红，而无需对文案做子串断言
    （本仓禁文本判据 —— 换措辞即绕过的判据不是约束）。
    """
    from hiveweave.services.attestation import POLICY_REQUIRED_KINDS
    from hiveweave.services.tasks.policy import submit_expectations

    hard = {p: k for p, k in POLICY_REQUIRED_KINDS.items() if k}
    assert hard, "策略表为空，测试失去意义"
    for policy, kinds in hard.items():
        exp = submit_expectations({"id": "t", "policy_id": policy, "tags": []})
        presc = set(exp["attestation_kinds"])
        assert presc == set(kinds), (policy, presc, kinds)

        rows = {f"a{i}": _row(k, id=f"a{i}") for i, k in enumerate(sorted(kinds))}
        rows["surplus"] = _row("_surplus_probe")
        svc = _svc_with_rows(monkeypatch, rows)
        ids = list(rows)
        ok, err = await svc.verify_ids("p", ids, expected_kinds=presc)
        assert ok, f"{policy}: 集合+surplus 应通过，实得 {err}"

        if presc:
            drop = sorted(presc)[0]
            kept = [
                f"a{i}" for i, k in enumerate(sorted(kinds)) if k != drop
            ]
            svc2 = _svc_with_rows(monkeypatch, rows)
            ok2, err2 = await svc2.verify_ids("p", kept, expected_kinds=presc)
            assert not ok2, f"{policy}: 缺 {drop} 必须拒，实得 {err2}"


@pytest.mark.asyncio
async def test_soft_policy_unchanged(monkeypatch):
    """软策略（expected_kinds=None）不适用 surplus 语义：非法 id 照旧拒。"""
    svc = _svc_with_rows(monkeypatch, {"tr": _row("test_run", stdout_hash="")})
    ok, err = await svc.verify_ids("p", ["tr"])
    assert not ok
    assert "stdout_hash" in err


# ── 回执层：走真实门禁入口 `_submit_preflight` ────────────────


def _run_git(cwd: str, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                   check=False)


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())
        repo_path = str((Path(tmpdir) / "repo").resolve())
        Path(repo_path).mkdir(parents=True, exist_ok=True)
        _run_git(repo_path, "init")
        _run_git(repo_path, "symbolic-ref", "HEAD", "refs/heads/main")
        _run_git(repo_path, "config", "user.email", "t@example.com")
        _run_git(repo_path, "config", "user.name", "tester")
        _run_git(repo_path, "config", "core.autocrlf", "false")
        (Path(repo_path) / "README.md").write_text("hello\n", encoding="utf-8")
        _run_git(repo_path, "add", "-A")
        _run_git(repo_path, "commit", "-m", "init")

        async def fake_ws(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        att_module._migrated.clear()
        with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
            yield {"workspace_path": workspace_path, "repo_path": repo_path}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


def _task() -> dict:
    return {
        "id": TASK_ID,
        "title": "API work",
        "tags": ["backend"],
        "policy_id": "generic_tests",   # 只要求 test_run
        "status": "running",
        "evidence": {},
    }


@pytest.mark.asyncio
async def test_preflight_accepts_surplus_and_reports_it(env):
    """入口级：所需 test_run 齐 + 夹带 browse_e2e ⇒ 门禁不再报 attestation 缺项。"""
    repos = env["repo_path"]
    tr = await attestation_service.create(
        PROJECT_ID, agent_id=AGENT_ID, kind="test_run", task_id=TASK_ID,
        command_or_url="uv run pytest -q", exit_code=0, workspace=repos,
        stdout="3 passed", commit_hash="deadbeef",
    )
    br = await attestation_service.create(
        PROJECT_ID, agent_id=AGENT_ID, kind="browse_e2e", task_id=TASK_ID,
        command_or_url="http://127.0.0.1:3000", exit_code=0, workspace=repos,
        stdout="core interaction ok", commit_hash="deadbeef",
    )
    params = SubmitTaskParams(
        summary="交付完成", testsPassed=True, taskId=TASK_ID,
        attestationIds=[tr, br],
    )
    with patch(
        "hiveweave.services.worktree_review.agent_worktree_path",
        AsyncMock(return_value=repos),
    ):
        pf = await _submit_preflight(PROJECT_ID, AGENT_ID, TASK_ID, _task(), params)
    assert "attestation" not in {i["code"] for i in pf["issues"]}, pf["issues"]


@pytest.mark.asyncio
async def test_preflight_receipt_carries_structured_kinds(env):
    """验收 ② 反向：缺件被拒时，回执带 gate/expectedKinds/givenKinds/ignoredKinds。"""
    repos = env["repo_path"]
    br = await attestation_service.create(
        PROJECT_ID, agent_id=AGENT_ID, kind="browse_e2e", task_id=TASK_ID,
        command_or_url="http://127.0.0.1:3000", exit_code=0, workspace=repos,
        stdout="core interaction ok", commit_hash="deadbeef",
    )
    params = SubmitTaskParams(
        summary="交付完成", testsPassed=True, taskId=TASK_ID,
        attestationIds=[br],
    )
    with patch(
        "hiveweave.services.worktree_review.agent_worktree_path",
        AsyncMock(return_value=repos),
    ):
        pf = await _submit_preflight(PROJECT_ID, AGENT_ID, TASK_ID, _task(), params)
    att_issues = [i for i in pf["issues"] if i["code"] == "attestation"]
    assert att_issues, pf["issues"]
    issue = att_issues[0]
    assert issue["gate"] == "submit_attestation"
    assert issue["expectedKinds"] == ["test_run"]
    assert issue["ignoredKinds"] == ["browse_e2e"]
    assert issue["givenKinds"] == []
