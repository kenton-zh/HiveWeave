"""host_env 探测框架测试（platform-issue-remediation Phase 0）。

验收子集（计划 §1）：探测项齐全 + fail-closed + 超时拒绝 0 + 懒缓存语义。
子进程缝在 runner.run_command —— 探测模块 ``from ..runner import
run_command`` 是各自命名空间的引用，monkeypatch 必须打在探测模块上。
"""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.services.host_env import (
    CapabilityLevel,
    CapabilityUnavailableError,
    ProbeResult,
    ProbeTiming,
    aget_capability,
    capability_snapshot,
    get_capability,
    register,
    register_builtin_probes,
    registry,
    reset_runner,
    run_startup_probes,
    runner,
)
from hiveweave.services.host_env.registry import reset_registry


@pytest.fixture(autouse=True)
def _clean_host_env():
    """每个测试前清注册表 + 缓存，之后按需重装内置探测项。"""
    reset_registry()
    reset_runner()
    yield
    reset_registry()
    reset_runner()


def _cmd_result(stdout: str, returncode: int = 0, stderr: str = ""):
    """造一个 CompletedProcess 等价物（探测函数只读这三个字段）。"""
    return type("R", (), {"stdout": stdout, "stderr": stderr, "returncode": returncode})


def _fake_run(results: dict[str, object]):
    """按 argv[0] 返回预置结果；未命中 → FileNotFoundError（模拟不在 PATH）。"""

    def _impl(argv, *, timeout_s, cwd=None):
        if argv[0] not in results:
            raise FileNotFoundError(argv[0])
        return results[argv[0]]

    return _impl


def _full(name: str) -> ProbeResult:
    return ProbeResult(name=name, level=CapabilityLevel.FULL)


# ── 注册表纪律（invariants 三条） ────────────────────────────────────────


def test_duplicate_registration_raises():
    register("x", lambda **_: _full("x"))
    with pytest.raises(RuntimeError, match="already registered"):
        register("x", lambda **_: _full("x"))


def test_disposer_unregisters_and_evicts_cache():
    dispose = register("x", lambda **_: _full("x"), timing=ProbeTiming.LAZY)
    get_capability("x")
    dispose()
    with pytest.raises(KeyError):
        registry.get_entry("x")
    # 重注册后缓存是空的 —— 调用计数验证真跑了新探测（invariants 纪律 2）。
    calls = {"n": 0}

    def _counting(**_):
        calls["n"] += 1
        return _full("x")

    register("x", _counting, timing=ProbeTiming.LAZY)
    get_capability("x")
    assert calls["n"] == 1


# ── runner：fail-closed / 超时 / 懒缓存 ──────────────────────────────────


def test_probe_exception_is_fail_closed():
    def _boom(**_):
        raise RuntimeError("disk on fire")

    register("boom", _boom, timing=ProbeTiming.LAZY)
    with pytest.raises(CapabilityUnavailableError, match="RuntimeError"):
        get_capability("boom")


def test_timeout_rejects_zero_and_negative():
    with pytest.raises(ValueError, match="拒绝 0"):
        runner.reject_bad_timeout(0, "x")
    with pytest.raises(ValueError):
        runner.reject_bad_timeout(-1, "x")
    # run_command 同样拒绝 0
    with pytest.raises(ValueError):
        runner.run_command(["git", "--version"], timeout_s=0)


def test_probe_wall_clock_timeout():
    def _hang(**_):
        time.sleep(2)

    register("hang", _hang, timing=ProbeTiming.LAZY)
    with patch.object(runner, "DEFAULT_PROBE_TIMEOUT_S", 0.05):
        with pytest.raises(CapabilityUnavailableError, match="wall clock"):
            get_capability("hang")


def test_bad_probe_contract_rejected():
    """探测函数返回非 ProbeResult = 违约 → fail-closed，不是「大概能用」。"""
    register("badret", lambda **_: {"level": "full"}, timing=ProbeTiming.LAZY)
    with pytest.raises(CapabilityUnavailableError, match="expected ProbeResult"):
        get_capability("badret")


def test_lazy_probe_runs_once_then_cache_hit():
    calls = {"n": 0}

    def _probe(**_):
        calls["n"] += 1
        return ProbeResult(name="lazy", level=CapabilityLevel.FULL, detail="v1")

    register("lazy", _probe, timing=ProbeTiming.LAZY)
    r1 = get_capability("lazy")
    r2 = get_capability("lazy")
    assert calls["n"] == 1
    assert r1 is r2  # 不可变结果对象直接复用


