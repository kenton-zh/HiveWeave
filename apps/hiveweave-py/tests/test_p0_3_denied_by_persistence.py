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
from hiveweave.tools.fact_positions import classify_denied_by, sealed_match
from hiveweave.tools.result import SEALED_BY_PREFIX

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


# ── Stage 2b：封条知识的**携带**（装配阶段记住 → 执行阶段读取）────


class _Policy:
    """最小假 policy（只带封条携带用到的两个根）。"""

    def __init__(self, boundary: str, project: str) -> None:
        self.boundary_root = boundary
        self.project_root = project


def test_sealed_carry_and_lookup_by_boundary():
    policy = _Policy(ROOT, ROOT)
    S._remember_sealed(policy, [f"seal:{ROOT}\\.git\\config"])
    got = S._sealed_for_boundary(ROOT)
    assert got == (f"seal:{ROOT}\\.git\\config",)
    # 别的边界查不到（不许串台）
    assert S._sealed_for_boundary(r"D:\\other\\place") == ()
    assert S._sealed_for_boundary(None) == ()


def test_no_seal_recorded_means_empty_not_guessed():
    S._remember_sealed(_Policy(r"D:\\no\\seal", r"D:\\no\\seal"), [])
    assert S._sealed_for_boundary(r"D:\\no\\seal") == ()


def test_sealed_match_returns_raw_target():
    stamped = [f"seal:{ROOT}\\.git\\config", f"deny-dc-all:{ROOT}\\.git"]
    # 目录级封条覆盖其子项
    assert sealed_match(
        f"Access to the path '{ROOT}\\.git\\index.lock' is denied.", stamped
    ) == f"{ROOT}\\.git"
    # 文件级封条
    assert sealed_match(
        f"Access to the path '{ROOT}\\.git\\config' is denied.", stamped
    ) == f"{ROOT}\\.git\\config"
    assert sealed_match("Access to the path 'D:\\tmp\\a' is denied.", stamped) is None


def test_acceptance5_git_index_lock_is_sealed_git():
    """⭐ §1 验收 5：受限命令写 `.git/index.lock` ⇒ `denied_by='sealed_git'`
    且 `sealed_by LIKE 'acl_lockdown%'`。

    夹具走**真实链路的一半**：装配阶段记住封条（`_remember_sealed`）→ 执行阶段
    的拒绝提示点（`_maybe_append_rejection_hint`）读到它并改判成因。
    """
    S._remember_sealed(_Policy(ROOT, ROOT), [
        f"create+seal:{ROOT}\\.git\\config",
        f"deny-dc-all:{ROOT}\\.git",
    ])
    res = _hint(
        f"Set-Content: Access to the path '{ROOT}\\.git\\index.lock' is denied.",
        1,
        "p03-accept5",
    )
    assert res["denied_by"] == "sealed_git"
    assert res["sealed_by"].startswith(SEALED_BY_PREFIX), res["sealed_by"]
    assert res["sealed_by"] == f"{SEALED_BY_PREFIX}{ROOT}\\.git"
    # 文案也必须换成「封条」口径（不能再劝去申请豁免）
    assert "封条" in res["stderr"]
    assert "之外" not in res["stderr"]


def test_sealed_mark_absent_when_not_sealed():
    """没命中封条 ⇒ 不得凭空写 `sealed_by`（NULL = 不知道）。"""
    res = _hint(
        f"Access to the path '{ROOT}\\src\\a.txt' is denied.", 1, "p03-noseal"
    )
    assert res["denied_by"] == "no_write_sid"
    assert "sealed_by" not in res


# ── Stage 2c：门去噪（`warning:` 行不是拒绝证据）+ 中文方言（A4）──


def test_pytest_warning_is_not_a_rejection():
    """⭐ 实证形态：19/53 条历史提示行的 stderr 是 **pytest 的非致命警告**
    （`warning: could not open directory 'pytest-cache-files-X/': Permission denied`）
    ⇒ 旧门据此追加「写入被沙箱拒绝：目标在授权树之外」——**那行没有发生任何写入拒绝**。
    """
    from hiveweave.tools.fact_positions import is_acl_rejection

    warning_only = (
        "warning: could not open directory 'pytest-cache-files-c3zwxps4/': "
        "Permission denied\nhead: The term 'head' is not recognized as a name\n"
    )
    assert is_acl_rejection(warning_only, 1) is False
    res = _hint(warning_only, 1, "p03-noise")
    assert "denied_by" not in res
    assert "[沙箱提示]" not in res["stderr"]


def test_real_denial_alongside_warning_still_counts():
    """同一段里有警告 + **真的**拒绝行 ⇒ 仍是拒绝（逐行判定，不是整段否定）。"""
    from hiveweave.tools.fact_positions import is_acl_rejection

    blob = (
        "warning: could not open directory 'x/': Permission denied\n"
        f"Out-File: Access to the path '{ROOT}\\src\\a.txt' is denied.\n"
    )
    assert is_acl_rejection(blob, 1) is True
    assert classify_denied_by(blob, 1, boundary_root=ROOT) == "no_write_sid"


def test_chinese_acl_dialect_is_recognized():
    """A4：中文 Windows 的 ACL 文案此前是**死支**（既不追加提示也不落位）。"""
    from hiveweave.tools.fact_positions import is_acl_rejection

    chinese = f"Out-File: 对路径“{ROOT}\\src\\a.txt”的访问被拒绝。"
    assert is_acl_rejection(chinese, 1) is True
    assert classify_denied_by(chinese, 1, boundary_root=ROOT) == "no_write_sid"
    inside = classify_denied_by(
        f"对路径“{ROOT}\\.hiveweave\\reports\\x”的访问被拒绝。", 1,
        boundary_root=ROOT,
    )
    assert inside == "no_write_sid"


def test_chinese_outside_path():
    assert classify_denied_by(
        "对路径“D:\\tmp\\a.txt”的访问被拒绝。", 1, boundary_root=ROOT
    ) == "outside_boundary"
