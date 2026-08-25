"""E9–E17 (复盘 P1/P2) 验收：venv 产品化 / argv 引号 / python_script /
轮次疏导 / 数据卫生 / bash venv 提示。

每项验收锚点见 .trae/documents/s3-clone_04平台复盘报告.md 附录 B。
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# ── E9: venv 产品化 ──────────────────────────────────────────


async def test_ensure_project_venv_creates_dir_and_gitignore(tmpdir):
    from hiveweave.services.venv_setup import (
        ensure_project_venv,
        project_venv_python,
    )

    ws = str(tmpdir)
    # 预置 .gitignore（无 .venv 条目）——只补条目，不代建
    (Path(ws) / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    ok = await asyncio.to_thread(ensure_project_venv, ws)
    venv = Path(ws) / ".venv"
    if not ok:  # 环境无 python/uv 时 fail-open，不阻断（验收点为不抛异常）
        pytest.skip("venv 创建失败（无可用 python/uv），跳过存在性断言")
    assert venv.is_dir()
    assert project_venv_python(ws)
    gi = (Path(ws) / ".gitignore").read_text(encoding="utf-8")
    assert ".venv/" in gi


def test_project_venv_python_none_for_no_workspace():
    from hiveweave.services.venv_setup import project_venv_python

    assert project_venv_python(None) is None
    assert project_venv_python("") is None


# ── E10: argv 化 + Windows 引号 ──────────────────────────────


def test_quote_windows_arg_roundtrip():
    from hiveweave.services.acl_sandbox.integration import (
        _quote_windows_arg,
        quote_windows_argv,
    )

    # 无空格无引号无反斜杠 → 原样
    assert _quote_windows_arg("abc") == "abc"
    # 含空格 → 加引号
    assert _quote_windows_arg("a b") == '"a b"'
    # 空串 → 双引号
    assert _quote_windows_arg("") == '""'
    # 内嵌引号 → 引号前反斜杠翻倍再补一（list2cmdline 规则）
    assert _quote_windows_arg('say "hi"') == '"say \\"hi\\""'
    # 含反斜杠（如 UNC）→ 整体引用，非引号前保持原样
    assert _quote_windows_arg("a\\b") == '"a\\b"'
    assert _quote_windows_arg("a\\b c") == '"a\\b c"'
    # 行尾反斜杠 → 翻倍（避免与闭合引号合成转义）
    assert _quote_windows_arg("C:\\x\\") == '"C:\\x\\\\"'
    # 数组拼接：逐元素引用，用户在命令里写的引号不再被外层剥掉
    line = quote_windows_argv(["pwsh", "-c", 'echo "x"'])
    assert line.count('"') >= 4
    assert '\\"x\\"' in line


def test_build_confined_argv_shape():
    from hiveweave.services.acl_sandbox.integration import build_confined_argv

    argv = build_confined_argv("echo hello")
    assert isinstance(argv, list) and len(argv) >= 2
    # 收尾元素承载规范化后的命令（pwsh -Command / cmd /c 的脚本位）
    assert "hello" in argv[-1].lower()


def test_spawn_confined_requires_command_or_argv():
    from hiveweave.services.acl_sandbox.service import spawn_confined

    with pytest.raises(ValueError):
        asyncio.get_event_loop().run_until_complete(spawn_confined(
            workdir=".", workspace_path=".", agent_id="a",
        ))


# ── E11: python_script 一等公民（注册 + schema + 权限 + doom + 执行）──


def test_python_script_wired_everywhere():
    """5 处接线齐全：registry / TOOL_PARAM_SCHEMAS / permission / doom_loop。"""
    from hiveweave.llm.streamer.doom_loop import DOOM_LOOP_TOOL_LIMITS
    from hiveweave.services.permission import READWRITE_TOOLS
    from hiveweave.tools.base import _TOOL_REGISTRY
    from hiveweave.tools.executor import TOOL_PARAM_SCHEMAS

    assert "python_script" in _TOOL_REGISTRY
    assert "python_script" in TOOL_PARAM_SCHEMAS
    assert "python_script" in READWRITE_TOOLS
    assert DOOM_LOOP_TOOL_LIMITS.get("python_script") == 3


@pytest.mark.asyncio
async def test_python_script_runs_code_native(tmpdir):
    from hiveweave.tools.python_script import (
        PythonScriptParams,
        python_script_execute,
    )

    ws = str(tmpdir)
    with (
        patch(
            "hiveweave.services.acl_sandbox.integration.acl_sandbox_active",
            return_value=False,
        ),
        patch("hiveweave.tools.helpers.get_project_id", new=AsyncMock(return_value="p1")),
    ):
        r = await python_script_execute(
            PythonScriptParams(script="print(40 + 2)\n"), "aid", ws
        )
    assert r.success is True
    assert "42" in (r.output or "")


@pytest.mark.asyncio
async def test_python_script_error_surface(tmpdir):
    from hiveweave.tools.python_script import (
        PythonScriptParams,
        python_script_execute,
    )

    ws = str(tmpdir)
    with (
        patch(
            "hiveweave.services.acl_sandbox.integration.acl_sandbox_active",
            return_value=False,
        ),
        patch("hiveweave.tools.helpers.get_project_id", new=AsyncMock(return_value="p1")),
    ):
        r = await python_script_execute(
            PythonScriptParams(script="raise RuntimeError('boom')"), "aid", ws
        )
    assert r.success is False
    assert "boom" in (r.error or "")


# ── E14: turn 轮次疏导上限 ───────────────────────────────────


def test_force_commit_rounds_constants_env_tunable(monkeypatch):
    import importlib

    import hiveweave.llm.streamer.constants as c

    assert isinstance(c.FORCE_COMMIT_ROUNDS, int) and c.FORCE_COMMIT_ROUNDS > 0
    assert isinstance(c.FORCE_COMMIT_GRACE_ROUNDS, int)
    monkeypatch.setenv("HIVEWEAVE_FORCE_COMMIT_ROUNDS", "3")
    importlib.reload(c)
    try:
        assert c.FORCE_COMMIT_ROUNDS == 3
    finally:
        monkeypatch.delenv("HIVEWEAVE_FORCE_COMMIT_ROUNDS", raising=False)
        importlib.reload(c)


def test_budget_exhausted_result_carries_steering_reason():
    from hiveweave.llm.streamer.core import Streamer

    class _Stub:
        @staticmethod
        def _strip_placeholder(text: str) -> str:
            return text

    result = Streamer._budget_exhausted_result(
        _Stub(),  # 纯收口回执构造，不触 LLM
        text_acc="done",
        thinking_acc="",
        tool_history=[{"name": "x"}],
        tool_turn_acc=[],
        round_num=50,
        last_usage=None,
        usage_rounds=[],
        note="[TURN ROUND CAP] test",
        reason="force_commit_rounds",
    )
    assert result["budget_exhausted"] is True
    assert result["steering_reason"] == "force_commit_rounds"
    assert "[TURN ROUND CAP]" in result["content"]


# ── E16: 启动收尾 sweep ──────────────────────────────────────


@pytest.mark.asyncio
async def test_sweep_stale_agent_runs_marks_interrupted(tmpdir):
    from hiveweave.db import project as project_db
    from hiveweave.services.run_ledger import sweep_stale_agent_runs

    ws = str(tmpdir)
    conn = await project_db.ensure_project_db(ws)
    now = int(time.time() * 1000)
    await conn.execute(
        "INSERT INTO agent_runs "
        "(id, agent_id, activation_id, status, lease_expires_at, "
        "budget_llm_calls, budget_tool_calls, budget_elapsed_ms, "
        "actual_llm_calls, actual_tool_calls, started_at) "
        "VALUES (?, ?, ?, 'running', ?, 10, 10, 1000, 0, 0, ?)",
        ["r-stale", "a1", "act1", now + 1000, now],
    )
    await conn.execute(
        "INSERT INTO agent_runs "
        "(id, agent_id, activation_id, status, lease_expires_at, "
        "budget_llm_calls, budget_tool_calls, budget_elapsed_ms, "
        "actual_llm_calls, actual_tool_calls, started_at) "
        "VALUES (?, ?, ?, 'completed', ?, 10, 10, 1000, 0, 0, ?)",
        ["r-ok", "a1", "act1", now + 1000, now],
    )
    await conn.commit()

    n = await sweep_stale_agent_runs(ws)
    assert n == 1
    cur = await conn.execute("SELECT status FROM agent_runs WHERE id = 'r-stale'")
    row = await cur.fetchone()
    await cur.close()
    assert row["status"] == "interrupted"


# ── E9 配套：bash venv 提示 ──────────────────────────────────


def test_bash_venv_hint_appends_when_module_missing(monkeypatch):
    from hiveweave.tools.bash import _maybe_append_venv_hint

    monkeypatch.setattr(
        "hiveweave.services.venv_setup.project_venv_python",
        lambda ws: "D:/proj/.venv/Scripts/python.exe",
    )

    # 命中缺依赖 → 提示追加
    msg = "Traceback ... ModuleNotFoundError: No module named 'pandas'"
    out = _maybe_append_venv_hint("D:/proj", msg)
    assert "[venv hint]" in out and ".venv" in out

    # 未命中（正常错误）→ 原样返回
    plain = _maybe_append_venv_hint("D:/proj", "Error: file not found")
    assert plain == "Error: file not found"

    # 无 venv → 原样返回
    monkeypatch.setattr(
        "hiveweave.services.venv_setup.project_venv_python",
        lambda ws: None,
    )
    out2 = _maybe_append_venv_hint("D:/proj", msg)
    assert "[venv hint]" not in out2