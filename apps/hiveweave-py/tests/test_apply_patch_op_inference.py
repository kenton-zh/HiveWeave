"""apply_patch：模型可见契约 × 代码强制契约必须一致（report TEST_DSH_54 #11）。

现象（v1 稿归因已被推翻，此处按核码后的真根因写）：报错永远长一个样 ——
`'patches.<N>.op': Field required (Array of patch operations.)`，4 个 Agent
各自独立撞上共 13 次。

真根因不是"提示词示例不一致"（prompts 里根本没有 apply_patch 示例），而是
**平台自己手写的、发给模型的 schema 表**与 **pydantic 模型**之间的落差：

- `tools/executor.py` 的 TOOL_PARAM_SCHEMAS["apply_patch"] 顶层 description
  原文鼓励双形态（patches[] 或顶层直传），items 也没有 required —— 模型看到的
  契约里数组项的 `op` 是可选的；
- `tools/patch.py` 的 `PatchItem.op` 无默认值 = pydantic 必填；
- 而 op 推断只覆盖**顶层直传**形态（`_normalize_direct_params`），不救数组项。

⇒ 模型照可见契约传，必然被拒。修法选"让 op 真的可选"（与模型从 edit_file
学来的自然分布一致），而不是把 schema 标成 required —— 那样只是把不一致
换个方向藏起来。

判据来源：我们自己的工具契约（executor schema 表 + PatchItem 模型）本身。
不引用 DSH：DSH 无本工具面。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hiveweave.tools.patch import ApplyPatchParams, PatchItem


# ── 1. 契约面：数组项缺 op 必须能被接受（这是那 13 次的确切形态）──────────

class TestArrayItemOpInference:
    def test_update_inferred_from_old_string(self):
        p = ApplyPatchParams(
            patches=[
                {"filePath": "a.py", "oldString": "x", "newString": "y"},
            ]
        )
        assert p.patches[0].op == "update"

    def test_update_inferred_from_snake_case_alias(self):
        p = ApplyPatchParams(
            patches=[{"filePath": "a.py", "old_string": "x", "new_string": "y"}]
        )
        assert p.patches[0].op == "update"

    def test_add_inferred_from_content(self):
        p = ApplyPatchParams(patches=[{"filePath": "b.md", "content": "# hi"}])
        assert p.patches[0].op == "add"

    def test_no_op_no_content_defaults_to_add(self):
        p = ApplyPatchParams(patches=[{"filePath": "c.md"}])
        assert p.patches[0].op == "add"

    def test_explicit_op_is_never_overridden(self):
        """显式 op='delete' 不得被推断覆盖（delete 无法从字段推断）。"""
        p = ApplyPatchParams(patches=[{"op": "delete", "filePath": "d.md"}])
        assert p.patches[0].op == "delete"

    def test_json_string_form_also_infers(self):
        p = ApplyPatchParams(
            patches='[{"filePath": "e.md", "content": "x"}]'
        )
        assert p.patches[0].op == "add"

    def test_mixed_items_infer_independently(self):
        p = ApplyPatchParams(
            patches=[
                {"filePath": "f.md", "content": "x"},
                {"filePath": "g.py", "oldString": "a", "newString": "b"},
            ]
        )
        assert [i.op for i in p.patches] == ["add", "update"]

    def test_direct_form_still_works(self):
        """顶层直传形态（原有能力）不得回归。"""
        p = ApplyPatchParams(filePath="h.py", oldString="a", newString="b")
        assert len(p.patches) == 1
        assert p.patches[0].op == "update"


# ── 2. 契约一致性：LLM 可见 schema 不得宣称代码不强制的东西 ─────────────

class TestSchemaCodeParity:
    def test_llm_schema_does_not_claim_op_required(self):
        """若 schema 把 op 标成必填，而代码能推断 ⇒ 又是新的不一致。"""
        from hiveweave.tools.executor import TOOL_PARAM_SCHEMAS

        items = TOOL_PARAM_SCHEMAS["apply_patch"]["properties"]["patches"]["items"]
        assert not items.get("required"), (
            "apply_patch 数组项的 op 已由 _infer_op 推断（真的可选），"
            "schema 不得再宣称 required —— 契约两面必须一致"
        )

    def test_llm_schema_documents_inference(self):
        """模型可见面必须说明 op 可省 + 怎么推断（否则模型只能靠撞）。"""
        from hiveweave.tools.executor import TOOL_PARAM_SCHEMAS

        desc = TOOL_PARAM_SCHEMAS["apply_patch"]["description"]
        op_desc = TOOL_PARAM_SCHEMAS["apply_patch"]["properties"]["patches"][
            "items"
        ]["properties"]["op"]["description"]
        assert "infer" in (desc + op_desc).lower()

    def test_patch_item_model_still_declares_op(self):
        """字段仍在（不删字段），只是由校验器补值。"""
        assert "op" in PatchItem.model_fields


# ── 3. 行为：真跑一次（模型的真实调用形态）────────────────────────────

class TestRealInvocation:
    @pytest.mark.asyncio
    async def test_apply_patch_with_op_less_item_really_writes(self, tmp_path: Path):
        """端到端：数组项不带 op，必须真的把文件写出来。"""
        from hiveweave.tools.patch import apply_patch

        params = ApplyPatchParams(
            patches=[{"filePath": "note.md", "content": "# Hello"}]
        )
        assert params.patches[0].op == "add"  # 推断发生在校验期

        result = await apply_patch(
            patches=[p.model_dump(by_alias=True) for p in params.patches],
            workspace_path=str(tmp_path),
        )
        assert result["success"] is True, result.get("error")
        assert (tmp_path / "note.md").read_text(encoding="utf-8") == "# Hello"

    @pytest.mark.asyncio
    async def test_raw_input_array_items_infer_op(self, tmp_path: Path):
        """legacy raw_input 形态同样要推断（唯一漏斗 `_infer_op`）。"""
        from hiveweave.tools.patch import _normalize_patches, apply_patch

        (tmp_path / "seed.md").write_text("old text", encoding="utf-8")
        normalized = _normalize_patches(
            {"patches": [{"filePath": "seed.md", "oldString": "old", "newString": "new"}]}
        )
        assert normalized[0]["op"] == "update"

        result = await apply_patch(
            patches=None,
            workspace_path=str(tmp_path),
            raw_input={
                "patches": [
                    {"filePath": "seed.md", "oldString": "old", "newString": "new"}
                ]
            },
        )
        assert result["success"] is True, result.get("error")
        assert (tmp_path / "seed.md").read_text(encoding="utf-8") == "new text"

    @pytest.mark.asyncio
    async def test_delete_still_requires_explicit_op(self, tmp_path: Path):
        """反向对照：推断不能把 delete 也"兜"成 add —— 必须显式传。"""
        from hiveweave.tools.patch import apply_patch

        (tmp_path / "gone.md").write_text("x", encoding="utf-8")
        # 未传 op、只给 filePath ⇒ 推断为 add（文件已存在 ⇒ 失败），
        # 绝不会被误当作 delete。
        result = await apply_patch(
            patches=[{"filePath": "gone.md"}], workspace_path=str(tmp_path)
        )
        assert result["success"] is False
        assert (tmp_path / "gone.md").exists(), "推断不得导致文件被删"
