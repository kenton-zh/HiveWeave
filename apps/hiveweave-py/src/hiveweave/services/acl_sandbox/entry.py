"""entry.py —— agent 命令 spawn 的**唯一入口**：判定 + 路由 + 盖戳（#1 治本）。

## 为什么需要这一层

改造前每个工具自己 `if acl_sandbox_active():` —— 策略散在 seam 里、
**没人知道这次 spawn 实际走了哪条**、漏接**不产生任何信号**
（`start_dev_server` 从未 import 过 sandbox，而它照样跑：TEST_DSH_56 的
越界写实证）。且同一个内部信号被各调用方各自解释：`spawn_confined` 返回
`None` 在四条路上被读成「沙箱关（回落原生）」、在 `python_script` 里被读成
「沙箱坏（拒绝执行）」⇒ 同一个平台状态（项目级 `danger-full-access`）
**四跑一拒**。根因是判定含义由调用方解释，不是某一处写错。

## 本层提供的不变式

1. **判定唯一**：判定只由 `policy.resolve_spawn_decision()` 做，本层消费它；
   调用方拿不到「要不要开沙箱」这个问题（它只能提供两条实现）。
2. **路由一致**：`confined` / `native` 两条实现在同一处被选择，因此
   「同平台状态在两条路上行为不同」在结构上不可表达。
3. **必盖戳**：每次路由都发一条结构化日志（`acl_sandbox.spawn_routed`，
   含 entry / agent / enforcement / level / reason）—— 于是「漏接」在数据里
   表现为**某个入口没有戳**，而不是表现为「一切正常」。
4. **失败 fail loud**：判定说受限而受限实现却没给出结果 ⇒ 抛
   `SandboxUnavailableError`（fail-closed），绝不静默降级为原生 ——
   「以为在沙箱里、其实在沙箱外」是本条最坏的形态。
"""

from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import structlog

from hiveweave.services.acl_sandbox import policy
from hiveweave.services.acl_sandbox.errors import SandboxUnavailableError
from hiveweave.services.acl_sandbox.policy import SpawnDecision

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SpawnContext:
    """路由上下文：交给 `confined` 实现去接线 `spawn_confined` 的公共参数。"""

    entry: str
    agent_id: str
    workspace_path: str
    workdir: str
    project_id: str | None
    project_root: str | None
    decision: SpawnDecision

    def confined_kwargs(self) -> dict[str, Any]:
        """`spawn_confined` 的公共接线 —— 写一次，六个入口不再各写一份。"""
        return {
            "workdir": self.workdir,
            "workspace_path": self.workspace_path,
            "agent_id": self.agent_id,
            "project_id": self.project_id,
            "project_workspace_path": self.project_root,
            "entry": self.entry,
            "decision": self.decision,
        }


@dataclass(frozen=True)
class RoutedSpawn:
    """路由结果：结果本体 + 这次走的哪条路（戳随结果一起向上走）。

    ``executed`` 是 F5 的**执行事实**：``True`` 执行函数返回了结果、
    ``False`` 执行函数抛了（从未启动）、``None`` 不适用/未判定。

    ⚠⚠ **事实只有一个来源**（审计 BLOCKING-1，2026-09-17）：`executed` 是
    **由 `result` 算出来的**（见 `executed_actual`），不是独立存一份 ——
    早先版本让它同时存在于 `self.executed` 与 `result["executed"]`，两者
    一旦不一致（入口写 `True`、执行函数声明 `False`）就会**自相矛盾**。
    实测复现：`result['executed']=False` 而 `stamp()['executed']=True`。
    这与本批要修的 F5 是**同一个病**：一个事实存在两份、然后打架。

    本仓纪律：「**一次 spawn 的全部事实**」只登记一次（`policy` 那组常量），
    同样地「这一条事实的值」也只应有一个权威来源。
    """

    result: Any
    decision: SpawnDecision
    executed: bool | None = None

    @property
    def native(self) -> bool:
        return not self.decision.confined

    @property
    def executed_actual(self) -> bool | None:
        """事实位的**唯一权威来源**：优先 `result` 里执行函数自己的声明。

        为什么 `result` 优先而不是 `self.executed`：`result` 是**执行函数亲手
        写的**，它比入口更接近事实（入口只知道"函数返回了个 dict"，不知道那个
        dict 是"没跑"）。入口的 `self.executed` 只在执行函数**没表态**时兜底
        —— 即 `entry.py` 的 `_mark_executed` / `_mark_not_executed` 两条路。
        """
        if isinstance(self.result, dict):
            declared = self.result.get("executed")
            if declared is not None:
                return bool(declared)
        return self.executed

    def stamp(self, *, boundary_root: str | None = None) -> dict[str, Any]:
        """决策面戳 + **执行面事实**（唯一取戳入口，工具层一律用它）。

        ``executed`` 只在**确定**时进戳（缺键不补默认值）。
        ⚠ 取值一律经 `executed_actual` —— 保证戳与 `result` **永不打架**。
        """
        out = dict(self.decision.stamp(boundary_root=boundary_root))
        actual = self.executed_actual
        if actual is not None:
            out["executed"] = actual
        return out


