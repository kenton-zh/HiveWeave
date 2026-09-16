"""P0-2 (TEST_DSH_33)/P1-3（B 结构解）: bash→pwsh 方言 gate + pwsh 工具。

TEST_DSH_33 实测 126 次方言失败（占失败步 41.9%）：受限沙箱 shell = pwsh。
P1-3（B 结构解，对齐 deepseek-harness"不翻译、原生双工具"）词典翻译层
退役后，gate 契约更新为：
- pwsh 原生/兼容命令（Get-Content / python / git / 裸 echo）→ 放行
- unix 惯用语（head/tail/ls 带 flag/grep/wc/mkdir -p/cat|grep）→ **前置拒绝**，
  出带等价写法的可操作错误（曾"翻译后放行"的形态现在同样被拦 —— 兑现
  bash 工具 description 承诺的 rejected up front with the pwsh equivalent）
- 非受限 pwsh 环境（Git Bash native / cmd 兜底）→ gate 闭嘴
- pwsh 工具（dialect="pwsh"）→ 命令本就是 PowerShell，不走 gate
"""

from __future__ import annotations

import pytest

from hiveweave.tools.bash import (
    _ALIAS_FLAG_HINTS,
    _UNIX_ONLY_HINTS,
    _pwsh_dialect_gate,
    _segment_head_token,
    _split_command_segments,
    detect_untranslated_unix,
    try_closed_pipe_translation,
    try_dialect_translation,
    try_readonly_limit_translation,
)


@pytest.fixture
def gate_on(monkeypatch: pytest.MonkeyPatch):
    """强制方言 gate 生效（无视平台/沙箱探测）。"""
    monkeypatch.setattr(
        "hiveweave.tools.bash._pwsh_is_effective_shell", lambda: True
    )


# ── P1-3（B 结构解）：pwsh 原生/兼容命令放行（回归保护）─────────


@pytest.mark.parametrize(
    "command",
    [
        "Get-Content x -TotalCount 80",  # PowerShell 方言
        "python script.py",              # 外部程序，pwsh 直调
        "echo hello world",              # 裸 echo 无 unix flag → pwsh 兼容
        "git status",
        "cd src; Get-ChildItem",
    ],
)
def test_gate_passes_native_or_pwsh_idiom(gate_on, command):
    assert _pwsh_dialect_gate(command) is None, command


# ── P1-3（B 结构解）：unix 惯用语不再转译 —— 前置拒绝 + 给等价 ──


@pytest.mark.parametrize(
    "command",
    [
        "head -n 800 docs/design.md",   # TEST_DSH_33 曾原样透传 → 现在前置拒
        "head -80 docs/design.md",
        "tail -n 20 out.log",
        "tail -20 out.log",
        "ls -lh src",                   # pwsh 有 ls 别名但 flag 语义不同
        "ls -la src",
        'grep -rn "judge" src/',
        "wc -l x.ts",
        "cat app.log | grep warning",   # 管道尾 grep
        # R3 P0-2：模型在 pwsh 工具里照写 unix 管道（2518560b 实证）——
        # gate 对 pwsh 方言不再短路，照样拒。
        "Get-Content README.md | head -n 50",
        "Get-Content out.log -Tail 5 | grep error",
        # 注：`mkdir -p` 不在本组 —— pwsh 有 mkdir 别名（兼容命令），
        # `-p` 由 pwsh 自身参数校验报可见错误，模型可自纠，非静默错译。
    ],
)
def test_gate_rejects_unix_idiom_verbatim(gate_on, command):
    err = _pwsh_dialect_gate(command)
    assert err is not None, command
    # 文案必须指路 pwsh 工具（第二出路），且禁止「换个 flag 重试」
    assert "pwsh" in err
    assert "Do not retry" in err


# ── 翻译不了的形态：拦截并给等价写法 ────────────────────────


@pytest.mark.parametrize(
    ("command", "needle"),
    [
        ('sed -n "1,40p" R.md', "Get-Content"),
        ("awk '{print $1}' data.tsv", "ForEach-Object"),
        ("grep -v warning app.log", "Select-String"),
        # 类 2：pwsh 有同名别名但 unix flag 语义对不上 → 仅带 flag 时拦
        ("ps aux", "Get-Process"),
        ('find src -name "*.tsx" | head -5', "Get-ChildItem"),
        ("cat p.json | head -n 30", "Get-Content"),
    ],
)
def test_gate_blocks_unix_only_with_hint(gate_on, command, needle):
    err = _pwsh_dialect_gate(command)
    assert err is not None, command
    assert needle in err
    # 文案必须指路 pwsh 工具（第二出路），且禁止「换个 flag 重试」
    assert "pwsh" in err
    assert "Do not retry" in err


