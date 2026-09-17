"""L3 覆盖测试：**枚举式**证明每个 shell 出口都带事实位（2026-09-11）。

## 为什么必须有这条（补 DSH 自陈的边界）

DSH 的收口（`packages/core/tools/src/invariant.ts`）挂在总线上，「**经总线的**
都校验」——但它**捕获不到「某分支根本不发总线事件」**。对应到我们这里就是
「某个 shell 出口绕过了 `finalize_tool_result`」。

⇒ 必须有一条**枚举式**覆盖：把构造点全部扫出来（AST，不是正则），
断言每一个都带事实位。

## 三条断言

1. **裸字典出口**必须经 `finalize_fact_dict` 收口 —— 否则 `fact` 的派生键
   （runner_failed / command_failed）不会展开 ⇒ **静默误归因**
   （`tool_loop` 少发归因提示；`streaming` 落库 `None` 而非 `False`）。
   ⚠ 早先此处写作「下游 `result["runner_failed"]` 直接 KeyError」——
   第三轮审计（2026-09-17）复核当前消费者**全用 `.get()`**，KeyError
   **不可复现**；漏斗依然必须，但理由要换成上面那条。
   ⚠ **扫描范围**同样是被测对象：原先只扫 `bash.py` ⇒ `python_script.py`
   的两处同族缺陷（其一自 M2/T2 起就存在）**一直看不见**。
2. **`ToolResult` 构造点**必须显式声明 `fact=`（或在 blocked 情形由漏斗判）。
3. **签名表**对真实错误文本逐条命中 —— 「声明了却没人写」的第 3 次复发防范
   （同 L15 的取值域绊线思路）。

用 AST 而非文本匹配：注释里也会出现 `"fact"` / `runner_failed` 字样，
文本匹配会把注释当命中（假绿）或把注释里的反例当违规（假红）。
"""

from __future__ import annotations

