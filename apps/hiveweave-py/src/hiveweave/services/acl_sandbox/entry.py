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
    """路由结果：结果本体 + 这次走的哪条路（戳随结果一起向上走）。"""

    result: Any
    decision: SpawnDecision

    @property
    def native(self) -> bool:
        return not self.decision.confined

    def stamp(self, *, boundary_root: str | None = None) -> dict[str, str]:
        return self.decision.stamp(boundary_root=boundary_root)


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

    result = await confined(ctx)
    if result is None:
        # 可达路径：受限实现自身返回「未启用」（例如绕开 decision 直连
        # 未接线的东西）。判定与执行不一致 ⇒ fail-closed，绝不按原生再跑一遍。
        raise SandboxUnavailableError(
            f"entry {entry!r}: sandbox decision={decision.reason!r} says confined, "
            "but the confined implementation returned no result"
        )
    return RoutedSpawn(result, decision)


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
