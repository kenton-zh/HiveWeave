"""正典 DDL 必须包含迁移清单里的列（防「正典落后」再次发生）。

2026-09-11 实测的代价：`tasks` 正典缺 11 列、`inbox` 正典缺 9 列 → **每个新建
库**都要跑 20 条 ALTER。而 Windows 上首次 schema 变更要 ~1.5s（首次写页 + 实时
扫描），其余每条 ~11ms（实测 `ALTER tasks.due_at` = 1456ms）。后果是全量测试
从 8min 涨到 16min。

「迁移清单」与「正典 DDL」是同一份 schema 的两个表达，必须一致：

  · **正典** = 权威源（新库建表即完整）；
  · **迁移** = 存量老库的补齐路径。

只写迁移、不写正典 = **每个新库都为历史欠账付一次 ALTER 代价**
（DSH ``packages/AGENTS.md:15``「Publish state only at its commit point」——
状态集中在权威源，而不是散在补齐路径里）。

另：本守卫同时记录了「按 DB 世代失效的迁移标记」这一机制的落点清单
（见 tests/test_migration_marker_generation.py），两者一起保证
「迁移只对存量库生效、且不会跨库世代误判」。
"""

from __future__ import annotations

import re

from hiveweave.db.schema import PROJECT_DB_TABLES


def _canonical_columns(table: str) -> set[str]:
    """从 ``PROJECT_DB_TABLES`` 里取某张正典表的列名集合。"""
    for ddl in PROJECT_DB_TABLES:
        m = re.search(
            rf"CREATE TABLE IF NOT EXISTS {table}\s*\((.*?)\n\s*\)\n", ddl, re.S
        )
        if m:
            return set(re.findall(r"^\s{4,10}(\w+)\s", m.group(1), re.M))
    return set()


def test_tasks_migration_columns_are_in_the_canonical_ddl():
    from hiveweave.services.tasks.constants import _MISSING_COLUMNS

    canon = _canonical_columns("tasks")
    assert canon, "正典里找不到 tasks 表"
    missing = [col for col, _ in _MISSING_COLUMNS if col not in canon]
    assert not missing, (
        f"tasks 的迁移清单里有 {missing} 不在正典 DDL —— 每个新建库都要为它们"
        "多跑一条 ALTER（首次 schema 变更在 Windows 上 ~1.5s）。"
        "请把这列补进 db/schema.py 的 tasks CREATE（迁移清单保留给存量老库）。"
    )


def test_inbox_migration_columns_are_in_the_canonical_ddl():
    from hiveweave.services.inbox import _MISSING_COLUMNS

    canon = _canonical_columns("inbox")
    assert canon, "正典里找不到 inbox 表"
    missing = [col for col, _ in _MISSING_COLUMNS if col not in canon]
    assert not missing, (
        f"inbox 的迁移清单里有 {missing} 不在正典 DDL —— 理由同 tasks："
        "全新库不该靠逐条 ALTER 补齐。"
    )


def test_handoff_migration_columns_are_in_the_canonical_ddl():
    from hiveweave.services.handoff import _MISSING_COLUMNS

    canon = _canonical_columns("handoffs")
    assert canon, "正典里找不到 handoffs 表"
    missing = [col for col, _ in _MISSING_COLUMNS if col not in canon]
    assert not missing, f"handoffs 的迁移清单里有 {missing} 不在正典 DDL"


def test_dispatch_migration_columns_are_in_the_canonical_ddl():
    from hiveweave.services.dispatch import _MISSING_COLUMNS

    canon = _canonical_columns("work_logs")
    assert canon, "正典里找不到 work_logs 表"
    missing = [col for col, _ in _MISSING_COLUMNS if col not in canon]
    assert not missing, f"work_logs 的迁移清单里有 {missing} 不在正典 DDL"


def test_attestation_migration_columns_are_in_the_canonical_ddl():
    """tool_attestations 的迁移列必须在正典里。

    ``audit_cache`` 例外：它不在 PROJECT_DB_TABLES，由 attestation 服务自建，
    所以对其只断言"列定义存在"而不做正典化要求（记在这里以免被误认为遗漏）。
    """
    from hiveweave.services.attestation import _ATTESTATION_COLUMN_MIGRATIONS

    checked = 0
    for table, column, _def in _ATTESTATION_COLUMN_MIGRATIONS:
        if table != "tool_attestations":
            continue
        canon = _canonical_columns(table)
        assert canon, f"正典里找不到 {table} 表"
        assert column in canon, (
            f"{table}.{column} 不在正典 DDL —— 新库不该靠 ALTER 补它"
        )
        checked += 1
    assert checked, "迁移清单里没有 tool_attestations 的条目？（口径已变，请复核）"


def test_modules_migration_columns_are_in_the_canonical_ddl():
    """modules 的懒迁移列必须在正典 DDL 里（三件套纪律，批次 7）。

    批次 7 给 modules 补了三列（``parent_module_id`` / ``status`` /
    ``current_agent_id``），按纪律必须是**正典 + 懒迁移 + 守卫**三件套：
    正典管新库（建表即完整），懒迁移管存量老库。只写迁移不写正典 =
    每个新库都为历史欠账多跑 ALTER（本仓已实测过这条代价，见本文件顶部）。
    """
    from hiveweave.services.modules import _MISSING_COLUMNS

    canon = _canonical_columns("modules")
    assert canon, "正典里找不到 modules 表"
    missing = [col for col, _ in _MISSING_COLUMNS if col not in canon]
    assert not missing, (
        f"modules 的懒迁移清单里有 {missing} 不在正典 DDL —— "
        "新库不该靠 ALTER 补它们。"
    )
    # 批次 7 的核心列（交付面本身）也在正典里
    for col in ("parent_module_id", "status", "current_agent_id"):
        assert col in canon, f"modules.{col} 是批次 7 交付面，必须在正典 DDL 里"

