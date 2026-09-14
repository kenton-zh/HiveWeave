"""`tasks.kind` 字段（#11 阶段 A）：闭合枚举语义 + 落库往返 + 「不读标题」。

## 背景

#11 的病灶：VERIFY 的判定用的是**任务标题**（`is_verify_title`，文本判据）——
改标题即可翻转全部验收门与串行锁：真验收写成「验收：xxx」⇒ 不被认；
普通任务加 `VERIFY:` 前缀 ⇒ 伪装成 VERIFY ⇒ 拿到 VERIFY 的隔离门/MAIN 证据闸/
串行锁。

阶段 A（本文件覆盖）只做**纯增量**：加列 + 创建点写值 + 闭合枚举访问器。
**此时还没有任何消费方读它** ⇒ 行为零变化（翻转是阶段 B）。

## 本文件钉三件事

1. **闭合枚举**：非成员/缺失 ⇒ None（「未知」**不猜**，对齐
   `services/delivery_plane.py::normalize_delivery_plane` 的范式）；
2. **落库往返**：`create_task(kind=…)` 写进去、`get_task`/`list_tasks` 读得回
   —— 后者靠 `crud._COLUMNS` 含 `kind`（漏了的话字段恒 None，是"单测绿、生产
   里字段恒空"那个坑）；
3. **判定与标题完全无关**：`kind='verify'` 配普通标题 ⇒ 认；标题像 VERIFY
   但没有 kind ⇒ **不认**（这正是验收②「普通任务加前缀不被认」）。
"""

from __future__ import annotations

import ast
import pathlib
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import task as task_module
from hiveweave.services.task import TaskService
from hiveweave.services.tasks.crud import CrudMixin
from hiveweave.services.tasks.verify import (
    VERIFY_KIND,
    is_verify_task,
    task_kind,
)

PROJECT_ID = "test-kind-project"
AGENT_ID = "test-kind-agent"


# ── 1. 闭合枚举（纯函数） ────────────────────────────────────────


def test_task_kind_is_a_closed_enum_unknown_is_none():
    """非成员/缺失/脏类型 ⇒ None（**不猜**）。"""
    assert task_kind({"kind": VERIFY_KIND}) == VERIFY_KIND
    assert task_kind({"kind": "VERIFY"}) == VERIFY_KIND, "大小写/空白应归一"
    assert task_kind({"kind": "  Verify  "}) == VERIFY_KIND
    for bad in (None, {}, {"kind": None}, {"kind": ""}, {"kind": "task"},
                {"kind": "verification"}, {"kind": 42}, "not-a-dict", []):
        assert task_kind(bad) is None, f"非法输入必须返回 None（不猜）：{bad!r}"


def test_verify_judgment_reads_kind_not_title():
    """判定只认 `kind`；标题**完全不参与**。

    这是 #11 的核心验收：① 真 VERIFY 换个标题照样被认；② 普通任务加
    `VERIFY:` 前缀**不被认**。
    """
    # ① kind 对 ⇒ 标题长什么样都被认（含「验收：xxx」这个原判据认不出的形态）
    for title in ("验收：M4 收口", "QA：收口", "随便什么标题", None, ""):
        assert is_verify_task({"kind": VERIFY_KIND, "title": title}) is True

    # ② 标题像 VERIFY 但没 kind ⇒ **不认**（旧的文本判据在这里会误认）
    for title in ("VERIFY: 伪造的", "【VERIFY: 伪造的】", "VERIFY：伪造的"):
        assert is_verify_task({"title": title}) is False, (
            f"普通任务加 VERIFY 前缀不得被认成 VERIFY（验收②）：{title!r}"
        )
        assert is_verify_task({"kind": None, "title": title}) is False
        assert is_verify_task({"kind": "task", "title": title}) is False

    assert is_verify_task(None) is False


