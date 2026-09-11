"""机械门禁：``PROJECT_DB_TABLES`` 里的**每张表必须有写入方**。

2026-09-11 教训（fixplan §6 #13）：``modules`` 表被建进 per-project DB 的
正典 DDL，却**全仓零 INSERT** —— 表建了没人写，读取路由成了永远返回空数组
的装饰品。这类"定义了但没接线"的缺陷不会被任何功能测试打到（表存在 ⇒
查询不报错 ⇒ 测试全绿），只能靠**机械门禁**在代码层断言。

判据来源：我们自己的 schema 模型（``db/schema.py::PROJECT_DB_TABLES`` 是
per-project DB 的唯一权威源，见 ``db/project.py:215`` 的建表循环），
外加 fixplan §6 #13「附带立机械门，PROJECT_DB_TABLES 里每张表必须有写入方」。

实现要点（避免"文本子串断言"的假绿）：
- 用 **AST** 遍历 ``src/hiveweave/**.py``，只取**字符串字面量**里的 SQL；
  docstring / 注释 / 日志文案里的 "INSERT INTO xxx" 不算（子串 grep 会
  被这些糊过去）。
- 写入方判定 = 字符串字面量里出现 ``INSERT ... INTO <table>``（含
  ``INSERT OR REPLACE/IGNORE`` 与 ``REPLACE INTO``）。
- 回退代码（删掉某张表的 INSERT）会让本测试**打红**，这是它存在的全部意义。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from hiveweave.db.schema import PROJECT_DB_TABLES

_SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "hiveweave"

# 建表 DDL 之外的"表名出现在 DDL 字符串里"不算写入方 —— 只认 INSERT 形态。
_INSERT_RE = re.compile(
    r"\bINSERT\s+(?:OR\s+\w+\s+)?INTO\s+[`\"\[]?(\w+)",
    re.IGNORECASE,
)
_REPLACE_RE = re.compile(r"\bREPLACE\s+INTO\s+[`\"\[]?(\w+)", re.IGNORECASE)


def _canonical_table_names() -> set[str]:
    """从 ``PROJECT_DB_TABLES`` 的建表 DDL 里取表名集合。"""
    names: set[str] = set()
    for ddl in PROJECT_DB_TABLES:
        for m in re.finditer(
            r"CREATE TABLE IF NOT EXISTS\s+[`\"\[]?(\w+)", ddl, re.IGNORECASE
        ):
            names.add(m.group(1))
    return names


def _iter_string_literals() -> list[tuple[str, str]]:
    """(file_rel, literal) —— 所有 Python 字符串字面量（含 f-string 静态段）。

    只遍历 ``ast.Constant(str)`` 与 f-string 的 ``Str`` 部分：注释/docstring
    也确实是 ``ast.Constant``，但 docstring 不可能写成可执行的 INSERT
    （它们在 ``Expr`` 里）；为稳妥起见这里额外排除模块/类/函数首条 docstring。
    """
    out: list[tuple[str, str]] = []
    for py in _SRC_ROOT.rglob("*.py"):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        docstrings: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                body = getattr(node, "body", None)
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)
                ):
                    docstrings.add(id(body[0].value))
        rel = str(py.relative_to(_SRC_ROOT.parent.parent))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in docstrings:
                    continue  # docstring 不是可执行 SQL
                out.append((rel, node.value))
            elif isinstance(node, ast.JoinedStr):
                # f-string：把静态段拼起来，表名基本都在静态段里
                parts = [
                    v.value
                    for v in node.values
                    if isinstance(v, ast.Constant) and isinstance(v.value, str)
                ]
                if parts:
                    out.append((rel, "".join(parts)))
    return out


def _writers_by_table() -> dict[str, list[str]]:
    """table → 出现 INSERT/REPLACE 的源文件列表。"""
    writers: dict[str, list[str]] = {t: [] for t in _canonical_table_names()}
    for rel, lit in _iter_string_literals():
        if "INSERT" not in lit.upper() and "REPLACE" not in lit.upper():
            continue
        for m in _INSERT_RE.finditer(lit):
            writers.setdefault(m.group(1), [])
            if rel not in writers[m.group(1)]:
                writers[m.group(1)].append(rel)
        for m in _REPLACE_RE.finditer(lit):
            writers.setdefault(m.group(1), [])
            if rel not in writers[m.group(1)]:
                writers[m.group(1)].append(rel)
    return writers


def test_canonical_tables_extracted():
    """自检：表名抽取没坏（抽取为空 ⇒ 下面的断言会空转假绿）。"""
    names = _canonical_table_names()
    assert "tasks" in names
    assert "inbox" in names
    assert len(names) >= 20, f"只抽到 {len(names)} 张表？抽取口径变了请复核"


def test_every_project_db_table_has_a_writer():
    """每张 per-project DB 表都必须有 INSERT 写入方（防下一个 modules 死表）。"""
    names = _canonical_table_names()
    writers = _writers_by_table()
    dead = sorted(t for t in names if not writers.get(t))
    assert not dead, (
        f"这些表建在 PROJECT_DB_TABLES 里但**全仓零 INSERT**：{dead}。\n"
        "「建了表没人写」= 死表（`modules` 就是这样：读取路由永远返回空）。\n"
        "请二选一：① 接上写入方；② 若确认无消费者，从 DDL 摘除"
        "（并同步 db/meta.py 的 _LEGACY_TABLES_TO_DROP 与读取路由）。"
    )


def test_gate_detects_a_simulated_dead_table():
    """负样本：门禁必须能对"无写入方"打红 —— 否则它是假绿。"""
    writers_real = _writers_by_table()
    # 造一张确定没人写的表名，走同一判定逻辑
    assert not writers_real.get("__no_such_table_written_anywhere__")
    # 且真表确实有写入方（证明判定不是恒真）
    assert writers_real.get("tasks"), "tasks 应该被多处 INSERT"