def test_lazy_probe_caches_per_params():
    calls = {"n": 0}

    def _probe(*, path, **_):
        calls["n"] += 1
        return ProbeResult(name="ws", level=CapabilityLevel.FULL, detail=path)

    register("ws", _probe, timing=ProbeTiming.LAZY)
    a = get_capability("ws", path="/a")
    a2 = get_capability("ws", path="/a")
    b = get_capability("ws", path="/b")
    assert calls["n"] == 2
    assert a is a2
    assert a.detail == "/a"
    assert b.detail == "/b"


def test_negative_cache_unavailability():
    calls = {"n": 0}

    def _fail(**_):
        calls["n"] += 1
        raise CapabilityUnavailableError("nope", probe="nf", reason="x")

    register("nf", _fail, timing=ProbeTiming.LAZY)
    with pytest.raises(CapabilityUnavailableError):
        get_capability("nf")
    with pytest.raises(CapabilityUnavailableError):
        get_capability("nf")
    assert calls["n"] == 1  # 不可用也缓存（DSH ??= 语义）


def test_startup_probe_must_run_first():
    register("st", lambda **_: _full("st"), timing=ProbeTiming.STARTUP)
    with pytest.raises(CapabilityUnavailableError, match="not run yet"):
        get_capability("st")


def test_run_startup_probes_records_results_and_unavailability():
    register("ok1", lambda **_: _full("ok1"), timing=ProbeTiming.STARTUP)

    def _bad(**_):
        raise CapabilityUnavailableError("broken", probe="bad", reason="probe-error")

    register("bad", _bad, timing=ProbeTiming.STARTUP)
    results = run_startup_probes()
    assert set(results) == {"ok1"}
    assert get_capability("ok1").level is CapabilityLevel.FULL
    with pytest.raises(CapabilityUnavailableError, match="broken"):
        get_capability("bad")
    assert capability_snapshot()["__startup_done__"] is True


async def test_aget_capability_lazy_runs_in_thread():
    import threading as _t

    main_tid = _t.get_ident()
    seen = {"tid": None}

    def _probe(**_):
        # P2-9：真钉住「跑在事件循环之外的线程里」，同步直调会暴露。
        seen["tid"] = _t.get_ident()
        return ProbeResult(name="al", level=CapabilityLevel.PARTIAL, detail="p")

    register("al", _probe, timing=ProbeTiming.LAZY)
    r = await aget_capability("al")
    assert seen["tid"] is not None
    assert seen["tid"] != main_tid
    assert r.level is CapabilityLevel.PARTIAL


# ── 内置探测项 ───────────────────────────────────────────────────────────

STARTUP_NAMES = {
    "platform.os", "platform.arch", "shell.pwsh", "shell.git_bash",
    "sandbox.acl", "toolchain.git", "toolchain.node", "toolchain.npm",
    "workspace.cache_writable",
}


def test_builtin_probes_register_idempotently():
    register_builtin_probes()
    names = {e.name for e in registry.all_entries()}
    assert STARTUP_NAMES <= names
    register_builtin_probes()  # 二次调用 no-op（不抛 duplicate）


def test_platform_probes_are_full_with_data():
    register_builtin_probes()
    run_startup_probes()
    os_r = get_capability("platform.os")
    arch_r = get_capability("platform.arch")
    assert os_r.level is CapabilityLevel.FULL
    assert os_r.data["system"] in ("Windows", "Linux", "Darwin")
    assert arch_r.level is CapabilityLevel.FULL
    assert arch_r.data["machine"]


def test_pwsh_full_when_ge_76():
    register_builtin_probes()
    import hiveweave.services.host_env.probes.shells as shells_mod

    with patch.object(
        shells_mod, "run_command",
        lambda argv, **_: _cmd_result("7.6.1\n"),
    ):
        reset_runner()
        run_startup_probes()
        r = get_capability("shell.pwsh")
    assert r.level is CapabilityLevel.FULL
    assert r.data["version"] == "7.6.1"


def test_pwsh_partial_when_lt_76():
    register_builtin_probes()
    import hiveweave.services.host_env.probes.shells as shells_mod

    with patch.object(
        shells_mod, "run_command",
        lambda argv, **_: _cmd_result("7.4.1"),
    ):
        reset_runner()
        run_startup_probes()
        assert get_capability("shell.pwsh").level is CapabilityLevel.PARTIAL


