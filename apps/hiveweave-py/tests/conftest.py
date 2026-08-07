"""Pytest 全局夹具。

每个测试结束后关闭该测试期间打开的 aiosqlite 连接（meta DB 单例 +
per-project 连接缓存）。aiosqlite 的连接 worker 线程是**非守护线程**，
不关闭时线程会一直阻塞在队列读取上，导致 pytest 全量单进程跑完汇总后
无法退出（exit hang）。生产进程里这些连接本就该常驻，无需改动 db 层。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
async def _close_db_connections_after_test():
    yield
    try:
        from hiveweave.db.project import close_all

        await close_all()
    except Exception:
        pass
    try:
        from hiveweave.db.meta import close_meta_db

        await close_meta_db()
    except Exception:
        pass


class _FakeStdin:
    """Records bytes written; drain/close are no-ops."""

    def __init__(self) -> None:
        self.written = b""

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


class _FakeStream:
    """Chunked stdout/stderr; ``hang=True`` simulates a daemon that never
    closes its inherited pipe handles (EOF never arrives)."""

    def __init__(self, chunks: list[bytes] | None = None, hang: bool = False) -> None:
        self._chunks = list(chunks or [])
        self._hang = hang

    async def read(self, n: int) -> bytes:
        if self._hang:
            await asyncio.sleep(100)
        return self._chunks.pop(0) if self._chunks else b""


class _FakeProc:
    def __init__(
        self,
        returncode: int = 0,
        out: bytes = b"",
        err: bytes = b"",
        hang_pipes: bool = False,
        wait_sleep: float = 0,
    ) -> None:
        self.returncode = returncode
        self.stdout = _FakeStream([out] if out else [], hang=hang_pipes)
        self.stderr = _FakeStream([err] if err else [], hang=hang_pipes)
        self.stdin = _FakeStdin()
        self._wait_sleep = wait_sleep

    async def wait(self) -> int:
        if self._wait_sleep:
            await asyncio.sleep(self._wait_sleep)
        return self.returncode

    def kill(self) -> None:
        return None


@pytest.fixture
def browse_fake_proc():
    """Fake agent-browser child process wired with the three patches every
    browse subprocess test needs (binary resolution, Windows startupinfo,
    create_subprocess_exec). Configure via the returned context — assignments
    are read live at spawn time, so set them before OR inside the with block:

        with browse_fake_proc as ctx:
            ctx.out = b"ok"
            ...  # ctx.stdin_is_pipe / ctx.stdin_written expose the wiring
    """
    state: dict = {}

    async def fake_exec(*_a, **_k):
        state["kwargs"] = _k
        # Read the ctx's CURRENT attributes at spawn time — assignments made
        # inside the `with` block (or before it) both take effect.
        p = _FakeProc(
            returncode=ctx.returncode,
            out=ctx.out,
            err=ctx.err,
            hang_pipes=ctx.hang_pipes,
            wait_sleep=ctx.wait_sleep,
        )
        state["proc"] = p
        return p

    class Ctx:
        returncode = 0
        out: bytes = b""
        err: bytes = b""
        hang_pipes = False
        wait_sleep = 0.0

        @property
        def stdin_arg(self):
            return state.get("kwargs", {}).get("stdin")

        @property
        def stdin_is_pipe(self):
            return self.stdin_arg is asyncio.subprocess.PIPE

        @property
        def spawn_env(self):
            return state.get("kwargs", {}).get("env")

        @property
        def stdin_written(self) -> bytes:
            proc = state.get("proc")
            return proc.stdin.written if proc is not None else b""

        def __enter__(self):
            self._patches = [
                patch(
                    "hiveweave.tools.browse_tools.resolve_browse_bin",
                    return_value=Path("fake-ab.exe"),
                ),
                patch(
                    "hiveweave.util.win_subprocess.windows_no_window_kwargs",
                    return_value={},
                ),
                patch("asyncio.create_subprocess_exec", new=fake_exec),
            ]
            for m in self._patches:
                m.start()
            return self

        def __exit__(self, *_exc):
            for m in reversed(self._patches):
                m.stop()
            return False

    ctx = Ctx()
    return ctx
