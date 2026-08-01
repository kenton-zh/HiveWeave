"""calculate tool — scientific calculator via safe AST evaluation.

契约 02: 工具执行器 — calculate 子模块
- LLM 心算不可靠（多位乘除/浮点/大数）且无证据链；复杂数学必须走本工具。
- 用 Python `ast` 白名单求值（非 eval 裸跑）：只允许数字常量、四则/取模/幂、
  括号、白名单 math 函数与常量。禁止属性访问、下标、字符串、比较、import 等。
- 无文件/网络/进程副作用 → requires_workspace=False, security_level="standard"，
  所有角色（含 CEO/HR）默认可用。
- 结果格式化：int 完整显示（大整数不截断），float 用 %.15g 保持可读精度。
"""

from __future__ import annotations

import asyncio
import ast
import math
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# ── Constants ───────────────────────────────────────────────

MAX_EXPR_LEN = 500
"""表达式长度上限 — 防超长输入烧 token / 深递归。"""

MAX_NEST_DEPTH = 64
"""AST 嵌套深度上限 — 防深递归 DoS。"""

MAX_RESULT_DIGITS = 4300
"""结果位数上限（计算前预估）— 防超大整数计算 DoS。

对齐 Python 自带 int→str 4300 位限制（C 层字符串转换上限），
但那个限制是事后检查，不防计算本身：`9**9**8` 只有 7 个字符，
却要先算出 4100 万位整数（冻结事件循环数分钟）才会报错。
所有可能产生超大整数的运算（幂 / factorial / exp）必须在计算前
用对数预估位数并拒绝。
"""


def _check_estimated_digits(digits: int | float) -> None:
    """位数预估超限即拒绝（计算前守卫）。

    抛裸消息（不带调用者名）：Call 分支会统一加函数名前缀，
    幂运算符（BinOp）由 _safe_pow 自行包装。
    """
    if not math.isfinite(digits) or digits > MAX_RESULT_DIGITS:
        raise ValueError(f"result would exceed {MAX_RESULT_DIGITS} digits — refused")


def _guard_or_raise(what: str, digits: int | float) -> None:
    try:
        _check_estimated_digits(digits)
    except ValueError as e:
        raise ValueError(f"{what}: {e}") from None


def _safe_pow(left: Any, right: Any) -> Any:
    """幂运算（带计算前位数守卫）。

    大整数幂是主要 DoS 面：先对数预估结果位数（int 指数精确，
    float 指数估算），超限直接拒绝，避免在事件循环上做 4100 万位乘法。
    """
    base = abs(left)
    if base not in (0, 1) and isinstance(right, (int, float)):
        digits: float | int = 0
        if isinstance(right, int) and right > 0:
            try:
                digits = int(right * math.log10(base)) + 1
            except OverflowError:
                digits = MAX_RESULT_DIGITS + 1
            _guard_or_raise("power", digits)
        elif isinstance(right, float) and right > 0:
            try:
                digits = right * math.log10(base) + 1
            except OverflowError:
                digits = MAX_RESULT_DIGITS + 1
            _guard_or_raise("power", digits)
    result = left**right
    if isinstance(result, complex):
        raise ValueError(
            f"result is a complex number ({result!r}) — "
            f"negative base with fractional exponent is not supported"
        )
    return result


def _safe_factorial(n: int) -> int:
    """factorial 带 Stirling 位数预估（n! 位数 ≈ n·log10(n/e)）。"""
    if n < 0:
        raise ValueError(f"factorial({n}): negative argument")
    if n >= 100:  # Stirling 在小 n 时不稳，小 n 直接算（结果小）
        try:
            digits = (
                n * math.log10(n / math.e) + 0.5 * math.log10(2 * math.pi * n)
            )
        except (OverflowError, ValueError):
            digits = MAX_RESULT_DIGITS + 1
        _check_estimated_digits(digits)  # Call 分支会加 factorial(n) 前缀
    return math.factorial(n)


def _safe_exp(x: Any) -> float:
    """exp 带位数预估（exp(x) 位数 ≈ x·log10(e)）。"""
    try:
        digits = abs(x) * math.log10(math.e)
    except (OverflowError, ValueError, TypeError):
        digits = MAX_RESULT_DIGITS + 1
    _check_estimated_digits(digits)  # Call 分支会加 exp 前缀
    return math.exp(x)

# math 白名单函数（不含任何可逃逸对象访问的函数）
_SAFE_FUNCS: dict[str, Any] = {
    "abs": abs,
    "sqrt": math.sqrt,
    "cbrt": math.cbrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "atan2": math.atan2,
    "sinh": math.sinh,
    "cosh": math.cosh,
    "tanh": math.tanh,
    "log": math.log,
    "log10": math.log10,
    "log2": math.log2,
    "exp": _safe_exp,
    "pow": math.pow,
    "hypot": math.hypot,
    "fmod": math.fmod,
    "floor": math.floor,
    "ceil": math.ceil,
    "trunc": math.trunc,
    "round": round,
    "gcd": math.gcd,
    "lcm": math.lcm,
    "factorial": _safe_factorial,
    "degrees": math.degrees,
    "radians": math.radians,
    "isfinite": math.isfinite,
    "isnan": math.isnan,
    "isinf": math.isinf,
    "copysign": math.copysign,
}

# 白名单常量（仅数字常量，无对象）
_SAFE_CONSTS: dict[str, Any] = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
    "inf": math.inf,
    "nan": math.nan,
}


def _unsafe(node: ast.AST, what: str) -> ValueError:
    line = getattr(node, "lineno", "?")
    return ValueError(
        f"unsupported syntax ({what}) at line {line} — only numbers, "
        f"+ - * / // % **, parentheses, math functions and constants are allowed"
    )


