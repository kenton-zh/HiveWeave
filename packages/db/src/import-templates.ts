/**
 * Import agent templates from the agency-agents repository.
 *
 * Usage:
 *   npx tsx packages/db/src/import-templates.ts <path-to-agency-agents-repo>
 *
 * The script:
 *   1. Scans all division directories (engineering, design, marketing, etc.)
 *   2. Parses YAML frontmatter + markdown body from each .md file
 *   3. Maps division → HiveWeave role
 *   4. Inserts into agent_templates table (replaces existing agency-agents entries)
 *   5. Inserts a custom HiveWeave AI-HR template
 */

import * as fs from "fs";
import * as path from "path";
import { randomUUID } from "crypto";
import { db } from "./client.js";
import { agentTemplates } from "./schema/agent-templates.js";
import { eq } from "drizzle-orm";

// ── Division → Role mapping ──────────────────────────────────────
const DIVISION_ROLE_MAP: Record<string, string> = {
  "engineering": "developer",
  "testing": "qa",
  "design": "designer",
  "product": "manager",
  "project-management": "manager",
  "marketing": "marketing",
  "sales": "sales",
  "security": "security",
  "finance": "finance",
  "game-development": "developer",
  "academic": "researcher",
  "gis": "specialist",
  "paid-media": "marketing",
  "spatial-computing": "developer",
  "specialized": "specialist",
  "support": "support",
  "strategy": "manager",
};

// ── YAML frontmatter parser (no dependencies) ────────────────────
interface Frontmatter {
  name: string;
  description: string;
  color: string;
  emoji: string;
  vibe: string;
  [key: string]: string;
}

function parseFrontmatter(content: string): { meta: Frontmatter; body: string } {
  const meta: Frontmatter = { name: "", description: "", color: "", emoji: "", vibe: "" };

  // Match --- delimited frontmatter block at the start
  const match = content.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/);
  if (!match) {
    return { meta, body: content };
  }

  const yamlBlock = match[1];
  const body = match[2];

  // Parse simple key: value pairs (handles multi-line with continuation)
  let currentKey = "";
  let currentValue = "";

  for (const line of yamlBlock.split("\n")) {
    const trimmed = line.trimEnd();
    if (!trimmed || trimmed.startsWith("#")) continue;

    // Check for "key: value" pattern
    const kvMatch = trimmed.match(/^([a-zA-Z_][\w-]*)\s*:\s*(.*)$/);
    if (kvMatch) {
      // Save previous key
      if (currentKey) {
        meta[currentKey] = currentValue.trim();
      }
      currentKey = kvMatch[1];
      currentValue = kvMatch[2];
    } else if (currentKey && (trimmed.startsWith("  ") || trimmed.startsWith("\t"))) {
      // Continuation line
      currentValue += " " + trimmed.trim();
    }
  }
  // Save last key
  if (currentKey) {
    meta[currentKey] = currentValue.trim();
  }

  return { meta, body };
}

// ── Division discovery (from divisions.json or directory scan) ────
function discoverDivisions(repoPath: string): string[] {
  const divisionsJsonPath = path.join(repoPath, "divisions.json");
  if (fs.existsSync(divisionsJsonPath)) {
    try {
      const data = JSON.parse(fs.readFileSync(divisionsJsonPath, "utf-8"));
      if (data.divisions) {
        return Object.keys(data.divisions);
      }
    } catch {
      // Fall through to directory scan
    }
  }

  // Fallback: scan directories
  return fs.readdirSync(repoPath, { withFileTypes: true })
    .filter(d => d.isDirectory() && !d.name.startsWith(".") && d.name !== "scripts" && d.name !== "examples" && d.name !== "integrations" && d.name !== "i18n")
    .map(d => d.name);
}

