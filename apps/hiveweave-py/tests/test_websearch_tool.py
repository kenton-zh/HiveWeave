"""websearch tool — keyless web search cascade + failure cooldown.

回归重点：
- Brave/DDG 在本环境直连不可达（ConnectTimeout），级联每次固定空等
  15s×2 = 30s 才轮到 Bing —— 修复：失败后端进入冷却（BACKEND_COOLDOWN_S），
  冷却期内直接跳过，Bing 兜底立即返回。
- snippet/title 里的 HTML 实体（&ensp; &#0183; &amp; 等）必须解码。
- 全后端失败显式报错，不伪装成 "No results found"。
"""

from __future__ import annotations

import time

import pytest

from hiveweave.tools import websearch as ws
from hiveweave.tools.websearch import (
    _extract_snippet,
    _format_results,
    execute_websearch,
)


@pytest.fixture(autouse=True)
def _reset_cooldown():
    ws._backend_cooldown_until.clear()
    yield
    ws._backend_cooldown_until.clear()


# ── 冷却熔断 ───────────────────────────────────────────────


def test_cooldown_skips_failed_backend(monkeypatch):
    """失败后端进入冷却：冷却期内不再次调用。"""
    ws._mark_backend_down("brave")
    assert ws._backend_in_cooldown("brave") is True
    assert ws._backend_in_cooldown("duckduckgo") is False


def test_cooldown_expires_after_window():
    """冷却到期后恢复尝试。"""
    ws._backend_cooldown_until["brave"] = time.monotonic() - 1.0
    assert ws._backend_in_cooldown("brave") is False


@pytest.mark.asyncio
async def test_search_skips_cooled_backends_and_uses_bing(monkeypatch):
    """Brave/DDG 冷却中 → 直接走 Bing，不等超时。"""
    called = {"brave": 0, "ddg": 0, "bing": 0}

    async def fake_brave(client, q, limit):
        called["brave"] += 1
        raise RuntimeError("brave unreachable")

    async def fake_ddg(client, q, limit):
        called["ddg"] += 1
        raise RuntimeError("ddg unreachable")

    async def fake_bing(client, q, limit):
        called["bing"] += 1
        return [ws.SearchResult(title="t", url="https://x.dev", snippet="s", source="bing")]

    monkeypatch.setattr(ws, "_brave_search", fake_brave)
    monkeypatch.setattr(ws, "_duckduckgo_search", fake_ddg)
    monkeypatch.setattr(ws, "_bing_search", fake_bing)

    # 第一次：brave/ddg 各失败一次并进入冷却
    r1 = await ws._search_keyless("q", 5)
    assert r1[0]["source"] == "bing"
    assert called == {"brave": 1, "ddg": 1, "bing": 1}
    assert ws._backend_in_cooldown("brave")
    assert ws._backend_in_cooldown("duckduckgo")

    # 第二次：冷却命中，直接 bing，不再碰 brave/ddg
    r2 = await ws._search_keyless("q", 5)
    assert r2[0]["source"] == "bing"
    assert called == {"brave": 1, "ddg": 1, "bing": 2}


@pytest.mark.asyncio
async def test_all_backends_failed_raises_not_empty(monkeypatch):
    """全后端失败 → 显式报错（不伪装成无结果）。"""
    async def boom(client, q, limit):
        raise RuntimeError("nope")

    monkeypatch.setattr(ws, "_brave_search", boom)
    monkeypatch.setattr(ws, "_duckduckgo_search", boom)
    monkeypatch.setattr(ws, "_bing_search", boom)

    with pytest.raises(RuntimeError, match="All search backends failed"):
        await ws._search_keyless("q", 5)


# ── 实体解码 + snippet ────────────────────────────────────


def test_extract_snippet_unescapes_entities_and_trims():
    raw = "&ensp;&#0183;&ensp;FastAPI is a modern framework &amp; easy to use. " * 5
    out = _extract_snippet(raw, "FastAPI")
    assert "&amp;" not in out
    assert "&ensp;" not in out
    assert "FastAPI is a modern framework & easy to use." in out
    assert len(out) <= ws.MAX_SNIPPET_CHARS


def test_extract_snippet_strips_title_prefix():
    out = _extract_snippet("FastAPI - Docs FastAPI docs here", "FastAPI - Docs")
    assert out == "FastAPI docs here"


def test_format_results_empty_and_filled():
    assert _format_results([]) == "No results found."
    out = _format_results([ws.SearchResult(
        title="T", url="https://t.dev", snippet="s", source="bing")])
    assert "**T**" in out and "https://t.dev" in out and "bing" in out


# ── 后端 HTML 解析（Bing 为当前环境主路径）─────────────────


def test_bing_parse_unescapes_title(monkeypatch):
    """Bing 页面解析：标题实体解码、snippet 提取、非 http 链接过滤。"""
    html = """
    <ol id="b_results">
      <li class="b_algo">
        <h2><a href="https://example.com/a">FastAPI &amp; Co</a></h2>
        <div class="b_caption"><p>The best &lt;framework&gt; ever.</p></div>
      </li>
      <li class="b_algo">
        <h2><a href="/local">Relative Link</a></h2>
        <p>Should be skipped.</p>
      </li>
      <li class="b_algo">
        <h2><a href="https://example.com/b">Another Result</a></h2>
        <p>Second snippet.</p>
      </li>
    </ol>
    """

    class _FakeResp:
        status_code = 200
        text = html

    class _FakeClient:
        async def get(self, url, **kw):
            return _FakeResp()

    results = __import__("asyncio").get_event_loop().run_until_complete(
        ws._bing_search(_FakeClient(), "q", 5)
    )
    assert [r["title"] for r in results] == ["FastAPI & Co", "Another Result"]
    assert results[0]["snippet"] == "The best <framework> ever."
    assert results[0]["url"] == "https://example.com/a"


@pytest.mark.asyncio
async def test_execute_websearch_success_and_error_paths(monkeypatch):
    """execute_websearch：正常返回 / 全后端失败返回 error。"""
    monkeypatch.setattr(ws, "_search_keyless", _async_ret([ws.SearchResult(
        title="T", url="https://t.dev", snippet="s", source="bing")]))
    r = await execute_websearch("query")
    assert r["success"] is True and "https://t.dev" in r["output"]

    async def boom(q, limit):
        raise RuntimeError("All search backends failed: x")

    monkeypatch.setattr(ws, "_search_keyless", boom)
    r = await execute_websearch("query")
    assert r["success"] is False and "Search failed" in r["error"]

    r = await execute_websearch("")
    assert r["success"] is False and "query is required" in r["error"]


def _async_ret(value):
    async def inner(*args, **kwargs):
        return value
    return inner