async def spawn_agent_command(
    *,
    entry: str,
    agent_id: str,
    workspace_path: str,
    workdir: str,
    project_id: str | None,
    confined: Callable[[SpawnContext], Awaitable[dict | None]],
    native: Callable[[], Any],
    project_root: str | None = None,
    decision: SpawnDecision | None = None,
) -> RoutedSpawn:
    """判定 → 路由 → 盖戳。**agent 命令的 spawn 必须经此**（唯一入口）。

    - ``entry``：`policy.ENTRY_BOUNDARY` 的六入口之一（`spawn_confined` 会
      对非法值 fail-closed）。
    - ``confined``：受限实现（**异步**，应经 `spawn_confined(**ctx.confined_kwargs())`）。
    - ``native``：原生实现（各工具**既有的**路径；同步/异步都收，见 `_call`
      —— 入口是接线层，不逼工具改写自己的原生路径）。
    - ``project_root``：调用方已知的项目根（如 alarm 用它当 workdir）；
      缺省时**仅在受限分支**解析（原生分支不需要 git/cache SID 派生源）。
    - ``decision``：调用方**已经**判定过时传入（避免同一次调用判两次 ——
      例如 `execute_bash` 在 spawn 前就按判定决定"方言门是否生效"）。
      传了就不再判定：判定与那一步必须**同源**，否则又长出两套。
    """
    if decision is None:
        # ⚠ 经**模块属性**调用（不是 `from … import resolve_spawn_decision`）：
        # 判定点是全平台唯一的接缝，绑成两个名字会出现"改了哪一份"的问题 ——
        # 测试也需要恰好一个可替换的落点。
        decision = await policy.resolve_spawn_decision(project_id)

    if decision.confined and project_root is None:
        from hiveweave.services.acl_sandbox.integration import resolve_project_root

        project_root = await resolve_project_root(project_id)
        if project_root is None:
            # ⚠ 可见性补丁（行为不变）：原先这条"解析不到项目根"完全静默，
            # 于是 policy 回落到把 worktree 当项目根 ⇒ git/cache SID 派生错、
            # 表现为莫名其妙的 ACL 拒绝而无人知道原因。是否改为 fail-closed
            # 是**独立的行为变更**，不在本批（需自己的验收用例）。
            log.warning(
                "acl_sandbox.project_root_unresolved",
                entry=entry,
                agent_id=agent_id,
                project_id=project_id,
            )

    ctx = SpawnContext(
        entry=entry,
        agent_id=agent_id,
        workspace_path=workspace_path,
        workdir=workdir,
        project_id=project_id,
        project_root=project_root,
        decision=decision,
    )
    log.info(
        "acl_sandbox.spawn_routed",
        entry=entry,
        agent_id=agent_id,
        project_id=project_id,
        **decision.stamp(),
    )

    if not decision.confined:
        return RoutedSpawn(_with_stamp(await _call(native), decision), decision)

    try:
        result = await confined(ctx)
    except Exception:
        # F5：受限实现**抛出** = 命令从未启动（例：`PwshUnavailableError`
        # 从 `build_confined_argv` 冒出来）。执行事实必须跟着异常一起上报，
        # 否则调用方按 `decision.confined` 盖戳时会宣告
        # 「被沙箱约束」，而事实是没有进程、没有边界。
        raise _mark_not_executed(exc=sys.exc_info()[1])
    if result is None:
        # 可达路径：受限实现自身返回「未启用」（例如绕开 decision 直连
        # 未接线的东西）。判定与执行不一致 ⇒ fail-closed，绝不按原生再跑一遍。
        #
        # ⚠ F5（2026-09-17 审计必修 LOW）：**本分支同样"命令从未启动"**，
        # 也要经 `_mark_not_executed` —— 否则调用方 `_executed_stamp(e)`
        # 取不到属性返回 `{}`，这条出口就只有"被拒绝了"而没有"没跑过"
        # （下游第三次回到"沉默的默认值"，见 B3 审计项）。
        # 语义与「受限实现抛出」完全同档：判定说 confined、而执行面没起来。
        raise _mark_not_executed(exc=SandboxUnavailableError(
            f"entry {entry!r}: sandbox decision={decision.reason!r} says confined, "
            "but the confined implementation returned no result"
        ))
    return RoutedSpawn(_mark_executed(result), decision)


