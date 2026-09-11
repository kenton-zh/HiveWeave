"""批次 7 · ``modules`` 模块树写侧 + 子树读取 + 记忆回指的行为测试。

被测对象：``services/modules.py::ModuleService``（蓝图 `:283-287` 的形状）。

覆盖面（每条都对应交付面里的一个具体承诺）：
1. 建树：``parent_module_id`` 真的建出父子关系（不是存了个没用的字段）；
2. 写侧真的落行（这是批次 5 白名单要求「移出」的前置条件）；
3. 环检测：换父不能把节点移进自己的子树（否则 ``get_subtree`` 死循环）；
4. 子树读取：递归取到孙节点，父先于子；
5. ``status`` 流转与 ``current_agent_id`` 绑定；
6. 删模块：有子模块默认拒绝（fail-closed），``cascade=True`` 才级联；
7. 记忆回指（蓝图 `:299`）：``get_archived_memories_for_module``
   按真实 module_id 取到归档记忆，且**含子模块**、带 ``_via_module_id`` 归因。

纪律：断言用**行为**（读回来的形状 / 抛错），不用文本子串。
涉及 DB 的用例都在临时 workspace 上跑，靠 conftest 的 close_all 收尾。
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from hiveweave.db import meta as meta_db
from hiveweave.db.project import close_all, ensure_project_db
from hiveweave.services.modules import VALID_STATUSES, ModuleService


async def _make_project(tmp_path: Path) -> tuple[str, str]:
    """注册一个临时项目，返回 (project_id, workspace)。"""
    project_id = f"modproj-{uuid.uuid4().hex[:12]}"
    ws = tmp_path / project_id
    ws.mkdir(parents=True, exist_ok=True)
    await meta_db.init_meta_db()
    await meta_db.execute(
        "INSERT INTO projects (id, name, workspace_path, created_at) "
        "VALUES (?, ?, ?, ?)",
        [project_id, "Module Tree Test", str(ws), int(time.time() * 1000)],
    )
    return project_id, str(ws)


@pytest.fixture
async def project(tmp_path):
    project_id, ws = await _make_project(tmp_path)
    yield project_id, ws
    await close_all()


# ── 1 / 2 · 建树 + 真的落行 ─────────────────────────────────


async def test_create_module_writes_a_row(project):
    """写侧真的 INSERT —— 这是白名单「移出」的前提（不是建了个空壳）。"""
    project_id, ws = project
    svc = ModuleService()
    mod = await svc.create_module(project_id, "auth", path="src/auth")

    assert mod["id"]
    assert mod["status"] == "active"
    # 直连 DB 核验落行（不经 service 的读路径，防「写假读真」的假绿）
    conn = await ensure_project_db(ws)
    cursor = await conn.execute(
        "SELECT name, path, parent_module_id, status FROM modules WHERE id = ?",
        [mod["id"]],
    )
    row = await cursor.fetchone()
    await cursor.close()
    assert row is not None, "create_module 没往 modules 表写行"
    assert row["name"] == "auth"
    assert row["path"] == "src/auth"
    assert row["parent_module_id"] is None
    assert row["status"] == "active"


async def test_parent_child_relationship_is_real(project):
    """``parent_module_id`` 真的建出层级，且子树读得回来。"""
    project_id, _ = project
    svc = ModuleService()
    root = await svc.create_module(project_id, "platform")
    child = await svc.create_module(
        project_id, "billing", parent_module_id=root["id"]
    )
    grand = await svc.create_module(
        project_id, "invoices", parent_module_id=child["id"]
    )

    subtree = await svc.get_subtree(project_id, root["id"])
    ids = [m["id"] for m in subtree]
    assert ids[0] == root["id"], "子树必须含 root 且 root 在最前"
    assert child["id"] in ids
    assert grand["id"] in ids
    assert len(ids) == 3
    # 父先于子
    assert ids.index(root["id"]) < ids.index(child["id"]) < ids.index(grand["id"])


async def test_subtree_of_leaf_is_itself(project):
    project_id, _ = project
    svc = ModuleService()
    leaf = await svc.create_module(project_id, "solo")
    subtree = await svc.get_subtree(project_id, leaf["id"])
    assert [m["id"] for m in subtree] == [leaf["id"]]


async def test_create_rejects_missing_parent(project):
    """挂到不存在的父 → 报错（防孤儿节点，不是静默建出来）。"""
    project_id, _ = project
    svc = ModuleService()
    with pytest.raises(ValueError, match="parent module not found"):
        await svc.create_module(
            project_id, "orphan", parent_module_id="no-such-module-id"
        )


async def test_create_rejects_blank_name_and_bad_status(project):
    project_id, _ = project
    svc = ModuleService()
    with pytest.raises(ValueError, match="name is required"):
        await svc.create_module(project_id, "   ")
    with pytest.raises(ValueError, match="invalid module status"):
        await svc.create_module(project_id, "x", status="nonsense")


# ── 3 · 环检测 ─────────────────────────────────────────────


async def test_update_rejects_moving_node_under_its_own_descendant(project):
    """换父时环检测：不能把节点移进自己的子树（否则 get_subtree 死循环）。"""
    project_id, _ = project
    svc = ModuleService()
    root = await svc.create_module(project_id, "root")
    child = await svc.create_module(
        project_id, "child", parent_module_id=root["id"]
    )

    with pytest.raises(ValueError, match="descendant"):
        await svc.update_module(
            project_id, root["id"], parent_module_id=child["id"]
        )
    # 未变（拒绝是真拒绝，不是改了一半）
    fresh = await svc.get_module(project_id, root["id"])
    assert fresh["parent_module_id"] is None


async def test_update_rejects_self_parent(project):
    project_id, _ = project
    svc = ModuleService()
    m = await svc.create_module(project_id, "self")
    with pytest.raises(ValueError, match="cannot be its own parent"):
        await svc.update_module(project_id, m["id"], parent_module_id=m["id"])


async def test_update_allows_legitimate_reparent(project):
    """合法的换父要能成功（证明上面的拒绝不是「一律拒绝」的假门）。"""
    project_id, _ = project
    svc = ModuleService()
    a = await svc.create_module(project_id, "a")
    b = await svc.create_module(project_id, "b")
    moved = await svc.update_module(project_id, b["id"], parent_module_id=a["id"])
    assert moved["parent_module_id"] == a["id"]


# ── 4 / 5 · status 流转与负责人绑定 ────────────────────────


async def test_status_transition_and_bind_agent(project):
    project_id, _ = project
    svc = ModuleService()
    m = await svc.create_module(project_id, "feature")
    assert m["current_agent_id"] is None

    done = await svc.update_module(project_id, m["id"], status="completed")
    assert done["status"] == "completed"
    assert done["status"] in VALID_STATUSES

    bound = await svc.bind_agent(project_id, m["id"], "agent-xyz")
    assert bound["current_agent_id"] == "agent-xyz"
    unbound = await svc.bind_agent(project_id, m["id"], None)
    assert unbound["current_agent_id"] is None


async def test_update_rejects_bad_status(project):
    project_id, _ = project
    svc = ModuleService()
    m = await svc.create_module(project_id, "s")
    with pytest.raises(ValueError, match="invalid module status"):
        await svc.update_module(project_id, m["id"], status="nope")


async def test_list_modules_filters_by_status(project):
    project_id, _ = project
    svc = ModuleService()
    await svc.create_module(project_id, "live")
    dead = await svc.create_module(project_id, "old")
    await svc.update_module(project_id, dead["id"], status="archived")

    allm = await svc.list_modules(project_id)
    assert len(allm) == 2
    active = await svc.list_modules(project_id, status="active")
    assert [m["name"] for m in active] == ["live"]


# ── 6 · 删除 ───────────────────────────────────────────────


async def test_delete_refuses_while_children_exist(project):
    """有子模块时默认拒绝（fail-closed）—— 静默级联会连带丢掉记忆归属。"""
    project_id, _ = project
    svc = ModuleService()
    root = await svc.create_module(project_id, "root")
    child = await svc.create_module(
        project_id, "child", parent_module_id=root["id"]
    )

    with pytest.raises(ValueError, match="descendant"):
        await svc.delete_module(project_id, root["id"])
    # 都还在
    assert await svc.get_module(project_id, root["id"]) is not None
    assert await svc.get_module(project_id, child["id"]) is not None


async def test_delete_cascade_removes_whole_subtree(project):
    project_id, _ = project
    svc = ModuleService()
    root = await svc.create_module(project_id, "root")
    child = await svc.create_module(
        project_id, "child", parent_module_id=root["id"]
    )
    grand = await svc.create_module(
        project_id, "grand", parent_module_id=child["id"]
    )

    ok = await svc.delete_module(project_id, root["id"], cascade=True)
    assert ok is True
    assert await svc.get_module(project_id, root["id"]) is None
    assert await svc.get_module(project_id, child["id"]) is None
    assert await svc.get_module(project_id, grand["id"]) is None


async def test_delete_leaf_only_needs_no_cascade(project):
    project_id, _ = project
    svc = ModuleService()
    leaf = await svc.create_module(project_id, "leaf")
    assert await svc.delete_module(project_id, leaf["id"]) is True
    assert await svc.get_module(project_id, leaf["id"]) is None


async def test_delete_missing_module_returns_false(project):
    project_id, _ = project
    svc = ModuleService()
    assert await svc.delete_module(project_id, "ghost") is False


# ── 7 · 记忆回指（蓝图 :299）───────────────────────────────


async def test_archived_memories_resolve_by_real_module_id(project):
    """归档记忆能按**真实 module_id** 取回，且含子模块、带来源归因。"""
    from hiveweave.services.memory import MemoryService

    project_id, _ = project
    svc = ModuleService()
    mem = MemoryService()

    root = await svc.create_module(project_id, "platform")
    child = await svc.create_module(
        project_id, "networking", parent_module_id=root["id"]
    )

    # 两条归档记忆：一条挂 root，一条挂 child（模拟两任负责人的冻结经验）
    await mem.save_memory(
        agent_id="ex-dev-1", project_id=project_id, scope="archive",
        content="root module lesson", module_id=root["id"],
    )
    await mem.save_memory(
        agent_id="ex-dev-2", project_id=project_id, scope="archive",
        content="child module lesson", module_id=child["id"],
    )

    # 默认含子模块 → 两条都取到
    both = await svc.get_archived_memories_for_module(project_id, root["id"])
    contents = {m["content"] for m in both}
    assert contents == {"root module lesson", "child module lesson"}
    # 归因：每条都知道自己是从哪个模块捞上来的
    via = {m["content"]: m["_via_module_id"] for m in both}
    assert via["root module lesson"] == root["id"]
    assert via["child module lesson"] == child["id"]

    # 关掉子模块展开 → 只命中 root 自己的那条
    direct = await svc.get_archived_memories_for_module(
        project_id, root["id"], include_descendants=False
    )
    assert [m["content"] for m in direct] == ["root module lesson"]


async def test_archived_memories_empty_for_module_without_history(project):
    """无历史模块返回空列表（不是报错、也不是编造条目）。"""
    project_id, _ = project
    svc = ModuleService()
    m = await svc.create_module(project_id, "fresh")
    assert await svc.get_archived_memories_for_module(project_id, m["id"]) == []
