"""T4：建项时按规格推荐 solo 范式 —— 推荐显式化（不改创建行为）。

背景（78 实验定论）：单规格项目「solo 单兵 + 锚定自查」≈ 组织版 1/5 时间、
85-90% 保真；D77 反例 = 六模块组织图做单文件（组织税）。现状曾是
ProjectCreate.orgPattern 只存不用（行为消费者为零）；本修复把推荐显式化：

- create_project 响应新增 orgPatternRecommended + orgPatternRecommendReason；
- 调用方显式传 orgPattern/orgParadigm 时完全尊重（recommended=None）；
- 启发式：charterVision/Goals 命中「单个 HTML/单文件/单一交付面/一个页面」
  ⇒ 推荐 solo；命中多交付面/多服务关键词 ⇒ 不推荐仅提示；
- 不改创建行为本身（仍建 CEO+HR，charter.orgPattern 落库口径不变）。

测试口径沿 test_dsh70_batch4_p2 P2-5 先例：直接单测 api/projects.py 的模块
函数，不拉 FastAPI TestClient（会触发完整 lifespan + DB 初始化 + agent
seed 等重流程）。
"""

from __future__ import annotations

from pathlib import Path

from hiveweave.api.projects import (
    ProjectCreate,
    _build_charter_dict,
    _recommend_org_pattern,
)

_API_PROJECTS_SRC = Path(
    __import__("hiveweave.api.projects", fromlist=["x"]).__file__
).read_text(encoding="utf-8")


def test_create_project_org_pattern_explicit_is_respected():
    """形态一：显式传 orgPattern 时完全尊重 —— 规格命中 solo 关键词也不出推荐。"""
    body = ProjectCreate(
        name="p",
        workspacePath="C:/tmp/ws",
        charterVision="做一个单文件 HTML 小工具",
        orgPattern="hierarchy",
    )
    # 落库口径不变：显式值优先，不被推荐逻辑改写
    assert _build_charter_dict(body)["orgPattern"] == "hierarchy"
    recommended, reason = _recommend_org_pattern(
        body.charterVision, body.charterGoals, body.orgPattern
    )
    assert recommended is None
    assert "显式" in reason

    # 兼容别名 orgParadigm 同样算显式（与端点合并口径 orgPattern or orgParadigm 一致）
    body_alias = ProjectCreate(
        name="p",
        workspacePath="C:/tmp/ws",
        charterVision="单文件",
        orgParadigm="flat",
    )
    rec_alias, _ = _recommend_org_pattern(
        body_alias.charterVision,
        body_alias.charterGoals,
        body_alias.orgPattern or body_alias.orgParadigm,
    )
    assert rec_alias is None


def test_create_project_org_pattern_single_file_recommends_solo():
    """形态二：无显式 orgPattern + 规格命中单文件/单页面关键词 ⇒ 推荐 solo。"""
    for vision in (
        "构建一个单个 HTML 页面的计时器工具",
        "做一个单文件的 Markdown 编辑器",
        "交付一个页面，所有交互都在里面",
    ):
        body = ProjectCreate(
            name="p", workspacePath="C:/tmp/ws", charterVision=vision
        )
        recommended, reason = _recommend_org_pattern(
            body.charterVision, body.charterGoals, body.orgPattern
        )
        assert recommended == "solo", f"{vision!r} 应推荐 solo"
        assert reason.strip(), "推荐必须带一句话 reason"


def test_create_project_org_pattern_multi_surface_hint_only():
    """形态三：多交付面/多服务关键词 ⇒ 不推荐，仅提示（recommended=None）。"""
    for vision in (
        "一套多服务架构：网关 + 三个业务服务",
        "多交付面：Web 端 + CLI + 常驻服务",
        "微服务拆分的交易平台",
    ):
        body = ProjectCreate(
            name="p", workspacePath="C:/tmp/ws", charterVision=vision
        )
        recommended, reason = _recommend_org_pattern(
            body.charterVision, body.charterGoals, body.orgPattern
        )
        assert recommended is None, f"{vision!r} 多面信号不应推荐 solo"
        assert "多交付面" in reason or "多服务" in reason


def test_create_project_org_pattern_no_signal_no_recommendation():
    """无信号文本不出推荐；charter 默认落库口径不变（仍 'solo'，P2-5 行为）。"""
    body = ProjectCreate(
        name="p", workspacePath="C:/tmp/ws", charterVision="做一个日历应用"
    )
    recommended, reason = _recommend_org_pattern(
        body.charterVision, body.charterGoals, body.orgPattern
    )
    assert recommended is None
    assert reason.strip()
    assert _build_charter_dict(body)["orgPattern"] == "solo"


def test_create_project_response_wires_recommendation_fields():
    """响应接线钉：create_project 的返回 dict 必须带两个推荐字段。

    端到端会触发 seed/启动等重流程（仓规：projects 测试不拉 TestClient），
    按 test_dsh70_batch4_p2 的源码断言先例钉住接线。
    """
    assert '"orgPatternRecommended"' in _API_PROJECTS_SRC
    assert '"orgPatternRecommendReason"' in _API_PROJECTS_SRC
    assert "_recommend_org_pattern(" in _API_PROJECTS_SRC