def _eval_node(node: ast.AST, depth: int) -> Any:
    """递归白名单求值。仅返回 int / float；其余抛 ValueError。"""
    if depth > MAX_NEST_DEPTH:
        raise ValueError("expression nested too deeply")

    if isinstance(node, ast.Expression):
        return _eval_node(node.body, depth + 1)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        raise _unsafe(node, f"literal {type(node.value).__name__}")

    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left, depth + 1)
        right = _eval_node(node.right, depth + 1)
        op = type(node.op)
        if op is ast.Add:
            return left + right
        if op is ast.Sub:
            return left - right
        if op is ast.Mult:
            return left * right
        if op is ast.Div:
            return left / right
        if op is ast.FloorDiv:
            return left // right
        if op is ast.Mod:
            return left % right
        if op is ast.Pow:
            result = _safe_pow(left, right)
            return result
        raise _unsafe(node, f"operator {type(node.op).__name__}")

    if isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand, depth + 1)
        if type(node.op) is ast.USub:
            return -operand
        if type(node.op) is ast.UAdd:
            return +operand
        raise _unsafe(node, f"unary operator {type(node.op).__name__}")

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise _unsafe(node, "non-name function call")
        func_name = node.func.id
        func = _SAFE_FUNCS.get(func_name)
        if func is None:
            raise _unsafe(node, f"function '{func_name}' not in whitelist")
        args = [_eval_node(a, depth + 1) for a in node.args]
        if node.keywords:
            raise _unsafe(node, "keyword arguments")
        try:
            result = func(*args)
        except ValueError as e:
            # math domain errors → 友好信息（如 sqrt(-1), log(0)）
            raise ValueError(f"{func_name}({', '.join(map(str, args))}): {e}")
        except OverflowError:
            raise ValueError(f"{func_name}({', '.join(map(str, args))}): overflow")
        if isinstance(result, complex):
            raise ValueError(
                f"{func_name}({', '.join(map(str, args))}): complex result not supported"
            )
        return result

    if isinstance(node, ast.Name):
        const = _SAFE_CONSTS.get(node.id)
        if const is None:
            raise _unsafe(node, f"unknown name '{node.id}'")
        return const

    raise _unsafe(node, type(node).__name__)


def _format_result(value: Any) -> str:
    """int 完整显示；float 用 %.15g 保持可读精度。"""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        formatted = format(value, ".15g")
        # `-0.0` 的 IEEE 负零与数学零无区别，避免 LLM 误读为整数 `-0`
        if formatted == "-0":
            return "0"
        return formatted
    return repr(value)


def evaluate_expression(expression: str) -> str:
    """求值数学表达式，返回结果字符串。失败抛 ValueError（带原因）。"""
    expr = (expression or "").strip()
    if not expr:
        raise ValueError("expression is required")
    if len(expr) > MAX_EXPR_LEN:
        raise ValueError(
            f"expression too long ({len(expr)} chars, max {MAX_EXPR_LEN})"
        )
    # `^` 幂运算 → Python `**`（计算器语境无位运算需求）
    expr = expr.replace("^", "**")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ValueError(f"invalid expression: {e.msg}")
    result = _eval_node(tree, 0)
    return _format_result(result)


async def execute_calculate(expression: str) -> dict[str, Any]:
    """Evaluate a math expression. Returns {success, output, error}."""
    try:
        # 兜底防线：同步求值放线程池，即使预估守卫有遗漏，也只卡线程不冻事件循环
        output = await asyncio.to_thread(evaluate_expression, expression)
    except ValueError as e:
        log.info("calculate.error", error=str(e)[:160])
        return {"success": False, "output": "", "error": f"Error: {e}"}
    except Exception as e:  # noqa: BLE001 — 防御未知求值异常
        log.warning("calculate.crashed", error=repr(e))
        return {"success": False, "output": "",
                "error": f"Error: calculation failed — {e}"}
    log.info("calculate.ok", expression=expression[:120], result=output[:80])
    return {"success": True, "output": output, "error": None}


# ── Pydantic models + @tool registration ──────────────────────

from pydantic import BaseModel, Field, ConfigDict

from .base import tool
from .result import ToolResult


class CalculateParams(BaseModel):
    """Parameters for calculate tool."""
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    expression: str = Field(
        description=(
            "Math expression to evaluate, e.g. '2**10', 'sqrt(2)', "
            "'(1+5)/3', 'sin(pi/2)', '123456789*987654321'. Supports + - * / "
            "// % **, parentheses, functions (sqrt cbrt sin cos tan asin acos "
            "atan atan2 sinh cosh tanh log log10 log2 exp pow hypot fmod "
            "floor ceil trunc round gcd lcm factorial degrees radians) and "
            "constants (pi e tau inf). '^' is power."
        ),
        json_schema_extra={"aliases": ["expr", "formula", "math", "calc"]},
    )


@tool(
    "calculate",
    "Evaluate a math expression exactly (scientific calculator). "
    "Use it for ANY non-trivial arithmetic (large numbers, floats, percentages, "
    "multiplications, trigonometry, logarithms) — never compute by hand. "
    "Returns the exact result.",
    requires_workspace=False,
    security_level="standard",
)
async def calculate_tool(
    params: CalculateParams, agent_id: str, workspace: str
) -> ToolResult:
    """Evaluate a math expression."""
    result = await execute_calculate(params.expression)
    if result.get("success"):
        return ToolResult.ok(
            f"{params.expression} = {result['output']}"
        )
    return ToolResult.err(result.get("error", "Unknown error"))
