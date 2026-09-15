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


def test_every_confined_call_carries_a_decision():
    """受限执行器**不许**被「自己判出来的许可」调用：必须显式带 `decision`。

    只允许包内（`services/acl_sandbox/`，即判定与探针自己）不传 —— 那里调用
    时本函数自己走唯一判定点。包外必须二选一：显式 `decision=…`，或
    展开唯一接线助手 `ctx.confined_kwargs()`（它带 decision）。

    ⚠ 不守什么：不守「伪造一个 confined 判定」。它守的是**默认路径**——
    新增一条受限执行接线时，作者必须显式表态「这次走哪条路」
    （`make_decision` 要求给理由），而不是写个 `if` 自己判。
    """
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

    offenders = [
        f"{rel}:{lineno} ({owner})"
        for rel, owner, lineno, call in _call_sites("spawn_confined")
        if not rel.startswith("services/acl_sandbox/")
        and not _carries_decision(call)
    ]
    assert not offenders, (
        "这些地方直接调 spawn_confined 却没带 decision —— 即「自己判沙箱」"
        "（#1 的复发形态）："
        f"{offenders}\n请改用 entry.spawn_agent_command（判定+路由+盖戳）。"
    )


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