def test_gate_blocks_ls_with_unmapped_flag(gate_on):
    """`ls -x` 这类翻译表没有的 flag 组合 → 类 2 拦截。"""
    err = _pwsh_dialect_gate("ls -x src")
    assert err is not None
    assert "ls" in err


def test_gate_quiet_when_not_pwsh_shell(monkeypatch: pytest.MonkeyPatch):
    """非受限 pwsh 环境（Git Bash native）→ gate 必须闭嘴（防误伤真 bash）。"""
    monkeypatch.setattr(
        "hiveweave.tools.bash._pwsh_is_effective_shell", lambda: False
    )
    assert _pwsh_dialect_gate('sed -n "1,40p" R.md') is None


# ── 引号感知的段切分（防误拦）───────────────────────────────


def test_split_segments_quotes_do_not_split():
    segs = _split_command_segments('git commit -m "fix: parse; sed edge case"')
    assert len(segs) == 1
    assert "sed edge case" in segs[0]


def test_split_segments_operators_do_split():
    segs = _split_command_segments("cd /tmp && ls -la ; pwd")
    assert len(segs) == 3


def test_head_token_strips_env_prefix_and_path():
    assert _segment_head_token("FOO=bar wc -l x") == "wc"
    assert _segment_head_token("/usr/bin/sed -i s/a/b/ f") == "sed"
    assert _segment_head_token("C:\\Windows\\System32\\sort.exe x") == "sort"


def test_detect_after_normalize_pipe_with_translated_head(gate_on):
    """管道尾的 head 无文件参数 → 翻译规则（要求 head -N file）不适用，
    head 连同 find 一起被拦——拦下来给等价写法，不再静默透传。"""
    err = _pwsh_dialect_gate('find src -name "*.tsx" | head -5')
    assert err is not None
    assert "find" in err
    assert "head" in err


def test_detect_untranslated_unix_dedupes_same_head():
    """同一命令里同名命令段只提示一次（seen 去重）。"""
    err = detect_untranslated_unix("wc -l a.txt && wc -l b.txt")
    assert err is not None
    assert err.count("wc →") == 1


# ── pwsh 工具注册不变式（5 处接线缺一不可）─────────────────


def test_pwsh_tool_wiring_invariants():
    """新工具 5 处接线：注册表 / executor schema / 权限 / 硬门 / 超时表。

    对照 bash 逐项校验——pwsh 与 bash 同壳层同语义，任何一处漏接
    都会复现「注册可见但调用即死」（python_script 事故形态）。
    """
    from hiveweave.services.permission import (
        ALL_TOOLS,
        COORDINATOR_BUILDER_TOOLS,
        READONLY_TOOLS,
        READWRITE_TOOLS,
    )
    from hiveweave.services.policy import TOOL_CAPABILITY
    from hiveweave.tools.base import _TOOL_REGISTRY
    from hiveweave.tools.executor import TOOL_PARAM_SCHEMAS

    for registry in (READONLY_TOOLS, READWRITE_TOOLS, ALL_TOOLS):
        assert "pwsh" in registry, registry
    assert "pwsh" in COORDINATOR_BUILDER_TOOLS
    # 硬门与 bash 同能力位（BASH_SHELL）——未知能力位会落 mode 兜底 ask
    assert TOOL_CAPABILITY.get("pwsh") == TOOL_CAPABILITY.get("bash")
    # executor schema 是 LLM 的唯一主源（@tool 只是回退）
    assert "pwsh" in TOOL_PARAM_SCHEMAS
    props = sorted((TOOL_PARAM_SCHEMAS["pwsh"] or {}).get("properties", {}))
    assert props == ["background", "command", "taskId", "testEvidence", "timeout"]
    # @tool 注册表（回退路径）
    assert "pwsh" in _TOOL_REGISTRY
    d = _TOOL_REGISTRY["pwsh"]
    assert getattr(d, "security_level", None) == "shell"
    assert getattr(d, "requires_workspace", None) is True


def test_pwsh_description_declares_dialect():
    """方言声明必须在 schema description 里（P1-6：引导缺口的治本位）。"""
    from hiveweave.tools.bash import PWSH_TOOL_DESCRIPTION

    # 提示词注入给模型的是「写 PowerShell」而非「写 bash」
    assert "PowerShell" in PWSH_TOOL_DESCRIPTION
    assert "bash" in PWSH_TOOL_DESCRIPTION.lower()