def test_pwsh_partial_when_version_unreadable():
    register_builtin_probes()
    import hiveweave.services.host_env.probes.shells as shells_mod

    with patch.object(
        shells_mod, "run_command",
        lambda argv, **_: _cmd_result("not-a-version"),
    ):
        reset_runner()
        run_startup_probes()
        r = get_capability("shell.pwsh")
    assert r.level is CapabilityLevel.PARTIAL
    assert "unreadable" in r.detail


def test_pwsh_unavailable_when_missing():
    register_builtin_probes()
    import hiveweave.services.host_env.probes.shells as shells_mod

    with patch.object(shells_mod, "run_command", _fake_run({})):
        reset_runner()
        run_startup_probes()
        with pytest.raises(CapabilityUnavailableError, match="FileNotFoundError"):
            get_capability("shell.pwsh")


def test_git_bash_requires_msys_tag():
    register_builtin_probes()
    import hiveweave.services.host_env.probes.shells as shells_mod

    # 有 bash 但无 msys（WSL/Linux bash）→ unavailable，而不是 partial。
    with patch.object(
        shells_mod, "run_command",
        lambda argv, **_: _cmd_result(
            "GNU bash, version 5.1 (x86_64-pc-linux-gnu)")
    ):
        reset_runner()
        run_startup_probes()
        with pytest.raises(CapabilityUnavailableError, match="not Git Bash"):
            get_capability("shell.git_bash")
    # MSYS 标记在（Git Bash）→ full
    with patch.object(
        shells_mod, "run_command",
        lambda argv, **_: _cmd_result(
            "GNU bash, version 5.2.26(1)-release (x86_64-pc-msys)")
    ):
        reset_runner()
        run_startup_probes()
        assert get_capability("shell.git_bash").level is CapabilityLevel.FULL


def test_sandbox_acl_unavailable_on_non_windows():
    register_builtin_probes()
    import hiveweave.services.host_env.probes.sandbox as sandbox_mod

    with patch.object(sandbox_mod.sys, "platform", "linux"):
        reset_runner()
        run_startup_probes()
        with pytest.raises(CapabilityUnavailableError, match="windows-only"):
            get_capability("sandbox.acl")


def test_sandbox_acl_full_with_roundtrip():
    register_builtin_probes()
    import hiveweave.services.host_env.probes.sandbox as sandbox_mod

    def fake_run(argv, *, timeout_s, cwd=None, encoding="utf-8"):
        probe_dir = argv[1]
        return _cmd_result(f"{probe_dir} NT AUTHORITY\\SYSTEM:(OI)(CI)(F)\n")

    with patch.object(sandbox_mod.sys, "platform", "win32"):
        with patch.object(sandbox_mod, "run_command", fake_run):
            reset_runner()
            run_startup_probes()
            assert get_capability("sandbox.acl").level is CapabilityLevel.FULL


def test_sandbox_acl_partial_when_readback_mismatch():
    register_builtin_probes()
    import hiveweave.services.host_env.probes.sandbox as sandbox_mod

    # icacls 退出 0 但输出不含探测目录 → partial（验证不完整）
    with patch.object(sandbox_mod.sys, "platform", "win32"):
        with patch.object(
            sandbox_mod, "run_command",
            lambda argv, **_: _cmd_result("unrelated output"),
        ):
            reset_runner()
            run_startup_probes()
            assert get_capability("sandbox.acl").level is CapabilityLevel.PARTIAL


def test_workspace_cache_writable_full_and_missing_path(tmp_path: Path):
    register_builtin_probes()
    r = get_capability("workspace.cache_writable", path=str(tmp_path))
    assert r.level is CapabilityLevel.FULL
    assert r.data["cache_dir"].endswith(".hiveweave-cache")
    with pytest.raises(CapabilityUnavailableError, match="does not exist"):
        get_capability("workspace.cache_writable", path=str(tmp_path / "nope"))


def test_workspace_cache_writable_partial_when_unlink_locked(tmp_path: Path):
    register_builtin_probes()
    real_unlink = Path.unlink

    def locked_unlink(self, *a, **k):
        if self.name.startswith(".host-env-probe-"):
            raise PermissionError(13, "文件被另一进程占用")
        return real_unlink(self, *a, **k)

    mp = pytest.MonkeyPatch()
    mp.setattr("pathlib.Path.unlink", locked_unlink)
    try:
        r = get_capability("workspace.cache_writable", path=str(tmp_path))
    finally:
        mp.undo()
    assert r.level is CapabilityLevel.PARTIAL
    assert "leftover_probe" in r.data


