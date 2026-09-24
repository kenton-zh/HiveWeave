"""内联多行脚本转义失败 ⇒ 处方（``write_file`` 落盘后再 ``node <file>``）。

两条失败签名此前**没有任何处方**：
  · 宿主为 pwsh：``ParserError``；
  · 内联脚本落到 V8：``Unterminated regexp literal``。

按既有形态（``_maybe_append_node_isolation_hint``）接线：命令里有内联 ``node -e``
+ 输出里有上述签名 ⇒ 追加"落盘后再跑"的处方；且必须**经唯一链**
``_maybe_append_test_hints`` 生效（否则"函数写好了但没接线"照样全绿 —— 生产走链）。

阳性对照：命令侧 / 输出侧任一**观测**缺失 ⇒ 处方必须消失。
"""

from __future__ import annotations

import pytest

from hiveweave.tools.bash import (
    INLINE_EVAL_NOTE,
    _maybe_append_inline_eval_hint,
    _maybe_append_test_hints,
)

_CMD = 'node -e "\nconst r = /x/;\nconsole.log(r);\n"'
_PRESCRIPTION = "跨行脚本别内联 -e：write_file 落盘后再 node <file>"


@pytest.mark.parametrize(
    "error",
    [
        "SyntaxError: Invalid regular expression: /[/: Unterminated regexp literal",
        "ParserError: Unexpected token '}' in expression or statement.",
    ],
)
def test_inline_eval_hint_fires_and_carries_prescription(error: str):
    """★ 处方必须同时：① 含处方关键词；② 经**唯一链**生效。"""
    out = _maybe_append_inline_eval_hint(_CMD, error)
    assert _PRESCRIPTION in out, out
    assert "write_file" in out and "node <file>" in out, out

    chain = _maybe_append_test_hints(_CMD, error)
    assert _PRESCRIPTION in chain, f"处方没接进链（生产不过这个函数）：{chain[:200]!r}"


@pytest.mark.parametrize(
    "command,error",
    [
        ('node -e "console.log(1)"', "Error: boom"),   # 有内联 -e、无失败签名
        ("node --test t.js", "ParserError: bad"),       # 有签名、无内联 -e
        ("git status", "ParserError: bad"),             # 两者都无
        ("", "Unterminated regexp literal"),            # 无命令
        (_CMD, ""),                                     # 无输出
    ],
)
def test_no_prescription_without_both_observations(command: str, error: str):
    """反向对照：命令 / 输出任一观测缺失 ⇒ 一个字都不加（零扰动旁支）。"""
    assert _maybe_append_inline_eval_hint(command, error) == error


def test_note_constant_is_the_single_source():
    """处方文案来自**构造点常量**（唯一源），防两处漂移。"""
    assert INLINE_EVAL_NOTE.strip()
    assert _PRESCRIPTION in INLINE_EVAL_NOTE
