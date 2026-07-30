"""Submit task + evidence workspace resolution."""
from __future__ import annotations

import json
import time
import uuid

import structlog

from .db import _conn, _ensure_schema, _execute, _execute_tx, _query

log = structlog.get_logger(__name__)


class SubmitMixin:
    """submit_task / _resolve_evidence_workspace."""

    async def submit_task(self, project_id: str, task_id: str,
                          evidence: dict) -> None:
        """Submit a task (running → submitted). Sets evidence (JSON) + submitted_at.

        BUG-P1b: 保留既有 evidence.merged_by —— VERIFY spawn 时写入的
        合并人标记是 review_task 独立审门排除合并人的唯一依据，submit
        整体覆盖 evidence 会让该门禁失效。

        TEST11 #3: on submit, pin ``reviewer_id`` (default creator_id) so the
        designated reviewer has obligations from the submitted window onward —
        not only after they call start_review.

        Slice P0: if ``contract_json`` present, L0 machine clauses must pass
        against the assignee worktree (or project root) before transition.
        """
        task_id = await self.require_task_id(project_id, task_id)
        task = await self.get_task(project_id, task_id)

        # SUBMITTED MACHINE PRE-RUN (slice-driven L0)
        if task and task.get("contract_json"):
            from hiveweave.services.task_contract import (
                ensure_slice_status,
                format_prerun_failure,
                parse_contract,
                run_machine_acceptance,
            )

            contract = parse_contract(task.get("contract_json"))
            if contract:
                ws_root = await self._resolve_evidence_workspace(
                    project_id, task
                )
                prerun = run_machine_acceptance(
                    contract, workspace_root=ws_root
                )
                contract = dict(contract)
                contract["machine_pre_run"] = {
                    **prerun.to_dict(),
                    "at_ms": int(time.time() * 1000),
                    "workspace": str(ws_root),
                }
                if not prerun.passed:
                    await self._persist_contract_json(
                        project_id, task_id, contract
                    )
                    raise ValueError(format_prerun_failure(prerun))
                contract = ensure_slice_status(contract, "submitted")
                await self._persist_contract_json(
                    project_id, task_id, contract
                )
                if isinstance(evidence, dict):
                    evidence = dict(evidence)
                    evidence["machine_pre_run"] = contract["machine_pre_run"]

        await self._transition(project_id, task_id, "submitted",
                               actor_id=(task or {}).get("assignee_id"))
        if isinstance(evidence, dict) and "merged_by" not in evidence:
            rows0 = await _query(
                project_id, "SELECT evidence FROM tasks WHERE id = ?", [task_id]
            )
            if rows0 and rows0[0]["evidence"]:
                try:
                    prev = rows0[0]["evidence"]
                    prev = json.loads(prev) if isinstance(prev, str) else dict(prev)
                except (json.JSONDecodeError, TypeError):
                    prev = {}
                if isinstance(prev, dict) and prev.get("merged_by"):
                    evidence = dict(evidence)
                    evidence["merged_by"] = prev["merged_by"]
        now_ms = int(time.time() * 1000)
        # Pin reviewer at submit: existing column wins; evidence.reviewer_id
        # only fills when column is empty (non-VERIFY). VERIFY always → creator.
        meta_rows = await _query(
            project_id,
            "SELECT assignee_id, creator_id, reviewer_id, tags, title "
            "FROM tasks WHERE id = ?",
            [task_id],
        )
        agent_id = meta_rows[0]["assignee_id"] if meta_rows else None
        reviewer_id = None
        if meta_rows:
            creator_id = meta_rows[0]["creator_id"]
            existing_reviewer = meta_rows[0]["reviewer_id"]
            draft = {
                "tags": meta_rows[0]["tags"],
                "title": meta_rows[0]["title"],
            }
            if self._is_verify_task(draft):
                reviewer_id = creator_id
            elif existing_reviewer:
                reviewer_id = existing_reviewer
            elif isinstance(evidence, dict) and evidence.get("reviewer_id"):
                reviewer_id = str(evidence["reviewer_id"])
            else:
                reviewer_id = creator_id
        if reviewer_id:
            await _execute(
                project_id,
                "UPDATE tasks SET evidence = ?, submitted_at = ?, "
                "reviewer_id = ?, updated_at = ? WHERE id = ?",
                [json.dumps(evidence), now_ms, reviewer_id, now_ms, task_id],
            )
        else:
            await _execute(
                project_id,
                "UPDATE tasks SET evidence = ?, submitted_at = ?, updated_at = ? "
                "WHERE id = ?",
                [json.dumps(evidence), now_ms, now_ms, task_id],
            )
        await self.emit_task_event(
            project_id,
            task_id,
            "submitted",
            agent_id=agent_id,
            summary=f"[submitted] task {task_id[:8]}",
        )

    async def _resolve_evidence_workspace(
        self, project_id: str, task: dict
    ) -> str:
        """Prefer assignee write worktree; fall back to project root."""
        from hiveweave.db import meta as meta_db

        project_ws = await meta_db.get_project_workspace(project_id) or ""
        assignee_id = task.get("assignee_id")
        if not assignee_id:
            return project_ws
        try:
            from hiveweave.services.org import OrgService

            agent = await OrgService().get_agent(str(assignee_id))
            wt = (agent or {}).get("workspace_path") or ""
            if wt:
                from pathlib import Path

                if Path(wt).is_dir():
                    return wt
        except Exception as e:
            log.debug(
                "evidence_workspace_fallback",
                task_id=(task.get("id") or "")[:12],
                error=str(e),
            )
        return project_ws

