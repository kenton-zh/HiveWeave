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
    SKILLS_SH_MAX_RESULTS,
    SkillRegistryService,
    _filter_skill_slugs,
    _skill_md_requires_api_key,
    _slug_from_sitemap_url,
)


@pytest.fixture(autouse=True)
def _clear_skillhub_cache():
    SkillRegistryService._skillhub_detail_cache.clear()
    SkillRegistryService._sitemap_slugs = None
    SkillRegistryService._sitemap_fetched_at = 0.0
    yield
    SkillRegistryService._skillhub_detail_cache.clear()
    SkillRegistryService._sitemap_slugs = None
    SkillRegistryService._sitemap_fetched_at = 0.0


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
    """搜索命中数超过 SKILLS_SH_MAX_RESULTS 时截断。"""
    svc = SkillRegistryService()
    slugs = [f"owner/repo/skill-{i}" for i in range(20)]
    # 全部命中"skill"
    detail = {"slug": "x", "summary": "s", "skill_md": "# x\n\nbody"}
    monkeypatch.setattr(svc, "_fetch_skills_sh_detail", _async(detail))
    out = await svc._search_skills_sh_slugs(slugs, "skill")
    assert len(out) == SKILLS_SH_MAX_RESULTS


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


@pytest.mark.asyncio
async def test_search_skills_sh_prefers_sitemap(monkeypatch):
    """sitemap 索引可用时走全量搜索；sitemap 失败时降级首页。"""
    import hiveweave.services.skill_registry as reg

    svc = SkillRegistryService()

    async def fake_sitemap():
        return ["cloudai-x/threejs-skills/threejs-animation",
                "wshobson/agents/typescript-advanced-types"]

    monkeypatch.setattr(svc, "_fetch_skills_sh_sitemap", fake_sitemap)
    home_called = {"v": False}

    async def fake_home(search=None):
        home_called["v"] = True
        return []

    monkeypatch.setattr(svc, "_search_skills_sh_homepage", fake_home)

    detail = {"slug": "x", "summary": "s", "skill_md": "# x\n\nbody"}
    monkeypatch.setattr(svc, "_fetch_skills_sh_detail", _async(detail))

    out = await svc._search_skills_sh("typescript")
    assert [s["slug"] for s in out] == ["wshobson/agents/typescript-advanced-types"]
    assert home_called["v"] is False, "sitemap 可用时不应走首页降级"

    # sitemap 失败 → 首页兜底
    async def fake_sitemap_empty():
        return []

    monkeypatch.setattr(svc, "_fetch_skills_sh_sitemap", fake_sitemap_empty)
    await svc._search_skills_sh("x")
    assert home_called["v"] is True


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
    """搜索过滤 requires_api_key 的技能，并从 3x 候选补足上限。"""
    svc = SkillRegistryService()
    slugs = [f"owner/repo/skill-{i}" for i in range(15)]
    api_key_slugs = {f"owner/repo/skill-{i}" for i in (1, 4, 8, 13)}

    async def fake_detail(slug):
        return {"slug": slug, "summary": "s", "skill_md": "# x",
                "requires_api_key": slug in api_key_slugs}

    monkeypatch.setattr(svc, "_fetch_skills_sh_detail", fake_detail)

    out = await svc._search_skills_sh_slugs(slugs, "skill")
    # 15 个候选全命中，4 个需 API key → 11 个可用 → 截断 10
    assert len(out) == SKILLS_SH_MAX_RESULTS
    assert all(s["slug"] not in api_key_slugs for s in out)


@pytest.mark.asyncio
async def test_search_skills_sh_slugs_keeps_no_key_skills(monkeypatch):
    """不需要 API key 的技能全部保留（默认 requires_api_key=False）。"""
    svc = SkillRegistryService()
    slugs = [f"owner/repo/skill-{i}" for i in range(5)]
    detail = {"slug": "x", "summary": "s", "skill_md": "# x\n\nbody"}
    monkeypatch.setattr(svc, "_fetch_skills_sh_detail", _async(detail))
    out = await svc._search_skills_sh_slugs(slugs, "skill")
    assert len(out) == 5


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
async def test_list_available_prunes_stale_market_slugs_when_market_down(monkeypatch):
    """市场不可达时清掉 per-agent 缓存里的过期市场 slug，避免 hire 门槛死锁。

    回归场景：会话早期市场可达（缓存留下市场 slug）→ 市场中途不可达 →
    HR 按 tail 提示全绑内置 → gate 不应再因过期市场 slug 拒绝。
    """
    svc = SkillRegistryService()
    # 缓存里混着内置 slug + 早期见过的市场 slug
    svc._skill_search_cache["hr-1"] = [
        "self-review",
        "anthropics/skills/webapp-testing",
        "anthropics/skills/frontend-design",
    ]

    async def fake_sh(search=None):
        return None  # skills.sh 不可达

    async def fake_hub(search=None):
        return []  # SkillHub 也无结果 → 市场整体不可达

    monkeypatch.setattr(svc, "_search_skills_sh", fake_sh)
    monkeypatch.setattr(svc, "_search_skillhub", fake_hub)

    out = await svc.list_available_skills(search="x", agent_id="hr-1")
    assert "self-review" in out
    assert "webapp-testing" not in out
    assert "frontend-design" not in out
    cached = svc._skill_search_cache["hr-1"]
    assert "webapp-testing" not in cached and "frontend-design" not in cached
    assert "self-review" in cached  # 内置 slug 保留

    # gate 对清过后的缓存放行
    from hiveweave.tools.org_tools import _hire_market_skill_gate

    assert _hire_market_skill_gate(
        skills=["self-review"],
        seen_slugs=svc._skill_search_cache["hr-1"],
        builtin_lookup=SkillRegistryService._get_builtin_skill,
    ) is None
