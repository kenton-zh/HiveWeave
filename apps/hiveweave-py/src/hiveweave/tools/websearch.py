"""websearch tool — keyless web search.

契约 02: 工具执行器 — websearch 子模块
- 无 API key：Brave Search → DuckDuckGo → Bing 级联回退
- 用 httpx 异步客户端
- 返回最多 8 条结果，每条带 title/url/snippet/source
- 支持 HTTPS_PROXY 代理（自动从环境变量读取）
- 超时 15s
- 后端失败熔断：某后端 ConnectTimeout/HTTP 失败后冷却 5 分钟，
  避免每次搜索都空等 15s（如本环境 Brave/DDG 直连不可达，级联
  每次固定多等 30s 才轮到 Bing）
"""

from __future__ import annotations

import html
import re
import time
from typing import Any
from urllib.parse import quote_plus

import httpx
import structlog

log = structlog.get_logger(__name__)

# ── Constants ───────────────────────────────────────────────

MAX_RESULTS = 8
MAX_SNIPPET_CHARS = 160
REQUEST_TIMEOUT_S = 15.0

# 后端失败冷却：一次失败后跳过该后端一段时间（进程内）
BACKEND_COOLDOWN_S = 300.0

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


# 后端失败冷却状态：{backend_name: monotonic 冷却截止时间}
# 模块级而非实例级：同一进程内所有 agent 共享，避免每个 agent
# 第一次搜索都各自空等一遍超时。
_backend_cooldown_until: dict[str, float] = {}


def _backend_in_cooldown(backend: str) -> bool:
    return time.monotonic() < _backend_cooldown_until.get(backend, 0.0)


def _mark_backend_down(backend: str) -> None:
    _backend_cooldown_until[backend] = time.monotonic() + BACKEND_COOLDOWN_S


# ── Snippet helper ─────────────────────────────────────────

def _extract_snippet(raw: str, title: str) -> str:
    text = re.sub(r"\s+", " ", raw).strip()
    if not text or text == title:
        return ""
    if text.startswith(title):
        text = text[len(title):].strip()
    text = html.unescape(text)
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
    page = res.text

    results: list[SearchResult] = []
    seen: set[str] = set()

    # Brave result anchors
    for m in re.finditer(
        r'<a[^>]*data-testid="result-title-a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        page, re.S | re.I,
    ):
        href = m.group(1).strip()
        title = html.unescape(re.sub(r"<[^>]+>", "", m.group(2)))
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
        snippet = _extract_snippet(page[max(0, m.end() - 500):m.end() + 1000],
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
    page = res.text

    results: list[SearchResult] = []
    seen: set[str] = set()

    # DDG result anchors: <a class="result__a" href="...">
    for m in re.finditer(
        r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        page, re.S | re.I,
    ):
        href = m.group(1).strip()
        title = html.unescape(re.sub(r"<[^>]+>", "", m.group(2)))
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
            page[m.end():m.end() + 2000], re.S | re.I,
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
    page = res.text

    results: list[SearchResult] = []
    seen: set[str] = set()

    # Bing: <li class="b_algo">...<h2><a href="...">title</a></h2>...
    for m in re.finditer(
        r'<li[^>]*class="b_algo"[^>]*>(.*?)</li>',
        page, re.S | re.I,
    ):
        block = m.group(1)
        anchor = re.search(
            r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            block, re.S | re.I,
        )
        if not anchor:
            continue
        href = anchor.group(1).strip()
        title = html.unescape(re.sub(r"<[^>]+>", "", anchor.group(2)))
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
    """Try Brave → DuckDuckGo → Bing, returning the first non-empty result.

    失败的后端进入冷却（BACKEND_COOLDOWN_S 内跳过），避免本环境
    Brave/DDG 直连不可达时每次搜索都各空等 15s 超时。
    """
    # connect 单独短超时（5s）：本环境 Brave/DDG 直连不可达（TCP 丢包），
    # connect 等满 15s 太慢；Bing 连接 <1s 不受影响。read 保持 15s。
    timeout = httpx.Timeout(REQUEST_TIMEOUT_S, connect=5.0)
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
        if not _backend_in_cooldown("brave"):
            try:
                r = await _brave_search(client, query, limit)
                if r:
                    return r
            except Exception as exc:  # noqa: BLE001
                log.debug("websearch.brave_failed", error=repr(exc))
                errors.append(f"Brave: {type(exc).__name__}: {exc}")
                _mark_backend_down("brave")

        # 2. DuckDuckGo
        if not _backend_in_cooldown("duckduckgo"):
            try:
                r = await _duckduckgo_search(client, query, limit)
                if r:
                    return r
            except Exception as exc:  # noqa: BLE001
                log.debug("websearch.duckduckgo_failed", error=repr(exc))
                errors.append(f"DuckDuckGo: {type(exc).__name__}: {exc}")
                _mark_backend_down("duckduckgo")

        # 3. Bing (works in China without proxy)
        try:
            return await _bing_search(client, query, limit)
        except Exception as exc:  # noqa: BLE001
            log.warning("websearch.all_backends_failed", error=repr(exc))
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
