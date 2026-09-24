"""browse 一次只跑一个子命令 —— 多子命令 argv 走 **advisory 提示**，不再前置拒绝。

现象（实测）：``browse(args=["goto", url, "snapshot", "-i"])`` 只执行 ``goto``，
后续子命令输出被**静默吞掉**（agent 拿不到 ref 树 ⇒ 复用旧 ref ⇒ ``Unknown ref``）。

历史缺陷（本文件守它不再复发）：旧实现把多子命令做成了**前置拒绝**——

  · **漏**：词表 ``_AB_SUBCOMMANDS`` 只覆盖 43 个命令，vendored agent-browser
    README 里另有 40 个缺席 ⇒ 真多子命令（``goto … dblclick``、``open … drag``）
    照样静默吞掉；
  · **误报**：``["cookies","set","--domain","x"]``、``["keyboard","type","hello"]``
    这类**合法单条**（head 之后的 token 是它的子命令/参数）被拒；
  · **安全面净减少**：``cookies set`` 在前置 gate 就被挡 ⇒ cookies/profile 软护栏
    （``browse_tools`` 里 ``cred_tokens`` 那段）变成**死分支**，再也拦不到
    ``cookies import`` / ``--restore`` 之类。

现契约：判据**结构化** —— 剥掉全局 ``--args`` 前缀后，除 head 外的**裸词** token
只有**既是一级命令、又不是 head 的子命令**时才算"第二个子命令"（父子关系见
``_AB_TWO_LEVEL_COMMANDS``，由 README 两级形态派生）⇒ 只在**成功结果尾部**附
advisory，**执行流程逐字不变**（仍只跑 ``argv[0]``，不改任何既有分支）。值（URL /
选择器 ``text=…`` / flag ``-i`` / 路径）不是标识符，永不命中。

文件名里的 ``rejected`` 是**历史名**（改名会牵动引用，本批只改这四个文件）。

本文件三条守卫：
  · 行为回归 —— 合法单条不再被拒、软护栏恢复可达、advisory 只出现在成功结果里；
  · 同源守卫 —— ``_AB_SUBCOMMANDS`` 必须 ⊇ vendored README 解析出的一级命令集，
    且 ``_AB_TWO_LEVEL_COMMANDS`` 必须 == README 派生的两级碰撞面
    （防"字面量复制 + 无同源守卫"的漂移；README 才是权威集）。
"""

from __future__ import annotations

import pathlib
import re

import pytest

from hiveweave.tools import browse_tools
from hiveweave.tools.browse_tools import (
    BrowseParams,
    _AB_SUBCOMMANDS,
    _AB_TWO_LEVEL_COMMANDS,
    _multi_subcommand_tokens,
)

#: 处方关键词（``MULTI_SUBCOMMAND_HINT`` 里逐字包含）。
_PRESCRIPTION = "一条 browse 只跑一个子命令"
#: cookies/profile 软护栏的关键词（``browse_tools`` 里的 ``ToolResult.err`` 文案）。
_SOFT_GUARD = "blocked for agents without an explicit"


@pytest.fixture
def browse_ok(monkeypatch: pytest.MonkeyPatch, tmp_path) -> str:
    """让 browse 走到**成功返回**且不碰 DB（advisory 只在成功结果上）。

    ``browse_exec`` 返回 0/ok；``get_project_id`` → None 使 attestation 早退
    （与 ``test_browse_viewport`` 同款），console capture fail-open 不受影响。
    """

    async def _exec(*_a, **_k):
        return 0, "ok", ""

    async def _no_project(_agent_id):
        return None

    monkeypatch.setattr(
        browse_tools, "resolve_browse_bin", lambda: "/fake/agent-browser"
    )
    monkeypatch.setattr(browse_tools, "browse_exec", _exec)
    monkeypatch.setattr(
        "hiveweave.tools.helpers.get_project_id", _no_project
    )
    return str(tmp_path)


# ── 行为回归：advisory，不是拒绝 ────────────────────────────────────────


