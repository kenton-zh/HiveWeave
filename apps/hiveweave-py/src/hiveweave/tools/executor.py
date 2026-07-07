"""ToolExecutor — permission gating + tool dispatch + output truncation.

契约 02: 工具执行器 — 主分发器
- 接收 tool_name + tool_args，执行对应工具
- 执行前检查权限（PermissionService.evaluate → allow/deny/ask）
- ask → ApprovalService.request_permission（120s 超时）
- 工具输出截断（> 2000 行或 50KB 存临时文件，返回 head+tail 预览）
- 错误处理：工具异常不崩溃，返回 "Error: ..." 字符串
- 临时文件保留 7 天（.hiveweave/tool_outputs/<agent>_<ts>_<tool>.txt）
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Awaitable

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.services.approval import (
    ApprovalService, PermissionRejected, PermissionTimeout,
)
from hiveweave.services.charter import CharterService
from hiveweave.services.inbox import InboxService
from hiveweave.services.model import ModelService
from hiveweave.services.org import OrgService
from hiveweave.services.permission import PermissionService
from hiveweave.services.roster import RosterService
from hiveweave.services.skill_registry import SkillRegistryService
from hiveweave.services.template import TemplateService
from hiveweave.tools.bash import execute_bash, execute_run_command
from hiveweave.tools.file import read_file, write_file, list_files
from hiveweave.tools.grep import execute_grep
from hiveweave.tools.patch import apply_patch
from hiveweave.tools.question import execute_question
from hiveweave.tools.review import execute_review, ReviewLLMCallback
from hiveweave.tools.todowrite import execute_todowrite
from hiveweave.tools.websearch import execute_websearch

log = structlog.get_logger(__name__)

# ── Constants (契约 02) ────────────────────────────────────

TOOL_OUTPUT_MAX_LINES = 2000
TOOL_OUTPUT_MAX_BYTES = 50_000
TOOL_OUTPUT_RETENTION_DAYS = 7
TOOL_OUTPUT_FILE_MAX_BYTES = 10 * 1024 * 1024  # 10MB
PREVIEW_HEAD_LINES = 20
PREVIEW_TAIL_LINES = 5
PREVIEW_TAIL_THRESHOLD = 25  # only include tail if total > 25 lines

APPROVAL_TIMEOUT_S = 120

# Tool name regex for filename sanitization (non-alphanumeric → "_")
_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")


# ── Result type ────────────────────────────────────────────

class ToolResult(dict):
    """Dict with success/output/error keys (returned by all tools)."""


# ── ToolExecutor ───────────────────────────────────────────

class ToolExecutor:
    """Routes tool calls to implementations with permission gating +
    sandbox checks + output truncation.

    Usage:
        executor = ToolExecutor(permission_service, approval_service)
        result = await executor.execute(agent_id, "bash",
                                        {"command": "ls"}, workspace_path)
        # result: {"success": bool, "output": str, "error": str | None}
    """

    def __init__(
        self,
        permission_service: PermissionService,
        approval_service: ApprovalService,
        review_llm_callback: ReviewLLMCallback | None = None,
    ) -> None:
        self.permission = permission_service
        self.approval = approval_service
        self.review_llm_callback = review_llm_callback
        # Service instances for high-level orchestration tools
        self._org = OrgService()
        self._inbox = InboxService()
        self._charter = CharterService()
        self._roster = RosterService()
        self._skills = SkillRegistryService()
        self._templates = TemplateService()

    # ── Public API ────────────────────────────────────────

    async def execute(
        self,
        agent_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
        workspace_path: str,
    ) -> dict[str, Any]:
        """Execute a tool call. Returns {success, output, error}."""
        # 1. Strip hiveweave__ prefix
        name = tool_name
        if name.startswith("hiveweave__"):
            name = name[len("hiveweave__"):]

        log.info("tool.execute", agent_id=agent_id, tool=name,
                 args_preview=str(tool_args)[:200])

        # 2. Permission evaluation
        try:
            decision = await self.permission.evaluate(
                agent_id, name, tool_args
            )
        except Exception as exc:  # noqa: BLE001
            log.error("permission.evaluate_failed", error=str(exc))
            return self._error(f"Error: Permission check failed: {exc}")

        if decision == "deny":
            return self._error(
                f"Permission denied: {name} is blocked for this agent."
            )

        if decision == "ask":
            # Request approval (120s timeout)
            try:
                await self.approval.request_permission(
                    agent_id=agent_id,
                    tool_name=name,
                    tool_args=tool_args,
                    description=f"Agent {agent_id} wants to use {name}",
                )
            except PermissionTimeout:
                return self._error(
                    "Permission request timed out (120s). "
                    "The user may be away."
                )
            except PermissionRejected as exc:
                return self._error(f"Permission rejected: {exc}")
            except Exception as exc:  # noqa: BLE001
                return self._error(
                    f"Error: Approval request failed: {exc}"
                )

        # 3. Dispatch to the tool implementation
        try:
            result = await self._dispatch(
                name, tool_args, agent_id, workspace_path
            )
        except Exception as exc:  # noqa: BLE001
            log.error("tool.dispatch_failed", tool=name, error=str(exc))
            return self._error(f"Error: {exc}")

        # 4. Normalize result shape — R7: 统一工具返回契约
        # 所有工具必须返回 {success, output, error} 三字段。此处作为单一保障点，
        # 为任何遗漏字段的工具补默认值（success=True / output="" / error=None），
        # 确保下游消费方（agent / conversation store）总能拿到一致结构。
        if not isinstance(result, dict):
            result = {"success": True, "output": str(result), "error": None}
        result.setdefault("success", True)
        result.setdefault("output", "")
        result.setdefault("error", None)

        # 5. Apply large-output truncation (layer 1)
        if result.get("output"):
            truncated = self._maybe_save_large_output(
                result["output"], agent_id, name, workspace_path
            )
            result["output"] = truncated

        return result

    # ── Dispatch ─────────────────────────────────────────

    async def _dispatch(
        self,
        name: str,
        args: dict[str, Any],
        agent_id: str,
        workspace_path: str,
    ) -> dict[str, Any]:
        """Route to the specific tool implementation by name."""
        if name == "bash":
            command = args.get("command") or ""
            workdir = args.get("workdir") or ""
            timeout = args.get("timeout")
            return await execute_bash(
                command=command,
                workdir=workdir,
                workspace_path=workspace_path,
                timeout_ms=int(timeout) if timeout else None,
            )

        if name == "run_command":
            command = args.get("command") or ""
            cwd = args.get("cwd") or ""
            timeout = args.get("timeout") or 120_000
            return await execute_run_command(
                command=command, cwd=cwd,
                timeout_ms=int(timeout),
                workspace_path=workspace_path,
            )

        if name == "read_file":
            # BUG-008 修复：兼容 LLM 试错的多种字段名（filePath / file_path / path）
            file_path = (args.get("filePath") or args.get("file_path") or args.get("path") or "").strip()
            offset = int(args.get("offset") or 0)
            limit = int(args.get("limit") or 2000)
            return await read_file(
                file_path=file_path, offset=offset, limit=limit,
                workspace_path=workspace_path,
            )

        if name == "write_file":
            # BUG-008 修复：兼容 LLM 试错的多种字段名
            file_path = (args.get("filePath") or args.get("file_path") or args.get("path") or "").strip()
            content = args.get("content") or ""
            return await write_file(
                file_path=file_path, content=content,
                workspace_path=workspace_path,
            )

        if name == "edit_file":
            # BUG-008 修复：兼容多种字段名。apply_patch 内部 _normalize_patches
            # 已经处理 single-patch 形式 + 多 key 别名，我们只负责把 LLM 输入
            # 透传过去（让 _normalize 兜底）。
            return await apply_patch(
                patches=None,
                workspace_path=workspace_path,
                raw_input=args,
            )

        if name == "list_files":
            # BUG-008 修复：兼容 dirPath / directory / filePath
            path = (args.get("dirPath") or args.get("directory") or args.get("path") or args.get("filePath") or "").strip()
            # BUG-019 修复：支持 recursive + maxdepth
            recursive = bool(args.get("recursive", False))
            maxdepth = int(args.get("maxdepth") or 1)
            return await list_files(
                path=path, workspace_path=workspace_path,
                recursive=recursive, maxdepth=maxdepth,
            )

        if name == "grep":
            pattern = args.get("pattern") or ""
            path = args.get("path") or ""
            include = args.get("include")
            head_limit = args.get("head_limit") or args.get("limit")
            context = int(args.get("context") or 0)
            multiline = bool(args.get("multiline") or False)
            return await execute_grep(
                pattern=pattern, path=path, include=include,
                workspace_path=workspace_path,
                head_limit=int(head_limit) if head_limit else None,
                context=context, multiline=multiline,
            )

        if name == "apply_patch":
            return await apply_patch(
                patches=args.get("patches"),
                workspace_path=workspace_path,
                raw_input=args,
            )

        if name == "todowrite":
            todos = args.get("todos") or []
            return await execute_todowrite(
                agent_id=agent_id, todos=todos,
            )

        if name == "question":
            question = args.get("question") or ""
            options = args.get("options")
            return await execute_question(
                agent_id=agent_id, question=question, options=options,
            )

        if name == "websearch":
            query = args.get("query") or ""
            num_results = int(args.get("numResults") or 5)
            return await execute_websearch(
                query=query, num_results=num_results,
            )

        if name in (
            "run_code_review", "run_security_audit", "run_tests",
            "run_perf_audit", "run_full_review",
        ):
            review_type_map = {
                "run_code_review": "code_review",
                "run_security_audit": "security_audit",
                "run_tests": "test_review",
                "run_perf_audit": "perf_audit",
                "run_full_review": "full_review",
            }
            review_type = review_type_map[name]
            file_paths = args.get("filePaths") or []
            test_files = args.get("testFiles") or []
            return await execute_review(
                review_type=review_type,
                file_paths=file_paths,
                test_files=test_files,
                workspace_path=workspace_path,
                call_llm=self.review_llm_callback,
            )

        # ── High-level orchestration tools ──────────────────
        # These bridge the LLM tool calls to service-layer methods.

        if name in ("send_message", "message_subordinate", "message_superior",
                     "message_peer", "message_team", "message_user"):
            return await self._tool_send_message(agent_id, args)

        if name == "list_subordinates":
            return await self._tool_list_subordinates(agent_id)

        if name == "hire_agent":
            return await self._tool_hire_agent(agent_id, args)

        if name == "read_charter":
            return await self._tool_read_charter(agent_id)

        if name == "save_charter":
            return await self._tool_save_charter(agent_id, args)

        if name == "read_goals":
            return await self._tool_read_goals(agent_id)

        if name == "update_goals":
            return await self._tool_update_goals(agent_id, args)

        if name == "view_org_chart":
            return await self._tool_view_org_chart(agent_id)

        if name == "read_work_logs":
            return await self._tool_read_work_logs(agent_id, args)

        if name == "write_work_log":
            return await self._tool_write_work_log(agent_id, args)

        # ── Roster tools ────────────────────────────────────
        if name == "read_roster":
            project_id = await self._get_project_id(agent_id)
            if not project_id:
                return self._error(f"Agent {agent_id} has no project_id")
            roster_text = await self._roster.get_roster(project_id)
            return {"success": True, "output": roster_text, "error": None}

        if name == "update_roster":
            project_id = await self._get_project_id(agent_id)
            if not project_id:
                return self._error(f"Agent {agent_id} has no project_id")
            target = args.get("agentId") or args.get("agent_id") or ""
            if not target:
                return self._error("update_roster requires 'agentId'")
            target_agent = await self._org.resolve_agent(target)
            if not target_agent:
                return self._error(f"Agent not found: {target}")
            roster_attrs = {k: v for k, v in args.items()
                            if k in ("position", "department", "responsibilities",
                                     "status", "hire_date")}
            result = await self._roster.update_roster(
                project_id, target_agent["id"], roster_attrs)
            return {"success": True, "output": result, "error": None}

        # ── Template tools ──────────────────────────────────
        if name == "list_agent_templates":
            # 运行时角色校验 — 仅 HR 可浏览模板（参照 Elixir tool_executor.ex）
            caller = await self._org.get_agent(agent_id)
            if not caller or caller.get("role", "").lower() != "hr":
                return self._error(
                    "Permission denied: only HR can browse agent templates")
            opts: dict[str, Any] = {}
            if args.get("search"):
                opts["search"] = args["search"]
            if args.get("division"):
                opts["division"] = args["division"]
            templates = await self._templates.list_all(opts)
            if not templates:
                return {"success": True, "output": "No templates found. Try a different search keyword or division.", "error": None}
            lines = []
            for t in templates:
                lines.append(
                    f"- {t['name']} (role: {t.get('role', '?')}) — "
                    f"ID: {t['id']} — {t.get('description', 'no description')}")
            output = (f"Available agent templates ({len(templates)} found):\n"
                      + "\n".join(lines)
                      + "\n\nPass templateId in hire_agent to pre-fill "
                        "role/goal/skills.")
            return {"success": True, "output": output, "error": None}

        # ── Skill tools ─────────────────────────────────────
        if name == "list_available_skills":
            search = args.get("search")
            result = await self._skills.list_available_skills(search)
            return {"success": True, "output": result, "error": None}

        if name == "read_skill":
            slug = (args.get("slug") or args.get("skillName")
                    or args.get("skill") or "")
            if not slug:
                return self._error("read_skill requires 'slug' (skill name)")
            bound = await self._skills.get_bound_skills(agent_id)
            result = await self._skills.read_skill(slug, bound)
            return {"success": True, "output": result, "error": None}

        if name == "bind_skill":
            skill_name = (args.get("skillName") or args.get("skill")
                          or args.get("slug") or "")
            if not skill_name:
                return self._error("bind_skill requires 'skillName' (skill slug)")
            target_id = args.get("agentId") or args.get("agent_id") or agent_id
            if target_id != agent_id:
                target_agent = await self._org.resolve_agent(target_id)
                if not target_agent:
                    return self._error(f"Agent not found: {target_id}")
                target_id = target_agent["id"]
            result = await self._skills.bind_skill(target_id, skill_name)
            if result.get("ok"):
                return {"success": True, "output": f"Skill '{skill_name}' bound to agent {target_id[:8]}...", "error": None}
            return self._error(result.get("error", "Unknown error"))

        if name == "unbind_skill":
            skill_name = (args.get("skillName") or args.get("skill")
                          or args.get("slug") or "")
            if not skill_name:
                return self._error("unbind_skill requires 'skillName' (skill slug)")
            target_id = args.get("agentId") or args.get("agent_id") or agent_id
            if target_id != agent_id:
                target_agent = await self._org.resolve_agent(target_id)
                if not target_agent:
                    return self._error(f"Agent not found: {target_id}")
                target_id = target_agent["id"]
            result = await self._skills.unbind_skill(target_id, skill_name)
            if result.get("ok"):
                return {"success": True, "output": f"Skill '{skill_name}' unbound from agent {target_id[:8]}...", "error": None}
            return self._error(result.get("error", "Unknown error"))

        # ── Agent lifecycle tools ───────────────────────────
        if name == "dismiss_agent":
            project_id = await self._get_project_id(agent_id)
            if not project_id:
                return self._error(f"Agent {agent_id} has no project_id")
            target = args.get("agentId") or args.get("agent_id") or ""
            if not target:
                return self._error("dismiss_agent requires 'agentId'")
            target_agent = await self._org.resolve_agent(target)
            if not target_agent:
                return self._error(f"Agent not found: {target}")
            result = await self._org.dismiss_agent(
                project_id, target_agent["id"])
            if result.get("success"):
                return {"success": True, "output": f"Agent {target_agent['name']} ({target_agent.get('short_id', '?')}) has been dismissed.", "error": None}
            return self._error(result.get("message", "Unknown error"))

        if name == "transfer_agent":
            project_id = await self._get_project_id(agent_id)
            if not project_id:
                return self._error(f"Agent {agent_id} has no project_id")
            target = args.get("agentId") or args.get("agent_id") or ""
            new_parent = (args.get("newParentId")
                          or args.get("new_parent_id")
                          or args.get("parentId") or "")
            if not target:
                return self._error("transfer_agent requires 'agentId'")
            target_agent = await self._org.resolve_agent(target)
            if not target_agent:
                return self._error(f"Agent not found: {target}")
            resolved_parent = None
            if new_parent:
                parent_agent = await self._org.resolve_agent(new_parent)
                if not parent_agent:
                    return self._error(f"New parent agent not found: {new_parent}")
                resolved_parent = parent_agent["id"]
            result = await self._org.transfer_agent(
                project_id, target_agent["id"], resolved_parent)
            if result is None:
                return self._error("Agent not found")
            if isinstance(result, dict) and result.get("success") is False:
                return self._error(result.get("message", "Unknown error"))
            return {"success": True, "output": f"Agent {target_agent['name']} transferred to new parent.", "error": None}

        # Unknown tool — contract 02 error handling
        return self._error(f"Unknown tool: {name}")

    # ── Output truncation (layer 1) ──────────────────────

    def _maybe_save_large_output(
        self,
        output: str,
        agent_id: str,
        tool_name: str,
        workspace_path: str,
    ) -> str:
        """If output exceeds thresholds, save full to file and return preview.

        契约 02:
          - threshold: > 2000 lines OR > 50KB
          - file: .hiveweave/tool_outputs/<agent_id>_<ts>_<safe_tool>.txt
          - cap: 10MB per file
          - preview: head 20 lines + marker + tail 5 lines (tail only if > 25)
        """
        if not output:
            return output

        lines = output.split("\n")
        byte_len = len(output.encode("utf-8", errors="replace"))

        if len(lines) <= TOOL_OUTPUT_MAX_LINES \
                and byte_len <= TOOL_OUTPUT_MAX_BYTES:
            return output

        file_path = self._save_tool_output_file(
            output, agent_id, tool_name, workspace_path
        )

        head = lines[:PREVIEW_HEAD_LINES]
        tail = lines[-PREVIEW_TAIL_LINES:] if len(lines) > PREVIEW_TAIL_THRESHOLD \
            else []

        marker = (
            f"\n\n... [output truncated: {len(lines)} lines, "
            f"{byte_len} bytes. Full output saved to {file_path}] ...\n\n"
        )

        parts = head + [marker] + tail
        return "\n".join(parts)

    @staticmethod
    def _save_tool_output_file(
        output: str,
        agent_id: str,
        tool_name: str,
        workspace_path: str,
    ) -> str:
        """Save the full output to a temp file; return the file path.

        R6: 文件名内嵌创建时间戳（{agent_id}_{ts}_{tool}.txt），写入时 mtime
        也同步记录创建时间。cleanup_tool_outputs 据此判断保留期。
        """
        base_dir = workspace_path or os.getcwd()
        out_dir = Path(base_dir) / ".hiveweave" / "tool_outputs"
        out_dir.mkdir(parents=True, exist_ok=True)

        timestamp = int(time.time() * 1000)
        safe_name = _SAFE_NAME_RE.sub("_", tool_name)
        filename = f"{agent_id}_{timestamp}_{safe_name}.txt"
        full_path = out_dir / filename

        encoded = output.encode("utf-8", errors="replace")
        if len(encoded) > TOOL_OUTPUT_FILE_MAX_BYTES:
            capped = encoded[:TOOL_OUTPUT_FILE_MAX_BYTES]
            capped += (
                f"\n\n... [file capped at "
                f"{TOOL_OUTPUT_FILE_MAX_BYTES} bytes]"
            ).encode("utf-8")
        else:
            capped = encoded

        try:
            full_path.write_bytes(capped)
        except OSError as exc:
            log.warning("tool_output.save_failed", error=str(exc))
            return f"<save failed: {exc}>"

        return str(full_path)

    @staticmethod
    def cleanup_tool_outputs(workspace_path: str | None = None) -> None:
        """Delete tool output files older than the retention period (7 days).

        R6: 清理机制 —— 在 main.py 的 lifespan 启动阶段对每个项目工作区调用
        本方法（见 main.py "tool_outputs_cleaned"）。用文件 mtime 判断创建时间，
        删除超过 TOOL_OUTPUT_RETENTION_DAYS（7 天）的临时文件。文件名中的时间戳
        仅用于可读性，实际保留期判断以 mtime 为准（对齐 Elixir/TS 7 天保留策略）。
        """
        base_dir = workspace_path or os.getcwd()
        out_dir = Path(base_dir) / ".hiveweave" / "tool_outputs"
        if not out_dir.exists():
            return

        now = time.time()
        retention_s = TOOL_OUTPUT_RETENTION_DAYS * 86400

        for entry in out_dir.iterdir():
            try:
                mtime = entry.stat().st_mtime
                if now - mtime > retention_s:
                    entry.unlink()
            except OSError:
                continue

    # ── Helpers ──────────────────────────────────────────

    @staticmethod
    def _error(message: str) -> dict[str, Any]:
        """Build an error result dict."""
        return {"success": False, "output": "", "error": message}

    # ── High-level orchestration tool implementations ────

    async def _get_project_id(self, agent_id: str) -> str | None:
        """Resolve agent_id → project_id via Meta DB."""
        return await meta_db.get_agent_project_id(agent_id)

    async def _tool_send_message(self, agent_id: str, args: dict) -> dict:
        """send_message: CEO/HR → subordinates/peers via InboxService.

        Args (from LLM):
            recipients: list[str] — short_id or name of target agents
            message: str — message body (also accepts 'content')
            expectReport: bool — whether a response is expected
            priority: str — "normal" / "urgent"
        """
        recipients = args.get("recipients") or args.get("recipient") or []
        # Handle JSON string recipients (LLM sometimes sends '["HR"]' as string)
        if isinstance(recipients, str):
            try:
                parsed = json.loads(recipients)
                if isinstance(parsed, list):
                    recipients = parsed
                else:
                    recipients = [recipients]
            except (json.JSONDecodeError, ValueError):
                recipients = [recipients]
        if isinstance(recipients, (list, tuple)) and len(recipients) == 0:
            recipients = []
        message = args.get("message") or args.get("content") or args.get("body") or ""
        expect_report = bool(args.get("expectReport") or args.get("expect_report") or False)
        priority = args.get("priority") or "normal"

        if not recipients:
            return self._error("send_message requires 'recipients' (list of agent names or short_ids)")
        if not message:
            return self._error("send_message requires 'message' (body text)")

        project_id = await self._get_project_id(agent_id)
        if not project_id:
            return self._error(f"Agent {agent_id} has no project_id")

        # Resolve each recipient: short_id (A001) or name → agent record
        all_agents = await self._org.list_agents(project_id)
        resolved = []
        not_found = []
        for r in recipients:
            r_stripped = r.strip()
            # Try short_id match
            match = None
            for a in all_agents:
                if a.get("short_id", "").upper() == r_stripped.upper():
                    match = a
                    break
            # Try name match (case-insensitive)
            if not match:
                for a in all_agents:
                    if a.get("name", "").lower() == r_stripped.lower():
                        match = a
                        break
            # Try role match (e.g. "HR")
            if not match:
                for a in all_agents:
                    if a.get("role", "").lower() == r_stripped.lower():
                        match = a
                        break
            if match:
                resolved.append(match)
            else:
                not_found.append(r)

        if not resolved:
            return self._error(
                f"No recipients found. Unknown: {not_found}. "
                f"Available agents: {[(a['name'], a.get('short_id'), a.get('role')) for a in all_agents]}"
            )

        results = []
        # BUG-034: Also record team chat for the SENDER so they can see
        # "发送 → RecipientName" in their team comms panel. Previously only
        # the recipient's inbox was written — sender had no record.
        from hiveweave.services.team_chat import TeamChatService
        team_chat = TeamChatService()
        for target in resolved:
            msg = await self._inbox.send_message(
                from_agent_id=agent_id,
                to_agent_id=target["id"],
                message=message,
                priority=priority,
                expect_report=expect_report,
            )
            results.append({
                "to": target["name"],
                "short_id": target.get("short_id"),
                "message_id": msg["id"],
            })
            # Record for sender so team comms panel shows outgoing messages
            await team_chat.record_message(
                agent_id=agent_id,
                from_agent_id=agent_id,
                to_agent_id=target["id"],
                content=message,
            )
            # BUG-022 fix: do NOT trigger here — the target agent's inbox watcher
            # (agent.py:_inbox_watcher_loop) polls every 5s and triggers autonomously.
            # Double-triggering (here + watcher) caused the Engineer to receive the
            # same task twice.

        not_found_str = f" (not found: {not_found})" if not_found else ""
        return {
            "success": True,
            "output": f"Message sent to {len(resolved)} agent(s): "
                      f"{', '.join(r['to'] for r in results)}{not_found_str}",
            "error": None,
        }

    async def _tool_list_subordinates(self, agent_id: str) -> dict:
        """list_subordinates: list direct children of the calling agent."""
        subs = await self._org.get_subordinates(agent_id)
        if not subs:
            return {"success": True, "output": "You have no direct subordinates.", "error": None}

        lines = []
        for s in subs:
            lines.append(
                f"- {s['name']} ({s.get('short_id', '?')}) | "
                f"role={s.get('role', '?')} | "
                f"status={s.get('status', '?')} | "
                f"goal={s.get('goal', '')[:80]}"
            )
        return {
            "success": True,
            "output": f"Direct subordinates ({len(subs)}):\n" + "\n".join(lines),
            "error": None,
        }

    async def _tool_hire_agent(self, agent_id: str, args: dict) -> dict:
        """hire_agent: HR creates a new agent via OrgService.create_agent.

        Args (from LLM):
            name: str — agent codename (e.g. 折纸)
            role: str — Chinese job title (e.g. 前端工程师)
            backstory: str — 2-4 sentence character narrative
            skills: list[str] — skill slugs
            parentId: str — parent agent ID (default: CEO)
            goal: str — agent's goal
            templateId: str — optional template ID
        """
        name = args.get("name") or ""
        role = args.get("role") or ""
        backstory = args.get("backstory") or ""
        skills = args.get("skills") or []
        parent_id = args.get("parentId") or args.get("parent_id") or ""
        goal = args.get("goal") or ""
        template_id = args.get("templateId") or args.get("template_id")

        if not name:
            return self._error("hire_agent requires 'name' (agent codename)")
        if not role:
            return self._error("hire_agent requires 'role' (job title)")

        project_id = await self._get_project_id(agent_id)
        if not project_id:
            return self._error(f"Agent {agent_id} has no project_id")

        # Resolve parent_id: LLM may pass short_id (e.g. "A001") instead of UUID.
        # Try to resolve via short_id or name lookup.
        all_agents = await self._org.list_agents(project_id)
        if parent_id:
            resolved_parent = None
            # Check if it's a valid UUID matching an existing agent
            for a in all_agents:
                if a["id"] == parent_id:
                    resolved_parent = parent_id
                    break
            # If not UUID match, try short_id match
            if not resolved_parent:
                for a in all_agents:
                    if a.get("short_id", "").upper() == parent_id.upper():
                        resolved_parent = a["id"]
                        log.info("tool.hire_agent.parent_resolved",
                                 short_id=parent_id, uuid=a["id"][:8])
                        break
            # If still not resolved, try name match
            if not resolved_parent:
                for a in all_agents:
                    if a.get("name", "").lower() == parent_id.lower():
                        resolved_parent = a["id"]
                        break
            parent_id = resolved_parent or ""

        # If no parentId specified or resolved, default to CEO
        if not parent_id:
            ceo = await self._org.get_agent_by_role(project_id, "ceo")
            if ceo:
                parent_id = ceo["id"]

        # Determine permission_type: coordinator roles → coordinator, else executor
        coordinator_roles = {"ceo", "hr", "qa", "cto", "architect", "manager", "pm"}
        perm_type = "coordinator" if role.lower() in coordinator_roles else "executor"
        perm_mode = "readonly" if perm_type == "coordinator" else "readwrite"

        # Get default model_id: 优先从项目现有 agent 继承，其次从 ModelService 取第一个 active model
        existing_agents = await self._org.list_agents(project_id)
        model_id = None
        if existing_agents:
            for a in existing_agents:
                if a.get("model_id"):
                    model_id = a["model_id"]
                    break
        if not model_id:
            try:
                ms = ModelService()
                active = await ms.list_active()
                if active:
                    # 优先选非 free 的 step 系列模型，否则第一个 active
                    step_models = [m for m in active if "step" in (m.get("model_id") or "").lower()]
                    non_free = [m for m in active if not (m.get("is_free") or m.get("free", False))]
                    chosen = step_models[0] if step_models else (non_free[0] if non_free else active[0])
                    model_id = chosen.get("model_id") or chosen.get("id")
                    log.info("tool.hire_agent.model_from_service", model_id=model_id)
            except Exception as e:
                log.warning("tool.hire_agent.model_service_failed", error=str(e))
        if not model_id:
            model_id = "step-3.7-flash"  # fallback

        # Get language from project
        project_row = await meta_db.query_one(
            "SELECT language FROM projects WHERE id = ?", [project_id]
        )
        language = project_row["language"] if project_row else "zh"

        attrs = {
            "project_id": project_id,
            "name": name,
            "role": role,
            "parent_id": parent_id,
            "backstory": backstory,
            "goal": goal or f"Execute {role} responsibilities.",
            "model_id": model_id,
            "permission_type": perm_type,
            "permission_mode": perm_mode,
            "skills": skills if isinstance(skills, list) else [],
            "allowed_tools": [],
            "language": language,
            "status": "active",
            # short_id and id are intentionally omitted — auto-generated by
            # OrgService.create_agent (short_id: A001-style auto-increment,
            # id: UUID). HR must NOT control these.
        }

        try:
            new_agent = await self._org.create_agent(attrs)
            new_id = new_agent.get("id", "?")
            new_short = new_agent.get("short_id", "?")

            # BUG-010 修复：创建后立即启动 agent，让它能处理 inbox 消息。
            # 否则 hire_agent 创建的 executor 只是 DB 一行，无法消费任务。
            try:
                from hiveweave.agents.supervisor import agent_manager
                from hiveweave.realtime.event_bus import create_agent_callbacks
                on_status, on_stream = create_agent_callbacks(new_id, project_id)
                started = await agent_manager.start_agent(
                    new_id, project_id, new_agent,
                    on_stream_event=on_stream, on_status_change=on_status,
                )
                log.info("tool.hire_agent.started",
                         agent_id=agent_id, new_agent_id=new_id,
                         new_short_id=new_short, name=name, role=role,
                         status=started.status.value if started else "none")
            except Exception as start_err:
                log.warning("tool.hire_agent.start_failed",
                            new_agent_id=new_id, error=str(start_err))

            log.info("tool.hire_agent", agent_id=agent_id,
                     new_agent_id=new_id, new_short_id=new_short,
                     name=name, role=role)
            return {
                "success": True,
                "output": (
                    f"✅ 招聘成功！\n"
                    f"  花名: {name}\n"
                    f"  角色: {role}\n"
                    f"  编号(short_id): {new_short}  ← 后续引用此人时使用此编号\n"
                    f"  内部ID: {new_id}\n"
                    f"  上级: {parent_id[:8]}...\n"
                    f"  权限: {perm_type}\n"
                    f"  模型: {model_id}\n"
                    f"  技能: {skills}\n"
                    f"  背景: {backstory[:100] if backstory else '(无)'}"
                ),
                "error": None,
            }
        except Exception as e:
            return self._error(f"Failed to hire agent: {e}")

    async def _tool_read_charter(self, agent_id: str) -> dict:
        """read_charter: read the project charter."""
        project_id = await self._get_project_id(agent_id)
        if not project_id:
            return self._error(f"Agent {agent_id} has no project_id")

        charter = await self._charter.read_charter(project_id)
        if not charter:
            return {"success": True, "output": "No charter has been saved yet.", "error": None}

        output = f"=== Project Charter ===\n"
        output += f"Title: {charter.get('title', 'N/A')}\n"
        output += f"Status: {charter.get('status', 'N/A')}\n"
        output += f"Content:\n{charter.get('content', 'N/A')}\n"
        return {"success": True, "output": output, "error": None}

    async def _tool_save_charter(self, agent_id: str, args: dict) -> dict:
        """save_charter: save/update the project charter."""
        project_id = await self._get_project_id(agent_id)
        if not project_id:
            return self._error(f"Agent {agent_id} has no project_id")

        content = args.get("content") or args.get("charter") or ""
        title = args.get("title") or "Project Charter"

        if not content:
            return self._error("save_charter requires 'content' (charter body)")

        try:
            charter_id = await self._charter.save_charter(
                project_id, agent_id,
                {"title": title, "content": content, "status": "active"},
            )
            return {
                "success": True,
                "output": f"Charter saved (id={charter_id[:8]}...). Title: {title}",
                "error": None,
            }
        except Exception as e:
            return self._error(f"Failed to save charter: {e}")

    async def _tool_read_goals(self, agent_id: str) -> dict:
        """read_goals: read enterprise goals."""
        project_id = await self._get_project_id(agent_id)
        if not project_id:
            return self._error(f"Agent {agent_id} has no project_id")

        goals = await self._charter.read_goals(project_id)
        if not goals:
            return {"success": True, "output": "No goals have been set yet.", "error": None}

        output = "=== Enterprise Goals ===\n"
        output += f"Objective: {goals.get('objective', 'N/A')}\n"
        output += f"Focus: {goals.get('focus', 'N/A')}\n"
        output += f"User Involvement: {goals.get('userInvolvement', 'N/A')}\n"
        krs = goals.get("keyResults", [])
        if krs:
            output += "Key Results:\n"
            for i, kr in enumerate(krs, 1):
                if isinstance(kr, dict):
                    output += f"  {i}. {kr.get('description', kr.get('text', str(kr)))}\n"
                else:
                    output += f"  {i}. {kr}\n"
        return {"success": True, "output": output, "error": None}

    async def _tool_update_goals(self, agent_id: str, args: dict) -> dict:
        """update_goals: update enterprise goals."""
        project_id = await self._get_project_id(agent_id)
        if not project_id:
            return self._error(f"Agent {agent_id} has no project_id")

        goals = {
            "objective": args.get("objective"),
            "focus": args.get("focus"),
            "key_results": args.get("keyResults") or args.get("key_results"),
            "user_involvement": args.get("userInvolvement") or args.get("user_involvement"),
        }
        # Remove None values
        goals = {k: v for k, v in goals.items() if v is not None}

        if not goals:
            return self._error("update_goals requires at least one of: objective, focus, keyResults, userInvolvement")

        try:
            await self._charter.update_goals(project_id, goals)
            return {"success": True, "output": "Goals updated successfully.", "error": None}
        except Exception as e:
            return self._error(f"Failed to update goals: {e}")

    async def _tool_view_org_chart(self, agent_id: str) -> dict:
        """view_org_chart: show the full organization tree."""
        project_id = await self._get_project_id(agent_id)
        if not project_id:
            return self._error(f"Agent {agent_id} has no project_id")

        tree = await self._org.get_full_tree(project_id)
        if not tree:
            return {"success": True, "output": "Org chart is empty.", "error": None}

        def format_node(node, indent=0):
            prefix = "  " * indent
            line = f"{prefix}- {node['name']} ({node.get('short_id', '?')}) role={node.get('role', '?')}"
            if node.get("goal"):
                line += f" goal={node['goal'][:60]}"
            lines = [line]
            for child in (node.get("children") or []):
                lines.extend(format_node(child, indent + 1))
            return lines

        all_lines = []
        for root in tree:
            all_lines.extend(format_node(root))

        return {"success": True, "output": "=== Org Chart ===\n" + "\n".join(all_lines), "error": None}

    async def _tool_read_work_logs(self, agent_id: str, args: dict) -> dict:
        """read_work_logs: read work logs from subordinates or specific agent."""
        target = args.get("agentId") or args.get("agent_id") or args.get("agent")

        project_id = await self._get_project_id(agent_id)
        if not project_id:
            return self._error(f"Agent {agent_id} has no project_id")

        # If target specified, resolve it; otherwise list all subordinates' logs
        if target:
            target_agent = await self._org.resolve_agent(target)
            if not target_agent:
                return self._error(f"Agent not found: {target}")
            target_ids = [target_agent["id"]]
        else:
            subs = await self._org.get_subordinates(agent_id)
            target_ids = [s["id"] for s in subs]

        if not target_ids:
            return {"success": True, "output": "No agents to read work logs from.", "error": None}

        # Query work_logs from per-project DB
        from hiveweave.db import project as project_db
        all_logs = []
        for tid in target_ids:
            try:
                rows = await project_db.query(
                    tid,
                    "SELECT agent_id, content, log_type, created_at FROM work_logs "
                    "WHERE agent_id = ? ORDER BY created_at DESC LIMIT 10",
                    [tid],
                )
                for r in rows:
                    all_logs.append(r)
            except Exception:
                pass  # Table might not exist yet

        if not all_logs:
            return {"success": True, "output": "No work logs found.", "error": None}

        lines = []
        for log in all_logs:
            ts = log.get("created_at", 0)
            lines.append(f"[{ts}] {log.get('agent_id', '?')[:8]}... ({log.get('log_type', '?')}): {log.get('content', '')[:100]}")
        return {"success": True, "output": f"=== Work Logs ({len(all_logs)}) ===\n" + "\n".join(lines), "error": None}

    async def _tool_write_work_log(self, agent_id: str, args: dict) -> dict:
        """write_work_log: record a work log entry for the calling agent.

        BUG-026 修复：补上 write_work_log 工具的实际分发。之前该工具只在
        agent.py 的 _TOOL_DESCRIPTIONS 里声明，LLM 调用时 _dispatch 找不到
        对应分支，返回 "Unknown tool: write_work_log"，导致 work-logs 永远为空。
        """
        project_id = await self._get_project_id(agent_id)
        if not project_id:
            return self._error(f"Agent {agent_id} has no project_id")
        from hiveweave.services.work_log import WorkLogService

        wl = WorkLogService()
        log_type = args.get("type") or args.get("logType") or "discussion"
        summary = (
            args.get("summary")
            or args.get("content")
            or args.get("message")
            or ""
        )
        if not summary:
            return self._error("write_work_log requires 'summary'")
        details = args.get("details") or args.get("metadata")
        log_id = await wl.write_work_log(
            project_id, agent_id, None, log_type, summary, details=details,
        )
        return {
            "success": True,
            "output": f"Work log written (id={log_id[:8]}..., type={log_type}).",
            "error": None,
        }
