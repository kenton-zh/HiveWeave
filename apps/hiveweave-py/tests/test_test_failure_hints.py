"""测试类失败的**提示链**守卫（`bash.py::_maybe_append_test_hints`）。

## 为什么补这个文件
这两条提示此前**一条守卫都没有**（实测 `grep -rln "Sandbox anchor hint" tests/` = 0 命中）
—— 而它们是"撞墙的 agent 唯一拿得到的指路信息"。`#18` 又往里加了第三条，
正好一次把整条链纳入网。

## 链的顺序与职责
1. `_maybe_append_test_anchor_hint`（B-1 P1-1 ②，既有）：
   测试类命令 + 权限/拒绝类失败 ⇒ 指**可写锚点**（$env:TEMP 私有目录、共享缓存换 cache-dir）；
2. `_maybe_append_node_isolation_hint`（#18，2026-09-16）：
   node 测试运行器 + **EPERM** ⇒ 指**同进程隔离**（`--experimental-test-isolation=none`）；
3. `_maybe_append_spawn_eperm_hint`（T35，D85 实测）：
   回执含 **spawn 痕迹 + EPERM** ⇒ 一次交底 **Windows 沙箱 spawn 边界**
   （cmd.exe/.cmd shim 与沙箱内子进程不可用；直调目标 exe 不受影响；
   依赖安装走平台白名单通道或 vendor），幂等。

⚠ 两条都是**失败提示（advisory）**：只用"命令里有什么 + 输出里有什么"这两个**观测**事实，
不做门禁、不推断意图 ⇒ 文本判据在这里是**可接受**的（本仓禁用文本判据针对的是**门禁**）。
即便如此，本文件仍把"不该出提示的形态"逐条钉住（防噪声扩散）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hiveweave.tools.bash import (
    _maybe_append_node_isolation_hint,
    _maybe_append_spawn_eperm_hint,
    _maybe_append_test_anchor_hint,
    _maybe_append_test_hints,
)

_ANCHOR = "Sandbox anchor hint"
_NODE_NOTE = "Sandbox pipe boundary"
_ISOLATION_FLAG = "--experimental-test-isolation=none"
_SPAWN_NOTE = "Windows sandbox spawn boundary"


# ── 老提示（回归保护：此前无守卫）────────────────────────────────


def test_anchor_hint_still_fires_for_pytest_permission_wall():
    """★ 回归：pytest + `Access is denied` ⇒ 锚点提示必须**照旧出**。"""
    out = _maybe_append_test_hints("pytest -q", "OSError: [WinError 5] Access is denied")
    assert _ANCHOR in out, out
    assert _NODE_NOTE not in out, "pytest 撞权限墙不该出 node 的管道提示"


@pytest.mark.parametrize(
    "command,error",
    [
        ("node --test t.js", "1 failing: expected 1 to equal 2"),
        ("pytest -q", ""),
        ("git status", "fatal: not a git repository"),
    ],
)
def test_no_hint_when_nothing_matches(command: str, error: str):
    """反向对照：**没命中就一个字都不加**（提示必须是零扰动的旁支）。

    ⚠ 有意变更（T35）：原参数表里的 `("", "spawn EPERM")` 不再"零扰动"——
    spawn 类 EPERM 现在正是 spawn 边界处方的触发面（与命令文本无关），该形态
    移入下方 `test_spawn_eperm_prescription_*` 正向用例。
    """
    assert _maybe_append_test_hints(command, error) == error


# ── T35（D85）：spawn 类 EPERM ⇒ Windows 沙箱 spawn 边界处方 ────────────


@pytest.mark.parametrize(
    "command,error",
    [
        ("npm install", "Error: spawnSync C:/WINDOWS/system32/cmd.exe EPERM"),
        ("node --test t.js", "error: 'spawn EPERM'\n  code: 'EPERM'"),
        ("pnpm build", "Error: spawn cmd.exe EPERM"),
        ("uv run pytest -q", "operation not permitted: spawn python EPERM"),
    ],
)
def test_spawn_eperm_prescription_fires(command: str, error: str):
    """★ T35 验收：回执 error 含 spawn 痕迹 + EPERM ⇒ 必须追加沙箱 spawn 边界处方。

    D85 实测：agent 在回执只有 node 报错时烧 3+ 轮自行摸边界（cmd.exe shim
    被拦 / node 直跑放行 / 子进程 spawn 全拦）——处方把边界与出路一次交底。
    判据与命令文本无关（error 面观测），空 command 也必须命中。
    """
    for msg in (error, _maybe_append_test_hints(command, error)):
        out = _maybe_append_spawn_eperm_hint(command, msg)
        assert _SPAWN_NOTE in out, out
        # 处方三要素：不可用面 / 不受影响面 / 依赖安装出路
        assert "cmd.exe" in out and "node.exe" in out, out
    # 必须同时断"链真的接了它"（生产走链，不走单函数）
    chain = _maybe_append_test_hints(command, error)
    assert _SPAWN_NOTE in chain, (
        f"spawn 处方没接进链（生产路径不过这个函数）：{chain[:200]!r}"
    )


def test_spawn_eperm_prescription_idempotent():
    """幂等：同回执（已带处方的文本再进链/再进函数）不重复追加。"""
    once = _maybe_append_test_hints("npm install", "Error: spawn cmd.exe EPERM")
    assert once.count(_SPAWN_NOTE) == 1, once
    twice = _maybe_append_test_hints("npm install", once)
    assert twice == once, "同回执被重复追加处方"
    also_once = _maybe_append_spawn_eperm_hint("npm install", once)
    assert also_once == once


def test_spawn_eperm_prescription_not_fired_on_other_failures():
    """反向对照：普通失败 / 文件锁型 EPERM（无 spawn 痕迹）⇒ 不追加 spawn 处方。

    文件锁型 EPERM 的正确出路是锚点提示（换 cache/temp），给它 spawn 处方
    会把人推向错误方向（F1 审计同款误诊防线）。
    """
    for command, error in (
        ("npm test", "1 failing: expected 1 to equal 2"),
        ("git status", "fatal: not a git repository"),
        ("pytest -q", "Error: EPERM: operation not permitted, unlink "
                      "'<ws>/node_modules/.cache/x'"),
        ("", ""),
    ):
        out = _maybe_append_spawn_eperm_hint(command, error)
        assert _SPAWN_NOTE not in out, (command, error, out)


def test_spawn_prescription_coexists_with_node_note_and_lock_stays_anchor_only():
    """★ 组合面不回归：node --test 撞 spawn EPERM ⇒ 两条处方并存（互补不互斥）；
    文件锁型 EPERM ⇒ 仍然只有锚点提示、绝不出 spawn 处方（F1 基线保持）。"""
    spawn_out = _maybe_append_test_hints("node --test t.js", "error: 'spawn EPERM'")
    assert _NODE_NOTE in spawn_out and _SPAWN_NOTE in spawn_out, spawn_out
    lock_err = ("Error: EPERM: operation not permitted, unlink "
                "'<ws>/node_modules/.cache/x'")
    lock_out = _maybe_append_test_hints("node --test t.js", lock_err)
    assert _ANCHOR in lock_out, lock_out
    assert _SPAWN_NOTE not in lock_out, (
        "文件锁型 EPERM 被误诊成 spawn 边界（会让人放弃换 cache/temp 的正确修法）"
    )


# ── #18 新提示 ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "error",
    [
        "error: 'spawn EPERM'\n  code: 'EPERM'",
        "Error: spawnSync C:/WINDOWS/system32/cmd.exe EPERM",
        "operation not permitted: spawn node EPERM",
    ],
)
def test_node_isolation_hint_fires_for_node_test_plus_eperm(error: str):
    """★ #18 验收：node 测试运行器 + EPERM ⇒ 必须指出**同进程隔离**这条出路。

    为什么必须有这条：这条边界的 flag 空间里**只有隔离开关**能绕开（实测：
    `--experimental-test-isolation=none` 让 `node --test` 在受限沙箱内从 `fail 1`
    变成 `# pass 1 / # fail 0`）⇒ 不指路，agent 只会换 flag 白烧轮次。
    """
    out = _maybe_append_node_isolation_hint("node --test t.js", error)
    assert _NODE_NOTE in out, out
    assert _ISOLATION_FLAG in out, out
    # 必须同时说清"这是平台边界、不是你的代码"（否则 agent 会去改代码/重试）
    assert "platform" in out.lower() and "not a defect" in out.lower(), out
    # ⚠ **必须同时断"链真的接了它"**（阳性对照 A 暴露的缺口）：只断函数本身，
    # 那么"函数写好了但没接进 `_maybe_append_test_hints`"照样全绿 —— 而生产走的是链。
    chain = _maybe_append_test_hints("node --test t.js", error)
    assert _NODE_NOTE in chain, (
        f"node 提示没接进链（生产路径不过这个函数）：{chain[:200]!r}"
    )


@pytest.mark.parametrize("command", ["pytest -q", "vitest run", "go test ./..."])
def test_node_isolation_hint_is_node_only(command: str):
    """反向对照：**非 node 的**测试运行器撞 spawn EPERM ⇒ 不该收到 node 的处方。

    （别的运行器的出路不同：vitest 用 worker 线程、pytest 走可写锚点 ——
    给错药方比不给更糟。）
    ⚠ **`npm test` / `pnpm test` / `yarn test` 不在此列**（F3 审计订正）：它们常
    转发到 `node --test`，撞的是同一条边界 ⇒ 必须收到同一条处方。原先把
    `npm test` 写进本条是本文件自己写错的断言。
    """
    out = _maybe_append_node_isolation_hint(command, "spawn EPERM")
    assert _NODE_NOTE not in out, out


def test_node_isolation_hint_does_not_fire_on_ordinary_failure():
    """反向对照：node 测试**普通失败**（断言不过）⇒ 不加提示（否则是噪声）。"""
    out = _maybe_append_node_isolation_hint(
        "node --test t.js", "not ok 1 - tiny\n  failureType: 'testCodeFailure'"
    )
    assert _NODE_NOTE not in out, out


def test_hint_chain_has_exactly_one_entry_and_both_sites_use_it():
    """★ **链只能有一份**：两个错误出口都必须走 `_maybe_append_test_hints`。

    为什么单独钉：原先两个调用点各自直接调 `_maybe_append_test_anchor_hint`；
    加第三条提示时若在两边各加一行，就又长成"每处各列一份清单"（本仓在事实位
    白名单上栽过两次）。⇒ 断计数。
    """
    src = (
        Path(__file__).resolve().parents[1] / "src" / "hiveweave" / "tools" / "bash.py"
    ).read_text(encoding="utf-8")
    assert src.count("= _maybe_append_test_hints(command, error_msg)") == 2, (
        "提示链的调用点不是 2 处（execute_bash / run_command 的错误出口）"
    )
    assert src.count("= _maybe_append_test_anchor_hint(command, error_msg)") == 0, (
        "有人绕过链直接调老提示函数了"
    )


# ── F1 / F3 / F5（#18 审计处置，2026-09-16）──────────────────────


def test_spawn_eperm_and_filelock_eperm_get_different_prescriptions():
    """★ **F1（审计必修）**：受限沙箱里 `EPERM` 的主频是**文件锁/共享缓存 unlink**，
    不是管道边界 ⇒ 两类 EPERM 必须拿到**不同**处方。

    为什么这条最关键：把文件锁型 EPERM 误诊成管道边界，会把 agent 从正确修法
    （换 cache 目录 / 换 fresh temp）推向错误方向 —— 而仓内那类失败有 **46 分钟税**
    的先例。
    """
    spawn_err = "error: 'spawn EPERM'\n  code: 'EPERM'"
    lock_err = ("Error: EPERM: operation not permitted, unlink "
                "'<ws>/node_modules/.cache/x'")

    spawn_out = _maybe_append_test_hints("node --test t.js", spawn_err)
    lock_out = _maybe_append_test_hints("node --test t.js", lock_err)

    assert _NODE_NOTE in spawn_out, spawn_out          # 管道边界 ⇒ 关隔离
    assert _NODE_NOTE not in lock_out, (
        "文件锁型 EPERM 被误诊成管道边界（会把人从'换 cache 目录'推向硬改测试）"
    )
    assert _ANCHOR in lock_out, (
        "文件锁型 EPERM 必须拿到**锚点提示**（换 cache/temp）—— 否则两条出路都断了：" + lock_out
    )


@pytest.mark.parametrize("command", ["npm test", "pnpm test", "yarn test"])
def test_package_manager_test_entries_get_the_node_prescription(command: str):
    """★ **F3**：`npm/pnpm/yarn test` 常转发到 `node --test` ⇒ 撞同一条边界。

    此前它们**一条提示都拿不到**（不在 node 命令表里、EPERM 也不在锚点判据里）。
    """
    out = _maybe_append_test_hints(command, "Error: spawn EPERM")
    assert _NODE_NOTE in out, out


def test_node_flag_name_variants_and_no_false_positive_tail():
    """★ **F5/Q4**：提示必须给**两个 flag 名**（版本相关）+ 明确"`bad option` 不是新故障"；
    且 `--test` 要有词尾边界（`node x --test-mode` 不该命中）。"""
    out = _maybe_append_node_isolation_hint("node --test t.js", "spawn EPERM")
    assert "--experimental-test-isolation=none" in out, out
    assert "--test-isolation=none" in out, "必须同时给出 23+ 的名字（版本相关）"
    assert "bad option" in out, (
        "必须提前打招呼：换了名字会报 bad option，那不是新故障（否则 agent 会把它当二次故障）"
    )
    # 绝对化措辞已被审计纠掉（对"测试自己 pipe spawn"不成立）
    assert "nothing else in the flag space" not in out, out

    # Q4：词尾边界
    tweak = _maybe_append_node_isolation_hint("node x --test-mode", "spawn EPERM")
    assert _NODE_NOTE not in tweak, f"`--test-mode` 误命中：{tweak}"