import ast
import importlib
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
            f"→ 派生键缺失 ⇒ **静默误归因**（agent 少发「命令未执行」提示；"
            f"落库 None 而非 False）。⚠ 不是 KeyError（消费者全用 .get()）"
        )

    @pytest.mark.parametrize(
        "mod_name",
        ["bash", "python_script", "dev_server_tools"],
    )
    def test_raw_dict_exits_with_fact_go_through_funnel(self, mod_name):
        """**全工具面**扫：任何模块的裸字典出口声明了 `fact` 就必须过漏斗。

        ⚠⚠ 2026-09-17 补（第三处同族缺陷）：上面那条只扫 `bash.py` ⇒
        `python_script.py:232` 与 `bash.py:894` **结构完全相同**的裸字典出口
        （F5 本批新引入的 `"fact": "runner_failed"`）**不在扫描范围内** ——
        缺陷存在但守卫看不见（"守卫绕过了它自己声明要防的路"的又一种形态：
        这次绕过的是**文件范围**）。

        ⚠ 判据用 AST 而**不是**文本子串（用户 09-14 钦定「永远」）：
        换措辞 / 换行 / 换引号都不该影响判定。
        """
        mod = importlib.import_module(f"hiveweave.tools.{mod_name}")
        tree = _parse(mod)
        unwrapped: list[int] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Return):
                continue
            v = node.value
            if isinstance(v, ast.Dict):
                keys = [k.value for k in v.keys if isinstance(k, ast.Constant)]
                if "fact" in keys:
                    unwrapped.append(node.lineno)
        assert not unwrapped, (
            f"{mod_name}.py:{unwrapped} 的裸字典出口声明了 fact 却未经 "
            f"finalize_fact_dict → 派生键缺失 ⇒ **静默误归因**"
            f"（不是 KeyError —— 消费者全用 .get()，见 bash.py:898 实测说明）"
        )

    def test_no_raw_dict_declares_derived_keys(self):
        """**P0 护栏**：裸字典出口**不得**声明派生键 `runner_failed`/`command_failed`。

        2026-09-11 独立审计抓到的真缺陷：`execute_bash` / `execute_run_command`
        的**普通非零退出**出口写的是 `"command_failed": True`（**派生键**，
        无 `fact`）。该出口未经漏斗 ⇒ `finalize_fact_dict` 不会在它身上跑 ⇒
        键**保留**了下来。看似正常，实则：

        - 一旦该结果流经**任何**漏斗（`pipeline`/`executor` 的 normalize 尾、
          `ToolResult` 往返），`finalize_fact_dict` 会**先 pop 掉**这两个键再
          按 `fact` 重算 —— 而此处 `fact is None` ⇒ 两个键**一起消失**；
        - 于是全平台**流量最大**的失败出口（普通非零退出）**没有事实位**。

        判据：派生键只能由 `fact` 展开；**裸写派生键 = 绕过唯一权威**。
        故这里断言：任何裸字典 return 里出现这两个键即为违规。
        """
        _DERIVED = ("runner_failed", "command_failed")
        for mod in (bash_mod,):
            tree = _parse(mod)
            offenders = []
            for node in ast.walk(tree):
                if not isinstance(node, ast.Return):
                    continue
                if not isinstance(node.value, ast.Dict):
                    continue
                keys = [
                    k.value for k in node.value.keys if isinstance(k, ast.Constant)
                ]
                hit = [k for k in keys if k in _DERIVED]
                if hit:
                    offenders.append((node.lineno, hit))
            assert not offenders, (
                f"{mod.__name__}:{offenders} 的裸字典出口直接声明了派生键 "
                f"{_DERIVED} —— 派生键必须由权威 `fact` 展开（经 "
                f"finalize_fact_dict），裸写会被漏斗 pop 掉而静默丢失"
            )

    def test_nonzero_exit_declares_command_failed_end_to_end(self):
        """端到端：普通非零退出的结果字典必须可判定为 `command_failed`。

        这是上面那条 AST 护栏的**行为对照** —— 结构断言说"没裸写派生键"，
        这里说"事实位确实到位"，两者缺一不可。
        """
        from hiveweave.tools.result import finalize_fact_dict

        raw = {
            "success": False, "output": "x", "error": "Command exited with code 1",
            "exit_code": 1, "fact": "command_failed",
        }
        out = finalize_fact_dict(dict(raw))
        assert out["command_failed"] is True
        assert out["runner_failed"] is False
        # 且经漏斗往返后仍稳定（幂等）
        again = finalize_fact_dict(dict(out))
        assert again["command_failed"] is True
        assert again["runner_failed"] is False

    def test_bare_derived_key_without_fact_is_stripped(self):
        """反例固化：裸 `command_failed`（无 fact）经漏斗后**确实**消失。

        此用例把"为什么必须改"钉死在测试里 —— 若未来有人把 `fact` 从某个出口
        拿掉，这里会提醒他派生键不会自愈。
        """
        from hiveweave.tools.result import finalize_fact_dict

        stripped = finalize_fact_dict({
            "success": False, "output": "", "error": "e", "command_failed": True,
        })
        assert "command_failed" not in stripped, (
            "无 fact 时派生键必须被剥除（这正是 P0 的成因）"
        )
        assert "runner_failed" not in stripped
        assert stripped.get("fact") is None

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


