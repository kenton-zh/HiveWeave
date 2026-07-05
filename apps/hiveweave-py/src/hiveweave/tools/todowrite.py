"""todowrite tool — agent maintains a structured task list.

契约 02: 工具执行器 — todowrite 子模块
- 全量替换：每次调用覆盖该 agent 的所有 todos（与 TS/Elixir 一致）
- 持久化到 per-project DB 的 todos 表
- 状态: pending / in_progress / completed / cancelled
- 优先级: low / medium / high
- 返回计数摘要（completed/in-progress/pending/cancelled）
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.db import project as project_db

log = structlog.get_logger(__name__)

VALID_STATUSES = frozenset({
    "pending", "in_progress", "completed", "cancelled",
})
VALID_PRIORITIES = frozenset({"low", "medium", "high"})


async def execute_todowrite(
    agent_id: str,
    todos: list[dict[str, Any]],
) -> dict[str, Any]:
    """Replace the agent's todo list with `todos`.

    Each todo: {content, status, priority?}
    """
    if not isinstance(todos, list):
        return {"success": False, "output": "",
                "error": "Error: todos must be an array"}

    project_id = await meta_db.get_agent_project_id(agent_id) or ""
    now_ms = int(time.time() * 1000)

    # Normalize and validate
    normalized: list[dict[str, Any]] = []
    for entry in todos:
        if not isinstance(entry, dict):
            continue
        content = entry.get("content") or entry.get("task") or ""
        if not content:
            continue
        status = (entry.get("status") or "pending").strip().lower()
        if status not in VALID_STATUSES:
            status = "pending"
        priority = (entry.get("priority") or "medium").strip().lower()
        if priority not in VALID_PRIORITIES:
            priority = "medium"
        normalized.append({
            "content": content,
            "status": status,
            "priority": priority,
        })

    # Delete existing todos for this agent, then insert new ones
    try:
        await project_db.execute(
            agent_id,
            "DELETE FROM todos WHERE agent_id = ?",
            [agent_id],
        )

        for todo in normalized:
            todo_id = str(uuid.uuid4())
            await project_db.execute(
                agent_id,
                """INSERT INTO todos
                   (id, agent_id, project_id, content, status, priority,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [todo_id, agent_id, project_id, todo["content"],
                 todo["status"], todo["priority"], now_ms, now_ms],
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("todowrite.persist_failed", error=str(exc))
        return {"success": False, "output": "",
                "error": f"Error: {exc}"}

    # Counts
    counts = {"pending": 0, "in_progress": 0, "completed": 0, "cancelled": 0}
    for todo in normalized:
        counts[todo["status"]] = counts.get(todo["status"], 0) + 1

    summary = (
        f"Tasks updated: {counts['completed']} done, "
        f"{counts['in_progress']} in progress, "
        f"{counts['pending']} pending"
        + (f", {counts['cancelled']} cancelled"
           if counts["cancelled"] else "")
        + "."
    )
    log.info("todowrite.updated", agent_id=agent_id,
             total=len(normalized), counts=counts)
    return {"success": True, "output": summary, "error": None}


async def get_todos(agent_id: str) -> list[dict[str, Any]]:
    """Return the agent's current todos (read helper, not a tool)."""
    try:
        rows = await project_db.query(
            agent_id,
            """SELECT content, status, priority, updated_at
               FROM todos WHERE agent_id = ?
               ORDER BY created_at ASC""",
            [agent_id],
        )
        return [dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        log.warning("todowrite.get_failed", error=str(exc))
        return []
