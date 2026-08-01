"""Doom-loop thresholds and progress helpers."""
from __future__ import annotations

DOOM_LOOP_DEFAULT_LIMIT = 3
"""默认 doom loop 阈值 — 同一工具+同一参数连续 N 次中断。"""

# ── 只读/幂等轮询工具豁免集合 ──────────────────────────────
# 实锤事故（生产日志）：归零/拾光/潮汐各被 doom 杀过一次 ——
# 「Doom loop detected: tool get_tasks called 3+ times with same args」。
# agent 没有订阅机制，轮询 get_tasks / read_file 是它获取状态的唯一手段，
# 按默认阈值 3 判定 doom 属于误杀。
# 以 tools/executor.py 实际注册的工具名为准（勿写 service 层方法名）：
DOOM_LOOP_READONLY_TOOLS: frozenset[str] = frozenset({
    # 任务账本 / 状态轮询 — agent 获取任务与进度的唯一通道
    "get_tasks",
    # 文件探索
    "read_file",
    "list_files",
    "grep",
    "search_files",
    # 组织 / 角色 / 技能查询
    "read_charter",
    "read_goals",
    "read_memory",
    "read_roster",
    "read_skill",
    "list_available_skills",
    "view_org_chart",
    "list_subordinates",
    "check_agent_status",
    "get_platform_state",
    "list_agent_templates",
    # 日志 / 告警查询
    "read_work_logs",
    "list_alarms",
    # 科学计算 — 纯函数幂等，心算错误代价高于重复计算
    "calculate",
    # worktree 只读查询
    "git_worktree_list",
    "git_worktree_status",
})
"""只读/幂等工具集合 — 同参重复调用不按 doom 阈值计数，只受保险丝约束。"""

DOOM_LOOP_READONLY_FUSE = 15
"""只读工具保险丝阈值 — 同参连续调用超过 15 次仍熔断。

豁免不等于放任：只读轮询每次往返都烧 token（请求+响应全量进上下文），
15 次同参连续调用已远超正常轮询节奏，熔断防 token 烧钱。
"""

DOOM_LOOP_TOOL_LIMITS: dict[str, int] = {
    # Status polling — tighter than generic readonly (TEST3/TEST4 poll storms)
    "check_agent_status": 5,
    "get_tasks": 6,
    # 审查工具 — 中容忍，重试可能是 LLM 纠正输出格式
    "run_code_review": 6,
    "run_security_audit": 6,
    "run_tests": 6,
    "run_perf_audit": 6,
    "run_full_review": 6,
    # H5 harness — one case may need retries; mid tolerance
    "game_run_case": 6,
    # 每轮强制出口 — 被出口闸门拒收后必须重试；同参指纹才计数
    # （井字棋实测：CEO 首条指令即撞 doom，无任何正常输出）
    "commit_turn": 8,
    # 幂等写入 — 中容忍，覆盖写入无害但不应无限重复
    "write_file": 8,
    "save_charter": 8,
    "save_goals": 8,
    "save_memory": 8,
    "update_roster": 8,
    "todowrite": 8,
    "mark_read": 8,
    "write_work_log": 8,
    "update_goals": 8,
    # 外发消息 — 低容忍，避免刷屏
    "send_message": 5,
    "question": 4,
    # 副作用工具 — 最低容忍，防止真实损害
    "bash": 3,
    "apply_patch": 3,
    "websearch": 3,
    "execute_code": 3,
    # Paid Ark media — keep tight; retries burn quota
    "generate_image": 3,
}
"""Per-tool doom loop thresholds（写类/副作用工具）。不同工具不同限制：

- 审查工具 (6次): LLM 可能在纠正输出格式，需要更多尝试
- 幂等写入 (8次): 覆盖写入无害，但不应无限重复
- 外发消息 (4-5次): 避免对其他 agent 或用户造成骚扰
- 副作用工具 (3次): 真实命令执行，严格限制防止损害

只读工具不在此表 —— 见 DOOM_LOOP_READONLY_TOOLS + DOOM_LOOP_READONLY_FUSE。
"""


def doom_loop_limit(tool_name: str) -> int:
    """返回工具的 doom 阈值：显式表优先，只读工具走保险丝，其余默认 3。"""
    if tool_name in DOOM_LOOP_TOOL_LIMITS:
        return DOOM_LOOP_TOOL_LIMITS[tool_name]
    if tool_name in DOOM_LOOP_READONLY_TOOLS:
        return DOOM_LOOP_READONLY_FUSE
    return DOOM_LOOP_DEFAULT_LIMIT


def round_made_progress(
    tool_calls: list[dict],
    *,
    error_ids: set[str] | None = None,
    duplicate_ids: set[str] | None = None,
) -> bool:
    """True if this tool-loop round advanced work (DESIGN-2 stall counter).

    A round counts as progress when at least one non-readonly tool succeeded
    (not in error_ids / duplicate_ids). Pure readonly polling = no progress.
    """
    errs = error_ids or set()
    dups = duplicate_ids or set()
    for tc in tool_calls:
        name = tc.get("name") or ""
        tid = tc.get("id") or ""
        if name in DOOM_LOOP_READONLY_TOOLS:
            continue
        if tid in errs or tid in dups:
            continue
        return True
    return False


def round_was_readonly_only(
    tool_calls: list[dict],
    *,
    error_ids: set[str] | None = None,
    duplicate_ids: set[str] | None = None,
) -> bool:
    """True if the round had only successful readonly tools (or was empty).

    Used to apply TOOL_LOOP_READONLY_STALL_LIMIT instead of the stricter
    mutating stall limit.
    """
    if not tool_calls:
        return True
    errs = error_ids or set()
    dups = duplicate_ids or set()
    saw_ok_readonly = False
    for tc in tool_calls:
        name = tc.get("name") or ""
        tid = tc.get("id") or ""
        if tid in errs or tid in dups:
            return False
        if name not in DOOM_LOOP_READONLY_TOOLS:
            return False
        saw_ok_readonly = True
    return saw_ok_readonly

