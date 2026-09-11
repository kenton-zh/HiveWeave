"""F4 / F7 / R11 接线回归（TEST_DSH_50 + TEST_DSH_51 审计轮，2026-09-10）。

背景：08-30 的 F1–F14 批次把机制层都建好了，但**触发路径**只接了一部分。
实测（`q_verify_reform2.py`，机制命中率而非存在率）：

- R11 记账：终止 run 子集 50 = 2/9、51 = 0/7 —— 全库口径 96.8% / 96.3% 掩盖了它
- F4 `runner_failed`：50 = 5/13（38.5%）、51 = 2/10（20%）—— 方言/审批接了，
  护栏拒绝与 cwd 不存在没接
- F7 `timeout_kind`：50 = 1/2、51 = **0/3** —— 只接了「工具自身声明的超时」，
  整轮兜底出口完全没接

本文件的每条断言都对准一个**具体漏网路径**，而不是「机制存在」。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.llm.streamer.core import Streamer
from hiveweave.tools.bash import execute_run_command


@pytest.fixture(autouse=True)
def _sandbox_off(monkeypatch):
    """与 test_bash_self_destruct 同口径：本文件测守卫/事实位，关沙箱。"""
    from hiveweave.config import settings

    monkeypatch.setattr(settings, "acl_sandbox", False)


# ─────────────────────────────────────────────────────────────
# R11：终止 run 的 usage 不能在被 flush 之前销毁
# ─────────────────────────────────────────────────────────────


class TestPendingUsageClearGate:
    """`agent._pending_usage` 清空 —— 终止 run 零账的根因（L4 已根治）。

    旧链路：streamer 每轮把 usage 推进 sink → 硬超时走 `_error_result`
    （`usage_rounds` 硬编码为空）→ `record_rounds([])` 静默 return →
    旧代码**无条件** `clear()` 把 sink 销毁 → `_flush_pending_usage`
    拿空 → 该 run 零账。

    旧修法是加门控 `_usage_rounds_delivered(result)` 补偿分叉。
    **L4（2026-09-11）的修法是删掉分叉本身**：result 不再携带 `usage_rounds`，
    sink 成为唯一权威源，门控连同 `_error_result` 的空键一起退役。
    本类改为钉住「分叉已消失」这一事实（原三条门控断言全部作废）。
    """

    def test_error_result_no_longer_declares_usage(self):
        """L4 的有意行为变更：`_error_result` **不再**携带 `usage_rounds` 键。

        原断言（`err["usage_rounds"] == []`）钉的是「越界断言的现状」——
        即 error 路径自称"没有账"，而轮次其实就在 sink 里。该键正是分叉的
        载体，现已删除。留此断言防止有人"顺手加回去"。
        """
        err = Streamer._error_result("请求总超时", time.monotonic())
        assert "usage_rounds" not in err, (
            "L4：error 结果不得再声明 usage —— 记账权威源只有调用方的 sink"
        )

    def test_delivered_gate_is_gone(self):
        """门控函数与它的补偿对象一起退役（无分叉则无需门控）。"""
        import hiveweave.agents.agent as agent_mod

        assert not hasattr(agent_mod, "_usage_rounds_delivered"), (
            "L4：门控是补偿分叉的贴丁，分叉删掉后它必须一起走"
        )

    def test_agent_records_from_sink_not_result(self):
        """结构性绊线：主循环的记账取 sink 快照，不再读 result 的键。

        用 AST 而非文本断言 —— 注释里也提到了 `usage_rounds`，文本匹配会
        把注释当命中（假绿/假红都可能）。
        """
        import ast
        import inspect

        import hiveweave.agents.agent as agent_mod

        src = inspect.getsource(agent_mod)
        tree = ast.parse(src)
        # 全文件不允许再出现对 result 的 usage_rounds 取值
        hits = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Subscript)
            and isinstance(n.slice, ast.Constant)
            and n.slice.value == "usage_rounds"
        ]
        assert not hits, f"仍有 {len(hits)} 处从 dict 取 usage_rounds（应改读 sink）"
        # 且必须真的从 sink 取快照
        assert "_rounds_this_attempt = list(self._pending_usage)" in src, (
            "记账必须取 sink 快照（sink = 唯一权威源）"
        )


class TestLoopExitUsageHandoff:
    """循环出口的 usage 交接 —— 交付后审计发现的残留（漏账，非双计）。

    修好「error 路径不再销毁 sink」之后，仍有三条出口没交接：
    `status="error"` 的上游重试 `continue`、`status="empty"` 的重试
    `continue` 与放弃 `break`。这三条上推进 sink 的轮次既没落库、
    也不该被静默丢弃（那些调用确实发生过、也确实计费）。
    """

    @pytest.mark.asyncio
    async def test_flush_called_with_reason_and_clears_sink(self, monkeypatch):
        from hiveweave.agents import agent as agent_mod
        from hiveweave.agents import recovery as recovery_mod

        seen: list[str] = []

        async def _fake_flush(agent, *, reason="interrupted"):
            seen.append(reason)
            # 契约：flush 落库后重建 sink（保证不跨 attempt 残留）。
            agent._pending_usage = []

        monkeypatch.setattr(recovery_mod, "_flush_pending_usage", _fake_flush)

        class _FakeAgent:
            id = "a1"

            def __init__(self):
                self._pending_usage = [{"input": 10}, {"input": 20}]

        a = _FakeAgent()
        await agent_mod._flush_usage_at_loop_exit(a, reason="upstream_retry")
        assert seen == ["upstream_retry"]
        assert a._pending_usage == [], "flush 后 sink 必须清空（否则下一 attempt 会叠加）"

    @pytest.mark.asyncio
    async def test_flush_failure_does_not_break_control_flow(self, monkeypatch):
        """best-effort：记账失败绝不能打断 turn 控制流。"""
        from hiveweave.agents import agent as agent_mod
        from hiveweave.agents import recovery as recovery_mod

        async def _boom(*_a, **_k):
            raise RuntimeError("db down")

        monkeypatch.setattr(recovery_mod, "_flush_pending_usage", _boom)

        class _FakeAgent:
            id = "a1"

        with patch.object(agent_mod, "log") as fake_log:
            await agent_mod._flush_usage_at_loop_exit(
                _FakeAgent(), reason="empty_giveup"
            )
        assert fake_log.warning.called

    def test_three_retry_exits_are_guarded(self):
        """结构性门禁：三条出口（上游重试 / empty 重试 / empty 放弃）各有一处交接。

        用 `ast` 数调用点而不是正则 —— 若有人删掉某个出口的守卫，这条会红。
        若**有意**重构为单一出口，请同步修改本断言（它是刻意的绊线，不是巧合）。
        """
        import ast
        import inspect

        from hiveweave.agents import agent as agent_mod

        tree = ast.parse(inspect.getsource(agent_mod))
        calls = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "_flush_usage_at_loop_exit"
        ]
        # 1 处是函数定义体内部不计数（这里是调用），期望恰好 3 处调用点
        assert len(calls) == 3, (
            f"循环出口的 usage 交接应有 3 处（upstream_retry / empty_retry / "
            f"empty_giveup），实测 {len(calls)} 处"
        )
        reasons = sorted(
            kw.value.value for c in calls for kw in c.keywords
            if kw.arg == "reason" and isinstance(kw.value, ast.Constant)
        )
        assert reasons == ["empty_giveup", "empty_retry", "upstream_retry"], reasons


# ─────────────────────────────────────────────────────────────
# F4：shell 类「命令从未执行」的拒绝要带 runner_failed
# ─────────────────────────────────────────────────────────────


class TestRunnerFailedWiring:
    """`schema.py` 里 F4 的定义域明写 runner_failed 管「命令未执行（参数注入
    破坏 / 方言不支持 / **权限** / 审批 / runner 自身故障）」——cwd 不存在、
    沙箱拒绝都属此列，此前只置了 `blocked`。
    """

    def setup_method(self):
        self.workspace = tempfile.mkdtemp(prefix="hw_f4_")

    def teardown_method(self):
        shutil.rmtree(self.workspace, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_missing_cwd_sets_runner_failed(self):
        """`Working directory does not exist` —— 50/51 各撞 2~3 次的签名。"""
        missing = os.path.join(self.workspace, "no-such-dir", "nested")
        result = await execute_run_command(
            command="echo hi",
            cwd=missing,
            timeout_ms=5000,
            workspace_path=self.workspace,
        )
        assert result["success"] is False
        assert result["blocked"] is True
        assert "Working directory does not exist" in result["error"]
        assert result["runner_failed"] is True, (
            "cwd 不存在 = 命令从未执行，必须置 runner_failed（F4 定义域含权限/未执行）"
        )
        # 命令没跑，不该被记成 command_failed
        assert not result.get("command_failed")

    @pytest.mark.asyncio
    async def test_self_destruct_blocked_still_carries_runner_failed(self):
        """既有正确路径的字符化断言（防止被顺手改掉）。"""
        result = await execute_run_command(
            command="rm -rf /",
            cwd="",
            timeout_ms=5000,
            workspace_path=self.workspace,
        )
        assert result["blocked"] is True
        assert result["runner_failed"] is True


class _AllowAllPermission:
    """最小许可对象：只实现 pipeline 会走的那条分支。

    刻意**不**提供 evaluate（只留 evaluate_detailed），因为 pipeline
    用 hasattr(permission, "evaluate_detailed") 选路 —— MagicMock 两者都有，
    会走错分支并因返回值不是二元组而解包失败。
    """

    async def evaluate_detailed(self, *_a, **_k):
        return ("allow", None)


class TestProjectRootRefusalAttribution:
    """L19（2026-09-11，TEST_DSH_52_B 实测）：project-root 写入拒绝**是模型错误**，
    不得置 `blocked` / `runner_failed`。

    两者的语义都是「不是你的问题」——`ToolResult` docstring 明写 blocked =
    「the platform refused to execute — **not a model mistake**」；runner_failed →
    归因文案「命令未执行（执行器/方言/权限/审批）」。实测后果：agent 写错树后收到
    「不是你的 bug」的信号 → 同一 run 连撞 2 次（青崖 11:20:39），还被 R7 记一笔。
    """

    @pytest.mark.asyncio
    async def test_refusal_carries_no_platform_side_fact_bits(self, monkeypatch):
        from hiveweave.tools import pipeline

        monkeypatch.setattr(
            pipeline,
            "_refuse_project_root_write",
            AsyncMock(return_value="Refusing apply_patch on project root …"),
        )
        result = await pipeline.execute_registered_tool(
            tool_name="apply_patch",
            raw_args={"patches": [{"filePath": "a.txt", "op": "add", "content": "x"}]},
            agent_id="a1",
            workspace_path=tempfile.mkdtemp(prefix="hw_root_"),
            permission=_AllowAllPermission(),
            approval=AsyncMock(),
            ctx=None,
        )
        assert result is not None and result["success"] is False
        # ⚠️ 必须先证明**确实走到了这个分支**：只断言 blocked/runner_failed 为空，
        # 会在「参数校验失败 → 提前 return 一个无事实位的错误」时**假绿**
        # （本用例第一版就是这样：摘掉 pipeline.py 后仍通过）。
        assert "Refusing apply_patch on project root" in (result.get("error") or ""), (
            f"没走到 project-root 拒分支（实际错误：{result.get('error')!r}）"
        )
        assert not result.get("blocked"), (
            "写错树是模型错误，不能标 blocked（blocked 的定义是「不是模型错误」）"
        )
        assert not result.get("runner_failed"), (
            "runner 侧无责；标了会给模型「不是你的 bug」的错误归因"
        )


class TestPipelineShellChokePoint:
    """`tools/pipeline.py` 的 shell 预检收口 —— 「命令从未执行」的统一出口。

    交付后审计指出：#4 的收口此前没有任何测试覆盖，而它恰恰是
    `Command blocked - cannot access .hiveweave system directory`
    （50 ×3 / 51 ×2）这类签名的唯一出口。
    """

    def setup_method(self):
        self.workspace = tempfile.mkdtemp(prefix="hw_pipe_")

    def teardown_method(self):
        shutil.rmtree(self.workspace, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_hiveweave_path_guard_carries_runner_failed_through_toolresult(self):
        """**穿过 ToolResult** 的集成断言（不是只断言实现函数的返回 dict）。

        第 8 轮沉淀的坑：`_ff` 白名单 + blocked 分支曾把字段整个丢掉，
        只断言实现函数抓不到这一层。故这里走 `execute_registered_tool`，
        拿到的就是 `.to_dict()` 的产物。
        """
        from hiveweave.tools import pipeline

        result = await pipeline.execute_registered_tool(
            tool_name="pwsh",
            # 这条命中的正是实测签名本身：50 ×3 / 51 ×2 的
            # `Error: Command blocked - cannot access .hiveweave system directory`
            raw_args={"command": "Remove-Item .hiveweave/data.db"},
            agent_id="a1",
            workspace_path=self.workspace,
            # permission/approval 是 await 调用点。用最小 fake 而不是
            # MagicMock：AsyncMock 会自动长出 evaluate_detailed 属性，
            # hasattr 判真后返回非 2 元组 → 解包失败（实测）。
            permission=_AllowAllPermission(),
            approval=AsyncMock(),
            ctx=None,
        )
        assert result is not None, "pwsh 应在注册表里（否则本测试失去意义）"
        assert result["success"] is False
        assert result["blocked"] is True
        assert result["runner_failed"] is True, (
            "shell 预检拒绝 = 命令从未执行，必须经 ToolResult 透传出 runner_failed"
        )
        assert result.get("timeout_kind") is None, "拒绝不是超时，不该带超时分类"


class TestTokenMeterEmptyRounds:
    """`record_rounds([])` 不再静默 —— 零账必须留痕，否则无从归因。"""

    @pytest.mark.asyncio
    async def test_empty_rounds_logs_instead_of_silent_return(self):
        from hiveweave.services import token_meter as tm

        with patch.object(tm, "log") as fake_log:
            await tm.token_meter.record_rounds(
                agent_id="a1",
                project_id="p1",
                rounds=[],
                run_id="r1",
                request_type="main",
            )
        assert fake_log.info.called, "空批次必须留痕（原实现直接 return，观测管道无迹可寻）"
        assert "record_rounds_empty" in str(fake_log.info.call_args)


# ─────────────────────────────────────────────────────────────
# F7：整轮兜底超时也要有可机检分类
# ─────────────────────────────────────────────────────────────


class TestTurnTimeoutKind:
    """`stream_hard_timeout` 分支（外层 asyncio.wait_for）此前只返回裸 error
    result，run_steps.timeout_kind 恒 NULL —— 「超时不可分类」在此残留。
    """

    @pytest.mark.asyncio
    async def test_hard_timeout_branch_tags_result(self, monkeypatch):
        import hiveweave.llm.streamer.core as core

        async def _boom(*_a, **_k):
            raise TimeoutError

        class _FakeProvider:
            provider_type = "fake"
            model_name = "fake-model"

        monkeypatch.setattr(core.Streamer, "_run_tool_loop", _boom)
        st = Streamer(max_tool_rounds=1)
        # 绕过真实的 provider 构造（它会对 model_config 做物理不变量校验）；
        # 本测试只关心超时分支返回的 result 形状。
        st._provider_factory = MagicMock()
        st._provider_factory.create.return_value = _FakeProvider()

        deltas: list[dict] = []

        async def _on_delta(d):
            deltas.append(d)

        result = await st.stream(
            agent_id="a1",
            messages=[{"role": "user", "content": "hi"}],
            model_config={"name": "primary", "model_id": "m"},
            tools=[],
            on_delta=_on_delta,
            on_tool_call=None,
        )

        assert result["status"] == "error"
        assert result["error"] == "请求总超时"
        assert result["timeout_kind"] == "turn", (
            "整轮兜底超时必须有可机检分类（区别于工具自身超时的 'command'）"
        )
        assert result["timeout_ms"] and result["timeout_ms"] > 0
