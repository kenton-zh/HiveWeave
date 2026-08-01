"""Lifecycle hook handler — trigger.context.build: recall experiential lessons.

ChatDev Experiential Co-Learning 的召回端：新任务 dispatch（trigger 上下文构建）
时，按任务文本关键词召回 top-N 项目教训，注入触发上下文。

Input (from build_trigger_context):
    agent_id, project_id, trigger_type, context (built context string)

Output mutation:
    output["lessons_block"]: str | None — "## Past Lessons" 文本块（无命中则不设）

Fail-open：召回失败不影响触发主流程。
"""

from __future__ import annotations

from typing import Any, Mapping, MutableMapping

import structlog

from hiveweave.hooks import TRIGGER_CONTEXT_BUILD, hooks
from hiveweave.services.lessons import LessonService

log = structlog.get_logger(__name__)


async def on_trigger_context_build(
    input: Mapping[str, Any],
    output: MutableMapping[str, Any],
) -> None:
    """Append a lessons block to the trigger context when matches exist."""
    agent_id = input.get("agent_id")
    project_id = input.get("project_id")
    context_text = input.get("context") or ""
    if not agent_id or not project_id or not context_text:
        return

    try:
        keywords = LessonService.extract_keywords(context_text, max_keywords=20)
        lessons = await LessonService().recall_lessons(project_id, keywords)
    except Exception as e:
        log.warning("hook_lessons_recall_failed", error=str(e))
        return

    if not lessons:
        return

    lines: list[str] = []
    for lesson in lessons:
        meta = lesson.get("metadata") or {}
        content = (lesson.get("content") or "").strip()
        parts = [content]
        if meta.get("root_cause"):
            parts.append(f"根因: {meta['root_cause']}")
        if meta.get("fix"):
            parts.append(f"修复: {meta['fix']}")
        lines.append("- " + " | ".join(parts))

    output["lessons_block"] = (
        "## Past Lessons (reports from previous work on similar tasks — "
        "reference only, verify against current repo state before applying)\n"
        + "\n".join(lines)
    )
    log.info(
        "hook_lessons_injected",
        agent_id=agent_id,
        count=len(lessons),
        keywords=keywords,
    )


def register() -> None:
    hooks.register(
        TRIGGER_CONTEXT_BUILD,
        on_trigger_context_build,
        priority=30,
        fail="open",
        name="lessons_recall",
    )
