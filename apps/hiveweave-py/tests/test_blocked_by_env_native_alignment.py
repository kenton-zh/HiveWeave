"""批23（N2）：blocked_by_environment 三工具同源同判（native 路由补判）。

G10 实锤（census s3-clone_21 #3/#4）：同一故障键「Command exited with
code N [stderr tail] Traceback…」在 pwsh（blocked_by_environment=1）与
run_command/pwsh_main（=None）上事实位不同。

代码面根因：环境拒绝位只有一个生产点
（``acl_sandbox.service._maybe_append_rejection_hint``），只挂在
**confined** 路由 ⇒ 位的有无取决于当时走哪条路由，而不是取决于故障本身。

修法（最小切片）：在两条路由的汇合点（``tools/bash.py::_run_sandboxed``）
用**同一个判定函数**（``fact_positions.classify_denied_by``）对缺位的
native 结果补跑同一判定；confined 已显式盖戳的**绝不二次裁判**。

三工具入口映射：bash / pwsh / bash_main / pwsh_main 走 entry="bash"，
run_command 走 entry="run_command" —— 本测试对两个 entry 喂**同一份故障
证据**，断言 blocked 位一致（pwsh_main 与 pwsh 共用 entry="bash" 的同一
实现，不重复展开）。

剩余分裂面（如实记录，不在本切片内）：① 故障签名归一化只保留前 140 字符
（norm_sig[:140]），同键下不同 stderr 尾部本就可能判出不同 denied_by；
② pwsh 与 run_command 的受限 runner 方言不同（pwsh vs bash），拒绝方言的
命中率天然有差；③ native 路由拿不到封条上下文，``sealed_git`` 细分不可判。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hiveweave.tools.bash import _run_sandboxed


def _routed(result: dict) -> SimpleNamespace:
    return SimpleNamespace(result=result)


def _native_result(stderr: str, exit_code: int | None) -> dict:
    return {
        "output": "",
        "stdout": "",
        "stderr": stderr,
        "exit_code": exit_code,
        "timed_out": False,
        "enforcement": "native",
    }


async def _patched_spawn(result: dict, monkeypatch) -> dict:
    async def fake_spawn_agent_command(**kwargs):
        return _routed(result)

    monkeypatch.setattr(
        "hiveweave.services.acl_sandbox.entry.spawn_agent_command",
        fake_spawn_agent_command,
    )
    return result


async def _run(entry: str, result: dict, monkeypatch, tmp_path) -> dict:
    await _patched_spawn(result, monkeypatch)
    return await _run_sandboxed(
        "python script.py",
        str(tmp_path),
        10,
        workspace_path=str(tmp_path),
        agent_id="a1",
        project_id=None,
        entry=entry,
    )


# ── 同判：同一故障证据，两个 entry（→ 三工具）blocked 位一致 ────────


@pytest.mark.parametrize(
    "stderr", [
        "Traceback (most recent call last):\n"
        "PermissionError: [Errno 13] Permission denied",
        "Access to the path 'D:\\elsewhere\\f' is denied",
    ],
)
@pytest.mark.asyncio
async def test_same_denial_evidence_same_bit_across_entries(
    monkeypatch, tmp_path, stderr
):
    """同故障签名三路径 blocked 位一致（G10/N2 的机检判据）。"""
    bits = []
    for entry in ("bash", "run_command"):
        shaped = await _run(
            entry, _native_result(stderr, 1), monkeypatch, tmp_path
        )
        bits.append(
            (
                shaped.get("denied_by"),
                shaped.get("blocked_by_environment"),
            )
        )
    assert bits[0] == bits[1], (
        f"同一环境拒绝在两条 entry 上事实位不同 ⇒ 归因分裂（N2 回归）：{bits}"
    )
    denied_by, blocked = bits[0]
    assert denied_by is not None, "拒绝方言命中 ⇒ denied_by 必须落格"
    assert blocked is True, "exit_code 非 None（进程确实跑过）⇒ blocked 位=1"


# ── native 路由补判的行为面 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_native_denial_gets_bits(monkeypatch, tmp_path):
    """native 路由的环境拒绝此前恒 NULL —— 现在按同一判定函数补位。"""
    shaped = await _run(
        "bash",
        _native_result(
            "Traceback (most recent call last):\n"
            "PermissionError: [Errno 13] Permission denied",
            1,
        ),
        monkeypatch,
        tmp_path,
    )
    assert shaped.get("denied_by") is not None
    assert shaped.get("blocked_by_environment") is True


@pytest.mark.asyncio
async def test_native_business_failure_stays_unstamped(monkeypatch, tmp_path):
    """业务失败（无拒绝方言）⇒ 不贴位（缺席 = 未观测，不许硬塞）。"""
    shaped = await _run(
        "bash",
        _native_result("Traceback ...\nValueError: bad input", 1),
        monkeypatch,
        tmp_path,
    )
    assert shaped.get("denied_by") is None
    assert shaped.get("blocked_by_environment") is None


@pytest.mark.asyncio
async def test_native_no_exit_code_matches_confined_state_gate(
    monkeypatch, tmp_path,
):
    """exit_code=None（进程没跑过）⇒ 状态门不认拒绝（同判：confined 侧的
    ``is_acl_rejection`` 同一前提 —— 非零退出才算拒绝证据）。"""
    shaped = await _run(
        "bash",
        _native_result("terminal failure: Access is denied", None),
        monkeypatch,
        tmp_path,
    )
    assert shaped.get("denied_by") is None
    assert shaped.get("blocked_by_environment") is None


@pytest.mark.asyncio
async def test_confined_explicit_bits_never_rejudged(monkeypatch, tmp_path):
    """confined 路由已显式盖戳 ⇒ 显式位优先，汇合点绝不二次裁判。"""
    shaped = await _run(
        "bash",
        {
            **_native_result(
                "Access to the path 'D:\\x' is denied", 1
            ),
            "denied_by": "no_write_sid",
            "blocked_by_environment": True,
        },
        monkeypatch,
        tmp_path,
    )
    assert shaped.get("denied_by") == "no_write_sid"
    assert shaped.get("blocked_by_environment") is True


@pytest.mark.asyncio
async def test_no_denial_leaves_no_hint_text(monkeypatch, tmp_path):
    """native 补判只盖事实位、不贴沙箱提示文案（native 下那是假话）。"""
    shaped = await _run(
        "bash",
        _native_result(
            "Traceback ...\nPermissionError: [Errno 13] Permission denied", 1
        ),
        monkeypatch,
        tmp_path,
    )
    assert "沙箱提示" not in str(shaped.get("stderr") or "")
    assert "沙箱提示" not in str(shaped.get("output") or "")
