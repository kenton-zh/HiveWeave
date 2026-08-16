"""Uvicorn/ASGI long-running server detection + spawn port injection (unit)."""

from __future__ import annotations

from hiveweave.services.process_registry import (
    clear_registry_for_tests,
    prepare_spawn_command,
)
from hiveweave.tools.bash import (
    _detect_dev_server_command,
    _should_offturn_trailing_amp,
    _strip_trailing_ampersand,
)


def test_detect_uvicorn_reload():
    assert _detect_dev_server_command("uvicorn app:app --reload") is not None


def test_detect_uv_run_uvicorn():
    assert _detect_dev_server_command("uv run uvicorn app:app") is not None
    assert _detect_dev_server_command(
        "uv run --no-sync uvicorn app:app --reload"
    ) is not None
    assert _detect_dev_server_command(
        "uv run --with fastapi uvicorn app:app"
    ) is not None


def test_detect_env_prefixed_uvicorn():
    assert _detect_dev_server_command(
        "FOO=1 uvicorn app:app --reload"
    ) is not None
    assert _detect_dev_server_command(
        "cd app && uvicorn app:app --reload"
    ) is not None


def test_detect_uv_run_with_uvicorn_as_dep_not_server():
    assert _detect_dev_server_command(
        "uv run --with uvicorn pytest"
    ) is None
    assert _detect_dev_server_command("pip show uvicorn") is None
    assert _detect_dev_server_command(
        "uv run --group uvicorn pytest"
    ) is None
    assert _detect_dev_server_command(
        "uv run --package uvicorn pytest"
    ) is None


def test_detect_python_m_uvicorn_extracts_port():
    assert (
        _detect_dev_server_command("python -m uvicorn app:app --port 8102")
        == 8102
    )


def test_detect_python3_and_pythonw_m_uvicorn():
    assert _detect_dev_server_command(
        "python3 -m uvicorn app:app --reload"
    ) is not None
    assert _detect_dev_server_command(
        "pythonw -m uvicorn app:app --port 8102"
    ) == 8102


def test_detect_uvicorn_trailing_ampersand():
    assert (
        _detect_dev_server_command("uvicorn app:app --port 8102 &") == 8102
    )


def test_detect_npm_run_build_still_none():
    assert _detect_dev_server_command("npm run build") is None


def test_detect_uvicorn_help_not_a_server():
    assert _detect_dev_server_command("uvicorn --help") is None
    assert _detect_dev_server_command("python -m uvicorn --version") is None


def test_strip_trailing_ampersand():
    assert (
        _strip_trailing_ampersand("uvicorn app:app --port 8102 &")
        == "uvicorn app:app --port 8102"
    )
    assert _strip_trailing_ampersand("sleep 10 &") == "sleep 10"
    assert _strip_trailing_ampersand("echo hi") == "echo hi"


def test_foreground_amp_routes_non_server_to_offturn():
    assert _should_offturn_trailing_amp("sleep 30 &") is True
    assert _should_offturn_trailing_amp("npm run build &") is True
    assert _should_offturn_trailing_amp("uvicorn app:app &") is False
    assert _should_offturn_trailing_amp("uvicorn app:app") is False
    assert _should_offturn_trailing_amp("sleep 30") is False


def test_prepare_spawn_injects_uvicorn_port():
    clear_registry_for_tests()
    cmd, env, err = prepare_spawn_command(
        "uvicorn app:app", project_id="p-uvicorn"
    )
    assert err is None
    assert "--port" in cmd
    assert "--strictPort" not in cmd
    assert "4000" not in cmd
    assert "5173" not in cmd
    assert "4173" not in cmd
    port = env.get("PORT")
    assert port
    assert port not in ("4000", "5173", "4173")
    assert f"--port {port}" in cmd


def test_prepare_spawn_keeps_explicit_uvicorn_port():
    clear_registry_for_tests()
    cmd, env, err = prepare_spawn_command(
        "uvicorn app:app --port 8102", project_id="p-uvicorn-fixed"
    )
    assert err is None
    assert "--port 8102" in cmd
    assert cmd.count("--port") == 1
    assert "HIVEWEAVE_RESERVED_PORTS" in env
    assert "--strictPort" not in cmd


def test_prepare_spawn_strips_amp_before_uvicorn_inject():
    clear_registry_for_tests()
    cmd, env, err = prepare_spawn_command(
        "uvicorn app:app &", project_id="p-uvicorn-amp"
    )
    assert err is None
    assert "&" not in cmd
    assert "--port" in cmd
    assert env.get("PORT")


def test_prepare_spawn_python_m_uvicorn_injects_port():
    clear_registry_for_tests()
    cmd, env, err = prepare_spawn_command(
        "python -m uvicorn app:app", project_id="p-uvicorn-pym"
    )
    assert err is None
    assert "--port" in cmd
    assert env.get("PORT")
    assert "--strictPort" not in cmd


def test_prepare_spawn_does_not_rewrite_uvicorn_as_dependency():
    """`--with uvicorn` is a dep, not a server — do not inject --port."""
    clear_registry_for_tests()
    cmd, env, err = prepare_spawn_command(
        "uv run --with uvicorn pytest", project_id="p-uvicorn-dep"
    )
    assert err is None
    assert "--port" not in cmd
    assert env.get("PORT") is None
    cmd2, env2, err2 = prepare_spawn_command(
        "uv run --group uvicorn pytest", project_id="p-uvicorn-group"
    )
    assert err2 is None
    assert "--port" not in cmd2
    assert env2.get("PORT") is None