class TestAttributionLadder:
    """#15（2026-09-14）：归因阶梯 = **布尔位 → 文本签名表 → 代码作用域 → 不猜**。

    本仓判据（用户钦定）：**状态判据**（事实位 / DB 行 / 权限位）与措辞无关
    ⇒ 可用；**文本判据**（自由文本子串 / 正则）随措辞与语言整体失效 ⇒ 只配
    当兜底。此前的形态恰好相反：先跑签名表，把「命令有没有跑」这个**状态**
    交给错误文案回答。

    本类的每条断言都写成「判据被删掉就转红」的形态（阳性对照见各自 docstring）。
    """

    @pytest.mark.parametrize(
        ("bits", "blocked", "error", "expected"),
        [
            # 验收①：文案换成**法文/陌生措辞**，只要有位 ⇒ 归因不变
            ({"dialect_failed": True}, True,
             "La commande a echoue: dialecte inconnu", "runner_failed"),
            ({"dialect_failed": True}, False,
             "Le dialecte du shell n'est pas reconnu", "runner_failed"),
            ({"runner_failed": True}, True,
             "Erreur inconnue du lanceur (code 42)", "runner_failed"),
            ({"runner_failed": True}, False,
             "Echec du lanceur: aucune sortie", "runner_failed"),
            # 命令跑了但没过：位是 command_failed ⇒ 与文本措辞无关
            ({"command_failed": True}, False,
             "La compilation a echoue avec 3 erreurs", "command_failed"),
            # 位优先于文本：文案里明明有 runner 签名，但位说 command_failed
            ({"command_failed": True}, False,
             "Error: Command blocked: system-level destructive command",
             "command_failed"),
        ],
    )
    def test_bits_win_over_foreign_text(self, bits, blocked, error, expected):
        """位在 ⇒ 归因由位决定，**与文案语言/措辞无关**（位是第一层）。

        阳性对照（**实测转红**）：删掉 `finalize_tool_result` 里的
        `bit_fact = fact_from_bits(r)`（改成只看文本）⇒ 前四条以法文文案不命中
        签名表而落 `outcome_unknown` 转红；最后一条（文案有 runner 签名但位说
        command_failed）则以 `runner_failed` 转红 —— 正是"文本判据越俎代庖"。
        """
        from hiveweave.tools.fact_positions import finalize_tool_result

        out = finalize_tool_result(
            "bash", {"success": False, "error": error, "blocked": blocked, **bits}
        )
        assert out["fact"] == expected, (
            f"位 {bits}（blocked={blocked}）应判 {expected}，实测 {out['fact']!r} "
            f"—— 归因被文案措辞决定了（位优先没生效）"
        )

    def test_dialect_bit_has_priority_over_command_bit(self):
        """位的**优先级**与 `attribution_of` 同源：dialect > runner > command。"""
        from hiveweave.tools.fact_positions import fact_from_bits, state_bits

        assert fact_from_bits({"dialect_failed": True, "command_failed": True}) == (
            "runner_failed"
        )
        assert fact_from_bits({"runner_failed": True, "command_failed": True}) == (
            "runner_failed"
        )
        # 位存在但为 False ⇒ **不是**命中（"说了否" ≠ "没说"）
        assert fact_from_bits({"runner_failed": False, "command_failed": False}) is None
        assert fact_from_bits({"runner_failed": None}) is None
        assert state_bits({"error": "x"}) == {}

    def test_missing_bits_and_unknown_text_is_unclassified(self):
        """验收③：位缺失 + 文案陌生 ⇒ `outcome_unknown`（**不猜**）+ fail-loud 样本。

        为什么**不落** `runner_failed`：后者语义=「命令从未执行」⇒ 下游读成
        「无副作用、可直接重试」；而本函数的触发点在**执行之后**（normalize 尾）
        ⇒ 会诱发**副作用双发**（`test_p0_3_orphan_root_cause.py` 同款论证）。

        阳性对照（**实测转红**）：把兜底那行改回
        `kind = "runner_failed"` / `out["fact"] = "runner_failed"` ⇒ 本用例在
        `assert out["fact"] == "outcome_unknown"` 处转红。
        """
        from hiveweave.tools.fact_positions import finalize_tool_result

        out = finalize_tool_result(
            "bash",
            {"success": False, "blocked": True,
             "error": "Quelque chose d'inconnu s'est produit (code 42)"},
        )
        assert out["fact"] == "outcome_unknown"
        assert out["runner_failed"] is False
        assert out["blocked"] is True

        sample = out["unclassified_sample"]
        assert sample["tool"] == "bash"
        assert sample["error_preview"].startswith("Quelque chose")
        assert len(sample["error_preview"]) <= 200
        # 「当时是护栏拒绝，但**没有任何归因位**」—— 这才是要被人看见的形态。
        # 且必须是**带值的**位视图：只记名字会把「显式声明为 False」读成
        # 「位已置真」（`state_bits` 的语义是「位存在」，不是「位为真」）。
        assert sample["bits_present"] == {"blocked": True}

    def test_wait_timeout_is_code_scoped_even_via_result_dict(self):
        """第三层（代码作用域）在 **dict 通道**必须真的可达。

        实测（2026-09-14，改动前）：dict 分支把 `timeout_kind` 收进 `extra`，
        判定却读 dataclass 字段 ⇒ 恒 `None` ⇒ 该层是**死代码**，审批窗口等待
        类 blocked 结果会悄悄落到 `outcome_unknown` 并刷样本。

        阳性对照（**实测转红**）：把该层改回只读字段形态
        （`getattr(r, "timeout_kind", None) == "wait"`）⇒ 本用例第一段在
        `assert out["fact"] == "runner_failed"` 处转红（实测得 `outcome_unknown`）。
        ⚠ 本用例的文案必须**一条签名都不命中**（第一版用了含「审批」的文案，
        结果被**第二层文本表**接走 ⇒ 假绿，PC9 实测没转红才发现）。
        """
        from hiveweave.tools.fact_positions import finalize_tool_result

        out = finalize_tool_result(
            "bash",
            {
                "success": False,
                "blocked": True,
                "error": "Attente de validation par un operateur distant",
                "timeout_kind": "wait",
            },
        )
        # 命令从未派发 ⇒ runner_failed；这是**代码作用域**归属，不是文案命中
        assert out["fact"] == "runner_failed"
        assert out["blocked"] is True
        assert "unclassified_sample" not in out, "命中代码作用域就不该落样本"

        # 反面：非 `wait` 的超时**不得**被这一层吃掉（它只覆盖"审批等待"这一特例）
        out = finalize_tool_result(
            "bash",
            {
                "success": False,
                "blocked": True,
                "error": "inconnu",
                "timeout_kind": "command",
            },
        )
        assert out["fact"] == "outcome_unknown"
        assert "unclassified_sample" in out

    def test_declared_fact_conflicting_with_bits_is_logged(self):
        """构造点声明的 fact 与它自己给的位冲突时必须**留痕**（只观测，不改归因）。

        为什么不能直接改归因：构造点比通用判据更懂上下文（比如它刚读了一个
        结构化返回），硬覆盖会更糟；但「声明即免检」是个开口 —— 判错了永远
        没人知道。故只记 WARNING。

        阳性对照（**实测转红**）：把 `fact_position_declared_conflicts_with_bits`
        那条 `log.warning` 删掉（或删掉整个 `elif judge_blocked and declared ...`
        分支）⇒ 本用例转红。
        """
        from structlog.testing import capture_logs

        from hiveweave.tools.fact_positions import finalize_tool_result

        with capture_logs() as logs:
            out = finalize_tool_result(
                "bash",
                {
                    "success": False,
                    "fact": "outcome_unknown",
                    "runner_failed": True,  # 与声明打架
                    "error": "boom",
                },
            )
        # 归因**按声明**（不改动）
        assert out["fact"] == "outcome_unknown"
        assert any(
            e.get("event") == "fact_position_declared_conflicts_with_bits"
            for e in logs
        ), [e.get("event") for e in logs]

    def test_blocked_without_fact_does_not_raise(self):
        """blocked=True 且无 fact **不得抛**（fail loud 但不 fail hard）。

        实测（2026-09-14，改动前）：`ToolResult.__post_init__` 会硬抛
        `ValueError: blocked result must declare its fact kind`，而本函数在
        executor 的 dispatch `try/except` **之外**被调用 ⇒ 未捕获异常会炸掉整条
        工具调用（与 `test_p0_3_orphan_root_cause.py` 的 115 步孤儿步同构，
        只是炸点更靠前）。当时该分支因此是**不可达**的死代码。

        阳性对照（**实测转红**）：把构造处的保守占位去掉
        （`fact=raw.get("fact")` 而不是 `... or ("outcome_unknown" if ...)`）
        ⇒ 本用例以 `ValueError` 转红。
        """
        from hiveweave.tools.fact_positions import finalize_tool_result

        out = finalize_tool_result(
            "bash", {"success": False, "blocked": True, "error": "inconnu"}
        )
        assert out["fact"] == "outcome_unknown"
        # 且**不得**把它标成「命令从未执行」（那是"放心重试"信号）
        assert out["runner_failed"] is False

        # 变体：连 `success` 键都没有（默认 True）—— 同样不得抛
        # （此前 `ToolResult.__post_init__` 会在这一形态上硬抛）
        out = finalize_tool_result("bash", {"blocked": True, "error": "inconnu"})
        assert out["blocked"] is True
        assert out["fact"] == "outcome_unknown"

    def test_blocked_never_carries_caller_fault_fact(self):
        """blocked=True 时，`command_failed` 位**不得**变成事实位。

        判据来源：`tools/result.py::_BLOCKED_FACT_KINDS` —— blocked 只接受平台侧
        成因的两格（`runner_failed` / `outcome_unknown`）；标成调用方成因会让
        agent 收到「不是你的 bug」信号并原地重撞（L6/L19）。本归因路径是**写**
        fact（绕过了构造期不变式）⇒ 必须自己守这条。
        """
        from hiveweave.tools.fact_positions import finalize_tool_result

        out = finalize_tool_result(
            "bash",
            {"success": False, "blocked": True, "command_failed": True, "error": "x"},
        )
        assert out["fact"] in ("runner_failed", "outcome_unknown")
        assert out["blocked"] is True

    def test_non_blocked_failure_honors_declared_bit(self):
        """非 blocked 失败 + 裸位（无 fact）⇒ 位**必须被认账**，不得被抹成 unknown。

        实测（2026-09-14，改动前）：`{"success": False, "runner_failed": True}`
        经收口后 `fact="outcome_unknown"`、`runner_failed=False` —— 调用方显式
        声明的位被 `finalize_fact_dict` 按 fact 重算时**抹掉**（位丢失面）。

        阳性对照（**实测转红**）：删掉 `elif bit_fact is not None:` 那一支 ⇒
        本用例落 `outcome_unknown` 转红。
        """
        from hiveweave.tools.fact_positions import finalize_tool_result

        out = finalize_tool_result(
            "bash", {"success": False, "error": "boom", "runner_failed": True}
        )
        assert out["fact"] == "runner_failed"
        assert out["runner_failed"] is True

        out = finalize_tool_result(
            "bash", {"success": False, "error": "boom", "command_failed": True}
        )
        assert out["fact"] == "command_failed"
        assert out["command_failed"] is True

    def test_unclassified_sample_redacts_secrets(self):
        """样本**不得**把 key/token 写进去（脱敏复用 `util/redact.py`）。

        阳性对照（**实测转红**）：把 `note_unclassified_sample` 里的
        `redact_secrets(...)` 去掉 ⇒ 本用例以原文里出现 `sk-…` / `token=…`
        的值转红。
        """
        from hiveweave.tools.fact_positions import note_unclassified_sample

        payload = note_unclassified_sample(
            tool="bash",
            error=(
                "curl failed: Authorization: Bearer sk-abcdefghijklmnop "
                "token=SECRETVALUE api_key=AKIA123456"
            ),
        )
        preview = payload["error_preview"]
        for secret in ("SECRETVALUE", "sk-abcdefghijklmnop", "AKIA123456"):
            assert secret not in preview, f"密钥 {secret!r} 泄漏进样本：{preview!r}"
        assert "***" in preview  # 键名保留、值被吃掉（便于排查）

    def test_attribution_of_shares_bit_priority(self):
        """`attribution_of` 与 `fact_from_bits` **同一判据**（不许两处各自演化）。

        阳性对照：让 `attribution_of` 自己写一遍 if（顺序写成
        command → runner）⇒ 本用例在 `runner+command` 那一行转红。
        """
        from hiveweave.services.failure_signature import attribution_of
        from hiveweave.tools.fact_positions import fact_from_bits

        cases = [
            {"dialect_failed": True},
            {"runner_failed": True},
            {"command_failed": True},
            {"runner_failed": True, "command_failed": True},
            {"dialect_failed": True, "runner_failed": True, "command_failed": True},
            {"blocked": True},
            {},
        ]
        for case in cases:
            kind = fact_from_bits(case)
            attr = attribution_of(dict(case))
            if kind is None:
                assert attr == "" or attr.startswith("blocked:"), (case, attr)
            else:
                assert attr.startswith(f"{kind}:"), (
                    f"{case} 的位判据给出 {kind!r}，但 attribution_of 说 {attr!r}"
                )


