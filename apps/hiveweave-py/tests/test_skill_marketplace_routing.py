"""Skill marketplace routing — skills.sh → SkillHub 降级一致性。

回归测试：hire_agent 校验 / read_skill / get_skill_detail 的市场解析必须与
list_available_skills 的搜索路由保持同一契约——skills.sh（国外）优先，不可达
（_fetch_skills_sh_detail 返回 None）时自动降级到 SkillHub（国内）。

修复前的 bug：hire_agent 校验只调 _fetch_skills_sh_detail，无 SkillHub 降级，
导致国内商店技能「搜得到却绑不上」（被误判 invalid）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from hiveweave.services.skill_registry import (
    SKILLHUB_SOURCE_LABEL,
    SKILLS_SH_MAX_RESULTS,
    SkillRegistryService,
    _filter_skill_slugs,
    _skill_md_requires_api_key,
    _slug_from_sitemap_url,
)


@pytest.fixture(autouse=True)
def _clear_skillhub_cache(tmp_path, monkeypatch):
    SkillRegistryService._skillhub_detail_cache.clear()
    SkillRegistryService._skillhub_detail_cache_ts.clear()
    SkillRegistryService._skills_sh_cache.clear()
    SkillRegistryService._skills_sh_cache_ts.clear()
    SkillRegistryService._sitemap_slugs = None
    SkillRegistryService._sitemap_fetched_at = 0.0
    SkillRegistryService._resolve_cache.clear()
    SkillRegistryService._store_unreachable.clear()
    cache_dir = tmp_path / "skill_cache"
    cache_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(SkillRegistryService, "_skills_sh_disk_cache_dir", str(cache_dir))
    yield
    SkillRegistryService._skillhub_detail_cache.clear()
    SkillRegistryService._skillhub_detail_cache_ts.clear()
    SkillRegistryService._skills_sh_cache.clear()
    SkillRegistryService._skills_sh_cache_ts.clear()
    SkillRegistryService._sitemap_slugs = None
    SkillRegistryService._sitemap_fetched_at = 0.0
    SkillRegistryService._resolve_cache.clear()
    SkillRegistryService._store_unreachable.clear()


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


# ── 双商店 slug 命名空间冲突：全路径 slug 短名二次 fallback ──
# 根因（「搜得到却绑不上」现场）：sitemap 缓存存全路径 owner/repo/skill，
# skills.sh 实时 fetch 抖动时降级 SkillHub，而 SkillHub 只认短名。


def _recording_fetch(result_by_slug: dict):
    """返回 (fake, called) — fake 按 slug 查表返回，called 记录收到的参数。"""
    calls: list[str] = []

    async def fake(slug):
        calls.append(slug)
        return result_by_slug.get(slug)

    return fake, calls


@pytest.mark.asyncio
async def test_resolve_full_path_short_name_fallback_to_skillhub(monkeypatch):
    """核心回归：skills.sh 全路径失败（抖动）→ 短名 fallback 命中 SkillHub。"""
    svc = SkillRegistryService()
    monkeypatch.setattr(svc, "_fetch_skills_sh_detail", _async(None))
    hub, hub_calls = _recording_fetch({"async-python-patterns": _hub_detail("async-python-patterns")})
    monkeypatch.setattr(svc, "_fetch_skillhub_detail", hub)

    detail, label = await svc._resolve_marketplace_skill(
        "wshobson/agents/async-python-patterns"
    )
    assert detail is not None and detail["summary"] == "from skillhub"
    assert label == SKILLHUB_SOURCE_LABEL
    assert hub_calls == ["wshobson/agents/async-python-patterns", "async-python-patterns"], (
        "全路径首查失败后必须用短名二次 fallback"
    )


@pytest.mark.asyncio
async def test_resolve_full_path_short_name_fallback_skips_slashless(monkeypatch):
    """无 '/' 的 slug 只查一次，不重复。"""
    svc = SkillRegistryService()
    sh, sh_calls = _recording_fetch({"frontend": _sh_detail("frontend")})
    hub, hub_calls = _recording_fetch({})
    monkeypatch.setattr(svc, "_fetch_skills_sh_detail", sh)
    monkeypatch.setattr(svc, "_fetch_skillhub_detail", hub)

    detail, label = await svc._resolve_marketplace_skill("frontend")
    assert detail is not None and label == "skills.sh Marketplace"
    assert sh_calls == ["frontend"]
    assert hub_calls == []


@pytest.mark.asyncio
async def test_resolve_full_path_keeps_short_name_hit_before_skillhub(monkeypatch):
    """短名在 skills.sh 也可达时（技能名=唯一段），仍 skills.sh 优先。"""
    svc = SkillRegistryService()
    sh, sh_calls = _recording_fetch({
        "frontend-design": _sh_detail("frontend-design"),
    })
    hub, hub_calls = _recording_fetch({})
    monkeypatch.setattr(svc, "_fetch_skills_sh_detail", sh)
    monkeypatch.setattr(svc, "_fetch_skillhub_detail", hub)

    detail, label = await svc._resolve_marketplace_skill("anthropics/skills/frontend-design")
    assert detail is not None and detail["summary"] == "from skills.sh"
    assert label == "skills.sh Marketplace"
    assert hub_calls == ["anthropics/skills/frontend-design"]


@pytest.mark.asyncio
async def test_hire_validation_accepts_full_path_via_short_name(monkeypatch):
    """现场复刻：list 给全路径 #N，hire 时 skills.sh 抖动 → 不再 invalid。"""
    svc = SkillRegistryService()
    monkeypatch.setattr(svc, "_fetch_skills_sh_detail", _async(None))
    monkeypatch.setattr(
        svc, "_fetch_skillhub_detail",
        _async(_hub_detail("async-python-patterns")),
    )

    assert await _hire_validation_accepts(
        svc, "wshobson/agents/async-python-patterns"
    ) is True


