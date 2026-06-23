import type { FastifyInstance } from "fastify";
import { z } from "zod";
import { ProjectService, OrgService, RosterService } from "@hiveweave/core";
import { db, projects, agents, ensureProjectDb, evictProjectDb, unregisterProjectAgents, registerProjectAgents } from "@hiveweave/db";
import { randomUUID } from "crypto";
import { existsSync, rmSync, mkdirSync, cpSync } from "fs";
import { join } from "path";
import { eq } from "drizzle-orm";

const CreateProjectBody = z.object({
  name: z.string().min(1),
  workspacePath: z.string().optional(),
  description: z.string().optional(),
  orgParadigm: z.string().optional(),
});

export async function projectRoutes(fastify: FastifyInstance) {
  // ProjectService uses meta DB for projects table
  const projectService = new ProjectService(db);

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

        const ceoAgentId = await orgService.createAgent({
          name: "CEO",
          role: "ceo",
          goal: "Project leader — designs charter and org structure, delegates staffing to HR. Use read_charter and save_charter; delegate all staffing to HR.",
          backstory: "You are the project CEO at the top of the organization. Maintain the charter, choose org paradigms, and coordinate business managers.",
          skills: [],
          parentId: undefined,
          projectId,
          permissionType: "coordinator",
        });

        const hrAgentId = await orgService.createAgent({
          name: "HR",
          role: "hr",
          goal: "Staffing execution and communication hub — creates and manages agents per charter. You are the only role that may create, transfer, or dismiss agents.",
          backstory: "You report to the CEO. Execute staffing per the charter; confirm hiring plans with the user or CEO before building the team.",
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
          position: "CEO",
          department: "管理层",
          responsibilities: "维护项目章程；选定组织范式；协调业务负责人",
          notes: "组织顶层",
          status: "active",
          updatedBy: ceoAgentId,
        });

        await rosterService.upsertRecord({
          projectId,
          agentId: hrAgentId,
          position: "HR负责人",
          department: "人力资源部",
          responsibilities: "招募、调动、解雇Agent；维护人员编制表",
          notes: "向CEO汇报的人员管理者",
          status: "active",
          updatedBy: hrAgentId,
        });

        return reply.status(201).send({ id: projectId, mainAgentId: ceoAgentId });
      }

      return reply.status(201).send({ id: projectId });
    } catch (error: any) {
      fastify.log.error(error, "Failed to create project");
      return reply.status(500).send({ error: "Failed to create project", details: error.message });
    }
  });

  fastify.delete<{ Params: { id: string } }>("/:id", async (request, reply) => {
    const { id } = request.params;

    try {
      // Get project info before deletion (need workspacePath for cleanup)
      const project = await projectService.getProject(id);

      // Get per-project DB if workspace exists, for proper cascade deletion
      let projectDb = null;
      if (project?.workspacePath) {
        projectDb = ensureProjectDb(project.workspacePath);
      }

      const result = await projectService.deleteProjectCascade(id, projectDb || undefined);
      if (!result.ok) {
        return reply.status(404).send({ error: result.reason });
      }

      // Clean up per-project filesystem
      if (project?.workspacePath) {
        // Unregister all agents from the global registry
        unregisterProjectAgents(project.workspacePath);

        // Close DB connection and evict from cache BEFORE filesystem deletion
        evictProjectDb(project.workspacePath);

        // Delete .hiveweave directory
        const hwDir = join(project.workspacePath, ".hiveweave");
        if (existsSync(hwDir)) {
          try {
            rmSync(hwDir, { recursive: true, force: true });
            fastify.log.info(`Cleaned up .hiveweave directory: ${hwDir}`);
          } catch (err: any) {
            fastify.log.warn(err, `Failed to clean up .hiveweave directory: ${hwDir}`);
          }
        }
      }

      return { deleted: true };
    } catch (error: any) {
      fastify.log.error(error, "Failed to delete project");
      return reply.status(500).send({ error: "Failed to delete project", details: error.message });
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
          mkdirSync(newHwDir, { recursive: true });
          cpSync(oldHwDir, newHwDir, { recursive: true });
          rmSync(oldHwDir, { recursive: true, force: true });
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
}
