"""Durable Run Ledger — persists agent execution steps for recovery and audit.

Three tables:
- agent_activations: who was woken, by what event
- agent_runs: each execution of chat(), with budget and status
- run_steps: each LLM request, tool call, tool result — written incrementally

Key design:
- Steps are written immediately after each tool completes (not batched)
- On timeout/error, run is marked interrupted; steps survive
- On next activation, interrupted runs generate a checkpoint summary
- chat_messages and conversation_turns remain as UI/semantic views;
  run_steps is the audit trail
"""

import asyncio
import hashlib
import json
import sqlite3
import time
import uuid
from typing import Any

import structlog

from hiveweave.db import project as project_db

log = structlog.get_logger()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _short_hash(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]


class RunLedger:
    """Per-project run ledger service.

    All methods are async and operate on the per-project DB for the given agent.
    Errors are logged but never raised — the ledger is best-effort and must not
    block agent execution.
    """

    async def create_activation(
        self,
        agent_id: str,
        trigger_type: str,
        trigger_source: str = "",
        trigger_detail: str = "",
        inbox_msg_ids: list[str] | None = None,
        interrupted_run_id: str | None = None,
        checkpoint_summary: str | None = None,
    ) -> str:
        """Create an activation record when an agent is woken."""
        # M3 孤儿步骤清扫：run 已结束（status != 'running'）却仍 status='running'
        # 的 run_steps 是 record_step_end 写回丢失的孤儿（slack-clone_03 实测
        # 6 行跨 17h 残留），统一标 error 收尾。安全边界：仍 running 的 run
        # 被排除——其工具循环可能正在执行，步骤仍会被补 end；已结束 run 的
        # 工具循环已死，不可能再补写。选 error 而非 interrupted：
        # interrupted 保留恢复语义（generate_checkpoint 会为中断 run 生成
        # 摘要），孤儿步骤是永不收尾的悬挂项，标 error 使其被如实计入
        # 失败分支而非误报为进行中。best-effort，失败不影响激活创建。
        try:
            await project_db.execute(
                agent_id,
                "UPDATE run_steps SET status = 'error', ended_at = ?, error = ? "
                "WHERE run_id IN (SELECT id FROM agent_runs "
                "WHERE agent_id = ? AND status != 'running') "
                "AND status = 'running'",
                [_now_ms(), "orphan step swept: run ended while step running", agent_id],
            )
        except Exception as e:
            log.warning("run_ledger.orphan_step_sweep_failed", agent_id=agent_id, error=str(e))
        activation_id = str(uuid.uuid4())
        now = _now_ms()
        try:
            await project_db.execute(
                agent_id,
                "INSERT INTO agent_activations "
                "(id, agent_id, trigger_type, trigger_source, trigger_detail, "
                "inbox_msg_ids, interrupted_run_id, checkpoint_summary, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    activation_id,
                    agent_id,
                    trigger_type,
                    trigger_source,
                    trigger_detail,
                    json.dumps(inbox_msg_ids or []),
                    interrupted_run_id,
                    checkpoint_summary,
                    now,
                ],
            )
        except Exception as e:
            log.warning("run_ledger.create_activation_failed", agent_id=agent_id, error=str(e))
        return activation_id

    async def create_run(
        self,
        agent_id: str,
        activation_id: str,
        budget_llm_calls: int = 50,
        budget_tool_calls: int = 100,
        budget_elapsed_ms: int = 600_000,
    ) -> str:
        """Create a run record when _run_llm starts."""
        run_id = str(uuid.uuid4())
        now = _now_ms()
        lease_expires = now + budget_elapsed_ms
        try:
            await project_db.execute(
                agent_id,
                "INSERT INTO agent_runs "
                "(id, agent_id, activation_id, status, lease_expires_at, "
                "budget_llm_calls, budget_tool_calls, budget_elapsed_ms, "
                "actual_llm_calls, actual_tool_calls, started_at) "
                "VALUES (?, ?, ?, 'running', ?, ?, ?, ?, 0, 0, ?)",
                [
                    run_id,
                    agent_id,
                    activation_id,
                    lease_expires,
                    budget_llm_calls,
                    budget_tool_calls,
                    budget_elapsed_ms,
                    now,
                ],
            )
            # Link activation to run
            await project_db.execute(
                agent_id,
                "UPDATE agent_activations SET run_id = ?, consumed_at = ? WHERE id = ?",
                [run_id, now, activation_id],
            )
        except Exception as e:
            log.warning("run_ledger.create_run_failed", agent_id=agent_id, error=str(e))
        return run_id

    async def record_step_start(
        self,
        agent_id: str,
        run_id: str,
        step_index: int,
        step_type: str,
        tool_name: str | None = None,
        tool_call_id: str | None = None,
        tool_args_hash: str | None = None,
    ) -> str | None:
        """Record the start of a step (LLM round or tool call)."""
        step_id = str(uuid.uuid4())
        now = _now_ms()
        try:
            await project_db.execute(
                agent_id,
                "INSERT INTO run_steps "
                "(id, run_id, step_index, step_type, tool_name, tool_call_id, "
                "tool_args_hash, status, started_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?)",
                [
                    step_id,
                    run_id,
                    step_index,
                    step_type,
                    tool_name,
                    tool_call_id,
                    tool_args_hash,
                    now,
                ],
            )
        except Exception as e:
            log.warning("run_ledger.record_step_start_failed", agent_id=agent_id, error=str(e))
            return None
        return step_id

    async def record_step_end(
        self,
        agent_id: str,
        step_id: str,
        status: str = "completed",
        result_hash: str | None = None,
        result_size: int | None = None,
        error: str | None = None,
        result_excerpt: str | None = None,
    ) -> None:
        """Record the end of a step.

        result_excerpt: TEST10 观测性修复 — 截断 2KB 的结果摘录。
        此前 run_steps 只存 result_hash/size，conversation 裁剪后
        约 12% 的工具结果在 DB 中完全不可找回，审计/排障无据可查。
        """
        now = _now_ms()
        if result_excerpt and len(result_excerpt) > 2048:
            result_excerpt = result_excerpt[:2048] + "…[truncated]"
        try:
            # Calculate duration from started_at — SELECT 失败只影响 duration，
            # 降级 now 不重试
            try:
                rows = await project_db.query(
                    agent_id,
                    "SELECT started_at FROM run_steps WHERE id = ?",
                    [step_id],
                )
                started_at = rows[0]["started_at"] if rows else now
            except Exception:
                started_at = now
            duration = now - started_at if started_at else 0
            sql = (
                "UPDATE run_steps SET status = ?, result_hash = ?, "
                "result_size = ?, result_excerpt = ?, error = ?, "
                "ended_at = ?, duration_ms = ? "
                "WHERE id = ?"
            )
            params = [status, result_hash, result_size, result_excerpt, error,
                      now, duration, step_id]
            # M3 有界重试：仅对 sqlite3.OperationalError（锁竞争/瞬断，db 层
            # busy_timeout=5s 之后的第二道保险）重试 2 次，50/100ms 退避。
            # ProjectDbError（workspace 驱逐等）重试无意义，直接交给外层
            # 统一告警（绝不外抛）。耗尽后抛给外层统一告警。
            last_error: Exception | None = None
            for attempt in range(3):
                try:
                    await project_db.execute(agent_id, sql, params)
                    last_error = None
                    break
                except sqlite3.OperationalError as e:
                    last_error = e
                    if attempt < 2:
                        await asyncio.sleep(0.05 * (2 ** attempt))
                except Exception as e:
                    last_error = e
                    break
            if last_error is not None:
                raise last_error
        except Exception as e:
            log.warning("run_ledger.record_step_end_failed", agent_id=agent_id, error=str(e))

    async def increment_llm_calls(self, agent_id: str, run_id: str) -> None:
        """Increment the LLM call counter for a run."""
        try:
            await project_db.execute(
                agent_id,
                "UPDATE agent_runs SET actual_llm_calls = actual_llm_calls + 1 WHERE id = ?",
                [run_id],
            )
        except Exception as e:
            log.warning("run_ledger.increment_llm_calls_failed", error=str(e))

    async def increment_tool_calls(self, agent_id: str, run_id: str) -> None:
        """Increment the tool-call counter for a run (BUG-7)."""
        try:
            await project_db.execute(
                agent_id,
                "UPDATE agent_runs SET actual_tool_calls = actual_tool_calls + 1 "
                "WHERE id = ?",
                [run_id],
            )
        except Exception as e:
            log.warning("run_ledger.increment_tool_calls_failed", error=str(e))

    async def complete_run(
        self,
        agent_id: str,
        run_id: str,
        result_summary: str = "",
    ) -> None:
        """Mark a run as completed."""
        now = _now_ms()
        try:
            await project_db.execute(
                agent_id,
                "UPDATE agent_runs SET status = 'completed', ended_at = ?, "
                "result_summary = ? WHERE id = ?",
                [now, result_summary[:500], run_id],
            )
        except Exception as e:
            log.warning("run_ledger.complete_run_failed", error=str(e))

    async def interrupt_run(
        self,
        agent_id: str,
        run_id: str,
        reason: str,
        checkpoint_data: dict | None = None,
    ) -> None:
        """Mark a run as interrupted (timeout/error/cancel).

        Preserves all completed steps for recovery.
        """
        now = _now_ms()
        checkpoint_json = json.dumps(checkpoint_data, ensure_ascii=False) if checkpoint_data else None
        try:
            await project_db.execute(
                agent_id,
                "UPDATE agent_runs SET status = 'interrupted', ended_at = ?, "
                "error_reason = ?, checkpoint_data = ? WHERE id = ?",
                [now, reason[:500], checkpoint_json, run_id],
            )
        except Exception as e:
            log.warning("run_ledger.interrupt_run_failed", error=str(e))

    async def error_run(
        self,
        agent_id: str,
        run_id: str,
        error_reason: str,
    ) -> None:
        """Mark a run as errored."""
        now = _now_ms()
        try:
            await project_db.execute(
                agent_id,
                "UPDATE agent_runs SET status = 'error', ended_at = ?, "
                "error_reason = ? WHERE id = ?",
                [now, error_reason[:500], run_id],
            )
        except Exception as e:
            log.warning("run_ledger.error_run_failed", error=str(e))

    async def find_interrupted_run(self, agent_id: str) -> dict | None:
        """Find the most recent interrupted run for an agent."""
        try:
            rows = await project_db.query(
                agent_id,
                "SELECT id, agent_id, activation_id, started_at, ended_at, "
                "error_reason, checkpoint_data, actual_llm_calls, actual_tool_calls "
                "FROM agent_runs WHERE agent_id = ? AND status = 'interrupted' "
                "ORDER BY ended_at DESC LIMIT 1",
                [agent_id],
            )
            if rows:
                r = rows[0]
                return {
                    "run_id": r["id"],
                    "agent_id": r["agent_id"],
                    "activation_id": r["activation_id"],
                    "started_at": r["started_at"],
                    "ended_at": r["ended_at"],
                    "error_reason": r["error_reason"],
                    "checkpoint_data": r["checkpoint_data"],
                    "actual_llm_calls": r["actual_llm_calls"],
                    "actual_tool_calls": r["actual_tool_calls"],
                }
        except Exception as e:
            log.warning("run_ledger.find_interrupted_run_failed", error=str(e))
        return None

    async def get_run_steps(self, agent_id: str, run_id: str) -> list[dict]:
        """Get all steps for a run (for checkpoint generation)."""
        try:
            rows = await project_db.query(
                agent_id,
                "SELECT step_index, step_type, tool_name, tool_call_id, "
                "tool_args_hash, status, result_hash, result_size, "
                "result_excerpt, error, "
                "started_at, ended_at, duration_ms "
                "FROM run_steps WHERE run_id = ? ORDER BY step_index ASC",
                [run_id],
            )
            return [dict(r) for r in rows]
        except Exception as e:
            log.warning("run_ledger.get_run_steps_failed", error=str(e))
            return []

    async def generate_checkpoint(self, agent_id: str, run_id: str) -> str:
        """Generate a human-readable checkpoint summary from interrupted run steps."""
        steps = await self.get_run_steps(agent_id, run_id)
        if not steps:
            return "No steps recorded before interruption."

        lines = []
        tool_calls = [s for s in steps if s["step_type"] == "tool_call" and s["status"] == "completed"]
        llm_rounds = [s for s in steps if s["step_type"] == "llm_request"]

        lines.append(f"Interrupted run had {len(llm_rounds)} LLM round(s) and {len(tool_calls)} completed tool call(s).")
        lines.append("Completed tool calls:")
        for s in tool_calls:
            tn = s.get("tool_name") or "unknown"
            dur = s.get("duration_ms") or 0
            lines.append(f"  - {tn} ({dur}ms) result_hash={s.get('result_hash', 'n/a')}")

        # 清扫写入的孤儿步骤状态为 'error'（区别于失败 'failed' 与恢复
        # 语义 'interrupted'），checkpoint 统计必须同样纳入，否则孤儿步骤
        # 在摘要里静默缺失（审计 P2）。
        failed = [s for s in steps if s["status"] in ("failed", "error")]
        if failed:
            lines.append(f"Failed steps: {len(failed)}")
            for s in failed:
                lines.append(f"  - {s.get('tool_name', s['step_type'])}: {s.get('error', 'unknown')}")

        summary = "\n".join(lines)
        log.info("run_ledger.checkpoint_generated", agent_id=agent_id, run_id=run_id, steps=len(steps))
        return summary

    async def get_step_count(self, agent_id: str, run_id: str) -> int:
        """Get the total number of steps for a run."""
        try:
            rows = await project_db.query(
                agent_id,
                "SELECT COUNT(*) as cnt FROM run_steps WHERE run_id = ?",
                [run_id],
            )
            return rows[0]["cnt"] if rows else 0
        except Exception:
            return 0

    async def check_budget(
        self, agent_id: str, run_id: str
    ) -> tuple[bool, str]:
        """Check if the run has exceeded its call budgets.

        Wall-clock elapsed is not a stop condition here: long coding is
        expected to outlive 10 minutes across budget-checked slices (the
        streamer-level turn wrap handles per-slice wall clock; see
        ``hiveweave.llm.streamer.constants``).
        """
        try:
            rows = await project_db.query(
                agent_id,
                "SELECT actual_llm_calls, actual_tool_calls, "
                "budget_llm_calls, budget_tool_calls "
                "FROM agent_runs WHERE id = ?",
                [run_id],
            )
            if not rows:
                return False, ""
            r = rows[0]
            llm = r["actual_llm_calls"]
            tools = r["actual_tool_calls"]
            if llm >= r["budget_llm_calls"]:
                return True, f"llm_calls {llm} >= {r['budget_llm_calls']}"
            if tools >= r["budget_tool_calls"]:
                return True, f"tool_calls {tools} >= {r['budget_tool_calls']}"
            return False, ""
        except Exception as e:
            log.debug("run_ledger.check_budget_failed", error=str(e))
            return False, ""

    async def extend_elapsed_budget(
        self, agent_id: str, run_id: str, extra_ms: int
    ) -> None:
        """Credit back elapsed budget for a subagent spawn.

        Subagent runs synchronously inside the parent's turn; its wall-clock
        time must not starve the parent's own budget. Shifting started_at
        earlier by extra_ms gives the parent back that window.
        """
        if extra_ms <= 0:
            return
        try:
            await project_db.execute(
                agent_id,
                "UPDATE agent_runs SET started_at = started_at - ? WHERE id = ?",
                [extra_ms, run_id],
            )
        except Exception as e:
            log.warning("run_ledger.extend_budget_failed",
                        agent_id=agent_id, error=str(e))