@pytest.mark.asyncio
async def test_read_skill_full_path_via_short_name(monkeypatch):
    svc = SkillRegistryService()
    monkeypatch.setattr(svc, "_fetch_skills_sh_detail", _async(None))
    monkeypatch.setattr(
        svc, "_fetch_skillhub_detail",
        _async(_hub_detail("monday.com-automation")),
    )

    text = await svc.read_skill("claude-office-skills/skills/monday.com-automation")
    assert "hub body" in text
    assert "not found" not in text


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


# ── sitemap 全量索引搜索（修复"只能搜首页"的覆盖瓶颈）────────


def test_slug_from_sitemap_url_filters_non_skills():
    """sitemap URL 解析：只留 owner/repo/skill-name，排除导航/资源文件。"""
    assert _slug_from_sitemap_url("https://www.skills.sh/anthropics/skills/frontend-design") == "anthropics/skills/frontend-design"
    assert _slug_from_sitemap_url("https://www.skills.sh/topic/react") is None
    assert _slug_from_sitemap_url("https://www.skills.sh/docs/cli") is None
    assert _slug_from_sitemap_url("https://www.skills.sh/agent/claude-code") is None
    assert _slug_from_sitemap_url("https://www.skills.sh/trending") is None
    assert _slug_from_sitemap_url("https://www.skills.sh/favicon.ico") is None
    assert _slug_from_sitemap_url("https://www.skills.sh/a/b/c.svg") is None
    # 裸词前缀不误杀：owner 以 hot/search 开头的真实技能保留
    assert _slug_from_sitemap_url("https://www.skills.sh/hotcoffeeshake/tong-jincheng-skill/tong-jincheng-perspective") == "hotcoffeeshake/tong-jincheng-skill/tong-jincheng-perspective"
    # 含点技能名不是资源文件：按扩展名白名单判定
    assert _slug_from_sitemap_url("https://www.skills.sh/claude-office-skills/skills/monday.com-automation") == "claude-office-skills/skills/monday.com-automation"
    assert _slug_from_sitemap_url("https://www.skills.sh/pexoai/pexo-skills/veo-3.2-prompter") == "pexoai/pexo-skills/veo-3.2-prompter"
    assert _slug_from_sitemap_url("https://www.skills.sh/404kidwiz/claude-supercode-skills/dotnet-framework-4.8-expert") == "404kidwiz/claude-supercode-skills/dotnet-framework-4.8-expert"


def test_filter_skill_slugs_multiword_and_normalized():
    """关键词过滤：忽略非字母数字，多词取交集，支持 Three.js → threejs。"""
    slugs = [
        "cloudai-x/threejs-skills/threejs-animation",
        "cloudai-x/threejs-skills/threejs-shaders",
        "wshobson/agents/typescript-advanced-types",
        "anthropics/skills/frontend-design",
    ]
    assert _filter_skill_slugs(slugs, "Three.js") == [
        "cloudai-x/threejs-skills/threejs-animation",
        "cloudai-x/threejs-skills/threejs-shaders",
    ]
    assert _filter_skill_slugs(slugs, "three animation") == [
        "cloudai-x/threejs-skills/threejs-animation",
    ]
    assert _filter_skill_slugs(slugs, "frontend") == [
        "anthropics/skills/frontend-design",
    ]
    assert _filter_skill_slugs(slugs, None) == slugs
    assert _filter_skill_slugs(slugs, "zzz") == []


@pytest.mark.asyncio
async def test_search_skills_sh_slugs_caps_at_max_results(monkeypatch):
    """搜索命中数超过 SKILLS_SH_MAX_RESULTS 时截断，且不抓详情页。"""
    svc = SkillRegistryService()
    slugs = [f"owner/repo/skill-{i}" for i in range(20)]

    detail_fetched = {"v": 0}

    async def fake_detail(slug):
        detail_fetched["v"] += 1
        return {"slug": slug, "summary": "s", "skill_md": "# x\n\nbody"}

    monkeypatch.setattr(svc, "_fetch_skills_sh_detail", fake_detail)

    out = await svc._search_skills_sh_slugs(slugs, "skill")
    assert len(out) == SKILLS_SH_MAX_RESULTS
    assert detail_fetched["v"] == 0, "搜索阶段不得抓详情页（详情在绑定/读取时按需抓取）"
    assert out[0]["summary"].endswith("from owner/repo")


@pytest.mark.asyncio
async def test_search_skills_sh_slugs_bare_listing():
    """无关键词浏览：返回前 N 个 slug 的元数据列表（名字 — from owner/repo）。"""
    svc = SkillRegistryService()
    slugs = [f"owner{i}/repo/skill-{i}" for i in range(5)]
    out = await svc._search_skills_sh_slugs(slugs, None)
    assert [s["slug"] for s in out] == slugs
    assert out[0]["summary"] == "skill-0 — from owner0/repo"


