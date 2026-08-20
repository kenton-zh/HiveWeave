"""Marketplace list/bind must share a store-scoped identity.

TEST_DSH_07: HR listed skills.sh owner/repo/skill slugs, then hire sent those
ids to SkillHub (405 nested path, 404 last segment) because a later search
timeout marked the whole skills.sh store unreachable. skills.sh HTML detail
for the listed slugs was 200 — we never tried it.
"""

from __future__ import annotations

import httpx
import pytest

from hiveweave.services.skill_registry import (
    STORE_SKILLS_SH_DETAIL,
    STORE_SKILLS_SH_SEARCH,
    SkillRegistryService,
)


def _async(value):
    async def _fn(*_a, **_k):
        return value
    return _fn


@pytest.fixture(autouse=True)
def _clear_skill_caches():
    SkillRegistryService._resolve_cache.clear()
    SkillRegistryService._store_unreachable.clear()
    SkillRegistryService._skill_search_source.clear()
    yield
    SkillRegistryService._resolve_cache.clear()
    SkillRegistryService._store_unreachable.clear()
    SkillRegistryService._skill_search_source.clear()


@pytest.mark.asyncio
async def test_search_timeout_does_not_skip_skills_sh_detail(monkeypatch):
    """一次 /api/search 超时不得让已列出的 slug 在 bind 时跳过 HTML 详情。"""
    svc = SkillRegistryService()
    svc._store_mark_unreachable(STORE_SKILLS_SH_SEARCH)
    called = {"n": 0}

    async def fake_detail(slug):
        called["n"] += 1
        return {
            "slug": slug,
            "summary": "from skills.sh",
            "description": "",
            "skill_md": "# ok",
            "requires_api_key": False,
        }

    monkeypatch.setattr(svc, "_fetch_skills_sh_detail", fake_detail)
    monkeypatch.setattr(svc, "_fetch_skillhub_detail", _async(None))

    detail, label = await svc._resolve_marketplace_skill(
        "softaworks/agent-toolkit/react-dev"
    )
    assert called["n"] == 1
    assert detail is not None
    assert label == "skills.sh Marketplace"


@pytest.mark.asyncio
async def test_skillhub_nested_slug_does_not_hit_network(monkeypatch):
    import hiveweave.services.skill_registry as reg

    async def boom(*_a, **_k):
        raise AssertionError("nested SkillHub slug must not HTTP")

    monkeypatch.setattr(reg.httpx, "AsyncClient", boom)
    svc = SkillRegistryService()
    assert await svc._fetch_skillhub_detail(
        "softaworks/agent-toolkit/react-dev"
    ) is None


@pytest.mark.asyncio
async def test_list_records_source_and_does_not_require_market(monkeypatch):
    svc = SkillRegistryService()

    async def fake_sh(search=None):
        return [{
            "slug": "softaworks/agent-toolkit/react-dev",
            "summary": "react-dev — from softaworks/agent-toolkit",
            "description": "",
            "displayName": "react-dev",
            "source": "skills.sh",
        }]

    monkeypatch.setattr(svc, "_search_skills_sh", fake_sh)
    text = await svc.list_available_skills(
        search="React", agent_id="hr-1"
    )
    assert "REQUIRES at least one marketplace" not in text
    assert "Marketplace skills are optional" in text
    assert "[skills.sh]" in text
    assert svc.market_source_for_slug(
        "hr-1", "softaworks/agent-toolkit/react-dev"
    ) == "skills.sh"


@pytest.mark.asyncio
async def test_listed_skillhub_slug_skips_skills_sh(monkeypatch):
    svc = SkillRegistryService()
    sh_calls: list[str] = []

    async def fake_sh(slug):
        sh_calls.append(slug)
        return None

    async def fake_hub(slug):
        return {
            "slug": slug,
            "summary": "from skillhub",
            "description": "",
            "displayName": slug,
            "skill_md": "# hub",
        }

    monkeypatch.setattr(svc, "_fetch_skills_sh_detail", fake_sh)
    monkeypatch.setattr(svc, "_fetch_skillhub_detail", fake_hub)

    detail, label = await svc._resolve_marketplace_skill(
        "react", source="skillhub"
    )
    assert sh_calls == []
    assert detail is not None
    assert "SkillHub" in label


@pytest.mark.asyncio
async def test_connect_timeout_on_search_does_not_mark_detail(monkeypatch):
    import hiveweave.services.skill_registry as reg

    class _BoomClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            raise httpx.ConnectTimeout("timeout")

    monkeypatch.setattr(reg.httpx, "AsyncClient", lambda **kw: _BoomClient())
    svc = SkillRegistryService()
    out = await svc._search_skills_sh_api("browser QA UI E2E testing")
    assert out is None
    assert svc._store_is_unreachable(STORE_SKILLS_SH_SEARCH) is True
    assert svc._store_is_unreachable(STORE_SKILLS_SH_DETAIL) is False
