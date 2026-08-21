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


def test_detect_http_server():
    """DSH-18 事故回归：`python -m http.server 8787` 曾绕过触发正则 →
    bg-bash offturn job → 对永不结束的进程挂 waiting_on 契约 → 死等。
    现在必须识别为长驻服务并提取位置参数端口。"""
    assert _detect_dev_server_command("python -m http.server 8787") == 8787
    assert _detect_dev_server_command("python -m http.server") == 0
    assert _detect_dev_server_command("python3 -m http.server 8000") == 8000
    assert _detect_dev_server_command("python -m http.server 8787 &") == 8787
    assert _detect_dev_server_command("uv run python -m http.server 9000") == 9000
    assert _detect_dev_server_command("python -m http.server 5173") == 5173
    # Windows py 启动器 / 版本号后缀写法
    assert _detect_dev_server_command("py -m http.server 8787") == 8787
    assert _detect_dev_server_command("python3.11 -m http.server 8787") == 8787
    # 旗标前置的位置端口
    assert _detect_dev_server_command(
        "python -m http.server --directory pub 8787"
    ) == 8787
    assert _detect_dev_server_command("python -m http.server -b 127.0.0.1 8787") == 8787
    # --help 立即退出，不是长驻服务
    assert _detect_dev_server_command("python -m http.server --help") is None
    assert _detect_dev_server_command("npx serve --help") is None


def test_detect_http_server_non_server():
    assert _detect_dev_server_command("pip install http.server") is None
    assert _detect_dev_server_command("pytest tests/http_server_test.py") is None
    assert _detect_dev_server_command("cat http.server.log") is None


def test_detect_npx_static_servers():
    assert _detect_dev_server_command("npx serve") is not None
    assert _detect_dev_server_command("npx serve dist") is not None
    assert _detect_dev_server_command("npx -y serve") is not None
    assert _detect_dev_server_command("npx http-server -p 8080") == 8080
    assert _detect_dev_server_command("npx live-server") is not None
    assert _detect_dev_server_command("npm run serve") is not None
    assert _detect_dev_server_command("pnpm run serve") is not None
    assert _detect_dev_server_command("npx serve-handler") is None


def test_extract_http_server_positional_port():
    from hiveweave.services.process_registry import (
        check_command_reserved_ports,
        extract_ports_from_command,
    )

    assert extract_ports_from_command("python -m http.server 8787") == [8787]
    assert extract_ports_from_command("python -m http.server") == []
    # 旗标前置的位置端口也要提取（argparse 允许 -b/-d/--cgi 在端口前）
    assert extract_ports_from_command(
        "python -m http.server --directory pub 8787"
    ) == [8787]
    assert extract_ports_from_command(
        "python -m http.server -b 127.0.0.1 9000"
    ) == [9000]
    # 位置参数指定的保留端口必须被拦下（与 --port 5173 同罪）
    assert check_command_reserved_ports("python -m http.server 5173")
    assert check_command_reserved_ports("python -m http.server --cgi 5173")
    assert check_command_reserved_ports("python -m http.server 8787") is None


def test_http_server_trailing_amp_routes_to_dev_server():
    # 前台 `cmd &`：识别为长驻服务 → 不走 offturn job（禁止 shell 脱管）
    assert _should_offturn_trailing_amp("python -m http.server 8787 &") is False


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