@pytest.mark.asyncio
async def test_fetch_sitemap_index_parses_urls(monkeypatch):
    """sitemap 抓取：解析 <loc>，过滤非技能路径，缓存命中。"""
    import hiveweave.services.skill_registry as reg

    xml_1 = """<?xml version="1.0"?><urlset>
      <url><loc>https://www.skills.sh/anthropics/skills/frontend-design</loc></url>
      <url><loc>https://www.skills.sh/topic/react</loc></url>
      <url><loc>https://www.skills.sh/wshobson/agents/typescript-advanced-types</loc></url>
      <url><loc>https://www.skills.sh/docs/cli</loc></url>
    </urlset>"""
    xml_2 = """<?xml version="1.0"?><urlset>
      <url><loc>https://www.skills.sh/cloudai-x/threejs-skills/threejs-animation</loc></url>
      <url><loc>https://www.skills.sh/favicon.ico</loc></url>
    </urlset>"""

    class _FakeResp:
        def __init__(self, body):
            self.status_code = 200
            self.text = body

    class _FakeClient:
        def __init__(self):
            self._calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            if "sitemap-skills-1" in url:
                return _FakeResp(xml_1)
            return _FakeResp(xml_2)

    monkeypatch.setattr(reg.httpx, "AsyncClient", lambda **kw: _FakeClient())

    svc = SkillRegistryService()
    slugs = await svc._fetch_skills_sh_sitemap()
    assert slugs == [
        "anthropics/skills/frontend-design",
        "wshobson/agents/typescript-advanced-types",
        "cloudai-x/threejs-skills/threejs-animation",
    ]
    # 缓存命中：再调用不触发网络
    assert await svc._fetch_skills_sh_sitemap() == slugs


# ── API key 技能过滤（skills.sh 无商店标签，启发式检测）──────


def test_skill_md_requires_api_key_heuristic():
    """启发式：环境变量名或「requires/need/set … API key」类短语判需要。"""
    assert _skill_md_requires_api_key("Set ANTHROPIC_API_KEY before running.")
    assert _skill_md_requires_api_key("You must set the ELEVENLABS_API_KEY first")
    assert _skill_md_requires_api_key("requires an API key to call the service")
    assert _skill_md_requires_api_key("you will need to sign up for an API key")
    assert _skill_md_requires_api_key("must use the API key")
    # 不误报：仅文档提及、无要求语境
    assert not _skill_md_requires_api_key("how to use your API key with the SDK")
    assert not _skill_md_requires_api_key("MCP tools mcp_azure_mcp_eventhubs")
    assert not _skill_md_requires_api_key("")
    assert not _skill_md_requires_api_key(None)
    # 占位符不误报：前缀 <4 字符的通用名（API_KEY / JWT_TOKEN）
    assert not _skill_md_requires_api_key("Scan for hardcoded API_KEY values")
    assert not _skill_md_requires_api_key("Check for leaked JWT_TOKEN in logs")
    # 否定语境不误报：optional / if … not set
    assert not _skill_md_requires_api_key(
        "If OPENAI_API_KEY is not set, the skill still works with local models"
    )
    assert not _skill_md_requires_api_key(
        "The ANTHROPIC_API_KEY is optional; works without it"
    )


@pytest.mark.asyncio
async def test_search_skills_sh_slugs_filters_api_key_skills(monkeypatch):
    """搜索阶段不抓详情 → 不过滤 API key 技能（详情在绑定期才验证）。

    设计语义：搜索列表是「候选」，不并发抓详情页（限流根源）；
    需要 API key 的技能在 hire/bind 时由 _resolve_marketplace_skill
    按 requires_api_key 过滤（见 test_resolve_falls_back_to_skillhub_when_sh_requires_api_key）。
    """
    svc = SkillRegistryService()
    slugs = [f"owner/repo/skill-{i}" for i in range(15)]

    detail_fetched = {"v": 0}

    async def fake_detail(slug):
        detail_fetched["v"] += 1
        return {"slug": slug, "summary": "s", "skill_md": "# x",
                "requires_api_key": True}

    monkeypatch.setattr(svc, "_fetch_skills_sh_detail", fake_detail)

    out = await svc._search_skills_sh_slugs(slugs, "skill")
    assert len(out) == SKILLS_SH_MAX_RESULTS
    assert detail_fetched["v"] == 0, "搜索阶段不得触发详情抓取"


@pytest.mark.asyncio
async def test_search_skills_sh_slugs_keeps_no_key_skills(monkeypatch):
    """sitemap 兜底搜索：命中即列出（不抓详情，天然不过滤）。"""
    svc = SkillRegistryService()
    slugs = [f"owner/repo/skill-{i}" for i in range(5)]
    detail = {"slug": "x", "summary": "s", "skill_md": "# x\n\nbody"}
    monkeypatch.setattr(svc, "_fetch_skills_sh_detail", _async(detail))
    out = await svc._search_skills_sh_slugs(slugs, "skill")
    assert len(out) == 5
    assert [s["slug"] for s in out] == slugs


@pytest.mark.asyncio
async def test_resolve_falls_back_to_skillhub_when_sh_requires_api_key(monkeypatch):
    """skills.sh 详情启发式判定需要 API key → 降级 SkillHub（与不可达同语义）。"""
    svc = SkillRegistryService()
    monkeypatch.setattr(
        svc, "_fetch_skills_sh_detail",
        _async({**_sh_detail("frontend"), "requires_api_key": True}),
    )
    monkeypatch.setattr(svc, "_fetch_skillhub_detail", _async(_hub_detail("frontend")))
    detail, label = await svc._resolve_marketplace_skill("frontend")
    assert detail is not None and detail["summary"] == "from skillhub"
    assert label == SKILLHUB_SOURCE_LABEL


