/**
 * WebService — URL fetching and web search for HiveWeave agents.
 *
 * Provides `fetch_url` tool: agents can read web pages, documentation,
 * APIs, and any publicly accessible web resource.
 *
 * Security:
 * - SSRF prevention: DNS resolution checked against private IP ranges
 * - Only http/https protocols allowed
 * - Response size limit (50K chars)
 * - Request timeout (30s default)
 *
 * Inspired by OpenCode's webfetch tool.
 */

import * as dns from "dns";
import TurndownService from "turndown";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface FetchUrlParams {
  url: string;
  maxChars?: number;
}

export interface FetchResult {
  content: string;
  contentType: string;
  statusCode: number;
  truncated: boolean;
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const DEFAULT_MAX_CHARS = 50_000;
const DEFAULT_TIMEOUT_MS = 30_000;
const MAX_RESPONSE_BYTES = 5 * 1024 * 1024; // 5 MB

// ---------------------------------------------------------------------------
// WebService
// ---------------------------------------------------------------------------

const turndown = new TurndownService({
  headingStyle: "atx",
  codeBlockStyle: "fenced",
  bulletListMarker: "-",
});

// Remove noisy elements (scripts, styles, nav, footer, ads)
turndown.remove(["script", "style", "nav", "footer", "header", "iframe", "noscript"]);

export class WebService {
  /**
   * Fetch a URL and return its content as text.
   * HTML is converted to markdown for readability.
   *
   * @param params - URL and optional maxChars limit.
   */
  async fetchUrl(params: FetchUrlParams): Promise<FetchResult> {
    const { url, maxChars = DEFAULT_MAX_CHARS } = params;

    if (!url) {
      throw new Error("fetch_url requires a url parameter.");
    }

    // 1. Validate URL
    const parsed = this.validateUrl(url);

    // 2. SSRF check
    await this.checkSsrf(parsed.hostname);

    // 3. Fetch
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), DEFAULT_TIMEOUT_MS);

    try {
      const response = await fetch(url, {
        signal: controller.signal,
        headers: {
          "User-Agent": "HiveWeave-Agent/1.0 (+https://hiveweave.dev)",
          "Accept": "text/html,application/json,text/plain,*/*",
        },
        redirect: "follow",
      });

      clearTimeout(timer);

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
      }

      const contentType = response.headers.get("content-type") || "";
      const contentLength = parseInt(response.headers.get("content-length") || "0", 10);

      if (contentLength > MAX_RESPONSE_BYTES) {
        throw new Error(
          `Response too large: ${Math.round(contentLength / 1024 / 1024)}MB exceeds 5MB limit.`,
        );
      }

      const body = await response.text();

      // 4. Process based on content type
      let content: string;
      if (contentType.includes("text/html")) {
        content = turndown.turndown(body);
      } else if (contentType.includes("application/json")) {
        try {
          content = JSON.stringify(JSON.parse(body), null, 2);
        } catch {
          content = body;
        }
      } else {
        content = body;
      }

      // 5. Truncate if needed
      const truncated = content.length > maxChars;
      if (truncated) {
        content = content.slice(0, maxChars) + `\n\n[... truncated at ${maxChars} characters ...]`;
      }

      return {
        content,
        contentType: contentType.split(";")[0].trim(),
        statusCode: response.status,
        truncated,
      };
    } catch (err: any) {
      clearTimeout(timer);
      if (err.name === "AbortError") {
        throw new Error(`Request timed out after ${DEFAULT_TIMEOUT_MS / 1000}s.`);
      }
      throw err;
    }
  }

  // -------------------------------------------------------------------------
  // Private helpers
  // -------------------------------------------------------------------------

  private validateUrl(url: string): URL {
    let parsed: URL;
    try {
      parsed = new URL(url);
    } catch {
      throw new Error(`Invalid URL: "${url}"`);
    }

    if (!["http:", "https:"].includes(parsed.protocol)) {
      throw new Error(`Unsupported protocol "${parsed.protocol}". Only http and https are allowed.`);
    }

    return parsed;
  }

  /**
   * SSRF prevention: resolve hostname and check it doesn't point to
   * private/internal IP ranges.
   */
  private async checkSsrf(hostname: string): Promise<void> {
    // Allow localhost for development (agents in dev environment)
    // In production, this should be stricter
    if (hostname === "localhost" || hostname === "127.0.0.1") {
      // Allow for now — agents may need to call local dev servers
      return;
    }

    try {
      const result = await dns.promises.lookup(hostname, { all: false });
      const ip = result.address;

      if (this.isPrivateIp(ip)) {
        throw new Error(
          `SSRF blocked: "${hostname}" resolves to private IP ${ip}. ` +
          `Only public internet URLs are allowed.`,
        );
      }
    } catch (err: any) {
      // If DNS fails, let the fetch handle the error naturally
      if (err.message?.includes("SSRF blocked")) {
        throw err;
      }
    }
  }

  private isPrivateIp(ip: string): boolean {
    const parts = ip.split(".").map(Number);
    if (parts.length !== 4) return false;

    // 10.0.0.0/8
    if (parts[0] === 10) return true;
    // 172.16.0.0/12
    if (parts[0] === 172 && parts[1] >= 16 && parts[1] <= 31) return true;
    // 192.168.0.0/16
    if (parts[0] === 192 && parts[1] === 168) return true;
    // 127.0.0.0/8 (loopback)
    if (parts[0] === 127) return true;
    // 169.254.0.0/16 (link-local)
    if (parts[0] === 169 && parts[1] === 254) return true;
    // 0.0.0.0
    if (parts.every((p) => p === 0)) return true;

    return false;
  }
}
