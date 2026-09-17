"""fixplan #4：``start_dev_server`` 归因错位收尾 —— 早退回执按事实区分成败。

旧形态（TEST_DSH_56 实测）：越界/一次性命令 ``exit=0`` 执行**成功**却回执
``status=failed``（文案 ``Dev server exited (code=0)``）⇒ agent 须额外探针
确认（多花一轮），反向风险是误信 failed 而重试 ⇒ **重复副作用**。

新形态（本文件钉住）：``_early_exit_receipt`` 正反两侧都不谎报 ——
- ``exit_code=0`` ⇒ ``ok(exit_code=0, server_listening=False)``，且正文
  明说「服务没在跑」（真长驻服务秒退不会被误报成功）；
- ``exit_code≠0`` ⇒ ``err(fact="command_failed", exit_code=N)``——命令
  执行了且失败（不是 ``runner_failed`` 的"从未执行"）。
"""

from __future__ import annotations

from pathlib import Path

from hiveweave.tools.dev_server_tools import _early_exit_receipt


def _log(tmp_path: Path, body: str = "") -> Path:
    p = tmp_path / "devserver.log"
    p.write_text(body, encoding="utf-8")
    return p


def test_exit_zero_reports_success_with_facts(tmp_path):
    """exit=0 ⇒ 成功回执 + exit_code 事实 + 「服务没在跑」的明说。"""
    log = _log(tmp_path, "server booted then exited cleanly")
    r = _early_exit_receipt(0, "python -m app", log)
    assert r.success is True, r.error
    d = r.to_dict()
    assert d["exit_code"] == 0
    assert d["server_listening"] is False
    # 事实位一致：成功回执不得带任何失败归因位
    assert d.get("fact") is None
    assert not d.get("runner_failed") and not d.get("command_failed")
    # 不谎报"服务器在跑"
    assert "NOT running" in r.output
    assert "exit_code=0" in r.output
    assert "python -m app" in r.output


def test_exit_zero_with_log_tail(tmp_path):
    log = _log(tmp_path, "line1\nline2\n")
    r = _early_exit_receipt(0, "cmd", log)
    assert r.success is True
    assert "log tail" in r.output
    assert "line2" in r.output


def test_exit_nonzero_reports_failed_with_command_failed_fact(tmp_path):
    """exit≠0 ⇒ 失败回执 + command_failed 事实位（命令跑了但失败，
    不是 runner_failed 的"从未执行"——归因不能错位）。"""
    log = _log(tmp_path, "Traceback ...")
    r = _early_exit_receipt(1, "npm run dev", log)
    assert r.success is False
    assert r.fact == "command_failed"
    d = r.to_dict()
    assert d["exit_code"] == 1
    assert "exit_code=1" in (r.error or "")
    assert "npm run dev" in (r.error or "")


def test_missing_log_file_still_receipts(tmp_path):
    """日志不存在（起不来就没写）⇒ 回执仍成立，只是没有 tail 段。"""
    r = _early_exit_receipt(0, "cmd", tmp_path / "nope.log")
    assert r.success is True
    assert "log tail" not in r.output


def test_health_loop_wired_to_receipt_source_guard():
    """接线守卫：health 循环必须真的走 `_early_exit_receipt`（回退成内联
    ``err("Dev server exited …")`` 的旧行为时本测试红 —— 纯单元测试锁不住
    调用点，源码断言补这个缺口）。

    ⚠ **本守卫原先用文本子串**（`assert "_early_exit_receipt(proc.returncode,
    cmd, log_path)" in src`）。2026-09-17 F3/M1 给该函数加了第 4 个参数
    （`stamp`），调用点变成 `_early_exit_receipt(proc.returncode, cmd,
    log_path, _stamp)` ⇒ 子串不再匹配 ⇒ **本文件转红**，而当时的定向回归
    用 `-k` 过滤把它漏在外面，差点以"全绿"交付。
    —— 这正是本仓纪律禁的形态：**文本判据随措辞失效**，且失效方向是
    「看起来更绿」或「误报红」，两种都不可信。

    改法（不是把子串再补一个变体）：用 **AST** 判「该函数确实被调用，
    且带够了参数」。这样下次改签名（加参数/换位置）不会假红，而
    「回退成内联文案」仍然会被抓到。
    """
    import ast

    from hiveweave.tools import dev_server_tools

    src = Path(dev_server_tools.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)

    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
        if name == "_early_exit_receipt":
            calls.append(node)

    # 只算**真实调用点**（排除定义处的签名本身 —— 定义不是 Call）
    assert calls, (
        "health 循环必须真的调用 `_early_exit_receipt` —— 回退成内联 "
        'err("Dev server exited …") 时本断言转红'
    )
    for c in calls:
        nargs = len(c.args) + len([k for k in c.keywords if k.arg])
        assert nargs >= 3, (
            f"调用点 {c.lineno} 只传了 {nargs} 个参数 —— 回执至少要 "
            "exit_code / cmd / log_path 才能按事实区分成败"
        )
    # 旧的内联失败文案（docstring 里的历史引用不算 —— 只查代码里的 f-string 形态）
    assert '"Dev server exited (code={proc.returncode})"' not in src