# Singleton
run_ledger = RunLedger()


async def sweep_stale_agent_runs(workspace_path: str | None) -> int:
    """E16 (复盘 P2)：启动收尾 sweep —— 清算上次进程残留的 running runs。

    Agent 长驻服务在 turn 中途被杀时，agent_runs 会残留 ``status='running'``
    的孤儿行（无 ended_at），污染查询/统计并掩盖真实收尾语义。启动时把
    workspace 内所有仍为 running 的 run 归并为 ``interrupted``（预留步骤，
    与现有 interrupt_run 语义一致，供恢复/审计读取）。返回清理行数。
    """
    if not workspace_path:
        return 0
    now = _now_ms()
    reason = "startup_sweep: stale running run from prior process"
    try:
        conn = await project_db.ensure_project_db(workspace_path)
        cursor = await conn.execute(
            "SELECT id FROM agent_runs WHERE status = 'running'"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        cursor = await conn.execute(
            "UPDATE agent_runs SET status = 'interrupted', ended_at = ?, "
            "error_reason = ? WHERE status = 'running'",
            [now, reason],
        )
        await cursor.close()
        if rows:
            log.info(
                "run_ledger.startup_sweep",
                workspace=str(workspace_path),
                primary_count=len(rows),
            )
        return len(rows)
    except Exception as e:
        log.debug("run_ledger.startup_sweep_failed",
                  workspace=str(workspace_path), error=str(e))
        return 0
