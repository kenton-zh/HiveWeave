import "./env.js"; // Must be first — loads .env before other modules read process.env

import Fastify from "fastify";
import cors from "@fastify/cors";
import { orgRoutes } from "./routes/org.js";
import { chatRoutes } from "./routes/chat.js";
import { logRoutes } from "./routes/logs.js";
import { projectRoutes } from "./routes/projects.js";
import { permissionRoutes } from "./routes/permissions.js";
import { fsRoutes } from "./routes/fs.js";
import { templateRoutes } from "./routes/templates.js";
import { modelRoutes } from "./routes/models.js";
import { db, chatMessages, projects, agents, ensureProjectDb, registerProjectAgents, seedDefaultModel } from "@hiveweave/db";
import { conversationStore, OrgService, ChatMessageService, ApprovalService, RosterService } from "@hiveweave/core";
import "./services.js"; // clears in-memory pending approvals on import
import { eq } from "drizzle-orm";
import { randomUUID } from "crypto";

const app = Fastify({ logger: true });

await app.register(cors, { origin: "http://localhost:5173" });

// Clear conversation store in-memory caches on startup
conversationStore.clearAll();

// Helper: get per-project DB for a project (returns null if no workspacePath)
function getProjectDb(workspacePath: string | null) {
  if (!workspacePath) return null;
  try { return ensureProjectDb(workspacePath); } catch { return null; }
}

// For each existing project with a workspace: ensure per-project DB, clear chat,
// register agents, and clean up orphaned approval requests.
const existingProjects = await db.select().from(projects);
for (const proj of existingProjects) {
  const projectDb = getProjectDb(proj.workspacePath);
  if (!projectDb) continue;

  // Clear chat messages for fresh session (UI-visible chat is ephemeral per server instance)
  try { await projectDb.delete(chatMessages); } catch { /* table may not exist yet */ }

  // Register existing agents in the global registry
  const projectAgents = await projectDb.select({ id: agents.id }).from(agents).where(eq(agents.projectId, proj.id));
  if (projectAgents.length > 0) {
    registerProjectAgents(proj.workspacePath!, projectAgents.map(a => a.id));
  }

  // Ensure CEO + HR exist; migrate legacy HR-only roots under CEO
  {
    const orgService = new OrgService(projectDb, proj.workspacePath!);
    const rosterService = new RosterService(projectDb);

    let ceo = await orgService.findAgentByRole(proj.id, "ceo");

    if (projectAgents.length === 0) {
      const ceoAgentId = await orgService.createAgent({
        name: "CEO",
        role: "ceo",
        goal: "Project leader — designs charter and org structure, delegates staffing to HR. Use read_charter and save_charter; delegate all staffing to HR.",
        backstory: "You are the project CEO at the top of the organization. Maintain the charter, choose org paradigms, and coordinate business managers.",
        skills: [],
        parentId: undefined,
        projectId: proj.id,
        permissionType: "coordinator",
      });

      const hrAgentId = await orgService.createAgent({
        name: "HR",
        role: "hr",
        goal: "Staffing execution and communication hub — creates and manages agents per charter. You are the only role that may create, transfer, or dismiss agents.",
        backstory: "You report to the CEO. Execute staffing per the charter; confirm hiring plans with the user or CEO before building the team.",
        skills: [],
        parentId: ceoAgentId,
        projectId: proj.id,
        permissionType: "coordinator",
      });

      await rosterService.upsertRecord({
        projectId: proj.id,
        agentId: ceoAgentId,
        position: "CEO",
        department: "管理层",
        responsibilities: "维护项目章程；选定组织范式；协调业务负责人",
        notes: "组织顶层",
        status: "active",
        updatedBy: ceoAgentId,
      });

      await rosterService.upsertRecord({
        projectId: proj.id,
        agentId: hrAgentId,
        position: "HR负责人",
        department: "人力资源部",
        responsibilities: "招募、调动、解雇Agent；维护人员编制表",
        notes: "向CEO汇报的人员管理者",
        status: "active",
        updatedBy: hrAgentId,
      });

      registerProjectAgents(proj.workspacePath!, [ceoAgentId, hrAgentId]);
      console.log(`Created default CEO+HR for project "${proj.id}"`);
    } else if (!ceo) {
      const ceoAgentId = await orgService.createAgent({
        name: "CEO",
        role: "ceo",
        goal: "Project leader — designs charter and org structure, delegates staffing to HR. Use read_charter and save_charter; delegate all staffing to HR.",
        backstory: "You are the project CEO at the top of the organization. Maintain the charter, choose org paradigms, and coordinate business managers.",
        skills: [],
        parentId: undefined,
        projectId: proj.id,
        permissionType: "coordinator",
      });

      await rosterService.upsertRecord({
        projectId: proj.id,
        agentId: ceoAgentId,
        position: "CEO",
        department: "管理层",
        responsibilities: "维护项目章程；选定组织范式；协调业务负责人",
        notes: "组织顶层",
        status: "active",
        updatedBy: ceoAgentId,
      });

      registerProjectAgents(proj.workspacePath!, [ceoAgentId]);

      const hr = await orgService.findAgentByRole(proj.id, "hr");
      if (hr && !hr.parentId) {
        await orgService.updateParent(hr.id, ceoAgentId);
      }

      console.log(`Created CEO and re-parented HR under CEO for project "${proj.id}"`);
    }
  }

  // Clean up orphaned approval requests from previous server instance (per-project)
  try {
    const approvalService = new ApprovalService(projectDb);
    await approvalService.cleanupOrphanedRequests();
  } catch { /* permission_requests table may not exist in old DBs */ }
}

// Start periodic cleanup scheduler for old resolved approval requests (all projects)
setInterval(async () => {
  const allProjects = await db.select().from(projects);
  for (const proj of allProjects) {
    const pdb = getProjectDb(proj.workspacePath);
    if (!pdb) continue;
    try {
      const approvalService = new ApprovalService(pdb);
      await approvalService.cleanupOldRequests();
    } catch { /* ignore per-project cleanup errors */ }
  }
}, 60 * 60 * 1000); // every hour

console.log("Chat history and conversation store cleared for fresh session");

// Seed default LLM model if registry is empty
seedDefaultModel();

// Register routes
await app.register(projectRoutes, { prefix: "/api/projects" });
await app.register(orgRoutes, { prefix: "/api/org" });
await app.register(chatRoutes, { prefix: "/api/chat" });
await app.register(logRoutes, { prefix: "/api/logs" });
await app.register(permissionRoutes, { prefix: "/api/permissions" });
await app.register(fsRoutes, { prefix: "/api/fs" });
await app.register(templateRoutes, { prefix: "/api/templates" });
await app.register(modelRoutes, { prefix: "/api/models" });

// Health check
app.get("/api/health", async () => ({ status: "ok", timestamp: Date.now() }));

const port = Number(process.env.PORT) || 3200;
await app.listen({ port, host: "0.0.0.0" });
console.log(`HiveWeave server running on http://localhost:${port}`);