def test_wide_select_columns_include_kind():
    """`crud._COLUMNS` 必须含 `kind`。

    漏了它的后果是"单测全绿、生产里字段恒 None"—— 宽查询
    （`SELECT {_COLUMNS} FROM tasks`）会把字段悄悄吞掉，而所有消费点读到的
    都是 None ⇒ 归因静默降级。（本仓踩过同款：`runner_failed`/`dialect_failed`
    都因漏登记字段白名单而恒 None。）
    """
    cols = [c.strip() for c in CrudMixin._COLUMNS.split(",")]
    assert "kind" in cols, "`_COLUMNS` 漏了 kind ⇒ 宽查询读不到它"


# ── 2. 落库往返（穿透调用链） ────────────────────────────────────


@pytest.fixture
async def env():
    """真实 per-project DB + mock meta 路由；**teardown 先关连接再删目录**
    （Windows 上打开的文件句柄会挡住临时目录删除 —— 照抄
    `tests/test_task_service.py` 的 Windows-safe 清理）。
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        async def fake_get_agent_project_id(aid: str):
            return PROJECT_ID if aid == AGENT_ID else None

        task_module._migrated.clear()
        project_db._agent_cache.pop(AGENT_ID, None)

        with patch("hiveweave.db.meta.get_project_workspace",
                   fake_get_project_workspace), \
             patch("hiveweave.db.meta.get_agent_project_id",
                   fake_get_agent_project_id):
            yield {"project_id": PROJECT_ID, "workspace": workspace_path,
                   "agent_id": AGENT_ID}

        # Cleanup: 先关掉缓存连接，再让 tempdir 删目录。
        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
        project_db._agent_cache.pop(AGENT_ID, None)


@pytest.mark.asyncio
async def test_kind_round_trips_through_create_and_read(env):
    """`create_task(kind=…)` 写进去、`get_task`/`list_tasks` 读得回。"""
    ts = TaskService()

    plain_id = await ts.create_task(
        env["project_id"], "普通任务", "desc", env["agent_id"]
    )
    verify_id = await ts.create_task(
        env["project_id"], "随便什么标题都行", "desc", env["agent_id"],
        source="system", kind=VERIFY_KIND,
    )

    plain = await ts.get_task(env["project_id"], plain_id)
    verify = await ts.get_task(env["project_id"], verify_id)

    assert plain is not None and verify is not None
    assert task_kind(plain) is None, "普通任务必须留 NULL（不是 DEFAULT 值）"
    assert is_verify_task(verify) is True, (
        "kind 没落库或宽查询没带上它 —— 这是阶段 B 翻转的前提"
    )

    # 列表路径同样要带上（消费点多半走 list_tasks）
    listed = {t["id"]: t for t in await ts.list_tasks(env["project_id"])}
    assert task_kind(listed[verify_id]) == VERIFY_KIND
    assert task_kind(listed[plain_id]) is None


@pytest.mark.asyncio
async def test_column_exists_in_a_freshly_created_db(env):
    """新库直接就有该列（正典 DDL 生效，不依赖 ALTER 路径）。"""
    conn = await project_db.ensure_project_db(env["workspace"])
    cur = await conn.execute("SELECT name FROM pragma_table_info('tasks')")
    cols = {r[0] for r in await cur.fetchall()}
    assert "kind" in cols


# ── 4. 「迁移必须有终点」的机械守卫（#11 翻转） ─────────────────


def test_title_judgment_has_no_runtime_call_site():
    """AST 守卫：`is_verify_title` **只允许**出现在回填迁移模块里。

    #11 的计划原话：「回填完成后 `is_verify_title` **只保留在迁移脚本里、运行时
    不再调用**」。光删掉定义不够 —— 有人随时可以在某个服务里重新 import 它
    （那个名字没了就写个新的文本判据），所以这条守卫盯的是**引用面**。

    ⚠ 用 **AST** 而不是 grep：docstring / 注释里提到这个名字（本仓大量存在，
    因为它们要解释"为什么删掉它"）**不算调用**。grep 会把它们全部误报。
    """
    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "hiveweave"
    allowed = {"services/tasks/migrate_verify_kind.py"}
    offenders: list[str] = []
    for py in sorted(src.rglob("*.py")):
        rel = py.relative_to(src).as_posix()
        if rel in allowed:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "is_verify_title":
                offenders.append(f"{rel}:{node.lineno}")
            elif isinstance(node, ast.Attribute) and node.attr == "is_verify_title":
                offenders.append(f"{rel}:{node.lineno}")
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "is_verify_title":
                        offenders.append(f"{rel}:{node.lineno} (import)")
    assert not offenders, (
        "运行时仍在引用标题判据 is_verify_title（#11 要求运行时零调用）：\n  "
        + "\n  ".join(offenders)
        + "\n判定应改用 is_verify_task(task)（读 `kind` 字段）。"
    )


def test_title_regex_lives_only_in_the_migration_module():
    """旧标题正则只能留在迁移模块里（避免"删了定义、换个地方又写一份"）。"""
    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "hiveweave"
    hits: list[str] = []
    for py in sorted(src.rglob("*.py")):
        rel = py.relative_to(src).as_posix()
        text = py.read_text(encoding="utf-8")
        # 注释/docstring 里点名"原先定义过 _VERIFY_TITLE_RE"是允许的（解释性），
        # 故这里只禁**赋值**（真正再造一份判据表）。
        if "_VERIFY_TITLE_RE = " in text and "_LEGACY_VERIFY_TITLE_RE" not in text:
            hits.append(rel)
    assert hits == [], f"这些文件又造了一份标题判据表：{hits}"


def test_verify_module_no_longer_defines_the_title_judgment():
    """`verify.py` 本身不能再有旧判据的定义（它是运行时模块）。"""
    import hiveweave.services.tasks.verify as v

    assert not hasattr(v, "is_verify_title"), (
        "verify.py 仍定义着 is_verify_title —— 它已迁到迁移模块，运行时模块不该再有"
    )
    assert not hasattr(v, "_VERIFY_TITLE_RE")
    # 反向：新判据必须在
    assert hasattr(v, "is_verify_task") and hasattr(v, "VERIFY_KIND")


async def _insert_raw(env, *, task_id: str, title: str, created_at: int,
                      kind: str | None = None) -> None:
    """直接插一行（绕开 create_task，好控制 created_at / kind）。"""
    from hiveweave.services.tasks.db import _execute

    await _execute(
        env["project_id"],
        "INSERT INTO tasks (id, project_id, title, description, creator_id, "
        "status, created_at, updated_at, kind) VALUES (?,?,?,?,?,?,?,?,?)",
        [task_id, env["project_id"], title, "", "a1", "created",
         created_at, created_at, kind],
    )


async def _kinds(env) -> dict[str, str | None]:
    from hiveweave.services.tasks.db import _query

    return {
        r["id"]: r["kind"]
        for r in await _query(env["project_id"], "SELECT id, kind FROM tasks")
    }


@pytest.mark.asyncio
async def test_backfill_promotes_only_pre_cutover_rows(env):
    """回填只动 cutover **之前**创建的行 —— 这是"不能重跑"的那条界限。"""
    from hiveweave.services.tasks.migrate_verify_kind import (
        CUTOVER_MS,
        backfill_verify_kind,
    )

    before, after = CUTOVER_MS - 1_000, CUTOVER_MS + 1_000
    await _insert_raw(env, task_id="legacy-verify",
                      title="VERIFY: 老验收", created_at=before)
    await _insert_raw(env, task_id="legacy-bracket",
                      title="【VERIFY: 老验收2】", created_at=before)
    await _insert_raw(env, task_id="legacy-normal",
                      title="普通任务", created_at=before)
    # ⚠ 关键：cutover 之后新建、标题恰好像 VERIFY 的**普通任务** —— 不得升格。
    # 若升格，agent 只要起个这样的名字就白拿 VERIFY 的隔离门/串行锁（#11 的伪造）。
    await _insert_raw(env, task_id="new-looking",
                      title="VERIFY: 我自己起的名字", created_at=after)
    await _insert_raw(env, task_id="new-real",
                      title="新验收", created_at=after, kind=VERIFY_KIND)

    stats = await backfill_verify_kind(env["project_id"])
    assert stats["backfilled"] == 2, "只应回填两条存量"
    assert stats["post_cutover_matches"] == 1, "cutover 后的同名行要被记 warning（fail-loud）"

    kinds = await _kinds(env)
    assert kinds["legacy-verify"] == VERIFY_KIND
    assert kinds["legacy-bracket"] == VERIFY_KIND
    assert kinds["legacy-normal"] is None, "存量普通任务必须保持 NULL"
    assert kinds["new-looking"] is None, (
        "★ cutover 之后新建的普通任务不得被回填升格 —— 回填一旦重跑就会造成"
        "「起个像 VERIFY 的标题就拿到 VERIFY 门」的伪造面。"
    )
    assert kinds["new-real"] == VERIFY_KIND


@pytest.mark.asyncio
async def test_backfill_is_idempotent(env):
    """重跑是 no-op（幂等 ⇒ 可以安全地放在每次 `_ensure_schema` 里）。"""
    from hiveweave.services.tasks.migrate_verify_kind import (
        CUTOVER_MS,
        backfill_verify_kind,
    )

    await _insert_raw(env, task_id="v1", title="VERIFY: x",
                      created_at=CUTOVER_MS - 1)
    assert (await backfill_verify_kind(env["project_id"]))["backfilled"] == 1
    assert (await backfill_verify_kind(env["project_id"]))["backfilled"] == 0
    assert (await _kinds(env))["v1"] == VERIFY_KIND


@pytest.mark.asyncio
async def test_backfill_reproduces_the_old_judgment_exactly(env):
    """验收③的行为侧：回填**照抄**旧判据，连它的误判一起抄。

    否则等于在迁移里偷偷改行为 —— 而这类改动没有任何显式信号。

    · 「验收：M4」在**旧**判据下**不**被认（这正是 #11 的病灶）⇒ 回填也不认。
    · 「VERIFY: 名义上是验收但其实是个普通活」在旧判据下**被认** ⇒ 回填也认。
    """
    from hiveweave.services.tasks.migrate_verify_kind import (
        CUTOVER_MS,
        backfill_verify_kind,
        is_legacy_verify_title,
    )

    assert is_legacy_verify_title("验收：M4 收口") is False
    assert is_legacy_verify_title("VERIFY: x") is True
    assert is_legacy_verify_title("VERIFY：x") is True
    assert is_legacy_verify_title("[VERIFY: x]") is True

    t = CUTOVER_MS - 1
    await _insert_raw(env, task_id="colon-cn", title="验收：M4 收口", created_at=t)
    await _insert_raw(env, task_id="fake-verify", title="VERIFY: 名义上是验收但其实是个普通活",
                      created_at=t)
    await backfill_verify_kind(env["project_id"])

    kinds = await _kinds(env)
    assert kinds["colon-cn"] is None, (
        "旧判据认不出的形态，回填也必须认不出 —— 否则行为不一致"
    )
    assert kinds["fake-verify"] == VERIFY_KIND, (
        "旧判据认得出的形态，回填也必须认 —— 照抄包括它的误判（当时的实际行为）"
    )


@pytest.mark.asyncio
async def test_positive_control_clearing_kind_can_be_restored(env):
    """验收④阳性对照：把 `kind` 全清回 NULL ⇒ 回填能复原。

    （等价于"回填漏跑了"这个最危险的情形，能不能被救回来。）
    """
    from hiveweave.services.tasks.db import _execute
    from hiveweave.services.tasks.migrate_verify_kind import (
        CUTOVER_MS,
        backfill_verify_kind,
    )

    await _insert_raw(env, task_id="v1", title="VERIFY: 甲",
                      created_at=CUTOVER_MS - 1)
    await _insert_raw(env, task_id="v2", title="VERIFY: 乙",
                      created_at=CUTOVER_MS - 1)
    await backfill_verify_kind(env["project_id"])
    assert (await _kinds(env))["v1"] == VERIFY_KIND

    # 模拟"回填漏跑/被清空"（真实事故形态：库整代重建回到基础列）
    await _execute(env["project_id"], "UPDATE tasks SET kind = NULL")
    assert set((await _kinds(env)).values()) == {None}

    stats = await backfill_verify_kind(env["project_id"])
    assert stats["backfilled"] == 2, "清空后回填必须能复原"
    assert (await _kinds(env))["v1"] == VERIFY_KIND
    assert (await _kinds(env))["v2"] == VERIFY_KIND
