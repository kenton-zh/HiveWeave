"""P1-3 ②：merge 成功必须留下**事实** `merge_landed`（否则等待方只能等 TTL）。

病灶：`grep -rn "fact_bus.publish(" src/` = 1（只有 `fs_errors.py`），而 merge 的
**三个成功返回点**一处都没发 —— 尽管 `fact_bus.py` 的 docstring 把 `merge_landed`
当例子。等待 `kind="fact"` 契约的 agent 因此永远等不到。

判据：
- 行为：调 `_publish_merge_landed(...)` ⇒ `fact_bus.recent_facts("merge_landed")` 命中，
  `subject` = short_id、payload 带 branch/target/hash；
- **结构（AST）**：模块内 `_publish_merge_landed(...)` 调用点数 **== `"merged": True`
  的成功字典数**（3 == 3）⇒ 新增成功返回点若漏发 fact，本条会红。
"""

from __future__ import annotations

import ast
import inspect

from hiveweave.services import fact_bus
from hiveweave.services.git_worktree import service_merge as sm


def test_publish_helper_lands_a_fact():
    fact_bus.reset_for_tests()
    sm._publish_merge_landed(
        "A227", branch="hw/A227/t-abc", target="main", hash_="deadbeef"
    )
    facts = fact_bus.recent_facts("merge_landed")
    assert facts, "merge_landed 事实没落总线"
    top = facts[-1]
    assert str(top.subject) == "A227"
    assert top.payload.get("branch") == "hw/A227/t-abc"
    assert top.payload.get("target") == "main"
    assert top.payload.get("hash") == "deadbeef"


def test_every_merged_success_return_publishes_the_fact():
    """AST 判据：成功返回点数 == 发布调用点数（3 == 3）。"""
    tree = ast.parse(inspect.getsource(sm))

    publishes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_publish_merge_landed"
    ]
    merged_true_dicts = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        and any(
            isinstance(k, ast.Constant) and k.value == "merged"
            and isinstance(v, ast.Constant) and v.value is True
            for k, v in zip(node.keys, node.values)
        )
    ]
    assert len(merged_true_dicts) == 3, (
        f"成功返回点数变了（{len(merged_true_dicts)}）—— 请同步核对本判据"
    )
    assert len(publishes) == len(merged_true_dicts), (
        f"发布调用点 {len(publishes)} != 成功返回点 {len(merged_true_dicts)}"
        " —— 有成功返回没发 merge_landed 事实（等待方会等到 TTL）"
    )