def _mark_not_executed(*, exc: BaseException) -> BaseException:
    """F5：给「受限实现抛出」的异常打上 ``executed=False`` 事实位。

    为什么打在**异常对象**上而不是另开一个返回值：抛出这条路上没有返回值可用
    （`raise` 之后就离开本层了），而调用方的 `except` 分支正是**盖戳的地方** ——
    事实位必须与异常同路到达，否则又回到"调用方靠猜"。

    ⚠ 只加属性、不改异常类型与文案：调用方原有的 `except SandboxUnavailableError`
    等契约**一字不动**（本批只加观测，不改失败形态 —— 后者需自己的验收用例，
    见 fixqueue F5 的"须独立批次"备注）。
    """
    try:
        exc.executed = False  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        # 少数内建异常不允许挂属性 —— 这不该让 spawn 本身失败（观测面问题
        # 不得升格为功能故障）。缺属性时调用方按"未判定"处理。
        log.warning("acl_sandbox.executed_flag_attach_failed", error=str(exc))
    return exc


def _mark_executed(result: Any) -> Any:
    """F5：把「命令**到底有没有启动**」与执行面判定**并排**记进结果。

    背景 —— **戳说「在沙箱里」而进程从未启动**：

    `python_script.py` / `bash.py` 的 `_confined` 在 `build_confined_argv` 抛
    `PwshUnavailableError`（受限 shell 缺失）时，返回的是一个**普通 dict**
    （``exit_code=None`` / ``error="pwsh not found"``）而**不是 None**。于是
    `result is not None` 成立、不抛异常、照常返回 `RoutedSpawn(result, decision)`
    ⇒ `decision.confined is True` ⇒ 戳宣告「被沙箱约束」。
    而 `exit_code is None` 已排除「跑了但失败」⇒ 进程**从未启动**、根本不存在
    边界。回执说"被约束"、事实是"没有进程"，**这句戳在说谎**。

    修法**不是**在下游靠 `exit_code is None` 反推（那是推断，且会误伤"跑了但
    拿不到退出码"的正常情形）—— 而是在**唯一入口**（知道 `confined` 是执行
    还是抛错的那一层）把这一事实显式记下来，让戳自带答案：

      · ``executed=True``  —— 执行函数**返回了结果** ⇒ 进程启动过（无论成败）。
      · ``executed=False`` —— 执行函数**抛出**了 ⇒ 从未启动。

    ⚠ **``False`` 不是错误码，是「无边界」的证据**：下游读
    ``enforcement=="confined" and executed is False`` 时，正确的解读是
    「沙箱判定成立，但这次没有进程受它约束」，而不是「沙箱坏了」。
    缺键（``None``）的语义与全局一致 —— **不适用/未判定**，不等于「跑了」。

    与 `_with_stamp` 的关系：本函数只补这一个键，不碰决策面 4 键 ——
    原生分支的戳仍只有 `_with_stamp` 能盖（原生侧 `executed` 由工具层自报，
    因为原生路径的"启动"概念属各工具自己的实现）。

    ⚠⚠ **`executed=False` 优先于本函数的 `True`**：受限实现**自己声明**了
    「我没启动进程」（如 `PwshUnavailableError` 的早返回）时，本函数**不得**
    用 `True` 覆盖它 —— 那正是 F5 原缺陷的同一个病：入口知道的信息量比
    执行函数少（它只知道"函数返回了个 dict"，不知道那个 dict 是"没跑"）。
    ⇒ 只在**没有既有声明**时补 `True`。这条与「缺键不补默认值」同源：
    **谁更接近事实谁说话**。
    """
    if not isinstance(result, dict):
        return result
    if result.get("executed") is False:
        return result
    return {**result, "executed": True}


def _with_stamp(result: Any, decision: SpawnDecision) -> Any:
    """原生分支也盖戳 —— 「这次没有沙箱」必须被**看见**。

    只在受限侧盖戳的话，原生就是那个**静默的默认值**：一个漏接的工具与一个
    正确接线但沙箱关的平台，在数据里长得一模一样。受限侧由 `spawn_confined`
    盖（带 `boundary`），原生侧的戳只有本层能盖。
    """
    if isinstance(result, dict):
        return {**result, **decision.stamp()}
    return result


async def _call(fn: Callable[[], Any]) -> Any:
    """调用原生实现：**同步/异步两种都收**。

    为什么不能要求调用方统一成 async：本层是**接线层**，不是重写层 ——
    原生实现是各工具既有的东西（`dev_server_tools` 的 `_native_spawn` 是同步
    的、返回 `(proc, err, meta)` 三元组，`bash` 的 `_native` 是异步的）。
    强制统一 = 逼每个工具为适配入口改写自己的原生路径，那正是本条要消灭的
    「同一功能多份实现」。

    ⚠ 曾经这里直接 `await native()` ⇒ 同步实现被 await 报
    `object tuple can't be used in 'await' expression`，且**只在判定为原生时
    发生**（沙箱开着时全绿）。是本文件的反向对照用例抓到的。
    """
    import inspect

    result = fn()
    if inspect.isawaitable(result):
        return await result
    return result
