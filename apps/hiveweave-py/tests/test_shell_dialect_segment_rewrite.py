"""P2（TEST_DSH_62 分段改写）：封闭集管道尾自动翻译的**分段/复合命令**行为。

根因：旧 ``try_closed_pipe_translation`` 把 ``_CLOSED_PIPE_TAIL_RE`` 的 ``$``
锚在**整条命令串尾** —— 复合命令中段（``git status | head -n 20; echo done``）
的管道尾永远够不着翻译，方言门随后发现 head 段整条拒绝（14 次方言拒绝 8 次
卡这）。修后按**语句**（``;`` / ``&&`` / ``||`` / ``&`` / 换行为界，管道属语句
内部）逐段套同一封闭集正则、按原分隔符重组。

判据（与 test_pwsh_dialect_gate.py 同一套纪律）：
- 正例必须**两头都断**：译了 + 译出的命令能过 ``detect_untranslated_unix``
  （译出仍被拒 = 没修）；
- 反例必须**不译且仍被拒**：``try_closed_pipe_translation`` 返回 None 且
  ``detect_untranslated_unix(原文)`` 非 None（既不译也不拒 = 被悄悄放行）。
- 安全判据不可破：``head -c`` / ``wc -c`` / 负数 / 尾注释 / heredoc /
  引号内 / ``$()`` 体内一律不译。
"""
from __future__ import annotations

from hiveweave.tools.bash import (
    _split_command_segments,
    detect_untranslated_unix,
    try_closed_pipe_translation,
    try_dialect_translation,
)


# ── 切分器重构回归：投影语义逐字符不变（既有测试另有钉，这里钉新增的
#    "保留分隔符"核心不改变旧投影的行为）──────────────────────────


def test_split_segments_projection_unchanged() -> None:
    """`_split_command_segments`（旧签名）在新核心之上的投影必须与既有语义一致：
    引号内不切、fd 重定向不切、`&&`/`||` 双字符切、单 `&` 切。"""
    assert _split_command_segments('git commit -m "fix; sed edge"') == [
        'git commit -m "fix; sed edge"'
    ]
    assert len(_split_command_segments("cd /tmp && ls -la ; pwd")) == 3
    assert len(_split_command_segments("a && b")) == 2
    assert len(_split_command_segments("python -u x.py 2>&1")) == 1
    assert len(_split_command_segments("sleep 1 & tail -f log")) == 2


# ── ① 复合命令中段的管道尾：必须被译 ─────────────────────────


def test_mid_statement_tail_is_translated() -> None:
    """★ 主修场景：`git status --short | head -n 20; echo done` 的管道尾在
    **第一段末尾**而非整条命令末尾 —— 旧实现永远够不着。"""
    command = "git status --short | head -n 20; echo done"
    got = try_closed_pipe_translation(command)
    assert got is not None, "中段管道尾没被译（修前：整条被拒）"
    translated, original = got
    assert translated == (
        "git status --short | Select-Object -First 20; echo done"
    ), translated
    assert original == command
    # `;` 段保留：后一段原样、分隔符原样
    assert "; echo done" in translated
    # 译出的命令必须能过方言门（译了等于没译的防回归）
    assert detect_untranslated_unix(translated) is None
    # 链真的接了（生产路径走 try_dialect_translation）
    chain = try_dialect_translation(command)
    assert chain is not None and chain[0] == translated


def test_multi_statement_all_tails_translated_with_count() -> None:
    """一条命令里**多段**管道尾都译，rewritten_segments 记段数。"""
    command = "git log | head -3; git log | tail -5"
    got = try_closed_pipe_translation(command)
    assert got is not None
    translated, _original = got
    assert translated == (
        "git log | Select-Object -First 3; git log | Select-Object -Last 5"
    ), translated
    assert getattr(got, "rewritten_segments", 1) == 2
    assert detect_untranslated_unix(translated) is None


def test_statement_separators_and_spacing_preserved() -> None:
    """未命中段**逐字保留**（含原空格），分隔符原样重组；分隔符**前的**
    原空白也要保留（`-First 3&` 会被 pwsh 并进同一 token，构词级差异）。"""
    # `&&` 两侧的双空格必须原样活着
    got = try_closed_pipe_translation("echo start  &&  git log | head -3")
    assert got is not None
    assert got[0] == "echo start  &&  git log | Select-Object -First 3", got[0]
    # 换行分隔符
    got = try_closed_pipe_translation("git add .\ngit log | head -3")
    assert got is not None
    assert got[0] == "git add .\ngit log | Select-Object -First 3", got[0]
    # `;` 后的前导空格保留
    got = try_closed_pipe_translation("git add .; git log | head -3")
    assert got is not None
    assert got[0] == "git add .; git log | Select-Object -First 3", got[0]
    # `;` / `&` 前的原空白保留（改写段的段尾空白不得被 rstrip 吃掉）
    got = try_closed_pipe_translation("git log | head -3 ; echo done")
    assert got is not None
    assert got[0] == "git log | Select-Object -First 3 ; echo done", got[0]
    got = try_closed_pipe_translation("git log | head -3 &")
    assert got is not None
    assert got[0] == "git log | Select-Object -First 3 &", got[0]
    # fd 重定向 2>&1 在管道头里不受影响
    got = try_closed_pipe_translation("pytest -q 2>&1 | tail -20")
    assert got is not None
    assert got[0] == "pytest -q 2>&1 | Select-Object -Last 20", got[0]