# ── 挂载点集成（gate 必须真的挂在 execute_bash 上）─────────


@pytest.mark.asyncio
async def test_execute_bash_mounts_dialect_gate(
    gate_on, monkeypatch: pytest.MonkeyPatch, tmp_path
):
    """删掉 execute_bash 里的 gate 调用不能逃过测试：unix-only 命令必须在
    执行前被拦成 blocked（而非透传给 pwsh 报「不是内部或外部命令」）。"""
    from hiveweave.tools.bash import execute_bash

    result = await execute_bash('sed -n "1,40p" R.md', "", str(tmp_path))
    assert result["success"] is False
    assert result["blocked"] is True
    assert "Select-Object" in result["error"]  # 等价写法在场
    assert "pwsh" in result["error"]
    # s3-clone_06 P0-3/P0-4：命令从未执行 → runner_failed=1（不只 blocked）。
    # 否则 (runner_failed=0, command_failed=0) 桶与"未知失败"不可区分，
    # 且 F10 归因会把"方言不兼容"错报成"平台护栏拒绝（安全拦截）"。
    assert result["runner_failed"] is True
    assert result["dialect_failed"] is True
    assert result.get("command_failed") is not True


@pytest.mark.asyncio
async def test_dialect_gate_fact_flags_survive_tool_result(
    gate_on, monkeypatch: pytest.MonkeyPatch, tmp_path
):
    """事实位必须一路活到 ToolResult，**包括 blocked 分支**。

    2026-09-01 实战抓到：`_shell_tool_result` 的 blocked 分支走
    `ToolResult.blocked_err(err_msg, **public)`，只传 public 把 `fact_flags`
    （runner_failed / command_failed / timeout_*）整个丢掉 —— 方言门与护栏
    拒绝恰恰都是 blocked=True，于是报告 #4「runner_failed 恒 0」在**最该
    置位的那类失败**上原样残留。单测 execute_bash 全绿也发现不了。
    """
    from hiveweave.tools.bash import BashParams, bash_tool

    params = BashParams(command='sed -n "1,40p" R.md')
    tr = await bash_tool(params, "agent-1", str(tmp_path))
    payload = tr.to_dict()

    assert payload.get("success") is False
    assert payload.get("blocked") is True
    assert payload.get("runner_failed") is True, (
        "blocked 路径丢了 runner_failed → run_steps 事实位失效"
    )
    assert payload.get("dialect_failed") is True


@pytest.mark.asyncio
async def test_execute_bash_pwsh_dialect_skips_gate(
    gate_on, monkeypatch: pytest.MonkeyPatch, tmp_path
):
    """pwsh 工具（dialect="pwsh"）不做 unix 规范化——PowerShell 方言直接
    到执行层；同一命令若走 bash 方言则会被 gate 拦（对照组在上一用例）。"""

    async def fake_exec(*args, **kwargs):
        return {"output": "ok", "stdout": "ok", "stderr": "",
                "exit_code": 0, "timed_out": False, "error": None}

    monkeypatch.setattr("hiveweave.tools.bash._run_sandboxed", fake_exec)
    from hiveweave.tools.bash import execute_bash

    # "Get-ChildItem -Force" 是 PowerShell 方言；bash 方言的翻译表不认识它
    result = await execute_bash(
        "Get-ChildItem -Force", "", str(tmp_path), dialect="pwsh"
    )
    assert result["success"] is True
    assert result["exit_code"] == 0


# ── 45 轮 P0 补充：新动词（od 实锤）/ env 前缀语法层 / kill 族护栏对齐 ──


def test_detect_flags_od_from_s3c10_pipeline():
    """s3-clone_10 实锤：`git show … | od -c | head -5` 中 od 此前不在表内。"""
    msg = detect_untranslated_unix("git show main:reqs.txt | od -c | head -5")
    assert msg is not None
    assert "od" in msg
    assert "head" in msg


def test_detect_flags_bash_env_prefix():
    msg = detect_untranslated_unix("FOO=bar python -m pytest -q")
    assert msg is not None
    assert "environment-prefix" in msg
    assert "$env:VAR='val'" in msg