# ── hire_agent 市场技能强制（市场可见时必须选至少一个）────────


def _builtin_lookup(builtin: set[str]):
    def lookup(slug: str) -> dict | None:
        return {"slug": slug} if slug in builtin else None
    return lookup


def test_hire_market_gate_rejects_all_builtin_when_market_seen():
    """HR 搜索时见过市场技能却全绑内置 → 拒绝并提示。"""
    from hiveweave.tools.org_tools import _hire_market_skill_gate

    seen = ["self-review", "anthropics/skills/webapp-testing", "anthropics/skills/frontend-design"]
    err = _hire_market_skill_gate(
        skills=["self-review"],
        seen_slugs=seen,
        builtin_lookup=_builtin_lookup({"self-review"}),
    )
    assert err is not None
    assert "marketplace" in err.lower()
    assert "webapp-testing" in err


def test_hire_market_gate_passes_when_market_skill_included():
    """skills 里含至少一个市场技能 → 放行。"""
    from hiveweave.tools.org_tools import _hire_market_skill_gate

    seen = ["self-review", "anthropics/skills/webapp-testing"]
    assert _hire_market_skill_gate(
        skills=["self-review", "anthropics/skills/webapp-testing"],
        seen_slugs=seen,
        builtin_lookup=_builtin_lookup({"self-review"}),
    ) is None


def test_hire_market_gate_passes_when_no_market_seen():
    """HR 没见过市场技能（市场不可达/没搜到）→ 全内置放行，不卡死招聘。"""
    from hiveweave.tools.org_tools import _hire_market_skill_gate

    assert _hire_market_skill_gate(
        skills=["self-review"],
        seen_slugs=["self-review"],
        builtin_lookup=_builtin_lookup({"self-review"}),
    ) is None


def test_hire_market_gate_passes_when_empty_skills():
    """不传技能 → 放行（招人不强制技能）。"""
    from hiveweave.tools.org_tools import _hire_market_skill_gate

    assert _hire_market_skill_gate(
        skills=[],
        seen_slugs=["anthropics/skills/webapp-testing"],
        builtin_lookup=_builtin_lookup(set()),
    ) is None


@pytest.mark.asyncio
async def test_list_available_last_search_not_accumulated_cache(monkeypatch):
    """市场不可达时 last-search 清空，累计 #N 缓存仍保留旧市场 slug。

    hire 门槛改看 last-search，不再靠清累计缓存防死锁。
    """
    svc = SkillRegistryService()
    svc._skill_search_cache["hr-1"] = [
        "self-review",
        "anthropics/skills/webapp-testing",
        "anthropics/skills/frontend-design",
    ]

    async def fake_sh(search=None):
        return None  # skills.sh 不可达

    async def fake_hub(search=None):
        return []  # SkillHub 也无结果 → 本次市场零命中

    monkeypatch.setattr(svc, "_search_skills_sh", fake_sh)
    monkeypatch.setattr(svc, "_search_skillhub", fake_hub)

    out = await svc.list_available_skills(search="x", agent_id="hr-1")
    assert "self-review" in out
    # this listing does not re-print stale market hits
    assert "webapp-testing" not in out
    cached = svc._skill_search_cache["hr-1"]
    assert "anthropics/skills/webapp-testing" in cached
    assert "self-review" in cached
    assert svc._skill_search_last.get("hr-1") == []

    from hiveweave.tools.org_tools import _hire_market_skill_gate

    assert _hire_market_skill_gate(
        skills=["self-review"],
        seen_slugs=svc._skill_search_last.get("hr-1", []),
        builtin_lookup=SkillRegistryService._get_builtin_skill,
    ) is None


# ── 官方 /api/search 轻量搜索接口 ─────────────────────────────
# 搜索阶段零详情页抓取：单次调用返回元数据（id/name/installs/source），
# 服务端按安装量排序。q 为空时接口返回 400 → None → sitemap 兜底。


@pytest.mark.asyncio
async def test_search_skills_sh_api_parses_results(monkeypatch):
    import hiveweave.services.skill_registry as reg

    payload = {
        "query": "websocket",
        "searchType": "fuzzy",
        "skills": [
            {"id": "jeffallan/claude-skills/websocket-engineer",
             "skillId": "websocket-engineer", "name": "websocket-engineer",
             "installs": 5446, "source": "jeffallan/claude-skills"},
            {"id": "yaklang/hack-skills/websocket-security",
             "skillId": "websocket-security", "name": "websocket-security",
             "installs": 2461, "source": "yaklang/hack-skills"},
            {"id": "no-installs/some-repo/skill-x",
             "skillId": "skill-x", "name": "skill-x", "installs": 0,
             "source": "no-installs/some-repo"},
        ],
        "count": 3,
        "duration_ms": 435,
    }
    monkeypatch.setattr(reg.httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeResp(200, payload)))

    svc = SkillRegistryService()
    out = await svc._search_skills_sh_api("websocket")
    assert [s["slug"] for s in out] == [
        "jeffallan/claude-skills/websocket-engineer",
        "yaklang/hack-skills/websocket-security",
        "no-installs/some-repo/skill-x",
    ]
    assert "5446 installs" in out[0]["summary"]
    assert "from jeffallan/claude-skills" in out[0]["summary"]
    assert out[2]["summary"] == "skill-x — from no-installs/some-repo"


