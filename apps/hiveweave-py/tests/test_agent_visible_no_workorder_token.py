"""用户可见文案不得泄漏内部工单号（``fixqueue #…``）—— **AST 构造点**守卫。

判据是**结构化**的：用 AST 枚举"面向 agent 的异常/回执文案构造点"
（``raise <Exc>(...)`` 与 ``*.err / *.ok / *.blocked_err(...)`` 的实参），
断言其字符串取值不含 ``fixqueue``。

实参覆盖面（本文件守的就是它不能窄于真实构造点）：
  · **位置实参** 与 **关键字实参**（``err(message="…")``）；
  · 字符串取值形态：常量 ``str``、f-string 静态片段、以及 ``+`` 拼接（``BinOp``）。

**刻意不做文件子串扫描**：注释 / docstring 里保留 ``fixqueue`` 是允许的
（那是给维护者看的），子串扫描会把它们误判成违规 —— 故只取构造点的实参。

阳性对照：把同一提取器指向一段**带工单号**的合成源码 ⇒ 必须报出违规
（含 kwargs / BinOp 两种构造点）；反向边界：注释 / docstring 里的工单号
**不**被提取（证明"只扫构造点"成立）。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "hiveweave"
#: 局部修补涉及的两处面向 agent 的面（异常文案 + 回执文案）。
_SCAN_DIRS = (("services", "acl_sandbox"), ("tools",))
_TOKEN = "fixqueue"
#: ``ToolResult`` 一族的回执构造方法名（文本对 agent 可见）。
_RECEIPT_FUNCS = {"err", "ok", "blocked_err"}


def _string_parts(node: ast.AST) -> list[str]:
    """常量 ``str`` + f-string 静态片段 + ``+`` 拼接（``BinOp``）的静态文本。"""
    out: list[str] = []
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        out.append(node.value)
    elif isinstance(node, ast.JoinedStr):
        for value in node.values:
            out.extend(_string_parts(value))
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        # 字符串拼接（``"a" + "b"`` / ``f"x" + y`` / ``"a" "b"`` 已由解析器合并）。
        out.extend(_string_parts(node.left))
        out.extend(_string_parts(node.right))
    return out


def _message_strings(source: str) -> list[str]:
    """源码里**面向 agent 的文案构造点**的字符串实参（结构化枚举）。

    覆盖**位置实参**与**关键字实参**（``err(message="…")``）；漏掉任何一类
    都会让"换个写法就逃逸"（本仓审计过同类逃逸）。
    """
    tree = ast.parse(source)
    found: list[str] = []
    for node in ast.walk(tree):
        call: ast.Call | None = None
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            call = node.exc
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _RECEIPT_FUNCS
        ):
            call = node
        if call is None:
            continue
        for arg in call.args:
            found.extend(_string_parts(arg))
        for kw in call.keywords:
            if kw.value is not None:  # kw.arg is None ⇒ **kwargs 展开
                found.extend(_string_parts(kw.value))
    return found


def _iter_module_files():
    for parts in _SCAN_DIRS:
        yield from sorted(_SRC_ROOT.joinpath(*parts).rglob("*.py"))


def test_no_workorder_token_in_agent_visible_messages():
    """真实源码：acl_sandbox 与 tools 的异常/回执文案**不得**含内部工单号。"""
    offenders: dict[str, list[str]] = {}
    for path in _iter_module_files():
        hits = [
            s
            for s in _message_strings(path.read_text(encoding="utf-8"))
            if _TOKEN in s
        ]
        if hits:
            offenders[str(path)] = hits
    assert not offenders, (
        f"用户可见的异常/回执文案出现了内部工单号 `{_TOKEN}`"
        f"（应只留在注释 / docstring）：{offenders}"
    )


@pytest.mark.parametrize(
    "snippet",
    [
        'raise SandboxUnavailableError("blocked (fixqueue #2「A1」)")',
        'raise RuntimeError(f"bad: {x} —— fixqueue #7")',
        'def f():\n    return ToolResult.err("nope fixqueue #9")',
        # kwargs 构造点：位置实参换成 ``message=`` 后**不得**逃逸。
        'def f():\n    return ToolResult.err(message="nope fixqueue #10")',
        # BinOp 构造点：字符串拼接**不得**逃逸。
        'raise RuntimeError("blocked: " + "fixqueue #11")',
        # kwargs + BinOp 同时出现（最容易被漏的组合写法）。
        'def f():\n    return ToolResult.blocked_err("x", message="a " + "fixqueue #12")',
    ],
)
def test_positive_control_extractor_flags_a_workorder(snippet: str):
    """阳性对照：带工单号的**构造点**必须被提取器抓到（否则守卫是空壳）。

    覆盖位置实参 / kwargs / BinOp 三类构造点 —— 任意一类漏掉都算守卫有缺口。
    """
    assert any(_TOKEN in s for s in _message_strings(snippet)), snippet


def test_extractor_ignores_comments_and_docstrings():
    """边界：注释 / docstring 里的工单号**不算**违规（这正是禁用子串扫描的理由）。"""
    src = (
        '"""见 fixqueue #2 残余 R3。"""\n'
        "# 退休 fixqueue #2 残余\n"
        "x = 1\n"
    )
    assert not any(_TOKEN in s for s in _message_strings(src))
