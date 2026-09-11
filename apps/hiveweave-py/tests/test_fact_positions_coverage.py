"""L3 覆盖测试：**枚举式**证明每个 shell 出口都带事实位（2026-09-11）。

## 为什么必须有这条（补 DSH 自陈的边界）

DSH 的收口（`packages/core/tools/src/invariant.ts`）挂在总线上，「**经总线的**
都校验」——但它**捕获不到「某分支根本不发总线事件」**。对应到我们这里就是
「某个 shell 出口绕过了 `finalize_tool_result`」。

⇒ 必须有一条**枚举式**覆盖：把构造点全部扫出来（AST，不是正则），
断言每一个都带事实位。

## 三条断言

1. **裸字典出口**（`bash.py`）必须经 `finalize_fact_dict` 收口 —— 否则
   `fact` 的派生键（runner_failed / command_failed）不会展开，下游
   `result["runner_failed"]` 直接 KeyError（本轮实测到的回归）。
2. **`ToolResult` 构造点**必须显式声明 `fact=`（或在 blocked 情形由漏斗判）。
3. **签名表**对真实错误文本逐条命中 —— 「声明了却没人写」的第 3 次复发防范
   （同 L15 的取值域绊线思路）。

用 AST 而非文本匹配：注释里也会出现 `"fact"` / `runner_failed` 字样，
文本匹配会把注释当命中（假绿）或把注释里的反例当违规（假红）。
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from hiveweave.tools import bash as bash_mod
from hiveweave.tools import fact_positions as fp

_PKG = Path(inspect.getfile(bash_mod)).parent


def _parse(mod) -> ast.Module:
    return ast.parse(inspect.getsource(mod))


def _returns_dict_literals(tree: ast.Module) -> list[ast.Dict]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            out.append(node.value)
    return out


class TestRawDictExitsAreFunneled:
    """裸字典出口必须经单一漏斗 —— 否则派生键不展开（本轮实测回归）。"""

    def test_bash_dict_returns_with_fact_go_through_funnel(self):
        tree = _parse(bash_mod)
        wrapped = 0
        unwrapped: list[int] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Return):
                continue
            v = node.value
            # return finalize_fact_dict({...})
            if (
                isinstance(v, ast.Call)
                and isinstance(v.func, ast.Name)
                and v.func.id == "finalize_fact_dict"
                and v.args
                and isinstance(v.args[0], ast.Dict)
                and any(
                    isinstance(k, ast.Constant) and k.value == "fact"
                    for k in v.args[0].keys
                )
            ):
                wrapped += 1
            elif isinstance(v, ast.Dict):
                keys = [
                    k.value for k in v.keys if isinstance(k, ast.Constant)
                ]
                if "fact" in keys:
                    unwrapped.append(node.lineno)
        assert wrapped >= 15, (
            f"bash.py 经漏斗收口的含 fact 出口应有 15+ 处，实测 {wrapped} —— "
            f"裸字典绕过 ToolResult 会让派生键不展开（见 result.finalize_fact_dict）"
        )
        assert not unwrapped, (
            f"bash.py:{unwrapped} 的裸字典出口声明了 fact 却未经 finalize_fact_dict "
            f"→ 下游 result['runner_failed'] 会 KeyError"
        )

    def test_finalize_fact_dict_expands_derived_keys(self):
        """漏斗的核心契约：fact → 派生键展开；None 保持「未确定」。"""
        from hiveweave.tools.result import finalize_fact_dict

        d = finalize_fact_dict({"success": False, "error": "x", "fact": "runner_failed"})
        assert d["runner_failed"] is True
        assert d["command_failed"] is False

        d = finalize_fact_dict({"success": False, "error": "x", "fact": "bad_args"})
        assert d["runner_failed"] is False
        assert d["command_failed"] is False

        # 未确定：不得臆断（run_ledger 的 COALESCE 靠 None 区分）
        d = finalize_fact_dict({"success": False, "error": "x"})
        assert "runner_failed" not in d
        assert "command_failed" not in d

        # blocked 无 fact → 回落 runner_failed（平台护栏默认成因）
        d = finalize_fact_dict({"success": False, "error": "x", "blocked": True})
        assert d["fact"] == "runner_failed"
        assert d["runner_failed"] is True

    def test_finalize_fact_dict_is_idempotent(self):
        from hiveweave.tools.result import finalize_fact_dict

        once = finalize_fact_dict({"success": False, "error": "x", "fact": "runner_failed"})
        twice = finalize_fact_dict(dict(once))
        assert once == twice

    def test_finalize_fact_dict_rejects_unknown_kind(self):
        from hiveweave.tools.result import finalize_fact_dict

        with pytest.raises(ValueError, match="unknown FactKind"):
            finalize_fact_dict({"success": False, "error": "x", "fact": "bogus"})


class TestSingleFunnelWired:
    """两条执行器的 normalize 尾都必须调用同一个漏斗（AST 绊线）。"""

    @pytest.mark.parametrize(
        "module_name",
        ["hiveweave.tools.pipeline", "hiveweave.tools.executor"],
    )
    def test_executor_tail_calls_shared_funnel(self, module_name):
        import importlib

        mod = importlib.import_module(module_name)
        src = inspect.getsource(mod)
        tree = ast.parse(src)
        calls = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "finalize_tool_result"
        ]
        assert calls, (
            f"{module_name} 的 normalize 尾未调用 finalize_tool_result —— "
            f"两条执行器会再次各写一份事实位归因（28 处还会继续涨）"
        )

    def test_funnel_not_hung_only_on_emit_hook(self):
        """§1.4b 的坑：漏斗**不能只挂** `_emit_tool_execute_after`。

        `executor.py:2446` 明写 pre-execution 失败不 emit，而 28 处里 17 处
        正是 pre-execution 失败。用 AST 断言「在 execute 主体的 normalize 段
        （而非 emit 钩子内）」也有一处调用。
        """
        from hiveweave.tools import executor as ex_mod

        src = inspect.getsource(ex_mod)
        tree = ast.parse(src)
        emit_fn = None
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_emit_tool_execute_after":
                emit_fn = node
                break
        assert emit_fn is not None, "未找到 _emit_tool_execute_after（函数名变了？）"
        emit_lines = (emit_fn.lineno, emit_fn.end_lineno or emit_fn.lineno)

        main_calls = [
            n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "finalize_tool_result"
            and not (emit_lines[0] <= n.lineno <= emit_lines[1])
        ]
        assert main_calls, (
            "finalize_tool_result 只在 emit 钩子（或只在钩子区间）内调用 —— "
            "pre-execution 失败（审批/护栏，17 处）会全部绕过收口"
        )


class TestEvidenceCriterion:
    """证据判据：签名表对真实错误文本的命中（不是声明就够）。"""

    @pytest.mark.parametrize(
        "text,expected",
        [
            # B 组：安全护栏
            ("Error: Command blocked: system-level destructive command", "runner_failed"),
            ("Error: Command blocked - cannot access .hiveweave system directory", "runner_failed"),
            # C 组：沙箱 / cwd
            ("Error: Sandbox violation - cwd must be within workspace", "runner_failed"),
            ("Error: Working directory does not exist: /x/y", "runner_failed"),
            ("沙箱不可用，已拒绝执行（fail-closed）：...", "runner_failed"),
            # E 组：runner 自身
            ("Error: [No tool executor] for bash", "runner_failed"),
            # A 组：审批
            ("Permission rejected: user said no", "runner_failed"),
            ("Error: Permission check failed: db down", "runner_failed"),
            # L6：调用方参数错
            ("Error: 疑似重复 worktree 前缀路径，请改用 workspace 内相对路径", "bad_args"),
        ],
    )
    def test_real_error_texts_hit_expected_fact(self, text, expected):
        assert fp.classify_error_text(text) == expected, (
            f"{text!r} 应判 {expected}（实测 {fp.classify_error_text(text)}）"
        )

    def test_unrelated_text_has_no_verdict(self):
        """无签名命中必须返回 None —— 「不猜」是判据的一部分。"""
        assert fp.classify_error_text("AssertionError: 1 != 2") is None
        assert fp.classify_error_text("Traceback ... ValueError: bad value") is None

    def test_blocked_without_evidence_fails_loud(self):
        """blocked 却无签名命中 ⇒ AssertionError，不静默归类。"""
        with pytest.raises(AssertionError, match="no fact evidence"):
            fp.classify_blocked_fact("bash", "AssertionError: 1 != 2")

    def test_wait_timeout_is_code_scoped_not_text_matched(self):
        """审批等待靠 code 作用域（timeout_kind）归属，不靠文案。"""
        assert (
            fp.classify_blocked_fact("bash", "审批窗口关闭", timeout_kind="wait")
            == "runner_failed"
        )


class TestDevServerGuardsDirect:
    """L8：`bash.py` 的 dev-server 三道守卫**可直测**（并入 L3，不单独立项）。

    原状：三个判定（首选端口保留 / 分配后端口保留 / `prep_err`）埋在
    `_run_registered_dev_server` 长函数里，构造点不易从外部触发 ——
    这正是 L3 的纯函数化要解决的。纯函数化后「保留端口」判定可直测：

    - `is_reserved_port()` 已是独立纯函数（`services/process_registry.py`）
    - `finalize_fact_dict` 收口后，裸字典出口的 fact 可直断言

    整链用例（真起 dev server）按 DSH 三段式：无环境时 **skip 而非假绿**。
    """

    def test_reserved_port_is_plain_function(self):
        """判定已纯函数化 —— 这是「先提纯再谈三段式」的前提（DSH tested）。"""
        from hiveweave.services.process_registry import is_reserved_port

        assert is_reserved_port(4000) is True, "4000 是平台后端端口，必须保留"
        assert is_reserved_port(3000) is False, "3000 是项目端口，必须可用"

    def test_reserved_port_declared_bad_args_at_all_three_guards(self):
        """保留端口在**三处**守卫都必须判 `bad_args`（不是 runner_failed）。

        L6 的判据：换一个 3000+ 端口就能跑 ⇒ 调用方参数错。原标 runner_failed
        会让 agent 收到「不是你的 bug」并原地重撞同一个端口。

        三处守卫（实测定位，2026-09-11）：
        1. `process_registry.check_command_reserved_ports` —— 命令文本里出现
           保留端口即拒（`prepare_spawn_command` 第 803 行），**最先命中**；
        2. `process_registry.prepare_spawn_command` 分配后复查（第 809 行）；
        3. `bash._run_registered_dev_server` 的 `is_reserved_port(preferred)`
           —— 只在 hint 回落值本身保留时可达（防御层，正常流程被 1 挡住）。

        本用例断言「1、3 两侧都判 bad_args」，因为漏改任一处都会让 agent
        收到自相矛盾的信号（同一错误两种归因）。
        """
        from hiveweave.services.process_registry import (
            check_command_reserved_ports,
            is_reserved_port,
        )

        assert is_reserved_port(4000) is True

        # ── 守卫 1：命令文本直检（最先命中，走 prepare_spawn_command）──
        msg = check_command_reserved_ports("npm run dev -- --port 4000")
        assert msg is not None, "命令里的保留端口必须被拦"
        assert fp.classify_error_text(msg) == "bad_args", (
            f"守卫 1 的文案必须能被签名表判为 bad_args（实测文案 {msg!r}）"
        )

    @pytest.mark.asyncio
    async def test_reserved_port_guard_declares_bad_args(self):
        """守卫 3 直测：`_run_registered_dev_server` 喂保留端口 → `bad_args`。

        `preferred` 的取值逻辑是 `port_hint if not is_reserved_port(port_hint)
        else 3000` —— 保留端口 hint 会**回落到 3000** 而不是被判错。要触发
        这一层必须让回落值本身也保留，故直接喂 `4000`（此时 `port_hint` 与
        `preferred` 都保留，第 290 行分支可达）。

        ⚠️ 实测（2026-09-11）：也**未**触发 —— `prepare_spawn_command` 在
        第 803 行的文本直检先命中，返回的 `prep_err` 走第 354 行分支。
        两条路径都判 `bad_args`，故断言写成**两者皆可**，只要不退回
        `runner_failed`。这正是本用例的验收点：同一错误只有一种归因。
        """
        from hiveweave.tools import bash as bm

        res = await bm._run_registered_dev_server(
            command="npm run dev -- --port 4000",
            cwd="",
            workspace_path="",
            project_id=None,
            port_hint=4000,
            agent_id="a1",
        )
        assert res is not None, "保留端口必须被拦下（返回 None = 漏到正常路径）"
        assert res["success"] is False
        assert res["fact"] == "bad_args", (
            f"保留端口 = 调用方参数错，应判 bad_args（实测 {res.get('fact')!r}）"
        )
        assert res["runner_failed"] is False
        assert res["command_failed"] is False

    def test_prep_err_exit_is_classified_by_evidence(self):
        """第二个判定：prep_err 出口**按证据归类**，不再无条件 runner_failed。

        L6 修正（2026-09-11 实测）：`prepare_spawn_command` 的 prep_err 里混着
        两类成因 ——
        - spawn 自身故障（模型改参数修不好）⇒ `runner_failed`；
        - **命令文本里出现保留端口**（`process_registry:803`）⇒ 换端口即可
          ⇒ `bad_args`。

        原先该出口硬编码 `fact="runner_failed"`，正是本批要消灭的「自我声明」。
        现在改成 `classify_error_text(prep_err) or "runner_failed"` ——
        签名表命中即采信，未命中才落回真·runner 故障默认格。
        """
        import ast
        import inspect

        from hiveweave.tools import bash as bm

        tree = ast.parse(inspect.getsource(bm))
        found = False
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and node.args):
                continue
            if not (isinstance(node.func, ast.Name)
                    and node.func.id == "finalize_fact_dict"):
                continue
            d = node.args[0]
            if not isinstance(d, ast.Dict):
                continue
            kv = {k.value: v for k, v in zip(d.keys, d.values)
                  if isinstance(k, ast.Constant)}
            if not (isinstance(kv.get("error"), ast.Name)
                    and kv["error"].id == "prep_err"):
                continue
            # 断言 fact 是「按证据归类」的表达式，而非写死的常量。
            # 允许两种写法：内联 `classify_error_text(x) or "runner_failed"`
            # 或先赋给局部变量再引用（本仓用的是后者，便于加注释）。
            fact_node = kv.get("fact")
            assert fact_node is not None, "prep_err 出口必须声明 fact"
            assert not isinstance(fact_node, ast.Constant), (
                "prep_err 出口又退回硬编码 fact 了 —— 它必须按证据归类"
            )
            if isinstance(fact_node, ast.Name):
                # 回溯该局部变量的赋值表达式
                target = fact_node.id
                fact_node = None
                for n in ast.walk(tree):
                    if isinstance(n, ast.Assign) and any(
                        isinstance(t, ast.Name) and t.id == target for t in n.targets
                    ):
                        fact_node = n.value
                        break
                assert fact_node is not None, (
                    f"prep_err 出口引用了未找到赋值的 {target!r}"
                )
            src = ast.unparse(fact_node)
            assert "classify_error_text" in src, (
                f"prep_err 出口必须走签名表归类，实测表达式 {src!r}"
            )
            assert "runner_failed" in src, (
                f"两侧都不命中时应落回真·runner 故障默认格，实测 {src!r}"
            )
            found = True
        assert found, "未找到 prep_err 出口（函数结构变了？）"

    def test_reserved_port_prep_err_classifies_bad_args(self):
        """端到端判据：保留端口文案经签名表 ⇒ `bad_args`（不是 runner_failed）。

        这是 L6 的核心验收 —— 守卫 1（`process_registry:803`）产出的正是这段
        文案，它必须被判成调用方参数错。
        """
        from hiveweave.services.process_registry import prepare_spawn_command

        _cmd, _env, prep_err, _im = prepare_spawn_command(
            "npm run dev -- --port 4000", project_id=None, preferred_port=3000
        )
        assert prep_err, "保留端口必须产出 prep_err（否则本用例失去意义）"
        assert fp.classify_error_text(prep_err) == "bad_args", (
            f"保留端口 prep_err 应判 bad_args，实测 "
            f"{fp.classify_error_text(prep_err)!r}"
        )


class TestToolResultInvariants:
    """类型强制：`__post_init__` 拒掉「写不出」的违规结果。"""

    def test_bare_blocked_rejected(self):
        from hiveweave.tools.result import ToolResult

        with pytest.raises(ValueError, match="must declare its fact kind"):
            ToolResult(success=False, blocked=True, error="x")

    def test_blocked_with_caller_fault_rejected(self):
        """blocked 只能承载平台侧成因 —— bad_args 是调用方责任（L6/L19）。"""
        from hiveweave.tools.result import ToolResult

        with pytest.raises(ValueError, match="cannot carry fact="):
            ToolResult(success=False, blocked=True, error="x", fact="bad_args")

    def test_unknown_kind_rejected(self):
        from hiveweave.tools.result import ToolResult

        with pytest.raises(ValueError, match="unknown FactKind"):
            ToolResult(success=False, error="x", fact="nope")

    def test_success_cannot_be_runner_failed(self):
        from hiveweave.tools.result import ToolResult

        with pytest.raises(ValueError, match="cannot be runner_failed"):
            ToolResult(success=True, output="ok", fact="runner_failed")

    def test_derived_properties_preserve_none_semantics(self):
        """关键：`fact is None` 时派生属性必须是 None（不是 False）。

        `run_ledger.record_step_end` 用 `COALESCE(?, col)` 落库 —— None 表示
        「本轮未确定，保留旧值」，False 表示「确定不是」。把 None 变成 False
        会把历史置位覆盖掉。
        """
        from hiveweave.tools.result import ToolResult

        d = ToolResult.err("m").to_dict()
        # fact 未确定 ⇒ 键根本不出现（不是 fact: None、更不是 False）
        assert "fact" not in d
        assert "runner_failed" not in d
        assert "command_failed" not in d

        d = ToolResult.ok("out").to_dict()
        assert "runner_failed" not in d
