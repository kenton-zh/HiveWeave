"""批21 修复2 回归：apply_patch 参数别名收编（真实错用形态）。

实锤（s3-clone_21，2026-10-02 server.out.log 实测分布）：
  · `'item': Extra inputs are not permitted` ×6 —— 顶层 `item` 群体形态；
  · `'newString'/'old_string': Extra inputs; 'item': Extra inputs` ×11 ——
    `patches` 数组 + 顶层散落直传键的混合形态（agent 自述「我把 newString
    放在了 patches 数组外部当顶层参数」）；
  · `'patches.N.$text': Extra inputs + 'patches.N.filePath': Field required`
    ×5 —— 数组项内 `$text` 顶替 `newString`（agent 自述「我用 `$text` 代替
    `newString`」）。
以上整批被拒。本文件锁死：这些形态收编为合法输入（语义不变），未知键仍
fail-closed 拒绝且回执附正确形状样例。
"""

from __future__ import annotations

import hiveweave.tools  # noqa: F401 — populate @tool registry

import pytest
from pydantic import ValidationError

from hiveweave.tools.patch import ApplyPatchParams, _infer_op

_P = [{"op": "update", "filePath": "a.py", "oldString": "x", "newString": "y"}]


def test_mixed_patches_with_stray_top_level_keys_accepted():
    """patches 数组 + 顶层散落 newString/item ⇒ 接受，数组为权威载荷。"""
    m = ApplyPatchParams.model_validate(
        {"patches": _P, "newString": "y", "item": _P}
    )
    assert len(m.patches) == 1
    assert m.patches[0].file_path == "a.py"
    assert m.patches[0].new_string == "y"


def test_item_group_form_unwrapped():
    """顶层 `{"item": […]}` 群体形态 ⇒ 解包成 patches（P1-6 同口径）。"""
    m = ApplyPatchParams.model_validate({"item": _P})
    assert len(m.patches) == 1
    assert m.patches[0].file_path == "a.py"
    # patches 为空数组时 item 同样接管
    m2 = ApplyPatchParams.model_validate({"patches": [], "item": _P})
    assert len(m2.patches) == 1
    # item 为单个对象（非数组）⇒ 包成单元素数组
    m3 = ApplyPatchParams.model_validate(
        {"item": {"filePath": "b.py", "content": "hi"}}
    )
    assert len(m3.patches) == 1
    assert m3.patches[0].op == "add"


def test_dollar_text_alias_absorbed_as_new_string():
    """数组项 `$text` 顶替 newString ⇒ 收编；op 推断为 update（非 add）。"""
    m = ApplyPatchParams.model_validate(
        {"patches": [{"filePath": "c.py", "oldString": "a", "$text": "b"}]}
    )
    assert m.patches[0].op == "update"
    assert m.patches[0].new_string == "b"
    assert _infer_op({"filePath": "x", "$text": "y"}) == "update"


def test_list_form_old_new_string_joined():
    """oldString/newString 传成 list（逐行文本）⇒ "\\n".join 归一。"""
    m = ApplyPatchParams.model_validate(
        {
            "patches": [
                {
                    "filePath": "d.py",
                    "oldString": ["l1", "l2"],
                    "newString": ["n1", "n2"],
                }
            ]
        }
    )
    assert m.patches[0].old_string == "l1\nl2"
    assert m.patches[0].new_string == "n1\nn2"


def test_truly_unknown_keys_still_fail_closed():
    """未知键不收编 —— extra 报错保持（fail-closed 不放宽）。"""
    with pytest.raises(ValidationError) as ei:
        ApplyPatchParams.model_validate({"patches": _P, "bogus": 1})
    assert "bogus" in str(ei.value)
    # patches 本身形状坏（dict）也保持原拒绝，不做静默丢弃
    with pytest.raises(ValidationError):
        ApplyPatchParams.model_validate({"patches": {"x": 1}})


def test_rejection_receipt_carries_prescription():
    """「Extra inputs」拒绝回执带处方：正确键名清单 + 最小可抄示例。"""
    from hiveweave.tools.base import get_tool_def

    td = get_tool_def("apply_patch")
    assert td is not None
    params, err, violations = td.validate_detailed(
        {"patches": _P, "bogus": 1}
    )
    assert params is None and err and "bogus" in err
    # 处方 = 工具声明的正确形状样例（pipeline 回执原样附带）
    example = getattr(td, "param_example")
    assert example and "patches" in example and "newString" in example


def test_direct_form_and_legacy_paths_no_regression():
    """既有合法形态零回归：直传 / 数组 / JSON 字符串 / 显式 op。"""
    m1 = ApplyPatchParams.model_validate(
        {"filePath": "e.py", "oldString": "a", "newString": "b"}
    )
    assert m1.patches[0].op == "update"
    m2 = ApplyPatchParams.model_validate({"patches": _P})
    assert m2.patches[0].op == "update"
    m3 = ApplyPatchParams.model_validate(
        {"patches": '[{"filePath": "f.md", "content": "x"}]'}
    )
    assert m3.patches[0].op == "add"
    m4 = ApplyPatchParams.model_validate(
        {"patches": [{"op": "delete", "filePath": "g.md"}]}
    )
    assert m4.patches[0].op == "delete"