def test_toolchain_probes_report_versions():
    register_builtin_probes()
    import hiveweave.services.host_env.probes.toolchain as tc_mod

    fake = _fake_run({
        "git": _cmd_result("git version 2.45.0"),
        "node": _cmd_result("v22.20.0"),
        "npm": _cmd_result("10.9.0"),
    })
    with patch.object(tc_mod, "run_command", fake):
        for name, version in (
            ("toolchain.git", "git version 2.45.0"),
            ("toolchain.node", "v22.20.0"),
            ("toolchain.npm", "10.9.0"),
        ):
            r = get_capability(name)
            assert r.level is CapabilityLevel.FULL
            assert r.data["version"] == version


def test_result_is_frozen_and_data_mapping_is_readonly():
    data = {"a": 1}
    r = ProbeResult(name="t", level=CapabilityLevel.FULL, data=data)
    data["a"] = 999  # 外部改原字典不得影响结果
    assert r.data["a"] == 1
    with pytest.raises(Exception):
        r.level = CapabilityLevel.PARTIAL  # type: ignore[misc]
    with pytest.raises(TypeError):
        r.data["a"] = 999  # P2-4：data 本体也不可写（MappingProxyType）


def test_run_command_missing_binary_raises_unavailable():
    with pytest.raises(CapabilityUnavailableError, match="not found on PATH"):
        runner.run_command(
            ["hiveweave-no-such-binary-xyz", "--version"], timeout_s=5
        )


# ── 审计修复回归（P1-2 / P1-3 / P2-6 / P2-8） ────────────────────────────


def test_lazy_concurrent_first_visit_runs_once():
    """P1-2：并发首访同一 LAZY 项，探测函数只执行一次（in-flight 去重）。"""
    calls = {"n": 0}

    def _probe(**_):
        calls["n"] += 1
        time.sleep(0.2)  # 放大竞态窗口
        return _full("conc")

    register("conc", _probe, timing=ProbeTiming.LAZY)
    results: list = []

    def _worker():
        results.append(get_capability("conc"))

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert calls["n"] == 1
    assert len(results) == 4
    assert all(r.level is CapabilityLevel.FULL for r in results)


def test_transient_probe_error_not_negative_cached():
    """P2-6：probe-error 是瞬态 —— 第二次调用必须真重探。"""
    flip = {"fail": True}

    def _flaky(**_):
        if flip["fail"]:
            raise CapabilityUnavailableError(
                "boom", probe="flaky", reason="probe-error"
            )
        return _full("flaky")

    register("flaky", _flaky, timing=ProbeTiming.LAZY)
    with pytest.raises(CapabilityUnavailableError):
        get_capability("flaky")
    flip["fail"] = False
    assert get_capability("flaky").level is CapabilityLevel.FULL


def test_structural_unavailable_is_negative_cached():
    """结构性不可用（not-found）仍负缓存 —— 与瞬态相区分。"""
    calls = {"n": 0}

    def _missing(**_):
        calls["n"] += 1
        raise CapabilityUnavailableError(
            "gone", probe="missing", reason="not-found"
        )

    register("missing", _missing, timing=ProbeTiming.LAZY)
    with pytest.raises(CapabilityUnavailableError):
        get_capability("missing")
    with pytest.raises(CapabilityUnavailableError):
        get_capability("missing")
    assert calls["n"] == 1


def test_timeout_s_forwarded_to_probe():
    """P1-3：timeout_s 必须真传给探测函数，不能校验完就丢。"""
    seen = {"t": None}

    def _probe(*, timeout_s, **_):
        seen["t"] = timeout_s
        return _full("tt")

    register("tt", _probe, timing=ProbeTiming.LAZY)
    get_capability("tt", timeout_s=1.5)
    assert seen["t"] == 1.5


def test_startup_probe_rejects_params_with_clear_error():
    """P2-8：STARTUP 项带参数查询要有指对方向的报错，不是「没跑过」。"""
    register("st2", lambda **_: _full("st2"), timing=ProbeTiming.STARTUP)
    run_startup_probes()
    with pytest.raises(CapabilityUnavailableError, match="takes no parameters"):
        get_capability("st2", foo=1)
