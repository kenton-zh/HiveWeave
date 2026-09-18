"""grep 超长单行 — rg 路径缓冲上限、源头限长与 Python 兜底。

回归场景：>64KiB 单行文件（压缩/混淆产物常态）曾让 asyncio StreamReader
默认 64KiB limit 上的 readline 抛 ValueError('Separator is not found, and
chunk exceed the limit')穿出整个 rg 路径，把本可工作的 _scan_python 兜底
也一并跳过（execute_grep 直接 error 返回）。

修复三件套的回归钉：
- rg spawn limit=1MiB（_RG_STREAM_LIMIT）
- rg argv 源头限长 --max-columns=2000 --max-columns-preview（只截显示，
  不减少匹配数）
- 读循环 ValueError → 记 grep_rg_line_limit_exceeded → 降级 _scan_python
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from hiveweave.tools import grep as grep_mod
from hiveweave.tools.grep import MAX_CHARS_PER_LINE, execute_grep
from hiveweave.util import win_subprocess

# 三个用例都跑真实 rg（经 hidden_exec spawn）
pytestmark = pytest.mark.skipif(
    shutil.which("rg") is None, reason="ripgrep not on PATH"
)


class _LogRecorder:
    """structlog logger 替身：按 (event, kwargs) 记录调用，供断言降级事件。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def info(self, event: str, **kw: Any) -> None:
        self.events.append((event, kw))

    def warning(self, event: str, **kw: Any) -> None:
        self.events.append((event, kw))

    def error(self, event: str, **kw: Any) -> None:
        self.events.append((event, kw))


def _make_big_line_file(ws: Path, token: str = "NEEDLE_TOKEN") -> Path:
    """>64KiB 单行文件：token 在第 2 行行首（落在 500 字符截断窗口内）。"""
    f = ws / "big.txt"
    big_line = token + " " + "x" * 70000
    f.write_text("header\n" + big_line + "\ntrailer\n", encoding="utf-8")
    return f


def _install_limit_exec(
    monkeypatch: pytest.MonkeyPatch, forced_limit: int, captured: dict[str, Any]
) -> None:
    """把 rg 子进程的 StreamReader limit 强制压到 forced_limit。

    模拟「缓冲还是不够」：修复后 spawn 显式带 limit=1MiB，这里在
    hidden_exec 漏斗处覆写，让 readline 对超限行抛 ValueError。
    """

    orig_exec = win_subprocess.hidden_exec

    async def forced_limit_exec(*args: Any, **kwargs: Any) -> Any:
        captured["argv"] = list(args)
        captured["limit"] = kwargs.get("limit")
        kwargs["limit"] = forced_limit
        return await orig_exec(*args, **kwargs)

    monkeypatch.setattr(win_subprocess, "hidden_exec", forced_limit_exec)


class TestGrepLargeLineRgPath:
    @pytest.mark.asyncio
    async def test_over_64kib_single_line_match_succeeds(self, tmp_path: Path):
        """真实 rg 路径：>64KiB 单行（默认 64KiB limit 必炸）应正常命中。

        修复后 limit=1MiB + --max-columns=2000 双保险：输出行源头截到
        ~2K 列，匹配数不受影响（token 嵌在 70000 列处仍命中）。
        """
        ws = tmp_path / "ws"
        ws.mkdir()
        _make_big_line_file(ws)
        result = await execute_grep(
            pattern="NEEDLE_TOKEN", path="", include=None,
            workspace_path=str(ws),
        )
        assert result["success"] is True
        out = result["output"]
        assert "big.txt" in out
        assert "  2:" in out            # 行号正确（大行是第 2 行）
        assert "NEEDLE_TOKEN" in out     # 行首 token 在截断窗口内可见


class TestGrepFallsBackWhenBufferTooSmall:
    @pytest.mark.asyncio
    async def test_valueerror_falls_back_to_python_scan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """缓冲不够（limit=1024 < 截断后 ~2K 的输出行）→ ValueError 不穿出，
        记降级事件后回退 _scan_python，结果仍然 success。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        _make_big_line_file(ws)

        recorder = _LogRecorder()
        monkeypatch.setattr(grep_mod, "log", recorder)
        _install_limit_exec(monkeypatch, 1024, {})

        result = await execute_grep(
            pattern="NEEDLE_TOKEN", path="", include=None,
            workspace_path=str(ws),
        )
        assert result["success"] is True
        assert "  2:" in result["output"]
        assert "NEEDLE_TOKEN" in result["output"]
        # 降级事件可 grep（凭证），确认结果确实来自 _scan_python 兜底
        assert any(
            ev == "grep_rg_line_limit_exceeded"
            and kw.get("falling_back") == "python_scan"
            for ev, kw in recorder.events
        )


class TestGrepMaxColumns:
    @pytest.mark.asyncio
    async def test_max_columns_caps_output_at_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """--max-columns 在源头把输出行截到 ~2K：注入 limit=4096（大于截断
        后行宽、远小于 70KiB 原始行）——若源头没限长会立刻 ValueError 降级；
        限长生效则 rg 路径全程无降级事件地成功。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        _make_big_line_file(ws)

        recorder = _LogRecorder()
        monkeypatch.setattr(grep_mod, "log", recorder)
        captured: dict[str, Any] = {}
        _install_limit_exec(monkeypatch, 4096, captured)

        result = await execute_grep(
            pattern="NEEDLE_TOKEN", path="", include=None,
            workspace_path=str(ws),
        )
        assert result["success"] is True
        # argv 源头限长参数在位
        assert "--max-columns=2000" in captured["argv"]
        assert "--max-columns-preview" in captured["argv"]
        # spawn 显式带了 1MiB 缓冲（asyncio 默认 64KiB）
        assert captured["limit"] == 1 * 1024 * 1024
        # 全程未走兜底（输出行 ~2K < 4096，未触发 ValueError）
        assert not any(
            ev == "grep_rg_line_limit_exceeded" for ev, _ in recorder.events
        )
        # 单行内容仍被 MAX_CHARS_PER_LINE 截断
        for ln in result["output"].splitlines():
            if ln.startswith("  2:"):
                assert len(ln) - len("  2: ") <= MAX_CHARS_PER_LINE
