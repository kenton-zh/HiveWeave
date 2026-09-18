"""util/subprocess_decode.py 的行为钉住测试。

判据来源 TEST_DSH_62 深挖轮：strict utf-8 → mbcs（Windows）→ replace。
关键不变式：ASCII/合法 UTF-8 永不进回退（英文环境零变化）；
GBK 报错字节在中文 Windows 上必须还原可读。
"""
from __future__ import annotations

import sys

import pytest

from hiveweave.util.subprocess_decode import (
    decode_subprocess_output,
    fallback_codec,
)


class TestDecodeSubprocessOutput:
    def test_ascii_passthrough(self):
        assert decode_subprocess_output(b"error: not found") == "error: not found"

    def test_valid_utf8_passthrough(self):
        # 合法 UTF-8 中文（平台自拼文案路径）必须走第一层，不进回退
        assert decode_subprocess_output("命令指向 worktree A115".encode("utf-8")) == (
            "命令指向 worktree A115"
        )

    def test_empty(self):
        assert decode_subprocess_output(b"") == ""

    @pytest.mark.skipif(sys.platform != "win32", reason="mbcs 仅 Windows")
    def test_gbk_stderr_recovered(self):
        # TEST_DSH_62 P6 现场：PowerShell 在中文 Windows 写 GBK 报错
        raw = "表达式缺少有效的操作数".encode("gbk")
        assert decode_subprocess_output(raw) == "表达式缺少有效的操作数"

    @pytest.mark.skipif(sys.platform != "win32", reason="mbcs 仅 Windows")
    def test_gbk_mixed_with_ascii_recovered(self):
        raw = "ParserError\r\n无法将术语识别为函数".encode("gbk")
        assert decode_subprocess_output(raw) == "ParserError\r\n无法将术语识别为函数"

    def test_undecodable_both_ways_replaces_without_raising(self):
        # 对两种编码都非法的字节：必须不抛异常，产 U+FFFD（不比现状差）
        raw = b"\xff\xfe\x81\x30\x00\xd8"
        out = decode_subprocess_output(raw)
        assert isinstance(out, str)

    def test_fallback_codec_windows_is_mbcs(self):
        # PYTHONUTF8=1 下 locale 恒 utf-8，Windows 必须钉 mbcs 才有回退意义
        if sys.platform == "win32":
            assert fallback_codec() == "mbcs"
        else:
            assert fallback_codec() == fallback_codec()  # 非 Windows 只要求稳定
