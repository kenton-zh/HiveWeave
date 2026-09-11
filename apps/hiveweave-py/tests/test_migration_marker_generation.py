"""迁移标记必须随 DB 世代失效（L18 收口 · DSH slot-vs-lifecycle）。

DSH ``packages/session/session-projection-cache/src/spec.ts:33-40`` 原文：

    The stored-log identity a record is bound to: the immutable header fields
    that distinguish one session lifecycle from another under the same id.
    **A session id names a slot, not a lifecycle** — a deleted-then-recreated
    id, or a persistence root swapped under a surviving cache, would otherwise
    let an old record pass every watermark check and seed state folded from an
    unrelated log. Reads validate this against the live header before accepting
    any record.

本项目 L17 实况事故（TEST_DSH_52_A 的 ``no such column: wake``）就是这个形态的
极端：``_migrated`` 的键是 **workspace 路径 / project_id / agent_id**（槽位），
库被整代重建回到基础列之后标记仍然存活 → 补列静默跳过 → 只在**下游**炸开。

这一模式在本仓共 **9 处**（不是 1 处、也不是 4 处）。按判据「同一模式第 3 次
出现时，改的是机制不是点位」，它们统一改用
:func:`db.project.schema_marker_key_for_agent` /
:func:`db.project.schema_marker_key_for_project`。

本文件锁三件事：

    A. 9 处标记都经**共用世代键**取键（防有人改回裸 id）；
    B. 键的形状是 ``(slot, generation)``；
    C. 同一路径的库整代重建后，新旧键**必然不同**（这是"失效"的定义）。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from hiveweave.db import project as project_db

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "hiveweave"

# 同一模式的全部落点（2026-09-11 收口）。新增一处就往这里加一行，
# 下面的测试会强制它使用共用世代键。
_MARKER_SITES = [
    "services/attestation.py",
    "services/audit_retry.py",
    "services/dispatch.py",
    "services/handoff.py",
    "services/inbox.py",
    "services/inbox_triage.py",
    "services/roster.py",
    "services/tasks/db.py",
    "services/wait_contract.py",
]


def _shared_key_call_names(tree: ast.AST) -> set[str]:
    """返回被赋值为「共用访问器调用结果」的变量名。

    只认真的调用（AST 层），不认文档字符串里出现的名字 —— 审计指出：只做
    文本子串匹配时，9 个文件的 docstring 都写着
    ``:func:`db.project.schema_marker_key_for_agent` ``，于是**把代码回退成
    裸 id 当键也会照样绿**，等于没挡。
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        value: ast.AST | None = None
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            value, targets = node.value, list(node.targets)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            value, targets = node.value, [node.target]
        if value is None:
            continue
        if isinstance(value, ast.Await):  # `key = await project_db.schema_...(…)`
            value = value.value
        if not isinstance(value, ast.Call):
            continue
        func = value.func
        attr = (
            func.attr if isinstance(func, ast.Attribute)
            else func.id if isinstance(func, ast.Name)
            else ""
        )
        if not attr.startswith("schema_marker_key_for_"):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    return names


def _is_marker_set(node: ast.AST) -> bool:
    """``_migrated`` / ``_retry_migrated``（裸名或 ``self.`` 前缀）。"""
    if isinstance(node, ast.Name):
        return node.id in {"_migrated", "_retry_migrated"}
    if isinstance(node, ast.Attribute):
        return node.attr in {"_migrated", "_retry_migrated"}
    return False


def _guard_tests_the_computed_key(tree: ast.AST, names: set[str]) -> bool:
    """这些变量是否被真正用于 ``<var> in _migrated`` 这种守卫判定。"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        left = node.left
        if not (isinstance(left, ast.Name) and left.id in names):
            continue
        for comparator, op in zip(node.comparators, node.ops):
            if isinstance(op, ast.In) and _is_marker_set(comparator):
                return True
    return False


def test_every_marker_site_computes_its_key_via_the_shared_helper():
    """9 处「已迁移」标记都必须**真的调用**共用世代键，且真的用它做判定。

    三重断言（缺一不可）：
      1. AST 里存在对 ``schema_marker_key_for_*`` 的调用；
      2. 该调用的结果被赋给某个变量；
      3. 该变量被用于 ``<var> in _migrated`` 守卫（防"调了但没用"）。
    """
    offenders: dict[str, str] = {}
    for rel in _MARKER_SITES:
        path = _SRC / rel
        assert path.exists(), f"标记落点已搬迁：{rel}（请更新本清单）"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = _shared_key_call_names(tree)
        if not names:
            offenders[rel] = "没有调用共用访问器（键可能已回退成裸 id）"
        elif not _guard_tests_the_computed_key(tree, names):
            offenders[rel] = f"算了键 {sorted(names)} 但没用于 `in _migrated` 守卫"
    assert not offenders, (
        "迁移标记必须走共用世代键 (workspace, 连接世代) 并真正用于守卫判定："
        f"{offenders}。裸 id/路径当键会让标记跨库世代存活 —— 见 L17 实况事故。"
    )


def test_generation_key_shape_is_slot_plus_generation():
    """键必须是 ``(slot, generation)`` 二元组，且 generation 可比较。"""
    ws_key = project_db.schema_marker_key_for_workspace("/tmp/hw-spot-check")
    assert isinstance(ws_key, tuple) and len(ws_key) == 2
    assert isinstance(ws_key[0], str)
    assert isinstance(ws_key[1], int)

    # 空路径不得炸（调用方可能在 project 未就绪时调用）
    assert project_db.schema_marker_key_for_workspace("") == ("", 0)


@pytest.mark.asyncio
async def test_same_path_rebuilt_db_yields_a_different_key(tmp_path):
    """世代语义：同路径的库整代重建后，键必须变（= 旧标记自动失效）。

    这是本机制存在的**唯一理由** —— 若键不随重建而变，L17 会原样复发。
    """
    ws = str(tmp_path / "ws")

    await project_db.ensure_project_db(ws)
    first = project_db.schema_marker_key_for_workspace(ws)

    # 模拟「同一路径上的库被整代重建」：丢连接（等价于目录被删后重建）
    async with project_db._ensure_lock:
        old = project_db._cache.pop(ws, None)
    if old is not None:
        await old.close()

    await project_db.ensure_project_db(ws)
    second = project_db.schema_marker_key_for_workspace(ws)

    assert first != second, (
        "同路径库重建后键没变 —— 旧迁移标记会继续命中、补列被静默跳过（L17）"
    )
    assert first[0] == second[0], "槽位（路径）分量应当保持不变"
    assert second[1] > first[1], "世代分量必须单调递增"


@pytest.mark.asyncio
async def test_agent_key_falls_back_when_routing_fails():
    """路由失败（agent 未注册 / workspace 被驱逐）→ 回退 agent 级键 + 世代 0。

    后续 ALTER 的 execute 同样会失败，所以这是「不会更糟」的降级路径；
    关键是不能抛异常（旧实现同样如此，行为保持一致）。
    """
    key = await project_db.schema_marker_key_for_agent("no-such-agent-xyz")
    assert key == ("agent:no-such-agent-xyz", 0)