@pytest.mark.parametrize(
    "command",
    [
        "$env:FOO='bar'; python -m pytest -q",   # pwsh 原生赋值
        "python -c \"a=1; print(a)\"",           # 引号内 = 不在段首
        "git -c core.autocrlf=false log --oneline",  # -c 选项不是段首 VAR=
        "pytest -q 2>&1 | Select-Object -Last 15",   # pwsh 原生管道尾
    ],
)
def test_detect_env_prefix_clean_forms_not_blocked(command):
    assert detect_untranslated_unix(command) is None


def test_gate_blocks_env_prefix(gate_on):
    msg = _pwsh_dialect_gate("FOO=bar python -m pytest -q")
    assert msg is not None
    assert "environment-prefix" in msg


@pytest.mark.parametrize("verb", ["pkill", "kill"])
def test_kill_hints_do_not_suggest_guard_denied_forms(verb):
    """kill 族等价建议不得指向护栏 deny 的 Stop-Process（两头撞墙）。"""
    hint = _UNIX_ONLY_HINTS.get(verb) or _ALIAS_FLAG_HINTS.get(verb) or ""
    assert "stop-process" not in hint.lower()
    assert "kill <pid>" in hint  # 护栏放行的精确 PID 形式


def test_new_verbs_present_in_tables():
    for v in ("od", "export", "base64", "uname", "lsof", "time"):
        assert v in _UNIX_ONLY_HINTS, v


# ── ① 直接形态的只读限流词：**前置转译**而非整条拒绝（2026-09-16）────
#
# 计划 §并行池「三条路径在、但走不通」① 的原文：只读限流词（head/tail/wc -l）
# **前置转译**而非整条拒绝；验收「判据从"拦了几个"改成"**译了几个**"」。
#
# 修前的实测边界（探针，2026-09-16）：
#   `cat f | head -5`   → **已译**（管道尾，`try_closed_pipe_translation`）
#   `head -5 f`         → **整条被拒**   ← agent 最常用的写法，正是缺口
#   `head -n 5 f`       → 整条被拒
#   `tail -20 f`        → 整条被拒
#   `wc -l f`           → 整条被拒


@pytest.mark.parametrize(
    "command,expected",
    [
        ("head -5 f.txt", "Get-Content f.txt -TotalCount 5"),
        ("head -n 5 f.txt", "Get-Content f.txt -TotalCount 5"),
        ("head -n5 f.txt", "Get-Content f.txt -TotalCount 5"),
        ("tail -20 f.log", "Get-Content f.log -Tail 20"),
        ("tail -n 3 a.txt", "Get-Content a.txt -Tail 3"),
        ("wc -l f.txt", "(Get-Content f.txt).Count"),
    ],
)
def test_direct_readonly_limit_is_translated_and_passes_gate(gate_on, command, expected):
    """★ ① 验收：直接形态必须被**译**，且译出来的命令能过 gate。

    ⚠ 两头都断：只断"译了"不够 —— 译出一个**仍然被拒**的命令等于没修
    （第二段断言就是防这个）。
    """
    got = try_readonly_limit_translation(command)
    assert got is not None, f"{command!r} 没被译（修前：整条被拒）"
    translated, original = got
    assert translated == expected, translated
    assert original == command, "必须把原命令一并带回（日志/回执要用）"
    # ⚠ **必须同时断"链真的接了它"**（阳性对照 A 暴露的缺口）：只断函数能译，
    # 那么"函数写好了但没接进 `try_dialect_translation`"照样全绿 —— 而生产走的是链。
    chain = try_dialect_translation(command)
    assert chain is not None and chain[0] == expected, (
        f"翻译器没接进链（生产路径不过这个函数）：chain={chain!r}"
    )
    # 译完必须真的能过门（否则"译了等于没译"）
    assert detect_untranslated_unix(translated) is None, (
        f"译出来的 {translated!r} 仍被 gate 拒 ⇒ agent 还是走不通"
    )


@pytest.mark.parametrize(
    "command",
    [
        "wc -c f.txt",              # 字节：等价物不同（不是 .Count）⇒ 不译
        "head -5 f.txt > out.txt",  # 重定向
        "head -5 f.txt && echo ok",  # 复合命令
        "head -5 'my file.txt'",    # 带空格的引号路径（本翻译器不解析引号）
        "head -5 -v f.txt",         # 额外 flag
        "grep x f.txt",             # 不是只读限流词族
        "ls -la",                   # 类 2：同名不同语义
        "head -5 f.txt | sort",     # 管道尾不是 head/tail/wc ⇒ 链也不接
    ],
)
def test_direct_translation_is_conservative(gate_on, command):
    """★ 反向对照：**猜不准就不译** —— 宁可维持"拒绝 + 教学"。

    翻译器只在能**完整确认**整条命令形状时才动手；猜错 = 把 agent 的命令改坏，
    比拒绝更糟（拒绝至少给了正确的写法）。每条都必须**仍然被拒**（不能悄悄放行）。
    """
    assert try_readonly_limit_translation(command) is None, command
    assert detect_untranslated_unix(command) is not None, (
        f"{command!r} 既没被译也没被拒 —— 有东西被悄悄放行了"
    )


