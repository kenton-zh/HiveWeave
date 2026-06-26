import type { FastifyInstance } from "fastify";
import { z } from "zod";
import { ProjectService, OrgService, RosterService, getGameTimeService, formatGoalsForPrompt } from "@hiveweave/core";
import type { EnterpriseGoals } from "@hiveweave/core";
import { db, projects, agents, ensureProjectDb, evictProjectDb, unregisterProjectAgents, registerProjectAgents } from "@hiveweave/db";
import { randomUUID } from "crypto";
import { existsSync } from "fs";
import { rm, mkdir, cp } from "fs/promises";
import { execFile } from "child_process";
import { join } from "path";
import { eq } from "drizzle-orm";
import { generateFlowerName } from "@hiveweave/shared";

/** Promisified execFile — non-blocking, won't stall the event loop like execSync. */
function run(cmd: string, args: string[], opts: { cwd: string; timeout?: number }): Promise<{ stdout: string; stderr: string }> {
  return new Promise((resolve, reject) => {
    execFile(cmd, args, { ...opts, encoding: "utf-8", maxBuffer: 1024 * 1024 }, (err, stdout, stderr) => {
      if (err) reject(err);
      else resolve({ stdout, stderr });
    });
  });
}

const CreateProjectBody = z.object({
  name: z.string().min(1),
  workspacePath: z.string().optional(),
  description: z.string().optional(),
  orgParadigm: z.string().optional(),
});