async def test_multi_subcommand_is_advisory_not_rejection(browse_ok):
    """① 多子命令 argv ⇒ **成功**返回，且处方出现在输出尾部（不是 err）。"""
    params = BrowseParams(args=["goto", "http://x", "snapshot", "-i"])
    result = await browse_tools.browse_tool(params, "agent-1", browse_ok)
    assert result.success is True, result
    assert result.error is None, result
    assert _PRESCRIPTION in (result.output or ""), result.output


@pytest.mark.parametrize(
    "argv",
    [
        ["cookies", "set", "--domain", "x"],
        ["keyboard", "type", "hello"],
    ],
)
async def test_legit_single_commands_with_command_word_args_not_rejected(
    browse_ok, argv
):
    """② 合法单条（head 之后的 token 是它的子命令/参数）**不再被拒**，
    且经父子收窄后**不再拿到 advisory**。

    这正是旧 gate 的误报面：``cookies set`` 与 ``keyboard type`` 都是**一条**
    两级命令（README 记载），不是"head + 第二个子命令"。
    """
    params = BrowseParams(args=argv)
    result = await browse_tools.browse_tool(params, "agent-1", browse_ok)
    assert result.success is True, result
    assert result.error is None, result
    assert _PRESCRIPTION not in (result.output or ""), result.output


@pytest.mark.parametrize(
    "argv",
    [
        ["cookies", "set", "--domain", "x"],   # README: cookies set <name> <val>
        ["keyboard", "type", "hello"],          # README: keyboard type <text>
        ["tab", "close", "docs"],               # README: tab close [t<N>|label]
        ["clipboard", "read"],                  # README: clipboard read
        ["set", "viewport", "390", "844"],      # README: set viewport <w> <h> [scale]
        ["diff", "snapshot"],                   # README: diff snapshot
        ["storage", "session"],                 # README: storage session
        ["get", "text", "@e1"],                 # child `text` 非一级命令（另一类安全）
    ],
)
def test_readme_two_level_forms_are_never_flagged(argv):
    """回归：README 记载的两级命令必须**永不**被当作第二个子命令。

    这些形态都是**一条**命令——child 是 head 的子命令（``_AB_TWO_LEVEL_COMMANDS``
    覆盖其中的碰撞面），漏掉任一 ⇒ 该形态又回到误报（无害但掩盖真信号）。
    """
    assert _multi_subcommand_tokens(argv) == [], argv


async def test_single_subcommand_has_no_advisory(browse_ok):
    """③ 单子命令 ``["snapshot","-i"]`` ⇒ 成功且**无** advisory。"""
    params = BrowseParams(args=["snapshot", "-i"])
    result = await browse_tools.browse_tool(params, "agent-1", browse_ok)
    assert result.success is True, result
    assert _PRESCRIPTION not in (result.output or ""), result.output
    assert _multi_subcommand_tokens(["snapshot", "-i"]) == []


async def test_cookies_soft_guard_is_reachable_again(browse_ok):
    """④ 安全面回归：``cookies set``（无 ``--domain``）现在能走到**软护栏**。

    旧 gate 在前置就把 ``cookies set`` 拒了 ⇒ 软护栏（``cred_tokens``）是死分支。
    advisory 化之后，真正的拒绝必须来自软护栏（含 ``--domain`` 提示），
    而不是多子命令处方 —— 否则安全面又被 advisory 挤掉了。
    """
    params = BrowseParams(args=["cookies", "set", "sess", "abc"])
    result = await browse_tools.browse_tool(params, "agent-1", browse_ok)
    assert result.success is False, result
    assert _SOFT_GUARD in (result.error or ""), result.error
    assert _PRESCRIPTION not in (result.error or ""), result.error


# ── 阳性对照：advisory 一去掉，断言必须变红 ─────────────────────────────