@pytest.mark.asyncio
async def test_search_skills_sh_api_caps_at_max_results(monkeypatch):
    import hiveweave.services.skill_registry as reg

    payload = {
        "skills": [
            {"id": f"o{i}/r/skill-{i}", "skillId": f"skill-{i}",
             "name": f"skill-{i}", "installs": 100 - i, "source": f"o{i}/r"}
            for i in range(30)
        ],
    }
    monkeypatch.setattr(reg.httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeResp(200, payload)))

    svc = SkillRegistryService()
    out = await svc._search_skills_sh_api("skill")
    assert len(out) == SKILLS_SH_MAX_RESULTS


@pytest.mark.asyncio
async def test_search_skills_sh_api_empty_query_no_network(monkeypatch):
    import hiveweave.services.skill_registry as reg

    async def boom(*a, **kw):
        raise AssertionError("empty query must not hit network")

    monkeypatch.setattr(reg.httpx, "AsyncClient", boom)
    svc = SkillRegistryService()
    assert await svc._search_skills_sh_api("") is None
    assert await svc._search_skills_sh_api(None) is None


@pytest.mark.asyncio
async def test_search_skills_sh_api_non200_returns_none(monkeypatch):
    import hiveweave.services.skill_registry as reg

    monkeypatch.setattr(reg.httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeResp(503, {})))
    svc = SkillRegistryService()
    assert await svc._search_skills_sh_api("websocket") is None


@pytest.mark.asyncio
async def test_search_skills_sh_api_malformed_payload_returns_none(monkeypatch):
    import hiveweave.services.skill_registry as reg

    monkeypatch.setattr(reg.httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeResp(200, {"skills": "nope"})))
    svc = SkillRegistryService()
    assert await svc._search_skills_sh_api("websocket") is None


@pytest.mark.asyncio
async def test_search_skills_sh_prefers_api(monkeypatch):
    """/api/search 可用时走轻量接口，不走 sitemap/首页，不抓详情页。"""
    svc = SkillRegistryService()

    async def fake_api(search=None):
        return [{"slug": "a/b/c", "summary": "c (100 installs) — from a/b",
                 "description": "", "displayName": "c"}]

    sitemap_called = {"v": False}

    async def fake_sitemap():
        sitemap_called["v"] = True
        return ["x/y/z"]

    detail_fetched = {"v": 0}

    async def fake_detail(slug):
        detail_fetched["v"] += 1
        return {"slug": slug, "summary": "s"}

    monkeypatch.setattr(svc, "_search_skills_sh_api", fake_api)
    monkeypatch.setattr(svc, "_fetch_skills_sh_sitemap", fake_sitemap)
    monkeypatch.setattr(svc, "_fetch_skills_sh_detail", fake_detail)

    out = await svc._search_skills_sh("websocket")
    assert [s["slug"] for s in out] == ["a/b/c"]
    assert sitemap_called["v"] is False, "API 可用时不应走 sitemap"
    assert detail_fetched["v"] == 0, "API 搜索不得抓详情页"


@pytest.mark.asyncio
async def test_search_skills_sh_falls_back_to_sitemap_when_api_down(monkeypatch):
    """/api/search 不可用（返回 None）→ sitemap 客户端过滤兜底；再失败 → 首页。"""
    import hiveweave.services.skill_registry as reg

    svc = SkillRegistryService()
    monkeypatch.setattr(svc, "_search_skills_sh_api", _async(None))

    async def fake_sitemap():
        return ["cloudai-x/threejs-skills/threejs-animation",
                "wshobson/agents/typescript-advanced-types"]

    monkeypatch.setattr(svc, "_fetch_skills_sh_sitemap", fake_sitemap)
    home_called = {"v": False}

    async def fake_home(search=None):
        home_called["v"] = True
        return []

    monkeypatch.setattr(svc, "_search_skills_sh_homepage", fake_home)

    detail_fetched = {"v": 0}

    async def fake_detail(slug):
        detail_fetched["v"] += 1
        return {"slug": slug, "summary": "s"}

    monkeypatch.setattr(svc, "_fetch_skills_sh_detail", fake_detail)

    out = await svc._search_skills_sh("typescript")
    assert [s["slug"] for s in out] == ["wshobson/agents/typescript-advanced-types"]
    assert home_called["v"] is False, "sitemap 可用时不应走首页降级"
    assert detail_fetched["v"] == 0, "sitemap 兜底搜索不得抓详情页"

    # sitemap 失败 → 首页兜底
    async def fake_sitemap_empty():
        return []

    monkeypatch.setattr(svc, "_fetch_skills_sh_sitemap", fake_sitemap_empty)
    await svc._search_skills_sh("x")
    assert home_called["v"] is True


# ── 详情磁盘缓存（绑定/读取时按需抓取 → 落盘复用）──────────────


