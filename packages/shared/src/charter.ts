import { z } from "zod";

export const CharterRoleSchema = z.object({
  role: z.string(),
  description: z.string(),
  permissionType: z.enum(["coordinator", "executor"]).optional(),
});

export const CharterArtifactKindSchema = z.object({
  kind: z.string(),
  description: z.string(),
});

export const StaffingPolicySchema = z.object({
  hiringRequiresCeoApproval: z.boolean().optional(),
  defaultParentRole: z.string().optional(),
  notes: z.string().optional(),
});

export const ProjectCharterSchema = z.object({
  version: z.number().default(1),
  mission: z.string(),
  goals: z.array(z.string()).default([]),
  orgParadigm: z.string().nullable().optional(),
  roles: z.array(CharterRoleSchema).default([]),
  artifactKinds: z.array(CharterArtifactKindSchema).default([]),
  staffingPolicy: StaffingPolicySchema.optional(),
  constraints: z.array(z.string()).default([]),
  updatedAt: z.number().optional(),
  updatedByAgentId: z.string().optional(),
});

export type CharterRole = z.infer<typeof CharterRoleSchema>;
export type CharterArtifactKind = z.infer<typeof CharterArtifactKindSchema>;
export type StaffingPolicy = z.infer<typeof StaffingPolicySchema>;
export type ProjectCharter = z.infer<typeof ProjectCharterSchema>;

export function getDefaultCharter(): ProjectCharter {
  return {
    version: 1,
    mission: "Deliver the project goals through a well-structured AI engineering organization.",
    goals: [],
    orgParadigm: null,
    roles: [
      { role: "ceo", description: "Project leader — designs charter and org structure, delegates staffing to HR.", permissionType: "coordinator" },
      { role: "hr", description: "Staffing execution and communication hub — creates and manages agents per charter.", permissionType: "coordinator" },
      { role: "manager", description: "Coordinates a functional area and dispatches work to executors.", permissionType: "coordinator" },
      { role: "developer", description: "Implements features and fixes in the workspace.", permissionType: "executor" },
    ],
    artifactKinds: [
      { kind: "code", description: "Source code changes in the project workspace." },
      { kind: "design", description: "Architecture or UX decisions documented in work logs or memory." },
      { kind: "report", description: "Status summaries and completion reports." },
    ],
    staffingPolicy: {
      hiringRequiresCeoApproval: false,
      defaultParentRole: "ceo",
      notes: "HR places new agents under the CEO or a business manager — never under HR or as root unless CEO directs.",
    },
    constraints: [
      "Only HR may create, transfer, or dismiss agents.",
      "CEO authors and updates the project charter.",
      "Coordinators do not write code directly; executors do the implementation work.",
    ],
  };
}

export function parseCharterJson(json: string | null | undefined): ProjectCharter | null {
  if (!json || !json.trim()) return null;
  try {
    const parsed = JSON.parse(json);
    const result = ProjectCharterSchema.safeParse(parsed);
    return result.success ? result.data : null;
  } catch {
    return null;
  }
}

export function formatCharterForPrompt(charter: ProjectCharter | null): string {
  if (!charter) {
    return "## Project Charter\n(no charter defined yet — CEO should author one via save_charter)";
  }

  const lines: string[] = ["## Project Charter"];

  if (charter.mission) {
    lines.push(`**Mission:** ${charter.mission}`);
  }

  if (charter.goals.length > 0) {
    lines.push("**Goals:**");
    for (const g of charter.goals) lines.push(`- ${g}`);
  }

  if (charter.orgParadigm) {
    lines.push(`**Org paradigm:** ${charter.orgParadigm}`);
  }

  if (charter.roles.length > 0) {
    lines.push("**Defined roles:**");
    for (const r of charter.roles) {
      const perm = r.permissionType ? ` (${r.permissionType})` : "";
      lines.push(`- **${r.role}**${perm}: ${r.description}`);
    }
  }

  if (charter.artifactKinds.length > 0) {
    lines.push("**Artifact kinds:**");
    for (const a of charter.artifactKinds) {
      lines.push(`- ${a.kind}: ${a.description}`);
    }
  }

  if (charter.staffingPolicy) {
    const sp = charter.staffingPolicy;
    const parts: string[] = [];
    if (sp.hiringRequiresCeoApproval) parts.push("hiring requires CEO approval");
    if (sp.defaultParentRole) parts.push(`default parent role: ${sp.defaultParentRole}`);
    if (sp.notes) parts.push(sp.notes);
    if (parts.length > 0) lines.push(`**Staffing policy:** ${parts.join("; ")}`);
  }

  if (charter.constraints.length > 0) {
    lines.push("**Constraints:**");
    for (const c of charter.constraints) lines.push(`- ${c}`);
  }

  if (charter.updatedAt) {
    lines.push(`_Last updated: ${new Date(charter.updatedAt).toISOString()}_`);
  }

  return lines.join("\n");
}
