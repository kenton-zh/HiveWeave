"""Agent 实时活动状态（八轮 TEST_DSH_38 前端观测缺口）。

前端组织树此前只有组织生命周期静态态（created/active/…），LLM 输出中 /
工具执行中 / 子代理工作中三种「正在干活」的事实无任何呈现。数据其实全在
per-project 库里，本模块把它们推导成 per-agent 的实时活动相位。

三个正交事实位独立推导、独立上报（对齐 DSH defensive-patterns 的正交
上报戒律——合成一个 bool 就会重演「只看到 LLM 一种」）：

- tool      run_steps 存在未收口步骤（ended_at IS NULL）
- llm       chat_messages.is_streaming=1（流式输出中）
- subagent  agent_waits kind='subagent' 且 cleared_at IS NULL
- working   run 开但无未收口步骤（步间空档）
- waiting   其他 kind 的开放等待（task / external…）
- idle      以上皆无

全部只读、best-effort：单表查询失败降级跳过，不让面板白屏。
"""

from __future__ import annotations

import time

import structlog

from .tasks.db import _query

log = structlog.get_logger(__name__)

# 与前端 useChatMessages 的 ZOMBIE_STREAMING_MS 阈值一致：流式行可能因
# 崩溃/强杀残留 is_streaming=1，超龄视作僵尸、不进 live 状态。
_ZOMBIE_STREAMING_MS = 12 * 60 * 1000


def _now_ms() -> int:
    return int(time.time() * 1000)


async def live_status(project_id: str) -> dict:
    """推导 per-agent 实时活动相位。返回 {agents: [...], generated_at}。"""
    now = _now_ms()
    agents_rows = await _safe(
        _query(
            project_id,
            "SELECT id, status, last_active_at FROM agents",
        ),
        "agents",
        [],
    )

    # 开放 run：run 开着（ended_at IS NULL）
    open_runs = {
        r["agent_id"]: r["started_at"]
        for r in await _safe(
            _query(
                project_id,
                "SELECT agent_id, started_at FROM agent_runs "
                "WHERE ended_at IS NULL",
            ),
            "agent_runs",
            [],
        )
        if r["agent_id"]
    }

    # 未收口步骤：挂在开放 run 上（工具执行中）。JOIN 侧必须同过滤
    # ar.ended_at IS NULL——崩溃残留的孤儿步会让 agent 永久显示「工具」。
    open_steps: dict[str, dict] = {}
    for r in await _safe(
        _query(
            project_id,
            "SELECT ar.agent_id AS agent_id, rs.tool_name AS tool_name, "
            "rs.started_at AS started_at FROM run_steps rs "
            "JOIN agent_runs ar ON ar.id = rs.run_id "
            "WHERE rs.ended_at IS NULL AND ar.ended_at IS NULL "
            "ORDER BY rs.started_at ASC",
        ),
        "run_steps",
        [],
    ):
        if r["agent_id"]:
            # 同一 run 内理论上单开放步；多则保留最早（当前卡在的第一步）
            open_steps.setdefault(
                r["agent_id"],
                {"tool": r["tool_name"], "since": r["started_at"]},
            )

    # 流式输出中（is_streaming=1）。SQL 侧先过滤僵尸行（崩溃残留的超龄
    # 流式行会让 MIN 取到旧值、把正在流式的 agent 整体误杀），再取 MIN
    # 作为本轮起点。
    streaming: dict[str, int] = {
        r["agent_id"]: r["since"]
        for r in await _safe(
            _query(
                project_id,
                "SELECT agent_id, MIN(created_at) AS since FROM chat_messages "
                "WHERE role='assistant' AND is_streaming=1 AND created_at > ? "
                "GROUP BY agent_id",
                [now - _ZOMBIE_STREAMING_MS],
            ),
            "chat_messages",
            [],
        )
        if r["agent_id"] and r["since"]
    }

    # 开放等待（subagent / task / external …）；expires_at 已过期的等待
    # 视作已失效（GC 未及清理时不误报）。
    open_waits: dict[str, dict] = {}
    for r in await _safe(
        _query(
            project_id,
            "SELECT agent_id, kind, created_at FROM agent_waits "
            "WHERE cleared_at IS NULL "
            "AND (expires_at IS NULL OR expires_at > ?)",
            [now],
        ),
        "agent_waits",
        [],
    ):
        if not r["agent_id"]:
            continue
        # 同一 agent 多个开放等待：保留最早的（等得最久的那个）
        cur = open_waits.get(r["agent_id"])
        if cur is None or (r["created_at"] or 0) < cur["since"]:
            open_waits[r["agent_id"]] = {
                "kind": str(r["kind"] or "external"),
                "since": r["created_at"],
            }

    out: list[dict] = []
    for a in agents_rows:
        aid = a["id"]
        phase, detail, since = "idle", None, None
        if aid in open_steps:
            phase, since = "tool", open_steps[aid]["since"]
            detail = open_steps[aid]["tool"]
        elif aid in streaming:
            phase, since, detail = "llm", streaming[aid], "LLM 输出中"
        elif open_waits.get(aid, {}).get("kind") == "subagent":
            phase, since, detail = "subagent", open_waits[aid]["since"], "子代理工作中"
        elif aid in open_runs:
            phase, since, detail = "working", open_runs[aid], "运行中"
        elif aid in open_waits:
            phase = "waiting"
            since = open_waits[aid]["since"]
            detail = f"等待:{open_waits[aid]['kind']}"
        out.append(
            {
                "agent_id": aid,
                "status": a["status"],
                "phase": phase,
                "detail": detail,
                "since_ms": since,
            }
        )

    return {"agents": out, "generated_at": now}


async def _safe(coro, table: str, default):
    """单表查询失败降级——面板宁缺勿炸（best-effort 只读）。"""
    try:
        return await coro
    except Exception as e:
        log.warning("agent_activity.query_failed", table=table, error=str(e))
        return default
