"""F6 守卫：「收尾许可」不许由白名单谓词产生（commit-license guard）。

根因（PLATFORM-ISSUES §1.3 / §11）：``get_actionable_obligations`` 是
**白名单**谓词（醒后行动清单，排 blocked）；拿它的**空结果**当
「可以收尾 / 名下无待办」的许可输出，账本就对 agent 说谎
（TEST_DSH_61 砺石五次被告知「名下无待办」）。许可语义的唯一判定源是
``services/tasks/obligations.py::TaskService.can_idle``（= not has_open_work，
闭式、fail-closed）。

本守卫按「三 AST 事实合取」（§11.7 订正版，非文案匹配）机械检出违规：

  违规 := 存在 If 节点 I，使得
    (a) I.test 含 ``not`` 包裹的 Name N（直接或经 BoolOp/UnaryOp 摊平）；
    (b) N 在 I 所属函数内被赋值为对 ``get_actionable_obligations`` 的调用
        （func 的 attr/Name 名精确匹配；支持 5 源 AND 多源形态）；
    (c) 违规输出形态二选一：
        (c)-1 I.**body**（含嵌套，不含 orelse）里有
        ``return <str 常量 / f-string / 串接>`` —— ``return None/True/False``
        **不算**（排除 trigger.py 的 R4 例外与 health_supervisor 的
        「许可不唤醒」）；orelse 分支的字符串 return 不算（那是
        "有活"分支，是正当输出）；
        (c)-2 I.body 内存在「以 N 参与字符串拼接的赋值」，且**函数体最后
        一条语句**是 ``return <该变量>``（hint 模板形态：污染变量拼进
        尾部返回的文案）。

**不守什么**（§11.2 精确化）：跨函数数据流看不穿 ——
``game_time._open_duty_probe`` 的白名单结果经 dict 传递回
``_check_silent_agents``，AST 不可判定 ⇒ 该处永远不会被本守卫抓到
（其 docstring 的「口径同源」声明已单独订正为事实表述）。

用法::

    python scripts/verify_commit_license.py [repo_root]   # 违规 exit 1

验收（§11.10 #5/#6）：对真实仓库命中集 = {poll, turn_exit hint} 两处
（F6 修后应为**空**；多于此 = 假阳性，须收窄）；合成第 3 个漏网点必须
报出（tests/test_commit_license_guard.py 正/负 fixture）。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

WHITELIST_PREDICATES = frozenset({"get_actionable_obligations"})
_EXCLUDED_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv"}


def _names_in_test(test: ast.expr) -> set[str]:
    """(a)：test 里被 ``not`` 包裹的 Name 集合（摊平 BoolOp / 嵌套 not）。"""
    out: set[str] = set()
    stack: list[ast.expr] = [test]
    while stack:
        cur = stack.pop()
        if isinstance(cur, ast.BoolOp):
            stack.extend(cur.values)
        elif isinstance(cur, ast.UnaryOp) and isinstance(cur.op, ast.Not):
            if isinstance(cur.operand, ast.Name):
                out.add(cur.operand.id)
            else:
                stack.append(cur.operand)
    return out


def _collect_whitelist_assigns(fn: ast.AST) -> dict[str, None]:
    """函数内「名字 → 白名单谓词调用」赋值表（``await`` 包裹穿透）。"""
    table: dict[str, None] = {}
    for node in ast.walk(fn):
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if value is None:
            continue
        if isinstance(value, ast.Await):
            value = value.value
        if not isinstance(value, ast.Call):
            continue
        f = value.func
        fname = (
            f.attr
            if isinstance(f, ast.Attribute)
            else (f.id if isinstance(f, ast.Name) else "")
        )
        if fname in WHITELIST_PREDICATES:
            for t in targets:
                if isinstance(t, ast.Name):
                    table[t.id] = None
    return table


def _stringy(expr: ast.expr) -> bool:
    """return 值是字符串常量 / f-string / 串接（None / 布尔 / 名字不算）。"""
    if isinstance(expr, ast.Constant):
        return isinstance(expr.value, str)
    if isinstance(expr, ast.JoinedStr):
        return True
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        return True
    return False


def _body_string_returns(if_node: ast.If) -> list[int]:
    """(c)-1：**仅 body 链**（不含 orelse）里 return 字符串的行号。"""
    hits: list[int] = []
    stack: list[ast.stmt] = list(if_node.body)
    while stack:
        stmt = stack.pop()
        if isinstance(stmt, ast.Return) and stmt.value is not None and _stringy(
            stmt.value
        ):
            hits.append(stmt.lineno)
        for attr in ("body", "orelse", "finalbody"):
            for child in getattr(stmt, attr, []) or []:
                stack.append(child)
        for child in ast.iter_child_nodes(stmt):
            if isinstance(child, ast.If):
                stack.extend(child.body)
    return hits


def _tail_concat_names(if_node: ast.If, tainted: set[str]) -> set[str]:
    """(c)-2 前半：body 内「N 参与字符串拼接」的赋值目标名。"""
    concat_targets: set[str] = set()

    def _has_tainted_leaf(expr: ast.expr) -> bool:
        for leaf in ast.walk(expr):
            if isinstance(leaf, ast.Name) and leaf.id in tainted:
                return True
        return False

    for stmt in if_node.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(stmt, ast.Assign):
            targets, value = list(stmt.targets), stmt.value
        elif isinstance(stmt, ast.AugAssign) and isinstance(stmt.op, ast.Add):
            targets, value = [stmt.target], stmt.value
        if value is None:
            continue
        if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
            if _has_tainted_leaf(value) or _stringy(value):
                for t in targets:
                    if isinstance(t, ast.Name):
                        concat_targets.add(t.id)
    return concat_targets


def collect_commit_license_violations(root: Path) -> list[str]:
    """扫描 root 下的 .py，返回违规清单（``file:line: 描述``），空 = 干净。"""
    violations: list[str] = []
    seen: set[tuple[str, int]] = set()
    for path in sorted(Path(root).rglob("*.py")):
        if any(part in _EXCLUDED_DIRS for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        rel = path.as_posix()
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            assigns = _collect_whitelist_assigns(fn)
            if not assigns:
                continue
            tail_return_name: str | None = None
            if fn.body and isinstance(fn.body[-1], ast.Return):
                v = fn.body[-1].value
                if isinstance(v, ast.Name):
                    tail_return_name = v.id
            for node in ast.walk(fn):
                if not isinstance(node, ast.If):
                    continue
                guarded = _names_in_test(node.test) & set(assigns)
                if not guarded:
                    continue
                for lineno in _body_string_returns(node):
                    key = (rel, lineno)
                    if key in seen:  # 嵌套 If 会经多条路径重入，按行去重
                        continue
                    seen.add(key)
                    violations.append(
                        f"{rel}:{lineno}: permission phrasing gated by "
                        f"whitelist-obligation emptiness (``{sorted(guarded)[0]}``"
                        f" from get_actionable_obligations) — use "
                        f"TaskService.can_idle as the sole permission source"
                    )
                if tail_return_name:
                    for name in _tail_concat_names(node, set(assigns)):
                        if name == tail_return_name:
                            key = (rel, node.lineno)
                            if key in seen:
                                continue
                            seen.add(key)
                            violations.append(
                                f"{rel}:{node.lineno}: tail return builds on "
                                f"whitelist-obligation emptiness (``{name}``) — "
                                f"use TaskService.can_idle as the sole "
                                f"permission source"
                            )
    return violations


def main(argv: list[str]) -> int:
    # 默认 = 仓库根（scripts/ 的上一级）—— F6 审计 HIGH-1：默认只扫
    # scripts/ 自己会假绿；裸跑必须扫全仓。
    default_root = Path(__file__).resolve().parent.parent
    root = Path(argv[1]) if len(argv) > 1 else default_root
    violations = collect_commit_license_violations(root)
    if violations:
        print("commit-license guard: 违规（白名单谓词产生收尾许可）：")
        for v in violations:
            print(f"  - {v}")
        print(
            "\n许可语义唯一判定源 = services/tasks/obligations.py::"
            "TaskService.can_idle（闭式 fail-closed）。"
            "白名单 get_actionable_obligations 只做醒后行动清单。"
        )
        return 1
    print("commit-license guard: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
