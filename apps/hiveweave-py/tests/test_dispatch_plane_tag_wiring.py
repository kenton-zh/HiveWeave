"""P1-4：dispatch 路径的交付物平面降档**接线**（2026-09-12）。

背景：`delivery_plane.py:148-149` 自己写着「调用方（create / dispatch / api
三处）必须把 tag 写进任务、把原因写进回执 —— 降档**不许静默**」。但
`tools/tasks/dispatch.py` 原先：

1. **不传 `tags`** ⇒ `resolve_and_downgrade(tags=None)` ⇒ 任务级
   `plane:<x>` 在该路径失效（只剩项目级 `project_meta.delivery_plane`）；
2. **把返回的 tag 用 `_` 丢弃** ⇒ 任务账本无 `gate_downgraded:` 留痕。

本测试用 **AST 结构断言 + 行为断言**（不是文本子串）—— 回退接线即打红。

判据来源：我们自己的 `delivery_plane.py` docstring（三处调用方契约）。
**不引用 DSH**：DSH 无交付物平面概念（无组织层/无任务账本），其做法
不构成本场景判据（fixplan §10 纪律 + MEMORY.md 强制三问）。
"""

from __future__ import annotations

import ast
from pathlib import Path

import hiveweave.services.dispatch as dispatch_mod
import hiveweave.tools.tasks.dispatch as dispatch_tool_mod
from hiveweave.tools.tasks.dispatch import DispatchTaskParams


# ── 1. 参数面：tags 必须存在且可被 LLM 传到 ──────────────────────

class TestDispatchTagsParam:
    def test_tags_field_exists(self):
        assert "tags" in DispatchTaskParams.model_fields

    def test_tags_accepts_list(self):
        p = DispatchTaskParams(target="A009", task="x", tags=["plane:cli"])
        assert p.tags == ["plane:cli"]

    def test_tags_defaults_to_none(self):
        p = DispatchTaskParams(target="A009", task="x")
        assert p.tags is None

    def test_tags_has_aliases_so_llm_can_pass_tag_singular(self):
        """alias 必须含单数 `tag` —— 与同文件其他字段同族。"""
        from hiveweave.tools.base import _extract_aliases

        aliases = _extract_aliases(DispatchTaskParams.model_fields["tags"])
        assert "tags" in aliases
        assert "tag" in aliases


# ── 2. 工具 schema：三件套的第三件（executor 的 TOOL_PARAM_SCHEMAS）──

class TestDispatchTagsInToolSchema:
    def test_executor_schema_declares_tags(self):
        """漏这条 ⇒ 模型端根本看不到该字段（被拒一次才自纠）。"""
        from hiveweave.tools.executor import TOOL_PARAM_SCHEMAS

        assert "tags" in TOOL_PARAM_SCHEMAS["dispatch_task"]["properties"]

    def test_dispatch_tool_description_mentions_plane_tag(self):
        from hiveweave.tools.executor import TOOL_PARAM_SCHEMAS

        desc = TOOL_PARAM_SCHEMAS["dispatch_task"]["properties"]["tags"].get(
            "description", ""
        )
        assert "plane:" in desc


# ── 3. 接线：AST 守卫（回退即红）───────────────────────────────

def _dispatch_tool_source() -> str:
    return Path(dispatch_tool_mod.__file__).read_text(encoding="utf-8")


def _unwrap_await(node: ast.AST) -> ast.AST:
    """剥一层 `await`。

    **必需**：`x = await f(...)` 的 RHS 是 `ast.Await`，`isinstance(v, ast.Call)`
    为 False。忘了这层 ⇒ AST 守卫恒绿（假守卫）。本项目已因此栽过。
    """
    if isinstance(node, ast.Await):
        return node.value
    return node


def _called_name(call: ast.AST) -> str | None:
    if not isinstance(call, ast.Call):
        return None
    fn = call.func
    return getattr(fn, "id", None) or getattr(fn, "attr", None)


class TestDispatchDowngradeWiring:
    def test_resolve_and_downgrade_is_called_with_tags_kwarg(self):
        """AST：`resolve_and_downgrade(...)` 必须带 `tags=` 关键字实参。

        不带 ⇒ 任务级 plane 失效（P1-4 原缺陷）。
        """
        tree = ast.parse(_dispatch_tool_source())
        found: list[int] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _called_name(node) != "resolve_and_downgrade":
                continue
            if "tags" in {kw.arg for kw in node.keywords}:
                found.append(node.lineno)
        assert found, (
            "dispatch.py 里的 resolve_and_downgrade 调用未传 tags= —— "
            "任务级 plane:<x> 会在 dispatch 路径静默失效（P1-4）"
        )

    def test_returned_tag_is_not_discarded(self):
        """AST：不得把 `resolve_and_downgrade` 的第二个返回值绑给 `_`。

        `policy_id, _plane_tag, plane_reason = ...` 里的 `_plane_tag` 曾被
        丢弃 ⇒ 账本无留痕。**注意 RHS 是 `Await` 不是 `Call`** —— 必须
        先剥一层，否则本守卫恒绿（假守卫）。
        """
        tree = ast.parse(_dispatch_tool_source())
        offenders: list[int] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            call = _unwrap_await(node.value)
            if _called_name(call) != "resolve_and_downgrade":
                continue
            if not isinstance(node.targets[0], ast.Tuple):
                continue
            elts = node.targets[0].elts
            if len(elts) < 2:
                continue
            second = elts[1]
            varname = getattr(second, "id", None)
            # 绑给裸 `_` 是明确丢弃 → 不合格
            if varname in (None, "_", "_plane_tag"):
                offenders.append(node.lineno)
        assert not offenders, (
            f"dispatch.py 行 {offenders}: 降档 tag 被丢弃（绑给 `_`/`_plane_tag`）"
            " —— 降档会静默（delivery_plane.py 明写『降档不许静默』）"
        )

    def test_dispatch_service_accepts_and_forwards_tags(self):
        """服务层必须能承接 tags（否则工具层传了也无处落）。"""
        import inspect

        sig = inspect.signature(dispatch_mod.DispatchService.dispatch_task)
        assert "tags" in sig.parameters

    def test_dispatch_service_forwards_tags_to_create_task(self):
        """AST：dispatch_task 内 create_task(...) 必须带 tags=。"""
        src = Path(dispatch_mod.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        found = False
        for node in ast.walk(tree):
            if _called_name(node) != "create_task":
                continue
            if "tags" in {kw.arg for kw in node.keywords}:
                found = True
        assert found, "dispatch_task 内 create_task 未转发 tags —— 接线又断了"


# ── 4. 行为：复用 taskId 时不追加（不改既有数据的取舍）──────────

class TestReuseDoesNotMutateExistingTask:
    def test_tag_appended_only_when_creating_new_task(self):
        """AST：追加 tag 的分支必须有 `not params.task_id` 守卫。

        这是用户 2026-09-12 拍板的取舍：复用已有任务不追加 tag（避免改
        既有数据），只保留回执提示。
        """
        tree = ast.parse(_dispatch_tool_source())
        # 找 `if plane_tag and not params.task_id:` 形态
        found = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            dump = ast.dump(node.test)
            has_plane_tag = "plane_tag" in dump
            has_task_id_guard = "task_id" in dump
            if has_plane_tag and has_task_id_guard:
                found = True
        assert found, (
            "追加降档 tag 的分支缺少 `not params.task_id` 守卫 —— "
            "复用已有任务时会被追加 tag（改既有数据）"
        )
