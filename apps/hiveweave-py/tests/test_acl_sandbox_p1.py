"""P1 单元测试（spec §5.5b① / §5.7 / §13 遥测与哨兵）—— 跨平台。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from hiveweave.services.acl_sandbox import telemetry
from hiveweave.services.acl_sandbox.integration import (
    acl_sandbox_active,
    build_confined_command,
)
from hiveweave.services.acl_sandbox.sentinel import _probe_passed
from hiveweave.services.acl_sandbox.service import (
    _build_sandbox_env,
    ensure_standing_grants,
)
from hiveweave.tools.bash import _native_shaped, _run_sandboxed
from hiveweave.tools.file import _resolve_for_read_detail


# ── integration ─────────────────────────────────────────────
def test_acl_sandbox_active_default_on() -> None:
    """P3 默认 on：acl_sandbox 默认 True；Windows 下 acl_sandbox_active() True。"""
    from hiveweave.config import settings

    assert settings.acl_sandbox is True
    assert acl_sandbox_active() is (sys.platform.startswith("win"))


def test_build_confined_command_wraps_shell() -> None:
    """受限 shell 命令行：pwsh 优先 + cmd 兜底（Git Bash 受限不可用，S1）。"""
    cmd = build_confined_command("echo hi")
    assert isinstance(cmd, str) and cmd
    assert '"' in cmd
    assert "echo hi" in cmd


# ── P1-3（B 结构解）：受限 bash 不再词典翻译，命令 verbatim ────────
def test_confined_bash_is_verbatim_pwsh_plus_encoding_pin() -> None:
    """受限 bash 命令原样交给 pwsh（不再经 _normalize_for_pwsh 翻译）；
    pwsh 分支带 UTF-8 编码钉；unix-only 由 gate 前置拒绝（test_pwsh_dialect_gate）。
    旧 _normalize_for_pwsh 翻译断言已随 P1-3 退役删除。"""
    from hiveweave.tools.bash import PWSH_ENCODING_PREAMBLE
    from hiveweave.services.acl_sandbox.integration import build_confined_argv

    for cmd in ("git status", "ls -la", "head -5 f.txt"):
        argv = build_confined_argv(cmd, dialect="bash")
        assert argv[-1].endswith(cmd), (cmd, argv[-1])  # verbatim 无翻译
        if str(argv[0]).lower().find("pwsh") != -1:
            assert argv[-1].startswith(PWSH_ENCODING_PREAMBLE)

    argv2 = build_confined_argv("Get-Content x.txt", dialect="pwsh")
    assert argv2[-1].endswith("Get-Content x.txt")


# ── bash 接线 ───────────────────────────────────────────────
async def test_run_sandboxed_returns_none_when_off(monkeypatch) -> None:
    """沙箱 off → _run_sandboxed 返回 None（调用方回退 native）。"""
    from hiveweave.config import settings

    monkeypatch.setattr(settings, "acl_sandbox", False)
    r = await _run_sandboxed(
        "echo hi", "/tmp", 30,
        workspace_path="/tmp", agent_id="A001", project_id=None, entry="bash",
    )
    assert r is None


def test_native_shaped_normalizes() -> None:
    """spawn_confined 结果 → native 形态（output/error 归一）。"""
    r = _native_shaped({
        "exit_code": 0, "stdout": "out", "stderr": "err", "timed_out": False,
    })
    assert r["output"] == "out\nerr"
    assert r["error"] is None
    assert r["exit_code"] == 0


# ── env / telemetry ─────────────────────────────────────────
def test_sandbox_env_env_extra() -> None:
    """env_extra 覆盖在白名单之上（dev server PORT 注入）。"""
    env = _build_sandbox_env(
        r"D:\ws", r"D:\ws\.hiveweave-cache", r"D:\ws\tmp",
        env_extra={"PORT": "3001", "VITE_PORT": "3001"},
    )
    assert env["PORT"] == "3001"
    assert env["TEMP"] == r"D:\ws\tmp"


def test_telemetry_snapshot() -> None:
    telemetry.reset_for_tests()
    telemetry.record_fail_closed("CreateRestrictedToken")
    telemetry.record_rejection(True)
    telemetry.record_rejection(False)
    telemetry.record_mint_ms(10.0)
    telemetry.record_propagation_ms(5.0)
    s = telemetry.snapshot()
    assert s["fail_closed_count"] == 1
    assert s["runs_total"] == 2
    assert s["rejection_hits"] == 1
    assert s["rejection_hit_rate"] == 0.5
    assert s["mint_count"] == 1
    assert s["propagation_count"] == 1


# ── 外部只读参考（§5.5b①） ─────────────────────────────────
def test_read_resolution_extra_dirs(tmp_path: Path) -> None:
    """读白名单扩展：项目根 ∪ extra_read_dirs 内可读，外仍拒。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    extra = tmp_path / "ref"
    extra.mkdir()
    outside = tmp_path / "out"
    outside.mkdir()

    f_in_extra = extra / "a.txt"
    f_outside = outside / "b.txt"

    full, hint = _resolve_for_read_detail(
        str(ws), str(f_in_extra), str(ws), [str(extra)]
    )
    assert full == str(f_in_extra.resolve()) and hint is None

    # 未配置该 extra → 拒
    full2, _ = _resolve_for_read_detail(str(ws), str(f_in_extra), str(ws))
    assert full2 is None

    # 配置了 extra，但路径在 extra 之外 → 拒
    full3, _ = _resolve_for_read_detail(
        str(ws), str(f_outside), str(ws), [str(extra)]
    )
    assert full3 is None


def test_read_resolution_still_allows_project_root(tmp_path: Path) -> None:
    """带 extra_dirs 时项目根内路径不受影响。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "x.py").write_text("x", encoding="utf-8")
    full, _ = _resolve_for_read_detail(
        str(ws), "x.py", str(ws), [str(tmp_path / "ref")]
    )
    assert full == str((ws / "x.py").resolve())


# ── ensure_standing_grants off no-op ────────────────────────
async def test_ensure_standing_grants_off_noop(monkeypatch, tmp_path: Path) -> None:
    """沙箱 off → ensure_standing_grants 直接返回（创建钩子不阻塞）。"""
    from hiveweave.config import settings

    monkeypatch.setattr(settings, "acl_sandbox", False)
    await ensure_standing_grants(
        workspace_path=str(tmp_path), project_workspace_path=str(tmp_path)
    )


# ── 哨兵探针判定 ────────────────────────────────────────────
def test_sentinel_probe_passed() -> None:
    # TEMP 标记：受限下 TEMP 指向 sandbox-temp
    assert _probe_passed({
        "output": "C:\\ws\\.hiveweave\\sandbox-temp\\A", "exit_code": 0,
    })["ok"] is True
    assert _probe_passed({
        "output": "C:\\Users\\u\\AppData\\Local\\Temp", "exit_code": 0,
    })["ok"] is False
    assert _probe_passed(None)["ok"] is False
    assert _probe_passed({"stdout": "sandbox-temp", "exit_code": 0})["ok"] is True
