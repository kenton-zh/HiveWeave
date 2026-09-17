"""#1 治本：agent 命令 spawn 的**唯一入口**契约 + 结构守卫。

## 被测的病灶（一手证据）

`docs/platform-issue-research/fixplan-16items-2026-09-14.md` §三 #1 与
`deliverables/text-judgment-escape-inventory-2026-09-14.md` 记录：沙箱路由是
**每个工具自己的约定**（`acl_sandbox_active()` 散落在 `bash.py` / `dev_server_tools.py`
/ `python_script.py` / `game_time.py`），后果有三：

1. `start_dev_server` **从来就 import 过** sandbox，照样以平台身份执行任意
   `params.command`（TEST_DSH_56 实证：叶子用它写出项目之外）；
2. **没人知道**某次 spawn 实际走了哪条路（结果里没有执行面字段）；
3. 漏接**不产生任何信号**——不报错、不告警，只是"看起来一切正常"。

更隐蔽的一条：同一个内部信号被各调用方**各自解释**。`spawn_confined` 返回
`None` 的真实语义是「判定为原生」，但四条路读成「沙箱关 ⇒ 回落原生」、
`python_script` 读成「沙箱坏 ⇒ 拒绝执行」。于是**项目级
`danger-full-access`（显式逃生门）下四跑一拒**。根因不是那一条写错了，
而是没有单一判定源。

## 本文件钉什么

| 用例 | 钉住的性质 |
|---|---|
| `test_reason_enum_is_closed_and_total` | 判定理由=**闭合枚举**，新增成员必须自带 (enforcement, level)，不许猜 |
| `test_default_is_confined_not_native` | 「未知」**不许**被读成「原生」（fail-safe 方向） |
| `test_python_script_does_not_refuse_danger_full_access` | ★ 跨路一致性回归：逃生门下 python_script **照样跑** |
| `test_confined_unavailable_must_not_fall_back_to_native` | 受限不可用 ⇒ 拒绝，**绝不**静默按原生再跑一遍 |
| `test_native_decision_passed_to_confined_is_rejected` | 判定为原生时传进受限执行器 ⇒ **ValueError**（约束住在操作内部） |
| `test_every_confined_call_carries_a_decision` | 结构守卫：受限路径**拿不到**「自己判出来」的许可 |
| `test_no_new_route_judgment_sites` | 结构守卫：`acl_sandbox_active()` 不再被新代码用来**决定路由** |
| `test_guard_scanners_are_not_vacuous` | 两个守卫的**阳性对照**（空扫描器也会全绿） |

## 守卫不守什么（本仓纪律：说「有测试守着」必须同时说明它不守什么）

- `test_every_confined_call_carries_a_decision` **不守**「调用方伪造一个
  confined 判定」—— `make_decision` 是唯一构造点且必须给理由（显式动作），
  但一个人可以写下 `make_decision(R_CONFINED)`。它守的是**默认路径**：
  写代码时想接受限执行，就必须显式表态走哪条路。
- 它**不守**工具是否把 `enforcement*` 戳透传到最终结果（那是各工具自己
  的事；bash/python_script 已接，日志侧由入口无条件盖戳兜底）。
"""

from __future__ import annotations

import ast
import pathlib
import sys
from unittest.mock import AsyncMock, patch

import pytest

import hiveweave.services.acl_sandbox.policy as policy

_SRC_ROOT = pathlib.Path(__file__).resolve().parents[1] / "src" / "hiveweave"
_DECISION_SEAM = "hiveweave.services.acl_sandbox.policy.resolve_spawn_decision"

# `acl_sandbox_active()` 的**合法存量**调用点（形如 `相对路径::函数名`）。
# 这些都不是路由判定，而是「平台状态视图」：
#   · bash.py::_pwsh_is_effective_shell —— legacy 零参近似（保留给 subagent 的
#     方言提示与既有测试；execute_bash/execute_run_command 已改传真实判定）
#   · main.py::lifespan —— 启动自检/遥测打点
#   · api/system.py::acl_sandbox_stats —— 状态端点
# 新增一个**用来决定 spawn 走哪条路**的调用点 = 本文件转红（#1 的复发形态）。
_ACTIVE_VIEW_ALLOWLIST: frozenset[str] = frozenset({
    "tools/bash.py::_pwsh_is_effective_shell",
    "main.py::lifespan",
    "api/system.py::acl_sandbox_stats",
})

# F5（§6.9.4，2026-09-18）：包内「不判 / 跳过」豁免从**目录前缀一刀切**收成
# **显式登记**（形态对齐 `_ACTIVE_VIEW_ALLOWLIST`）。现状唯一合法成员 =
# sentinel 探针：它用字符串形态 `build_confined_command` 构造探测命令、
# **不传 decision**、native 判定时返回 `None`（不是抛错），且**不消费**戳、
# 不进 run_steps —— 收编进 spawn_agent_command 反而会让探针**真的以平台
# 身份执行一次越权写**（§6.9.4 方案 2 的三条真成本之一，是安全倒退）。
# ⚠ 目录前缀豁免的危险形态：日后任何**新建**在 `services/acl_sandbox/` 下、
# 且直调 `spawn_confined` 的文件都会**静默继承**豁免 —— 收成登记后，
# 包内新增未登记直调点转红，而不是被豁免。
_CONFINED_DIRECT_CALL_ALLOWLIST: frozenset[str] = frozenset({
    "services/acl_sandbox/sentinel.py::_probe_via_spawn",
})