async def test_positive_control_disabling_the_detector_drops_the_advisory(
    browse_ok, monkeypatch: pytest.MonkeyPatch
):
    """⑤ 把判据短路成 ``[]`` ⇒ 同一条 argv 的 advisory **消失**。

    证明 ① 的断言真的在守**这道新逻辑**（不是别的原因碰巧让处方出现）。
    """
    params = BrowseParams(args=["goto", "http://x", "snapshot", "-i"])

    before = await browse_tools.browse_tool(params, "agent-1", browse_ok)
    assert _PRESCRIPTION in (before.output or ""), (
        f"基线没有 advisory ⇒ 阳性对照无意义：{before.output!r}"
    )

    monkeypatch.setattr(browse_tools, "_multi_subcommand_tokens", lambda _argv: [])
    after = await browse_tools.browse_tool(params, "agent-1", browse_ok)
    assert _PRESCRIPTION not in (after.output or ""), (
        f"短路判据后仍出现 advisory ⇒ 该测试没在守这道逻辑：{after.output!r}"
    )


# ── 判据本身（结构化，无文本猜测）─────────────────────────────────────


@pytest.mark.parametrize(
    "argv",
    [
        ["goto", "http://127.0.0.1:3000"],
        ["fill", "#email", "user@example.com"],
        ["click", "text=Close"],           # 值含命令词但带 '=' ⇒ 非裸词
        ["screenshot", "evidence/a.png"],  # 路径 ⇒ 非标识符
        ["--args", "--disable-http-cache", "goto", "http://x"],  # 全局前缀被剥离
        ["eval", "document.title"],
    ],
)
def test_values_never_trip_the_detector(argv):
    """反向对照：值（URL/选择器/flag/路径）与全局 ``--args`` 前缀不误命中。"""
    assert _multi_subcommand_tokens(argv) == [], argv


def test_detects_second_subcommand_token_structurally():
    """正向判据本身：除 head 外的裸词子命令被如实报出。

    ① 真链：``goto … snapshot``（snapshot 非 goto 的子命令）；
    ② 碰撞 head 上的真链：``cookies set snapshot`` —— ``set`` 是 cookies 的
       子命令（被排除），``snapshot`` 才是第二个子命令（须报出）。
    """
    assert _multi_subcommand_tokens(
        ["goto", "http://x", "snapshot", "-i"]
    ) == ["snapshot"]
    assert _multi_subcommand_tokens(
        ["cookies", "set", "snapshot"]
    ) == ["snapshot"]


# ── 同源守卫：词表必须 ⊇ vendored README 的命令集（防漂移）──────────────

#: 词表的来源候选路径（仓库根相对，兼容 pnpm 与扁平 node_modules）。
_README_PATTERNS = (
    "node_modules/.pnpm/agent-browser@*/node_modules/agent-browser/README.md",
    "apps/web/node_modules/.pnpm/agent-browser@*/node_modules/agent-browser/README.md",
    "node_modules/agent-browser/README.md",
    "apps/web/node_modules/agent-browser/README.md",
)


def _vendored_readme_path() -> pathlib.Path:
    """定位 vendored README；找不到就 **fail-loud**（不能静默跳过）。"""
    root = pathlib.Path(__file__).resolve().parents[3]
    hits: list[pathlib.Path] = []
    for pattern in _README_PATTERNS:
        hits.extend(root.glob(pattern))
    if not hits:
        pytest.fail(
            "找不到 vendored agent-browser README —— 同源守卫无法取证（fail-loud，"
            f"不得静默跳过）。root={root}；已查：{_README_PATTERNS}"
        )
    return sorted(hits)[-1]


def _parse_readme(text: str) -> tuple[set[str], set[tuple[str, str]]]:
    """**一个解析器、两个派生集**，都来自 README 的 ```bash 段（单一权威源）。

    · ``top_level`` —— 每条 ``agent-browser <cmd>`` 的首 token（一级命令集）；
    · ``pairs``     —— 两 token 形态 ``agent-browser <head> <child>`` 的
      ``(head, child)``，child 必须是裸词（``str.isidentifier``）。

    解析失败**必须 fail-loud**（README 缺 → 由 ``_vendored_readme_path`` 抛；
    无代码段 / 什么都没解析出 / 一条两级形态都没有 ⇒ 这里 pytest.fail）——
    否则守卫形同虚设。
    """
    blocks = re.findall(r"```bash\n(.*?)```", text, re.DOTALL)
    if not blocks:
        pytest.fail("README 里没有 ```bash 代码段 —— 解析器失效（fail-loud）")
    top_level: set[str] = set()
    pairs: set[tuple[str, str]] = set()
    for block in blocks:
        for line in block.splitlines():
            m = re.match(
                r"\s*agent-browser\s+([A-Za-z][\w-]*)(?:\s+([A-Za-z][\w-]*))?",
                line,
            )
            if not m:
                continue
            top_level.add(m.group(1))
            child = m.group(2)
            if child and child.isidentifier():
                pairs.add((m.group(1), child))
    if not top_level:
        pytest.fail(
            "```bash 代码段里没解析出任何 `agent-browser <cmd>` —— 解析器失效"
            "（fail-loud）"
        )
    if not pairs:
        pytest.fail(
            "README 里没有解析出任何两级形态（`agent-browser <head> <child>`）—— "
            "父子收窄守卫会静默空壳（fail-loud）"
        )
    return top_level, pairs


