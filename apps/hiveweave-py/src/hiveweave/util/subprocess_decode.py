"""子进程输出解码统一漏斗。

Windows 上子进程（PowerShell / 原生工具）常按系统 ANSI 代码页（中文 Windows =
GBK/cp936）输出本地化报错；直接按 UTF-8 解码会把可读报错销毁成 U+FFFD
（TEST_DSH_62 P6，字节级已证：GBK 字节按 utf-8/replace 解码后 4/4 错字）。

回退序：strict utf-8 → 系统 ANSI（Windows 用 ``mbcs``，其他平台 locale）→
utf-8/replace。纯 ASCII / 合法 UTF-8 输出在第一层成功、永不进回退，英文环境
零变化；只有今天已经变 U+FFFD 垃圾的字节才走回退。

注意不能用 ``locale.getpreferredencoding()`` 做 Windows 回退：本机
``PYTHONUTF8=1``（及 PEP 686 之后所有 Python）下它恒返回 utf-8，回退等于没
回退；``mbcs`` 不受 UTF-8 模式影响。
"""
from __future__ import annotations

import locale
import logging
import sys

logger = logging.getLogger("hiveweave.util.subprocess_decode")


def fallback_codec() -> str:
    """UTF-8 严格解码失败后的回退编码名。"""
    if sys.platform == "win32":
        return "mbcs"
    return locale.getpreferredencoding(False)


def decode_subprocess_output(data: bytes) -> str:
    """按 strict utf-8 → ANSI 回退 → replace 的顺序解码子进程输出。"""
    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        # 棘轮纪律（2026-09-21）：落到下一档 codec 前**记一笔**（原来是 `pass`；
        # 随后 `..._fallback_used` 只说明"用了 fallback"，不说明 utf-8 失败过）。
        logger.debug("subprocess_decode_utf8_failed nbytes=%d", len(data))
    codec = fallback_codec()
    try:
        text = data.decode(codec)
    except (UnicodeDecodeError, LookupError):
        return data.decode("utf-8", errors="replace")
    logger.debug(
        "subprocess_decode_fallback_used codec=%s nbytes=%d", codec, len(data)
    )
    return text
