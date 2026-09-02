"""attest_doc_review tool

Split from tools/task_tools.py (AI-friendly package layout). Behavior unchanged.
"""
from __future__ import annotations

import json
import time
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator

from hiveweave.services import task as _task_svc
from hiveweave.tools.base import tool
from hiveweave.tools import helpers as _helpers

_coerce_to_list = _helpers.coerce_to_list
from hiveweave.tools.result import ToolResult

log = structlog.get_logger(__name__)

# ── attest_doc_review ────────────────────────────────────────


class AttestDocReviewParams(BaseModel):
    """Parameters for attest_doc_review tool."""

    model_config = ConfigDict(populate_by_name=True)

    task_id: str | None = Field(
        default=None,
        alias="taskId",
        description="Optional task to bind this attestation to.",
        json_schema_extra={"aliases": ["taskId", "task_id"]},
    )
    files: list[Any] = Field(
        description=(
            "Files to verify, each {path, minLines?}. Paths relative to the "
            "chosen workspace root (worktree or project main)."
        ),
    )
    source: str = Field(
        default="auto",
        description=(
            "Where to read files: 'worktree' (caller's write worktree), "
            "'main' (project root), or 'auto' (prefer worktree when all "
            "files exist there — use for submit before merge; VERIFY on "
            "main after merge)."
        ),
        json_schema_extra={"aliases": ["source", "workspaceSource", "workspace"]},
    )


@tool(
    "attest_doc_review",
    "Machine-check document deliverables: each file must exist; optional "
    "minLines. Creates a doc_review attestation (sha256, LF-normalized). "
    "source=auto prefers the caller's worktree when files are there "
    "(submit before merge), else project main (post-merge VERIFY). "
    "Tag tasks with docs_only so submit/approve require this kind. "
    "Returns attestationId for submit_task/review evidence.",
    requires_workspace=True,
    security_level="standard",
)
async def attest_doc_review_tool(
    params: AttestDocReviewParams, agent_id: str, workspace: str
) -> ToolResult:
    """Issue doc_review attestation after verifying files on disk."""
    from pathlib import Path

    from hiveweave.services.attestation import create_doc_review

    project_id = await _helpers.get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")

    files = params.files or []
    if isinstance(files, dict):
        files = [files]
    if not isinstance(files, list) or not files:
        return ToolResult.err(
            "attest_doc_review requires files=[{path, minLines?}, ...]"
        )

    try:
        from hiveweave.db import meta as meta_db

        root = await meta_db.get_project_workspace(project_id)
    except Exception:
        root = None
    main_ws = root or workspace

    # Resolve caller's write worktree (if any)
    wt_ws: str | None = None
    branch: str | None = None
    commit: str | None = None
    try:
        from hiveweave.services.git_worktree import _current_branch, _git
        from hiveweave.services.org import OrgService

        agent = await OrgService().get_agent(agent_id)
        cand = (agent or {}).get("workspace_path") or ""
        if cand and Path(cand).is_dir() and (Path(cand) / ".git").exists():
            wt_ws = cand
            branch = await _current_branch(cand)
            ok_h, out_h = await _git(["rev-parse", "HEAD"], cand)
            if ok_h and (out_h or "").strip():
                commit = out_h.strip()
    except Exception:
        wt_ws = None

    def _all_files_exist(base: str) -> bool:
        for entry in files:
            if not isinstance(entry, dict):
                return False
            rel = (entry.get("path") or entry.get("file") or "").strip().replace(
                "\\", "/"
            )
            if not rel or ".." in rel.split("/"):
                return False
            if not (Path(base) / rel).is_file():
                return False
        return True

    source = (params.source or "auto").strip().lower()
    if source in ("worktree", "wt", "agent"):
        if not wt_ws:
            # 39 审计 P2-2：CEO 设计上永无 worktree——"ensure worktree exists"
            # 对它是死路提示。按角色给可执行的替代通道。
            role_note = ""
            try:
                from hiveweave.db.meta import get_agent_by_id

                agent_row = await get_agent_by_id(agent_id)
                if (agent_row or {}).get("role") == "ceo":
                    role_note = (
                        " CEO has no worktree by design — use source=main "
                        "(the merged design doc lives on main)."
                    )
            except Exception:  # noqa: BLE001 — 角色查询失败不影响原提示
                pass
            return ToolResult.err(
                "source=worktree but caller has no valid write worktree. "
                "Use source=main after merge, or ensure worktree exists."
                + role_note
            )
        if not _all_files_exist(wt_ws):
            return ToolResult.err(
                "source=worktree but not all files exist in your worktree. "
                "Write them under your worktree first (do NOT copy to project "
                "root for attestation)."
            )
        check_ws = wt_ws
        ws_kind = "worktree"
    elif source in ("main", "root", "project"):
        check_ws = main_ws
        ws_kind = "main"
        # Stamp main HEAD for VERIFY baseline (TEST6 evening E3)
        if main_ws and not commit:
            try:
                ok_h, out_h = await _git(["rev-parse", "HEAD"], main_ws)
                if ok_h and (out_h or "").strip():
                    commit = out_h.strip()
            except Exception:
                pass
    else:
        # auto: prefer worktree when complete, else main
        if wt_ws and _all_files_exist(wt_ws):
            check_ws = wt_ws
            ws_kind = "worktree"
        else:
            check_ws = main_ws
            ws_kind = "main"
            if main_ws and not commit:
                try:
                    ok_h, out_h = await _git(["rev-parse", "HEAD"], main_ws)
                    if ok_h and (out_h or "").strip():
                        commit = out_h.strip()
                except Exception:
                    pass

    try:
        att_id, report = await create_doc_review(
            project_id,
            agent_id=agent_id,
            task_id=params.task_id,
            files=files,
            workspace=check_ws,
            commit_hash=commit,
        )
    except ValueError as e:
        return ToolResult.err(str(e))
    except Exception as e:
        return ToolResult.err(f"doc_review failed: {e}")

    paths = ", ".join(f["path"] for f in report.get("files") or [])
    extra = ""
    if ws_kind == "worktree":
        extra = (
            f" source=worktree branch={branch or '?'} "
            f"commit={(commit or '')[:12] or '?'}. "
            "After merge, VERIFY should re-attest on main."
        )
    else:
        extra = (
            f" source=main commit={(commit or '')[:12] or '?'}."
        )
    return ToolResult.ok(
        f"doc_review attestation {att_id} recorded for [{paths}] "
        f"under {check_ws}.{extra} Pass attestationIds=[\"{att_id}\"] on "
        f"submit_task / keep in evidence for approve."
    )

