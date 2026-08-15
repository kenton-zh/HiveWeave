"""job_kill — stop a live off-turn bash or spawn_subagent job."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from hiveweave.tools.base import tool
from hiveweave.tools.result import ToolResult


class JobKillParams(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    job_id: str = Field(
        alias="jobId",
        description=(
            "Job id returned when the background work started "
            "(bg-bash-… / bg-sub-…)."
        ),
        json_schema_extra={"aliases": ["jobId", "job_id", "id"]},
    )


@tool(
    "job_kill",
    "Request cancellation of a live background bash or spawn_subagent job "
    "by job id. Returns immediately; the job settles as killed once its work "
    "actually stops. Woken with [BASH FAILED] / [SUBAGENT FAILED] if a wait "
    "was armed.",
    requires_workspace=False,
    security_level="standard",
)
async def job_kill_tool(
    params: JobKillParams, agent_id: str, workspace: str
) -> ToolResult:
    from hiveweave.services.offturn import is_live_job, kill_offturn_job

    jid = (params.job_id or "").strip()
    if not jid:
        return ToolResult.err("job_kill requires job_id")
    if not is_live_job(jid) and not jid.startswith(("bg-bash-", "bg-sub-")):
        return ToolResult.err(
            f"job_kill: {jid!r} is not an off-turn job id "
            "(expected bg-bash-… or bg-sub-…)"
        )
    result = await kill_offturn_job(jid, agent_id=agent_id)
    if not result.get("ok"):
        return ToolResult.err(result.get("error") or "kill failed")
    if result.get("already_done"):
        return ToolResult.ok(f"Job {jid} already finished.")
    return ToolResult.ok(
        f"Job {jid} killed. You will be woken with "
        "[BASH FAILED] or [SUBAGENT FAILED] if a wait was armed."
    )
