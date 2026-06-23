/**
 * ClawHubService — Skill registry integration for HiveWeave.
 *
 * Connects to ClawHub (https://clawhub.ai) as the primary skill registry,
 * with automatic fallback to domestic SkillHub when ClawHub is unreachable.
 *
 * Following the OpenClaw skill model: skills are SKILL.md files with YAML
 * frontmatter + markdown instructions. Binding a skill means injecting its
 * instructions into the agent's prompt context.
 *
 * Two registries:
 *   - International (primary): ClawHub — https://clawhub.ai/api/v1/skills
 *   - Domestic (fallback): SkillHub — https://skillhub.tencent.com (Tencent mirror)
 */

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface ClawHubSkill {
  slug: string;
  displayName: string;
  summary: string;
  description: string | null;
  topics: string[];
  tags: Record<string, string>;
  stats: {
    downloads: number;
    installsAllTime: number;
    stars: number;
    versions: number;
  };
  latestVersion?: {
    version: string;
    changelog?: string;
    license?: string | null;
  };
}

export interface ClawHubListResponse {
  items: ClawHubSkill[];
  nextCursor: string | null;
}

export interface SkillDetail extends ClawHubSkill {
  /** Full SKILL.md content (YAML frontmatter + markdown body) */
  skillMd: string;
  /** Parsed metadata from frontmatter (requires_api_key, setup keys, etc.) */
  metadata: {
    setup?: Array<{ key: string; required: boolean }>;
    os?: string[] | null;
    systems?: string | null;
  };
  owner?: {
    handle: string;
    displayName: string;
  };
}

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

const CLAWHUB_API = "https://clawhub.ai/api/v1";
const SKILLHUB_API = "https://skillhub.tencent.com/api/v1"; // Domestic fallback

/** Request timeout in ms — if ClawHub doesn't respond within this, try fallback. */
const REQUEST_TIMEOUT_MS = 8000;

/** In-memory cache TTL in ms (10 minutes). */
const CACHE_TTL_MS = 10 * 60 * 1000;

// ---------------------------------------------------------------------------
// Cache
// ---------------------------------------------------------------------------

interface CacheEntry<T> {
  data: T;
  timestamp: number;
}

// ---------------------------------------------------------------------------
// ClawHubService
// ---------------------------------------------------------------------------

export class ClawHubService {
  private listCache = new Map<string, CacheEntry<ClawHubListResponse>>();
  private detailCache = new Map<string, CacheEntry<SkillDetail>>();