export async function projectRoutes(fastify: FastifyInstance) {
  // ProjectService uses meta DB for projects table
  const projectService = new ProjectService(db);
  const gameTimeService = getGameTimeService(db);

  fastify.get("/", async (_request, reply) => {
    try {
      return await projectService.listProjects();
    } catch (error: any) {
      fastify.log.error(error, "Failed to list projects");
      return reply.status(500).send({ error: "Failed to list projects", details: error.message });
    }
  });

  fastify.post<{ Body: z.infer<typeof CreateProjectBody> }>("/", async (request, reply) => {
    const parsed = CreateProjectBody.safeParse(request.body);
    if (!parsed.success) {
      return reply.status(400).send({ error: "Validation failed", issues: parsed.error.issues });
    }

    try {
      const { workspacePath } = parsed.data;

      // Create the project record in meta DB
      const projectId = await projectService.createProject(
        parsed.data.name,
        workspacePath,
        parsed.data.description,
        parsed.data.orgParadigm,
      );

      // If workspacePath is set, initialize per-project DB and create CEO + HR
      if (workspacePath) {
        const projectDb = ensureProjectDb(workspacePath);
        const orgService = new OrgService(projectDb, workspacePath);
        const rosterService = new RosterService(projectDb);

        const ceoName = generateFlowerName().name;
        const hrName = generateFlowerName().name;

        const ceoAgentId = await orgService.createAgent({
          name: ceoName,
          role: "ceo",
          goal: "Project leader — designs charter and org structure, delegates staffing to HR. Use read_charter and save_charter; delegate all staffing to HR.",
          backstory: `花名${ceoName}，35岁，三次创业两次失败。第一次死在现金流，第二次死在合伙人跑路。第三次总算活了下来，但因为太累把公司卖了。现在只想用AI搭一个"不会吵架的团队"。喜欢在深夜看项目日志，认为每一行代码背后都有一个决策。口头禅："不急，先把方向聊清楚。"`,
          skills: [],
          parentId: undefined,
          projectId,
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
          projectId,
          permissionType: "coordinator",
        });

        if (parsed.data.orgParadigm) {
          const { getDefaultCharter } = await import("@hiveweave/shared");
          const charter = (await projectService.getCharter(projectId)) ?? getDefaultCharter();
          charter.orgParadigm = parsed.data.orgParadigm;
          await projectService.saveCharter(projectId, charter, ceoAgentId);
        }

        await rosterService.upsertRecord({
          projectId,
          agentId: ceoAgentId,
          position: "首席执行官",
          department: "管理层",
          responsibilities: "维护项目章程；选定组织范式；协调业务负责人",
          notes: "组织顶层",
          status: "active",
          updatedBy: ceoAgentId,
        });

        await rosterService.upsertRecord({
          projectId,
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
          projectId,
          permissionType: "executor",
        });

        await rosterService.upsertRecord({
          projectId,
          agentId: qaId,
          position: qaDef.position,
          department: qaDef.dept,
          responsibilities: qaDef.resp,
          notes: "常驻编制，按需调度。双模式：①接收 dispatch 执行定制化测试 ②调用标准化审查工具（run_code_review/run_security_audit/run_tests/run_perf_audit/run_full_review）。两种模式可组合使用——先标准化扫描，再定制深入。只记忆审查结论，不记忆代码。",
          status: "active",
          updatedBy: qaId,
        });

        return reply.status(201).send({ id: projectId, mainAgentId: ceoAgentId });
      }

      return reply.status(201).send({ id: projectId });
    } catch (error: any) {
      fastify.log.error(error, "Failed to create project");
      // Duplicate workspace path → Conflict
      if (error.message?.includes("already used by project")) {
        return reply.status(409).send({ error: "Duplicate workspace", details: error.message });
      }
      return reply.status(500).send({ error: "Failed to create project", details: error.message });
    }
  });


  fastify.get<{ Params: { id: string } }>("/:id/game-time", async (request, reply) => {
    const { id } = request.params;
    try {
      const project = await projectService.getProject(id);
      if (!project) return reply.status(404).send({ error: "Project not found" });
      await gameTimeService.initProject(id);
      const snap = gameTimeService.getSnapshot(id);
      return {
        projectId: id,
        gameSeconds: snap.gameSeconds,
        day: snap.day,
        formatted: snap.formatted,
        realFormatted: snap.realFormatted,
        realTimestamp: snap.realTimestamp,
      };
    } catch (error: any) {
      fastify.log.error(error, "Failed to get project game time");
      return reply.status(500).send({ error: "Failed to get project game time", details: error.message });
    }
  });

  fastify.delete<{ Params: { id: string } }>("/:id", async (request, reply) => {
    const { id } = request.params;

    try {
      // Step 1: Get project info before deletion (need workspacePath for cleanup)
      fastify.log.info(`[DELETE] Step 1: Getting project ${id}`);
      const project = await projectService.getProject(id);

      // Step 2: Get per-project DB if workspace exists, for proper cascade deletion
      let projectDb = null;
      if (project?.workspacePath) {
        fastify.log.info(`[DELETE] Step 2: Opening project DB at ${project.workspacePath}`);
        projectDb = ensureProjectDb(project.workspacePath);
      }

      // Step 3: Cascade delete from database
      fastify.log.info(`[DELETE] Step 3: Running cascade delete`);
      const result = await projectService.deleteProjectCascade(id, projectDb || undefined);
      if (!result.ok) {
        return reply.status(404).send({ error: result.reason });
      }

      // Step 4: Clean up per-project filesystem
      if (project?.workspacePath) {
        fastify.log.info(`[DELETE] Step 4a: Unregistering agents`);
        unregisterProjectAgents(project.workspacePath);

        fastify.log.info(`[DELETE] Step 4b: Evicting project DB`);
        evictProjectDb(project.workspacePath);

        // Step 4c: Clean up git worktrees and branches before removing .hiveweave/
        const hwDir = join(project.workspacePath, ".hiveweave");
        const gitDir = join(project.workspacePath, ".git");
        if (existsSync(gitDir)) {
          try {
            // Remove all hiveweave-managed git worktrees (async — won't block event loop)
            const { stdout: wtList } = await run("git", ["worktree", "list"], { cwd: project.workspacePath, timeout: 10000 });
            for (const line of wtList.split("\n")) {
              if (line.includes(".hiveweave/worktrees")) {
                const wtPath = line.split(/\s+/)[0];
                if (wtPath) {
                  try {
                    await run("git", ["worktree", "remove", wtPath, "--force"], { cwd: project.workspacePath, timeout: 10000 });
                    fastify.log.info(`[DELETE] Removed worktree: ${wtPath}`);
                  } catch { /* worktree may already be gone */ }
                }
              }
            }

            // Delete all hw/* branches
            const { stdout: branchesOut } = await run("git", ["branch"], { cwd: project.workspacePath, timeout: 10000 });
            for (const line of branchesOut.split("\n")) {
              const trimmed = line.replace(/^\*?\s+/, "").trim();
              if (trimmed.startsWith("hw/")) {
                try {
                  await run("git", ["branch", "-D", trimmed], { cwd: project.workspacePath, timeout: 10000 });
                  fastify.log.info(`[DELETE] Deleted branch: ${trimmed}`);
                } catch { /* branch may already be gone */ }
              }
            }

            // Prune stale worktree references
            try { await run("git", ["worktree", "prune"], { cwd: project.workspacePath, timeout: 10000 }); } catch {}
          } catch (err: any) {
            fastify.log.warn(`[DELETE] Git cleanup warning: ${err.message?.slice(0, 200)}`);
          }
        }

        // Step 4d: Remove .hiveweave/ directory (async — won't block event loop)
        if (existsSync(hwDir)) {
          try {
            fastify.log.info(`[DELETE] Step 4d: Removing ${hwDir}`);
            await rm(hwDir, { recursive: true, force: true });
            fastify.log.info(`Cleaned up .hiveweave directory: ${hwDir}`);
          } catch (err: any) {
            fastify.log.warn(err, `Failed to clean up .hiveweave directory: ${hwDir}`);
          }
        }
      }

      fastify.log.info(`[DELETE] Complete: project ${id} deleted`);
      return { deleted: true };
    } catch (error: any) {
      fastify.log.error(error, `Failed to delete project (code=${error.code}, errno=${error.errno})`);
      return reply.status(500).send({
        error: "Failed to delete project",
        details: error.message,
        code: error.code,
        errno: error.errno,
      });
    }
  });

  // Update workspace path for an existing project
  const UpdateWorkspaceBody = z.object({
    workspacePath: z.string().nullable(),
  });

  fastify.put<{ Params: { id: string }; Body: z.infer<typeof UpdateWorkspaceBody> }>("/:id/workspace", async (request, reply) => {
    const parsed = UpdateWorkspaceBody.safeParse(request.body);
    if (!parsed.success) {
      return reply.status(400).send({ error: "Validation failed", issues: parsed.error.issues });
    }

    try {
      const projectId = request.params.id;
      const newWsPath = parsed.data.workspacePath;

      // Get old project info
      const oldProject = await projectService.getProject(projectId);
      if (!oldProject) {
        return reply.status(404).send({ error: "Project not found" });
      }

      const oldWsPath = oldProject.workspacePath;

      // If workspace is actually changing, move the .hiveweave data
      if (oldWsPath && newWsPath && oldWsPath !== newWsPath) {
        // Close old DB connection and evict from cache
        evictProjectDb(oldWsPath);

        // Move old .hiveweave directory to new workspace
        const oldHwDir = join(oldWsPath, ".hiveweave");
        const newHwDir = join(newWsPath, ".hiveweave");

        if (existsSync(oldHwDir)) {
          await mkdir(newHwDir, { recursive: true });
          await cp(oldHwDir, newHwDir, { recursive: true });
          await rm(oldHwDir, { recursive: true, force: true });
        }

        // Re-register agents with new workspace path
        unregisterProjectAgents(oldWsPath);
        const newProjectDb = ensureProjectDb(newWsPath);
        const agentRows = await newProjectDb.select({ id: agents.id }).from(agents).where(eq(agents.projectId, projectId));
        if (agentRows.length > 0) {
          registerProjectAgents(newWsPath, agentRows.map(a => a.id));
        }
      } else if (newWsPath && !oldWsPath) {
        // First time setting a workspace — just initialize the per-project DB
        ensureProjectDb(newWsPath);
      } else if (!newWsPath && oldWsPath) {
        // Removing workspace — close connection and evict cache
        evictProjectDb(oldWsPath);
        unregisterProjectAgents(oldWsPath);
      }

      await projectService.updateWorkspacePath(projectId, newWsPath);
      return { ok: true };
    } catch (error: any) {
      fastify.log.error(error, "Failed to update workspace path");
      return reply.status(500).send({ error: "Failed to update workspace", details: error.message });
    }
  });

  // Update project description and/or organizational paradigm
  const UpdateProjectBody = z.object({
    description: z.string().nullable().optional(),
    orgParadigm: z.string().nullable().optional(),
  });

  fastify.patch<{ Params: { id: string }; Body: z.infer<typeof UpdateProjectBody> }>("/:id", async (request, reply) => {
    const parsed = UpdateProjectBody.safeParse(request.body);
    if (!parsed.success) {
      return reply.status(400).send({ error: "Validation failed", issues: parsed.error.issues });
    }

    try {
      const project = await projectService.getProject(request.params.id);
      if (!project) {
        return reply.status(404).send({ error: "Project not found" });
      }

      const updates: Record<string, string | null> = {};
      if (parsed.data.description !== undefined) updates.description = parsed.data.description;
      if (parsed.data.orgParadigm !== undefined) updates.orgParadigm = parsed.data.orgParadigm;

      if (Object.keys(updates).length > 0) {
        await projectService.updateProject(request.params.id, updates);
      }

      return { ok: true };
    } catch (error: any) {
      fastify.log.error(error, "Failed to update project");
      return reply.status(500).send({ error: "Failed to update project", details: error.message });
    }
  });

  // ── Enterprise Goals (workboard) ──────────────────────────────
  fastify.get<{ Params: { id: string } }>("/:id/goals", async (request, reply) => {
    try {
      const project = await projectService.getProject(request.params.id);
      if (!project) return reply.status(404).send({ error: "Project not found" });
      const goals = await projectService.getGoals(request.params.id);
      return { goals };
    } catch (error: any) {
      fastify.log.error(error, "Failed to get goals");
      return reply.status(500).send({ error: "Failed to get goals", details: error.message });
    }
  });

  const GoalsBody = z.object({
    objective: z.string().optional(),
    focus: z.string().optional(),
    keyResults: z.array(z.object({
      text: z.string(),
      status: z.enum(["todo", "doing", "done"]),
      owner: z.string().optional(),
    })).optional(),
  });

  fastify.put<{ Params: { id: string }; Body: z.infer<typeof GoalsBody> }>("/:id/goals", async (request, reply) => {
    const parsed = GoalsBody.safeParse(request.body);
    if (!parsed.success) {
      return reply.status(400).send({ error: "Validation failed", issues: parsed.error.issues });
    }

    try {
      const project = await projectService.getProject(request.params.id);
      if (!project) return reply.status(404).send({ error: "Project not found" });

      const existing = await projectService.getGoals(request.params.id) || {
        objective: "",
        focus: "",
        keyResults: [],
      };

      if (parsed.data.objective !== undefined) existing.objective = parsed.data.objective;
      if (parsed.data.focus !== undefined) existing.focus = parsed.data.focus;
      if (parsed.data.keyResults !== undefined) {
        for (const kr of parsed.data.keyResults) {
          const idx = existing.keyResults.findIndex((k) => k.text === kr.text);
          if (idx >= 0) {
            existing.keyResults[idx] = { ...existing.keyResults[idx], ...kr };
          } else {
            existing.keyResults.push(kr);
          }
        }
      }

      await projectService.saveGoals(request.params.id, existing as EnterpriseGoals);
      return { ok: true, goals: existing };
    } catch (error: any) {
      fastify.log.error(error, "Failed to update goals");
      return reply.status(500).send({ error: "Failed to update goals", details: error.message });
    }
  });
}
