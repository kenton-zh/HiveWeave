"""I14（fixplan 批 5 · 2026-09-27）：``user_pings`` 门禁盲区收口回归。

缺陷原状：``user_pings`` 的 DDL 在 ``api/communications.py::
_ensure_user_pings_table``（**读接口自己 CREATE TABLE**），全仓零 INSERT，
且**不在** ``db/schema.py::PROJECT_DB_TABLES`` —— 正向门禁
（test_every_project_db_table_has_writer.py）只看「清单里的表」，
清单外的表它根本不看 ⇒ 三判据同时成立的死表长期门禁全绿。

本批修法：① DDL 收进正典清单（建表永远只有一条路径）；② 删读接口自建表；
③ 门禁加反向断言（库中表 ≡ 清单，多出即红，见门禁文件）。

本文件钉三件事（对应 fixplan 验收 ①②③）：
1. ``user_pings`` 在正典清单里；
2. ``api/communications.py`` 不再含任何建表路径；
3. 反向断言能被打红（阳性对照：塞清单外表 → 红 → 移除 → 恢复）。

另钉「正典路径建出的表形状 = 旧自建路径形状」（存量库由旧路径建表，
形状分叉 = 旧库读接口静默 500）与「读端点 SQL 在纯正典库上可用」。
"""

from __future__ import annotations

import ast
import re
import sqlite3
from pathlib import Path

from tests.test_every_project_db_table_has_writer import (
    _canonical_table_names,
    _extra_tables_in_project_db,
    _tables_in_fresh_project_db,
)

_COMMUNICATIONS = (
    Path(__file__).resolve().parents[1]
    / "src" / "hiveweave" / "api" / "communications.py"
)

# 旧自建路径（api/communications.py::_ensure_user_pings_table，已删）的列形状。
# 正典 DDL 必须与它逐列一致 —— 存量库的 user_pings 是旧路径建的。
_LEGACY_SHAPE: dict[str, str] = {
    "id": "TEXT",
    "project_id": "TEXT",
    "from_agent_id": "TEXT",
    "message": "TEXT",
    "is_read": "INTEGER",
    "created_at": "INTEGER",
    "read_at": "INTEGER",
}


# ── 验收 ①：user_pings 在正典清单 ──────────────────────────────


def test_user_pings_is_in_canonical_table_list():
    names = _canonical_table_names()
    assert "user_pings" in names, (
        "user_pings 必须在 db/schema.py::PROJECT_DB_TABLES（建表唯一路径）——"
        "它不在清单里 = 门禁对它全盲（I14 原缺陷）。"
    )


def test_user_pings_ddl_lives_only_in_schema_module():
    """全仓 ``src/`` 里 user_pings 的 CREATE TABLE 只允许出现在 db/schema.py。"""
    src_root = Path(__file__).resolve().parents[1] / "src" / "hiveweave"
    offenders: list[Path] = []
    for py in src_root.rglob("*.py"):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue  # 与门禁扫描器同口径：不可解析文件不归本门禁管
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "CREATE TABLE" in node.value and "user_pings" in node.value:
                    offenders.append(py)
    assert offenders == [] or all(
        p.parts[-2:] == ("db", "schema.py") for p in offenders
    ), f"user_pings 的建表 DDL 只允许在 db/schema.py，还出现在：{offenders}"


#: 批 5 审计 P1①：**全仓旁路建表门禁** —— src/ 内一切 CREATE TABLE 字面量
#: 只允许出现在 db/schema.py，**除**以下两类（各有语义）：
#: ① **双 declaring**：该表已在正典清单里（``PROJECT_DB_TABLES``），service
#:    侧 ``_ensure_schema`` 只是幂等兜底（实例：``mcp_servers`` / 
#:    ``agent_waits``）—— 不产生清单外的新表，放行；
#: ② **真旁路挂账**：表只在 service 侧建（历史形成、有真实写入方、非
#:    user_pings 型死表），处置二选一 —— DDL 收编进正典清单，或正式立
#:    「service ensure_schema 家族」为第二条 sanctioned 路径并记台账。
#:    新增旁路表必须在此登记（表名 + 文件），否则本门禁打红。
_BYPASS_TABLE_ALLOWLIST: dict[str, str] = {
    # 表名: 挂账文件（相对 src/hiveweave）
    "audit_retry": "services/audit_retry.py",
    "tool_attestations": "services/attestation.py",  # 与 schema.py:526 双 declaring
    "audit_cache": "services/attestation.py",
    "inbox_triage_batches": "services/inbox_triage.py",
}

#: 与 ``test_every_project_db_table_has_writer`` 同款正则：从 DDL 提取表名。
#: 表名后必须紧跟 ``(`` —— 防 docstring 里「CREATE TABLE IF NOT EXISTS
#: 本身就是幂等的」这类完整短语提及被误抓成表名 "IF"（实测踩过）。
_BYPASS_TABLE_NAME_RE = re.compile(
    r"CREATE TABLE (?:IF NOT EXISTS )?[`\"\[]?(\w+)\s*\(", re.IGNORECASE
)


