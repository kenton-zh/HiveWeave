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
    assert _should_offturn_trailing_amp("flask run &") is False
    assert _should_offturn_trailing_amp("gunicorn app:app &") is False
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


def test_prepare_spawn_does_not_rewrite_flask_as_dependency():
    clear_registry_for_tests()
    cmd, env, err = prepare_spawn_command(
        "uv run --with flask pytest", project_id="p-flask-dep"
    )
    assert err is None
    assert "--port" not in cmd
    assert env.get("PORT") is None
    cmd2, env2, err2 = prepare_spawn_command(
        "flask shell", project_id="p-flask-shell"
    )
    assert err2 is None
    assert "--port" not in cmd2
    assert env2.get("PORT") is None


def test_detect_python_m_app_server():
    assert _detect_dev_server_command("python -m app.server") is not None
    assert _detect_dev_server_command("python3 -m app.server") is not None
    assert _detect_dev_server_command("pythonw -m app.server --reload") is not None
    from hiveweave.tools.bash import _DEV_SERVER_TRIGGER_RE

    assert _DEV_SERVER_TRIGGER_RE.search("python -m app.server")
    assert _DEV_SERVER_TRIGGER_RE.search("python app/server.py")
    assert _DEV_SERVER_TRIGGER_RE.search(r"pythonw app\server.py")
    assert not _DEV_SERVER_TRIGGER_RE.search("uv run --with uvicorn pytest")
    assert not _DEV_SERVER_TRIGGER_RE.search("uv run --with uvicorn")


def test_detect_python_app_server_py_not_tests():
    assert _detect_dev_server_command("python app/server.py") is not None
    assert _detect_dev_server_command("python ./app/server.py") is not None
    assert _detect_dev_server_command(r"python app\server.py") is not None
    assert _detect_dev_server_command("python tests/app/server.py") is None
    assert _detect_dev_server_command("python -m pytest app/server.py") is None


def test_detect_app_server_help_not_a_server():
    assert _detect_dev_server_command("python -m app.server --help") is None
    assert _detect_dev_server_command("python app/server.py --version") is None


def test_prepare_spawn_app_server_does_not_inject_port_flag():
    clear_registry_for_tests()
    cmd, env, err = prepare_spawn_command(
        "python -m app.server", project_id="p-app-server"
    )
    assert err is None
    assert "--port" not in cmd
    assert "--strictPort" not in cmd
    assert env.get("PORT")
    assert env["PORT"] not in ("4000", "5173", "4173")


def test_prepare_spawn_app_server_keeps_explicit_port():
    clear_registry_for_tests()
    cmd, env, err = prepare_spawn_command(
        "python -m app.server --port 8102", project_id="p-app-fixed"
    )
    assert err is None
    assert "--port 8102" in cmd
    assert cmd.count("--port") == 1


def test_detect_flask_run():
    assert _detect_dev_server_command("flask run") is not None
    assert _detect_dev_server_command("python -m flask run") is not None
    assert _detect_dev_server_command("python -m flask --app hello run") is not None
    assert _detect_dev_server_command("FLASK_APP=hello flask run") is not None
    assert _detect_dev_server_command("uv run flask run") is not None
    assert _detect_dev_server_command("cd app && flask run") is not None


def test_detect_flask_non_server():
    assert _detect_dev_server_command("pip install flask") is None
    assert _detect_dev_server_command("uv run --with flask pytest") is None
    assert _detect_dev_server_command("flask --help") is None
    assert _detect_dev_server_command("flask shell") is None
    assert _detect_dev_server_command("cd flask ; npm run") is None
    assert _detect_dev_server_command("echo flask run") is None
    assert _detect_dev_server_command("python -c flask run") is None


def test_detect_gunicorn():
    assert _detect_dev_server_command("gunicorn app:app") is not None
    assert _detect_dev_server_command("python -m gunicorn app:app") is not None
    assert _detect_dev_server_command("uv run gunicorn app:app") is not None
    assert _detect_dev_server_command(
        "gunicorn app:app --bind 0.0.0.0:8102"
    ) == 8102


def test_detect_gunicorn_non_server():
    assert _detect_dev_server_command("uv run --with gunicorn pytest") is None
    assert _detect_dev_server_command("uv run --with  gunicorn pytest") is None
    assert _detect_dev_server_command("pip show gunicorn") is None
    assert _detect_dev_server_command("gunicorn --help") is None
    assert _detect_dev_server_command("gunicorn --version") is None


