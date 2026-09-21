"""P2-1 参数校验：`files` 的形状错误必须在**门内**被点名，而不是漏到服务层。

病灶：`attest_doc_review` 的 `files: list[Any]` —— ``Any`` 让 pydantic **永不
报错**，`{}` / `path=""` 一路漏到 `create_doc_review` 才炸成
`Unsafe or empty path: ''`（不说哪条、不说字段名）；`path=123` 更糟：`.strip()`
AttributeError 被兜成 `'int' object has no attribute 'strip'`。

修法：`files: list[DocReviewFile]`（`path: str` 必填）+ before-validator 保住
历史宽容写法（dict→[dict]、裸字符串、`{file: …}` 别名）+ 手写 schema 补 `items`
形状 + pipeline 回执带结构化 `invalidArgs`（方括号形态）。

本文件的判据**全部是状态/结构化字段**（`error` 里的 pydantic loc + `invalidArgs`
清单），不做「整句文案」断言。
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from hiveweave.tools.base import get_tool_def, loc_to_bracket_path
from hiveweave.tools.pipeline import execute_registered_tool

TOOL = "attest_doc_review"


class _Allow:
    async def evaluate_detailed(self, agent_id, tool_name, args):
        return ("allow", None)


async def _call(args: dict) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        ws = str(Path(tmp) / "ws")
        Path(ws).mkdir()
        return await execute_registered_tool(TOOL, args, "agent-p21", ws, _Allow(), None)


def _validate(args: dict):
    td = get_tool_def(TOOL)
    assert td is not None, "工具未注册"
    return td.validate(args)


# ── ① 原形状（缺 path）⇒ 门内点名 files[0].path ────────────────


@pytest.mark.asyncio
async def test_missing_path_names_the_entry():
    """⭐ 验收：`error` 含 `files.0.path`，且 `invalidArgs[0].path == 'files[0].path'`。"""
    res = await _call({"files": [{}]})
    assert res["success"] is False
    assert "files.0.path" in res["error"]
    assert res["invalidArgs"] == [
        {"path": "files[0].path", "message": "Field required", "type": "missing"}
    ]


@pytest.mark.asyncio
async def test_missing_files_field_names_files():
    res = await _call({})
    assert res["success"] is False
    assert res["invalidArgs"][0]["path"] == "files"
    assert res["invalidArgs"][0]["type"] == "missing"


@pytest.mark.asyncio
async def test_empty_files_list_rejected_in_gate():
    """空列表也在门内被挡（`min_length=1`），不再落到工具体兜底。"""
    res = await _call({"files": []})
    assert res["success"] is False
    assert res["invalidArgs"][0]["path"] == "files"
    assert "at least 1 item" in res["error"] or "shorter" in res["error"]


@pytest.mark.asyncio
async def test_int_path_is_rejected_with_loc_not_attributeerror():
    """`path=123` 不再是 `'int' object has no attribute 'strip'`。"""
    res = await _call({"files": [{"path": 123}]})
    assert res["success"] is False
    assert res["invalidArgs"][0]["path"] == "files[0].path"
    assert "strip" not in res["error"]
    assert "'files.0.path'" in res["error"]


@pytest.mark.asyncio
async def test_empty_path_rejected_in_gate():
    """空字符串 path 在门内就被挡（不再漏到服务层）。"""
    res = await _call({"files": [{"path": ""}]})
    assert res["success"] is False
    assert res["invalidArgs"][0]["path"] == "files[0].path"


# ── ② 历史宽容写法不能被收窄（形状收窄 ≠ 输入面收窄）──────────


@pytest.mark.parametrize(
    "files",
    [
        {"path": "x.md"},                 # 单个 dict（工具体内原本就包装）
        "x.md",                            # 单个字符串
        ["x.md"],                          # 字符串数组
        [{"file": "x.md"}],                # 服务层旧别名 file
        [{"path": "x.md", "minLines": 3}],
        [{"path": "x.md", "min_lines": 3}],
    ],
)
def test_legacy_shapes_still_validate(files):
    params, err = _validate({"files": files})
    assert err is None, err
    assert params is not None
    assert params.files[0].path == "x.md"


def test_min_lines_roundtrip_to_service_key():
    """模型 → 服务层的键名必须是 `min_lines`（服务只认这个）。"""
    params, err = _validate({"files": [{"path": "x.md", "minLines": 7}]})
    assert err is None, err
    assert params.files[0].model_dump() == {"path": "x.md", "min_lines": 7}


# ── ③ 服务层兜底（直调）也必须点名第几条 ─────────────────────


@pytest.mark.asyncio
async def test_service_layer_fallback_names_index(tmp_path):
    """直调服务层时，四条报错都点名第几条（`files[i].path`）。"""
    from hiveweave.services.attestation import create_doc_review

    with pytest.raises(ValueError) as ei:
        await create_doc_review(
            "p", agent_id="a", task_id=None,
            files=[{"path": "  "}, {"path": "ok.md"}],
            workspace=str(tmp_path), commit_hash=None,
        )
    assert "files[0].path" in str(ei.value), str(ei.value)


@pytest.mark.asyncio
async def test_service_layer_missing_file_names_index(tmp_path):
    from hiveweave.services.attestation import create_doc_review

    with pytest.raises(ValueError) as ei:
        await create_doc_review(
            "p", agent_id="a", task_id=None,
            files=[{"path": "nope.md"}], workspace=str(tmp_path), commit_hash=None,
        )
    assert "files[0].path" in str(ei.value), str(ei.value)


@pytest.mark.asyncio
async def test_service_layer_non_dict_entry_names_index(tmp_path):
    from hiveweave.services.attestation import create_doc_review

    with pytest.raises(ValueError) as ei:
        await create_doc_review(
            "p", agent_id="a", task_id=None,
            files=["not-a-dict"], workspace=str(tmp_path), commit_hash=None,
        )
    assert "files[0]" in str(ei.value)


# ── ④ loc → 方括号路径的纯函数（结构化回执的形状源）────────────


@pytest.mark.parametrize(
    "loc,expected",
    [
        (("files", 0, "path"), "files[0].path"),
        (("files",), "files"),
        (("files", 10), "files[10]"),
        (("a", 1, "b", 2, "c"), "a[1].b[2].c"),
        ((), "<root>"),
    ],
)
def test_loc_to_bracket_path(loc, expected):
    assert loc_to_bracket_path(loc) == expected


# ── ⑤ 手写 schema 与模型一致（两件套别漂移）──────────────────


def test_handwritten_schema_has_file_item_shape():
    from hiveweave.tools.executor import TOOL_PARAM_SCHEMAS

    files = TOOL_PARAM_SCHEMAS[TOOL]["properties"]["files"]
    items = files["items"]
    assert items["required"] == ["path"]
    assert items["properties"]["path"]["type"] == "string"
    assert items["properties"]["minLines"]["type"] == "integer"


def test_pipeline_validation_is_synchronous_path():
    """确认 `validate_detailed` 不改变 `validate` 的既有二元组契约。"""
    td = get_tool_def(TOOL)
    params, err = td.validate({"files": [{"path": "a.md"}]})
    assert err is None and params is not None
    assert asyncio.iscoroutine(params) is False
