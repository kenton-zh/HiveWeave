"""P0-3 Stage 2a：拒绝成因**落库面**（schema 三列 + 转发面 + 环境拒绝位）。

验收 1（§1）：`PRAGMA table_info(run_steps)` 含 `denied_by TEXT` /
`blocked_by_environment INTEGER` / `sealed_by TEXT`，且**无 DEFAULT**
（NULL 必须与 False/0 不同形 —— 否则回扫判据 `denied_by IS NULL` 会失去意义）。

⚖ 判据是状态判据（PRAGMA 结果 / 登记集合成员 / 函数返回值），不做源码子串断言。
"""

from __future__ import annotations

import sqlite3

import pytest

from hiveweave.db.schema import PROJECT_DB_COLUMN_CHECKS, PROJECT_DB_TABLES
from hiveweave.services.acl_sandbox import service as S
from hiveweave.tools.bash import _SHELL_FACT_FLAG_KEYS, _native_shaped

NEW_COLUMNS = ("denied_by", "blocked_by_environment", "sealed_by")
ROOT = r"D:\PC_AI\Project\HiveTestProject\TEST_DSH_65"


def _fresh_run_steps_db() -> sqlite3.Connection:
    """按**生产同款语义**跑一遍 DDL 列表（`db/project.py:214-221`）。

    生产只吞 `ALTER` 的失败（列已存在），其余异常照抛 ⇒ 这里逐字镜像，
    于是「迁移断裂」（ALTER 被吞且 CREATE 没带新列）会被下面的 PRAGMA 抓到。
    """
    conn = sqlite3.connect(":memory:")
    for sql in PROJECT_DB_TABLES:
        if not isinstance(sql, str) or "run_steps" not in sql:
            continue
        try:
            conn.execute(sql)
        except Exception:
            if not sql.strip().upper().startswith("ALTER"):
                raise
    return conn


# ── 验收 1：三列存在、无 DEFAULT、可空 ──────────────────────


def test_columns_exist_without_default():
    conn = _fresh_run_steps_db()
    info = {r[1]: r for r in conn.execute("PRAGMA table_info(run_steps)")}
    for col in NEW_COLUMNS:
        assert col in info, f"缺列 {col}"
        row = info[col]
        assert row[4] is None, f"{col} 不得有 DEFAULT（现为 {row[4]!r}）"
        assert row[3] == 0, f"{col} 必须可空（NULL = 未记录）"
    conn.close()


def test_canonical_ddl_also_declares_them():
    """正典 DDL 与 ALTER 两条路都要有（旧库走 ALTER、新库走 CREATE）。"""
    ddl = next(
        d for d in PROJECT_DB_TABLES
        if isinstance(d, str) and "CREATE TABLE IF NOT EXISTS run_steps" in d
    )
    for col in NEW_COLUMNS:
        assert col in ddl, f"正典 DDL 缺 {col}"


def test_startup_column_check_registers_them():
    """启动自检（db/project.py 消费）必须盯住三列 —— 漏登记 = 迁移断裂时静默 NULL。"""
    required = PROJECT_DB_COLUMN_CHECKS["run_steps"]
    assert set(NEW_COLUMNS) <= set(required)


# ── 转发面：登记点 + 归一化重建不丢 ──────────────────────────


def test_forward_key_registry_contains_them():
    for col in NEW_COLUMNS:
        assert col in _SHELL_FACT_FLAG_KEYS, f"{col} 未登记 ⇒ 到不了调用方"


def test_native_shaped_keeps_them():
    """`_native_shaped` 会**重建** dict（审计 B：只补白名单会白做）。"""
    out = _native_shaped({
        "stdout": "", "stderr": "Access is denied.", "exit_code": 1,
        "denied_by": "unknown_acl", "blocked_by_environment": True,
        "sealed_by": "acl_lockdown:D:\\x",
    })
    assert out["denied_by"] == "unknown_acl"
    assert out["blocked_by_environment"] is True
    assert out["sealed_by"] == "acl_lockdown:D:\\x"


def test_native_shaped_absent_keys_are_not_invented():
    out = _native_shaped({"stdout": "", "stderr": "", "exit_code": 0})
    for col in NEW_COLUMNS:
        assert col not in out, f"{col} 不得凭空补默认值"


# ── 环境拒绝位：状态判据（exit_code 存在 ⇒ 进程跑过）─────────


def _hint(stderr, exit_code, agent_id):
    S._hint_counts.pop(agent_id, None)
    return S._maybe_append_rejection_hint(
        agent_id, ROOT, {"stderr": stderr, "exit_code": exit_code}
    )


def test_blocked_by_environment_set_when_process_ran():
    res = _hint(f"Access to the path '{ROOT}\\src\\a.txt' is denied.", 1, "p03-bwe")
    assert res["denied_by"] == "no_write_sid"
    assert res["blocked_by_environment"] is True


def test_blocked_by_environment_none_when_no_exit_code():
    """exit_code=None ⇒ 进程未跑（runner 侧）⇒ 不得宣称「环境拒绝」。"""
    res = _hint("Access to the path 'x' is denied.", None, "p03-bwe2")
    assert res.get("denied_by") is None
    assert res.get("blocked_by_environment") is None
