"""VERIFY 任务 acceptance_criteria 非空门 —— TEST_DSH_70 P0-3。

事故定案：终验任务的 ``acceptance_criteria`` 为空 ⇒ coverage 门失去适用对象
（``services/tasks/submit.py`` 对空清单不设门），等于**合规绕过终验**——
71 轮实测 6 张 verify 单里前 5 张（带清单）全被门拒收 cancel，第 6 张
criteria=NULL 一次通过终验；且「空清单可绕过」的降档条件就印在拒收回执
末行。本模块三条收紧：

1. **建单硬门**：VERIFY 类建单（create_task / dispatch_task 的
   ``milestoneVerify`` 路径）空 criteria 直接拒绝，除非调用方显式传
   ``allow_empty_criteria=true``（协调者/CEO 的显式批准位）——批准事实落
   ``task_events``（``event_type=empty_criteria_approved``，带 actor），
   可在任务事实里查到，不是静默旁路。
2. **关单/通过前查批准事实**：VERIFY 任务 approve 与 close 前校验 criteria
   非空，或存在上述批准事实行（防「先建带 criteria 再 strip」与「批准位
   缺失的存量空单」两条路）。
3. 删降档披露文案在 ``services/tasks/acceptance.py``（拒收回执不再教配方）。

判据一律走 ``kind``（状态判据，``is_verify_task``），不认标题文案。
"""
from __future__ import annotations

from typing import Any

import structlog

from .acceptance import parse_acceptance_plan
from .db import _query, insert_task_event
from .verify import is_verify_task

log = structlog.get_logger(__name__)

#: 空 criteria 显式批准的**审计事件类型**（task_events.event_type）。
EMPTY_CRITERIA_APPROVAL_EVENT = "empty_criteria_approved"


def verify_criteria_is_empty(criteria: Any) -> bool:
    """``acceptance_criteria`` 解析后是否没有任何条目。"""
    return not parse_acceptance_plan(criteria)


def verify_creation_criteria_error(
    *,
    criteria: Any,
    allow_empty: bool,
) -> str | None:
    """VERIFY 建单空清单门（建单**前**调用）。返回错误文案或 None（放行）。

    空 criteria 且未显式批准 ⇒ 拒绝。文案只给正路（附上真实清单），不把
    「可以传批准位」写成配方默认动作 —— 批准位是协调者/CEO 的显式决定，
    参数说明里自会有它的语义与留痕说明。
    """
    if not verify_criteria_is_empty(criteria):
        return None
    if not allow_empty:
        return (
            "VERIFY milestone task rejected: acceptance_criteria is empty. "
            "A VERIFY task with no checklist has nothing for the "
            "acceptance-coverage gate to check (final acceptance bypass). "
            "Pass acceptanceCriteria=[{id, text}, …] (one per-item DoD entry)."
        )
    return None


async def record_empty_criteria_approval(
    project_id: str,
    task_id: str,
    actor_id: str | None,
    *,
    title: str = "",
) -> None:
    """落「空 criteria 显式批准」审计事实行（task_events），并留显著日志。

    该事实是 approve/close 侧 :func:`assert_verify_criteria_or_approval`
    的放行判据 —— 批准必须可查（任务事实里能翻到是谁、何时批的）。
    """
    await insert_task_event(
        project_id,
        task_id,
        EMPTY_CRITERIA_APPROVAL_EVENT,
        from_status=None,
        to_status=None,
        actor_id=actor_id,
        payload={
            "allow_empty_criteria": True,
            "title": (title or "")[:120],
        },
    )
    log.warning(
        "verify_empty_criteria_approved",
        project_id=project_id,
        task_id=task_id,
        actor_id=actor_id,
    )


async def has_empty_criteria_approval(
    project_id: str, task_id: str
) -> bool:
    """任务是否已有空 criteria 显式批准事实行。"""
    if not task_id:
        return False
    try:
        rows = await _query(
            project_id,
            "SELECT id FROM task_events "
            "WHERE task_id = ? AND event_type = ? LIMIT 1",
            [task_id, EMPTY_CRITERIA_APPROVAL_EVENT],
        )
    except Exception as e:  # noqa: BLE001 — 查不到事实即不放行（fail-closed）
        log.warning(
            "verify_criteria_approval_lookup_failed",
            project_id=project_id,
            task_id=task_id,
            error=str(e),
        )
        return False
    return bool(rows)


async def assert_verify_criteria_or_approval(
    project_id: str, task: dict[str, Any] | None
) -> None:
    """VERIFY 任务**通过/关单前**的 criteria 门（非 VERIFY 直接放行）。

    criteria 非空 ⇒ 放行；空但有批准事实行 ⇒ 放行；否则 raise ValueError
    （截断 approve → approved 与 close 两条路）。出路只指正路（重建带清单
    的 VERIFY 单或 cancel），不披露批准位配方。
    """
    if not task or not is_verify_task(task):
        return
    if not verify_criteria_is_empty(task.get("acceptance_criteria")):
        return
    task_id = str(task.get("id") or "")
    if await has_empty_criteria_approval(project_id, task_id):
        log.info(
            "verify_empty_criteria_close_allowed_by_approval",
            project_id=project_id,
            task_id=task_id,
        )
        return
    raise ValueError(
        "VERIFY task cannot be approved/closed: acceptance_criteria is empty "
        f"(task {task_id[:8]}). A VERIFY task with no checklist bypasses the "
        "final-acceptance gate. Re-create the VERIFY task with a real "
        "acceptanceCriteria list, or cancel this task."
    )
