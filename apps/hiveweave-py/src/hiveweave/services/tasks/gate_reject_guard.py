"""门禁连拒活性出口 —— TEST_DSH_70 P1-2「≥N 次拒收自动 blocked + external」。

事故定案：同一 VERIFY 任务被 coverage 门连拒 37 次、QA↔CEO 51 条消息拉锯
110 分钟 —— 门每次都从活状态现拼文案，没有「连拒要收口」的机制。本模块给
submit 门禁加**任务级连拒计数**：同一任务连续被门禁拒 N 次后，平台自动把
任务置 ``blocked(wait_kind="external")`` —— 无自动解封路径，由平台的升级
兜底（obligations.arbitration → scan_overdue → org parent）接手，不再拉锯。

- 计数键 = ``(project_id, task_id)``，**按任务**计（与 rejection_memory 的
  「按因（签名）计数」正交：签名归一掉 task id，正好不能用它做任务级出口）。
- 进程内内存即可（对齐 rejection_memory 纪律：连拒发生在 turn 级窗口，重启
  丢失无碍），容量有界。
- 阈值取 3（读现有阈值风格：DOOM_LOOP_DEFAULT_LIMIT=3 / BLOCKED_STALL_LIMIT=3
  同档 —— 连续 3 轮同向被拒即「当前方案不可执行」的平台口径）。
- 计数在**提交成功**（services/tasks/submit.py）与**评审决定**（rework/approve
  都意味着门曾放行）时清零 —— 只有「连续」被拒才累计。
- 不新建持久化事实位：blocked 本身就是状态机事实，blocked_reason 落
  触发原文供审计。
"""
from __future__ import annotations

import threading
import time
from typing import Any

import structlog

log = structlog.get_logger(__name__)

#: 同一任务连续被门拒 N 次 ⇒ 自动 blocked + external（读现有阈值风格后定 3）。
GATE_REJECT_ESCALATE_AFTER = 3

_MAX_KEYS = 500

_lock = threading.Lock()
_counts: dict[str, dict] = {}
# entry: {count, last_error, last_ts}


def _key(project_id: str, task_id: str) -> str:
    return f"{project_id}|{task_id}"


def register_gate_rejection(
    project_id: str, task_id: str, error_text: str = ""
) -> int:
    """登记一次门禁拒收，返回当前连拒次数。"""
    key = _key(project_id, task_id)
    now = time.time()
    with _lock:
        entry = _counts.get(key)
        if entry is None:
            if len(_counts) >= _MAX_KEYS:
                stale = sorted(
                    _counts.items(), key=lambda kv: kv[1]["last_ts"]
                )[: _MAX_KEYS // 2]
                for k, _ in stale:
                    del _counts[k]
            entry = {"count": 0, "last_error": "", "last_ts": now}
            _counts[key] = entry
        entry["count"] += 1
        entry["last_error"] = (error_text or "")[:200]
        entry["last_ts"] = now
        return int(entry["count"])


def reset_gate_rejections(project_id: str, task_id: str) -> None:
    """清零连拒计数（提交成功 / 评审已给出决定时调用）。"""
    with _lock:
        _counts.pop(_key(project_id, task_id), None)


def gate_rejection_count(project_id: str, task_id: str) -> int:
    """只读查询当前连拒计数（测试/遥测用）。"""
    with _lock:
        entry = _counts.get(_key(project_id, task_id))
        return int(entry["count"]) if entry else 0


def reset_for_tests() -> None:
    with _lock:
        _counts.clear()


async def escalate_if_gate_reject_loop(
    project_id: str,
    task: dict[str, Any] | None,
    *,
    error_text: str = "",
) -> str:
    """登记一次门拒；同任务连拒达阈值 ⇒ 自动 blocked(wait_kind=external)。

    返回追加到拒收回执的说明（未触发 / 触发失败为 ""）。挂起动作失败不改变
    本次拒绝结果（best-effort，留 warning 日志）。
    """
    task_id = str((task or {}).get("id") or "")
    if not task_id:
        return ""
    n = register_gate_rejection(project_id, task_id, error_text)
    if n < GATE_REJECT_ESCALATE_AFTER:
        return ""
    reset_gate_rejections(project_id, task_id)
    reason = (
        f"[GATE REJECT LOOP] 该任务连续 {n} 次提交被门禁拒收，平台自动挂起"
        f"升级，不再拉锯。最后拒收摘要：{(error_text or '')[:160]}"
    )
    try:
        from hiveweave.services.task import TaskService

        await TaskService().block_task(
            project_id, task_id, reason, wait_kind="external"
        )
    except Exception as e:  # noqa: BLE001 — 挂起失败不改变本次拒绝
        log.warning(
            "gate_reject_auto_block_failed",
            project_id=project_id,
            task_id=task_id,
            rejections=n,
            error=str(e),
        )
        return ""
    log.warning(
        "gate_reject_auto_blocked",
        project_id=project_id,
        task_id=task_id,
        rejections=n,
        assignee_id=str((task or {}).get("assignee_id") or "") or None,
    )
    return (
        f"\n\n[GATE REJECT ESCALATION] 该任务已被门禁连续拒收 {n} 次 —— "
        "平台已自动置为 blocked(wait_kind='external') 并升级到组织上级裁决；"
        "不要再原样重交，等上级裁决或另立任务。"
    )
