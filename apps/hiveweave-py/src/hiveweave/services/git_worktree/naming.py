"""Branch naming helpers for git worktrees."""
from __future__ import annotations

from .constants import (
    SLUG_MAX_LEN,
    _SLUG_INVALID,
    _SLUG_SPACE,
    _SLUG_TRIM,
)

def _slugify(name: str) -> str:
    """Slugify a task name (契约 09 slugify 规则).

    1. 空格/正反斜杠 → "-"
    2. 删除非 [a-zA-Z0-9_-] 和 CJK 以外字符
    3. 截断至 40 字符
    4. 去除首尾连字符
    5. 空串 → "task"
    """
    s = _SLUG_SPACE.sub("-", name)
    s = _SLUG_INVALID.sub("", s)
    s = s[:SLUG_MAX_LEN]
    s = _SLUG_TRIM.sub("", s)
    return s or "task"


def _branch_name(short_id: str, task_name: str) -> str:
    """LEGACY slug 命名 (P0 之前) — 仅为兼容存量分支保留。

    新代码一律用 compute_branch_name(); 本函数只在解析/清理
    老 slug 分支 (hw/<sid>/<task-slug>) 时作兜底。
    """
    return f"hw/{short_id}/{_slugify(task_name)}"


def compute_branch_name(short_id: str, task_id: str | None = None) -> str:
    """稳定分支命名 (P0) — 从 task_id 派生, 与任务描述文本无关。

    - 有 task_id → ``hw/<shortId>/t-<task_id 前 8 位小写>``
      (同一任务重算必同名, 根治 description[:40] 每次重算导致的分支增生)
    - 无 task_id → ``hw/<shortId>/work`` (每个 agent 一条稳定工作分支)
    """
    tid = (task_id or "").strip().lower()
    if tid:
        return f"hw/{short_id}/t-{tid[:8]}"
    return f"hw/{short_id}/work"