def test_quoted_separator_does_not_split_statements() -> None:
    """引号内的 `;` 不是语句边界：`echo "a;b" | head -3` 是**一条**语句。"""
    got = try_closed_pipe_translation('echo "a;b" | head -3')
    assert got is not None
    assert got[0] == 'echo "a;b" | Select-Object -First 3', got[0]
    assert detect_untranslated_unix(got[0]) is None


def test_closed_cmd_substitution_before_tail_is_fine() -> None:
    """闭合的 `$()` 在管道头里：体内不动，尾照译（体内 git 不该挡翻译）。"""
    got = try_closed_pipe_translation("echo $(git status --short) | head -3")
    assert got is not None
    assert "$(git status --short)" in got[0], got[0]
    assert got[0].endswith("| Select-Object -First 3")


# ── ② fail-safe：拿不准 ⇒ 整体 return None（不做半吊子改写）────


def test_unix_only_in_other_statement_blocks_whole_translation() -> None:
    """★ 其他语句残留 unix-only（sed）⇒ 整条不译 —— 半改比不改糟，
    交给 gate 按 sed 给处方。"""
    command = "sed -n 1,40p R.md; git log | head -3"
    assert try_closed_pipe_translation(command) is None, command
    assert try_dialect_translation(command) is None, command
    # 且原文仍会被拒（不是被悄悄放行）
    assert detect_untranslated_unix(command) is not None


def test_unix_only_pipe_head_blocks_that_statement() -> None:
    """管道头本身是 unix-only（sed 在段首）⇒ 该语句不译 ⇒ 整体不译。"""
    command = "sed -n '1,40p' R.md | head -5"
    assert try_closed_pipe_translation(command) is None, command
    assert detect_untranslated_unix(command) is not None


def test_unclosed_cmd_substitution_is_not_translated() -> None:
    """未闭合 `$(` ⇒ 匹配到的"管道尾"可能身在替换体内 ⇒ 整条不译。"""
    assert try_closed_pipe_translation("echo $(git log | head -3") is None


def test_empty_pipe_head_statement_bails_out() -> None:
    """命中段的前段为空（如尾随孤立 `| head -5` 段）⇒ 整体不译。"""
    assert try_closed_pipe_translation("git log | head -3; | head -5") is None


def test_double_pipe_logical_or_not_mistranslated() -> None:
    """`a || head -3`：`||` 是语句边界，右侧 ` head -3` 自成语句（无管道符）
    不命中 —— 不得像旧实现那样错切成 `a | | Select-Object …`。"""
    assert try_closed_pipe_translation("cat a || head -3") is None
    # 正向对照：`||` 右侧是完整管道（含自己的 `|`）时照译
    got = try_closed_pipe_translation("false || git log | head -3")
    assert got is not None
    assert got[0] == "false || git log | Select-Object -First 3", got[0]


# ── ③④ 安全封闭集不放宽：这些形态一律不译 ────────────────────


def test_safe_forms_are_never_translated() -> None:
    """head -c / wc -c / wc -w / 负数 / 尾注释 / heredoc：正则与 detect
    双保险，分段改写不得放宽任何一条。"""
    for command in (
        "git log | head -c 400",
        "git log | head -c 400; echo done",
        "git log | wc -c",
        "git log | wc -w",
        "git log | head -n -5",
        "git log | head -3  # note",
        "cat f << EOF | head -3",
        "cat f << EOF | head -3; echo done",
    ):
        assert try_closed_pipe_translation(command) is None, command
        # 除尾注释/heredoc 变体外，原文都必须仍被方言门拒（不悄悄放行）
        if "# note" not in command:
            assert detect_untranslated_unix(command) is not None, command


def test_head_inside_quotes_is_not_translated() -> None:
    """引号内的 `| head -3` 不是管道尾（段以引号收口，正则锚不中）。"""
    for command in (
        'echo "foo | head -3"',
        'echo "foo | head -3"; echo done',
        "echo 'bar | tail -5'",
    ):
        assert try_closed_pipe_translation(command) is None, command


# ── ⑤ 纯串尾形态防回归（旧行为的等价对照）─────────────────────


def test_pure_tail_forms_regressions() -> None:
    """修前就能译的形态必须原样保留（含尾随空白被吃掉的既有行为）。"""
    cases = {
        "git log --oneline -5 | head -3":
            "git log --oneline -5 | Select-Object -First 3",
        "cat f.txt | head -5":
            "cat f.txt | Select-Object -First 5",
        "git log | tail -3":
            "git log | Select-Object -Last 3",
        "cat f | wc -l":
            "cat f | Measure-Object -Line",
        "  git log | head -3  ":
            "  git log | Select-Object -First 3",
    }
    for command, expected in cases.items():
        got = try_closed_pipe_translation(command)
        assert got is not None, command
        assert got[0] == expected, (command, got[0])
        assert got[1] == command, "原命令必须原样带回（尾注/日志要用）"
        assert detect_untranslated_unix(got[0]) is None, got[0]


def test_translated_pair_still_a_plain_two_tuple() -> None:
    """`_TranslatedCommand` 载体必须仍是二元组：既有解包/索引兼容。"""
    got = try_closed_pipe_translation("git log | head -3")
    assert got is not None
    a, b = got              # 两元解包不炸
    assert got[0] == a and got[1] == b
    assert got == ("git log | Select-Object -First 3", "git log | head -3")