# ── 判定层（闭合枚举 + fail-safe 方向）────────────────────────────

def test_reason_enum_is_closed_and_total():
    """理由必须闭合、且每条都自带 (enforcement, level) 二元组。

    这条不是形式主义：判定表是**唯一**能新增「一条执行面」的地方。若某个
    理由没有对应档位，就会出现「新增了一条路，但没人知道它算不算受限」——
    正是 #1 的病（`None` 的含义无人定义、五处各自解释）。

    回滚探针：把 `_DECISIONS` 里某条的取值改成 1 元组（或加一条缺档位的
    成员）即转红。
    """
    for reason, pair in policy._DECISIONS.items():
        assert isinstance(reason, str) and reason, reason
        assert len(pair) == 2, f"{reason} 必须同时给出 (enforcement, level)"
        enforcement, level = pair
        assert enforcement in (policy.ENF_CONFINED, policy.ENF_NATIVE), (reason, pair)
        assert level in (policy.LEVEL_PARTIAL, policy.LEVEL_NONE), (reason, pair)
    # 三类原生理由都必须**具名**（不许出现「其他/默认」这类兜底理由）
    assert policy.make_decision(policy.R_NATIVE_PLATFORM).confined is False
    assert policy.make_decision(policy.R_NATIVE_CONFIG_OFF).confined is False
    assert policy.make_decision(policy.R_NATIVE_PROJECT_OPT_OUT).confined is False
    with pytest.raises(ValueError, match="unknown sandbox reason"):
        policy.make_decision("whatever")


@pytest.mark.asyncio
async def test_default_is_confined_not_native(monkeypatch):
    """「未知」一律判 **confined**（fail-safe 方向）。

    判错的代价必须落在「拒绝执行（可见）」而不是「静默无沙箱执行（不可见）」。
    项目级沙箱模式查询失败也走这条 —— `project_sandbox_mode` 出错返回 `""`，
    不会被读成逃生门。
    """
    monkeypatch.setattr(policy, "sandbox_disabled_reason", lambda: None)
    with patch(
        "hiveweave.services.acl_sandbox.integration.project_sandbox_mode",
        new=AsyncMock(return_value=""),
    ):
        d = await policy.resolve_spawn_decision("proj-1")
    assert d.confined is True
    assert d.reason == policy.R_CONFINED
    assert d.level == policy.LEVEL_PARTIAL

    # 查询失败（异常 → ""）同样不许被读成原生
    with patch(
        "hiveweave.services.acl_sandbox.integration.project_sandbox_mode",
        new=AsyncMock(side_effect=RuntimeError("db down")),
    ):
        d2 = await policy.resolve_spawn_decision("proj-1")
    assert d2.confined is True, "查询失败不得被读成「逃生门」"


@pytest.mark.asyncio
async def test_danger_full_access_is_a_named_native_reason(monkeypatch):
    """项目级逃生门 = **具名**原生理由（不是 None、不是异常）。"""
    monkeypatch.setattr(policy, "sandbox_disabled_reason", lambda: None)
    with patch(
        "hiveweave.services.acl_sandbox.integration.project_sandbox_mode",
        new=AsyncMock(return_value="danger-full-access"),
    ):
        d = await policy.resolve_spawn_decision("proj-1")
    assert d.confined is False
    assert d.reason == policy.R_NATIVE_PROJECT_OPT_OUT
    assert d.level == policy.LEVEL_NONE


def test_stamp_keys_cover_every_reported_field():
    """戳的键名清单与 `SpawnDecision.stamp()` 的实际输出必须一致。

    两边各写一份名字，就会出现「工具透传了一个没人认得的键」或「上报字段
    悄悄少了一个」——`ENFORCEMENT_STAMP_KEYS` 是给透传方用的，必须同源。
    """
    d = policy.make_decision(policy.R_CONFINED)
    assert set(d.stamp()) <= set(policy.ENFORCEMENT_STAMP_KEYS)
    assert set(d.stamp(boundary_root=r"C:\ws")) == set(
        policy.ENFORCEMENT_STAMP_KEYS
    )
    n = policy.make_decision(policy.R_NATIVE_CONFIG_OFF)
    assert "enforcement_boundary" not in n.stamp(), (
        "原生侧不该有边界标记 —— 「没有边界」本身就是要被看见的事实"
    )


# ── 跨路一致性（本条修掉的真实缺陷）──────────────────────────────

