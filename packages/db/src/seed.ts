import { randomUUID } from "crypto";
import { db } from "./client.js";
import { agents, modules, memories } from "./schema/index.js";

const now = Date.now();

// --- Modules ---

const hiveWeaveModuleId = randomUUID();
const frontendModuleId = randomUUID();

db.insert(modules)
  .values([
    {
      id: hiveWeaveModuleId,
      name: "HiveWeave",
      parentModuleId: null,
      status: "active",
      currentAgentId: null,
      createdAt: now,
      updatedAt: now,
    },
    {
      id: frontendModuleId,
      name: "前端",
      parentModuleId: hiveWeaveModuleId,
      status: "active",
      currentAgentId: null,
      createdAt: now,
      updatedAt: now,
    },
  ])
  .run();

// --- Agents ---

const architectId = randomUUID();
const managerId = randomUUID();
const leafId = randomUUID();

db.insert(agents)
  .values([
    {
      id: architectId,
      name: "总架构师",
      role: "architect",
      parentId: null,
      moduleId: null,
      status: "active",
      goal: "协调并监督整个 HiveWeave 项目的架构设计与实施",
      backstory: "经验丰富的系统架构师，负责全局决策与跨模块协调",
      skills: JSON.stringify(["architecture", "coordination", "review"]),
      permissionType: "coordinator",
      createdAt: now,
      updatedAt: now,
    },
    {
      id: managerId,
      name: "前端经理",
      role: "manager",
      parentId: architectId,
      moduleId: frontendModuleId,
      status: "active",
      goal: "管理前端模块的开发进度与质量",
      backstory: "资深前端团队负责人，擅长任务分配与进度跟踪",
      skills: JSON.stringify(["management", "frontend", "coordination"]),
      permissionType: "coordinator",
      createdAt: now,
      updatedAt: now,
    },
    {
      id: leafId,
      name: "首页模块负责人",
      role: "module_dev",
      parentId: managerId,
      moduleId: frontendModuleId,
      status: "created",
      goal: "完成首页模块的设计与实现",
      backstory: "专注首页模块开发，确保用户体验流畅",
      skills: JSON.stringify(["react", "ui", "performance"]),
      permissionType: "executor",
      createdAt: now,
      updatedAt: now,
    },
  ])
  .run();

// --- Project-scope memory: project constitution ---

db.insert(memories)
  .values({
    id: randomUUID(),
    agentId: null,
    scope: "project",
    moduleId: null,
    type: "constitution",
    content: JSON.stringify({
      projectName: "HiveWeave",
      description: "多智能体协作的项目管理系统",
      principles: [
        "模块化设计，每个模块独立可测试",
        "Agent 分层协作：架构师 → 经理 → 模块开发者",
        "所有决策记录在记忆系统中，确保可追溯",
        "代码质量优先，遵循严格类型检查",
      ],
      techStack: [
        "TypeScript",
        "Drizzle ORM",
        "SQLite",
        "React",
        "tRPC",
      ],
    }),
    sourceAgentId: architectId,
    metadata: JSON.stringify({ version: "1.0" }),
    createdAt: now,
    updatedAt: now,
  })
  .run();

console.log("Seed data created successfully:");
console.log(`  Modules: HiveWeave (${hiveWeaveModuleId}), 前端 (${frontendModuleId})`);
console.log(`  Agents: 总架构师 (${architectId}), 前端经理 (${managerId}), 首页模块负责人 (${leafId})`);
console.log(`  Memories: project constitution`);