@pytest.mark.asyncio
async def test_fetch_skills_sh_detail_disk_cache_write_and_hit(monkeypatch):
    """详情抓取落盘：清空内存缓存后再次读取命中磁盘缓存，不再请求网络。"""
    import hiveweave.services.skill_registry as reg

    calls = {"n": 0}
    class _CountingClient:
        def __init__(self, resp):
            self._resp = resp
            calls["n"] += 1

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            return self._resp

    class _TextResp:
        status_code = 200
        text = ("<html><body><div>Summary</div> <p>Summary websocket skill.</p> "
                "<h2>SKILL.md</h2><p>websocket instructions here.</p> "
                "<div>Installs 10</div></body></html>")

    monkeypatch.setattr(
        reg.httpx, "AsyncClient",
        lambda **kw: _CountingClient(_TextResp()),
    )

    svc = SkillRegistryService()
    first = await svc._fetch_skills_sh_detail("owner/repo/websocket-engineer")
    assert first is not None and "websocket instructions" in first["skill_md"]
    assert calls["n"] == 1

    # 清空内存缓存 → 磁盘缓存命中，不重新请求网络
    svc._skills_sh_cache.clear()
    second = await svc._fetch_skills_sh_detail("owner/repo/websocket-engineer")
    assert second is not None
    assert second["skill_md"] == first["skill_md"]
    assert second["summary"] == first["summary"]
    assert calls["n"] == 1, "磁盘缓存命中后不应再请求网络"


@pytest.mark.asyncio
async def test_skill_disk_cache_key_isolates_sources(tmp_path):
    """缓存 key 分来源：同 slug 的 skills.sh 与 SkillHub 详情互不覆盖。"""
    svc = SkillRegistryService()
    slug = "frontend"
    sh_key = SkillRegistryService._skill_cache_key(slug, "skills.sh")
    hub_key = SkillRegistryService._skill_cache_key(slug, "skillhub")
    assert sh_key != hub_key

    # 两个来源的 detail 落盘到不同文件
    d = SkillRegistryService._get_skills_sh_cache_dir()
    assert d is not None
    svc._save_skill_disk_cache(slug, "skills.sh", {"skill_md": "# sh"})
    svc._save_skill_disk_cache(slug, "skillhub", {"skill_md": "# hub"})
    assert (Path(tmp_path / "skill_cache") / f"{sh_key}.json").exists()
    assert (Path(tmp_path / "skill_cache") / f"{hub_key}.json").exists()
    assert "skill_md" in svc._load_skill_disk_cache(slug, "skills.sh")


@pytest.mark.asyncio
async def test_skill_disk_cache_ttl_expiry(monkeypatch):
    """磁盘缓存超 TTL 后视为失效，重新抓取。"""
    import hiveweave.services.skill_registry as mod

    calls = {"n": 0}
    class _CountingClient:
        def __init__(self, resp):
            self._resp = resp
            calls["n"] += 1

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            return self._resp

    class _TextResp:
        status_code = 200
        text = ("<html><body><h2>SKILL.md</h2><p>fresh instructions.</p> "
                "<div>Installs 10</div></body></html>")

    monkeypatch.setattr(
        mod.httpx, "AsyncClient",
        lambda **kw: _CountingClient(_TextResp()),
    )

    svc = SkillRegistryService()
    await svc._fetch_skills_sh_detail("owner/repo/stale-skill")
    assert calls["n"] == 1

    # 手动把磁盘缓存 fetched_at 改老 → TTL 过期
    d = SkillRegistryService._get_skills_sh_cache_dir()
    key = SkillRegistryService._skill_cache_key("owner/repo/stale-skill", "skills.sh")
    path = Path(d) / f"{key}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["fetched_at"] = time.time() - (mod.SKILL_DETAIL_DISK_TTL + 100)
    path.write_text(json.dumps(data), encoding="utf-8")

    svc._skills_sh_cache.clear()
    out = await svc._fetch_skills_sh_detail("owner/repo/stale-skill")
    assert out is not None and "fresh instructions" in out["skill_md"]
    assert calls["n"] == 2, "TTL 过期后应重新抓取"


# ── 绑定链路重构：resolve 缓存 + 商店不可达探测 + slug 候选 ──


def test_slug_candidates_full_path_to_short_name():
    """全路径 slug → 短名候选；无 '/' 只返回原值。"""
    svc = SkillRegistryService()
    assert svc._slug_candidates("wshobson/agents/async-python-patterns") == [
        "wshobson/agents/async-python-patterns", "async-python-patterns",
    ]
    assert svc._slug_candidates("frontend") == ["frontend"]
    assert svc._slug_candidates("a/b/c") == ["a/b/c", "c"]


@pytest.mark.asyncio
async def test_resolve_cache_skips_second_fetch(monkeypatch):
    """resolve 结果缓存：同 slug 二次 resolve 命中缓存，不重复请求网络。"""
    svc = SkillRegistryService()
    sh_calls = {"n": 0}

    async def fake_sh(slug):
        sh_calls["n"] += 1
        return _sh_detail(slug)

    monkeypatch.setattr(svc, "_fetch_skills_sh_detail", fake_sh)

    d1, l1 = await svc._resolve_marketplace_skill("frontend")
    assert l1 == "skills.sh Marketplace"
    d2, l2 = await svc._resolve_marketplace_skill("frontend")
    assert d2 == d1 and l2 == l1
    assert sh_calls["n"] == 1, "二次 resolve 应命中缓存"


