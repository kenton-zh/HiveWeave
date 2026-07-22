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

import hashlib
import json
import time
import uuid
from typing import Any

import structlog

from hiveweave.db import project_db

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
    ) -> None:
        """Record the end of a step."""
        now = _now_ms()
        try:
            # Calculate duration from started_at
            rows = await project_db.query(
                agent_id,
                "SELECT started_at FROM run_steps WHERE id = ?",
                [step_id],
            )
            started_at = rows[0]["started_at"] if rows else now
            duration = now - started_at if started_at else 0
            await project_db.execute(
                agent_id,
                "UPDATE run_steps SET status = ?, result_hash = ?, "
                "result_size = ?, error = ?, ended_at = ?, duration_ms = ? "
                "WHERE id = ?",
                [status, result_hash, result_size, error, now, duration, step_id],
            )
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
                "tool_args_hash, status, result_hash, result_size, error, "
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

        failed = [s for s in steps if s["status"] == "failed"]
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


# Singleton
run_ledger = RunLedger()
