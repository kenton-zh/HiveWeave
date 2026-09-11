"""空 catch 棘轮（DSH ``AGENTS.md:122``）。

判据原文：
    **An empty `catch` names what it swallows and why nothing else can reach
    it**; keep the `try` to one statement.

本项目 L17 实况事故（TEST_DSH_52_A 的 ``no such column: wake``）的根因正是
``except: pass`` 吞掉 ALTER 失败：三条失败分支**本该**打 warning，而全日志里
``inbox_schema_*`` 0 条 —— 失效是静默的，只在**下游**炸开，归因完全错位
（CEO 把它判成「HR 消息通道平台故障」）。

全库存量 365 处（113 文件，2026-09-11 冻结）一次改完既不现实、也多数是无害的
best-effort（例如 teardown 期间的 rollback 重试）。所以分两级：

  1. **迁移 / schema / 持久化路径零容忍** —— 这些是 L17 同类事故的温床：
     schema 状态被静默吞掉不会当场报错，只会在很久之后的下游以
     ``no such column/table`` 炸开；
  2. **全局棘轮** —— 冻结当前基线，只挡新增（减少不报错，增加即失败）。
     基线是 ``tests/_bare_except_baseline.json``；清理了存量就把对应数字
     往下调（**只能降**），不要为了让它变绿而调高。
"""

from __future__ import annotations

import ast
import json
import pathlib

import pytest

_SRC_ROOT = pathlib.Path(__file__).resolve().parents[1] / "src" / "hiveweave"
_BASELINE_PATH = pathlib.Path(__file__).with_name("_bare_except_baseline.json")

# 迁移 / schema / 持久化关键路径：裸 except 必须为 0。
# 往这里加文件时，请先把该文件的裸 except 清理干净（写明它吞掉了什么）。
_ZERO_TOLERANCE = {
    "api/projects.py",
    "db/meta.py",
    "db/project.py",
    "services/attestation.py",
    "services/audit_retry.py",
    "services/dispatch.py",
    "services/handoff.py",
    "services/inbox.py",
    "services/inbox_triage.py",
    "services/mcp.py",
    "services/roster.py",
    "services/tasks/db.py",
    "services/tasks/verify.py",
    "services/wait_contract.py",
}


def _is_empty_handler_body(body: list[ast.stmt]) -> bool:
    """块体是否「什么都没做」：单条 ``pass``，或单条 ``...``（等价 pass）。

    只看 ``ast.Pass`` 会漏掉 ``except Exception: ...`` 这个等价写法
    （审计实测当前用量为 0，但棘轮的完整性不该依赖"没人这么写"）。
    """
    if len(body) != 1:
        return False
    only = body[0]
    if isinstance(only, ast.Pass):
        return True
    return (
        isinstance(only, ast.Expr)
        and isinstance(only.value, ast.Constant)
        and only.value.value is Ellipsis
    )


def _bare_except_counts() -> dict[str, int]:
    """每个文件的空 catch 数（``except ...:`` 的块体什么都没做）。"""
    counts: dict[str, int] = {}
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # 不该发生；真坏了有别的测试会红
            continue
        n = sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, ast.ExceptHandler)
            and _is_empty_handler_body(node.body)
        )
        if n:
            counts[path.relative_to(_SRC_ROOT).as_posix()] = n
    return counts


def _baseline() -> dict:
    return json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))


def _schema_funcs(text: str) -> list[ast.AST]:
    """取出模块里所有「schema / 迁移自检 / 建表」函数（裸 except 的高危温床）。

    名字口径不能只看 ``schema|migrat``：真正建全表 + 列自检的
    ``ensure_project_db``、项目创建的 ``create_project`` 都不含这两个词，
    却是同一类温床（审计实测的漏网）。
    """
    tree = ast.parse(text)
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and (
            "schema" in node.name
            or "migrat" in node.name
            or node.name.startswith("ensure_")
            or "create_project" in node.name
        )
    ]


def test_migration_paths_have_no_bare_except():
    """迁移/schema 函数内部零容忍。

    只查 schema/迁移函数**内部**：这些地方的静默失败正是 L17 的形状 ——
    不会当场报错，只会在很久之后的下游以 ``no such column/table`` 炸开、
    归因完全错位。同一文件里 teardown / rollback 之类的 best-effort 空 catch
    由下面的全局棘轮管（它们在调用栈上离 schema 状态很远）。
    """
    offenders: dict[str, list[int]] = {}
    for rel in sorted(_ZERO_TOLERANCE):
        path = _SRC_ROOT / rel
        assert path.exists(), f"迁移路径已搬迁：{rel}（请更新 _ZERO_TOLERANCE）"
        for func in _schema_funcs(path.read_text(encoding="utf-8")):
            bad = [
                sub.lineno
                for sub in ast.walk(func)
                if isinstance(sub, ast.ExceptHandler)
                and _is_empty_handler_body(sub.body)
            ]
            if bad:
                offenders.setdefault(rel, []).extend(bad)
    assert not offenders, (
        "迁移/schema 函数里不得有裸 except（DSH AGENTS.md:122：写清它吞掉了什么，"
        f"并保持 try 体只有一条语句）；file → 行号：{offenders}"
    )


def test_bare_except_total_does_not_grow():
    """全局棘轮：总数只能降。"""
    baseline = _baseline()
    counts = _bare_except_counts()
    total = sum(counts.values())
    assert total <= baseline["_total"], (
        f"裸 except 总数从基线 {baseline['_total']} 涨到 {total}。"
        "新增处请写明吞掉了什么（log.warning/debug），而不是 pass；"
        "若确实是有意为之的空 catch，至少在旁边注释说明为什么没有别的路径能到达。"
    )


def test_no_file_exceeds_its_own_baseline():
    """逐文件棘轮 —— 挡住「A 文件减 5、B 文件加 5、总数不变」这种此消彼长。"""
    baseline = _baseline()
    known = baseline["files"]
    counts = _bare_except_counts()
    grew = {
        f: {"baseline": known.get(f, 0), "now": n}
        for f, n in sorted(counts.items())
        if n > known.get(f, 0)
    }
    assert not grew, f"这些文件的裸 except 超过各自基线：{grew}"


def test_baseline_matches_reality_for_known_files():
    """基线自身必须仍然认识这些文件（防重命名/搬迁后棘轮悄悄失效）。"""
    baseline = _baseline()
    missing = [
        rel for rel in baseline["files"]
        if not (_SRC_ROOT / rel).exists()
    ]
    if missing:
        pytest.fail(
            "基线里的文件已不存在（重命名/搬迁后棘轮会静默失效，请重新生成）："
            f"{missing[:10]}"
        )
