import { DispatchService } from "./dispatch-service.js";
import { MemoryService } from "./memory-service.js";
import { OrgService } from "./org-service.js";
import { HandoffService } from "./handoff-service.js";
import { InboxService } from "./inbox-service.js";
import { RosterService } from "./roster-service.js";
import { FileService } from "./file-service.js";
import { ProjectService } from "./project-service.js";
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
import { randomUUID } from "crypto";
import { runBashCommand } from "./tools/bash.js";
import { executeGrep } from "./tools/grep.js";
import { executeApplyPatch } from "./tools/apply-patch.js";
import { executeQuestion } from "./tools/question.js";
import { executeTodoWrite } from "./tools/todowrite.js";
import { executeWebSearch } from "./tools/websearch.js";
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
  "browse_templates",
  "create_from_template",
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
          // Format logs concisely for Claude
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
          // For now, read subordinate's recent logs as a code review proxy
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
          const { message } = input;
          if (!message) {
            return "Error: message_superior requires a message.";
          }
          // Find the current agent's parent (superior)
          const currentAgent = await this.org.getAgent(agentId);
          if (!currentAgent?.parentId) {
            return "You don't have a superior to message. You are a root agent.";
          }
          const superiorId = currentAgent.parentId;
          const msgId = await this.inbox.sendMessage(agentId, superiorId, this.prefixForInbox(message), "superior");
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
        case "message_peer": {
          const { message, expectReport } = input;
          const toAgentId = typeof input.toAgentId === "string" ? input.toAgentId.trim() : input.toAgentId;
          if (!toAgentId || !message) {
            return "Error: message_peer requires toAgentId and message.";
          }
          console.log(`[TOOL] message_peer: from=${agentId} to=${toAgentId}`);
          const target = await this.org.resolveAgent(toAgentId);
          if (!target) {
            console.log(`[TOOL] message_peer: ERROR - No agent found with ID ${toAgentId}`);
            return `Error: No agent found with ID ${toAgentId}.`;
          }
          const resolvedToId = target.id;
          const targetName = target.name || toAgentId;
          const msgId = await this.inbox.sendMessage(agentId, resolvedToId, message, "peer", expectReport === true);
          // Track this as a peer communication for the org chart
          communicationService.addCommunication(agentId, resolvedToId, "peer");
          if (this.teamChat) {
            await this.teamChat.recordIncoming(resolvedToId, agentId, message, msgId);
            await this.teamChat.recordOutgoing(agentId, resolvedToId, message, JSON.stringify([{ tool: name, input }]));
          }
          return `Message sent to peer ${targetName} (${target.shortId}). msgId=${msgId}`;
        }

        // ── General Send Message (to user or agent) ──────────
        case "send_message": {
          const { content, recipients } = input;
          if (!content || !recipients) {
            return "Error: send_message requires content and recipients.";
          }
          const list = String(recipients).split(",").map((s: string) => s.trim()).filter(Boolean);
          const results: string[] = [];
          for (const rcpt of list) {
            if (rcpt.toLowerCase() === "user") {
              // Direct message to the human operator — visible in this agent's chat
              if (this.teamChat) {
                const msgId = randomUUID();
                try {
                  // Use team chat with null teamToAgentId for user-bound messages
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
                results.push(`Error: Could not find agent matching "${rcpt}". Use read_roster to see available agents.`);
                continue;
              }
              const msgId = await this.inbox.sendMessage(agentId, target.id, this.prefixForInbox(content), "peer");
              communicationService.addCommunication(agentId, target.id, "peer");
              if (this.teamChat) {
                await this.teamChat.recordIncoming(target.id, agentId, content, msgId);
                await this.teamChat.recordOutgoing(agentId, target.id, content, JSON.stringify([{ tool: name, input }]));
              }
              results.push(`Sent to ${target.name} (${target.shortId}).`);
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
              return `- **${child.name}** (${child.role}, ID: ${child.shortId || child.id}) — Status: ${child.status} | ${taskStatus} | Subordinates: ${subCount} | Last: ${lastActivity}`;
            }),
          );
          return `## Your Subordinates (${children.length})\n${lines.join("\n")}`;
        }

        // ── HR: Create Agent (hire/recruit) ─────────────────
        case "create_agent": {
          {
            const roleCheck = await assertRole(this.org, agentId, ["hr"]);
            if (!roleCheck.ok) return roleCheck.error;
          }
          const { name, role, goal, backstory, permissionType, position, department, responsibilities } = input;
          const parentId = typeof input.parentId === "string" ? input.parentId.trim() : input.parentId;
          if (!name || !role || !goal) {
            return "Error: create_agent requires name, role, and goal.";
          }
          const normalizedRole = String(role).toLowerCase();
          if (normalizedRole === "ceo" || normalizedRole === "hr") {
            return "Error: Cannot create agents with role ceo or hr. These roles are reserved.";
          }

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
          let parentShortId: string | null = null;
          if (parentId) {
            const parentAgent = await this.org.resolveAgent(parentId);
            if (!parentAgent) {
              return `Error: No agent found with ID ${parentId}.`;
            }
            if (projectId && parentAgent.projectId !== projectId) {
              return `Error: Parent agent belongs to a different project.`;
            }
            resolvedParentId = parentAgent.id;
            parentShortId = parentAgent.shortId || null;
          } else if (projectId) {
            const ceo = await this.org.findAgentByRole(projectId, "ceo");
            if (ceo) {
              resolvedParentId = ceo.id;
              parentShortId = ceo.shortId || null;
            }
          }

          const permType = permissionType === "coordinator" ? "coordinator" : "executor";

          // IRON RULE: HR can NEVER create agents under itself
          if (caller.role?.toLowerCase() === "hr" && resolvedParentId === agentId) {
            console.log(`[TOOL] create_agent: BLOCKED — HR tried to create agent under itself`);
            return "Error: HR cannot create agents under itself. You are a personnel service role, not an org manager. Set parentId to null (root-level) or to another agent's ID.";
          }

          console.log(`[TOOL] create_agent: name="${name}" parentId=${resolvedParentId} callerId=${agentId} skills=${JSON.stringify(initialSkills)} mcp=${JSON.stringify(initialMcp)}`);
          const newId = await this.org.createAgent({
            name,
            role,
            goal,
            backstory: backstory || "",
            skills: [],
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
            ? `under parent agent ${parentShortId || resolvedParentId.slice(0, 8)}`
            : "as root-level agent";
          const bindingNote = [
            initialSkills.length > 0 ? `Skills: [${initialSkills.join(", ")}]` : null,
            initialMcp.length > 0 ? `MCP: [${initialMcp.join(", ")}]` : null,
          ].filter(Boolean).join(" | ");
          return `Agent created successfully!\nName: ${name}\nID: ${newShortId}\nRole: ${role}\nType: ${permLabel}\nPlacement: ${parentLabel}${bindingNote ? `\n${bindingNote}` : ""}\nRoster entry created.${skillWarning}`;
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
          return `Agent "${target.name}" (${target.shortId || targetAgentId}) transferred ${parentLabel}.`;
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
          return `Agent "${target.name}" (${target.shortId || resolvedTargetId.slice(0, 8)}) has been dismissed (archived). Roster record terminated.${reasonNote}`;
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
            return `${statusBadge} **${r.position || "(no position)"}** — Agent: ${displayId} | Dept: ${r.department || "—"} | Status: ${r.status}\n   Responsibilities: ${r.responsibilities || "—"}`;
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
              flatList.push(`${indent}${permBadge} **${node.name}** (role: ${node.role}, status: ${node.status}, ID: ${node.shortId || node.id.slice(0, 8)})`);
              if (node.children && node.children.length > 0) {
                flatten(node.children, depth + 1);
              }
            }
          };
          flatten(Array.isArray(tree) ? tree : [tree]);

          if (flatList.length === 0) return "No agents in the organization.";
          return `## Full Organization (${flatList.length} agents)\n${flatList.join("\n")}`;
        }

        // ── HR: Browse Templates ─────────────────────────────
        case "browse_templates": {
          {
            const roleCheck = await assertRole(this.org, agentId, ["hr"]);
            if (!roleCheck.ok) return roleCheck.error;
          }
          const { division, search, role } = input;
          const templates = await this.templates.listTemplates({
            division: division || undefined,
            search: search || undefined,
            role: role || undefined,
          });
          if (templates.length === 0) {
            return "No templates found matching your criteria. Try a broader search or omit filters.";
          }
          const lines = templates.map((t: any) => {
            const emoji = t.emoji || "📋";
            return `${emoji} **${t.name}** [${t.division}] — role: ${t.role} | ${t.vibe || t.description.slice(0, 60) || "No description"}\n   ID: ${t.id}`;
          });
          return `## Agent Templates (${templates.length} results)\n${lines.join("\n")}\n\nTo create an agent from a template, use create_from_template with the template ID.`;
        }

        // ── HR: Create Agent from Template ───────────────────
        case "create_from_template": {
          {
            const roleCheck = await assertRole(this.org, agentId, ["hr"]);
            if (!roleCheck.ok) return roleCheck.error;
          }
          const { templateId, parentId, position, department } = input;
          let { name, permissionType } = input;
          const trimmedParentId = typeof parentId === "string" ? parentId.trim() : parentId;
          if (!templateId) {
            return "Error: create_from_template requires templateId. Use browse_templates first to find a template.";
          }

          // Parse comma-separated skill and MCP server lists
          const initialSkills = typeof input.skills === "string" && input.skills.trim()
            ? input.skills.split(",").map((s: string) => s.trim()).filter(Boolean)
            : [];
          const initialMcp = typeof input.mcpServers === "string" && input.mcpServers.trim()
            ? input.mcpServers.split(",").map((s: string) => s.trim()).filter(Boolean)
            : [];

          // Load the template
          const template = await this.templates.getTemplate(templateId);
          if (!template) {
            return `Error: No template found with ID ${templateId}.`;
          }

          // Use template defaults for unspecified fields
          const agentName = name || template.name;
          const agentRole = template.role;
          if (String(agentRole).toLowerCase() === "ceo" || String(agentRole).toLowerCase() === "hr") {
            return "Error: Cannot create agents with role ceo or hr from templates.";
          }
          const agentGoal = template.vibe || template.description || `Expert ${template.name.toLowerCase()} ready to work on assigned tasks.`;
          const agentBackstory = template.promptBody || template.description || "";
          const permType = permissionType === "coordinator" ? "coordinator" : "executor";

          // Look up the calling agent to get projectId
          const caller = await this.org.getAgent(agentId);
          if (!caller) {
            return "Error: Could not find your own agent record.";
          }
          const projectId = caller.projectId || undefined;

          // Validate parentId — default to CEO when omitted
          let resolvedParentId: string | null = null;
          let parentShortId: string | null = null;
          if (trimmedParentId) {
            const parentAgent = await this.org.resolveAgent(trimmedParentId);
            if (!parentAgent) {
              return `Error: No agent found with ID ${trimmedParentId}.`;
            }
            if (projectId && parentAgent.projectId !== projectId) {
              return `Error: Parent agent belongs to a different project.`;
            }
            resolvedParentId = parentAgent.id;
            parentShortId = parentAgent.shortId || null;
          } else if (projectId) {
            const ceo = await this.org.findAgentByRole(projectId, "ceo");
            if (ceo) {
              resolvedParentId = ceo.id;
              parentShortId = ceo.shortId || null;
            }
          }

          // IRON RULE: HR can NEVER create agents under itself
          if (caller.role?.toLowerCase() === "hr" && resolvedParentId === agentId) {
            return "Error: HR cannot create agents under itself. Set parentId to the CEO or a business manager.";
          }

          console.log(`[TOOL] create_from_template: template="${template.name}" name="${agentName}" parentId=${resolvedParentId} skills=${JSON.stringify(initialSkills)} mcp=${JSON.stringify(initialMcp)}`);
          const newId = await this.org.createAgent({
            name: agentName,
            role: agentRole,
            goal: agentGoal,
            backstory: agentBackstory,
            skills: [],
            parentId: resolvedParentId || undefined,
            projectId,
            permissionType: permType,
            mcpServers: initialMcp.length > 0 ? initialMcp : undefined,
            boundSkills: initialSkills.length > 0 ? initialSkills : undefined,
          });

          // Auto-create roster entry
          try {
            await this.roster.upsertRecord({
              projectId: projectId || "",
              agentId: newId,
              position: position || `${agentName} (${agentRole})`,
              department: department || template.division || "",
              responsibilities: agentGoal,
              notes: `Created from template: ${template.name}`,
              status: "active",
              updatedBy: agentId,
            });
          } catch {
            // Non-critical
          }

          const permLabel = permType === "coordinator" ? "协调者" : "执行者";
          const newAgent = await this.org.getAgent(newId);
          const newShortId = newAgent?.shortId || newId;
          const parentLabel = resolvedParentId
            ? `under parent agent ${parentShortId || resolvedParentId.slice(0, 8)}`
            : "as root-level agent";
          const bindingNote = [
            initialSkills.length > 0 ? `Skills: [${initialSkills.join(", ")}]` : null,
            initialMcp.length > 0 ? `MCP: [${initialMcp.join(", ")}]` : null,
          ].filter(Boolean).join(" | ");
          return `Agent created from template "${template.name}"!\nName: ${agentName}\nID: ${newShortId}\nRole: ${agentRole}\nType: ${permLabel}\nPlacement: ${parentLabel}${bindingNote ? `\n${bindingNote}` : ""}\nRoster entry created.`;
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

        // ── File Operations (workspace tools) ──────────────────
        case "read_file": {
          const caller = await this.org.getAgent(agentId);
          if (!caller?.projectId) return "Error: Could not determine your project.";
          const project = await this.projects.getProject(caller.projectId);
          if (!project?.workspacePath) return "Error: This project has no workspace configured. Ask the user to set a workspace path first.";
          const { filePath, offset, limit } = input;
          if (!filePath) return "Error: read_file requires filePath.";
          return await this.files.readFile(project.workspacePath, filePath, offset, limit);
        }

        case "write_file": {
          const caller = await this.org.getAgent(agentId);
          if (!caller?.projectId) return "Error: Could not determine your project.";
          const project = await this.projects.getProject(caller.projectId);
          if (!project?.workspacePath) return "Error: This project has no workspace configured. Ask the user to set a workspace path first.";
          const { filePath, content, append } = input;
          if (!filePath || content === undefined) return "Error: write_file requires filePath and content.";
          return await this.files.writeFile(project.workspacePath, filePath, content, append);
        }

        case "edit_file": {
          const caller = await this.org.getAgent(agentId);
          if (!caller?.projectId) return "Error: Could not determine your project.";
          const project = await this.projects.getProject(caller.projectId);
          if (!project?.workspacePath) return "Error: This project has no workspace configured. Ask the user to set a workspace path first.";
          const { filePath, oldText, newText } = input;
          if (!filePath) return "Error: edit_file requires filePath.";
          return await this.files.editFile(project.workspacePath, filePath, oldText || "", newText || "");
        }

        case "list_files": {
          const caller = await this.org.getAgent(agentId);
          if (!caller?.projectId) return "Error: Could not determine your project.";
          const project = await this.projects.getProject(caller.projectId);
          if (!project?.workspacePath) return "Error: This project has no workspace configured. Ask the user to set a workspace path first.";
          const { dirPath, recursive } = input;
          return await this.files.listFiles(project.workspacePath, dirPath, recursive);
        }

        case "search_files": {
          const caller = await this.org.getAgent(agentId);
          if (!caller?.projectId) return "Error: Could not determine your project.";
          const project = await this.projects.getProject(caller.projectId);
          if (!project?.workspacePath) return "Error: This project has no workspace configured. Ask the user to set a workspace path first.";
          const { pattern, searchPath, include } = input;
          if (!pattern) return "Error: search_files requires pattern.";
          return await this.files.searchFiles(project.workspacePath, pattern, searchPath, include);
        }

        case "delete_file": {
          const caller = await this.org.getAgent(agentId);
          if (!caller?.projectId) return "Error: Could not determine your project.";
          const project = await this.projects.getProject(caller.projectId);
          if (!project?.workspacePath) return "Error: This project has no workspace configured. Ask the user to set a workspace path first.";
          const { filePath } = input;
          if (!filePath) return "Error: delete_file requires filePath.";
          return await this.files.deleteFile(project.workspacePath, filePath);
        }

        // ── Bash Shell Execution (Effect-based, ported from OpenCode) ─
        case "bash": {
          const caller = await this.org.getAgent(agentId);
          if (!caller?.projectId) return "Error: Could not determine your project.";
          const project = await this.projects.getProject(caller.projectId);
          if (!project?.workspacePath) return "Error: This project has no workspace configured. Ask the user to set a workspace path first.";
          try {
            const result = await Effect.runPromise(
              runBashCommand(project.workspacePath, input).pipe(
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
          const caller = await this.org.getAgent(agentId);
          if (!caller?.projectId) return "Error: Could not determine your project.";
          const project = await this.projects.getProject(caller.projectId);
          if (!project?.workspacePath) return "Error: This project has no workspace configured. Ask the user to set a workspace path first.";
          const { command, cwd, timeout } = input;
          if (!command) return "Error: run_command requires a command.";
          try {
            const result = await this.shell.runCommand(project.workspacePath, {
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
          const caller = await this.org.getAgent(agentId);
          if (!caller?.projectId) return "Error: Could not determine your project.";
          const project = await this.projects.getProject(caller.projectId);
          if (!project?.workspacePath) return "Error: This project has no workspace configured. Ask the user to set a workspace path first.";
          const { pattern, cwd, limit } = input;
          if (!pattern) return "Error: glob requires a pattern.";
          return await this.files.globFiles(
            project.workspacePath,
            pattern,
            cwd || undefined,
            limit ? parseInt(String(limit), 10) : undefined,
          );
        }

        // ── Grep (regex file search, Effect-based) ──────────────
        case "grep": {
          const caller = await this.org.getAgent(agentId);
          if (!caller?.projectId) return "Error: Could not determine your project.";
          const project = await this.projects.getProject(caller.projectId);
          if (!project?.workspacePath) return "Error: This project has no workspace configured.";
          try {
            return await Effect.runPromise(executeGrep(project.workspacePath, input).pipe(
              Effect.catchAll((err) => Effect.succeed(`Error: ${err.message}`)),
            ));
          } catch (err: any) {
            return `Error: ${err.message}`;
          }
        }

        // ── Apply Patch (structured file editing) ─────────────
        case "apply_patch": {
          const caller = await this.org.getAgent(agentId);
          if (!caller?.projectId) return "Error: Could not determine your project.";
          const project = await this.projects.getProject(caller.projectId);
          if (!project?.workspacePath) return "Error: This project has no workspace configured.";
          try {
            return await Effect.runPromise(executeApplyPatch(project.workspacePath, input).pipe(
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
          const caller = await this.org.getAgent(agentId);
          if (!caller?.projectId) return "Error: Could not determine your project.";
          const project = await this.projects.getProject(caller.projectId);
          if (!project?.workspacePath) return "Error: This project has no workspace configured. Ask the user to set a workspace path first.";
          const { source, destination, overwrite } = input;
          if (!source || !destination) return "Error: move_file requires source and destination.";
          return await this.files.moveFile(
            project.workspacePath,
            source,
            destination,
            overwrite === true,
          );
        }

        // ── Create Directory ────────────────────────────────────
        case "create_directory": {
          const caller = await this.org.getAgent(agentId);
          if (!caller?.projectId) return "Error: Could not determine your project.";
          const project = await this.projects.getProject(caller.projectId);
          if (!project?.workspacePath) return "Error: This project has no workspace configured. Ask the user to set a workspace path first.";
          const { path: dirPath } = input;
          if (!dirPath) return "Error: create_directory requires a path.";
          return await this.files.createDirectory(project.workspacePath, dirPath);
        }

        // ── Delete Directory ────────────────────────────────────
        case "delete_directory": {
          const caller = await this.org.getAgent(agentId);
          if (!caller?.projectId) return "Error: Could not determine your project.";
          const project = await this.projects.getProject(caller.projectId);
          if (!project?.workspacePath) return "Error: This project has no workspace configured. Ask the user to set a workspace path first.";
          const { path: dirPath } = input;
          if (!dirPath) return "Error: delete_directory requires a path.";
          return await this.files.deleteDirectory(project.workspacePath, dirPath);
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
