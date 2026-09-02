"""交付级冒烟门（service_smoke）端到端测试——起真服务、真 HTTP 探测。

s3-clone_07 GAP：17/18 任务内部门禁全绿 vs 官方 verifier 0/22——门禁体系缺
"交付物作为整体是否可用"这道门。service_smoke 条款 = 平台在 submit 时代跑
确定型冒烟（零 LLM token）：启动服务 → 等端口 LISTEN → 跑冻结探针 → 退出码断言。

本测试文件全部用**真实子进程**（python -m http.server + urllib 探针），
不 mock spawn——因为 07 的教训是"mock 层全绿、装配层全死"。
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from hiveweave.services.smoke_gate import find_smoke_clause, run_service_smoke_clause
from hiveweave.services.task_contract import validate_contract

PY = sys.executable


def _mk_workspace(tmp_path: Path, script_body: str) -> Path:
    """tmp 工作区：最小服务（http.server 服务目录）+ 探针脚本 + 数据文件。"""
    (tmp_path / "data.txt").write_text("hello-smoke\n", encoding="utf-8")
    smoke_dir = tmp_path / "tests" / "smoke"
    smoke_dir.mkdir(parents=True)
    (smoke_dir / "smoke_test.py").write_text(
        textwrap.dedent(script_body), encoding="utf-8"
    )
    return tmp_path


def _clause(tmp_path: Path, **overrides) -> dict:
    c = {
        "id": "smoke-1",
        "type": "service_smoke",
        "script": "tests/smoke/smoke_test.py",
        "startCommand": f'"{PY}" -m http.server {{port}} --bind 127.0.0.1',
        "timeout": 30,
    }
    c.update(overrides)
    return c


GOOD_SCRIPT = """
import os, urllib.request
port = os.environ["SMOKE_PORT"]
base = f"http://127.0.0.1:{port}"
r = urllib.request.urlopen(base + "/", timeout=5)
assert r.status == 200, f"root status {r.status}"
body = urllib.request.urlopen(base + "/data.txt", timeout=5).read().decode()
assert body.strip() == "hello-smoke", f"data mismatch: {body!r}"
print("SMOKE-OK")
"""


@pytest.mark.asyncio
async def test_smoke_passes_on_working_service(tmp_path):
    ws = _mk_workspace(tmp_path, GOOD_SCRIPT)
    result, freeze = await run_service_smoke_clause(
        _clause(ws), workspace_root=str(ws)
    )
    assert result.passed, f"{result.message}\n{result.evidence}"
    assert freeze and freeze["sha256"]
    assert "SMOKE-OK" in (result.evidence or "")


@pytest.mark.asyncio
async def test_smoke_fails_when_main_chain_broken(tmp_path):
    """服务起得来（健康检查会过）但主链路断——正是 07 的 0/22 形态。"""
    broken = GOOD_SCRIPT + """
raise SystemExit("DATA CHAIN BROKEN: expected hello-smoke")
"""
    ws = _mk_workspace(tmp_path, broken)
    result, freeze = await run_service_smoke_clause(
        _clause(ws), workspace_root=str(ws)
    )
    assert not result.passed
    assert "DATA CHAIN BROKEN" in (result.evidence or "")
    # 失败也要留冻结指纹（防实现者随后改脚本重试绕过比对基线）
    assert freeze and freeze["sha256"]


@pytest.mark.asyncio
async def test_smoke_fails_when_service_dies_at_boot(tmp_path):
    """startCommand 起不来（端口都不会听）——07 静默降级的响亮版本。"""
    ws = _mk_workspace(tmp_path, GOOD_SCRIPT)
    clause = _clause(ws, startCommand=f'"{PY}" -c "import sys; sys.exit(3)"')
    result, _ = await run_service_smoke_clause(
        clause, workspace_root=str(ws)
    )
    assert not result.passed
    assert "退出" in result.message or "exit" in result.message


@pytest.mark.asyncio
async def test_freeze_blocks_script_weakening(tmp_path):
    """首次验证后削弱探针 → 立即失败（防篡改冻结）。"""
    ws = _mk_workspace(tmp_path, GOOD_SCRIPT)
    clause = _clause(ws)
    r1, freeze = await run_service_smoke_clause(clause, workspace_root=str(ws))
    assert r1.passed and freeze

    weakened = GOOD_SCRIPT + """
print("weakened: skip everything")
"""
    (ws / "tests" / "smoke" / "smoke_test.py").write_text(
        weakened, encoding="utf-8"
    )
    r2, _ = await run_service_smoke_clause(
        clause, workspace_root=str(ws), frozen=freeze
    )
    assert not r2.passed
    assert "修改" in r2.message or "sha256" in r2.message


@pytest.mark.asyncio
async def test_smoke_script_missing(tmp_path):
    ws = _mk_workspace(tmp_path, GOOD_SCRIPT)
    (ws / "tests" / "smoke" / "smoke_test.py").unlink()
    result, freeze = await run_service_smoke_clause(
        _clause(ws), workspace_root=str(ws)
    )
    assert not result.passed
    assert "not found" in result.message
    assert freeze is None


def test_validate_requires_script_and_start_command():
    ok = _clause(Path("."), )
    assert validate_contract({"id": "s", "acceptance": [ok]}) is None
    bad1 = _clause(Path("."), script="")
    err = validate_contract({"id": "s", "acceptance": [bad1]})
    assert err and "script" in err
    bad2 = _clause(Path("."), startCommand="")
    err = validate_contract({"id": "s", "acceptance": [bad2]})
    assert err and "startCommand" in err


def test_find_smoke_clause():
    contract = {"acceptance": [{"id": "a", "type": "file_exists", "path": "x"},
                               _clause(Path("."))]}
    assert find_smoke_clause(contract)["id"] == "smoke-1"
    assert find_smoke_clause({"acceptance": []}) is None
    assert find_smoke_clause(None) is None
