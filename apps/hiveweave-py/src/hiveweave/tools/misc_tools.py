"""Misc tools: Git worktree, legacy task tools, message_user, webfetch.

Migrated from executor.py ``_tool_*`` methods and inline dispatch code to
``@tool``-registered standalone functions.

Tools:
    Git worktree:  git_worktree_create, git_worktree_list,
                   git_worktree_merge, git_worktree_remove,
                   git_worktree_status, git_worktree_checkpoint
    Legacy tasks:  report_completion, request_review,
                   approve_work, reject_work
    Other:         message_user, webfetch
"""

from __future__ import annotations

import html as html_mod
import ipaddress
import re
from typing import Any
from urllib.parse import urlparse

import structlog

from pydantic import BaseModel, Field, ConfigDict

from .base import tool
from .result import ToolResult
from .helpers import get_project_id

log = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# Section 1: Git worktree tools
# ═══════════════════════════════════════════════════════════════════════


async def _get_worktree_context(
    agent_id: str, ctx: Any = None
) -> tuple[str, str, str] | ToolResult:
    """Resolve workspace_path and short_id for git worktree operations.

    Returns ``(workspace_path, short_id, project_id)`` on success,
    or a ``ToolResult`` error on failure.
    """
    from hiveweave.db import meta as meta_db

    project_id = await get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")

    ws_path = await meta_db.get_project_workspace(project_id)
    if not ws_path:
        return ToolResult.err(
            f"No workspace path for project {project_id}"
        )
    workspace_path = str(ws_path)

    # Resolve agent short_id for worktree naming
    short_id = agent_id[:8]
    if ctx and getattr(ctx, "org", None):
        agent_rec = await ctx.org.get_agent(agent_id)
        if agent_rec:
            short_id = agent_rec.get("short_id", agent_id[:8])
    else:
        try:
            from hiveweave.services.org import OrgService

            org = OrgService()
            agent_rec = await org.get_agent(agent_id)
            if agent_rec:
                short_id = agent_rec.get("short_id", agent_id[:8])
        except Exception:
            pass

    return workspace_path, short_id, project_id


# ── git_worktree_create ──────────────────────────────────


class GitWorktreeCreateParams(BaseModel):
    """Parameters for git_worktree_create tool."""

    model_config = ConfigDict(populate_by_name=True)

    branch_name: str = Field(
        alias="branchName",
        description=(
            "Branch/task name for the worktree. A unique branch will be "
            "generated from this name and the agent's short_id."
        ),
        json_schema_extra={
            "aliases": ["branchName", "branch_name", "branch", "name", "taskName", "task_name", "task"]
        },
    )
    base_branch: str | None = Field(
        default=None,
        alias="baseBranch",
        description="Base branch to create from (default: main).",
        json_schema_extra={"aliases": ["baseBranch", "base_branch", "base"]},
    )


