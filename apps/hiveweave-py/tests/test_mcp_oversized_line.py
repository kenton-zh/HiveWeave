"""MCP stdio 超长单行 — readline 超过 StreamReader limit 的结构化失败。

回归场景：JSON-RPC 响应单行超过 asyncio StreamReader limit（原为默认
64KiB）时 readline 抛 ValueError，且超限会**清掉缓冲** —— stdio 流就此
错位，下一次 call 会把长行的剩余部分当成新行读成半截 JSON，毒化整条连接。

修复的回归钉：
- spawn limit=16MiB（_STDIO_STREAM_LIMIT）
- ValueError → RuntimeError（结构化文案：连接已错位、需重建），
  并终止子进程复位连接（下次 _ensure_proc 干净重启）
"""

from __future__ import annotations

from typing import Any

import pytest

from hiveweave.services.mcp import _STDIO_STREAM_LIMIT, _StdioTransport
from hiveweave.util import win_subprocess


class _FakeStdin:
    def __init__(self) -> None:
        self.written = b""

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        return None


class _PoisonedStdout:
    """readline 抛 asyncio 超限的 ValueError（缓冲已被 readline 清空）。"""

    async def readline(self) -> bytes:
        raise ValueError("Separator is not found, and chunk exceed the limit")


class _GoodStdout:
    def __init__(self, line: bytes) -> None:
        self._line = line

    async def readline(self) -> bytes:
        return self._line


class _FakeProc:
    def __init__(self, stdout: Any) -> None:
        self.stdout = stdout
        self.stdin = _FakeStdin()
        self.pid = 4242
        self.returncode: int | None = None
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 1

    def kill(self) -> None:
        self.returncode = 2

    async def wait(self) -> int | None:
        return self.returncode


_GOOD_LINE = (
    b'{"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"echo"}]}}\n'
)


class TestStdioOversizedLine:
    @pytest.mark.asyncio
    async def test_oversized_line_raises_structured_runtime_error(self):
        """超限 ValueError 不裸穿：抛 RuntimeError，文案说清流已错位、需重建。"""
        transport = _StdioTransport(command="fake-server")
        transport._proc = _FakeProc(_PoisonedStdout())

        with pytest.raises(RuntimeError) as ei:
            await transport.call("tools/list", {})

        # pytest.raises(RuntimeError) 本身已证明不是裸 ValueError；再钉文案
        msg = str(ei.value)
        assert "缓冲上限" in msg
        assert "错位" in msg
        assert "重建" in msg

    @pytest.mark.asyncio
    async def test_poisoned_connection_is_reset_for_respawn(self):
        """超限后必须复位连接：进程已终止、_proc 已清空（下次 call 重启）。"""
        transport = _StdioTransport(command="fake-server")
        poisoned = _FakeProc(_PoisonedStdout())
        transport._proc = poisoned

        with pytest.raises(RuntimeError):
            await transport.call("tools/list", {})

        assert poisoned.terminated is True
        assert transport._proc is None

    @pytest.mark.asyncio
    async def test_respawn_after_poison_reads_clean_json(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """毒化 → 重建后的下一次 call 在新流上读到完整 JSON-RPC 行，
        不再把长行剩余部分读成半截 JSON。顺带钉 spawn 的 16MiB limit。"""
        captured: dict[str, Any] = {}

        async def fake_spawn(*args: Any, **kwargs: Any) -> _FakeProc:
            captured["limit"] = kwargs.get("limit")
            return _FakeProc(_GoodStdout(_GOOD_LINE))

        monkeypatch.setattr(win_subprocess, "hidden_exec", fake_spawn)

        transport = _StdioTransport(command="fake-server")
        transport._proc = _FakeProc(_PoisonedStdout())
        with pytest.raises(RuntimeError):
            await transport.call("tools/list", {})

        # 第二次 call：_ensure_proc 重启（fake_spawn），干净读到完整行
        result = await transport.call("tools/list", {})
        assert result == {"tools": [{"name": "echo"}]}
        # spawn 带 16MiB 缓冲
        assert captured["limit"] == _STDIO_STREAM_LIMIT == 16 * 1024 * 1024