@pytest.mark.asyncio
async def test_store_unreachable_skips_network(monkeypatch):
    """商店不可达探测缓存：标记后 TTL 内直接跳过，不再请求。"""
    svc = SkillRegistryService()
    sh_calls = {"n": 0}

    async def fake_sh(slug):
        sh_calls["n"] += 1
        return None

    monkeypatch.setattr(svc, "_fetch_skills_sh_detail", fake_sh)

    # 手动标记 skills.sh 不可达
    svc._store_mark_unreachable("skills.sh")
    assert svc._store_is_unreachable("skills.sh") is True

    detail, _label = await svc._resolve_marketplace_skill("some-slug")
    assert sh_calls["n"] == 0, "商店不可达缓存内不应发起请求"
    assert detail is None

    # TTL 过期后恢复探测
    svc._store_unreachable["skills.sh"] = time.monotonic() - 120
    assert svc._store_is_unreachable("skills.sh") is False


@pytest.mark.asyncio
async def test_resolve_negative_cache_expires(monkeypatch):
    """负结果（解析不到）只短时缓存：过期后允许重新尝试。"""
    svc = SkillRegistryService()
    monkeypatch.setattr(svc, "_fetch_skills_sh_detail", _async(None))
    monkeypatch.setattr(svc, "_fetch_skillhub_detail", _async(None))

    d1, l1 = await svc._resolve_marketplace_skill("ghost")
    assert d1 is None
    assert l1 == ""

    # 负缓存 TTL 短（SKILL_STORE_PROBE_TTL=60s），过期后重试
    import hiveweave.services.skill_registry as mod

    expires_at, cached_d, cached_l = SkillRegistryService._resolve_cache["ghost"]
    SkillRegistryService._resolve_cache["ghost"] = (
        time.monotonic() - 1, cached_d, cached_l
    )

    # 过期后重新走 fetch（仍 None，但 fetch 被再次调用）
    sh_calls = {"n": 0}

    async def fake_sh(slug):
        sh_calls["n"] += 1
        return None

    monkeypatch.setattr(svc, "_fetch_skills_sh_detail", fake_sh)
    await svc._resolve_marketplace_skill("ghost")
    assert sh_calls["n"] == 1, "负缓存过期后应重新尝试"


# ── 审计补强：网络异常自动标记商店 + 边界防护 ────────────────


@pytest.mark.asyncio
async def test_fetch_detail_network_error_marks_store_unreachable(monkeypatch):
    """fetch 内网络异常自动标记商店不可达（真实路径，非手动标记）。"""
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
    assert await svc._fetch_skills_sh_detail("owner/repo/x") is None
    assert svc._store_is_unreachable("skills.sh") is True


@pytest.mark.asyncio
async def test_fetch_detail_http404_does_not_mark_store(monkeypatch):
    """404 只代表该 slug 不存在，不标记商店不可达。"""
    import hiveweave.services.skill_registry as reg

    monkeypatch.setattr(
        reg.httpx, "AsyncClient",
        lambda **kw: _FakeClient(_FakeResp(404, {})),
    )

    svc = SkillRegistryService()
    # _fetch_skills_sh_detail 读 .text；404 响应在 status 分支返回 None
    assert await svc._fetch_skills_sh_detail("owner/repo/not-here") is None
    assert svc._store_is_unreachable("skills.sh") is False


@pytest.mark.asyncio
async def test_search_skills_sh_api_empty_results_short_circuits(monkeypatch):
    """/api/search 返回 []（可达但无匹配）→ 直接短路，不降级 sitemap。"""
    svc = SkillRegistryService()

    async def fake_api(search=None):
        return []

    sitemap_called = {"v": False}

    async def fake_sitemap():
        sitemap_called["v"] = True
        return ["x/y/z"]

    monkeypatch.setattr(svc, "_search_skills_sh_api", fake_api)
    monkeypatch.setattr(svc, "_fetch_skills_sh_sitemap", fake_sitemap)

    out = await svc._search_skills_sh("nothing-matches")
    assert out == []
    assert sitemap_called["v"] is False, "API 可达无匹配时不应再走 sitemap"


@pytest.mark.asyncio
async def test_search_skills_sh_skips_when_store_unreachable(monkeypatch):
    """商店不可达标记内：_search_skills_sh 直接返回 None（触发国内降级），不请求。"""
    svc = SkillRegistryService()
    svc._store_mark_unreachable("skills.sh")

    api_called = {"v": False}

    async def fake_api(search=None):
        api_called["v"] = True
        return []

    monkeypatch.setattr(svc, "_search_skills_sh_api", fake_api)

    out = await svc._search_skills_sh("frontend")
    assert out is None
    assert api_called["v"] is False, "商店不可达缓存内不应发起请求"


def test_slug_candidates_empty_and_trailing_slash():
    """空 slug / 尾斜杠归一化：空串返回 []，尾斜杠归一后正常展开。"""
    svc = SkillRegistryService()
    assert svc._slug_candidates("") == []
    assert svc._slug_candidates("   ") == []
    assert svc._slug_candidates("owner/repo/skill/") == [
        "owner/repo/skill", "skill",
    ]
    assert svc._slug_candidates("/") == []


@pytest.mark.asyncio
async def test_disk_cache_corrupt_fetched_at_is_ignored(tmp_path):
    """损坏的 fetched_at（非数字）不炸调用方，视为缓存未命中。"""
    svc = SkillRegistryService()
    d = SkillRegistryService._get_skills_sh_cache_dir()
    assert d is not None
    key = SkillRegistryService._skill_cache_key("owner/repo/x", "skills.sh")
    (Path(d) / f"{key}.json").write_text(
        json.dumps({"slug": "owner/repo/x", "fetched_at": "not-a-number"}),
        encoding="utf-8",
    )

    assert svc._load_skill_disk_cache("owner/repo/x", "skills.sh") is None


