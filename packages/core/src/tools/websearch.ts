/**
 * websearch tool — keyless, ported from pi-web-extension.
 * Tries Brave Search first, falls back to DuckDuckGo, then Bing.
 * No API key required. If HTTPS_PROXY is set, uses it; otherwise direct connect.
 */
import { Effect, Schema } from "effect";
import { JSDOM } from "jsdom";

const MAX_RESULTS = 8;
const MAX_SNIPPET_CHARS = 160;

export const WebSearchInput = Schema.Struct({
  query: Schema.String.annotations({ description: "Search query." }),
  numResults: Schema.optional(Schema.Number.pipe(Schema.int(), Schema.positive(), Schema.lessThanOrEqualTo(MAX_RESULTS))).annotations({
    description: `Max results (default 5, max ${MAX_RESULTS}).`,
  }),
});

export interface SearchResult {
  title: string;
  url: string;
  snippet: string;
  source: "brave" | "duckduckgo" | "bing";
}

const browserHeaders = {
  "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
  "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
  "Accept-Language": "en-US,en;q=0.9",
};

const fetchOpts = (extra?: RequestInit): RequestInit => ({
  ...extra,
  headers: { ...browserHeaders, ...extra?.headers },
});

function extractSnippet(raw: string, title: string): string {
  let text = raw.replace(/\s+/g, " ").trim();
  if (!text || text === title) return "";
  if (text.startsWith(title)) text = text.slice(title.length).trim();
  return text.slice(0, MAX_SNIPPET_CHARS);
}

// ── Brave Search ────────────────────────────────────────────

async function braveSearch(query: string, limit: number): Promise<SearchResult[]> {
  const url = new URL("https://search.brave.com/search");
  url.searchParams.set("q", query);
  url.searchParams.set("source", "web");

  const res = await fetch(url.toString(), { headers: browserHeaders });
  if (!res.ok) throw new Error(`Brave: ${res.status}`);
  const html = await res.text();

  const dom = new JSDOM(html, { url: url.toString() });
  const doc = dom.window.document;

  const anchors = Array.from(
    doc.querySelectorAll<HTMLAnchorElement>(
      "a[data-testid='result-title-a'], .snippet.fdb a, .result h2 a, a.heading-serpresult",
    ),
  );

  const results: SearchResult[] = [];
  const seen = new Set<string>();

  for (const anchor of anchors) {
    const href = anchor.href?.trim();
    const title = anchor.textContent?.replace(/\s+/g, " ").trim() ?? "";
    if (!href || !title) continue;
    if (!/^https?:\/\//i.test(href)) continue;
    if (href.includes("search.brave.com")) continue;
    if (seen.has(href)) continue;

    const container = anchor.closest("[data-type='web']") ?? anchor.closest(".snippet") ?? anchor.closest(".fdb") ?? anchor.parentElement;
    const snippet = extractSnippet(container?.textContent ?? "", title);

    seen.add(href);
    results.push({ title, url: href, snippet, source: "brave" });
    if (results.length >= limit) break;
  }

  return results;
}

// ── DuckDuckGo HTML Fallback ────────────────────────────────

async function duckDuckGoSearch(query: string, limit: number): Promise<SearchResult[]> {
  const url = new URL("https://html.duckduckgo.com/html/");
  url.searchParams.set("q", query);

  const res = await fetch(url.toString(), { headers: browserHeaders });
  if (!res.ok) throw new Error(`DuckDuckGo: ${res.status}`);
  const html = await res.text();

  const dom = new JSDOM(html, { url: url.toString() });
  const doc = dom.window.document;

  const seen = new Set<string>();
  const results: SearchResult[] = [];
  const items = Array.from(doc.querySelectorAll(".result"));

  for (const item of items) {
    const anchor = item.querySelector(".result__title a, a.result__a") as HTMLAnchorElement | null;
    if (!anchor) continue;

    const href = anchor.href?.trim();
    const title = anchor.textContent?.replace(/\s+/g, " ").trim() ?? "";
    if (!href || !title) continue;
    if (!/^https?:\/\//i.test(href)) continue;
    if (seen.has(href)) continue;

    const snippetNode = item.querySelector(".result__snippet") ?? item.querySelector(".result__body") ?? item.querySelector(".result__extras");
    const snippet = extractSnippet(snippetNode?.textContent ?? "", title);

    seen.add(href);
    results.push({ title, url: href, snippet, source: "duckduckgo" });
    if (results.length >= limit) break;
  }

  return results;
}

// ── Bing Search (China-friendly, cn.bing.com) ──────────────

async function bingSearch(query: string, limit: number): Promise<SearchResult[]> {
  const url = new URL("https://www.bing.com/search");
  url.searchParams.set("q", query);
  url.searchParams.set("setlang", "en");

  const res = await fetch(url.toString(), {
    headers: browserHeaders,
    redirect: "follow",
  });
  if (!res.ok) throw new Error(`Bing: ${res.status}`);
  const html = await res.text();

  const dom = new JSDOM(html, { url: url.toString() });
  const doc = dom.window.document;

  const seen = new Set<string>();
  const results: SearchResult[] = [];
  const items = Array.from(doc.querySelectorAll("li.b_algo"));

  for (const item of items) {
    const anchor = item.querySelector("h2 a") as HTMLAnchorElement | null;
    if (!anchor) continue;

    const href = anchor.href?.trim();
    const title = anchor.textContent?.replace(/\s+/g, " ").trim() ?? "";
    if (!href || !title) continue;
    if (!/^https?:\/\//i.test(href)) continue;
    if (seen.has(href)) continue;

    const snippetEl = item.querySelector(".b_caption p, .b_lineclamp2");
    const snippet = extractSnippet(snippetEl?.textContent ?? "", title);

    seen.add(href);
    results.push({ title, url: href, snippet, source: "bing" });
    if (results.length >= limit) break;
  }

  return results;
}

// ── Orchestration ───────────────────────────────────────────

async function searchKeyless(query: string, limit: number): Promise<SearchResult[]> {
  // Brave first (global)
  try { const r = await braveSearch(query, limit); if (r.length > 0) return r; } catch { /* fall through */ }
  // DuckDuckGo fallback
  try { const r = await duckDuckGoSearch(query, limit); if (r.length > 0) return r; } catch { /* fall through */ }
  // Bing final fallback (works in China without proxy)
  return bingSearch(query, limit);
}

// ── Format ──────────────────────────────────────────────────

function formatResults(results: SearchResult[]): string {
  if (results.length === 0) return "No results found.";
  return results
    .map((r, i) => `${i + 1}. **${r.title}** (${r.source})\n   ${r.url}\n   ${r.snippet}`)
    .join("\n\n");
}

// ── Effect tool ─────────────────────────────────────────────

export function executeWebSearch(rawInput: Record<string, any>): Effect.Effect<string, Error> {
  return Effect.gen(function* () {
    const input = yield* Schema.decodeUnknown(WebSearchInput)(rawInput).pipe(
      Effect.mapError((e) => new Error(`WebSearch validation: ${e.message}`)),
    );
    const results = yield* Effect.tryPromise({
      try: () => searchKeyless(input.query, input.numResults || 5),
      catch: (err) => new Error(`Search failed: ${err instanceof Error ? err.message : String(err)}`),
    });
    return formatResults(results);
  });
}
