import { DispatchService } from "./dispatch-service.js";
import { MemoryService } from "./memory-service.js";
import { OrgService } from "./org-service.js";
import { HandoffService } from "./handoff-service.js";
import { InboxService } from "./inbox-service.js";
import { RosterService } from "./roster-service.js";
import { FileService } from "./file-service.js";
import { ProjectService, formatGoalsForPrompt } from "./project-service.js";
import type { EnterpriseGoals } from "./project-service.js";
import { communicationService, userPingTracker } from "./communication-service.js";
import { TemplateService } from "./template-service.js";
import { clawhubService } from "./clawhub-service.js";
import { ShellService } from "./shell-service.js";
import { WebService } from "./web-service.js";
import type { TeamChatService } from "./team-chat-service.js";
import { ProjectCharterSchema, formatCharterForPrompt, parseGameTimeOffset, formatGameTime } from "@hiveweave/shared";
import type { GameTimeService } from "./game-time-service.js";
import type { AlarmService } from "./alarm-service.js";
import { prefixInterAgentMessage } from "./time-context.js";
import { statusEventBus } from "./status-event-bus.js";
import { randomUUID } from "crypto";
import { runBashCommand } from "./tools/bash.js";
import { executeGrep } from "./tools/grep.js";
import { executeApplyPatch } from "./tools/apply-patch.js";
import { executeQuestion } from "./tools/question.js";
import { executeTodoWrite } from "./tools/todowrite.js";
import { executeWebSearch } from "./tools/websearch.js";
import { runCodeReview, runSecurityAudit, runTestReview, runPerfAudit, runFullReview } from "./tools/review.js";
import type { ReviewLLMCallback, ReviewResult } from "./tools/review.js";
import { GitWorktreeService } from "./git-worktree-service.js";
import { existsSync } from "fs";
import { Effect } from "effect";
import { mcpService } from "./mcp/mcp-service.js";

// ---------------------------------------------------------------------------
// Binding Registry — available skills and MCP servers
// Following OpenCode/OpenClaw pattern: config-driven registry, per-agent binding.
// This is a static placeholder; will be replaced with DB-backed config later.
// ---------------------------------------------------------------------------

interface RegistryItem {
  name: string;
  description: string;
  type?: string;
}

const BINDING_REGISTRY: { skills: RegistryItem[]; mcpServers: RegistryItem[] } = {
  skills: [
    { name: "code-review", description: "Review code for quality, patterns, and potential bugs." },
    { name: "testing", description: "Write and run unit tests and integration tests." },
    { name: "documentation", description: "Generate and maintain project documentation." },
    { name: "debugging", description: "Diagnose and fix runtime errors and performance issues." },
    { name: "refactoring", description: "Restructure code for better maintainability without changing behavior." },
    { name: "security-audit", description: "Scan code for security vulnerabilities and best practice violations." },
    { name: "deployment", description: "Manage CI/CD pipelines and deployment workflows." },
    { name: "data-analysis", description: "Analyze data sets, generate reports and visualizations." },
  ],
  mcpServers: [
    { name: "filesystem", description: "Read/write/search files in the workspace.", type: "local" },
    { name: "github", description: "Interact with GitHub repositories, issues, and PRs.", type: "remote" },
    { name: "browser", description: "Browse web pages and extract information.", type: "local" },
    { name: "database", description: "Query and manage database records.", type: "local" },
    { name: "slack", description: "Send and read messages in Slack channels.", type: "remote" },
  ],
};

/**
 * ToolExecutor — routes HiveWeave tool calls to the appropriate service.
 * Called by the AgentRuntime when the LLM returns a tool_use event.
 */

const HR_ONLY_TOOLS = new Set([
  "create_agent",
  "transfer_agent",
  "dismiss_agent",
  "update_roster",
]);

async function assertRole(
  org: OrgService,
  agentId: string,
  allowedRoles: string[],
): Promise<{ ok: true; caller: any } | { ok: false; error: string }> {
  const caller = await org.getAgent(agentId);
  if (!caller) {
    return { ok: false, error: "Error: Could not find your own agent record." };
  }
  const role = String(caller.role || "").toLowerCase();
  const allowed = allowedRoles.map((r) => r.toLowerCase());
  if (!allowed.includes(role)) {
    return {
      ok: false,
      error: `Error: This tool requires role ${allowedRoles.join(" or ")}. Your role is "${caller.role}".`,
    };
  }
  return { ok: true, caller };
}


export class ToolExecutor {
  constructor(
    private readonly dispatch: DispatchService,
    private readonly memory: MemoryService,
    private readonly org: OrgService,
    private readonly handoff: HandoffService,
    private readonly inbox: InboxService,
    private readonly roster: RosterService,
    private readonly files: FileService,
    private readonly projects: ProjectService,
    private readonly templates: TemplateService,
    private readonly shell: ShellService,
    private readonly web: WebService,
    private readonly teamChat?: TeamChatService,
    private readonly gameTimeService?: GameTimeService,
    private readonly alarmService?: AlarmService,
    private readonly projectId?: string | null,
    private readonly reviewLLM?: ReviewLLMCallback,
  ) {}

  private getTimeSnapshot() {
    if (!this.gameTimeService || !this.projectId) return null;
    return this.gameTimeService.getSnapshot(this.projectId);
  }

  private prefixForInbox(message: string): string {
    const snap = this.getTimeSnapshot();
    if (!snap) return message;
    return prefixInterAgentMessage(snap, message);
  }