@pytest.mark.asyncio
async def test_fetch_skills_sh_detail_rejects_empty_slug(monkeypatch):
    """空 slug 直接拒绝，不抓 https://www.skills.sh/ 首页。"""
    import hiveweave.services.skill_registry as reg

    async def boom(*a, **kw):
        raise AssertionError("empty slug must not hit network")

    monkeypatch.setattr(reg.httpx, "AsyncClient", boom)
    svc = SkillRegistryService()
    assert await svc._fetch_skills_sh_detail("") is None
    assert await svc._fetch_skills_sh_detail("   ") is None


@pytest.mark.asyncio
async def test_resolve_skillhub_positive_cache_short_ttl(monkeypatch):
    """SkillHub 来源的正缓存 TTL 短（不锁死整个会话）：过期后重新探测。"""
    svc = SkillRegistryService()
    monkeypatch.setattr(svc, "_fetch_skills_sh_detail", _async(None))

    async def fake_hub(slug):
        return _hub_detail(slug)

    monkeypatch.setattr(svc, "_fetch_skillhub_detail", fake_hub)

    d, label = await svc._resolve_marketplace_skill("frontend")
    assert label == SKILLHUB_SOURCE_LABEL

    expires_at, cached_d, cached_l = SkillRegistryService._resolve_cache["frontend"]
    import hiveweave.services.skill_registry as mod

    # SkillHub 正缓存 TTL 应为探测级（60s），远小于 7 天
    assert expires_at - time.monotonic() <= mod.SKILL_STORE_PROBE_TTL + 1
    assert expires_at - time.monotonic() < mod.SKILL_DETAIL_DISK_TTL


# ── 终审补强：搜索链路不可达短路对称性 ───────────────────────


@pytest.mark.asyncio
async def test_search_skillhub_skips_when_store_unreachable(monkeypatch):
    """SkillHub 不可达标记内：_search_skillhub 直接返回 []，不请求。"""
    import hiveweave.services.skill_registry as reg

    svc = SkillRegistryService()
    svc._store_mark_unreachable("skillhub")

    async def boom(*a, **kw):
        raise AssertionError("store unreachable must not hit network")

    monkeypatch.setattr(reg.httpx, "AsyncClient", boom)

    assert await svc._search_skillhub("frontend") == []
    assert await svc._search_skillhub("") == []


@pytest.mark.asyncio
async def test_search_skillhub_network_error_marks_store(monkeypatch):
    """_search_skillhub 网络异常自动标记 SkillHub 不可达（与 skills.sh 对称）。"""
    import hiveweave.services.skill_registry as reg

    class _BoomClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            raise httpx.ReadTimeout("timeout")

    monkeypatch.setattr(reg.httpx, "AsyncClient", lambda **kw: _BoomClient())

    svc = SkillRegistryService()
    assert await svc._search_skillhub("frontend") == []
    assert svc._store_is_unreachable("skillhub") is True


@pytest.mark.asyncio
async def test_search_skills_sh_api_network_failure_short_circuits_rest(monkeypatch):
    """/api/search 网络异常标记商店后，同一次调用不再串行吃 sitemap/homepage 超时。"""
    import hiveweave.services.skill_registry as reg

    class _BoomClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            raise httpx.ConnectError("conn refused")

    monkeypatch.setattr(reg.httpx, "AsyncClient", lambda **kw: _BoomClient())

    svc = SkillRegistryService()
    sitemap_called = {"v": False}

    async def fake_sitemap():
        sitemap_called["v"] = True
        return ["x/y/z"]

    monkeypatch.setattr(svc, "_fetch_skills_sh_sitemap", fake_sitemap)

    out = await svc._search_skills_sh("frontend")
    assert out is None, "商店网络失败 → None → 触发国内降级"
    assert sitemap_called["v"] is False, "API 网络失败标记后不应再串行走 sitemap"
    assert svc._store_is_unreachable("skills.sh") is True


@pytest.mark.asyncio
async def test_search_skills_sh_api_http_error_still_falls_back(monkeypatch):
    """/api/search 非网络失败（503）不标记商店 → 继续 sitemap 兜底。"""
    import hiveweave.services.skill_registry as reg

    monkeypatch.setattr(
        reg.httpx, "AsyncClient",
        lambda **kw: _FakeClient(_FakeResp(503, {})),
    )

    svc = SkillRegistryService()
    sitemap_called = {"v": False}

    async def fake_sitemap():
        sitemap_called["v"] = True
        return ["cloudai-x/threejs-skills/threejs-animation"]

    monkeypatch.setattr(svc, "_fetch_skills_sh_sitemap", fake_sitemap)

    out = await svc._search_skills_sh("threejs")
    assert out == [{"slug": "cloudai-x/threejs-skills/threejs-animation",
                    "summary": "threejs-animation — from cloudai-x/threejs-skills",
                    "description": "", "displayName": "threejs-animation"}]
    assert sitemap_called["v"] is True, "非网络失败应继续 sitemap 兜底"
    assert svc._store_is_unreachable("skills.sh") is False