// ── Custom AI-HR template ────────────────────────────────────────
const HIVWEAVE_HR_TEMPLATE = {
  source: "hiveweave",
  division: "hr",
  name: "AI 团队架构师",
  role: "hr",
  color: "blue",
  emoji: "🏗️",
  vibe: "为 HiveWeave 项目组建高效的 AI agent 团队",
  description: "专业的 AI agent 团队架构师，擅长根据项目需求设计合理的团队结构、选择最佳角色模板、管理 agent 全生命周期。",
  promptBody: `# AI 团队架构师

## 身份定位
你是一名专业的 AI agent 团队架构师，负责为 HiveWeave 项目组建和管理高效的 AI agent 团队。你的工作不是管理人类员工，而是设计和构建 AI agent 的组织架构。

## 核心职责

### 1. 需求分析
- 理解项目目标和范围，确定需要哪些专业角色的 agent
- 评估任务复杂度，决定需要 coordinator（协调者）还是 executor（执行者）
- 识别角色之间的协作关系和依赖

### 2. 模板选型
- 使用 browse_templates 浏览可用的人才模板库
- 根据项目需求选择最匹配的角色模板
- 评估模板的 division、role、vibe 是否与需求契合
- 必要时用 create_from_template 快速创建 agent

### 3. 范式匹配与团队设计
你需要根据项目特征选择合适的组织范式，然后基于范式设计团队结构。内置范式包括：
- **单兵模式** — 1人，极小目标明确的任务
- **扁平小组** — 2-5人平级协作，无协调层
- **Tech Lead 制** — 技术负责人 + 执行者，纯技术项目
- **PM + 架构师** — 双线领导，中大型多领域项目
- **Pod/小组制** — 自治子团队，大型项目
- **流水线** — 阶段顺序推进，严格依赖

范式是参考基线，不是死规矩。你可以根据项目需要裁剪、混搭或微调。

### 4. 生命周期管理
- 创建 agent 时设置清晰的 goal 和 backstory
- 定期审视团队结构，transfer 或 dismiss 不再需要的 agent
- 保持团队精简，避免创建冗余 agent
- 用 read_roster 和 list_all_agents 掌握团队全貌

### 5. 编制维护
- 为每个 agent 维护准确的 position（岗位）、department（部门）、responsibilities（职责）
- 及时更新 agent 状态（active/inactive/probation）

## 铁律
- **HR 绝不能创建自己作为 parent 的 agent**。你是人事服务角色，不是组织管理者。
- parentId 绝不能指向自己。新 agent 应该放在 root-level（parentId=null）或其他 coordinator 下。
- 你不拥有任何下属，你服务于整个组织。

## 工作流程（灵活执行，不要死板）

1. **了解项目** — 需求不清楚就问。但如果项目信息已经有了，或用户已经描述过，就别再问了。
2. **选范式** — 根据项目特征选最合适的范式，简短告诉用户你选了什么、为什么（一句话就够）。
3. **建团队** — 按范式指导创建 agent，设好 parentId 和 roster。

**关键原则 — 读懂用户意图：**
- 用户说"开始招人"、"直接建"、"你看着办" → **别废话，直接建。** 可以边建边用一句话说明你选了哪个范式。
- 用户明确说了要哪些角色 → **直接创建那些角色**，不要先提案再确认。
- 用户指定了某个范式 → **直接用**，不要推荐别的。
- 只有"帮我建个团队"这种真正模糊的请求才走完整的提案确认流程。
- **确认过的事情不要再确认。** 用户说OK了就直接执行。

## 成功指标
- 团队结构清晰，每个 agent 职责明确
- 无冗余 agent，所有人都有事做
- 层级合理，信息传递高效
- 编制表完整准确`,
  originalFile: "hiveweave-ai-hr-architect.md",
};

// ── Main import logic ────────────────────────────────────────────
async function main() {
  const repoPath = process.argv[2];
  if (!repoPath) {
    console.error("Usage: npx tsx packages/db/src/import-templates.ts <path-to-agency-agents-repo>");
    process.exit(1);
  }

  if (!fs.existsSync(repoPath)) {
    console.error(`Error: Path does not exist: ${repoPath}`);
    process.exit(1);
  }

  console.log(`Importing templates from: ${repoPath}`);

  // 1. Clear existing agency-agents entries (keep hiveweave custom ones)
  const deleted = await db
    .delete(agentTemplates)
    .where(eq(agentTemplates.source, "agency-agents"));
  console.log(`Cleared existing agency-agents entries.`);

  // 2. Discover and process divisions
  const divisions = discoverDivisions(repoPath);
  console.log(`Found ${divisions.length} divisions: ${divisions.join(", ")}`);

  let totalImported = 0;
  const now = Date.now();

  for (const division of divisions) {
    const divPath = path.join(repoPath, division);
    if (!fs.existsSync(divPath) || !fs.statSync(divPath).isDirectory()) continue;

    const files = fs.readdirSync(divPath).filter(f => f.endsWith(".md"));
    const role = DIVISION_ROLE_MAP[division] || "specialist";

    for (const file of files) {
      const filePath = path.join(divPath, file);
      const content = fs.readFileSync(filePath, "utf-8");
      const { meta, body } = parseFrontmatter(content);

      const name = meta.name || file.replace(".md", "").replace(/^[^-]*-/, "").replace(/-/g, " ");

      await db.insert(agentTemplates).values({
        id: randomUUID(),
        source: "agency-agents",
        division,
        name,
        role,
        color: meta.color || "",
        emoji: meta.emoji || "",
        vibe: meta.vibe || "",
        description: meta.description || "",
        promptBody: body.trim(),
        originalFile: file,
        createdAt: now,
      });

      totalImported++;
    }
    console.log(`  ${division}: ${files.length} agents (role=${role})`);
  }

  // 3. Insert custom HiveWeave AI-HR template (upsert by source+division+name)
  const existingHr = await db
    .select({ id: agentTemplates.id })
    .from(agentTemplates)
    .where(eq(agentTemplates.source, "hiveweave"));

  if (existingHr.length > 0) {
    await db.delete(agentTemplates).where(eq(agentTemplates.source, "hiveweave"));
  }

  await db.insert(agentTemplates).values({
    id: randomUUID(),
    ...HIVWEAVE_HR_TEMPLATE,
    createdAt: now,
  });
  console.log(`  hiveweave: 1 custom AI-HR template`);

  console.log(`\nImport complete: ${totalImported} agency-agents + 1 hiveweave = ${totalImported + 1} total templates.`);
}

main().catch(err => {
  console.error("Import failed:", err);
  process.exit(1);
});
