"""保留端口被拦时的**工具级**回归（fixplan #15 审计发现的 P1 真崩溃）。

## 背景

L6（2026-09-11）把「保留端口」的事实位从 `runner_failed` **改判成 `bad_args`**
（对：模型换个 3000+ 端口即可通过，判 runner_failed 会让它收到「不是你的 bug」
并原地重撞同一端口）。但改判时**只换了 fact、没换构造器** —— 两处仍是

    ToolResult.blocked_err(reserved_err, fact="bad_args")

而 `blocked_err` 的 `__post_init__` 不变式明确拒绝这个组合：

    blocked=True cannot carry fact='bad_args'（bad_args 是**调用方**成因，
    标成平台护栏拒绝会给 agent 发「不是你的 bug」信号 —— L6/L19）

⇒ 任何 agent 跑 `npm run dev -- --port 4000` 都会撞 `ValueError`，看到的是
Python traceback 而不是端口指引；且 `executor._dispatch` 的 `except` 会早退成
**无 fact 的裸 dict**（漏斗旁路）⇒ 事实位兜底与归因样本都不触发。

## ⚠ 为什么既有测试没抓到

`test_fact_positions_coverage.py::test_real_reserved_port_messages_still_hit_bad_args`
只测 `check_command_reserved_ports` / `prepare_spawn_command` / `classify_error_text`
三个**纯函数**，**从不经过工具** ⇒ 它证明的是「判据函数说 bad_args」，
**不是**「生产上这条路径能跑通」。本文件补的就是那一步：经由 `bash_tool`。
（对应本仓纪律：**实现函数单测绿 ≠ 装配层活**。）

## 本文件钉三件事

1. 经工具调用**不抛**，且回执是 `bad_args` + **非** blocked；
2. 该回执过漏斗（`finalize_tool_result`）后事实位**不被改写**；
3. `blocked_err` 的不变式**仍然拒绝**调用方成因的格 —— 防止有人用
   「放宽 `_BLOCKED_FACT_KINDS`」来修 1 的崩溃（那是把 L6 的病放回来）。
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest


def _patch_project_id(monkeypatch) -> None:
    """工具会取 project_id；测试里给个常量，避免碰 meta DB。"""

    async def _pid(_agent_id: str) -> str:  # noqa: ANN001
        return "p1"

    monkeypatch.setattr("hiveweave.tools.helpers.get_project_id", _pid)


@pytest.mark.asyncio
async def test_reserved_port_through_bash_tool_is_bad_args_not_a_crash(monkeypatch):
    """`bash_tool` 撞保留端口 ⇒ 返回 bad_args 回执，**不抛 ValueError**。"""
    from hiveweave.tools.bash import BashParams, bash_tool

    _patch_project_id(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        ws = str(pathlib.Path(tmp) / "ws")
        pathlib.Path(ws).mkdir()
        result = await bash_tool(
            BashParams(command="npm run dev -- --port 4000"), "a1", ws
        )

    d = result.to_dict()
    assert d["success"] is False
    assert d.get("blocked") is not True, (
        "保留端口是**调用方参数错**（换个 3000+ 端口即可通过），不是平台护栏拒绝。"
        "标 blocked 会向 agent 发「不是你的 bug」信号并让它原地重撞（L6/L19）。"
    )
    assert d.get("fact") == "bad_args", (
        f"事实位应为 bad_args，实际 {d.get('fact')!r} —— "
        "若这里变成 runner_failed，说明又回到了 L6 之前的错标。"
    )
    assert "4000" in (d.get("error") or ""), (
        "回执必须给到端口指引（含具体端口号），而不是 Python 异常文本 —— "
        "agent 靠这句话才能改对参数。"
    )


@pytest.mark.asyncio
async def test_reserved_port_through_run_command_tool(monkeypatch):
    """另一个入口 `run_command_tool` 同款（两处构造点，别只修一处）。"""
    from hiveweave.tools.bash import RunCommandParams, run_command_tool

    _patch_project_id(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        ws = str(pathlib.Path(tmp) / "ws")
        pathlib.Path(ws).mkdir()
        result = await run_command_tool(
            RunCommandParams(command="npx vite --port 5173"), "a1", ws
        )

    d = result.to_dict()
    assert d["success"] is False
    assert d.get("blocked") is not True
    assert d.get("fact") == "bad_args"


@pytest.mark.asyncio
async def test_reserved_port_result_survives_the_funnel(monkeypatch):
    """过漏斗后事实位不被改写（否则 agent 收到的是另一个格的意思）。"""
    from hiveweave.tools.bash import BashParams, bash_tool
    from hiveweave.tools.fact_positions import finalize_tool_result

    _patch_project_id(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        ws = str(pathlib.Path(tmp) / "ws")
        pathlib.Path(ws).mkdir()
        result = await bash_tool(
            BashParams(command="npm run dev -- --port 4000"), "a1", ws
        )

    out = finalize_tool_result("bash", result)
    assert out.get("fact") == "bad_args", (
        f"漏斗改写了事实位 → {out.get('fact')!r}。bad_args 是调用方声明/判据给出的"
        "结论，漏斗只该在**缺位**时补，不该覆盖。"
    )
    assert out.get("blocked") is not True


@pytest.mark.asyncio
async def test_reserved_port_attribution_is_not_empty(monkeypatch):
    """归因一句话不许为空 —— 空串等于「撞了坑但没人告诉你方向」。"""
    from hiveweave.services.failure_signature import attribution_of
    from hiveweave.tools.bash import BashParams, bash_tool

    _patch_project_id(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        ws = str(pathlib.Path(tmp) / "ws")
        pathlib.Path(ws).mkdir()
        result = await bash_tool(
            BashParams(command="npm run dev -- --port 4000"), "a1", ws
        )

    attr = attribution_of(result.to_dict())
    assert attr, (
        "bad_args 的归因文案为空 —— L6 把它从 blocked 改判出来后没有对应文案，"
        "会让撞坑 Agent 拿不到方向（本仓纪律：平台报的错必须指对责任层）。"
    )
    assert "bad_args" in attr


def test_routed_background_result_never_builds_an_invalid_combination():
    """dev-server 路由侧同款：`blocked=True` + 调用方成因的 fact 必须被归一。

    这是 #17 的**同源形态** —— `_bash_background` 里原来的写法是
    `ToolResult.blocked_err(err_msg, fact=routed.get("fact"))`，而它上方注释
    自己写着「保留端口=bad_args」⇒ 一旦路由侧真的产出该组合就撞同一条不变式崩。

    ⚠ 归一方向必须是「**保留 fact、去掉 blocked**」：
      · 保留端口是**调用方参数错**，标 blocked 会给 agent 发「不是你的 bug」
        信号并让它原地重撞（L6/L19）；
      · 但也**不能**悄悄改成 `runner_failed` —— 那是把 L6 撤销。
    """
    from hiveweave.tools.bash import _wrap_routed_background_result

    # ① 原来会崩的组合：保留 fact、去掉 blocked
    r = _wrap_routed_background_result(
        {"success": False, "error": "reserved port 4000", "fact": "bad_args", "blocked": True},
        "a1",
    )
    d = r.to_dict()
    assert d["fact"] == "bad_args", "必须保留路由侧给的权威事实位"
    assert d.get("blocked") is not True, "调用方成因不得被标成平台护栏拒绝（L6/L19）"
    assert d["success"] is False

    # ② 合法组合原样透传（blocked 是平台侧成因）
    r2 = _wrap_routed_background_result(
        {"success": False, "error": "spawn failed", "fact": "runner_failed", "blocked": True},
        "a1",
    )
    assert r2.to_dict().get("blocked") is True
    assert r2.to_dict()["fact"] == "runner_failed"

    # ③ blocked 但没给 fact ⇒ 默认 runner_failed（平台护栏出口的既有语义）
    r3 = _wrap_routed_background_result(
        {"success": False, "error": "blocked", "blocked": True}, "a1"
    )
    assert r3.to_dict()["fact"] == "runner_failed"

    # ④ 非 blocked 的失败与成功路径不变
    r4 = _wrap_routed_background_result(
        {"success": False, "error": "x", "fact": "command_failed"}, "a1"
    )
    assert r4.to_dict()["fact"] == "command_failed"
    assert r4.to_dict().get("blocked") is not True
    r5 = _wrap_routed_background_result({"success": True, "output": "ok"}, "a1")
    assert r5.to_dict()["success"] is True


def test_shell_tool_result_converges_the_illegal_combination():
    """`_shell_tool_result` 对同一非法组合的处置已**收敛**（不再静默反向）。

    这是本条修完 #17 后由复审查出的**第三处**处置 —— 三处曾经各不相同：
      · `_wrap_routed_background_result` ⇒ 保留 fact、去掉 blocked（对）；
      · `finalize_tool_result`            ⇒ 丢弃调用方成因的位、继续阶梯（可接受）；
      · `_shell_tool_result`              ⇒ **保留 blocked、把 fact 静默改写成
        runner_failed** —— 方向**相反**（把「你的参数错」说成「命令从未执行」，
        正是 L6/L19 要治的病）且**无任何日志**。

    现在三处统一为「保留 fact、去掉 blocked」。⚠ 关键：**护栏出口确实没声明格
    （`fact is None`）时仍回落 runner_failed** —— 那是既有语义、有测试钉住，
    不能一起改掉（改了会让护栏出口变成 bad_args-free 的裸 err）。
    """
    from hiveweave.tools.bash import _shell_tool_result

    def _call(fact: str | None, *, blocked: bool = True):
        return _shell_tool_result(
            success=False,
            blocked=blocked,
            output="",
            error="boom",
            banner="",
            suffix="",
            public={} if fact is None else {"fact": fact},
        )

    # ① 非法组合：保留 fact、去掉 blocked（不再改成 runner_failed）
    d = _call("bad_args").to_dict()
    assert d["fact"] == "bad_args", "不得把调用方成因改写成 runner_failed（方向相反）"
    assert d.get("blocked") is not True

    # ② 护栏出口没声明格 ⇒ 既有回落语义**保持不变**（有测试钉住）
    d2 = _call(None).to_dict()
    assert d2["fact"] == "runner_failed"
    assert d2.get("blocked") is True

    # ③ 平台侧成因原样透传
    d3 = _call("outcome_unknown").to_dict()
    assert d3["fact"] == "outcome_unknown"
    assert d3.get("blocked") is True

    # ④ 非 blocked 路径不受影响
    d4 = _call("command_failed", blocked=False).to_dict()
    assert d4["fact"] == "command_failed"
    assert d4.get("blocked") is not True


def test_blocked_err_still_rejects_caller_fault_facts():
    """钉住不变式本身：**不许**用「放宽 `_BLOCKED_FACT_KINDS`」修上面的崩溃。

    `_BLOCKED_FACT_KINDS` 只收平台侧成因（runner_failed / outcome_unknown）。
    把 `bad_args` / `command_failed` 放进去 = 让 agent 收到「不是你的 bug」
    并对同一组参数原地重撞 —— 正是 L6/L19 要治的病。

    正确修法是**换构造器**（`blocked_err` → `err`），因为「保留端口」是
    调用方参数错，本来就该是 `blocked=False`。若你认为该改不变式，
    请先在 fixqueue 上开条目并说明为什么 L6 的判断不再成立。
    """
    from hiveweave.tools.result import ToolResult

    with pytest.raises(ValueError, match="cannot carry fact"):
        ToolResult.blocked_err("reserved port 4000", fact="bad_args")
    with pytest.raises(ValueError, match="cannot carry fact"):
        ToolResult.blocked_err("your test failed", fact="command_failed")
