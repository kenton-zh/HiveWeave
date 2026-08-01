"""Tests for the calculate (scientific calculator) tool.

Covers: basic arithmetic, math functions, large numbers, floats,
error cases (invalid syntax, unsafe AST, division by zero, over-long),
LLM-facing schema, and permission availability for all roles.
"""

from __future__ import annotations

import asyncio

import pytest

from hiveweave.services.permission import (
    CEO_TOOLS,
    COORDINATOR_BUILDER_TOOLS,
    HR_TOOLS,
    READONLY_TOOLS,
)
from hiveweave.tools.base import get_tool_def, get_tool_schema_for_llm
from hiveweave.tools.calculator import (
    MAX_EXPR_LEN,
    MAX_NEST_DEPTH,
    evaluate_expression,
    execute_calculate,
)


# ── Evaluate (pure function) ────────────────────────────────


@pytest.mark.parametrize(
    "expr, expected",
    [
        ("2+2", "4"),
        ("2**10", "1024"),
        ("(1+5)/3", "2"),
        ("123456789*987654321", "121932631112635269"),
        ("100/3", "33.3333333333333"),
        ("7//2", "3"),
        ("7%2", "1"),
        ("2^10", "1024"),  # ^ → **
        ("-5+3", "-2"),
        ("+5", "5"),
        ("sqrt(2)", "1.4142135623731"),
        ("sin(pi/2)", "1"),
        ("log(100, 10)", "2"),
        ("abs(-7)", "7"),
        ("round(3.14159, 2)", "3.14"),
        ("factorial(5)", "120"),
        ("gcd(12, 18)", "6"),
        ("pi", "3.14159265358979"),
        ("2**100", "1267650600228229401496703205376"),
        ("floor(3.9)", "3"),
        ("ceil(3.1)", "4"),
    ],
)
def test_evaluate_basic(expr: str, expected: str):
    assert evaluate_expression(expr) == expected


def test_evaluate_float_precision():
    # 浮点精度保留 .15g
    assert evaluate_expression("0.1+0.2") == "0.3"
    assert evaluate_expression("1/8") == "0.125"


def test_evaluate_trig_degrees():
    assert evaluate_expression("degrees(pi)") == "180"
    assert evaluate_expression("radians(180)") == "3.14159265358979"


def test_evaluate_nested():
    assert evaluate_expression("(2+3)*(4+5)") == "45"
    assert evaluate_expression("-1+1") == "0"


# ── Errors / safety ─────────────────────────────────────────


def test_evaluate_empty_expression():
    with pytest.raises(ValueError, match="expression is required"):
        evaluate_expression("")
    with pytest.raises(ValueError, match="expression is required"):
        evaluate_expression("   ")


def test_evaluate_invalid_syntax():
    with pytest.raises(ValueError, match="invalid expression"):
        evaluate_expression("2+")
    with pytest.raises(ValueError, match="invalid expression"):
        evaluate_expression("(2+3")


def test_evaluate_division_by_zero():
    with pytest.raises(ZeroDivisionError):
        evaluate_expression("1/0")


def test_evaluate_math_domain_error():
    with pytest.raises(ValueError, match="sqrt"):
        evaluate_expression("sqrt(-1)")
    with pytest.raises(ValueError, match="log"):
        evaluate_expression("log(0)")


def test_evaluate_complex_result_rejected():
    with pytest.raises(ValueError, match="complex"):
        evaluate_expression("(-8)**(1/3)")


@pytest.mark.parametrize(
    "expr",
    [
        "__import__('os').system('echo pwned')",
        "open('/etc/passwd')",
        "[].__class__",
        "().__class__.__bases__",
        "lambda: 1",
        "'str'",
        "b'bytes'",
        "True",
        "1 if True else 0",
        "a < b",
        "[1,2,3]",
        "{1: 2}",
        "os.getcwd()",
        "math.sqrt(4)",  # 属性访问拒绝（常量/函数必须裸名）
        "2; import os",
        "1@2",  # MatMult 节点拒绝
        "(x := 3)",  # walrus 节点拒绝
        "f'{2}'",  # f-string（JoinedStr）节点拒绝
        "~5",  # 位运算（Invert）拒绝
        "2 << 3",  # 位移拒绝
        "1 and 0",  # BoolOp 拒绝
        "not 1",  # UnaryOp Not 拒绝
        "{1,2,3}",  # Set 拒绝
        "(1,2)",  # Tuple 拒绝
        "1 + 2j",  # complex 字面量拒绝
    ],
)
def test_evaluate_rejects_unsafe(expr: str):
    with pytest.raises(ValueError):
        evaluate_expression(expr)


