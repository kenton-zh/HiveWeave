"""H6①＋②：参数文案恒等式 ＋ `invalidArgs` 未落 durable 可见面。

平台 issue H6（"#10①②"）两件事同一病灶：**模型可见的契约 ≠ 代码强制的契约**。

- ① `apply_patch.patches.items` 的手写契约（`tools/executor.py` 的
  `TOOL_PARAM_SCHEMAS`）**没有任何 `required`**，而代码侧
  （`PatchItem.file_path`，无默认值）是必填 ⇒ 模型看到的 items 形状比实际
  强制面更宽松。修法：如实补 `required: ["filePath"]`，**刻意不含 `op`**
  （`op` 由 `tools/patch.py::_infer_op` 推断，真的可选）。
  同居 precedent：`attest_doc_review.files` 的 `"required": ["path"]`。

- ② `pipeline` 的 per-item 结构化违规只进了**进程内**的 `invalidArgs`；真正
  落库 / 进模型可见面的 `error` 文本里只有 pydantic 的原生单句（**点号**形态
  `patches.0.filePath`），模型看不出**哪一条**数组项错。修法：`error` 追加
  逐项**方括号**路径 + message（`patches[0].filePath`）。

判据纪律（仓规：**状态/结构，绝不 grep 源码文本**）：
- 断言读的是真实 `TOOL_PARAM_SCHEMAS` 结构、真实校验路径（`validate_detailed`）
  的产出、真实 `execute_registered_tool` 运行出的 `error` 文本与 `invalidArgs`；
- 不做任何「源码里有没有某字符串」的检查。

正向对照（末尾 `test_positive_control_*`）：把新增 helper 中和为旧行为
（返回 `""`）后，「error 含方括号路径」这条断言**会转红** —— 这正是它作为
真守卫、而非恒真式的证明（pydantic 原生只给点号 `patches.0.filePath`，
方括号形态只可能来自新 helper）。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from hiveweave.tools import pipeline
from hiveweave.tools.base import get_tool_def
from hiveweave.tools.pipeline import execute_registered_tool

TOOL = "apply_patch"


class _Allow:
    async def evaluate_detailed(self, agent_id, tool_name, args):
        return ("allow", None)


async def _call(args: dict) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        ws = str(Path(tmp) / "ws")
        Path(ws).mkdir()
        return await execute_registered_tool(
            TOOL, args, "agent-h6", ws, _Allow(), None
        )


# ── ① 契约结构：items.required 恰好只有 filePath，且不含 op ─────────────


def test_patches_items_required_is_exactly_filepath():
    """手写契约（模型可见面）与 pydantic 强制面对齐：items 必填 filePath。"""
    from hiveweave.tools.executor import TOOL_PARAM_SCHEMAS

    items = TOOL_PARAM_SCHEMAS[TOOL]["properties"]["patches"]["items"]
    assert items["required"] == ["filePath"]
    # op 仍由 _infer_op 推断，绝非必填（引 op 推断的既有测试不得被破坏）
    assert "op" not in items["required"]


def test_missing_filepath_rejected_by_real_validation_path():
    """真实校验路径：缺 filePath 的数组项在门内被拒，并点名方括号路径。"""
    td = get_tool_def(TOOL)
    assert td is not None, "apply_patch 未注册"

    params, err, violations = td.validate_detailed({"patches": [{"content": "x"}]})
    assert params is None
    assert err is not None
    assert violations == [
        {"path": "patches[0].filePath", "message": "Field required", "type": "missing"}
    ]


def test_op_less_item_still_validates_via_real_path():
    """正向对照：缺 op 但带 oldString/newString 的项仍走通推断（不回归）。"""
    td = get_tool_def(TOOL)

    params, err, violations = td.validate_detailed(
        {"patches": [{"filePath": "a.py", "oldString": "x", "newString": "y"}]}
    )
    assert err is None, err
    assert violations == []
    assert params is not None
    assert params.patches[0].op == "update"  # 推断发生在校验期


# ── ② 模型可见面：error 文本必须点名第几条 ──────────────────────────────


@pytest.mark.asyncio
async def test_error_text_names_offending_item_bracket_path():
    """⭐ 验收：`error` 含逐项方括号路径 + message；结构化回执不变。"""
    res = await _call({"patches": [{"content": "x"}]})
    assert res["success"] is False
    # 新增的逐项清单（方括号形态）—— 只有新 helper 能产生它
    assert "patches[0].filePath" in res["error"]
    assert "Field required" in res["error"]
    # 进程内结构化回执照旧（part ② 未改 invalidArgs 的形状）
    assert res["invalidArgs"] == [
        {"path": "patches[0].filePath", "message": "Field required", "type": "missing"}
    ]


# ── ② 正向对照：中和 helper ⇒ 「含方括号路径」断言转红 ───────────────────


@pytest.mark.asyncio
async def test_positive_control_neutralising_helper_removes_bracket_path(monkeypatch):
    """证明上面那条断言是真守卫，而非恒真：

    · pydantic 原生文案是**点号**形态 `patches.0.filePath`；
    · **方括号**形态 `patches[0].filePath` 只可能来自新 helper。
    把 helper 换回旧行为（返回 `''`）后 —— 即中和掉 part ② ——
    `test_error_text_names_offending_item_bracket_path` 的
    `"patches[0].filePath" in res["error"]` 断言**转红**。
    """
    monkeypatch.setattr(pipeline, "format_invalid_args_detail", lambda violations: "")

    res = await _call({"patches": [{"content": "x"}]})
    assert res["success"] is False

    # 关键：方括号路径消失 ⇒ 主断言在无 helper 时必然失败（守卫成立）
    assert "patches[0].filePath" not in res["error"]
    # 反向确认 error 本身非空、只是少了新清单（不是"整段报错消失"骗过断言）
    assert "patches.0.filePath" in res["error"]
    assert "Field required" in res["error"]
    # invalidArgs 不受 helper 影响（仍为结构化方括号形态）
    assert res["invalidArgs"][0]["path"] == "patches[0].filePath"
