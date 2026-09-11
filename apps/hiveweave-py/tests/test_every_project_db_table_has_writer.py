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

**交接状态（批次 5 ⇄ 批次 7 · 已收口）**：``modules`` 表按 A 方案保留（形状见
``docs/AI工程组织_MVP蓝图.md:283-287``，含 ``parent_module_id`` 自引用模块树），
写侧由**批次 7** 在 ``services/modules.py::create_module`` 落地（INSERT INTO
modules）。因此 ``modules`` 已从 ``_KNOWN_WRITERLESS_PENDING`` 白名单**移出**，
白名单现在为空 —— 门禁覆盖全部正典表，无豁免。

⚠ **已知边界（P2-4.2，2026-09-12 —— 诚实标注，不是待办）**：本门禁是
**静态扫描**，只认字符串**字面量**里的 INSERT 形态。若有人用拼接构造 SQL
表名（``"INSERT INTO " + tbl`` / f-string 里表名来自变量 / 由 ``%`` 或
``.format`` 填入），AST 看到的不是字面量 ⇒ **本门禁看不见，会漏报**。

**影响方向**：漏报 = 测试**不红**（门禁失效），**不是**误伤好代码。
且需要有人**刻意**这么写才会触发，当前无实际危害 ⇒ 不为它引入运行时检查。

**为什么不用"运行时试一次"替代**（decision-brief §4.2，**明确拒绝套用 DSH**）：
DSH ``packages/AGENTS.md:14`` 的判据（"test denial through the executor"）说的是
**运行时权限强制**——要证明拒绝就走执行器去试。但本门禁是**开发期回归守卫**
（防未来提交把冻结的表重新打开），运行时**根本没有对应的拒绝逻辑**，
它只是个测试。两者的 enforcement 点在**时间轴**上不同：运行时门禁能在执行点
验证，开发期回归守卫**只能在 CI 静态验证**。照抄会把一个正确的静态守卫换成
一个在我们场景下**没有意义**的运行时探针。这正是 MEMORY.md「引用外部参照前
强制三问」第 3 问要防的事（"照它改，会不会做出一个在我们场景下错误的实现？"）。

**若将来要收口**：正确方向是**增强静态扫描**（如同时识别
``"INSERT INTO " + x`` / ``f"INSERT INTO {x}"`` 的拼接模式并告警），
**而不是**换成运行时。
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


# 已知的「表已建、写侧未落地」白名单（**必须随写侧落地而清空**）。
# 这些表的存在是有意为之（先建对形状、接线随后），不是遗漏；
# 门禁对它们放行，但仍会在它们之外的任何新死表上打红。
#
# **当前为空**：批次 7 已在 ``services/modules.py`` 落地 ``modules`` 的写入方
# （``create_module`` 的 INSERT INTO modules），按批次 5 的交接约定把它移出。
# 空集合 = 门禁覆盖全部正典表，没有豁免。
_KNOWN_WRITERLESS_PENDING: frozenset[str] = frozenset()


def test_every_project_db_table_has_a_writer():
    """每张 per-project DB 表都必须有 INSERT 写入方（防下一个 modules 死表）。"""
    names = _canonical_table_names()
    writers = _writers_by_table()
    dead = sorted(
        t for t in names
        if not writers.get(t) and t not in _KNOWN_WRITERLESS_PENDING
    )
    assert not dead, (
        f"这些表建在 PROJECT_DB_TABLES 里但**全仓零 INSERT**：{dead}。\n"
        "「建了表没人写」= 死表（`modules` 曾长期如此：读取路由永远返回空）。\n"
        "请二选一：① 接上写入方；② 若确认无消费者，从 DDL 摘除"
        "（并同步 db/meta.py 的 _LEGACY_TABLES_TO_DROP 与读取路由）。"
        f"（若确属「形状先建、写侧随后」的交接状态，请显式加入"
        f" _KNOWN_WRITERLESS_PENDING 并写明接管方。）"
    )


def test_known_writerless_allowlist_does_not_rot():
    """白名单不得腐烂：已接上写入方的表必须从白名单移出。

    防「白名单变成垃圾桶」—— 某张表其实早已有 writer，却还挂在豁免里，
    让门禁对它永久失明（这是我们加门禁要防的同一类病）。
    """
    writers = _writers_by_table()
    stale = sorted(t for t in _KNOWN_WRITERLESS_PENDING if writers.get(t))
    assert not stale, (
        f"这些表已在 _KNOWN_WRITERLESS_PENDING 里，但**已经有写入方**了："
        f"{stale} —— 请把它们从白名单删掉，让门禁恢复覆盖。"
    )


def test_known_writerless_allowlist_entries_are_real_tables():
    """白名单里的每张表都必须是正典里的真表（防打错字造成假豁免）。"""
    names = _canonical_table_names()
    unknown = sorted(t for t in _KNOWN_WRITERLESS_PENDING if t not in names)
    assert not unknown, f"白名单里有正典中不存在的表名：{unknown}"


def test_gate_detects_a_simulated_dead_table():
    """负样本：门禁必须能对"无写入方"打红 —— 否则它是假绿。"""
    writers_real = _writers_by_table()
    # 造一张确定没人写的表名，走同一判定逻辑
    assert not writers_real.get("__no_such_table_written_anywhere__")
    # 且真表确实有写入方（证明判定不是恒真）
    assert writers_real.get("tasks"), "tasks 应该被多处 INSERT"
