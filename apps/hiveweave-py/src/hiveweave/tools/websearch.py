"""websearch tool — keyless web search.

契约 02: 工具执行器 — websearch 子模块
- 无 API key：Brave Search → DuckDuckGo → Bing 级联回退
- 用 httpx 异步客户端
- 返回最多 8 条结果，每条带 title/url/snippet/source
- 支持 HTTPS_PROXY 代理（自动从环境变量读取）
- 超时 15s
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote_plus

import httpx
import structlog

log = structlog.get_logger(__name__)

# ── Constants ───────────────────────────────────────────────

MAX_RESULTS = 8
MAX_SNIPPET_CHARS = 160
REQUEST_TIMEOUT_S = 15.0

BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
}


# ── Result type ─────────────────────────────────────────────

class SearchResult(dict):
    """A search result dict with title/url/snippet/source keys."""


# ── Snippet helper ─────────────────────────────────────────

def _extract_snippet(raw: str, title: str) -> str:
    text = re.sub(r"\s+", " ", raw).strip()
    if not text or text == title:
        return ""
    if text.startswith(title):
        text = text[len(title):].strip()
    return text[:MAX_SNIPPET_CHARS]


# ── Search backends ────────────────────────────────────────

async def _brave_search(
    client: httpx.AsyncClient, query: str, limit: int
) -> list[SearchResult]:
    url = ("https://search.brave.com/search?"
           f"q={quote_plus(query)}&source=web")
    res = await client.get(url, headers=BROWSER_HEADERS)
    if res.status_code != 200:
        raise RuntimeError(f"Brave: {res.status_code}")
    html = res.text

    results: list[SearchResult] = []
    seen: set[str] = set()

    # Brave result anchors
    for m in re.finditer(
        r'<a[^>]*data-testid="result-title-a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        html, re.S | re.I,
    ):
        href = m.group(1).strip()
        title = re.sub(r"<[^>]+>", "", m.group(2))
        title = re.sub(r"\s+", " ", title).strip()
        if not href or not title:
            continue
        if not re.match(r"^https?://", href, re.I):
            continue
        if "search.brave.com" in href:
            continue
        if href in seen:
            continue
        # Try to extract snippet from surrounding context
        snippet = _extract_snippet(html[max(0, m.end() - 500):m.end() + 1000],
                                   title)
        seen.add(href)
        results.append(SearchResult(title=title, url=href,
                                    snippet=snippet, source="brave"))
        if len(results) >= limit:
            break
    return results


async def _duckduckgo_search(
    client: httpx.AsyncClient, query: str, limit: int
) -> list[SearchResult]:
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    res = await client.get(url, headers=BROWSER_HEADERS)
    if res.status_code != 200:
        raise RuntimeError(f"DuckDuckGo: {res.status_code}")
    html = res.text

    results: list[SearchResult] = []
    seen: set[str] = set()

    # DDG result anchors: <a class="result__a" href="...">
    for m in re.finditer(
        r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        html, re.S | re.I,
    ):
        href = m.group(1).strip()
        title = re.sub(r"<[^>]+>", "", m.group(2))
        title = re.sub(r"\s+", " ", title).strip()
        if not href or not title:
            continue
        # DDG wraps URLs in a redirect; extract the actual URL
        if "uddg=" in href:
            import urllib.parse as up
            parsed = up.parse_qs(up.urlparse(href).query)
            href = parsed.get("uddg", [href])[0]
        if not re.match(r"^https?://", href, re.I):
            continue
        if href in seen:
            continue
        # Find snippet in the result__snippet element after this anchor
        snippet = ""
        snippet_match = re.search(
            r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
            html[m.end():m.end() + 2000], re.S | re.I,
        )
        if snippet_match:
            snippet = _extract_snippet(
                re.sub(r"<[^>]+>", "", snippet_match.group(1)), title
            )
        seen.add(href)
        results.append(SearchResult(title=title, url=href,
                                    snippet=snippet, source="duckduckgo"))
        if len(results) >= limit:
            break
    return results


async def _bing_search(
    client: httpx.AsyncClient, query: str, limit: int
) -> list[SearchResult]:
    url = (f"https://www.bing.com/search?q={quote_plus(query)}"
           "&setlang=en")
    res = await client.get(url, headers=BROWSER_HEADERS,
                           follow_redirects=True)
    if res.status_code != 200:
        raise RuntimeError(f"Bing: {res.status_code}")
    html = res.text

    results: list[SearchResult] = []
    seen: set[str] = set()

    # Bing: <li class="b_algo">...<h2><a href="...">title</a></h2>...
    for m in re.finditer(
        r'<li[^>]*class="b_algo"[^>]*>(.*?)</li>',
        html, re.S | re.I,
    ):
        block = m.group(1)
        anchor = re.search(
            r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            block, re.S | re.I,
        )
        if not anchor:
            continue
        href = anchor.group(1).strip()
        title = re.sub(r"<[^>]+>", "", anchor.group(2))
        title = re.sub(r"\s+", " ", title).strip()
        if not href or not title:
            continue
        if not re.match(r"^https?://", href, re.I):
            continue
        if href in seen:
            continue
        # Snippet
        snippet = ""
        snippet_match = re.search(
            r'<p[^>]*>(.*?)</p>', block, re.S | re.I,
        )
        if snippet_match:
            snippet = _extract_snippet(
                re.sub(r"<[^>]+>", "", snippet_match.group(1)), title
            )
        seen.add(href)
        results.append(SearchResult(title=title, url=href,
                                    snippet=snippet, source="bing"))
        if len(results) >= limit:
            break
    return results


async def _search_keyless(
    query: str, limit: int
) -> list[SearchResult]:
    """Try Brave → DuckDuckGo → Bing, returning the first non-empty result."""
    timeout = httpx.Timeout(REQUEST_TIMEOUT_S)
    # Respect HTTPS_PROXY env var if set
    proxy_url = None
    import os
    proxy_env = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    if proxy_env:
        proxy_url = proxy_env

    async with httpx.AsyncClient(
        timeout=timeout, proxy=proxy_url
    ) as client:
        errors: list[str] = []

        # 1. Brave
        try:
            r = await _brave_search(client, query, limit)
            if r:
                return r
        except Exception as exc:  # noqa: BLE001
            log.debug("websearch.brave_failed", error=str(exc))
            errors.append(f"Brave: {type(exc).__name__}: {exc}")

        # 2. DuckDuckGo
        try:
            r = await _duckduckgo_search(client, query, limit)
            if r:
                return r
        except Exception as exc:  # noqa: BLE001
            log.debug("websearch.duckduckgo_failed", error=str(exc))
            errors.append(f"DuckDuckGo: {type(exc).__name__}: {exc}")

        # 3. Bing (works in China without proxy)
        try:
            return await _bing_search(client, query, limit)
        except Exception as exc:  # noqa: BLE001
            log.warning("websearch.all_backends_failed", error=str(exc))
            errors.append(f"Bing: {type(exc).__name__}: {exc}")

        # 所有后端全挂 → 显式失败，不要伪装成"无结果"
        # 否则 LLM 会误以为查询太冷门而反复换词搜索
        raise RuntimeError(
            f"All search backends failed: {'; '.join(errors)}"
        )


def _format_results(results: list[SearchResult]) -> str:
    if not results:
        return "No results found."
    parts: list[str] = []
    for i, r in enumerate(results, 1):
        parts.append(
            f'{i}. **{r["title"]}** ({r["source"]})\n'
            f'   {r["url"]}\n'
            f'   {r["snippet"]}'
        )
    return "\n\n".join(parts)


async def execute_websearch(
    query: str,
    num_results: int = 5,
) -> dict[str, Any]:
    """Search the web for `query`. Returns {success, output, error}."""
    if not query or not query.strip():
        return {"success": False, "output": "",
                "error": "Error: query is required"}

    limit = max(1, min(int(num_results or 5), MAX_RESULTS))
    log.info("websearch.execute", query=query[:120], limit=limit)

    try:
        results = await _search_keyless(query, limit)
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "output": "",
                "error": f"Error: Search failed — {exc}"}

    return {"success": True, "output": _format_results(results), "error": None}


# ── Pydantic models + @tool registration (Phase 2 migration) ──────

from pydantic import BaseModel, Field, ConfigDict

from .base import tool
from .result import ToolResult


class WebSearchParams(BaseModel):
    """Parameters for websearch tool."""
    model_config = ConfigDict(populate_by_name=True)

    query: str = Field(
        description="Search query string.",
        json_schema_extra={"aliases": ["search", "q", "term"]},
    )
    num_results: int = Field(
        default=5,
        ge=1,
        le=8,
        description="Number of results to return (1-8). Default: 5.",
        json_schema_extra={"aliases": ["limit", "count"]},
    )


@tool(
    "websearch",
    "Search the web for information. Returns results with title, URL, and snippet from Brave/DuckDuckGo/Bing.",
    requires_workspace=False,
    security_level="standard",
)
async def websearch_tool(params: WebSearchParams, agent_id: str, workspace: str) -> ToolResult:
    """Search the web for a query."""
    result = await execute_websearch(
        query=params.query,
        num_results=params.num_results,
    )
    if result.get("success"):
        return ToolResult.ok(result["output"])
    return ToolResult.err(result.get("error", "Unknown error"))