@tool(
    "git_worktree_create",
    "Create an isolated git worktree on a new branch for safe parallel "
    "development.",
    requires_workspace=True,
    security_level="standard",
)
async def git_worktree_create_tool(
    params: GitWorktreeCreateParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Create a git worktree."""
    # BUG-034: 防止嵌套 worktree — 如果 agent 已在 worktree 中，拒绝创建
    if ".hiveweave" in workspace and "worktrees" in workspace:
        return ToolResult.err(
            "You are already inside a worktree. Do NOT create nested worktrees. "
            "Write code directly in your current directory. "
            "Use git_worktree_checkpoint to save progress, "
            "git_worktree_merge to merge to main."
        )

    from hiveweave.services.git_worktree import GitWorktreeService

    wt_ctx = await _get_worktree_context(agent_id, ctx)
    if isinstance(wt_ctx, ToolResult):
        return wt_ctx
    workspace_path, short_id, _ = wt_ctx

    gwt = GitWorktreeService()
    await gwt.ensure_git_repo(workspace_path)

    task_name = params.branch_name or "task"
    base_branch = params.base_branch or "main"

    result = await gwt.create(workspace_path, short_id, str(task_name), base_branch)
    if result.get("success"):
        return ToolResult.ok(
            f"Worktree created at {result.get('path')} "
            f"on branch {result.get('branch')}"
        )
    return ToolResult.err(result.get("message", "Failed to create worktree"))


# ── git_worktree_list ────────────────────────────────────


class GitWorktreeListParams(BaseModel):
    """Parameters for git_worktree_list tool."""

    model_config = ConfigDict(populate_by_name=True)


@tool(
    "git_worktree_list",
    "List all active git worktrees with their branch names and paths.",
    requires_workspace=True,
    security_level="standard",
)
async def git_worktree_list_tool(
    params: GitWorktreeListParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """List all git worktrees."""
    from hiveweave.services.git_worktree import GitWorktreeService

    wt_ctx = await _get_worktree_context(agent_id, ctx)
    if isinstance(wt_ctx, ToolResult):
        return wt_ctx
    workspace_path, _, _ = wt_ctx

    gwt = GitWorktreeService()
    await gwt.ensure_git_repo(workspace_path)

    result = await gwt.list(workspace_path)
    if result.get("success"):
        wts = result.get("worktrees", result.get("entries", []))
        if not wts:
            return ToolResult.ok("No active worktrees")
        lines = [
            f"{w.get('short_id', '?')}: {w.get('branch', '?')} "
            f"({w.get('status', '?')})"
            for w in wts
        ]
        return ToolResult.ok("\n".join(lines))
    return ToolResult.err(result.get("message", "Failed to list worktrees"))


# ── git_worktree_merge ───────────────────────────────────


class GitWorktreeMergeParams(BaseModel):
    """Parameters for git_worktree_merge tool."""

    model_config = ConfigDict(populate_by_name=True)

    branch_name: str = Field(
        alias="branchName",
        description="Branch/task name of the worktree to merge.",
        json_schema_extra={
            "aliases": ["branchName", "branch_name", "branch", "name", "taskName", "task_name", "task"]
        },
    )
    target_branch: str | None = Field(
        default=None,
        alias="targetBranch",
        description="Target branch to merge into (default: main).",
        json_schema_extra={"aliases": ["targetBranch", "target_branch", "target"]},
    )


@tool(
    "git_worktree_merge",
    "Merge a worktree branch back into main and remove the worktree.",
    requires_workspace=True,
    security_level="standard",
)
async def git_worktree_merge_tool(
    params: GitWorktreeMergeParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Merge a git worktree branch."""
    from hiveweave.services.git_worktree import GitWorktreeService

    wt_ctx = await _get_worktree_context(agent_id, ctx)
    if isinstance(wt_ctx, ToolResult):
        return wt_ctx
    workspace_path, short_id, _ = wt_ctx

    gwt = GitWorktreeService()
    await gwt.ensure_git_repo(workspace_path)

    task_name = params.branch_name or "task"
    target_branch = params.target_branch or "main"

    result = await gwt.merge(
        workspace_path, short_id, str(task_name), target_branch
    )
    if result.get("success"):
        return ToolResult.ok("Worktree merged and cleaned up")
    return ToolResult.err(result.get("message", "Failed to merge worktree"))


# ── git_worktree_remove ──────────────────────────────────


class GitWorktreeRemoveParams(BaseModel):
    """Parameters for git_worktree_remove tool."""

    model_config = ConfigDict(populate_by_name=True)

    branch_name: str = Field(
        alias="branchName",
        description="Branch/task name of the worktree to remove.",
        json_schema_extra={
            "aliases": ["branchName", "branch_name", "branch", "name", "taskName", "task_name", "task"]
        },
    )


@tool(
    "git_worktree_remove",
    "Remove a worktree and its branch without merging. Discards changes.",
    requires_workspace=True,
    security_level="standard",
)
async def git_worktree_remove_tool(
    params: GitWorktreeRemoveParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Remove a git worktree."""
    from hiveweave.services.git_worktree import GitWorktreeService

    wt_ctx = await _get_worktree_context(agent_id, ctx)
    if isinstance(wt_ctx, ToolResult):
        return wt_ctx
    workspace_path, short_id, _ = wt_ctx

    gwt = GitWorktreeService()
    await gwt.ensure_git_repo(workspace_path)

    task_name = params.branch_name or "task"

    result = await gwt.delete(workspace_path, short_id, task_name)
    if result.get("success"):
        return ToolResult.ok("Worktree removed")
    return ToolResult.err(result.get("message", "Failed to remove worktree"))


# ── git_worktree_status ──────────────────────────────────


class GitWorktreeStatusParams(BaseModel):
    """Parameters for git_worktree_status tool."""

    model_config = ConfigDict(populate_by_name=True)


@tool(
    "git_worktree_status",
    "Show uncommitted changes and branch status for worktrees.",
    requires_workspace=True,
    security_level="standard",
)
async def git_worktree_status_tool(
    params: GitWorktreeStatusParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Show git worktree status."""
    from hiveweave.services.git_worktree import GitWorktreeService

    wt_ctx = await _get_worktree_context(agent_id, ctx)
    if isinstance(wt_ctx, ToolResult):
        return wt_ctx
    workspace_path, short_id, _ = wt_ctx

    gwt = GitWorktreeService()
    await gwt.ensure_git_repo(workspace_path)

    result = await gwt.info(workspace_path, short_id)
    if result.get("success"):
        info = result.get("info", {})
        return ToolResult.ok(
            f"Branch: {info.get('branch', '?')}, "
            f"Status: {info.get('status', '?')}"
        )
    return ToolResult.err(result.get("message", "Failed to get worktree status"))


# ── git_worktree_checkpoint ──────────────────────────────


class GitWorktreeCheckpointParams(BaseModel):
    """Parameters for git_worktree_checkpoint tool."""

    model_config = ConfigDict(populate_by_name=True)

    message: str = Field(
        description="Checkpoint commit message.",
        json_schema_extra={
            "aliases": ["message", "commitMessage", "commit_message", "summary"]
        },
    )
    branch_name: str | None = Field(
        default=None,
        alias="branchName",
        description="Optional branch/task name (for API compatibility).",
        json_schema_extra={
            "aliases": ["branchName", "branch_name", "branch", "taskName", "task_name"]
        },
    )


@tool(
    "git_worktree_checkpoint",
    "Stage all changes and create a checkpoint commit in the active "
    "worktree.",
    requires_workspace=True,
    security_level="standard",
)
async def git_worktree_checkpoint_tool(
    params: GitWorktreeCheckpointParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Create a checkpoint commit in the worktree."""
    from hiveweave.services.git_worktree import GitWorktreeService

    wt_ctx = await _get_worktree_context(agent_id, ctx)
    if isinstance(wt_ctx, ToolResult):
        return wt_ctx
    workspace_path, short_id, _ = wt_ctx

    gwt = GitWorktreeService()
    await gwt.ensure_git_repo(workspace_path)

    message = params.message or "checkpoint"
    result = await gwt.checkpoint(workspace_path, short_id, str(message))
    if result.get("success"):
        return ToolResult.ok(
            f"Checkpoint saved: {result.get('commit', result.get('hash', 'unknown'))}"
        )
    return ToolResult.err(result.get("message", "Failed to checkpoint"))


# ═══════════════════════════════════════════════════════════════════════
# Section 2: Legacy task tools (deprecated — use Task Ledger tools)
# ═══════════════════════════════════════════════════════════════════════


# ── report_completion ────────────────────────────────────


class ReportCompletionParams(BaseModel):
    """Parameters for report_completion tool."""

    model_config = ConfigDict(populate_by_name=True)

    task_id: str = Field(
        alias="taskId",
        description="Task/handoff ID to report completion for.",
        json_schema_extra={
            "aliases": ["taskId", "task_id", "handoffId", "handoff_id", "id"]
        },
    )
    summary: str | None = Field(
        default=None,
        description="Completion summary/report.",
        json_schema_extra={
            "aliases": ["summary", "message", "content", "report", "description"]
        },
    )


@tool(
    "report_completion",
    "DEPRECATED: Use submit_task instead. Notify your superior that a "
    "delegated task is finished.",
    requires_workspace=False,
    security_level="standard",
)
async def report_completion_tool(
    params: ReportCompletionParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Report task completion to superior (legacy HandoffService flow)."""
    from hiveweave.services.handoff import HandoffService

    if not params.summary:
        return ToolResult.err("report_completion requires 'summary'")

    project_id = await get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")

    hs = HandoffService()
    handoffs = await hs.get_accepted_handoffs(project_id, agent_id)
    if not handoffs:
        return ToolResult.err("No accepted handoffs to report on")
    await hs.complete_handoff(project_id, handoffs[0]["id"])
    return ToolResult.ok("Completion reported to superior.")


# ── request_review ───────────────────────────────────────


class RequestReviewParams(BaseModel):
    """Parameters for request_review tool."""

    model_config = ConfigDict(populate_by_name=True)

    task_id: str = Field(
        alias="taskId",
        description="Task/handoff ID to request review for.",
        json_schema_extra={
            "aliases": ["taskId", "task_id", "handoffId", "handoff_id", "id"]
        },
    )
    summary: str | None = Field(
        default=None,
        description="Review request summary/description.",
        json_schema_extra={
            "aliases": ["summary", "message", "content", "description", "report"]
        },
    )


@tool(
    "request_review",
    "DEPRECATED: Use review_task instead. Ask a superior to review code, "
    "design, or deliverables.",
    requires_workspace=False,
    security_level="standard",
)
async def request_review_tool(
    params: RequestReviewParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Request a code review from superior (legacy flow)."""
    from hiveweave.services.handoff import HandoffService
    from hiveweave.services.inbox import InboxService

    description = params.summary or "Please review my work."

    project_id = await get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")

    hs = HandoffService()
    handoffs = await hs.get_accepted_handoffs(project_id, agent_id)
    if not handoffs:
        return ToolResult.err("No accepted handoffs to request review on")

    ib = InboxService()

    # Resolve superior
    superior = None
    if ctx and getattr(ctx, "org", None):
        superior = await ctx.org.get_superior(agent_id)
    else:
        from hiveweave.services.org import OrgService

        org = OrgService()
        superior = await org.get_superior(agent_id)

    if not superior:
        return ToolResult.err("No superior found")

    await ib.send_message(
        from_agent_id=agent_id,
        to_agent_id=superior["id"],
        message=f"[REVIEW REQUEST] {description}",
        message_type="review_request",
        priority="urgent",
    )
    return ToolResult.ok("Review requested from superior.")


# ── approve_work ─────────────────────────────────────────


class ApproveWorkParams(BaseModel):
    """Parameters for approve_work tool."""

    model_config = ConfigDict(populate_by_name=True)

    task_id: str = Field(
        alias="taskId",
        description="Subordinate agent ID or task ID to approve work for.",
        json_schema_extra={
            "aliases": [
                "taskId", "task_id", "subordinate", "subordinateId",
                "subordinate_id", "agentId", "agent_id", "target", "id",
            ]
        },
    )
    feedback: str | None = Field(
        default=None,
        description="Optional review feedback/comments.",
        json_schema_extra={
            "aliases": ["feedback", "review", "comment", "notes"]
        },
    )


@tool(
    "approve_work",
    "DEPRECATED: Use review_task with decision='approve' instead. "
    "Approve a subordinate's deliverable.",
    requires_workspace=False,
    security_level="standard",
)
async def approve_work_tool(
    params: ApproveWorkParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Approve a subordinate's work (legacy HandoffService flow)."""
    from hiveweave.services.handoff import HandoffService

    subordinate = params.task_id
    if not subordinate:
        return ToolResult.err("approve_work requires 'taskId' (subordinate)")

    project_id = await get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")

    hs = HandoffService()
    result = await hs.approve(project_id, agent_id, subordinate)
    if result.get("success"):
        return ToolResult.ok(f"Work approved for {subordinate}.")
    return ToolResult.err(result.get("message", "Approval failed"))


# ── reject_work ──────────────────────────────────────────


class RejectWorkParams(BaseModel):
    """Parameters for reject_work tool."""

    model_config = ConfigDict(populate_by_name=True)

    task_id: str = Field(
        alias="taskId",
        description="Subordinate agent ID or task ID to reject work for.",
        json_schema_extra={
            "aliases": [
                "taskId", "task_id", "subordinate", "subordinateId",
                "subordinate_id", "agentId", "agent_id", "target", "id",
            ]
        },
    )
    reason: str | None = Field(
        default=None,
        description="Required reason for rejection (rework instructions).",
        json_schema_extra={
            "aliases": ["reason", "feedback", "review", "comment", "message"]
        },
    )


@tool(
    "reject_work",
    "DEPRECATED: Use review_task with decision='rework' instead. "
    "Reject a subordinate's work with a required reason.",
    requires_workspace=False,
    security_level="standard",
)
async def reject_work_tool(
    params: RejectWorkParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Reject a subordinate's work (legacy HandoffService flow)."""
    from hiveweave.services.handoff import HandoffService

    subordinate = params.task_id
    if not subordinate:
        return ToolResult.err("reject_work requires 'taskId' (subordinate)")

    reason = params.reason or "Rework required."

    project_id = await get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")

    hs = HandoffService()
    result = await hs.reject(project_id, agent_id, subordinate, reason)
    if result.get("success"):
        return ToolResult.ok(
            f"Work rejected for {subordinate}: {reason}"
        )
    return ToolResult.err(result.get("message", "Rejection failed"))


# ═══════════════════════════════════════════════════════════════════════
# Section 3: Other tools (message_user, webfetch)
# ═══════════════════════════════════════════════════════════════════════


# ── message_user ─────────────────────────────────────────


class MessageUserParams(BaseModel):
    """Parameters for message_user tool."""

    model_config = ConfigDict(populate_by_name=True)

    message: str = Field(
        description="Message body to send to the user.",
        json_schema_extra={"aliases": ["content", "body", "text"]},
    )
    priority: str | None = Field(
        default=None,
        description="Message priority: 'normal' or 'urgent'.",
        json_schema_extra={"aliases": ["level"]},
    )


@tool(
    "message_user",
    "Send a message directly to the human user. The message appears in "
    "the user's chat window.",
    requires_workspace=False,
    security_level="standard",
)
async def message_user_tool(
    params: MessageUserParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Send a message to the human user."""
    if not params.message:
        return ToolResult.err("message_user requires 'message' (body text)")

    from hiveweave.services.chat_message import ChatMessageService

    chat_service = ChatMessageService()
    await chat_service.save_message({
        "agent_id": agent_id,
        "role": "assistant",
        "content": params.message,
        "thinking": None,
        "tool_calls": "[]",
        "is_streaming": False,
        "is_background": False,
    })

    # Push via WebSocket so the frontend updates in real-time
    try:
        from hiveweave.realtime.event_bus import status_event_bus

        await status_event_bus.publish_chat_message(
            agent_id=agent_id,
            message={"role": "assistant", "content": params.message},
        )
    except Exception as evt_err:
        log.debug("message_user_event_push_failed", error=str(evt_err))

    return ToolResult.ok("Message sent to user.")


# ── webfetch ─────────────────────────────────────────────


# SSRF protection: blocked hosts and IP ranges
_SSRF_BLOCKED_HOSTS = frozenset({
    "localhost", "127.0.0.1", "0.0.0.0", "::1",
    "169.254.169.254",  # cloud metadata
    "metadata.google.internal",
})


def _is_ssrf_blocked(host: str) -> bool:
    """Check if a host is an internal/blocked address."""
    host_lower = host.lower().rstrip(".")
    if host_lower in _SSRF_BLOCKED_HOSTS:
        return True
    # Block private IP ranges
    try:
        ip = ipaddress.ip_address(host_lower)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
    except ValueError:
        pass  # Not an IP, it's a hostname
    # Block common internal hostnames
    if host_lower.endswith(".internal") or host_lower.endswith(".local"):
        return True
    return False


class WebfetchParams(BaseModel):
    """Parameters for webfetch tool."""

    model_config = ConfigDict(populate_by_name=True)

    url: str = Field(
        description="URL to fetch (http or https only).",
        json_schema_extra={"aliases": ["url", "link", "href", "address"]},
    )
    query: str | None = Field(
        default=None,
        description="Optional question or instruction about the page content.",
        json_schema_extra={
            "aliases": ["query", "prompt", "question", "instruction"]
        },
    )


@tool(
    "webfetch",
    "Fetch a URL, extract readable text, and optionally answer a question "
    "about the page. Has SSRF protection.",
    requires_workspace=False,
    security_level="standard",
)
async def webfetch_tool(
    params: WebfetchParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Fetch a URL and convert to text, optionally answering a prompt.

    Security:
    - Scheme validation: only http/https allowed
    - SSRF protection: reject private IPs, localhost, link-local addresses
    - Content-length pre-check: reject >5MB responses
    - Redirect validation: each redirect target checked for SSRF
    """
    import httpx

    url = params.url
    prompt = params.query or ""

    if not url:
        return ToolResult.err("webfetch requires 'url'")

    # 1. URL scheme validation
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return ToolResult.err(
            f"Invalid URL scheme: {parsed.scheme}. Only http/https allowed."
        )
    if not parsed.hostname:
        return ToolResult.err("Invalid URL: no hostname")

    # 2. SSRF protection
    hostname = parsed.hostname
    if hostname and _is_ssrf_blocked(hostname):
        return ToolResult.err(
            f"Access denied: cannot fetch internal address {hostname}"
        )

    try:
        # 3. HEAD request to check content-length (if server supports it)
        async with httpx.AsyncClient(
            timeout=30, follow_redirects=False
        ) as client:
            try:
                head_resp = await client.head(
                    url, headers={"User-Agent": "HiveWeave/1.0"}
                )
                cl = head_resp.headers.get("content-length")
                if cl and int(cl) > 5_000_000:
                    return ToolResult.err(
                        f"Response too large: {int(cl)} bytes (max 5MB)"
                    )
            except Exception:
                pass  # Some servers don't support HEAD, continue with GET

            # 4. GET request -- don't auto-follow redirects, validate each one
            resp = await client.get(
                url, headers={"User-Agent": "HiveWeave/1.0"}
            )
            redirects = 0
            while resp.is_redirect and redirects < 5:
                loc = resp.headers.get("location", "")
                if not loc:
                    break
                redirect_url = str(httpx.URL(url).join(loc))
                redirect_parsed = urlparse(redirect_url)
                if redirect_parsed.scheme not in ("http", "https"):
                    return ToolResult.err(
                        f"Redirect to non-http scheme blocked: "
                        f"{redirect_parsed.scheme}"
                    )
                redirect_hostname = redirect_parsed.hostname
                if redirect_hostname and _is_ssrf_blocked(redirect_hostname):
                    return ToolResult.err(
                        f"Redirect to internal address blocked: "
                        f"{redirect_hostname}"
                    )
                url = redirect_url
                resp = await client.get(
                    url, headers={"User-Agent": "HiveWeave/1.0"}
                )
                redirects += 1

            # 5. Size check on actual response
            content_length = len(resp.content)
            if content_length > 5_000_000:
                return ToolResult.err(
                    f"Response too large: {content_length} bytes (max 5MB)"
                )
            html = resp.text[:500_000]  # Cap at 500KB for processing
    except Exception as e:
        return ToolResult.err(f"Failed to fetch {url}: {e}")

    # Strip HTML tags for plain text
    text = re.sub(
        r"<script[^>]*>.*?</script>", "", html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(
        r"<style[^>]*>.*?</style>", "", text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text[:20_000]

    if prompt:
        return ToolResult.ok(
            f"Fetched {url} ({len(text)} chars). "
            f"Prompt: {prompt}\n\n{text}"
        )
    return ToolResult.ok(text)
