"""企业目标同步段 — 契约 13 / 契约 14.

format_goals_block 是纯字符串格式化：将 charter.goals dict 渲染为
"Enterprise Goals Workbook" 段，供 identity / context prompt 注入。

输出格式（契约 13 §format_goals_block）：
    ## Enterprise Goals Workbook (updated)
    **Objective:** <objective>
    **Current Focus:** <focus>
    **Key Results:**
      - [status] text
    **User Involvement:** <involvement>
    Route decisions matching the user-involvement scope to the user...
    For decisions outside this scope, ask your superior...

goals dirty 检查（每轮注入完整 workbook 还是跳过）由 caller 通过
charter_service.goals_dirty(agent_id, project_id) 决定，本模块不访问 DB。

移植自 Elixir streamer.ex: format_goals_block。
"""

from __future__ import annotations

from typing import Any

from hiveweave.services.charter import DEFAULT_USER_INVOLVEMENT


def format_goals_block(goals: dict[str, Any]) -> str:
    """格式化 Enterprise Goals Workbook 段。

    goals 字段（大小写不敏感）：
      - objective:     项目总目标
      - focus:         当前焦点
      - keyResults:    关键结果列表（str 或 {"text","status"} 对象）
      - userInvolvement: 用户参与度描述

    末尾附决策路由指令：user-involvement 范围内 → user；范围外 → superior。
    """
    objective = _get_ci(goals, "objective", "")
    focus = _get_ci(goals, "focus", "")
    krs = _get_ci(goals, "keyResults", None) or _get_ci(goals, "key_results", [])
    involvement = _get_ci(
        goals,
        "userInvolvement",
        _get_ci(goals, "user_involvement", DEFAULT_USER_INVOLVEMENT),
    )

    if not krs:
        kr_lines = "  (none yet)"
    else:
        kr_lines = "\n".join(_format_kr(kr) for kr in krs)

    return (
        "## Enterprise Goals Workbook (updated)\n"
        f"**Objective:** {objective}\n"
        f"**Current Focus:** {focus}\n"
        f"**Key Results:**\n{kr_lines}\n"
        f"**User Involvement:** {involvement}\n"
        'Route decisions matching the user-involvement scope to the user '
        '(via `question` or `send_message` to "user"). '
        'For decisions outside this scope, ask your superior '
        '(`send_message` with recipients=["上级花名"]).'
    )


# ── Helpers ─────────────────────────────────────────────────


def _format_kr(kr: Any) -> str:
    """格式化单条 key result：[status] text 或纯文本。"""
    if isinstance(kr, dict):
        text = kr.get("text", "")
        status = kr.get("status", "")
        return f"  - [{status}] {text}"
    if isinstance(kr, str):
        return f"  - {kr}"
    return f"  - {kr!r}"


def _get_ci(d: dict[str, Any], key: str, default: Any) -> Any:
    """Case-insensitive get：先原 key，再 lowercase，再 default。

    兼容 Elixir atom key (:objective) 与 string key ("objective")。
    """
    if key in d:
        return d[key]
    lower = key.lower()
    if lower in d:
        return d[lower]
    return default
