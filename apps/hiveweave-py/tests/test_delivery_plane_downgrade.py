"""#12 交付物平面 → 视觉门降档（fixplan §6 #12）。

判据来源：我们自己的既有实现 —— 项目级权威事实在 per-project DB
``project_meta``（``api/projects.py:486`` ``_fetch_project_meta``；正典 DDL
``db/schema.py`` 的 project_meta 建表）。**不引用 DSH**：DSH 无交付物平面
概念，其做法不构成本场景判据（fixplan §10 纪律）。

本测试用**行为断言**（函数返回值）而非文本子串 —— 改回旧行为即打红。
"""

from __future__ import annotations

import re

from hiveweave.db.schema import PROJECT_DB_TABLES
from hiveweave.services.delivery_plane import (
    DELIVERY_PLANES,
    PLANE_TAG_PREFIX,
    VISUAL_POLICY_DOWNGRADE,
    downgrade_policy_for_plane,
    downgrade_tag,
    normalize_delivery_plane,
    plane_from_tags,
    resolve_delivery_plane,
)


class TestPlaneNormalization:
    def test_canonical_planes_accepted(self):
        for p in DELIVERY_PLANES:
            assert normalize_delivery_plane(p) == p

    def test_case_and_underscore_tolerated(self):
        assert normalize_delivery_plane("Native_Desktop") == "native-desktop"
        assert normalize_delivery_plane(" GAME-ENGINE ") == "game-engine"

    def test_unknown_returns_none_not_web(self):
        """未知必须返回 None（不是 web）—— 未知若当 web 会静默放过降档。"""
        assert normalize_delivery_plane(None) is None
        assert normalize_delivery_plane("") is None
        assert normalize_delivery_plane("mobile") is None
        assert normalize_delivery_plane("webapp") is None


class TestPlaneResolution:
    def test_project_field_wins_over_tag(self):
        assert (
            resolve_delivery_plane(project_plane="cli", tags=["plane:web"])
            == "cli"
        )

    def test_tag_is_fallback(self):
        assert resolve_delivery_plane(project_plane=None, tags=["plane:game-engine"]) == "game-engine"

    def test_tag_prefix_constant_is_the_only_form(self):
        assert plane_from_tags([f"{PLANE_TAG_PREFIX}cli"]) == "cli"
        # 裸 "cli" 不算（必须带前缀，避免与业务 tag 混淆）
        assert plane_from_tags(["cli"]) is None

    def test_both_missing_is_none(self):
        assert resolve_delivery_plane() is None


class TestVisualGateDowngrade:
    def test_visual_gate_downgrades_on_non_web(self):
        eff, reason = downgrade_policy_for_plane("ui_browser_e2e", "cli")
        assert eff == "generic_tests"
        assert reason and "cli" in reason and "ui_browser_e2e" in reason

    def test_code_audit_visual_downgrades(self):
        eff, reason = downgrade_policy_for_plane("code_audit_visual", "library")
        assert eff == "code_audit_unit"
        assert reason

    def test_web_plane_keeps_visual_gate(self):
        eff, reason = downgrade_policy_for_plane("ui_browser_e2e", "web")
        assert eff == "ui_browser_e2e"
        assert reason is None

    def test_unknown_plane_does_not_downgrade(self):
        """未知平面**不降档** —— 拿猜出来的平面改写 gate 是"静默"的一种。"""
        eff, reason = downgrade_policy_for_plane("ui_browser_e2e", None)
        assert eff == "ui_browser_e2e"
        assert reason is None

    def test_non_visual_gate_untouched(self):
        for p in ("generic_tests", "docs_only", "code_audit"):
            eff, reason = downgrade_policy_for_plane(p, "cli")
            assert eff == p and reason is None

    def test_every_downgrade_target_is_a_real_policy(self):
        """降档目标必须是合法 policy（防打错字把任务导向不存在的门）。"""
        for src, dst in VISUAL_POLICY_DOWNGRADE.items():
            assert src != dst
            assert dst and re.fullmatch(r"[a-z_]+", dst), dst

    def test_downgrade_tag_records_source_target_plane(self):
        """留痕 tag 必须同时含源门/目标门/平面 —— 缺一不可审计。"""
        tag = downgrade_tag("ui_browser_e2e", "generic_tests", "cli")
        assert "ui_browser_e2e" in tag
        assert "generic_tests" in tag
        assert "cli" in tag


class TestCanonicalDdlHasPlaneColumn:
    def test_project_meta_ddl_contains_delivery_plane(self):
        """正典 DDL 必须含 delivery_plane（新库建表即完整，不靠 ALTER 补）。"""
        ddl = next(
            d for d in PROJECT_DB_TABLES
            if "CREATE TABLE IF NOT EXISTS project_meta" in d
        )
        assert "delivery_plane" in ddl

    def test_plane_column_migration_is_present_and_idempotent(self):
        """存量库懒迁移存在（ALTER ... ADD COLUMN 幂等由建表循环吞异常保证）。"""
        assert any(
            "ALTER TABLE project_meta ADD COLUMN delivery_plane" in d
            for d in PROJECT_DB_TABLES
        )
