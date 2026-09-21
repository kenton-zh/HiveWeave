"""P1-8a 新-④：dev server spawn 路径也要做**可移植性别名**归一化。

病灶（P1-8a 审计 Q4 独立扫出）：`process_registry.prepare_spawn_command` →
`hidden_popen(cmd, shell=True)`（Windows 上 cmd /c）起长驻进程，这条链
**从不**经过 `_normalize_command` ⇒ `python3 -m uvicorn …` 与 bash 工具四个分支语义不一致
（Windows 上 python3 要么是 Store stub、要么不存在）。
"""

from __future__ import annotations

from hiveweave.services.process_registry import prepare_spawn_command


def _cmd(raw: str) -> str:
    command, _env, err, _meta = prepare_spawn_command(raw)
    assert err is None, err
    return command


def test_python3_is_normalized_for_dev_server():
    got = _cmd("python3 -m uvicorn app:app --reload")
    assert "python3" not in got
    assert got.startswith("python -m uvicorn")


def test_pip3_is_normalized_too():
    assert "pip3" not in _cmd("pip3 install -r requirements.txt")


def test_version_and_path_forms_are_preserved():
    """边界不许被改坏（与 bash 侧同一套正则）。"""
    assert "python3.11" in _cmd("python3.11 -m http.server 8787")
    assert "python3/bin" in _cmd("C:/tools/python3/bin/serve")


def test_unix_verbs_are_not_mapped():
    """`skip_cmd_mapping=True`：不做 unix→cmd 动词映射（起服务命令的参数语义不能动）。"""
    got = _cmd("sh -c 'rm -rf tmp && ls -la'")
    assert "ls -la" in got and "rm -rf" in got
