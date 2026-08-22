"""Task ledger constants (transitions, progress floors, schema cols)."""
from __future__ import annotations

# ADR-001 R1：唯一终态常量。所有"任务是否终结"的消费点统一引用这里，
# 禁止再各写一套终态集合（收敛 platform_state._SCOPE_CLOSED 与
# timeline._TERMINAL_STATUSES 两套旧口径）。未来新增终态只改这一处。
# completed/done 为防御性成员：状态机不产出，但存量数据可能残留，
# 不列入会令闭式 has_open_work 对这类行永真（fail-safe → fail-stuck）。
TERMINAL_STATUSES = frozenset(
    {"closed", "cancelled", "completed", "done", "archived"}
)

# 合法状态转换
_TRANSITIONS: dict[str, set[str]] = {
    "created": {"claimed", "closed", "blocked"},    # blocked: system VERIFY w/o QA
    "claimed": {"running", "created", "blocked"},   # start, unclaim, or queue on deps
    # running → claimed 已移除：防止 LLM 超时后 RESUME 时误调 claim_task
    # 导致 running↔claimed 无限弹跳。如需放弃任务请用 blocked。
    "running": {"blocked", "submitted"},             # 阻塞/提交
    "blocked": {"running", "closed"},               # 解除阻塞或关闭
    "submitted": {"reviewing", "running"},          # 进入评审或撤回
    "reviewing": {"approved", "rework"},            # 审批通过或返工
    "approved": {"verifying", "closed", "rework"},   # VERIFY、关闭、或 merge 冲突返工
    "verifying": {"closed", "approved"},              # VERIFY 通过 → 关闭；VERIFY 取消 → 回 approved
    "rework": {"running"},                          # 返工回到运行
    "closed": set(),                                # 终态
}

# schema.py 的 tasks 表缺列，启动时 ALTER TABLE 补齐（幂等）
_MISSING_COLUMNS = [
    ("due_at", "INTEGER"),
    ("wait_kind", "TEXT"),
    ("wake_at", "INTEGER"),
    ("policy_id", "TEXT"),
    # archive 审计字段（cancel_task 工具写入）：谁在什么时间为什么废弃
    ("archived_by", "TEXT"),
    ("archived_reason", "TEXT"),
    ("archived_at", "INTEGER"),
    # reviewer_id: pinned on submit (default creator); start_review may overwrite
    ("reviewer_id", "TEXT"),
    # Slice-driven work mode P0: declarative acceptance contract
    ("contract_json", "TEXT"),
    # TEST21 M2: first-running locks implementer; reassign must not break evidence
    ("implementer_id", "TEXT"),
    ("implementer_worktree", "TEXT"),
    # TEST21 M5: owner parked — pause task-stall nudges while agent is parked
    ("owner_parked", "INTEGER DEFAULT 0"),
]

# Progress floors driven by lifecycle events (LLM may only raise further)
_PROGRESS_FLOORS: dict[str, int] = {
    "claimed": 10,
    "running": 20,
    "test_attestation": 70,
    "submitted": 90,
    "reviewing": 92,
    "approved": 95,
    "verifying": 97,
    "rework": 40,
    "closed": 100,
}