  /**
   * Determine which API base to use. Tries ClawHub first; if it fails or
   * times out, falls back to domestic SkillHub.
   */
  private async resolveApiBase(): Promise<string> {
    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 3000);
      const resp = await fetch(`${CLAWHUB_API}/skills?limit=1`, {
        signal: controller.signal,
      });
      clearTimeout(timer);
      if (resp.ok) return CLAWHUB_API;
    } catch {
      // ClawHub unreachable — fall through to domestic
      console.log("[ClawHub] Primary registry unreachable, falling back to SkillHub");
    }
    return SKILLHUB_API;
  }

  /**
   * Fetch with timeout. Returns null on failure.
   */
  private async fetchWithTimeout(url: string, timeoutMs = REQUEST_TIMEOUT_MS): Promise<Response | null> {
    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeoutMs);
      const resp = await fetch(url, { signal: controller.signal });
      clearTimeout(timer);
      return resp;
    } catch {
      return null;
    }
  }

  /**
   * List/search skills from the registry.
   *
   * @param options.query - Optional search keyword
   * @param options.limit - Max results (default 20)
   * @param options.cursor - Pagination cursor from previous response
   */
  async listSkills(options?: {
    query?: string;
    limit?: number;
    cursor?: string;
  }): Promise<ClawHubListResponse> {
    const limit = options?.limit || 20;
    const query = options?.query || "";
    const cacheKey = `${query}:${limit}:${options?.cursor || ""}`;

    // Check cache
    const cached = this.listCache.get(cacheKey);
    if (cached && Date.now() - cached.timestamp < CACHE_TTL_MS) {
      return cached.data;
    }

    const base = await this.resolveApiBase();

    // IMPORTANT: ClawHub uses two different endpoints:
    //   - /skills  → listing (sorted by activity, ignores q parameter)
    //   - /search  → searching (relevance-ranked, returns {results: [...]})
    const useSearch = !!query;

    let url: string;
    if (useSearch) {
      url = `${base}/search?q=${encodeURIComponent(query)}&limit=${limit}`;
    } else {
      url = `${base}/skills?limit=${limit}`;
      if (options?.cursor) url += `&cursor=${encodeURIComponent(options.cursor)}`;
    }

    const resp = await this.fetchWithTimeout(url);
    if (!resp || !resp.ok) {
      // If primary failed and we haven't tried fallback yet
      if (base === CLAWHUB_API) {
        console.log("[ClawHub] Primary failed, trying SkillHub fallback");
        const fallbackUrl = useSearch
          ? `${SKILLHUB_API}/search?q=${encodeURIComponent(query)}&limit=${limit}`
          : `${SKILLHUB_API}/skills?limit=${limit}`;
        const fallbackResp = await this.fetchWithTimeout(fallbackUrl);
        if (fallbackResp && fallbackResp.ok) {
          const raw = await fallbackResp.json();
          const data = useSearch ? this.parseSearchResponse(raw) : (raw as ClawHubListResponse);
          this.listCache.set(cacheKey, { data, timestamp: Date.now() });
          return data;
        }
      }
      return { items: [], nextCursor: null };
    }

    const raw = await resp.json();
    const data = useSearch ? this.parseSearchResponse(raw) : (raw as ClawHubListResponse);
    this.listCache.set(cacheKey, { data, timestamp: Date.now() });
    return data;
  }

  /**
   * Parse ClawHub /search response into our standard ClawHubListResponse format.
   * Search returns {results: [{score, slug, displayName, summary, downloads, ...}]}
   * which differs from /skills that returns {items: [...], nextCursor}.
   */
  private parseSearchResponse(json: any): ClawHubListResponse {
    const results = json.results || [];
    const items: ClawHubSkill[] = results.map((r: any) => ({
      slug: r.slug || "",
      displayName: r.displayName || r.slug || "",
      summary: r.summary || "",
      description: null,
      topics: [],
      tags: {},
      stats: {
        downloads: r.downloads || 0,
        installsAllTime: 0,
        stars: 0,
        versions: 0,
      },
      latestVersion: r.version ? { version: r.version } : undefined,
    }));
    return { items, nextCursor: null };
  }

  /**
   * Get detailed info for a specific skill, including full SKILL.md content.
   *
   * @param slug - The skill slug (e.g. "pixellab-ai")
   */
  async getSkillDetail(slug: string): Promise<SkillDetail | null> {
    // Check cache
    const cached = this.detailCache.get(slug);
    if (cached && Date.now() - cached.timestamp < CACHE_TTL_MS) {
      return cached.data;
    }

    const base = await this.resolveApiBase();
    const resp = await this.fetchWithTimeout(`${base}/skills/${slug}`);

    if (!resp || !resp.ok) {
      // Try fallback
      if (base === CLAWHUB_API) {
        const fallbackResp = await this.fetchWithTimeout(`${SKILLHUB_API}/skills/${slug}`);
        if (fallbackResp && fallbackResp.ok) {
          return this.parseDetailResponse(await fallbackResp.json(), slug);
        }
      }
      return null;
    }

    return this.parseDetailResponse(await resp.json(), slug);
  }

  private parseDetailResponse(json: any, slug: string): SkillDetail {
    const skill = json.skill || json;
    const detail: SkillDetail = {
      slug: skill.slug || slug,
      displayName: skill.displayName || slug,
      summary: skill.summary || "",
      description: skill.description || null,
      topics: skill.topics || [],
      tags: skill.tags || {},
      stats: skill.stats || { downloads: 0, installsAllTime: 0, stars: 0, versions: 0 },
      latestVersion: skill.latestVersion || json.latestVersion,
      skillMd: skill.description || "",
      metadata: json.metadata || { setup: [], os: null, systems: null },
      owner: json.owner ? { handle: json.owner.handle, displayName: json.owner.displayName } : undefined,
    };

    this.detailCache.set(slug, { data: detail, timestamp: Date.now() });
    return detail;
  }

  /**
   * Build a compact skills summary block for injection into agent context prompts.
   *
   * Follows the OpenClaw **progressive disclosure** pattern:
   *   - Phase 1 (session start): inject only name + summary per skill (~50 tokens each)
   *   - Phase 2 (on demand): agent uses `read_skill` tool to load full SKILL.md
   *   - Phase 3 (execution): agent follows the loaded instructions
   *
   * This keeps context consumption low regardless of how many skills are bound.
   *
   * @param slugs - Array of skill slugs (e.g. ["clawseccheck", "pixellab-ai"])
   * @returns Formatted markdown summary string, or empty string if no skills bound.
   */
  async buildSkillsBlock(slugs: string[]): Promise<string> {
    if (!slugs || slugs.length === 0) return "";

    const lines: string[] = [];

    for (const slug of slugs) {
      try {
        const detail = await this.getSkillDetail(slug);
        if (detail) {
          const version = detail.latestVersion?.version || "";
          const summary = detail.summary || "No description available.";
          lines.push(`- **${detail.displayName}** (\`${slug}\`)${version ? ` v${version}` : ""} — ${summary}`);
        } else {
          lines.push(`- **${slug}** — (skill not found in registry)`);
        }
      } catch {
        lines.push(`- **${slug}** — (failed to load skill info)`);
      }
    }

    if (lines.length === 0) return "";

    return [
      "## Active Skills",
      "The following skills are bound to you. Only summaries are shown here to save context.",
      "**Use the `read_skill` tool to load a skill's full instructions when you need them.**",
      "",
      ...lines,
    ].join("\n");
  }

  /**
   * Search skills by keyword. Convenience wrapper around listSkills.
   */
  async searchSkills(query: string, limit = 10): Promise<ClawHubSkill[]> {
    const result = await this.listSkills({ query, limit });
    return result.items;
  }

  /**
   * Clear all caches. Useful when testing or after config changes.
   */
  clearCache(): void {
    this.listCache.clear();
    this.detailCache.clear();
  }
}

/** Singleton instance shared across the application. */
export const clawhubService = new ClawHubService();
