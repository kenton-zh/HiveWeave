"""写路径闸回归（46/11 #6 isConcurrencySafe 落地，2026-09-08）。

面：同路径并发写 advisory 冲突 / 不同路径互不干扰 / 异常路径释放持有 /
apply_patch 多文件全有或全无 / 谓词默认安全性。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hiveweave.tools import write_gate
from hiveweave.tools.file import WriteFileParams, write_file_tool
from hiveweave.tools.patch import (
    ApplyPatchParams,
    EditFileParams,
    apply_patch_tool,
    edit_file_tool,
)
from hiveweave.tools.result import ToolResult


@pytest.fixture(autouse=True)
def _clean_gate():
    write_gate.reset_for_tests()
    yield
    write_gate.reset_for_tests()


def _ok(result: ToolResult) -> bool:
    return bool(result.success)


class TestWriteGate:
    async def test_same_path_second_writer_gets_advisory_conflict(self, tmp_path: Path):
        ws = str(tmp_path)
        fp = "shared.txt"
        assert write_gate.try_acquire(fp, ws) is True
        result = await write_file_tool(
            WriteFileParams(file_path=fp, content="x"), "agent-1", ws
        )
        assert not _ok(result)
        assert "write conflict" in (result.error or "")
        assert "[write_gate/concurrency]" in (result.error or "")
        write_gate.release(fp, ws)

    async def test_after_release_write_succeeds(self, tmp_path: Path):
        ws = str(tmp_path)
        write_gate.try_acquire("a.txt", ws)
        write_gate.release("a.txt", ws)
        result = await write_file_tool(
            WriteFileParams(file_path="a.txt", content="hello"), "agent-1", ws
        )
        assert _ok(result)
        assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "hello"

    async def test_tool_release_held_on_write_failure(self, tmp_path: Path, monkeypatch):
        """write 抛异常 → finally 必须释放持有（防泄漏占坑）。"""
        ws = str(tmp_path)

        async def _boom(**kwargs):
            raise RuntimeError("disk explodes")

        monkeypatch.setattr("hiveweave.tools.file.write_file", _boom)
        with pytest.raises(RuntimeError):
            await write_file_tool(
                WriteFileParams(file_path="boom.txt", content="x"), "agent-1", ws
            )
        assert write_gate.inflight_paths() == []

    async def test_edit_file_conflict_and_success(self, tmp_path: Path):
        ws = str(tmp_path)
        (tmp_path / "code.py").write_text("value = 1\n", encoding="utf-8")
        write_gate.try_acquire("code.py", ws)
        result = await edit_file_tool(
            EditFileParams(file_path="code.py", old_string="1", new_string="2"),
            "agent-1",
            ws,
        )
        assert "write conflict" in (result.error or "")
        write_gate.release("code.py", ws)
        result = await edit_file_tool(
            EditFileParams(file_path="code.py", old_string="1", new_string="2"),
            "agent-1",
            ws,
        )
        assert _ok(result)
        assert (tmp_path / "code.py").read_text(encoding="utf-8") == "value = 2\n"

    async def test_apply_patch_all_or_nothing_acquisition(self, tmp_path: Path):
        """多文件 patch：第二个路径被占 → advisory 冲突且第一个路径不残留持有。"""
        ws = str(tmp_path)
        (tmp_path / "one.txt").write_text("1\n", encoding="utf-8")
        (tmp_path / "two.txt").write_text("2\n", encoding="utf-8")
        write_gate.try_acquire("two.txt", ws)

        patches = ApplyPatchParams(
            patches=[
                {"op": "update", "filePath": "one.txt", "oldString": "1", "newString": "x"},
                {"op": "update", "filePath": "two.txt", "oldString": "2", "newString": "y"},
            ]
        )
        result = await apply_patch_tool(patches, "agent-1", ws)
        assert "write conflict" in (result.error or "")
        # 全有或全无：one.txt 的持有必须已释放（仅剩测试预持有的 two.txt）
        inflight = write_gate.inflight_paths()
        assert len(inflight) == 1 and inflight[0].endswith("two.txt")
        write_gate.release("two.txt", ws)

        # 放行后重试成功
        result = await apply_patch_tool(patches, "agent-1", ws)
        assert _ok(result)

    async def test_predicate_defaults_and_write_tools(self, tmp_path: Path):
        ws = str(tmp_path)
        # 非写类工具恒安全
        assert write_gate.is_concurrency_safe("read_file", None, ws) is True
        assert write_gate.is_concurrency_safe("bash", "ignored", ws) is True
        # 写类工具：空闲路径安全，在飞路径不安全
        assert write_gate.is_concurrency_safe("write_file", "p.txt", ws) is True
        write_gate.try_acquire("p.txt", ws)
        assert write_gate.is_concurrency_safe("write_file", "p.txt", ws) is False
        # Windows 大小写不敏感：normcase 归一后同一文件
        assert write_gate.is_concurrency_safe("write_file", "P.TXT", ws) is False
        write_gate.release("p.txt", ws)