def test_no_bypass_create_table_outside_schema_module():
    """全仓 ``src/`` 的 CREATE TABLE 字面量只许在 db/schema.py 或挂账白名单。

    批 5 审计 P1①：反向断言（库表 ≡ 清单）只对**正典路径建出的库**有效，
    旁路 CREATE TABLE 在测试里不执行 ⇒ 表不会 materialize ⇒ 断言恒绿 ——
    它防不住「下一个 user_pings」。本门禁补上**静态**那一半：任何新的旁路
    建表（正是一 user_pings 的原始形态）必须先挂账才能合入。
    打红方式：在任意 service/api 文件里新写一个 CREATE TABLE 字面量 ⇒ 本
    测试红，逼你二选一（收编正典 / 白名单挂账）。
    """
    src_root = Path(__file__).resolve().parents[1] / "src" / "hiveweave"
    canonical = _canonical_table_names()
    offenders: list[str] = []
    for py in src_root.rglob("*.py"):
        rel = py.relative_to(src_root).as_posix()
        if rel == "db/schema.py":
            continue  # 正典路径
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if "CREATE TABLE" not in node.value:
                continue
            # 先提取真表名：只「提及 CREATE TABLE 字样」的 docstring/字符串
            # （实测 wait_contract.py:537 / mcp.py:53 的兜底函数 docstring）
            # 提取不出表名 ⇒ 不是建表 DDL，跳过。
            declared = {
                m.group(1)
                for m in _BYPASS_TABLE_NAME_RE.finditer(node.value)
            }
            if not declared:
                continue
            # 类别①：双 declaring —— DDL 里的表**全部**已在正典清单 ⇒ 幂等兜底放行
            if declared <= canonical:
                continue
            # 类别②：真旁路（declared 里有清单外表名）—— 必须挂账
            unregistered = sorted(
                t for t in declared
                if t not in canonical and t not in _BYPASS_TABLE_ALLOWLIST
            )
            misplaced = sorted(
                t for t in declared
                if t in _BYPASS_TABLE_ALLOWLIST
                and _BYPASS_TABLE_ALLOWLIST[t] != rel
            )
            for t in unregistered:
                offenders.append(f"{rel}:{node.lineno}（表 {t} 未登记挂账）")
            for t in misplaced:
                offenders.append(
                    f"{rel}:{node.lineno}（表 {t} 挂账在 "
                    f"{_BYPASS_TABLE_ALLOWLIST[t]}，与此文件不符）"
                )
    assert not offenders, (
        "src/ 内发现正典路径之外的 CREATE TABLE（user_pings 型旁路）：\n"
        + "\n".join(sorted(offenders))
        + "\n处置二选一：① DDL 收编 db/schema.py::PROJECT_DB_TABLES；"
        "② 在 _BYPASS_TABLE_ALLOWLIST 挂账（表名 + 文件 + 处置去向）。"
    )


# ── 验收 ②：读接口不再自建表 ───────────────────────────────────


def test_communications_module_has_no_table_creation():
    """api/communications.py 不得再有任何 CREATE TABLE / _ensure_*_table。"""
    tree = ast.parse(_COMMUNICATIONS.read_text(encoding="utf-8"))
    ensure_defs = [
        n.name
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name.startswith("_ensure_")
    ]
    assert not ensure_defs, f"读接口不得自建表，发现 _ensure_* 函数：{ensure_defs}"
    create_literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and "CREATE TABLE" in node.value
    ]
    assert not create_literals, (
        f"api/communications.py 里仍有建表 SQL 字面量：{create_literals}"
    )


# ── 正典路径形状 = 旧自建路径形状（存量库兼容）──────────────────


async def test_canonical_path_creates_user_pings_with_legacy_shape(tmp_path):
    """ensure_project_db 建出的 user_pings 必须与旧自建路径逐列一致。

    顺带核实 PROJECT_DB_COLUMN_CHECKS fail-loud 机制：本测试走完整
    ensure_project_db 流程（DDL → 列自检 → 索引），自检炸 = 这里先炸。
    """
    from hiveweave.db.project import ensure_project_db

    conn = await ensure_project_db(str(tmp_path))
    cur = await conn.execute("PRAGMA table_info(user_pings)")
    rows = await cur.fetchall()
    await cur.close()
    assert rows, "正典路径没建出 user_pings 表"
    actual = {r[1]: (r[2] or "").upper() for r in rows}
    assert actual == _LEGACY_SHAPE, (
        f"列形状分叉：正典 {actual} ≠ 旧自建形状 {_LEGACY_SHAPE}"
        "（存量库的 user_pings 由旧路径建，形状不一致 = 旧库读接口静默 500）"
    )


# ── 读端点 SQL 在纯正典库上可用（不再自建表也不退化为异常空数组）──


