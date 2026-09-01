"""s3-clone_06 #9：browse eval 的顶层 await 是**确定性失败**，不是 flake。

agent 惯用 `await new Promise(r=>setTimeout(r,N)); <取值表达式>` 等页面渲染，
而 agent-browser 的 eval 在非 async 作用域求值 → `SyntaxError: await is only
valid in async functions`，75–200ms 即失败。实测本项目 ≥4 次（11:51 / 12:24 /
02:14 等），Agent 每次都当成 flake 重试，直接挡住 M5 的 E2E 取证。

契约：命中该 SyntaxError 且命令是 eval 时，**给出可直接复制的 async IIFE 改写**；
不静默重写脚本（静默翻译是坑）；非 eval / 其它错误不误报。
"""

from __future__ import annotations

from hiveweave.tools.browse_tools import top_level_await_rewrite

_AWAIT_ERR = (
    "stderr:\n✗ Evaluation error: SyntaxError: await is only valid in async "
    "functions"
)
_SRC = (
    "await new Promise(r=>setTimeout(r,900)); "
    "JSON.stringify({rows: document.querySelectorAll('[data-testid=\"bucket-row\"]').length})"
)


def test_gives_copy_pasteable_async_iife_rewrite():
    hint = top_level_await_rewrite(["eval", _SRC], _AWAIT_ERR)
    assert hint is not None
    assert "这不是 flake" in hint
    assert "(async () => {" in hint
    # 首句 await 保留，取值表达式转成 return
    assert "await new Promise(r=>setTimeout(r,900));" in hint
    assert "return (" in hint
    assert hint.rstrip().endswith("重新提交。")


def test_generic_hint_when_shape_not_recognised():
    src = "await fetch('/x').then(r=>r.text())"  # 无分号分段的形态
    hint = top_level_await_rewrite(["eval", src], _AWAIT_ERR)
    assert hint is not None
    assert "(async () => { <脚本> })()" in hint


def test_no_hint_for_other_errors_or_commands():
    # 其它 eval 错误（真实内容问题）不给改写建议
    assert top_level_await_rewrite(["eval", _SRC], "TypeError: Failed to fetch") is None
    # 非 eval 子命令不受影响
    assert top_level_await_rewrite(["goto", "http://x"], _AWAIT_ERR) is None
    # 空 stderr
    assert top_level_await_rewrite(["eval", _SRC], "") is None
    # base64 形态也应识别（argv 里脚本不在 argv[1]）
    assert top_level_await_rewrite(["eval", "-b", _SRC], _AWAIT_ERR) is not None