# ── DoS 守卫（审计 C1：超大整数计算前位数预估拒绝）─────────────


def test_pow_digit_guard_rejects_huge_result():
    # 审计复现：`9**9**8` 结果 4100 万位，之前会冻结事件循环数分钟
    with pytest.raises(ValueError, match="exceed"):
        evaluate_expression("9**9**8")


def test_pow_digit_guard_rejects_huge_int_pow():
    with pytest.raises(ValueError, match="exceed"):
        evaluate_expression("2**1000000")


def test_pow_digit_guard_allows_big_but_reasonable():
    # 2**14200 ≈ 4275 位 —— 边界内（<4300）仍可算
    out = evaluate_expression("2**14200")
    assert len(out) == 4275


def test_factorial_digit_guard_rejects_huge():
    # 审计复现：factorial(2000000) 曾耗时 14.3s
    with pytest.raises(ValueError, match="exceed"):
        evaluate_expression("factorial(2000000)")


def test_factorial_guard_allows_reasonable():
    # 1500! ≈ 4115 位，边界内正常返回
    out = evaluate_expression("factorial(1500)")
    assert len(out) == 4115


def test_exp_digit_guard_rejects_huge():
    with pytest.raises(ValueError, match="exceed"):
        evaluate_expression("exp(100000)")


def test_exp_digit_guard_allows_reasonable():
    # exp(100) ≈ 43 位
    out = evaluate_expression("exp(100)")
    assert out.startswith("2.68811714181614e+43")


def test_execute_calculate_rejects_dos_quickly():
    # 端到端：错误立即返回（不冻结），且含友好信息
    result = asyncio.run(execute_calculate("9**9**8"))
    assert result["success"] is False
    assert "exceed" in result["error"]


def test_evaluate_too_long():
    long_expr = "1" + "+1" * MAX_EXPR_LEN
    with pytest.raises(ValueError, match="too long"):
        evaluate_expression(long_expr)


def test_evaluate_nested_too_deep():
    # 左结合加法链生成深 AST（括号不产生节点，深度来自运算嵌套）
    deep = "+1" * (MAX_NEST_DEPTH + 40)
    with pytest.raises(ValueError, match="nested too deeply"):
        evaluate_expression("1" + deep)


# ── execute_calculate (async wrapper) ───────────────────────


def test_execute_calculate_ok():
    result = asyncio.run(execute_calculate("2**20"))
    assert result["success"] is True
    assert result["output"] == "1048576"
    assert result["error"] is None


def test_execute_calculate_error():
    result = asyncio.run(execute_calculate("sqrt(-1)"))
    assert result["success"] is False
    assert "Error:" in result["error"]


def test_execute_calculate_empty():
    result = asyncio.run(execute_calculate(""))
    assert result["success"] is False


def test_format_negative_zero_float():
    # 审计 m1：-0.0 不得显示为 `-0`（避免 LLM 误读为整数）
    assert evaluate_expression("-0.0") == "0"
    assert evaluate_expression("0*-1") == "0"
    assert evaluate_expression("-1*0.0") == "0"


def test_unknown_params_rejected():
    # 审计 m2：extra="forbid" —— 未知参数不得静默吞掉
    from hiveweave.tools.calculator import CalculateParams

    with pytest.raises(Exception):
        CalculateParams(expression="2+2", bogus=1)


# ── Registration & schema ───────────────────────────────────


def test_calculate_registered_and_schema():
    td = get_tool_def("calculate")
    assert td is not None
    assert td.requires_workspace is False
    assert td.security_level == "standard"

    schema = get_tool_schema_for_llm("calculate")
    assert schema is not None
    props = schema.get("properties", {})
    assert "expression" in props
    assert schema.get("required") == ["expression"]
    assert "sqrt" in str(props.get("expression", {}))
    assert "calculator" in td.description.lower()


def test_calculate_in_all_role_tool_sets():
    assert "calculate" in READONLY_TOOLS
    assert "calculate" in CEO_TOOLS
    assert "calculate" in COORDINATOR_BUILDER_TOOLS
    assert "calculate" in HR_TOOLS