async def test_user_pings_endpoint_sql_works_on_canonical_db(tmp_path):
    """list_user_pings / mark_ping_read 的 SQL 原样跑一遍 —— 表由正典路径
    预先建好，读接口不再有任何建表动作也必须照常工作。"""
    from hiveweave.db.project import ensure_project_db

    conn = await ensure_project_db(str(tmp_path))
    # 造一行（仅测试库；src/ 全仓依旧零 INSERT —— 白名单判定不受影响）
    await conn.execute(
        "INSERT INTO user_pings (id, project_id, from_agent_id, message, "
        "is_read, created_at) VALUES ('p1', 'proj', 'a1', 'hello', 0, 1)"
    )
    await conn.commit()

    # list_user_pings（projectId 分支）的 SELECT
    cur = await conn.execute(
        "SELECT * FROM user_pings WHERE project_id = ? "
        "ORDER BY created_at DESC LIMIT 100",
        ["proj"],
    )
    rows = await cur.fetchall()
    await cur.close()
    assert len(rows) == 1 and dict(rows[0])["message"] == "hello"

    # list_user_pings（agentId 分支 + unreadOnly）的 SELECT
    cur = await conn.execute(
        "SELECT * FROM user_pings WHERE from_agent_id = ? AND is_read = 0 "
        "ORDER BY created_at DESC LIMIT 100",
        ["a1"],
    )
    rows = await cur.fetchall()
    await cur.close()
    assert len(rows) == 1

    # mark_ping_read 的 UPDATE
    await conn.execute(
        "UPDATE user_pings SET is_read = 1, read_at = ? WHERE id = ?",
        [2, "p1"],
    )
    await conn.commit()
    cur = await conn.execute(
        "SELECT * FROM user_pings WHERE from_agent_id = ? AND is_read = 0",
        ["a1"],
    )
    rows = await cur.fetchall()
    await cur.close()
    assert rows == [], "标记已读后 unreadOnly 查询应为空"


# ── 验收 ③：反向断言能被打红（阳性对照）────────────────────────


async def test_reverse_assertion_catches_rogue_table_positive_control(tmp_path):
    """阳性对照：往测试库塞一张清单外表 → 反向断言红 → 移除 → 恢复绿。

    这是对门禁自身的「门禁」：反向断言若恒绿（比如比对集合算错方向），
    它就防不了下一个 user_pings。
    """
    from hiveweave.db.project import ensure_project_db

    # 干净库：反向断言绿
    assert await _extra_tables_in_project_db(tmp_path) == set()

    conn = await ensure_project_db(str(tmp_path))
    await conn.execute(
        "CREATE TABLE rogue_i14_positive_control (id TEXT PRIMARY KEY)"
    )
    await conn.commit()

    extra = await _extra_tables_in_project_db(tmp_path)
    assert extra == {"rogue_i14_positive_control"}, (
        f"反向断言没打红旁路表，实得 {extra} —— 门禁失效（恒绿）"
    )

    # 移除 → 恢复
    await conn.execute("DROP TABLE rogue_i14_positive_control")
    await conn.commit()
    assert await _extra_tables_in_project_db(tmp_path) == set()


async def test_reverse_assertion_would_flag_the_original_user_pings_bug(tmp_path):
    """历史缺陷**真回放**（批 5 审计 P2：原版是手工集合减法的恒真测试，零检出力）。

    I14 原状 = 表真实存在、但不在正典清单。回放：正典路径建一个真库
    （user_pings 表 materialize），再**临时把门禁模块的清单绑定**换成摘除
    user_pings 的版本 ⇒ 反向断言必须恰好命中它。这才会打到真实比对函数
    ``_extra_tables_in_project_db``——将来有人把反向断言删掉/改弱，本测试
    与阳性对照一起转红。
    ⚠ patch 必须打在 ``test_every_project_db_table_has_writer`` 模块上：
    它是 ``from hiveweave.db.schema import PROJECT_DB_TABLES`` **按值导入**，
    patch schema 模块属性对它不可见。
    """
    from unittest.mock import patch

    from hiveweave.db.project import ensure_project_db
    from tests.test_every_project_db_table_has_writer import PROJECT_DB_TABLES

    # 正典路径建库：user_pings 表真实存在（I14 原状的「表在库中」半边）
    await ensure_project_db(str(tmp_path))

    pruned = [ddl for ddl in PROJECT_DB_TABLES if "user_pings" not in ddl]
    assert len(pruned) < len(PROJECT_DB_TABLES), (
        "摘除失败：清单里没有 user_pings 条目？"
    )

    with patch(
        "tests.test_every_project_db_table_has_writer.PROJECT_DB_TABLES", pruned
    ):
        extra = await _extra_tables_in_project_db(tmp_path)

    assert "user_pings" in extra, (
        f"user_pings 从清单摘除后，反向断言必须报它，实得 {extra} —— "
        "I14 原缺陷复燃不可机检"
    )


def test_sqlite_aux_tables_are_not_confused_with_user_tables():
    """反向断言的内部表过滤不许误伤：AUTOINCREMENT 伴生的 sqlite_sequence
    不算「清单外表」（facts 表用 AUTOINCREMENT，全新库必带它）。"""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT)")
    conn.execute("INSERT INTO t DEFAULT VALUES")
    names = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    }
    assert names == {"t"}, f"过滤口径坏了：{names}"