@pytest.mark.asyncio
async def test_python_script_does_not_refuse_danger_full_access(tmp_path):
    """★ 回归：项目级逃生门下，`python_script` 必须**照跑**（与其余四条路一致）。

    改造前：同一平台状态下 bash / run_command / dev_server / alarm 回落原生，
    `python_script` 却回 `python_script: sandbox unavailable` 拒执行 —— 因为
    它把入口的 `None` 读成了「沙箱坏」。这条钉住那个读法不再存在。

    回滚探针：把 `python_script` 的 native 分支删掉（改回
    `if not routed.confined: return ToolResult.err("sandbox unavailable")`）即转红。
    """
    from hiveweave.tools.python_script import (
        PythonScriptParams,
        python_script_execute,
    )

    with (
        patch(
            _DECISION_SEAM,
            new=AsyncMock(
                return_value=policy.make_decision(policy.R_NATIVE_PROJECT_OPT_OUT)
            ),
        ),
        patch(
            "hiveweave.tools.helpers.get_project_id",
            new=AsyncMock(return_value="p1"),
        ),
    ):
        r = await python_script_execute(
            PythonScriptParams(script="print(41 + 1)"), "aid", str(tmp_path)
        )
    assert r.success is True, (
        f"逃生门（显式配置性信任）下不应拒执行；实测 {r.error!r}"
    )
    assert "42" in (r.output or "")


@pytest.mark.asyncio
async def test_confined_unavailable_must_not_fall_back_to_native(tmp_path):
    """受限判定 + 受限路径失败 ⇒ 拒绝；**绝不**静默按原生再跑一遍。

    「以为在沙箱里、其实在沙箱外」是 #1 最坏的形态：它比明确拒绝糟得多，
    因为 agent 与审计方**都看不到**差别。

    回滚探针：在 `python_script` 里给受限异常加一条 native 兜底即转红。
    """
    from hiveweave.services.acl_sandbox.errors import SandboxUnavailableError
    from hiveweave.tools import python_script as ps

    native_spy = AsyncMock(side_effect=AssertionError("原生路径不该被调用"))
    with (
        patch.object(ps, "_run_native_argv", native_spy),
        patch(
            _DECISION_SEAM,
            new=AsyncMock(return_value=policy.make_decision(policy.R_CONFINED)),
        ),
        patch(
            "hiveweave.services.acl_sandbox.service.spawn_confined",
            new=AsyncMock(side_effect=SandboxUnavailableError("boom")),
        ),
        patch(
            "hiveweave.tools.helpers.get_project_id",
            new=AsyncMock(return_value="p1"),
        ),
    ):
        r = await ps.python_script_execute(
            ps.PythonScriptParams(script="print(1)"), "aid", str(tmp_path)
        )
    assert r.success is False, "受限路径失败必须体现在回执上"
    assert native_spy.await_count == 0, (
        "★ fail-closed：受限不可用时**绝不能**回落原生 —— 那就是"
        "「以为在沙箱里」"
    )


@pytest.mark.asyncio
async def test_native_decision_passed_to_confined_is_rejected(tmp_path):
    """判定为原生时把它传进受限执行器 ⇒ **ValueError**（约束住在操作内部）。

    为什么不是「返回 None 让它自己降级」：那正是 #1 的病 —— `None` 的含义
    由调用方解释。现在受限执行器要么收到受限判定，要么报「你找错入口了」。
    """
    import hiveweave.services.acl_sandbox.service as svc

    with pytest.raises(ValueError, match="原生判定"):
        await svc.spawn_confined(
            command="echo hi",
            workdir=str(tmp_path),
            workspace_path=str(tmp_path),
            agent_id="A001",
            decision=policy.make_decision(policy.R_NATIVE_CONFIG_OFF),
        )


@pytest.mark.asyncio
async def test_entry_refuses_to_silently_downgrade(tmp_path):
    """判定=受限、受限实现却没结果 ⇒ `SandboxUnavailableError`（且不落原生）。

    这条分支的真实用途：**将来新增的入口**若把「本次不需要受限」写进自己的
    实现里（返回 None），它会在接线层被拦住 —— 而不是静默补一次原生执行。
    """
    from hiveweave.services.acl_sandbox.entry import spawn_agent_command
    from hiveweave.services.acl_sandbox.errors import SandboxUnavailableError

    native_spy = AsyncMock(return_value={"exit_code": 0})
    with patch(
        _DECISION_SEAM, new=AsyncMock(return_value=policy.make_decision(policy.R_CONFINED))
    ):
        with pytest.raises(SandboxUnavailableError):
            await spawn_agent_command(
                entry="bash",
                agent_id="A001",
                workspace_path=str(tmp_path),
                workdir=str(tmp_path),
                project_id="p1",
                confined=AsyncMock(return_value=None),
                native=native_spy,
            )
    assert native_spy.await_count == 0


@pytest.mark.asyncio
async def test_entry_accepts_sync_native_implementations(tmp_path):
    """原生实现**同步/异步都收** —— 入口是接线层，不逼工具改写原生路径。

    回归来源（本批实测）：`dev_server_tools._native_spawn` 是**同步**的，返回
    `(proc, err, meta)` 三元组。入口第一版直接 `await native()` ⇒
    `object tuple can't be used in 'await' expression`，且**只在判定为原生时
    发生**（沙箱开着时全绿）—— 真机反向对照用例抓到。
    """
    from hiveweave.services.acl_sandbox.entry import spawn_agent_command

    with patch(
        _DECISION_SEAM,
        new=AsyncMock(return_value=policy.make_decision(policy.R_NATIVE_CONFIG_OFF)),
    ):
        routed = await spawn_agent_command(
            entry="dev_server",
            agent_id="A001",
            workspace_path=str(tmp_path),
            workdir=str(tmp_path),
            project_id="p1",
            confined=AsyncMock(),
            native=lambda: (None, "boom", {}),     # 同步 + 非 dict 返回值
        )
    assert routed.native is True
    assert routed.result == (None, "boom", {}), "非 dict 结果原样透传（不猜形状）"


