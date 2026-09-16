"""#15 残余（E19 / E20 / E21 / E23）—— 2026-09-16。

这四条都是「**平台写了事实位/判据，但没有消费者或覆盖不全**」的同族：
E19 判据表太宽（反而误判）、E20 位被判据层自己丢掉、E21 判据可被静默关掉、
E23 有分子没有分母。四条各自的验收都在下面逐条钉住。

⚠ 本文件里的例子文本**逐字取自 58/59 的真实 `run_steps.error`**（去掉换行），
不是编的 —— 「用真实语料当夹具」正是这一族缺陷唯一能证伪的方式。
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from hiveweave.tools.fact_positions import (
    RUNNER_FAILURE_SIGNATURES,
    classify_error_text,
    finalize_tool_result,
)

_SRC = Path(__file__).resolve().parents[1] / "src" / "hiveweave"


# ══════════════════════════════════════════════════════════════════
# E19：runner 签名表**不得**命中「命令跑了但失败」的内容
# ══════════════════════════════════════════════════════════════════
#
# 实测（58/59，在**有位可判**的行上，位=地面真值）：
#   改动前：136 条里文本层判 runner 的 75 条，其中**冲突 36 条**（与
#   `command_failed` 位矛盾）；冲突只由 4 个 needle 造成 ——
#   `spawn`(25) / `approval`(6) / `permission`(4) / `does not exist`(1)。
#   改动后：冲突 **0**。

#: 改动前每一条冲突现场的**真实原文**（缩写到 200 字内）。
_MEASURED_CONFLICT_TEXTS = (
    # 命中裸 "approval"：这是**命令输出里的 checkpoint 摘要**
    "Command exited with code 1 [stdout tail] 7eff63a checkpoint: M5 "
    "post-approval cleanup checkpoint eabaa7b Merge branch 'main'",
    # 命中裸 "permission"：这是命令自己的 stderr
    "Command exited with code 1 [stdout tail] 1472 [stderr tail] fatal: "
    "cannot create directory at '.hiveweave/reports/x': Permission denied",
    # 命中裸 "does not exist"：这是命令输出的内容
    "Command exited with code 1 [stdout tail] === HEADs === "
    "8c199204f481f8a8c5063ec77f7bd1de80d4419b file does not exist here",
    # 命中裸 "spawn"：命令输出里夹了 spawn 字样的进度行
    "Command exited with code 1 [stdout tail] building... will spawn worker "
    "pool size=4 done",
)


@pytest.mark.parametrize("text", _MEASURED_CONFLICT_TEXTS)
def test_measured_conflict_texts_are_not_runner_failed(text):
    """★ E19 验收：这些**命令输出**不得被判成「命令从未执行」。"""
    assert classify_error_text(text) != "runner_failed", text


def test_broad_needles_are_gone_and_platform_phrasings_remain():
    """收窄必须**两头都钉**：宽的去掉、平台自己的措辞留住。

    只钉"宽的没了"会退化成"全删" ⇒ 判据失效；只钉"平台措辞在"会挡住收窄。
    """
    table = set(RUNNER_FAILURE_SIGNATURES)
    for bad in ("spawn", "approval", "permission", "does not exist", "dialect"):
        assert bad not in table, (
            f"裸 {bad!r} 又回到表里 —— 它会在**命令输出**上误命中（实测有现场）"
        )
    for good in (
        "command blocked",                     # 平台护栏前缀
        "working directory does not exist",    # 平台 cwd 文案
        "not available in this shell",         # 方言门
        "approval_channel_unavailable",        # 审批通道（平台自产）
        "failed to spawn:",                    # process_registry.py:989
        "failed to spawn shell",               # bash.py:1792
        "no tool executor",                    # tool_loop.py:1097
    ):
        assert good in table, good


@pytest.mark.parametrize(
    "text",
    (
        "Error: Command blocked: 自毁命令（system-level destructive command）",
        "Error: Working directory does not exist: [worktree A075 .hiveweave/...]",
        "Error: unix-only command(s) not available in this shell — on Windows "
        "the sandbox executes bash via pwsh",
        "Error: Command blocked: [approval_channel_unavailable] 审批请求超时："
        "审批通道无应答 （超时 120s，未批准也未拒绝）",
        "Failed to spawn shell: [WinError 5] Access is denied",
        "[No tool executor] tool loop has no executor",
        # 平台权限拒绝的两种真实措辞（`executor.py:3284/3355`、`pipeline.py:336/421`）
        "Permission rejected: user said no",
        "Error: Permission check failed: db down",
    ),
)
def test_platform_guard_texts_still_classify_as_runner(text):
    """★ 反向对照：**平台自产的护栏/前提文案**仍必须判 runner_failed。

    没有这条，收窄就能靠"把表删空"来满足上一条 —— 那是把功能关掉冒充修缺陷。
    ⚠ 这两条 `Permission …` 原本由裸 `"permission"` 兜着；收窄后必须由
    **平台专有措辞**（带 `rejected:` / `check failed:`）接住 —— 这正是"收窄"
    与"删掉"的区别。
    """
    assert classify_error_text(text) == "runner_failed", text


def test_command_output_permission_shapes_are_not_runner():
    """★ E19 的另一半：**命令自己的**权限措辞不得被判 runner。

    与上一条配对：`Permission denied`（命令 stderr）≠ `Permission rejected:`
    （平台护栏）。两者的区别正是"措辞" —— 而这条恰恰是文本判据的**上限**：
    它靠的是平台措辞足够特殊，不是靠判据更强。
    """
    for text in (
        "Command exited with code 1 [stderr tail] cat: /root/x: Permission denied",
        "Command exited with code 1 [stderr tail] fatal: Permission denied: /repo/x",
        "Command exited with code 1 [stderr tail] Access is denied.",
    ):
        assert classify_error_text(text) != "runner_failed", text


# ══════════════════════════════════════════════════════════════════
# E20：`command_failed` 位不得被判据层自己丢掉
# ══════════════════════════════════════════════════════════════════


def test_command_failed_bit_is_promoted_to_fact_not_dropped():
    """★ E20 验收：裸 `command_failed=True`（无 `fact`）必须被**归一成 fact**。

    原来只认 `runner_failed`，`command_failed` 被无条件 `pop` 掉 ⇒ 位永久丢失：
    没有 fact 就派生不出位，下游只看到"没有位"，于是回落到**文本层**兜底 ——
    而文本层在有 stdout 的命令回执上正是 E19 那条不可靠的判据。两处叠一起，
    「命令跑了但失败」会被静默升级成「命令从未执行」（下游读作"可放心重试"）。
    """
    from hiveweave.tools.bash import _shell_tool_result

    res = _shell_tool_result(
        success=False, blocked=False, output="", error="boom",
        banner="", suffix="", public={},
        fact_flags={"command_failed": True},
    )
    d = res.to_dict()
    assert d["fact"] == "command_failed", d
    assert d["command_failed"] is True, d
    assert d.get("runner_failed") is not True, d

    # 反向对照：`runner_failed` 那条兼容路径照旧（不能只顾新的那条）
    res2 = _shell_tool_result(
        success=False, blocked=False, output="", error="boom",
        banner="", suffix="", public={},
        fact_flags={"runner_failed": True},
    ).to_dict()
    assert res2["fact"] == "runner_failed", res2

    # 两者同时给时取更保守的 runner（语义：命令从未执行）
    res3 = _shell_tool_result(
        success=False, blocked=False, output="", error="boom",
        banner="", suffix="", public={},
        fact_flags={"runner_failed": True, "command_failed": True},
    ).to_dict()
    assert res3["fact"] == "runner_failed", res3


def test_explicit_fact_still_wins_over_legacy_bits():
    """`fact` 是权威：显式给 fact 时裸位不得覆盖它。"""
    from hiveweave.tools.bash import _shell_tool_result

    d = _shell_tool_result(
        success=False, blocked=False, output="", error="boom",
        banner="", suffix="", public={},
        fact_flags={"fact": "bad_args", "command_failed": True},
    ).to_dict()
    assert d["fact"] == "bad_args", d


# ══════════════════════════════════════════════════════════════════
# E21：归因阶梯不得有"潜式 opt-out"
# ══════════════════════════════════════════════════════════════════


def test_finalize_tool_result_has_no_opt_out_switch():
    """★ E21 验收：`judge_blocked` 这个开关必须不存在。

    它是个**潜式 opt-out**：调用方传 False 就能静默跳过整个归因阶梯，而
    **没有任何一处会因此报警**（守卫只看"声明的 fact 是否合法"，不看"有没有
    走归因"）。全仓核查过没有调用方传过它 ⇒ 是一枚休眠旁路。
    """
    params = inspect.signature(finalize_tool_result).parameters
    assert "judge_blocked" not in params, params
    with pytest.raises(TypeError):
        finalize_tool_result("bash", {}, judge_blocked=False)  # type: ignore[call-arg]


def test_no_judge_blocked_identifier_left_in_the_module():
    """AST 守卫：模块里不得再有名为 `judge_blocked` 的**参数/变量**。

    只查标识符用途（docstring 里的说明不算）—— 防"改个默认值就当成删掉了"。
    """
    tree = ast.parse((_SRC / "tools" / "fact_positions.py").read_text("utf-8"))
    found: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(a.arg == "judge_blocked" for a in node.args.args + node.args.kwonlyargs):
                found.append(node.lineno)
        if isinstance(node, ast.Name) and node.id == "judge_blocked":
            found.append(node.lineno)
        if isinstance(node, ast.Attribute) and node.attr == "judge_blocked":
            found.append(node.lineno)
    assert not found, f"judge_blocked 仍在代码里（行 {found}）"


# ══════════════════════════════════════════════════════════════════
# E23：样本要有**分母**与比例
# ══════════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def _clean_samples():
    from hiveweave.llm.unknown_error_samples import clear_unknown_samples

    clear_unknown_samples()
    yield
    clear_unknown_samples()


def test_stats_ratio_is_none_when_no_judgement_recorded():
    """★ E23：没有分母时 `ratio` 必须是 **None**，不是 0.0。

    0 会被读成"覆盖率完美"，而真相是"还没有数据" —— 用默认值冒充结论是
    本仓的既有教训（`started` 列的 `DEFAULT 0` 那条）。
    """
    from hiveweave.llm.unknown_error_samples import (
        note_unknown_sample,
        unknown_sample_stats,
    )

    note_unknown_sample(source="t", status=None, body="x")
    st = unknown_sample_stats()
    assert st["total"] == 1 and st["judged"] == 0
    assert st["unknownPerJudgedCall"] is None, st


def test_stats_reports_ratio_and_per_family_denominators():
    from hiveweave.llm.unknown_error_samples import (
        note_judgement,
        note_unknown_sample,
        unknown_sample_stats,
    )

    for _ in range(4):
        note_judgement("http_error")
    for _ in range(2):
        note_judgement("fact_position")
    note_unknown_sample(source="classify_http_error", status=418, body="teapot")
    st = unknown_sample_stats()
    assert st["judged"] == 6, st
    assert st["judgedByFamily"] == {"fact_position": 2, "http_error": 4}, st
    assert st["unknownPerJudgedCall"] == round(1 / 6, 4), st
    assert st["bySource"] == {"classify_http_error": 1}, st


def test_classify_http_error_records_a_judgement():
    """分母接在**判定动作**上：分类一次记一次（漏记与真没判定要不同形）。"""
    from hiveweave.llm.retry import classify_http_error
    from hiveweave.llm.unknown_error_samples import unknown_sample_stats

    classify_http_error(429, "slow down")
    classify_http_error(403, "forbidden")
    st = unknown_sample_stats()
    assert st["judgedByFamily"].get("http_error") == 2, st


def test_finalize_tool_result_records_a_judgement():
    from hiveweave.llm.unknown_error_samples import unknown_sample_stats

    finalize_tool_result("bash", {"success": False, "error": "boom"})
    st = unknown_sample_stats()
    assert st["judgedByFamily"].get("fact_position") == 1, st


@pytest.mark.asyncio
async def test_debug_metrics_exposes_the_denominator():
    """/api/debug/metrics 必须暴露比例指标（E23 的「端点」那一半）。"""
    from hiveweave.api.debug import debug_metrics

    body = await debug_metrics()
    assert "unknownSamples" in body, body
    us = body["unknownSamples"]
    assert {"total", "judged", "unknownPerJudgedCall", "judgedByFamily"} <= set(us), us
    # ⚠ 名字刻意不叫 `ratio`（审计 D5）：分子分母不同源，叫 ratio 会被读成"覆盖率"
    assert "ratio" not in us, us


# ══════════════════════════════════════════════════════════════════
# 审计处置（2026-09-16）—— 三处**必须修**的守卫
# ══════════════════════════════════════════════════════════════════


def test_fallback_scope_is_all_tools_not_just_shell():
    """★ 审计 M1 守卫：非 shell 工具漏声明 fact 时，兜底 + 样本都必须生效。

    这是删 `judge_blocked` 时踩到的真坑：旧写法
    `if tool_name in SHELL_SECURITY_LEVEL_TOOLS or judge_blocked:` 里
    `judge_blocked` **恒为 True** ⇒ 作用域是**全工具**。若把它"顺手收窄成
    shell 家族"：① 打红 `test_p0_3_orphan_root_cause.py`（非 shell 工具拿不到
    兜底 `outcome_unknown` ⇒ `out` 连 `fact` 键都没有）；② 掐断
    `fact_position` 族样本的**唯一来源**（58/59 实测 104 条未分类样本
    **100% 来自非 shell 工具**）⇒ E23 新加的分母会对应一个恒 0 的分子。
    """
    out = finalize_tool_result("read_file", {"success": False, "error": "boom"})
    assert out.get("fact") == "outcome_unknown", out
    assert out.get("unclassified_sample"), out


def test_spawn_failure_declares_runner_failed_bit():
    """★ 审计 M2：`spawn 失败` = 命令从未执行 ⇒ **构造点声明位**，不靠文本。

    实测过的错法：`_run_native` 的两条 spawn 失败出口 `blocked=False`，而文本表
    **只在 blocked 分支被咨询** ⇒ 那两条 needle 永不生效、结果落
    `outcome_unknown`（"结果未知、别盲目重试"），而 `exit_code is None`
    已经明确说明进程没起来。判据应当来自**状态**。
    """
    from hiveweave.tools.bash import _run_native

    out = finalize_tool_result(
        "bash",
        {"success": False, "exit_code": None, "output": "",
         "fact": "runner_failed",
         "error": "Failed to spawn shell: [WinError 2] not found"},
    )
    assert out["fact"] == "runner_failed", out
    # 并钉住真实实现：`_run_native` 的 spawn 失败必须带该位（源码级过一遍）
    src = (_SRC / "tools" / "bash.py").read_text("utf-8")
    assert src.count('"fact": "runner_failed",') >= 2, (
        "_run_native 的 spawn 失败出口丢了 fact 声明（构造点必须声明位）"
    )
    assert _run_native is not None


def test_python_script_spawn_failure_declares_runner_failed():
    """★ 审计 T2：`python_script` 的 spawn 失败同样要声明位。

    它在 `SHELL_SECURITY_LEVEL_TOOLS` 之外，以前**既没有位、也没有文本可兜**
    ⇒ 落 `outcome_unknown`（58/59 里 python_script 有 2/7 条未分类样本）。
    """
    out = finalize_tool_result(
        "python_script",
        {"success": False, "exit_code": None, "output": "",
         "fact": "runner_failed",
         "error": "Failed to spawn python: [WinError 2] not found"},
    )
    assert out["fact"] == "runner_failed", out
    src = (_SRC / "tools" / "python_script.py").read_text("utf-8")
    assert '"fact": "runner_failed",' in src, "python_script 的 spawn 出口丢了位"
    assert 'fact=result.get("fact")' in src, (
        "python_script 的出口没把位传出去 —— 位在这一层被丢掉"
    )


def test_shell_fact_flag_whitelist_is_single_and_covers_dialect():
    """★ 审计 T1：shell 事实位白名单必须**只有一份**，且含 `dialect_failed`。

    两处出口（`_shell_tool_impl` / `run_command_tool`）曾各列一份，而
    `dialect_failed` 只在其中一份里 ⇒ `run_command` 的方言门失败会退化成通用
    "命令未运行（执行器/方言/权限/审批）"文案，而生产者明明写了位。
    """
    from hiveweave.tools import bash as bash_mod

    keys = set(bash_mod._SHELL_FACT_FLAG_KEYS)
    assert "dialect_failed" in keys, keys
    assert {"fact", "runner_failed", "command_failed", "git_hardened"} <= keys, keys
    src = (_SRC / "tools" / "bash.py").read_text("utf-8")
    assert src.count("_SHELL_FACT_FLAG_KEYS") == 3, (
        "白名单使用点不是「1 处定义 + 2 处出口」—— 有人又各列了一份清单"
    )


def test_blocked_with_legacy_command_failed_keeps_fact_drops_blocked():
    """审计 D4：`blocked=True` + 裸 `command_failed=True` 这一格的行为要钉住。

    改动的方向（保留 fact、摘掉 blocked）与 L6/L19 既有策略一致 ——
    `command_failed` 是**调用方的责任**，标成平台护栏拒绝会让 agent
    收到「不是你的 bug」并原地重撞。今天该格**不可达**（全仓无裸
    `command_failed` 生产者），但把它钉住免得日后改坏没人知道。
    """
    from hiveweave.tools.bash import _shell_tool_result

    d = _shell_tool_result(
        success=False, blocked=True, output="", error="boom",
        banner="", suffix="", public={},
        fact_flags={"command_failed": True},
    ).to_dict()
    assert d["fact"] == "command_failed", d
    assert d["blocked"] is False, (
        "command_failed 属调用方责任，不得标成平台护栏拒绝"
    )