def test_prepare_spawn_injects_flask_port():
    clear_registry_for_tests()
    cmd, env, err = prepare_spawn_command(
        "flask run", project_id="p-flask"
    )
    assert err is None
    port = env.get("PORT")
    assert port
    assert port not in ("4000", "5173", "4173")
    assert f"--port {port}" in cmd
    assert "--bind" not in cmd


def test_prepare_spawn_injects_gunicorn_bind_not_port():
    clear_registry_for_tests()
    cmd, env, err = prepare_spawn_command(
        "gunicorn app:app", project_id="p-gunicorn"
    )
    assert err is None
    port = env.get("PORT")
    assert port
    assert port not in ("4000", "5173", "4173")
    assert f"--bind 0.0.0.0:{port}" in cmd
    assert "--port" not in cmd


def test_prepare_spawn_keeps_explicit_gunicorn_bind():
    clear_registry_for_tests()
    cmd, env, err = prepare_spawn_command(
        "gunicorn app:app --bind 0.0.0.0:8102", project_id="p-gunicorn-fixed"
    )
    assert err is None
    assert "--bind 0.0.0.0:8102" in cmd
    assert cmd.count("--bind") == 1
    assert "--port" not in cmd


def test_prepare_spawn_gunicorn_reserved_bind_rejected():
    clear_registry_for_tests()
    _cmd, _env, err = prepare_spawn_command(
        "gunicorn app:app --bind 0.0.0.0:4000", project_id="p-gunicorn-rsv"
    )
    assert err
    assert "4000" in err


def test_extract_ports_ignores_git_checkout_b():
    from hiveweave.services.process_registry import extract_ports_from_command

    assert extract_ports_from_command("git checkout -b 4000") == []
    assert extract_ports_from_command(
        "gunicorn app:app -b 0.0.0.0:8102"
    ) == [8102]


def test_prepare_spawn_does_not_rewrite_gunicorn_as_dependency():
    clear_registry_for_tests()
    for cmd0 in (
        "uv run --with gunicorn pytest",
        "uv run --with  gunicorn pytest",
        "uv run --extra gunicorn pytest",
    ):
        cmd, env, err = prepare_spawn_command(cmd0, project_id="p-gdep")
        assert err is None
        assert "--bind" not in cmd
        assert "--port" not in cmd
        assert env.get("PORT") is None


def test_parse_netstat_listen_matches_descendant_pid():
    from hiveweave.services.process_registry import (
        descendant_pids,
        parse_netstat_listen_ports,
        pick_observed_listen_port,
    )
    from hiveweave.services import process_registry as pr

    text = (
        "  TCP    0.0.0.0:8000    0.0.0.0:0    LISTENING    99\n"
        "  TCP    0.0.0.0:4000    0.0.0.0:0    LISTENING    99\n"
    )
    assert parse_netstat_listen_ports(text, {1}) == []
    assert parse_netstat_listen_ports(text, {1, 99}) == [4000, 8000]
    assert descendant_pids(10, {11: 10, 12: 11, 10: 1}) == {10, 11, 12}

    original = pr.listening_ports_for_pid
    try:
        pr.listening_ports_for_pid = (  # type: ignore[method-assign]
            lambda _pid: parse_netstat_listen_ports(text, {1, 99})
        )
        assert pick_observed_listen_port(1, 3000) == 8000
        assert pick_observed_listen_port(1, 4000) == 8000
    finally:
        pr.listening_ports_for_pid = original


def test_pick_observed_listen_port_prefers_preferred(monkeypatch):
    from hiveweave.services import process_registry as pr

    monkeypatch.setattr(
        pr, "listening_ports_for_pid", lambda _pid: [8000, 3000, 4000]
    )
    assert pr.pick_observed_listen_port(1, 3000) == 3000
    assert pr.pick_observed_listen_port(1, 9999) == 8000
    assert pr.pick_observed_listen_port(1, 4000) == 8000
    monkeypatch.setattr(pr, "listening_ports_for_pid", lambda _pid: [4000, 5173])
    assert pr.pick_observed_listen_port(1, 4000) is None
    monkeypatch.setattr(pr, "listening_ports_for_pid", lambda _pid: [])
    assert pr.pick_observed_listen_port(1, 3000) is None
