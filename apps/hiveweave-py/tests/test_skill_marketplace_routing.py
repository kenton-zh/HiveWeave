"""Skill marketplace routing — skills.sh → SkillHub 降级一致性。

回归测试：hire_agent 校验 / read_skill / get_skill_detail 的市场解析必须与
list_available_skills 的搜索路由保持同一契约——skills.sh（国外）优先，不可达
（_fetch_skills_sh_detail 返回 None）时自动降级到 SkillHub（国内）。

修复前的 bug：hire_agent 校验只调 _fetch_skills_sh_detail，无 SkillHub 降级，
导致国内商店技能「搜得到却绑不上」（被误判 invalid）。
"""

from __future__ import annotations

import pytest

from hiveweave.services.skill_registry import (
    SKILLHUB_SOURCE_LABEL,
    SkillRegistryService,
)


@pytest.fixture(autouse=True)
def _clear_skillhub_cache():
    SkillRegistryService._skillhub_detail_cache.clear()
    yield
    SkillRegistryService._skillhub_detail_cache.clear()


def _sh_detail(slug: str) -> dict:
    return {"slug": slug, "summary": "from skills.sh", "skill_md": f"# {slug}\n\nsh body"}


def _hub_detail(slug: str) -> dict:
    return {"slug": slug, "summary": "from skillhub", "skill_md": f"# {slug}\n\nhub body"}


# ── _resolve_marketplace_skill 路由 ──────────────────────────


@pytest.mark.asyncio
async def test_resolve_prefers_skills_sh_when_reachable(monkeypatch):
    svc = SkillRegistryService()
    monkeypatch.setattr(
        svc, "_fetch_skills_sh_detail",
        _async(_sh_detail("frontend")),
    )
    called = {"hub": False}

    async def hub(slug):
        called["hub"] = True
        return _hub_detail(slug)

    monkeypatch.setattr(svc, "_fetch_skillhub_detail", hub)

    detail, label = await svc._resolve_marketplace_skill("frontend")
    assert detail is not None and detail["summary"] == "from skills.sh"
    assert label == "skills.sh Marketplace"
    assert called["hub"] is False, "skills.sh 可达时不应触发国内降级"


@pytest.mark.asyncio
async def test_resolve_falls_back_to_skillhub_when_sh_unreachable(monkeypatch):
    svc = SkillRegistryService()
    monkeypatch.setattr(svc, "_fetch_skills_sh_detail", _async(None))
    monkeypatch.setattr(
        svc, "_fetch_skillhub_detail",
        _async(_hub_detail("frontend")),
    )

    detail, label = await svc._resolve_marketplace_skill("frontend")
    assert detail is not None and detail["summary"] == "from skillhub"
    assert label == SKILLHUB_SOURCE_LABEL


@pytest.mark.asyncio
async def test_resolve_none_when_both_unavailable(monkeypatch):
    svc = SkillRegistryService()
    monkeypatch.setattr(svc, "_fetch_skills_sh_detail", _async(None))
    monkeypatch.setattr(svc, "_fetch_skillhub_detail", _async(None))

    detail, label = await svc._resolve_marketplace_skill("nope")
    assert detail is None
    assert label == ""


# ── read_skill 运行时加载（绑定后必须能读到）─────────────────


@pytest.mark.asyncio
async def test_read_skill_loads_via_skillhub_fallback(monkeypatch):
    svc = SkillRegistryService()
    monkeypatch.setattr(svc, "_fetch_skills_sh_detail", _async(None))
    monkeypatch.setattr(
        svc, "_fetch_skillhub_detail",
        _async(_hub_detail("frontend")),
    )

    text = await svc.read_skill("frontend")
    assert "hub body" in text
    assert "not found" not in text


@pytest.mark.asyncio
async def test_read_skill_builtin_short_circuits_market(monkeypatch):
    """内置技能直接命中，不触发任何市场抓取。"""
    svc = SkillRegistryService()

    async def boom(slug):
        raise AssertionError("builtin skill must not touch marketplace")

    monkeypatch.setattr(svc, "_fetch_skills_sh_detail", boom)
    monkeypatch.setattr(svc, "_fetch_skillhub_detail", boom)

    text = await svc.read_skill("self-review")
    assert "Self-Review Discipline" in text


# ── hire_agent 校验语义（与校验循环同构：内置 → 市场路由）────


async def _hire_validation_accepts(svc, slug: str) -> bool:
    """复刻 org_tools.hire_agent 的校验判定：内置命中或市场可解析 → 接受。"""
    if svc._get_builtin_skill(slug) is not None:
        return True
    detail, _source = await svc._resolve_marketplace_skill(slug)
    return detail is not None


@pytest.mark.asyncio
async def test_hire_validation_accepts_skillhub_skill_when_sh_down(monkeypatch):
    """核心回归：skills.sh 不可达时，国内商店技能不再被误判 invalid。"""
    svc = SkillRegistryService()
    monkeypatch.setattr(svc, "_fetch_skills_sh_detail", _async(None))
    monkeypatch.setattr(
        svc, "_fetch_skillhub_detail",
        _async(_hub_detail("frontend")),
    )

    assert await _hire_validation_accepts(svc, "frontend") is True


@pytest.mark.asyncio
async def test_hire_validation_rejects_unknown_when_both_down(monkeypatch):
    svc = SkillRegistryService()
    monkeypatch.setattr(svc, "_fetch_skills_sh_detail", _async(None))
    monkeypatch.setattr(svc, "_fetch_skillhub_detail", _async(None))

    assert await _hire_validation_accepts(svc, "not-a-real-skill") is False


# ── _fetch_skillhub_detail：requires_api_key 过滤 + 字段解析 ──


class _FakeResp:
    def __init__(self, status: int, payload: dict):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, resp: _FakeResp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kwargs):
        return self._resp


@pytest.mark.asyncio
async def test_fetch_skillhub_detail_filters_requires_api_key(monkeypatch):
    import hiveweave.services.skill_registry as reg

    payload = {"skill": {"slug": "x", "displayName": "X", "summary": "s",
                         "labels": {"requires_api_key": "true"}}}
    monkeypatch.setattr(reg.httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeResp(200, payload)))

    svc = SkillRegistryService()
    assert await svc._fetch_skillhub_detail("x") is None


@pytest.mark.asyncio
async def test_fetch_skillhub_detail_parses_free_skill(monkeypatch):
    import hiveweave.services.skill_registry as reg

    payload = {
        "latestVersion": {"version": "1.0.2"},
        "skill": {"slug": "frontend", "displayName": "Frontend Design",
                  "summary": "React stuff", "labels": {"requires_api_key": "false"}},
    }
    monkeypatch.setattr(reg.httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeResp(200, payload)))

    svc = SkillRegistryService()
    detail = await svc._fetch_skillhub_detail("frontend")
    assert detail is not None
    assert detail["displayName"] == "Frontend Design"
    assert "React stuff" in detail["skill_md"]
    assert "(v1.0.2)" in detail["skill_md"]


def _async(value):
    """构造一个协程函数：调用后返回可 await 的协程，结果为 value。"""
    async def _coro(*args, **kwargs):
        return value
    return _coro