def test_direct_translation_never_touches_already_legal_commands():
    """★ 硬不变式：**只译"本来就会被拒的"**。

    入口先跑 `detect_untranslated_unix`；本来就放行的命令一律 `None`。
    ⇒ 这个翻译器**不可能**改变任何合法命令的行为（改动面严格是"+译"）。
    """
    for legal in (
        "Get-Content x -TotalCount 80", "python script.py", "echo hello world",
        "git status", "cd src; Get-ChildItem", "ls", "cat f.txt",
        "uv run pytest -q", "npm test",
    ):
        assert detect_untranslated_unix(legal) is None, legal
        assert try_readonly_limit_translation(legal) is None, (
            f"{legal!r} 本来是合法命令，却被改了 —— 违反「只译会被拒的」不变式"
        )


def test_translation_chain_has_exactly_one_entry_point():
    """★ **链只能有一份**：两处调用点都走 `try_dialect_translation`。

    为什么单独钉：原先两个调用点各自直接调 `try_closed_pipe_translation`；加第二个
    翻译器时若在两边各加一行，就又长成"每处各列一份清单"（本仓在事实位白名单上
    栽过两次）。⇒ 断计数：链被调 **2** 次（execute_bash + run_command），
    而管道尾翻译器**只在链内部**被调 **1** 次。
    """
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1] / "src" / "hiveweave" / "tools" / "bash.py"
    ).read_text(encoding="utf-8")
    assert src.count("= try_dialect_translation(command)") == 2, (
        "方言转译的调用点不是 2 处 —— 有人绕过链直接调子翻译器了"
    )
    assert src.count("= try_closed_pipe_translation(command)") == 1, (
        "管道尾翻译器被直接调用（应只在 `try_dialect_translation` 链内部）"
    )


def test_chain_order_is_pipe_tail_first(gate_on):
    """链的顺序：管道尾 → 直接形态（先试更具体的那个）。"""
    command = "cat f.txt | head -5"
    got = try_dialect_translation(command)
    assert got is not None and got[0] == "cat f.txt | Select-Object -First 5", got
    assert try_closed_pipe_translation(command) is not None, "管道尾那半没变"


# ── ① 的审计处置（2026-09-16）：M-1 / M-2 / D-1 / D-4 / D-5 ──────────


@pytest.mark.parametrize(
    "command",
    [
        "head -5 .hiveweave/data.db",
        "tail -20 .hiveweave/env.sh",
        "wc -l .hiveweave/data.db",
        "head -3 .hiveweave/config",
    ],
)
def test_translation_refuses_protected_hiveweave_targets(gate_on, command):
    """★ **M-1（审计必修）**：目标落在 `.hiveweave` 非读放行面 ⇒ **不译**。

    为什么这条是必修：`.hiveweave` 护栏（`_check_hiveweave_command`）按**动词**匹配
    （`cat`/`rm`…），`head`/`tail`/`wc` **不在**动词表里；而 `Get-Content` 是被
    **刻意排除**的只读 cmdlet（否则 `.hiveweave/logs` 的只读放行会被关掉）。
    ⇒ 不加守卫时，`head -5 .hiveweave/data.db` 在**改动前**被方言门拦下（顺带保住
    了这条策略）、在**改动后**会译成 `Get-Content … -TotalCount 5` 并**执行** ——
    那是本改动**新开的一条读受保护文件的路径**。
    ⇒ 必须退回"拒 + 教学"（= 改动前行为）。
    """
    assert try_readonly_limit_translation(command) is None, (
        f"{command!r} 被译了 —— 这会新开一条读 .hiveweave 受保护文件的路径"
    )
    assert try_dialect_translation(command) is None, command
    assert detect_untranslated_unix(command) is not None, (
        f"{command!r} 既不译也不拒 —— 有东西被悄悄放行了"
    )