# ── 结构守卫 ────────────────────────────────────────────────────

def _call_sites(name: str) -> list[tuple[str, str, int, ast.Call]]:
    """全平台源码里对 `name` 的调用点 ⇒ [(relpath, 所在函数, lineno, Call)]。"""
    hits: list[tuple[str, str, int, ast.Call]] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        rel = path.relative_to(_SRC_ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        owners: list[tuple[int, int, str]] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                owners.append(
                    (node.lineno, getattr(node, "end_lineno", node.lineno), node.name)
                )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            called = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            if called != name:
                continue
            owner = "<module>"
            for lo, hi, fname in owners:
                if lo <= node.lineno <= hi:
                    owner = fname
                    break
            hits.append((rel, owner, node.lineno, node))
    return hits


def _carries_decision(call: ast.Call) -> bool:
    for k in call.keywords:
        if k.arg == "decision":
            return True
        # **ctx.confined_kwargs() —— 唯一被批准的接线展开
        if (
            k.arg is None
            and isinstance(k.value, ast.Call)
            and getattr(k.value.func, "attr", None) == "confined_kwargs"
        ):
            return True
    return False


def _confined_sites_without_decision(
    sites: list[tuple[str, str, int, ast.Call]],
) -> list[str]:
    """不带 decision、且**不在显式登记** ``_CONFINED_DIRECT_CALL_ALLOWLIST``
    里的 ``spawn_confined`` 直调点。

    F5（§6.9.4，2026-09-18）：豁免判据从「目录前缀一刀切」改成
    「前缀 **且** 命中登记」—— 包内新建文件不再静默继承豁免。
    """
    return [
        f"{rel}:{lineno} ({owner})"
        for rel, owner, lineno, call in sites
        if not _carries_decision(call)
        and f"{rel}::{owner}" not in _CONFINED_DIRECT_CALL_ALLOWLIST
    ]


def test_every_confined_call_carries_a_decision():
    """受限执行器**不许**被「自己判出来的许可」调用：必须显式带 `decision`。

    包内豁免走**显式登记** ``_CONFINED_DIRECT_CALL_ALLOWLIST``（现状唯一
    成员 = sentinel 探针，理由见该常量注释）；登记外的包内直调点一律转红。
    包外必须二选一：显式 `decision=…`，或展开唯一接线助手
    `ctx.confined_kwargs()`（它带 decision）。

    ⚠ 不守什么：不守「伪造一个 confined 判定」。它守的是**默认路径**——
    新增一条受限执行接线时，作者必须显式表态「这次走哪条路」
    （`make_decision` 要求给理由），而不是写个 `if` 自己判。
    """
    offenders = _confined_sites_without_decision(_call_sites("spawn_confined"))
    assert not offenders, (
        "这些地方直接调 spawn_confined 却没带 decision —— 即「自己判沙箱」"
        "（#1 的复发形态）："
        f"{offenders}\n请改用 entry.spawn_agent_command（判定+路由+盖戳）。"
    )


def test_confined_allowlist_is_registry_not_directory_prefix():
    """F5 阳性/反向对照（§6.9.8）：登记真的在咬，目录前缀不再是一刀切豁免。

    阳性①：清空 ``_CONFINED_DIRECT_CALL_ALLOWLIST`` ⇒ sentinel 现状转红
    （证明豁免来自**登记**，不是来自目录前缀——改坏动作＝把登记成员删掉）。
    阳性②：在 ``services/acl_sandbox/`` 下**新增**未登记直调点 ⇒ 转红
    （旧写法里它会被目录前缀静默豁免）。
    反向：登记成员保持绿；包外带 decision 的调用不进 offender。
    """
    real_sites = _call_sites("spawn_confined")
    sentinel_sites = [
        s for s in real_sites if s[0] == "services/acl_sandbox/sentinel.py"
    ]
    assert sentinel_sites, "扫描器没看见 sentinel 直调点 —— 探针失真"
    assert _confined_sites_without_decision(sentinel_sites) == [], (
        "sentinel 登记成员被报 offender —— allowlist 未生效"
    )
    # 阳性①：清空登记 ⇒ sentinel 转红
    mod = sys.modules[__name__]
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(mod, "_CONFINED_DIRECT_CALL_ALLOWLIST", frozenset())
        assert _confined_sites_without_decision(sentinel_sites), (
            "清空 allowlist 后 sentinel 仍绿 ⇒ 豁免其实来自目录前缀（改造失败）"
        )
    finally:
        monkey.undo()
    # 阳性②：包内新增未登记直调点 ⇒ 转红
    snippet = ast.parse("def _fake_new_probe():\n    spawn_confined(cmd)\n")
    fake_call = next(n for n in ast.walk(snippet) if isinstance(n, ast.Call))
    fake_sites = [
        ("services/acl_sandbox/fake_new_module.py", "_fake_new_probe", 2, fake_call)
    ]
    assert _confined_sites_without_decision(fake_sites), (
        "包内新增未登记直调点没有转红 ⇒ 目录前缀豁免仍是一刀切"
    )
    # 反向：带 decision 的调用不进 offender
    with_d = ast.parse(
        "def _with_decision():\n"
        "    spawn_confined(cmd, decision=make_decision('r'))\n"
    )
    d_call = next(n for n in ast.walk(with_d) if isinstance(n, ast.Call))
    assert _confined_sites_without_decision(
        [("tools/some_tool.py", "_with_decision", 2, d_call)]
    ) == []


def test_no_new_route_judgment_sites():
    """`acl_sandbox_active()` 不再被新代码用来**决定 spawn 路由**。

    存量只允许三种「状态视图」（启动打点 / 状态端点 / legacy 方言近似，
    见 `_ACTIVE_VIEW_ALLOWLIST`）。新增一个用来判路由的调用点 = 又长出
    一处「同一个事实各判各的」——那正是 #1。
    """
    found = {
        f"{rel}::{owner}" for rel, owner, _ln, _c in _call_sites("acl_sandbox_active")
    }
    new = sorted(found - _ACTIVE_VIEW_ALLOWLIST)
    assert not new, (
        f"新增了沙箱判定调用点：{new}\n"
        "判定请走 policy.resolve_spawn_decision（唯一判定点），"
        "路由请走 entry.spawn_agent_command —— 工具不再判断沙箱。"
    )


def test_guard_scanners_are_not_vacuous():
    """阳性对照：两个扫描器必须**看见**真东西。

    空扫描器（路径写错、AST 遍历写坏）会让上面两条守卫恒绿 ——
    「看似有守卫」比「没有守卫」更危险。
    """
    confined_calls = _call_sites("spawn_confined")
    assert len(confined_calls) >= 5, (
        f"只扫到 {len(confined_calls)} 处 spawn_confined 调用 —— 扫描器可能已失效"
        f"（当前应有：bash×2 / dev_server / python_script / alarm / sentinel）"
    )
    active_calls = {f"{rel}::{owner}" for rel, owner, _l, _c in _call_sites("acl_sandbox_active")}
    assert active_calls, "扫描器没看见任何 acl_sandbox_active 调用（含 allowlist 里的存量）"
    assert active_calls <= _ACTIVE_VIEW_ALLOWLIST, (
        f"扫描到的调用点超出 allowlist：{sorted(active_calls - _ACTIVE_VIEW_ALLOWLIST)}"
    )


# ── 执行面戳的**消费侧**守卫（F3，2026-09-17）────────────────────────
#
# 本文件原 docstring 明说：「它**不守**工具是否把 `enforcement*` 戳透传到
# 最终结果（那是各工具自己的事）」—— F3 证明了这个"各工具自己的事"恰好
# 就是没人管的那件事：58/59/60/61 共 4863 行 `run_steps` 的 `enforcement`
# **零落库**，而同批同表的 `git_hardened` 有 86 条。
#
# 所以那条「不守什么」现在要收回 —— 观测位的**生产者**（入口盖戳）与
# **消费者**（`run_steps.enforcement`）之间必须有守卫，否则「写了事实位
# 没有消费者」只是日志，不是状态。

def _stamp_carrying_functions() -> set[str]:
    """所有 `spawn_agent_command` 的**调用者**函数名（形如 `文件.py::函数`）。"""
    return {
        f"{rel}::{owner}"
        for rel, owner, _ln, _c in _call_sites("spawn_agent_command")
    }


_TS_CTORS = ("ok", "err", "blocked_err")


def _stamp_vars_expanded_into_toolresult(fn: ast.AST) -> set[str]:
    """函数体里被 `**` 展开进 `ToolResult.ok/err/blocked_err` 的**变量名**。

    「有搬运动作」的唯一可信判据是**这个动作真的连到了 ToolResult 构造上**
    —— 只看「函数里出现过 `enforcement` 字样」是**假阴性**判据：
    本次实测，`python_script` 把 `_stamp = {…startswith("enforcement")…}`
    改成 `_stamp = {}`（戳不再提取、但字面量还在别处）后，旧判据仍判"有搬运"、
    守卫不转红 ⇒ **假绿**。所以改为溯源：变量名 → 它的定义表达式。
    """
    names: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        if _call_name(node) not in _TS_CTORS:
            continue
        for kw in node.keywords:
            if kw.arg is None and isinstance(kw.value, ast.Name):
                names.add(kw.value.id)
    return names


def _stamp_source_kind(fn: ast.AST, name: str) -> set[str]:
    """`name` 在函数内的**赋值来源种类** —— 结构化判定，不做字符串匹配。

    返回集合，可能含：
      · `"helper"`    —— 表达式里调用了 `_enforcement_stamp`
      · `"decision"`  —— 表达式里调用了 `<x>.stamp()`（入口自身的判定）
      · `"prefix"`    —— 表达式里出现 `"enforcement"` 字符串字面量

    ⚠ **为什么不沿用 `ast.dump` 子串匹配**（2026-09-17 实证）：`ast.dump`
    的输出是不带括号的！`routed.stamp()` 渲染成
    `Call(func=Attribute(value=Name(id='routed', …), attr='stamp', …))` ——
    所以 `".stamp(" in dump` **恒为 False**，正确的写法反被误判成 offender。
    这正是本仓纪律禁的**文本判据**形态（换个写法/换个名字就失准），
    修法是改用 AST 结构判断调用目标，而不是把子串再补一个变体。
    """
    kinds: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            continue
        for sub in ast.walk(node.value):
            if isinstance(sub, ast.Constant) and sub.value == "enforcement":
                kinds.add("prefix")
            if not isinstance(sub, ast.Call):
                continue
            f = sub.func
            if isinstance(f, ast.Name) and f.id == "_enforcement_stamp":
                kinds.add("helper")
            elif isinstance(f, ast.Attribute) and f.attr == "stamp":
                kinds.add("decision")
    return kinds


def _assignment_source(fn: ast.AST, name: str) -> str:
    """`name` 在函数内的**赋值表达式**的 AST dump 拼接（仅供报错信息可读）。"""
    parts: list[str] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    parts.append(ast.dump(node.value))
        elif isinstance(node, ast.AnnAssign):
            t = node.target
            if isinstance(t, ast.Name) and t.id == name and node.value is not None:
                parts.append(ast.dump(node.value))
    return " ".join(parts)


def _functions_without_stamp_transport(path: pathlib.Path) -> list[str]:
    """一个文件里：调了 spawn 却**看不出**搬运了执行面戳的函数。

    判据（**溯源式**，两条同时成立才算「搬了」）：
      1. 函数体内有 `ToolResult.ok/err/blocked_err` 接收 `**<var>` 展开
         （= 确实有一个 dict 被注入进结果）；
      2. 其中**至少一个** `<var>` 的定义表达式里出现了
         `_enforcement_stamp` 调用、`routed.stamp()` 调用，或以
         `"enforcement"` 前缀过滤。

    ⚠ **为什么必须认 `routed.stamp()`**（2026-09-17，M1 之后）：`routed.result`
    的形状**逐分支不同** —— 受限侧是 dict（戳在里面）、原生侧是 `(proc, err,
    meta)` 三元组，而 `entry._with_stamp` 对非 dict **原样返回**。所以
    「从 result 里捞戳」在原生分支**结构性**拿不到戳（等于原生永远无观测）。
    本守卫原先只认前两种写法 ⇒ 正确的 `_stamp = routed.stamp()` 反被判成
    offender ——**守卫写窄了会逼出错误的修法**，比漏报更危险，故一并放开。

    ⚠ 为什么不用「函数里有没有 `enforcement` 字样」：那是文本/字面量判据，
    构造点（前置提取）与消费点（`**` 展开）分离时它抓不到 —— 见
    `_stamp_vars_expanded_into_toolresult` 的 docstring（本次实测的假绿）。

    ⚠ **只扫「构造 ToolResult 的函数」**：`services/game_time.py::_fire_alarm`
    调了 `spawn_agent_command` 却**从不构造 ToolResult**（它是内部告警脚本，
    结果不进 `run_steps`）—— 「戳没落库」在那里不是缺陷。不设这条前置会用
    误报淹没真问题（本次实测：扫描器确实一度把它报成 offender）。

    ⚠ 这条前置也必须是**结构化**判据（2026-09-17）：原先写的是
    `if "ToolResult" not in path.read_text()` —— 那是**文本子串**判据，
    文件里只要在注释/字符串里提一句 `ToolResult` 就会豁免整个文件
    （本仓纪律：文本判据随措辞失效）。改为扫描 AST 里有没有
    `ToolResult.ok/err/blocked_err` 的**调用**。判据同时从「模块级」收窄到
    **函数级**：一个文件里既有构造 ToolResult 的函数、也有不构造的，前者
    该被扫、后者不该被扫（原先是整文件一刀切）。
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return []

    out: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        calls = {_call_name(c) for c in ast.walk(fn) if isinstance(c, ast.Call)}
        if "spawn_agent_command" not in calls:
            continue
        # 函数级前置：本函数从不构造类型化结果 ⇒ 不适用本守卫
        if not (set(_TS_CTORS) & calls):
            continue
        expanded = _stamp_vars_expanded_into_toolresult(fn)
        if not expanded:
            # 调了 spawn 却一个 `**` 展开都没有 ⇒ 戳必丢
            out.append(fn.name)
            continue
        ok = False
        for var in expanded:
            # 判据三选一（**结构化**判定调用目标，不做子串匹配）：
            #   ① 调 `_enforcement_stamp`（bash.py 的提取助手）
            #   ② 调 `<x>.stamp()` —— 从**入口自身的判定**取戳。这是 M1 之后
            #      dev_server 的正确形态：`routed.result` 在原生分支是三元组、
            #      根本不是 dict，从里面捞戳必然漏掉整个原生侧。
            #   ③ 以 `"enforcement"` 前缀过滤
            if _stamp_source_kind(fn, var):
                ok = True
                break
        if not ok:
            out.append(fn.name)
    return out


def _call_name(node: ast.Call) -> str:
    fn = node.func
    if isinstance(fn, ast.Name):
        return fn.id
    return getattr(fn, "attr", "") or ""


def test_spawn_callers_transport_the_enforcement_stamp():
    """★ F3 复发守卫：调了 spawn 又返回 ToolResult 的函数，必须搬运执行面戳。

    ⚠ 判据是**状态式**的（AST：有没有搬运动作），不是文案式的 ——
    本仓纪律：文本子串判据会被换措辞绕过。

    ⚠ 不守什么：它**不守**戳运到了最终 `run_steps`（那要跑真库；本文件
    只守住「工具层有没有把入口盖的戳丢掉」这一段）。也不守 bash.py ——
    那条路出口返回裸 dict、由 `_shell_tool_result` 统一收口，形态不同，
    已由 `test_fact_position_taxonomy_fixes.py` 的白名单守卫覆盖。

    ⚠ **判据宽窄（2026-09-17 审计实测）**：`_stamp_source_kind` 的
    `"prefix"` 一档（赋值表达式里出现 `ast.Constant == "enforcement"`）**很宽**
    —— 审计实证 `_stamp = {"enforcement": None}`（写个占位就把真戳丢掉）
    照样**通过本守卫**。它得以存在只为兼容历史的「前缀过滤」写法，而那种
    写法现在已由 `test_no_tool_reimplements_the_stamp_key_list` 拦下，
    所以这一档的实际作用只是给字面量开后门。
    ⇒ **本守卫的真实保护面由行为测试兜底**，不是它自己：
    `test_python_script_stamp_transport.py`（独立断言 `git_hardened`）与
    `test_dev_server_sandbox_wiring.py` 的原生分支用例才是拦得住的那一层。
    引用本守卫时**不要**把它当成"戳一定送到了"的证明。
    """
    offenders: list[str] = []
    checked = 0
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        rel = path.relative_to(_SRC_ROOT).as_posix()
        # F5（§6.9.4，2026-09-18）：原「包内整目录跳过」已删 —— 现状包内
        # **零个** spawn_agent_command 调用点（sentinel 调的是 spawn_confined
        # 且不构造 ToolResult，见 ``_CONFINED_DIRECT_CALL_ALLOWLIST`` 注释），
        # 无需豁免；日后包内若真出现 spawn_agent_command + ToolResult 的
        # 组合，应当**被扫到**而不是静默豁免。tools/bash.py 的豁免保留：
        # 裸 dict + 统一漏斗，形态不同，已由
        # `test_fact_position_taxonomy_fixes.py` 的白名单守卫覆盖。
        if rel == "tools/bash.py":
            continue
        bad = _functions_without_stamp_transport(path)
        if bad:
            offenders.append(f"{rel}: {bad}")
        for _ln, _c in _call_sites_in(path, "spawn_agent_command"):
            checked += 1
    assert checked >= 3, (
        f"只扫到 {checked} 处 spawn_agent_command 调用点 —— 扫描器可能已失效"
    )
    assert not offenders, (
        "这些函数调了 spawn_agent_command 并返回 ToolResult，却**看不出**"
        "搬运执行面戳（F3 形态：入口盖了戳、工具层丢掉 ⇒ "
        f"run_steps.enforcement 恒 NULL）：{offenders}"
    )


def _call_sites_in(path: pathlib.Path, name: str) -> list[tuple[int, ast.Call]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return []
    return [
        (n.lineno, n)
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and _call_name(n) == name
    ]


def test_shell_whitelist_covers_every_spawn_stamp_key():
    """★ F3：shell 路的白名单必须**整体**覆盖 `SPAWN_STAMP_KEYS`。

    这是 F3 的**主丢失点**：八处 shell 出口都正确调了 `_enforcement_stamp`，
    而 `_SHELL_FACT_FLAG_KEYS` 没登记 `enforcement*` ⇒ 在 `_ff` 过滤时
    4 个键被一起丢掉。
    """
    from hiveweave.tools import bash as bash_mod

    missing = set(policy.SPAWN_STAMP_KEYS) - set(bash_mod._SHELL_FACT_FLAG_KEYS)
    assert not missing, (
        f"shell 事实位白名单缺 {sorted(missing)} —— spawn 面戳会在这层被"
        "整体滤掉（实证：4863 行 run_steps 零落库，而 git_hardened 有 86 条）"
    )


def _stamp_list_offenders_in_tree(rel: str, tree: ast.AST) -> list[str]:
    """本模块 AST 里「按 ``"enforcement"`` 字面量做 ``startswith`` 过滤」的
    取值动作（M3 的唯一可检形态；已知漏检形态见
    ``test_stamp_list_guard_known_misses_executable_evidence``）。"""
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        # `...startswith("enforcement")`
        if (
            isinstance(f, ast.Attribute)
            and f.attr == "startswith"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and node.args[0].value.startswith("enforcement")
        ):
            found.append(f"{rel}:{node.lineno} startswith(前缀过滤)")
    return found


def test_no_tool_reimplements_the_stamp_key_list():
    """★ M3（2026-09-17 审计必修）：**不许有第二份戳键清单**。

    病灶：`python_script` 用 `k.startswith("enforcement")` 做**本地前缀过滤**，
    而 bash / dev_server 用 `_enforcement_stamp`（`SPAWN_STAMP_KEYS`）。两套
    口径 ⇒ 加固面 `git_hardened` 在 python_script 被**静默丢掉**（它不含
    "enforcement" 前缀）。这是本仓反复栽的「每处各列一份清单」形态。

    判据（AST，非文本子串）：任何模块里出现「按 `"enforcement"` 字面量做
    `startswith` / `in` 过滤」的**取值动作**即转红。唯一允许的登记点是
    `policy.SPAWN_STAMP_KEYS`，唯一允许的提取助手是 `bash._enforcement_stamp`。

    ⚠ 为什么不禁 `ast.Constant == "enforcement"` 整体：注释里提到该词、
    或对 `result["enforcement"]` 做单键读取都是合法的（单键读取不构成
    "另一份清单"）。本守卫只打**前缀/成员过滤**这一种形态。

    ⚠ 不守什么（2026-09-17 审计实测，**实际保护面远窄于本守卫的名字**）：
    只认 `k.startswith("enforcement")` 这**一种**形态。审计构造的等价写法
    **全部漏过**：`k in ("enforcement", …)`、`k.startswith("enf")`、
    `"enforc" in k`、`re.match(r"^enforcement", k)`、
    `k.split("_")[0] == "enforcement"`、以及**本地元组常量 + `k in _KEYS`**
    （最后这条是真工程师最可能写的形态 —— 它连 `startswith` 都不需要）。
    ⇒ 它的价值是**报错信息可读**（直接点出"第二份清单"），不是捕获能力。
    真正拦住这些形态的是**行为测试**：审计把「本地元组清单」注入
    `python_script.py` 后，本守卫 GREEN 漏过，而
    `test_python_script_stamp_transport.py` 的 `git_hardened` 断言
    **4 条转红**。引用本守卫时不要把它当成通用防线。
    """
    offenders: list[str] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        rel = path.relative_to(_SRC_ROOT).as_posix()
        if rel.startswith("services/acl_sandbox/"):
            continue  # policy.py 是唯一登记点，自己当然可以出现字面量
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        offenders.extend(_stamp_list_offenders_in_tree(rel, tree))
    assert not offenders, (
        "这些地方又写了一份戳键清单（前缀过滤）—— 键名必须由 "
        f"`policy.SPAWN_STAMP_KEYS` 单点登记、经 `bash._enforcement_stamp` 提取："
        f"{offenders}"
    )


def test_stamp_list_guard_known_misses_executable_evidence():
    """F5（§6.9.5）：「实际保护面远窄于名字」的**可执行反例清单**。

    下方每个形态都是「第二份戳键清单」的等价写法，本守卫（M3）
    **全部漏过**——本用例把 docstring 的断言变成可执行证据：漏检形态
    进探测函数必须返回空（green 漏过被钉死为**已知边界**，而非巧合），
    唯一可检形态必须真的转红（探测器自身不是空转）。真拦截者是行为测试
    （`test_python_script_stamp_transport.py` 的 ``git_hardened`` 断言等），
    引用本守卫时不要把它当成通用防线。
    """
    # 已知漏检形态（2026-09-17 审计构造，全部 GREEN 漏过）：
    known_misses = [
        "def _f(k):\n    return k in ('enforcement', 'enforcement_level')\n",
        "def _f(k):\n    return k.startswith('enf')\n",
        "def _f(k):\n    return 'enforc' in k\n",
        'def _f(k):\n    import re\n    return re.match(r"^enforcement", k)\n',
        "def _f(k):\n    return k.split('_')[0] == 'enforcement'\n",
        "_KEYS = ('enforcement',)\n\n\ndef _f(k):\n    return k in _KEYS\n",
    ]
    for snippet in known_misses:
        tree = ast.parse(snippet)
        assert _stamp_list_offenders_in_tree("<synthetic>", tree) == [], (
            f"该形态已被探测函数捕获 ⇒ 本反例清单过期（守卫变严了，"
            f"请更新本用例）：{snippet!r}"
        )
    # 唯一可检形态必须真的转红（证明探测器在工作，不是恒空）：
    caught = ast.parse('def _f(k):\n    return k.startswith("enforcement")\n')
    assert _stamp_list_offenders_in_tree("<synthetic>", caught), (
        "startswith('enforcement') 没有转红 ⇒ 探测器空转（阳性对照失败）"
    )


def test_stamp_key_registry_is_the_single_source():
    """★ M3 的**阳性对照**：唯一登记点必须真的覆盖加固面键。

    若 `SPAWN_STAMP_KEYS` 掉了 `git_hardened`，那么"统一到唯一登记点"反而
    会让加固面键**在全平台**消失 —— 统一口径的前提是这个口径是对的。
    """
    assert "git_hardened" in policy.SPAWN_STAMP_KEYS, (
        "加固面键必须在唯一登记点里 —— 否则统一口径会把它从**全平台**抹掉"
    )
    assert set(policy.ENFORCEMENT_STAMP_KEYS) <= set(policy.SPAWN_STAMP_KEYS)
    from hiveweave.tools import bash as bash_mod

    # 助手必须真的认加固面键（不是只认 enforcement 前缀）
    assert bash_mod._enforcement_stamp({"git_hardened": True}) == {
        "git_hardened": True
    }, "提取助手必须覆盖加固面键，否则只是把前缀过滤换个地方写"
