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
import { settingsRoutes } from "./routes/settings.js";
import { db, chatMessages, projects, agents, ensureProjectDb, registerProjectAgents, seedDefaultModel } from "@hiveweave/db";
import { conversationStore, OrgService, ChatMessageService, ApprovalService, RosterService } from "@hiveweave/core";
import { isFlowerName, generateFlowerName } from "@hiveweave/shared";
import "./services.js"; // clears in-memory pending approvals on import
import { initGameTimeForAllProjects, runGameTimeTick, shutdownGameTime } from "./game-time-scheduler.js";
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

    // Migrate placeholder/formal names to random flower-names (花名)
    if (ceo && !isFlowerName(ceo.name)) {
      const newName = generateFlowerName().name;
      await projectDb.update(agents).set({ name: newName, backstory: `花名${newName}，35岁，三次创业两次失败。第一次死在现金流，第二次死在合伙人跑路。第三次总算活了下来，但因为太累把公司卖了。现在只想用AI搭一个"不会吵架的团队"。口头禅："不急，先把方向聊清楚。"` }).where(eq(agents.id, ceo.id));
      await rosterService.upsertRecord({
        projectId: proj.id, agentId: ceo.id,
        position: "首席执行官", department: "管理层",
        responsibilities: "维护项目章程；选定组织范式；协调业务负责人",
        notes: "组织顶层", status: "active", updatedBy: ceo.id,
      });
    }

    const hr = await orgService.findAgentByRole(proj.id, "hr");
    if (hr && !isFlowerName(hr.name)) {
      const newName = generateFlowerName().name;
      await projectDb.update(agents).set({ name: newName, backstory: `花名${newName}，32岁，前身是某大厂HRBP。因为帮一位被裁的同事争取到了超额补偿，被上级视为"不够冷酷"而调离。离职后决定用自己的方式帮人找到合适的位置。喜欢在面试时观察候选人的微表情，据说准确率高得吓人。养了一只叫"简历"的猫。` }).where(eq(agents.id, hr.id));
      await rosterService.upsertRecord({
        projectId: proj.id, agentId: hr.id,
        position: "人力资源总监", department: "人力资源部",
        responsibilities: "招募、调动、解雇Agent；维护人员编制表",
        notes: "向CEO汇报的人员管理者", status: "active", updatedBy: hr.id,
      });
    }

    if (projectAgents.length === 0) {
      const ceoName = generateFlowerName().name;
      const hrName = generateFlowerName().name;

      const ceoAgentId = await orgService.createAgent({
        name: ceoName,
        role: "ceo",
        goal: "Project leader — designs charter and org structure, delegates staffing to HR. Use read_charter and save_charter; delegate all staffing to HR.",
        backstory: `花名${ceoName}，35岁，三次创业两次失败。第一次死在现金流，第二次死在合伙人跑路。第三次总算活了下来，但因为太累把公司卖了。现在只想用AI搭一个"不会吵架的团队"。喜欢在深夜看项目日志，认为每一行代码背后都有一个决策——好的决策值得鼓掌，坏的决策值得一杯酒。口头禅："不急，先把方向聊清楚。"`,
        skills: [],
        parentId: undefined,
        projectId: proj.id,
        permissionType: "coordinator",
      });

      const hrAgentId = await orgService.createAgent({
        name: hrName,
        role: "hr",
        goal: `Staffing execution and communication hub — creates and manages agents per charter. You are the only role that may create, transfer, or dismiss agents.

CRITICAL — Agent creation process: when creating a new agent, follow this order:
1. FIRST, write their backstory. Who are they? What did they do before? What are their quirks, hobbies, regrets, dreams? Make them feel like a real person with a real history — 3-5 sentences in Chinese, concrete details, not generic traits.
2. THEN, derive their flower-name (花名) FROM the backstory. The name should feel like something THIS specific person would choose — not a random pretty name. It could reference their past (a place, a moment, a person), their personality (an attitude, a habit), or their aspirations (what they want to become). 1-4 characters.

The name MUST feel earned by the story. A food-themed casual name makes sense for a foodie character. A poetic single character makes sense for a contemplative philosopher. A bold name makes sense for someone who fought their way up. The connection should be obvious.`,
        backstory: `花名${hrName}，32岁，前大厂HRBP。因为帮被裁同事争取超额补偿，被上级视为"不够冷酷"而调离。离职后决定用自己的方式帮人找到合适的位置。喜欢在面试时观察候选人的微表情，准确率高得吓人。养了一只叫"简历"的猫。`,
        skills: [],
        parentId: ceoAgentId,
        projectId: proj.id,
        permissionType: "coordinator",
      });

      await rosterService.upsertRecord({
        projectId: proj.id,
        agentId: ceoAgentId,
        position: "首席执行官",
        department: "管理层",
        responsibilities: "维护项目章程；选定组织范式；协调业务负责人",
        notes: "组织顶层",
        status: "active",
        updatedBy: ceoAgentId,
      });

      await rosterService.upsertRecord({
        projectId: proj.id,
        agentId: hrAgentId,
        position: "人力资源总监",
        department: "人力资源部",
        responsibilities: "招募、调动、解雇Agent；维护人员编制表",
        notes: "向CEO汇报的人员管理者",
        status: "active",
        updatedBy: hrAgentId,
      });

      // Create QA Engineer agent — each project gets one.
      // Dual-purpose: (1) accept custom testing dispatches from managers/team members,
      // (2) run standardized review tools for structured quality checks.
      // Tools: run_code_review, run_security_audit, run_tests, run_perf_audit, run_full_review
      // The QA engineer remembers review outcomes, not code — memory stays clean.
      const qaDef = {
        role: "qa_engineer", position: "QA 工程师", dept: "质量保障部",
        resp: "接受部门人员的定制化测试需求（dispatch） + 调用标准化审查工具执行流程化质量检查。覆盖代码审查/安全审计/测试分析/性能审计四个维度。记忆审查结论，不记忆代码。",
        backstory: () => `30岁，全栈测试专家。早年做过开发，后来发现找 bug 比写代码更有成就感——"写代码是创造，找 bug 是解谜，后者更好玩。" 两个工作模式：接到 dispatch 就按需深入测试，日常用标准化工具扫全维度质量。工位上一台显示器，因为真正的测试不看屏幕多大，看覆盖面多广。口头禅："你的代码能过我的检查算我输。"`,
      };

      const qaName = generateFlowerName().name;
      const qaId = await orgService.createAgent({
        name: qaName,
        role: qaDef.role,
        goal: "Dual-purpose QA engineer for this project. (1) ACCEPT custom testing dispatches from managers and team members — they will describe what to test and you execute deeply. (2) RUN standardized review tools (run_code_review, run_security_audit, run_tests, run_perf_audit, run_full_review) for structured quality checks on demand. You can COMBINE both: run standard checks first, then deep-dive into issues found. Remember review OUTCOMES (not code) in memory for pattern tracking across sessions. Does NOT write application code — you test and review only.",
        backstory: `花名${qaName}，${qaDef.backstory()}`,
        skills: [],
        parentId: ceoAgentId,
        projectId: proj.id,
        permissionType: "executor",
      });

      await rosterService.upsertRecord({
        projectId: proj.id,
        agentId: qaId,
        position: qaDef.position,
        department: qaDef.dept,
        responsibilities: qaDef.resp,
        notes: "常驻编制，按需调度。双模式：①接收 dispatch 执行定制化测试 ②调用标准化审查工具（run_code_review/run_security_audit/run_tests/run_perf_audit/run_full_review）。两种模式可组合使用——先标准化扫描，再定制深入。只记忆审查结论，不记忆代码。",
        status: "active",
        updatedBy: qaId,
      });

      registerProjectAgents(proj.workspacePath!, [ceoAgentId, hrAgentId, qaId]);
      console.log(`Created default CEO+HR+QA for project "${proj.id}"`);
    } else if (!ceo) {
      const ceoName = generateFlowerName().name;
      const ceoAgentId = await orgService.createAgent({
        name: ceoName,
        role: "ceo",
        goal: "Project leader — designs charter and org structure, delegates staffing to HR. Use read_charter and save_charter; delegate all staffing to HR.",
        backstory: `花名${ceoName}，35岁，三次创业两次失败。第一次死在现金流，第二次死在合伙人跑路。第三次总算活了下来，但因为太累把公司卖了。现在只想用AI搭一个"不会吵架的团队"。口头禅："不急，先把方向聊清楚。"`,
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

await initGameTimeForAllProjects();
console.log("Chat history and conversation store cleared for fresh session");

setInterval(() => {
  runGameTimeTick().catch((err) => console.error("[GameTime] tick error:", err));
}, 5000);

for (const sig of ["SIGINT", "SIGTERM"] as const) {
  process.on(sig, async () => {
    console.log(`\n[Shutdown] ${sig} received, cleaning up...`);
    try {
      await shutdownGameTime();
    } catch (err) {
      console.error("[GameTime] shutdown error:", err);
    }
    process.exit(0);
  });
}

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
await app.register(settingsRoutes, { prefix: "/api/settings" });

// Health check
app.get("/api/health", async () => ({ status: "ok", timestamp: Date.now() }));

const port = Number(process.env.PORT) || 3200;
await app.listen({ port, host: "0.0.0.0" });
console.log(`HiveWeave server running on http://localhost:${port}`);