def _derived_two_level(
    top_level: set[str], pairs: set[tuple[str, str]]
) -> set[tuple[str, str]]:
    """README 派生出的**碰撞面** ``(head, child)``：child 本身是检测器认的
    一级命令（README 一级集 ∪ ``_AB_SUBCOMMANDS``，后者含 gstack-only 名
    ``viewport``）。

    只有这些 pair 会被"除 head 外的裸词一级命令"规则误判 ⇒ 正是
    ``_AB_TWO_LEVEL_COMMANDS`` 必须精确覆盖的集合。
    """
    known = top_level | set(_AB_SUBCOMMANDS)
    return {(head, child) for head, child in pairs if child in known}


def test_vocabulary_is_superset_of_vendored_readme_commands():
    """同源守卫：``_AB_SUBCOMMANDS`` ⊇ README 解析出的权威一级命令集（防漂移）。"""
    readme = _vendored_readme_path()
    top_level, _pairs = _parse_readme(readme.read_text(encoding="utf-8"))
    missing = sorted(top_level - set(_AB_SUBCOMMANDS))
    assert not missing, (
        f"_AB_SUBCOMMANDS 缺 {len(missing)} 个 vendored README 命令：{missing}\n"
        f"（权威集共 {len(top_level)} 个，词表 {len(_AB_SUBCOMMANDS)} 个）"
    )


def test_two_level_forms_match_vendored_readme():
    """同源守卫：``_AB_TWO_LEVEL_COMMANDS`` == README 派生的碰撞面（防漂移）。"""
    readme = _vendored_readme_path()
    top_level, pairs = _parse_readme(readme.read_text(encoding="utf-8"))
    derived = _derived_two_level(top_level, pairs)
    assert derived, "README 没派生任何两级碰撞面 —— 守卫是空壳（fail-loud 应已拦住）"
    assert derived == set(_AB_TWO_LEVEL_COMMANDS), (
        "两级命令表与 README 派生结果不一致：\n"
        f"  仅派生有（会被误判为第二子命令）：{sorted(derived - set(_AB_TWO_LEVEL_COMMANDS))}\n"
        f"  仅词表有（多余条目）：{sorted(set(_AB_TWO_LEVEL_COMMANDS) - derived)}"
    )


def test_readme_parser_flags_an_unknown_command():
    """阳性对照：解析器 + 差集口径真能抓出词表外的命令（否则守卫是空壳）。"""
    top_level, pairs = _parse_readme(
        "```bash\nagent-browser zzz-not-real\nagent-browser zzz-not-real child\n```\n"
    )
    assert top_level == {"zzz-not-real"}
    assert sorted(top_level - set(_AB_SUBCOMMANDS)) == ["zzz-not-real"]
    assert pairs == {("zzz-not-real", "child")}


def test_readme_parser_derives_two_level_pairs():
    """阳性对照：两 token 形态确实被解析成 ``(head, child)`` 碰撞对。"""
    top_level, pairs = _parse_readme(
        "```bash\nagent-browser foo\nagent-browser bar\nagent-browser foo bar\n```\n"
    )
    assert top_level == {"foo", "bar"}
    assert _derived_two_level(top_level, pairs) == {("foo", "bar")}
