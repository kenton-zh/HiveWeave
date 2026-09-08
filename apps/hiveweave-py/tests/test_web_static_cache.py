"""index.html no-cache / assets immutable 缓存纪律（EXE 白屏根因修复 09-08）。

根因链见 api/router.WebDistStaticFiles docstring：无 Cache-Control →
WebView2 启发式缓存跨构建直出旧 index.html → 旧 hash 懒加载 chunk 404 →
React.lazy 拒绝卸根白屏。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hiveweave.api.router import WebDistStaticFiles, _root


def _make_dist(tmp_path: Path) -> Path:
    web = tmp_path / "web"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text(
        "<!DOCTYPE html><html><body>hw</body></html>", encoding="utf-8"
    )
    (web / "assets" / "app-abc123.js").write_text("console.log(1)", encoding="utf-8")
    (web / "favicon.ico").write_bytes(b"\x00\x00\x01\x00")
    return web


def _app_with_mount(web: Path) -> FastAPI:
    app = FastAPI()
    app.mount("/", WebDistStaticFiles(directory=str(web), html=True), name="web")
    return app


def test_index_html_gets_no_cache(tmp_path: Path):
    client = TestClient(_app_with_mount(_make_dist(tmp_path)))
    r = client.get("/index.html")
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "no-cache"


def test_root_directory_serves_index_with_no_cache(tmp_path: Path):
    """html=True 目录回落到 index.html 时同样 no-cache（含 _root 分流外路径）。"""
    client = TestClient(_app_with_mount(_make_dist(tmp_path)))
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "no-cache"


def test_hashed_assets_get_immutable_cache(tmp_path: Path):
    client = TestClient(_app_with_mount(_make_dist(tmp_path)))
    r = client.get("/assets/app-abc123.js")
    assert r.status_code == 200
    assert (
        r.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    )


def test_non_asset_files_keep_default_headers(tmp_path: Path):
    client = TestClient(_app_with_mount(_make_dist(tmp_path)))
    r = client.get("/favicon.ico")
    assert r.status_code == 200
    assert "Cache-Control" not in r.headers


def test_conditional_request_304_keeps_cache_control(tmp_path: Path):
    """304 协商路径也必须保留 Cache-Control（依赖 starlette
    NotModifiedResponse 的保留头集合；升级若改此内部路径须报警）。"""
    client = TestClient(_app_with_mount(_make_dist(tmp_path)))
    first = client.get("/index.html")
    etag = first.headers["ETag"]
    cached = client.get(
        "/index.html", headers={"If-None-Match": etag}
    )
    assert cached.status_code == 304
    assert cached.headers["Cache-Control"] == "no-cache"


@pytest.mark.asyncio
async def test_root_route_serves_index_with_no_cache(
    tmp_path: Path, monkeypatch
):
    """_root 分流（注册期优先于 Mount）也必须 no-cache。"""
    web = _make_dist(tmp_path)
    monkeypatch.setattr(
        "hiveweave.config.resolve_web_dist", lambda: web
    )
    resp = await _root()
    assert resp.headers["Cache-Control"] == "no-cache"
