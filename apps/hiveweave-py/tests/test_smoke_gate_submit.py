"""service_smoke × submit 门禁的穿透集成测试。

纪律（09-01 三次"修了没生效"教训）：实现函数的单测绿 ≠ 门禁链生效。
本文件验证 TaskService.submit_for_transition 真的会跑 service_smoke 条款、
把结果并进 machine_pre_run、失败时拒绝提交——用的是与
test_slice_contract_p0.py 相同的 TaskService+tmp 工作区夹具。
"""

from __future__ import annotations

import sys
import tempfile
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import task as task_module
from hiveweave.services.task import TaskService
from hiveweave.services.task_contract import parse_contract

PROJECT_ID = "smoke-gate-proj"
COORD = "coord-smoke"
EXEC = "exec-smoke"
PY = sys.executable

GOOD_SCRIPT = """
import os, urllib.request
port = os.environ["SMOKE_PORT"]
body = urllib.request.urlopen(
    f"http://127.0.0.1:{port}/data.txt", timeout=5
).read().decode()
assert body.strip() == "hello-smoke", f"data mismatch: {body!r}"
print("SMOKE-OK")
"""

BROKEN_SCRIPT = """
import os
raise SystemExit("MAIN CHAIN BROKEN")
"""


def _contract(script_body: str) -> dict:
    return {
        "id": "milestone-smoke-slice",
        "name": "Milestone with smoke gate",
        "acceptance": [
            {
                "id": "smoke-1",
                "type": "service_smoke",
                "script": "tests/smoke/smoke_test.py",
                "startCommand": f'"{PY}" -m http.server {{port}} --bind 127.0.0.1',
                "timeout": 30,
            }
        ],
        "rework_limit": 3,
    }


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_ws(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        task_module._migrated.discard(PROJECT_ID)
        with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
            yield {"project_id": PROJECT_ID, "workspace": workspace_path}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


def _mk_files(ws: str, script_body: str) -> None:
    smoke = Path(ws) / "tests" / "smoke"
    smoke.mkdir(parents=True)
    (smoke / "smoke_test.py").write_text(
        textwrap.dedent(script_body), encoding="utf-8"
    )
    (Path(ws) / "data.txt").write_text("hello-smoke\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_submit_runs_smoke_and_persists_freeze(env):
    """提交时门禁真的跑了冒烟：machine_pre_run 含 service_smoke 结果 + 冻结落库。"""
    ts = TaskService()
    pid, ws = env["project_id"], env["workspace"]
    _mk_files(ws, GOOD_SCRIPT)
    tid = await ts.create_task(
        pid, "Milestone smoke", "d", creator_id=COORD, assignee_id=EXEC,
        contract_json=_contract(GOOD_SCRIPT),
    )
    await ts.start_task(pid, tid)
    await ts.submit_task(pid, tid, {"summary": "done", "tests_passed": True})

    task = await ts.get_task(pid, tid)
    assert task["status"] == "submitted"
    c = parse_contract(task["contract_json"])
    assert c["machine_pre_run"]["passed"] is True
    smoke_results = [
        r for r in c["machine_pre_run"]["results"]
        if r["type"] == "service_smoke"
    ]
    assert smoke_results and smoke_results[0]["passed"] is True
    assert c.get("smoke_freeze", {}).get("sha256")


@pytest.mark.asyncio
async def test_submit_rejected_when_smoke_fails(env):
    """主链路断（服务活、探针死）→ submit 被拒，回执带探针输出。"""
    ts = TaskService()
    pid, ws = env["project_id"], env["workspace"]
    _mk_files(ws, BROKEN_SCRIPT)
    tid = await ts.create_task(
        pid, "Milestone smoke broken", "d", creator_id=COORD,
        assignee_id=EXEC, contract_json=_contract(BROKEN_SCRIPT),
    )
    await ts.start_task(pid, tid)
    with pytest.raises(ValueError, match="MAIN CHAIN BROKEN"):
        await ts.submit_task(pid, tid, {"summary": "fake", "tests_passed": True})
    # 任务停在 running（未提交）
    task = await ts.get_task(pid, tid)
    assert task["status"] == "running"
    c = parse_contract(task["contract_json"])
    assert c["machine_pre_run"]["passed"] is False
    failed = [r for r in c["machine_pre_run"]["results"]
              if r["type"] == "service_smoke" and not r["passed"]]
    assert failed and "MAIN CHAIN BROKEN" in (failed[0].get("evidence") or "")
