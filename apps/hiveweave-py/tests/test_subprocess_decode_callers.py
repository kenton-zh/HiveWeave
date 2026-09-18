"""P6-a/P6-b：decode_subprocess_output 的**调用方**直接单测。

`tests/test_subprocess_decode.py` 钉的是漏斗本身（勿改）；这里钉**调用方**
真的换上了漏斗 —— GBK 字节经新路径解出可读中文（TEST_DSH_62 P6 现场：
PowerShell/cmd 在中文 Windows 写 GBK 报错，utf-8/replace 直解 4/4 错字；
旧 `locale.getpreferredencoding` 回退在 PYTHONUTF8=1 下是 no-op）。
"""
from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

import pytest

from hiveweave.tools.bash import _decode_output

_GBK_TEXT = "表达式缺少有效的操作数"
_GBK_BYTES = _GBK_TEXT.encode("gbk")


# ── P6-b：bash._decode_output（strict utf-8 → mbcs → replace）──


class TestDecodeOutput:
    def test_ascii_and_utf8_passthrough(self):
        assert _decode_output(b"error: not found") == "error: not found"
        assert _decode_output("命令正常".encode("utf-8")) == "命令正常"

    def test_empty(self):
        assert _decode_output(b"") == ""

    def test_delegates_to_subprocess_decode(self):
        """源级钉住：回退必须走统一漏斗，而不是自带一份 locale 回退。
        （只查 docstring 之后的函数体 —— docstring 记录的恰是旧病根。）"""
        body = inspect.getsource(_decode_output).split('"""')[-1]
        assert "decode_subprocess_output" in body, body
        assert "locale" not in body, (
            "又回到了 locale 回退 —— PYTHONUTF8=1 下恒 utf-8，等于没回退"
        )

    @pytest.mark.skipif(sys.platform != "win32", reason="mbcs 仅 Windows")
    def test_gbk_recovered(self):
        """★ GBK 报错字节必须解出可读中文（旧 locale 回退在 UTF-8 模式下
        会全毁成 U+FFFD）。"""
        assert _decode_output(_GBK_BYTES) == _GBK_TEXT

    @pytest.mark.skipif(sys.platform != "win32", reason="mbcs 仅 Windows")
    def test_gbk_mixed_ascii_recovered(self):
        raw = "ParserError\r\n无法将术语识别为函数".encode("gbk")
        assert _decode_output(raw) == "ParserError\r\n无法将术语识别为函数"


# ── P6-a：acl_sandbox/spawn.py 两个解码位 ─────────────────────


class TestSpawnDecode:
    @pytest.fixture
    def job(self):
        """直接构造 LongRunningJob（不 spawn）：只测 output/error_output 的
        解码路径，不碰 win32 句柄。"""
        from hiveweave.services.acl_sandbox.spawn import LongRunningJob

        class _FakeSpawned:
            pid = 0

        loop = asyncio.new_event_loop()
        try:
            yield LongRunningJob(_FakeSpawned(), loop)  # type: ignore[arg-type]
        finally:
            loop.close()

    def test_empty_buffers(self, job):
        assert job.output() == ""
        assert job.error_output() == ""

    def test_ascii_passthrough(self, job):
        job._out.append(b"server started\n")
        assert job.output() == "server started\n"

    @pytest.mark.skipif(sys.platform != "win32", reason="mbcs 仅 Windows")
    def test_gbk_stdout_stderr_recovered(self, job):
        """★ 长驻 job（dev server）的 GBK 输出/报错经新路径解出可读中文。"""
        job._out.append(_GBK_BYTES)
        job._err.append("找不到指定的文件".encode("gbk"))
        assert job.output() == _GBK_TEXT
        assert job.error_output() == "找不到指定的文件"

    def test_run_dict_sites_use_funnel(self):
        """源级钉住：run() 返回 dict 的两处 + output/error_output 两处都走
        漏斗，旧 `.decode("utf-8", errors="replace")` 全数清除。"""
        src = (
            Path(__file__).resolve().parents[1]
            / "src" / "hiveweave" / "services" / "acl_sandbox" / "spawn.py"
        ).read_text(encoding="utf-8")
        assert 'decode("utf-8", errors="replace")' not in src, (
            "spawn.py 仍有 utf-8/replace 直解 —— GBK 报错会毁成 U+FFFD"
        )
        # run() 两处 + output()/error_output() 两处
        assert src.count("decode_subprocess_output(") >= 4, src.count(
            "decode_subprocess_output("
        )