  /**
   * Execute a single tool call and return a human-readable result string.
   * The result is fed back to Claude as a tool_result message.
   */
  async execute(
    agentId: string,
    sessionId: string,
    toolName: string,
    input: Record<string, any>,
  ): Promise<string> {
    // Strip the hiveweave__ prefix if present
    const name = toolName.replace(/^hiveweave__/, "");

    console.log(`[TOOL] execute: ${name} agent=${agentId.slice(0, 8)} input=${JSON.stringify(input).slice(0, 200)}`);

    try {
      switch (name) {
        // ── Dispatch ──────────────────────────────────────────
        case "dispatch_task": {
          const { description, expectReport } = input;
          const toAgentId = typeof input.toAgentId === "string" ? input.toAgentId.trim() : input.toAgentId;
          if (!toAgentId || !description) {
            return "Error: dispatch_task requires toAgentId and description.";
          }
          // Resolve agent ID (supports partial/prefix IDs from LLM)
          const target = await this.org.resolveAgent(toAgentId);
          if (!target) {
            return `Error: No agent found with ID "${toAgentId}".`;
          }
          const resolvedToId = target.id;
          const targetName = target.name || toAgentId;
          const result = await this.dispatch.dispatchTask({
            fromAgentId: agentId,
            toAgentId: resolvedToId,
            description,
            sessionId,
          });
          // Create a handoff record so the subordinate can receive the task
          const handoffId = await this.handoff.createHandoff({
            fromAgentId: agentId,
            toAgentId: resolvedToId,
            summary: description,
            expectReport: expectReport === true,
          });
          // Track this as an active communication for the org chart
          communicationService.addCommunication(agentId, resolvedToId, "dispatch");
          if (this.teamChat) {
            await this.teamChat.recordIncoming(resolvedToId, agentId, description, handoffId);
            await this.teamChat.recordOutgoing(agentId, resolvedToId, description, JSON.stringify([{ tool: name, input }]));
          }
          return `Task dispatched to ${targetName}. taskId=${result.taskId}, handoffId=${handoffId}`;
        }

        // ── Work Logs ─────────────────────────────────────────
        case "write_work_log": {
          const { type, summary, details } = input;
          if (!summary) {
            return "Error: write_work_log requires a summary.";
          }
          const logId = await this.dispatch.writeWorkLog({
            agentId,
            sessionId,
            type: type || "discussion",
            summary,
            details: details || undefined,
          });
          return `Work log written. logId=${logId}`;
        }

        case "read_work_logs": {
          const { subordinateId, limit } = input;
          const logs = subordinateId
            ? await this.dispatch.getSubordinateLogs(subordinateId, limit || 10)
            : await this.dispatch.getAgentLogs(agentId, limit || 20);
          if (logs.length === 0) return "No work logs found.";
          return logs
            .map((l: any) => {
              const time = new Date(l.createdAt).toISOString();
              return `[${time}] ${l.type}: ${l.summary}`;
            })
            .join("\n");
        }

        // ── Completion ────────────────────────────────────────
        case "report_completion": {
          const { summary, handoffId } = input;
          const logId = await this.dispatch.writeWorkLog({
            agentId,
            sessionId,
            type: "completion",
            summary: summary || "Task completed.",
          });
          // Complete the associated handoff (if any)
          const handoffResult = await this.handoff.completeHandoff(agentId, handoffId);
          // Update agent status if possible
          try {
            await this.org.updateStatus(agentId, "active");
          } catch {
            // Non-critical: status update is best-effort
          }
          const handoffNote = handoffResult.completed
            ? ` handoffId=${handoffResult.handoffId}`
            : "";
          return `Completion reported. logId=${logId}${handoffNote}`;
        }

        // ── Review (coordinator tools) ────────────────────────
        case "approve_work": {
          const { review } = input;
          const subordinateId = typeof input.subordinateId === "string" ? input.subordinateId.trim() : input.subordinateId;
          if (!subordinateId) {
            return "Error: approve_work requires subordinateId.";
          }
          const logId = await this.dispatch.approveWork(agentId, sessionId, subordinateId, review);
          const sub = await this.org.resolveAgent(subordinateId);
          const subName = sub?.name || subordinateId;
          const resolvedSubId = sub?.id || subordinateId;
          // Transition handoff from "completed" to "approved" (terminal state)
          const approval = await this.handoff.approveHandoff(agentId, resolvedSubId);
          const statusNote = approval.approved
            ? ` Handoff ${approval.handoffId} marked as approved.`
            : " (No completed handoff found to approve — work log recorded anyway.)";
          return `Work by ${subName} approved. logId=${logId}${statusNote}`;
        }

        case "reject_work": {
          const { feedback } = input;
          const subordinateId = typeof input.subordinateId === "string" ? input.subordinateId.trim() : input.subordinateId;
          if (!subordinateId) {
            return "Error: reject_work requires subordinateId.";
          }
          const feedbackText = feedback || "Needs revision";
          const logId = await this.dispatch.rejectWork(agentId, sessionId, subordinateId, feedbackText);
          const sub = await this.org.resolveAgent(subordinateId);
          const subName = sub?.name || subordinateId;
          const resolvedSubId = sub?.id || subordinateId;
          // Reopen the handoff from "completed" back to "accepted" so subordinate can rework
          const reopened = await this.handoff.reopenHandoff(agentId, resolvedSubId);
          // Send rejection feedback to subordinate's inbox so they get triggered
          await this.inbox.sendMessage(agentId, resolvedSubId, `[REWORK REQUESTED] ${feedbackText}`, "superior", true);
          const reopenNote = reopened.reopened
            ? ` Handoff ${reopened.handoffId} reopened for rework.`
            : " (No completed handoff found to reopen — rejection logged anyway.)";
          return `Work by ${subName} rejected with feedback: "${feedbackText}". ${subName} will be notified to rework.${reopenNote} logId=${logId}`;
        }

        case "review_code": {
          const { limit } = input;
          const subordinateId = typeof input.subordinateId === "string" ? input.subordinateId.trim() : input.subordinateId;
          if (!subordinateId) {
            return "Error: review_code requires subordinateId.";
          }
          const logs = await this.dispatch.getSubordinateLogs(subordinateId, limit || 5);
          if (logs.length === 0) return "No recent work to review.";
          const summary = logs
            .map((l: any) => `- [${l.type}] ${l.summary}`)
            .join("\n");
          return `Recent work by subordinate:\n${summary}`;
        }

        case "trigger_integration": {
          // Placeholder: write an integration-trigger log entry
          const logId = await this.dispatch.writeWorkLog({
            agentId,
            sessionId,
            type: "decision",
            summary: "Integration test triggered.",
            details: input,
          });
          return `Integration test triggered. logId=${logId}`;
        }

        // ── Memory ────────────────────────────────────────────
        case "read_project_memory": {
          const memories = await this.memory.getProjectMemories();
          if (memories.length === 0) return "No project memories found.";
          return memories
            .map((m: any) => `[${m.type}] ${m.content.slice(0, 200)}`)
            .join("\n---\n");
        }

        case "write_memory": {
          const { type, content } = input;
          if (!content) return "Error: write_memory requires a content string.";
          const memType = type || "fact";
          const id = await this.memory.writeMemory({
            agentId,
            scope: "agent",
            type: memType,
            content,
            sourceAgentId: agentId,
          });
          return `Memory saved (id: ${id.slice(0, 8)}, type: ${memType}). This will persist across sessions and be available in your context on every turn.`;
        }

        // ── Upward Communication ─────────────────────────────
        case "message_superior": {
          const { message, priority } = input;
          if (!message) {
            return "Error: message_superior requires a message.";
          }
          // Find the current agent's parent (superior)
          const currentAgent = await this.org.getAgent(agentId);
          if (!currentAgent?.parentId) {
            return "You don't have a superior to message. You are a root agent.";
          }
          const superiorId = currentAgent.parentId;
          const validPriority = (priority === "low" || priority === "urgent") ? priority : "normal";
          const msgId = await this.inbox.sendMessage(agentId, superiorId, this.prefixForInbox(message), "superior", false, validPriority);
          const superior = await this.org.getAgent(superiorId);
          const superiorName = superior?.name || superiorId;
          // Track this as an active communication for the org chart
          communicationService.addCommunication(agentId, superiorId, "message");
          if (this.teamChat) {
            await this.teamChat.recordIncoming(superiorId, agentId, message, msgId);
            await this.teamChat.recordOutgoing(agentId, superiorId, message, JSON.stringify([{ tool: name, input }]));
          }
          return `Message sent to ${superiorName}. msgId=${msgId}`;
        }

        // ── Peer Communication ─────────────────────────────
        // ── Send Message (to user or agent) — the ONLY inter-agent messaging tool ──
        case "send_message": {
          // Support both new params (content/recipients) and legacy aliases (message/recipient/toAgentId)
          const content = input.content || input.message;
          let recipients = input.recipients || input.recipient || "";
          const toAgentId = input.toAgentId; // legacy alias from old message_peer
          const { expectReport, priority } = input;
          if (!content || (!recipients && !toAgentId)) {
            return "Error: send_message requires content and recipients (or recipient/toAgentId).";
          }
          // Merge toAgentId into recipients list if present
          if (toAgentId && !recipients) {
            recipients = toAgentId;
          } else if (toAgentId) {
            recipients = String(recipients) + "," + toAgentId;
          }
          const validPriority = (priority === "low" || priority === "urgent") ? priority : "normal";
          const list = String(recipients).split(",").map((s: string) => s.trim()).filter(Boolean);
          const results: string[] = [];
          for (const rcpt of list) {
            if (rcpt.toLowerCase() === "user") {
              // Direct message to the human operator — visible in this agent's chat
              if (this.teamChat) {
                const msgId = randomUUID();
                try {
                  await (this.teamChat as any).chat.saveMessage({
                    id: msgId,
                    agentId,
                    role: "assistant",
                    content: `📩 ${this.prefixForInbox(content)}`,
                    isBackground: false,
                    isRead: false,
                    createdAt: Date.now(),
                  });
                } catch { /* best-effort */ }
              }
              userPingTracker.ping(agentId);
              results.push("Message delivered to human operator.");
            } else {
              const target = await this.org.resolveAgent(rcpt);
              if (!target) {
                results.push(`Error: Could not find agent matching "${rcpt}". Try using their shortId (e.g. A001) instead — use \`read_roster\` to find it.`);
                continue;
              }
              // Self-send guard: prevent agents from messaging themselves
              if (target.id === agentId) {
                const self = await this.org.getAgent(agentId);
                const selfName = self?.name || "you";
                results.push(
                  `⚠️ STOP — You are trying to send a message to yourself ("${rcpt}" resolved to ${selfName}, which is YOU). ` +
                  `If you meant to reply to someone, use their name as the recipient. ` +
                  `Use \`list_subordinates\` to see your team, or \`read_roster\` to see all agents. ` +
                  `If you meant to talk to the human operator, use recipients="user".`
                );
                continue;
              }
              const msgId = await this.inbox.sendMessage(agentId, target.id, this.prefixForInbox(content), "peer", expectReport === true, validPriority);
              communicationService.addCommunication(agentId, target.id, "peer");
              if (this.teamChat) {
                await this.teamChat.recordIncoming(target.id, agentId, content, msgId);
                await this.teamChat.recordOutgoing(agentId, target.id, content, JSON.stringify([{ tool: name, input }]));
              }
              const resultLine = `Sent to ${target.name}. msgId=${msgId}`;
              if (expectReport) results.push(resultLine + " (report expected)");
              else results.push(resultLine);
            }
          }
          return results.join("\n");
        }

        // ── List Subordinates (coordinator tool) ───────────
        case "list_subordinates": {
          const children = await this.org.getChildren(agentId);
          if (children.length === 0) {
            return "You have no direct subordinates.";
          }
          const lines = await Promise.all(
            children.map(async (child: any) => {
              const pendingHandoffs = await this.handoff.getPendingHandoffs(child.id);
              const acceptedHandoffs = await this.org.getChildren(child.id);
              const recentLogs = await this.dispatch.getSubordinateLogs(child.id, 1);
              const lastActivity = recentLogs.length > 0
                ? `[${new Date(recentLogs[0].createdAt).toISOString()}] ${recentLogs[0].summary.slice(0, 80)}`
                : "No recent activity";
              const taskStatus = pendingHandoffs.length > 0
                ? `${pendingHandoffs.length} pending task(s)`
                : "No active tasks";
              const subCount = acceptedHandoffs.length;
              const busy = statusEventBus.isProcessing(child.id);
              const statusBadge = busy ? "🔴 working" : "🟢 idle";
              return `- **${child.name}** (${child.role}) — ${statusBadge} | ${taskStatus} | Subordinates: ${subCount} | Last: ${lastActivity}`;
            }),
          );
          return `## Your Subordinates (${children.length})\n${lines.join("\n")}`;
        }

        // ── HR: Create Agent (hire/recruit) ─────────────────
        case "create_agent": {
          {
            const roleCheck = await assertRole(this.org, agentId, ["hr"]);
            if (roleCheck.ok === false) return roleCheck.error;
          }
          const { name, role, description, backstory: inputBackstory, permissionType, position, department, responsibilities } = input;
          const parentId = typeof input.parentId === "string" ? input.parentId.trim() : input.parentId;
          if (!name || !role || !description || !position) {
            return "Error: create_agent requires name, role, description, and position (Chinese job title).";
          }
          // Name must contain Chinese characters
          if (!/[\u4e00-\u9fa5]/.test(String(name))) {
            return "Error: Agent name must contain Chinese characters (e.g. 张三, 李四).";
          }
          // Position must contain Chinese characters
          if (!/[\u4e00-\u9fa5]/.test(String(position))) {
            return "Error: Position must be a Chinese job title (e.g. 前端工程师, 后端开发, 产品经理).";
          }
          const normalizedRole = String(role).toLowerCase();
          if (normalizedRole === "ceo" || normalizedRole === "hr") {
            return "Error: Cannot create agents with role ceo or hr. These roles are reserved.";
          }

          // Goal must be project-aligned; backstory MUST be a personal narrative from HR
          const goal = input.goal || `As ${role}, fulfill the following responsibilities: ${description}`;
          const backstory = inputBackstory || "";

          // Parse comma-separated skill and MCP server lists
          const initialSkills = typeof input.skills === "string" && input.skills.trim()
            ? input.skills.split(",").map((s: string) => s.trim()).filter(Boolean)
            : [];
          const initialMcp = typeof input.mcpServers === "string" && input.mcpServers.trim()
            ? input.mcpServers.split(",").map((s: string) => s.trim()).filter(Boolean)
            : [];

          // Validate skill slugs against ClawHub marketplace
          let skillWarning = "";
          if (initialSkills.length > 0) {
            const verified: string[] = [];
            const unverified: string[] = [];
            for (const slug of initialSkills) {
              const detail = await clawhubService.getSkillDetail(slug);
              if (detail) verified.push(slug);
              else unverified.push(slug);
            }
            if (unverified.length > 0) {
              skillWarning = `\n\n⚠️ Skill validation: [${unverified.join(", ")}] not found in ClawHub marketplace. ` +
                `These will be stored as-is but won't provide any skill instructions. ` +
                `Use list_available_skills("keyword") to search for real skills before creating agents. ` +
                (verified.length > 0 ? `Verified skills: [${verified.join(", ")}].` : "");
            }
          }

          // Look up the calling agent to get projectId
          const caller = await this.org.getAgent(agentId);
          if (!caller) {
            return "Error: Could not find your own agent record.";
          }
          const projectId = caller.projectId || undefined;

          // Validate parentId — default to CEO when omitted (HR must not create root agents)
          let resolvedParentId: string | null = null;
          let parentName: string | null = null;
          if (parentId) {
            const parentAgent = await this.org.resolveAgent(parentId);
            if (!parentAgent) {
              return `Error: No agent found with ID ${parentId}.`;
            }
            if (projectId && parentAgent.projectId !== projectId) {
              return `Error: Parent agent belongs to a different project.`;
            }
            resolvedParentId = parentAgent.id;
            parentName = parentAgent.name;
          } else if (projectId) {
            const ceo = await this.org.findAgentByRole(projectId, "ceo");
            if (ceo) {
              resolvedParentId = ceo.id;
              parentName = ceo.name;
            }
          }

          const permType = permissionType === "coordinator" ? "coordinator" : "executor";

          // IRON RULE: HR can NEVER create agents under itself
          if (caller.role?.toLowerCase() === "hr" && resolvedParentId === agentId) {
            console.log(`[TOOL] create_agent: BLOCKED — HR tried to create agent under itself`);
            return "Error: HR cannot create agents under itself. New agents default to the CEO — omit parentId to place them under the CEO, or specify another manager's ID explicitly.";
          }

          console.log(`[TOOL] create_agent: name="${name}" parentId=${resolvedParentId} callerId=${agentId} skills=${JSON.stringify(initialSkills)} mcp=${JSON.stringify(initialMcp)}`);
          const newId = await this.org.createAgent({
            name,
            role,
            goal,
            backstory: backstory || "",
            skills: initialSkills,
            parentId: resolvedParentId || undefined,
            projectId,
            permissionType: permType,
            mcpServers: initialMcp.length > 0 ? initialMcp : undefined,
            boundSkills: initialSkills.length > 0 ? initialSkills : undefined,
          });

          // Auto-create roster entry for the new agent
          try {
            await this.roster.upsertRecord({
              projectId: projectId || "",
              agentId: newId,
              position: position || `${name} (${role})`,
              department: department || "",
              responsibilities: responsibilities || goal,
              notes: "",
              status: "active",
              updatedBy: agentId,
            });
          } catch {
            // Non-critical: roster entry creation is best-effort
          }

          const permLabel = permType === "coordinator" ? "协调者" : "执行者";
          const newAgent = await this.org.getAgent(newId);
          const newShortId = newAgent?.shortId || newId;
          const parentLabel = resolvedParentId
            ? `under ${parentName || "unknown"}`
            : "as root-level agent";
          const bindingNote = [
            initialSkills.length > 0 ? `Skills: [${initialSkills.join(", ")}]` : null,
            initialMcp.length > 0 ? `MCP: [${initialMcp.join(", ")}]` : null,
          ].filter(Boolean).join(" | ");
          return `Agent created successfully!\nName: ${name}\nRole: ${role}\nType: ${permLabel}\nPlacement: ${parentLabel}${bindingNote ? `\n${bindingNote}` : ""}\nRoster entry created.${skillWarning}`;
        }

        // ── HR: Transfer Agent (re-parent) ──────────────────
        case "transfer_agent": {
          {
            const roleCheck = await assertRole(this.org, agentId, ["hr"]);
            if (!roleCheck.ok) return roleCheck.error;
          }
          const targetAgentId = typeof input.agentId === "string" ? input.agentId.trim() : input.agentId;
          const newParentId = typeof input.newParentId === "string" ? input.newParentId.trim() : input.newParentId;
          if (!targetAgentId) {
            return "Error: transfer_agent requires agentId.";
          }
          const target = await this.org.resolveAgent(targetAgentId);
          if (!target) {
            return `Error: No agent found with ID ${targetAgentId}.`;
          }
          const resolvedTargetId = target.id;

          // Cycle detection: walk up from newParent to check if target is an ancestor
          let resolvedParentId: string | null = null;
          let newParentShortId: string | null = null;
          if (newParentId && newParentId !== "") {
            const newParent = await this.org.resolveAgent(newParentId);
            if (!newParent) {
              return `Error: No agent found with ID ${newParentId}.`;
            }
            resolvedParentId = newParent.id; // Use full resolved ID
            newParentShortId = newParent.shortId || null;
            // Validate same project
            if (target.projectId && newParent.projectId !== target.projectId) {
              return "Error: Cannot transfer to a parent in a different project.";
            }
            // Walk up the parent chain from newParent
            let current = newParent;
            while (current) {
              if (current.id === resolvedTargetId) {
                return "Error: Cannot transfer agent — this would create a cycle in the hierarchy.";
              }
              if (!current.parentId) break;
              const ancestor = await this.org.getAgent(current.parentId);
              if (!ancestor) break;
              current = ancestor;
            }
          }

          // IRON RULE: HR can NEVER make anyone its subordinate
          if (resolvedParentId === agentId) {
            const caller = await this.org.getAgent(agentId);
            if (caller?.role?.toLowerCase() === "hr") {
              console.log(`[TOOL] transfer_agent: BLOCKED — HR tried to transfer agent under itself`);
              return "Error: HR cannot make agents its subordinates. You are a personnel service role. Transfer them under a different agent or set parentId to null.";
            }
          }

          console.log(`[TOOL] transfer_agent: moving ${target.name} (${resolvedTargetId}) → parent ${resolvedParentId || "root"}`);
          await this.org.updateParent(resolvedTargetId, resolvedParentId);

          // Verify the update took effect
          const verify = await this.org.getAgent(resolvedTargetId);
          console.log(`[TOOL] transfer_agent: verified parentId=${verify?.parentId}`);

          const parentLabel = resolvedParentId
            ? `under agent ${newParentShortId || resolvedParentId.slice(0, 8)}`
            : "as root-level (no parent)";
          return `Agent "${target.name}" transferred ${parentLabel}.`;
        }

        // ── HR: Dismiss Agent (soft-delete) ─────────────────
        case "dismiss_agent": {
          {
            const roleCheck = await assertRole(this.org, agentId, ["hr"]);
            if (!roleCheck.ok) return roleCheck.error;
          }
          const targetAgentId = typeof input.agentId === "string" ? input.agentId.trim() : input.agentId;
          const { reason } = input;
          if (!targetAgentId) {
            return "Error: dismiss_agent requires agentId.";
          }
          const target = await this.org.resolveAgent(targetAgentId);
          if (!target) {
            return `Error: No agent found with ID ${targetAgentId}.`;
          }
          const resolvedTargetId = target.id;
          // Cannot dismiss self
          if (resolvedTargetId === agentId) {
            return "Error: You cannot dismiss yourself.";
          }
          // Check for active children
          const children = await this.org.getChildren(resolvedTargetId);
          const activeChildren = children.filter((c: any) => c.status !== "archived");
          if (activeChildren.length > 0) {
            return `Error: Cannot dismiss "${target.name}" — they have ${activeChildren.length} active subordinate(s). Transfer or dismiss subordinates first.`;
          }
          // Soft-delete: set status to archived
          await this.org.updateStatus(resolvedTargetId, "archived");
          // Terminate roster record
          await this.roster.terminateRecord(resolvedTargetId, agentId);

          const reasonNote = reason ? ` Reason: ${reason}` : "";
          return `Agent "${target.name}" has been dismissed (archived). Roster record terminated.${reasonNote}`;
        }

        // ── HR: Update Roster ───────────────────────────────
        case "update_roster": {
          {
            const roleCheck = await assertRole(this.org, agentId, ["hr"]);
            if (!roleCheck.ok) return roleCheck.error;
          }
          const targetAgentId = typeof input.agentId === "string" ? input.agentId.trim() : input.agentId;
          const { position, department, responsibilities, notes, status } = input;
          if (!targetAgentId) {
            return "Error: update_roster requires agentId.";
          }
          const target = await this.org.resolveAgent(targetAgentId);
          if (!target) {
            return `Error: No agent found with ID ${targetAgentId}.`;
          }
          const resolvedTargetId = target.id;

          const recordId = await this.roster.upsertRecord({
            projectId: target.projectId || "",
            agentId: resolvedTargetId,
            position,
            department,
            responsibilities,
            notes,
            status,
            updatedBy: agentId,
          });
          return `Roster updated for "${target.name}" (${target.shortId || targetAgentId}). recordId=${recordId}`;
        }

        // ── Read Roster (available to ALL agents) ──────────
        case "read_roster": {
          const caller = await this.org.getAgent(agentId);
          if (!caller?.projectId) {
            return "Error: Could not determine your project.";
          }
          const records = await this.roster.getProjectRoster(caller.projectId);
          if (records.length === 0) return "The personnel roster is currently empty.";

          // Build agent ID → shortId lookup
          const allAgents = await this.org.getProjectAgents(caller.projectId);
          const agentMap = new Map(allAgents.map(a => [a.id, a]));

          const lines = records.map((r: any) => {
            const statusBadge = r.status === "active" ? "✅" : r.status === "probation" ? "⚠️" : "🔲";
            const agent = agentMap.get(r.agentId);
            const displayId = agent?.shortId || r.agentId.slice(0, 8);
            const displayName = agent?.name ? ` ${agent.name}` : "";
            return `${statusBadge} **${r.position || "(no position)"}** — ${displayName} (${displayId}) | Dept: ${r.department || "—"} | Status: ${r.status}\n   Responsibilities: ${r.responsibilities || "—"}`;
          });
          return `## Personnel Roster (${records.length} records)\n${lines.join("\n")}`;
        }

        // ── HR: List All Agents (full org tree) ────────────
        case "list_all_agents": {
          const caller = await this.org.getAgent(agentId);
          if (!caller?.projectId) {
            return "Error: Could not determine your project.";
          }
          const tree = await this.org.getOrgTree(caller.projectId);

          // Flatten tree into a list with path info
          const flatList: string[] = [];
          const flatten = (nodes: any[], depth: number = 0) => {
            for (const node of nodes) {
              const indent = "  ".repeat(depth);
              const permBadge = node.permissionType === "coordinator" ? "👔" : "⚙️";
              flatList.push(`${indent}${permBadge} **${node.name}** (${node.role})`);
              if (node.children && node.children.length > 0) {
                flatten(node.children, depth + 1);
              }
            }
          };
          flatten(Array.isArray(tree) ? tree : [tree]);

          if (flatList.length === 0) return "No agents in the organization.";
          return `## Full Organization (${flatList.length} agents)\n${flatList.join("\n")}`;
        }

        // ── Check Agent Status (real-time busy/idle) ──────────
        case "check_agent_status": {
          const targetId = typeof input.agentId === "string" ? input.agentId.trim() : input.agentId;
          if (targetId) {
            // Check a specific agent
            const target = await this.org.resolveAgent(targetId);
            if (!target) {
              return `Error: No agent found with ID "${targetId}".`;
            }
            const busy = statusEventBus.isProcessing(target.id);
            const status = busy ? "🔴 working (currently processing, do NOT disturb unless urgent)" : "🟢 idle (available for new tasks)";
            return `**${target.name}** (${target.role}) — ${status}`;
          }
          // No specific agent — list all agents in the project with status
          const caller = await this.org.getAgent(agentId);
          if (!caller?.projectId) {
            return "Error: Could not determine your project.";
          }
          const allAgents = await this.org.getProjectAgents(caller.projectId);
          if (allAgents.length === 0) return "No agents in the project.";
          const lines = allAgents.map((a: any) => {
            const busy = statusEventBus.isProcessing(a.id);
            const badge = busy ? "🔴 working" : "🟢 idle";
            const permBadge = a.permissionType === "coordinator" ? "👔" : "⚙️";
            return `- ${permBadge} **${a.name}** (${a.role}) — ${badge}`;
          });
          return `## Agent Status (${allAgents.length} agents)\n${lines.join("\n")}`;
        }


        // ── Charter tools ─────────────────────────────────────
        case "save_charter": {
          const roleCheck = await assertRole(this.org, agentId, ["ceo"]);
          if (!roleCheck.ok) return roleCheck.error;
          const caller = roleCheck.caller;
          if (!caller.projectId) return "Error: Could not determine your project.";
          const charterJson = typeof input.charterJson === "string" ? input.charterJson.trim() : "";
          if (!charterJson) return "Error: save_charter requires charterJson.";
          let parsed: any;
          try {
            parsed = JSON.parse(charterJson);
          } catch {
            return "Error: charterJson is not valid JSON.";
          }
          const validated = ProjectCharterSchema.safeParse(parsed);
          if (!validated.success) {
            return `Error: Invalid charter: ${validated.error.message}`;
          }
          await this.projects.saveCharter(caller.projectId, validated.data, agentId);
          return "Project charter saved successfully.";
        }

        case "read_charter": {
          const roleCheck = await assertRole(this.org, agentId, ["ceo", "hr"]);
          if (!roleCheck.ok) return roleCheck.error;
          const caller = roleCheck.caller;
          if (!caller.projectId) return "Error: Could not determine your project.";
          const charter = await this.projects.getCharter(caller.projectId);
          if (!charter) return "No project charter has been defined yet.";
          return formatCharterForPrompt(charter);
        }

        // ── Enterprise Goals (workboard — visible to ALL agents) ──
        case "read_goals": {
          const caller = await this.org.getAgent(agentId);
          if (!caller?.projectId) return "Error: Could not determine your project.";
          const goals = await this.projects.getGoals(caller.projectId);
          if (!goals) return "No enterprise goals have been set yet. The workboard is empty.";
          return formatGoalsForPrompt(goals);
        }

        case "update_goals": {
          const roleCheck = await assertRole(this.org, agentId, ["ceo", "manager", "coordinator"]);
          if (!roleCheck.ok) return roleCheck.error;
          const caller = roleCheck.caller;
          if (!caller.projectId) return "Error: Could not determine your project.";
          const existing = await this.projects.getGoals(caller.projectId) || {
            objective: "",
            focus: "",
            keyResults: [],
          };
          if (input.objective !== undefined) existing.objective = input.objective;
          if (input.focus !== undefined) existing.focus = input.focus;
          if (Array.isArray(input.keyResults)) {
            const newKRs = input.keyResults as Array<{ text: string; status: string; owner?: string }>;
            for (const kr of newKRs) {
              const idx = existing.keyResults.findIndex((k) => k.text === kr.text);
              if (idx >= 0) {
                existing.keyResults[idx] = {
                  ...existing.keyResults[idx],
                  status: (kr.status as "todo" | "doing" | "done") || existing.keyResults[idx].status,
                  owner: kr.owner ?? existing.keyResults[idx].owner,
                };
              } else {
                existing.keyResults.push({
                  text: kr.text,
                  status: (kr.status as "todo" | "doing" | "done") || "todo",
                  owner: kr.owner,
                });
              }
            }
          }
          await this.projects.saveGoals(caller.projectId, existing as EnterpriseGoals);
          return "Enterprise goals updated. All agents will see the updated workboard.";
        }

        // ── File Operations (workspace tools) ──────────────────
        case "read_file": {
          let wp: string; try { wp = await this.resolveWorkspace(agentId); } catch (e: any) { return `Error: ${typeof e === "string" ? e : e.message}`; }
          const { filePath, offset, limit } = input;
          if (!filePath) return "Error: read_file requires filePath.";
          return await this.files.readFile(wp, filePath, offset, limit);
        }

        case "write_file": {
          let wp: string; try { wp = await this.resolveWorkspace(agentId); } catch (e: any) { return `Error: ${typeof e === "string" ? e : e.message}`; }
          const { filePath, content, append } = input;
          if (!filePath || content === undefined) return "Error: write_file requires filePath and content.";
          return await this.files.writeFile(wp, filePath, content, append);
        }

        case "edit_file": {
          let wp: string; try { wp = await this.resolveWorkspace(agentId); } catch (e: any) { return `Error: ${typeof e === "string" ? e : e.message}`; }
          const { filePath, oldText, newText } = input;
          if (!filePath) return "Error: edit_file requires filePath.";
          return await this.files.editFile(wp, filePath, oldText || "", newText || "");
        }

        case "list_files": {
          let wp: string; try { wp = await this.resolveWorkspace(agentId); } catch (e: any) { return `Error: ${typeof e === "string" ? e : e.message}`; }
          const { dirPath, recursive } = input;
          return await this.files.listFiles(wp, dirPath, recursive);
        }

        case "search_files": {
          let wp: string; try { wp = await this.resolveWorkspace(agentId); } catch (e: any) { return `Error: ${typeof e === "string" ? e : e.message}`; }
          const { pattern, searchPath, include } = input;
          if (!pattern) return "Error: search_files requires pattern.";
          return await this.files.searchFiles(wp, pattern, searchPath, include);
        }

        case "delete_file": {
          let wp: string; try { wp = await this.resolveWorkspace(agentId); } catch (e: any) { return `Error: ${typeof e === "string" ? e : e.message}`; }
          const { filePath } = input;
          if (!filePath) return "Error: delete_file requires filePath.";
          return await this.files.deleteFile(wp, filePath);
        }

        // ── Bash Shell Execution (Effect-based, ported from OpenCode) ─
        case "bash": {
          let wp: string; try { wp = await this.resolveWorkspace(agentId); } catch (e: any) { return `Error: ${typeof e === "string" ? e : e.message}`; }
          try {
            const result = await Effect.runPromise(
              runBashCommand(wp, input).pipe(
                Effect.catchAll((err) => Effect.succeed(`Error: ${err.message}`)),
              ),
            );
            return result;
          } catch (err: any) {
            return `Error executing bash command: ${err.message || err}`;
          }
        }

        // ── Shell Command Execution ─────────────────────────────
        case "run_command": {
          let wp: string; try { wp = await this.resolveWorkspace(agentId); } catch (e: any) { return `Error: ${typeof e === "string" ? e : e.message}`; }          const { command, cwd, timeout } = input;
          if (!command) return "Error: run_command requires a command.";
          try {
            const result = await this.shell.runCommand(wp, {
              command,
              cwd: cwd || undefined,
              timeout: timeout ? parseInt(String(timeout), 10) : undefined,
            });
            return result.output;
          } catch (err: any) {
            return `Error executing command: ${err.message || err}`;
          }
        }

        // ── Glob (file pattern matching) ────────────────────────
        case "glob": {
          let wp: string; try { wp = await this.resolveWorkspace(agentId); } catch (e: any) { return `Error: ${typeof e === "string" ? e : e.message}`; }          const { pattern, cwd, limit } = input;
          if (!pattern) return "Error: glob requires a pattern.";
          return await this.files.globFiles(
            wp,
            pattern,
            cwd || undefined,
            limit ? parseInt(String(limit), 10) : undefined,
          );
        }

        // ── Grep (regex file search, Effect-based) ──────────────
        case "grep": {
          let wp: string; try { wp = await this.resolveWorkspace(agentId); } catch (e: any) { return `Error: ${typeof e === "string" ? e : e.message}`; }          try {
            return await Effect.runPromise(executeGrep(wp, input).pipe(
              Effect.catchAll((err) => Effect.succeed(`Error: ${err.message}`)),
            ));
          } catch (err: any) {
            return `Error: ${err.message}`;
          }
        }

        // ── Apply Patch (structured file editing) ─────────────
        case "apply_patch": {
          let wp: string; try { wp = await this.resolveWorkspace(agentId); } catch (e: any) { return `Error: ${typeof e === "string" ? e : e.message}`; }          try {
            return await Effect.runPromise(executeApplyPatch(wp, input).pipe(
              Effect.catchAll((err) => Effect.succeed(`Error: ${err.message}`)),
            ));
          } catch (err: any) {
            return `Error: ${err.message}`;
          }
        }

        // ── Question (ask user interactively) ──────────────────
        case "question": {
          try {
            return await Effect.runPromise(executeQuestion(agentId, input).pipe(
              Effect.catchAll((err) => Effect.succeed(`Error: ${err.message}`)),
            ));
          } catch (err: any) {
            return `Error: ${err.message}`;
          }
        }

        // ── TodoWrite (task list) ──────────────────────────────
        case "todowrite": {
          try {
            return await Effect.runPromise(executeTodoWrite(agentId, input).pipe(
              Effect.catchAll((err) => Effect.succeed(`Error: ${err.message}`)),
            ));
          } catch (err: any) {
            return `Error: ${err.message}`;
          }
        }

        // ── Fetch URL ───────────────────────────────────────────
        case "fetch_url": {
          const { url, maxChars } = input;
          if (!url) return "Error: fetch_url requires a url.";
          try {
            const result = await this.web.fetchUrl({
              url,
              maxChars: maxChars ? parseInt(String(maxChars), 10) : undefined,
            });
            return `## ${url}\n[HTTP ${result.statusCode} | ${result.contentType}${result.truncated ? " | truncated" : ""}]\n\n${result.content}`;
          } catch (err: any) {
            return `Error fetching URL: ${err.message || err}`;
          }
        }

        // ── Web Search (DuckDuckGo, Effect-based) ──────────────
        case "websearch": {
          try {
            return await Effect.runPromise(executeWebSearch(input).pipe(
              Effect.catchAll((err) => Effect.succeed(`Search error: ${err.message}`)),
            ));
          } catch (err: any) {
            return `Search error: ${err.message}`;
          }
        }

        // ── MCP Tools ──────────────────────────────────────────
        case "mcp_list_tools": {
          try {
            const serverName = input.serverName as string | undefined;
            if (serverName) {
              const tools = await mcpService.listTools(serverName);
              if (tools.length === 0) return `No tools found on MCP server "${serverName}".`;
              return tools.map((t) => `- **${t.name}**: ${t.description}`).join("\n");
            }
            const all = await mcpService.listAllTools();
            if (all.length === 0) return "No MCP servers configured or connected. Configure servers in Settings.";
            const grouped: Record<string, string[]> = {};
            for (const t of all) {
              if (!grouped[t.serverName]) grouped[t.serverName] = [];
              grouped[t.serverName].push(`  - ${t.name}: ${t.description}`);
            }
            return Object.entries(grouped).map(([srv, tools]) => `## ${srv}\n${tools.join("\n")}`).join("\n\n");
          } catch (err: any) {
            return `MCP error: ${err.message}`;
          }
        }

        case "mcp_call": {
          const { serverName, toolName, args } = input;
          if (!serverName || !toolName) return "Error: mcp_call requires serverName and toolName.";
          try {
            return await mcpService.callTool(serverName, toolName, args || {});
          } catch (err: any) {
            return `MCP error: ${err.message}`;
          }
        }

        case "mcp_configure": {
          const { name, transport, command, args, cwd, url, enabled } = input;
          if (!name || !transport) return "Error: mcp_configure requires name and transport.";
          mcpService.setConfig({
            name,
            transport: transport as "stdio" | "http",
            command: command as string | undefined,
            args: args as string[] | undefined,
            cwd: cwd as string | undefined,
            url: url as string | undefined,
            enabled: enabled !== "false",
          });
          return `MCP server "${name}" configured (${transport}). Use mcp_list_tools to discover its tools.`;
        }

        // ── Move / Rename File ──────────────────────────────────
        case "move_file": {
          let wp: string; try { wp = await this.resolveWorkspace(agentId); } catch (e: any) { return `Error: ${typeof e === "string" ? e : e.message}`; }          const { source, destination, overwrite } = input;
          if (!source || !destination) return "Error: move_file requires source and destination.";
          return await this.files.moveFile(
            wp,
            source,
            destination,
            overwrite === true,
          );
        }

        // ── Create Directory ────────────────────────────────────
        case "create_directory": {
          let wp: string; try { wp = await this.resolveWorkspace(agentId); } catch (e: any) { return `Error: ${typeof e === "string" ? e : e.message}`; }          const { path: dirPath } = input;
          if (!dirPath) return "Error: create_directory requires a path.";
          return await this.files.createDirectory(wp, dirPath);
        }

        // ── Delete Directory ────────────────────────────────────
        case "delete_directory": {
          let wp: string; try { wp = await this.resolveWorkspace(agentId); } catch (e: any) { return `Error: ${typeof e === "string" ? e : e.message}`; }          const { path: dirPath } = input;
          if (!dirPath) return "Error: delete_directory requires a path.";
          return await this.files.deleteDirectory(wp, dirPath);
        }

        // ── Binding: Skills & MCP ──────────────────────────────
        // Authorization: caller must be self, superior (parent), or HR.
        // Following OpenCode/OpenClaw: per-agent binding config, runtime injection.

        case "list_available_skills": {
          const { search } = input;
          try {
            const result = await clawhubService.listSkills({
              query: search || undefined,
              limit: 15,
            });
            if (result.items.length === 0) {
              return search
                ? `No skills found matching "${search}". Try a broader search or omit the search term.`
                : "No skills currently available in the registry.";
            }
            const lines = result.items.map(s => {
              const installs = s.stats?.installsAllTime || 0;
              const version = s.latestVersion?.version || "?";
              return `- **${s.displayName}** (\`${s.slug}\`) v${version} — ${s.summary.slice(0, 120)}${s.summary.length > 120 ? "..." : ""} [${installs} installs]`;
            });
            const header = search
              ? `## Skills matching "${search}" (${result.items.length} results)`
              : `## Available Skills (${result.items.length} results)`;
            return `${header}\n${lines.join("\n")}\n\nTo bind a skill, use \`bind_skill\` with the slug as skillName. Source: ClawHub (clawhub.ai)`;
          } catch (err: any) {
            return `Error querying ClawHub registry: ${err.message || "Unknown error"}. Falling back to built-in skills:\n${BINDING_REGISTRY.skills.map(s => `- **${s.name}**: ${s.description}`).join("\n")}`;
          }
        }

        case "get_skill_detail": {
          const { slug } = input;
          if (!slug) return "Error: get_skill_detail requires a slug parameter.";
          try {
            const detail = await clawhubService.getSkillDetail(slug);
            if (!detail) {
              return `Skill "${slug}" not found in the ClawHub registry. Check the slug with list_available_skills.`;
            }
            const version = detail.latestVersion?.version || "?";
            const installs = detail.stats?.installsAllTime || 0;
            const os = detail.metadata?.os?.join(", ") || "all";
            const setupKeys = detail.metadata?.setup?.map((s: any) => s.key).join(", ") || "none";
            const owner = detail.owner?.handle || "unknown";
            // Return a structured summary + the full SKILL.md content
            return [
              `## ${detail.displayName} (\`${detail.slug}\`)`,
              `**Version:** ${version} | **Installs:** ${installs} | **OS:** ${os} | **Author:** ${owner}`,
              `**Setup keys required:** ${setupKeys}`,
              `**Summary:** ${detail.summary}`,
              ``,
              `### SKILL.md Content`,
              `\`\`\`markdown`,
              detail.skillMd?.slice(0, 8000) || "(empty)",
              detail.skillMd && detail.skillMd.length > 8000 ? "\n... (truncated, full content will be injected at runtime)" : "",
              `\`\`\``,
              ``,
              `To bind this skill, use: bind_skill(agentId, "${slug}")`,
            ].join("\n");
          } catch (err: any) {
            return `Error fetching skill detail: ${err.message || "Unknown error"}`;
          }
        }

        case "read_skill": {
          const { slug } = input;
          if (!slug) return "Error: read_skill requires a slug parameter.";

          // Phase 2 of progressive disclosure: only load skills that are actually bound
          const caller = await this.org.getAgent(agentId);
          if (!caller) return "Error: Could not verify your identity.";
          const boundSkills: string[] = JSON.parse(caller.boundSkills || "[]");
          if (!boundSkills.includes(slug)) {
            return `Error: Skill "${slug}" is not bound to you. Your active skills: [${boundSkills.join(", ") || "none"}]. Use bind_skill first, or use get_skill_detail to preview unbound skills.`;
          }

          try {
            const detail = await clawhubService.getSkillDetail(slug);
            if (!detail) {
              return `Skill "${slug}" not found in the ClawHub registry (it may have been removed). Consider unbinding it and finding an alternative.`;
            }
            // Return the full SKILL.md as working instructions — no metadata wrapping
            const header = `# ${detail.displayName} (${detail.slug}) — Full Instructions`;
            const content = detail.skillMd || "(This skill has no SKILL.md content.)";
            return `${header}\n\n${content}`;
          } catch (err: any) {
            return `Error loading skill instructions: ${err.message || "Unknown error"}. The skill is still bound — try again later.`;
          }
        }

        case "list_available_mcp": {
          const servers = BINDING_REGISTRY.mcpServers;
          if (servers.length === 0) return "No MCP servers currently registered in the system.";
          const lines = servers.map(s => `- **${s.name}**: ${s.description} [${s.type}]`);
          return `## Available MCP Servers (${servers.length})\n${lines.join("\n")}\n\nUse bind_mcp to connect an MCP server to an agent.`;
        }

        case "bind_skill": {
          const targetAgentId = typeof input.agentId === "string" ? input.agentId.trim() : input.agentId;
          const { skillName } = input;
          if (!targetAgentId || !skillName) {
            return "Error: bind_skill requires agentId and skillName.";
          }
          const target = await this.org.resolveAgent(targetAgentId);
          if (!target) return `Error: No agent found with ID ${targetAgentId}.`;

          // Authorization check
          const authError = await this.checkBindingAuth(agentId, target.id);
          if (authError) return authError;

          // Read current boundSkills, add if not present
          const current: string[] = JSON.parse(target.boundSkills || "[]");
          if (current.includes(skillName)) {
            return `Skill "${skillName}" is already bound to "${target.name}".`;
          }
          current.push(skillName);
          await this.org.updateAgent(target.id, { boundSkills: JSON.stringify(current) });
          return `Skill "${skillName}" bound to "${target.name}" (${target.shortId || target.id.slice(0, 8)}). Current skills: [${current.join(", ")}]`;
        }

        case "unbind_skill": {
          const targetAgentId = typeof input.agentId === "string" ? input.agentId.trim() : input.agentId;
          const { skillName } = input;
          if (!targetAgentId || !skillName) {
            return "Error: unbind_skill requires agentId and skillName.";
          }
          const target = await this.org.resolveAgent(targetAgentId);
          if (!target) return `Error: No agent found with ID ${targetAgentId}.`;

          const authError = await this.checkBindingAuth(agentId, target.id);
          if (authError) return authError;

          const current: string[] = JSON.parse(target.boundSkills || "[]");
          const idx = current.indexOf(skillName);
          if (idx === -1) {
            return `Skill "${skillName}" is not currently bound to "${target.name}".`;
          }
          current.splice(idx, 1);
          await this.org.updateAgent(target.id, { boundSkills: JSON.stringify(current) });
          return `Skill "${skillName}" unbound from "${target.name}" (${target.shortId || target.id.slice(0, 8)}). Remaining skills: [${current.join(", ")}]`;
        }

        case "bind_mcp": {
          const targetAgentId = typeof input.agentId === "string" ? input.agentId.trim() : input.agentId;
          const { mcpServer } = input;
          if (!targetAgentId || !mcpServer) {
            return "Error: bind_mcp requires agentId and mcpServer.";
          }
          const target = await this.org.resolveAgent(targetAgentId);
          if (!target) return `Error: No agent found with ID ${targetAgentId}.`;

          const authError = await this.checkBindingAuth(agentId, target.id);
          if (authError) return authError;

          const current: string[] = JSON.parse(target.mcpServers || "[]");
          if (current.includes(mcpServer)) {
            return `MCP server "${mcpServer}" is already bound to "${target.name}".`;
          }
          current.push(mcpServer);
          await this.org.updateAgent(target.id, { mcpServers: JSON.stringify(current) });
          return `MCP server "${mcpServer}" bound to "${target.name}" (${target.shortId || target.id.slice(0, 8)}). Current MCP servers: [${current.join(", ")}]`;
        }

        case "unbind_mcp": {
          const targetAgentId = typeof input.agentId === "string" ? input.agentId.trim() : input.agentId;
          const { mcpServer } = input;
          if (!targetAgentId || !mcpServer) {
            return "Error: unbind_mcp requires agentId and mcpServer.";
          }
          const target = await this.org.resolveAgent(targetAgentId);
          if (!target) return `Error: No agent found with ID ${targetAgentId}.`;

          const authError = await this.checkBindingAuth(agentId, target.id);
          if (authError) return authError;

          const current: string[] = JSON.parse(target.mcpServers || "[]");
          const idx = current.indexOf(mcpServer);
          if (idx === -1) {
            return `MCP server "${mcpServer}" is not currently bound to "${target.name}".`;
          }
          current.splice(idx, 1);
          await this.org.updateAgent(target.id, { mcpServers: JSON.stringify(current) });
          return `MCP server "${mcpServer}" unbound from "${target.name}" (${target.shortId || target.id.slice(0, 8)}). Remaining MCP servers: [${current.join(", ")}]`;
        }


        case "get_project_time": {
          if (!this.gameTimeService || !this.projectId) {
            return "Error: project time service unavailable.";
          }
          const snap = this.gameTimeService.getSnapshot(this.projectId);
          return "Current project time: " + snap.formatted + " (day " + snap.day + ", " + snap.gameSeconds + " project-seconds)";
        }

        case "get_real_time": {
          const snap = this.getTimeSnapshot();
          const real = snap?.realFormatted ?? new Date().toISOString();
          return "Current real-world time: " + real + ". Use this for news, web, and external calendar queries.";
        }

        case "set_alarm": {
          const { purpose, targetAgentId, dueInGameDays, dueInGameHours, dueInGameMinutes, dueInGameSeconds } = input;
          if (!purpose) return "Error: set_alarm requires purpose.";
          if (!this.alarmService || !this.gameTimeService || !this.projectId) {
            return "Error: alarm service unavailable.";
          }
          const offset = parseGameTimeOffset({ dueInGameDays, dueInGameHours, dueInGameMinutes, dueInGameSeconds });
          if (offset <= 0) {
            return "Error: specify a positive due time (dueInGameDays/Hours/Minutes/Seconds).";
          }
          let toId = agentId;
          let toName = "yourself";
          if (targetAgentId) {
            const target = await this.org.resolveAgent(String(targetAgentId).trim());
            if (!target) return 'Error: No agent found matching "' + targetAgentId + '".';
            toId = target.id;
            toName = target.name || toId;
          }
          const current = this.gameTimeService.getCurrentGameSeconds(this.projectId);
          const fireAt = current + offset;
          const alarmId = await this.alarmService.schedule({
            projectId: this.projectId,
            fromAgentId: agentId,
            toAgentId: toId,
            purpose: String(purpose),
            fireAtGameSeconds: fireAt,
          });
          const fireLabel = formatGameTime(fireAt);
          return "Alarm set (id: " + alarmId.slice(0, 8) + "). Recipient: " + toName + ". Fires at project time " + fireLabel + ". Purpose: " + purpose;
        }

        // ── Review Tools ──────────────────────────────────────
        // Called by the Reviewer agent. Each tool reads code,
        // sends it to an LLM for analysis, and returns only results.
        // The Reviewer agent does NOT see raw code in its context.

        case "run_code_review": {
          if (!this.reviewLLM) return "Error: Review LLM not configured.";
          const { filePaths, files } = input;
          const resolvedFiles = resolveFileList(filePaths, files);
          if (resolvedFiles.length === 0) return "Error: run_code_review requires filePaths (array) or files (comma-separated).";
          let wp: string; try { wp = await this.resolveWorkspace(agentId); } catch (e: any) { return `Error: ${typeof e === "string" ? e : e.message}`; }          const result = await runCodeReview(wp, resolvedFiles, this.reviewLLM);
          return formatReviewResult("Code Review", result);
        }

        case "run_security_audit": {
          if (!this.reviewLLM) return "Error: Review LLM not configured.";
          const { filePaths, files } = input;
          const resolvedFiles = resolveFileList(filePaths, files);
          if (resolvedFiles.length === 0) return "Error: run_security_audit requires filePaths (array) or files (comma-separated).";
          let wp: string; try { wp = await this.resolveWorkspace(agentId); } catch (e: any) { return `Error: ${typeof e === "string" ? e : e.message}`; }          const result = await runSecurityAudit(wp, resolvedFiles, this.reviewLLM);
          return formatReviewResult("Security Audit", result);
        }

        case "run_tests": {
          if (!this.reviewLLM) return "Error: Review LLM not configured.";
          const { filePaths, files, testFiles: inputTestFiles } = input;
          const resolvedFiles = resolveFileList(filePaths, files);
          const resolvedTestFiles = resolveFileList(inputTestFiles, input.testFiles);
          if (resolvedFiles.length === 0) return "Error: run_tests requires filePaths (source files).";
          let wp: string; try { wp = await this.resolveWorkspace(agentId); } catch (e: any) { return `Error: ${typeof e === "string" ? e : e.message}`; }          const result = await runTestReview(wp, resolvedFiles, resolvedTestFiles, this.reviewLLM);
          return formatReviewResult("Test Review", result);
        }

        case "run_perf_audit": {
          if (!this.reviewLLM) return "Error: Review LLM not configured.";
          const { filePaths, files } = input;
          const resolvedFiles = resolveFileList(filePaths, files);
          if (resolvedFiles.length === 0) return "Error: run_perf_audit requires filePaths (array) or files (comma-separated).";
          let wp: string; try { wp = await this.resolveWorkspace(agentId); } catch (e: any) { return `Error: ${typeof e === "string" ? e : e.message}`; }          const result = await runPerfAudit(wp, resolvedFiles, this.reviewLLM);
          return formatReviewResult("Performance Audit", result);
        }

        case "run_full_review": {
          if (!this.reviewLLM) return "Error: Review LLM not configured.";
          const { filePaths, files, testFiles: inputTestFiles } = input;
          const resolvedFiles = resolveFileList(filePaths, files);
          const resolvedTestFiles = resolveFileList(inputTestFiles, input.testFiles);
          if (resolvedFiles.length === 0) return "Error: run_full_review requires filePaths.";
          let wp: string; try { wp = await this.resolveWorkspace(agentId); } catch (e: any) { return `Error: ${typeof e === "string" ? e : e.message}`; }          const combined = await runFullReview(wp, resolvedFiles, resolvedTestFiles, this.reviewLLM);
          const sections = [
            formatReviewResult("1. Code Review", combined.codeReview),
            formatReviewResult("2. Security Audit", combined.securityAudit),
            formatReviewResult("3. Test Review", combined.testReview),
            formatReviewResult("4. Performance Audit", combined.perfAudit),
            `## Overall\n- Score: **${combined.overallScore}/100**\n- Verdict: ${combined.overallPassed ? "✅ PASS" : "❌ FAIL"}`,
          ];
          return sections.join("\n\n---\n\n");
        }

        // ── Git Worktree tools (coordinator only) ─────────────
        // Managers/CEO use these to give subordinates isolated workspaces.

        case "git_worktree_create": {
          const coordCheck = await assertCoordinator(this.org, agentId);
          if (!coordCheck.ok) return coordCheck.error;
          const { subordinateId, taskName, baseBranch } = input;
          if (!subordinateId || !taskName) return "Error: git_worktree_create requires subordinateId and taskName.";
          const sub = await this.org.resolveAgent(String(subordinateId).trim());
          if (!sub) return `Error: No agent found with ID "${subordinateId}".`;
          let wp: string; try { wp = await this.resolveMainWorkspace(agentId); } catch (e: any) { return `Error: ${typeof e === "string" ? e : e.message}`; }
          const git = new GitWorktreeService(wp);
          const shortId = sub.shortId || sub.id.slice(0, 8);
          try {
            const { path, branch } = await git.createWorktree(shortId, String(taskName), baseBranch || undefined);
            return `Worktree created for **${sub.name}** (${shortId}).\n- Branch: \`${branch}\`\n- Path: \`${path}\`\n\nTheir file operations (bash, write_file, edit_file) will operate in this isolated workspace. Use \`git_worktree_checkpoint\` to snapshot progress.`;
          } catch (err: any) {
            return `Error: ${err.message}`;
          }
        }

        case "git_worktree_checkpoint": {
          const coordCheck = await assertCoordinator(this.org, agentId);
          if (!coordCheck.ok) return coordCheck.error;
          const { subordinateId, message } = input;
          if (!subordinateId) return "Error: git_worktree_checkpoint requires subordinateId.";
          const sub = await this.org.resolveAgent(String(subordinateId).trim());
          if (!sub) return `Error: No agent found with ID "${subordinateId}".`;
          let wp: string; try { wp = await this.resolveMainWorkspace(agentId); } catch (e: any) { return `Error: ${typeof e === "string" ? e : e.message}`; }
          const git = new GitWorktreeService(wp);
          const shortId = sub.shortId || sub.id.slice(0, 8);
          try {
            const result = await git.checkpoint(shortId, String(message || "manual checkpoint"));
            if (result.count === 0) {
              return `No changes to checkpoint for **${sub.name}** (${shortId}). HEAD: \`${result.hash}\``;
            }
            return `Checkpoint created for **${sub.name}** (${shortId}).\n- Commit: \`${result.hash}\`\n- Total checkpoints (7d): ${result.count}`;
          } catch (err: any) {
            return `Error: ${err.message}`;
          }
        }

        case "git_worktree_merge": {
          const coordCheck = await assertCoordinator(this.org, agentId);
          if (!coordCheck.ok) return coordCheck.error;
          const { subordinateId, taskName, baseBranch } = input;
          if (!subordinateId || !taskName) return "Error: git_worktree_merge requires subordinateId and taskName.";
          const sub = await this.org.resolveAgent(String(subordinateId).trim());
          if (!sub) return `Error: No agent found with ID "${subordinateId}".`;
          let wp: string; try { wp = await this.resolveMainWorkspace(agentId); } catch (e: any) { return `Error: ${typeof e === "string" ? e : e.message}`; }
          const git = new GitWorktreeService(wp);
          const shortId = sub.shortId || sub.id.slice(0, 8);
          try {
            const { merged, hash } = await git.merge(shortId, String(taskName), baseBranch || undefined);
            return `Worktree merged for **${sub.name}** (${shortId}).\n- Merged into main at \`${hash}\`\n- Worktree removed, branch deleted.\n\nThe subordinate's work is now integrated into the main workspace.`;
          } catch (err: any) {
            return `Merge failed: ${err.message}`;
          }
        }

        case "git_worktree_rollback": {
          const coordCheck = await assertCoordinator(this.org, agentId);
          if (!coordCheck.ok) return coordCheck.error;
          const { subordinateId, commitHash } = input;
          if (!subordinateId) return "Error: git_worktree_rollback requires subordinateId.";
          const sub = await this.org.resolveAgent(String(subordinateId).trim());
          if (!sub) return `Error: No agent found with ID "${subordinateId}".`;
          let wp: string; try { wp = await this.resolveMainWorkspace(agentId); } catch (e: any) { return `Error: ${typeof e === "string" ? e : e.message}`; }
          const git = new GitWorktreeService(wp);
          const shortId = sub.shortId || sub.id.slice(0, 8);
          try {
            const { hash, message } = await git.rollback(shortId, commitHash || undefined);
            return `Rollback complete for **${sub.name}** (${shortId}).\n- HEAD now at \`${hash}\`\n- Message: "${message}"\n\nThe subordinate can now rework from this checkpoint.`;
          } catch (err: any) {
            return `Rollback failed: ${err.message}`;
          }
        }

        case "git_worktree_remove": {
          const coordCheck = await assertCoordinator(this.org, agentId);
          if (!coordCheck.ok) return coordCheck.error;
          const { subordinateId, taskName } = input;
          if (!subordinateId) return "Error: git_worktree_remove requires subordinateId.";
          const sub = await this.org.resolveAgent(String(subordinateId).trim());
          if (!sub) return `Error: No agent found with ID "${subordinateId}".`;
          let wp: string; try { wp = await this.resolveMainWorkspace(agentId); } catch (e: any) { return `Error: ${typeof e === "string" ? e : e.message}`; }
          const git = new GitWorktreeService(wp);
          const shortId = sub.shortId || sub.id.slice(0, 8);
          try {
            await git.removeWorktree(shortId, taskName || undefined);
            return `Worktree removed for **${sub.name}** (${shortId}). Branch deleted.`;
          } catch (err: any) {
            return `Error: ${err.message}`;
          }
        }

        case "git_worktree_list": {
          let wp: string; try { wp = await this.resolveMainWorkspace(agentId); } catch (e: any) { return `Error: ${typeof e === "string" ? e : e.message}`; }
          const git = new GitWorktreeService(wp);
          try {
            const entries = await git.listWorktrees();
            if (entries.length === 0) return "No HiveWeave-managed worktrees in this project.";
            const lines = entries.map((e) => {
              const status = e.active ? "🟢" : "🔴";
              return `${status} **${e.shortId}** — \`${e.branch}\` @ \`${e.head}\``;
            });
            return `## Worktrees (${entries.length})\n${lines.join("\n")}`;
          } catch (err: any) {
            return `Error: ${err.message}`;
          }
        }

        case "git_worktree_status": {
          const { subordinateId } = input;
          if (!subordinateId) return "Error: git_worktree_status requires subordinateId.";
          const sub = await this.org.resolveAgent(String(subordinateId).trim());
          if (!sub) return `Error: No agent found with ID "${subordinateId}".`;
          let wp: string; try { wp = await this.resolveMainWorkspace(agentId); } catch (e: any) { return `Error: ${typeof e === "string" ? e : e.message}`; }
          const git = new GitWorktreeService(wp);
          const shortId = sub.shortId || sub.id.slice(0, 8);
          try {
            const status = await git.getStatus(shortId);
            if (!status) return `No worktree found for **${sub.name}** (${shortId}). Use \`git_worktree_create\` to allocate one.`;
            const dirty = status.hasUncommitted ? " ⚠️ (uncommitted changes)" : "";
            const cpLines = status.checkpoints.length > 0
              ? status.checkpoints.map((c) => `  - \`${c.hash}\` ${c.date ? `[${c.date}] ` : ""}${c.message}`).join("\n")
              : "  (no checkpoints yet)";
            return [
              `## Worktree Status — **${sub.name}** (${shortId})`,
              `- Branch: \`${status.branch}\` @ \`${status.head}\`${dirty}`,
              `- Active: ${status.active ? "✅" : "❌"}`,
              `### Checkpoints`,
              cpLines,
            ].join("\n");
          } catch (err: any) {
            return `Error: ${err.message}`;
          }
        }

        default:
          return `Unknown tool: ${name}`;
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      console.error(`[TOOL] ${name} failed:`, err);
      return `Tool ${name} failed: ${msg}`;
    }
  }

  /**
   * Resolve the effective workspace path for an agent.
   * If the agent has a git worktree, operations route there instead of main.
   * Returns the path directly, or throws with a user-facing error string.
   */
  private async resolveWorkspace(agentId: string): Promise<string> {
    const caller = await this.org.getAgent(agentId);
    if (!caller?.projectId) throw "Could not determine your project.";
    const project = await this.projects.getProject(caller.projectId);
    if (!project?.workspacePath) throw "No workspace configured for this project. Ask the user to set a workspace path first.";
    const shortId = caller.shortId || caller.id.slice(0, 8);
    const wtPath = `${project.workspacePath}/.hiveweave/worktrees/${shortId}`;
    if (existsSync(wtPath)) return wtPath;
    return project.workspacePath;
  }

  /** Shorthand: resolveWorkspace wrapped in try/catch, returning error string on failure. */
  private async workspaceOrError(agentId: string): Promise<string | null> {
    try { return await this.resolveWorkspace(agentId); }
    catch (e: any) { return `Error: ${typeof e === "string" ? e : e.message}`; }
  }

  /** Always resolve the MAIN workspace (skips worktree lookup — for git management operations). */
  private async resolveMainWorkspace(agentId: string): Promise<string> {
    const caller = await this.org.getAgent(agentId);
    if (!caller?.projectId) throw "Could not determine your project.";
    const project = await this.projects.getProject(caller.projectId);
    if (!project?.workspacePath) throw "No workspace configured for this project.";
    return project.workspacePath;
  }

  /**
   * Check if the caller is authorized to modify bindings on the target agent.
   * Authorization rules (OpenCode/OpenClaw-inspired):
   *   - Self-binding: caller === target → allowed
   *   - Superior: caller is the direct parent of target → allowed
   *   - HR: caller has role "hr" → allowed (service role, global authority)
   *
   * @returns Error string if unauthorized, or null if authorized.
   */
  private async checkBindingAuth(callerId: string, targetId: string): Promise<string | null> {
    // Self-binding always allowed
    if (callerId === targetId) return null;

    const caller = await this.org.getAgent(callerId);
    if (!caller) return "Error: Could not verify your identity.";

    // HR has global binding authority
    if (caller.role?.toLowerCase() === "hr") return null;

    // Superior (direct parent) can bind subordinates
    const target = await this.org.getAgent(targetId);
    if (target?.parentId === callerId) return null;

    return `Error: You are not authorized to modify bindings for this agent. Only the agent itself, its direct superior, or HR can manage bindings.`;
  }
}

// ---------------------------------------------------------------------------
// Review tool helpers (used by ToolExecutor review cases)
// ---------------------------------------------------------------------------

/** Check that the caller has coordinator permission (CEO/HR/manager). */
async function assertCoordinator(
  org: OrgService,
  agentId: string,
): Promise<{ ok: true; caller: any } | { ok: false; error: string }> {
  const caller = await org.getAgent(agentId);
  if (!caller) return { ok: false, error: "Error: Could not find your own agent record." };
  if (caller.permissionType === "coordinator") return { ok: true, caller };
  const role = String(caller.role || "").toLowerCase();
  if (role === "ceo" || role === "hr") return { ok: true, caller };
  return { ok: false, error: "Error: Git worktree tools require coordinator permission. Your role does not have this authority." };
}

/** Resolve filePaths from either an array or a comma-separated string. */
function resolveFileList(arrayInput: unknown, stringInput: unknown): string[] {
  if (Array.isArray(arrayInput)) return arrayInput.map((s) => String(s).trim()).filter(Boolean);
  if (typeof stringInput === "string") return stringInput.split(",").map((s) => s.trim()).filter(Boolean);
  return [];
}

/** Format a ReviewResult into a readable markdown report string. */
function formatReviewResult(label: string, result: ReviewResult): string {
  const lines: string[] = [];
  const verdict = result.passed ? "✅ PASS" : "❌ FAIL";
  const score = result.score !== undefined ? ` | Score: **${result.score}/100**` : "";
  lines.push(`## ${label}\n**${verdict}**${score}\n`);
  lines.push(result.summary);

  if (result.issues.length > 0) {
    lines.push(`\n### Issues (${result.issues.length})`);
    for (const issue of result.issues) {
      const icon = issue.severity === "critical" ? "🔴" : issue.severity === "major" ? "🟠" : issue.severity === "minor" ? "🟡" : "ℹ️";
      const location = issue.file ? `\`${issue.file}${issue.line ? `:${issue.line}` : ""}\`` : "";
      lines.push(`\n${icon} **${issue.title}** [${issue.severity}] ${location}`);
      lines.push(`  ${issue.description}`);
      if (issue.suggestion) lines.push(`  → ${issue.suggestion}`);
    }
  }

  return lines.join("\n");
}