@pytest.mark.parametrize(
    "command",
    [
        "head -5 .hiveweave/logs/dev-server-a.log",   # 只读例外（诊断出口）
        "head -5 .hiveweave/shared/plan.md",          # 允许子树
    ],
)
def test_translation_allows_read_cleared_hiveweave_targets(gate_on, command):
    """反向对照：**读放行面内**的 `.hiveweave` 目标照常译。

    没有这条，"一律不译含 .hiveweave 的命令"也能让上一条绿 —— 那是把
    诊断出口（logs）与共享目录（shared）一起关掉。
    """
    got = try_readonly_limit_translation(command)
    assert got is not None, f"{command!r} 属读放行面，应该照译"
    assert got[0].startswith("Get-Content "), got


@pytest.mark.parametrize("command", ["head -n -5 f.txt", "tail -n -3 f.txt"])
def test_negative_line_count_is_not_translated(gate_on, command):
    """★ **D-1**：`head -n -5 f` 在 bash 里是「除最后 5 行以外」，不是「前 5 行」。

    初版正则的 `(?:-n\\s*)?-?` 会把负号吞掉、译成 `-TotalCount 5` —— 静默给错结果，
    比拒绝更糟（本文件自己的判据：猜错 = 把命令改坏）。⇒ 不译、退回教学。
    """
    assert try_readonly_limit_translation(command) is None, command
    assert detect_untranslated_unix(command) is not None, command


@pytest.mark.parametrize(
    "command,needles",
    [
        ("head -5 f.txt", ("Get-Content", "-TotalCount")),
        ("tail -20 f.txt", ("Get-Content", "-Tail")),
        ("wc -l f.txt", (".Count",)),
    ],
)
def test_translation_tokens_match_the_taught_hint(command, needles):
    """**D-4**：译出的形态必须与 `UNIX_ONLY_HINTS` **教给 agent 的写法同源**。

    注释里声称"逐字同源"，而实现是两处手抄 ⇒ 加这条断言钉住，否则改了 hint 表
    会静默漂移（本仓在"清单双写"上栽过两次；`shell_dialect.py` 的修改纪律明写
    "两边不允许再出现手抄副本"）。
    """
    from hiveweave.tools.shell_dialect import UNIX_ONLY_HINTS

    got = try_readonly_limit_translation(command)
    assert got is not None, command
    translated = got[0]
    for needle in needles:
        assert needle in translated, translated
    # hint 里必须出现同一批 token（两边指同一件事）
    hint_key = "head" if command.startswith("head") else (
        "tail" if command.startswith("tail") else "wc"
    )
    hint = UNIX_ONLY_HINTS[hint_key]
    for needle in needles:
        assert needle in hint, (
            f"hint 表里 {hint_key!r} 的写法变了（{hint!r}），而翻译器仍产出 "
            f"{translated!r} —— 两处已漂移，请同步（它们指的是同一件事）"
        )


def test_wc_paths_are_extracted_like_head(gate_on):
    """**D-5**：`wc` 与 `head`/`tail` 同族，路径提取不能漏它。

    漏了 `wc` ⇒ `wc -l .env` 的路径**不进敏感路径检查**（而 `head -5 .env` 会进）
    ⇒ 同一族命令两套判据。这是**路径提取**（不是词表），补进去是收口而不是加词表。
    """
    from hiveweave.tools.bash import _extract_file_paths_from_command

    for cmd in ("head -5 .env", "wc -l .env", "tail -3 .env"):
        assert _extract_file_paths_from_command(cmd) == [".env"], cmd


def test_llm_facing_descriptions_do_not_claim_no_translation():
    """★ **M-2（审计必修）**：**模型实际读到的那一份**文案不得再声称"no translation"。

    为什么必修：`@tool` 注册表里的 description 是**被遮蔽的**那份 ——
    `get_tool_description` / `get_tool_schema_for_llm` 优先取 `TOOL_PARAM_SCHEMAS`。
    ⇒ 只改 `@tool` 那份 = 模型仍然读到与实现相反的承诺（审计实测两份内容不一致）。
    """
    from hiveweave.tools.executor import get_tool_description

    desc = get_tool_description("bash")
    assert desc, "拿不到模型面的 bash 描述"
    assert "no unix" not in desc.lower(), (
        "模型面仍在声称「no unix→pwsh translation is applied」—— 与实现相反"
    )
    assert "verbatim — no unix→pwsh translation" not in desc, desc
    # 且必须**如实**说明有窄集合转译（否则模型不知道可以直写 head -N）
    assert "auto-translation" in desc or "translated" in desc.lower(), desc