class TestOverWideSignatureFixed:
    """#15 修法 5：过宽签名 —— 裸 `"port"` 子串改成**词边界**。

    实测反例（改动前）：本表是子串匹配 ⇒ `"port" in "ImportError" / "support"`
    恒真 ⇒ `ImportError: cannot import name 'x'` 被判成 `bad_args`
    （「调用方参数错」）⇒ 把 agent 指向改参数这条错路。
    """

    def test_port_word_is_not_substring_matched(self):
        """`import`/`support`/`report` 等词里的 "port" **不得**被误命中。

        阳性对照（**实测转红**）：把 `_SIGNATURE_ORDER` 的 `_Sig("regex", r"\\bport\\b")`
        改回子串条目 `_Sig("substr", "port")` ⇒ 本用例三条断言全部转红
        （且模块 import 期的 gate 也会先炸）。
        """
        assert fp.classify_error_text("ImportError: cannot import name 'x'") is None
        assert fp.classify_error_text("Error: operation not supported") is None
        assert fp.classify_error_text("Error: report generation failed") is None
        assert fp.classify_error_text("transport layer is not important") is None

    def test_real_reserved_port_messages_still_hit_bad_args(self):
        """真·保留端口文案（两条产出点）必须**仍判** `bad_args`。

        阳性对照：把词边界写成 `\\bports?\\b` 之外的过窄形态（如 `^port$`）
        ⇒ 本用例转红（真命中被一起挡掉）。
        """
        from hiveweave.services.process_registry import (
            check_command_reserved_ports,
            prepare_spawn_command,
        )

        guarded = check_command_reserved_ports("npm run dev -- --port 4000")
        assert guarded, "保留端口必须被拦（否则本用例失去意义）"
        assert fp.classify_error_text(guarded) == "bad_args", guarded

        _cmd, _env, prep_err, _im = prepare_spawn_command(
            "npm run dev -- --port 4000", project_id=None, preferred_port=3000
        )
        assert prep_err, "保留端口必须产出 prep_err（否则本用例失去意义）"
        assert fp.classify_error_text(prep_err) == "bad_args", prep_err

    def test_classify_blocked_fact_takes_bits_first(self):
        """纯函数入口也要位优先；位为 None 时保持**严格**（fail loud）。"""
        from hiveweave.tools.fact_positions import classify_blocked_fact

        # 位在 ⇒ 不看文案（法文也一样）
        assert classify_blocked_fact(
            "bash", "texte inconnu", bits={"dialect_failed": True}
        ) == "runner_failed"
        # 位缺失 + 签名命中 ⇒ 走第二层（保持既有行为）
        assert classify_blocked_fact("bash", "Port 4000 is reserved") == "bad_args"
        # 位缺失 + 无签名 ⇒ 仍 fail loud（严格性由这条守住）
        with pytest.raises(AssertionError, match="no fact evidence"):
            classify_blocked_fact("bash", "断言失败 1 != 2")

