"""D67-1：`event_audit` 对**子代理**（`sub-*` 运行时临时身份）100% 写失败。

## 病灶（TEST_DSH_67 巡检实测，非推断）

`apps/desktop/dist/HiveWeave/data/logs/launcher.out.log` 里 `event_audit.write_failed`
**36 次**（`grep -c`），**36/36 全是 `sub-*`**（非 `sub-` 计数 = **0**），跨 **6** 个父代理、
持续 **167.9 分钟**（12:28:51 → 15:16:47）—— 不是偶发失败，是**按身份类别切分的完备子集**。

根因：`_write()` 只把 `agent_id` 交给 `project_db.execute` → `AgentRouter`
**内存表**。而 `AgentRouter.rebuild()` 只登记 `agents` 表里 `status='active'`
的行，`sub-<parent>-<suffix>` 是**运行时临时生成、从不入库**的 ⇒ 内存表里
**必然没有** ⇒ 解析失败 ⇒ 整条事件丢弃。`log()` 的 docstring 自己写着
「project_id is accepted for API compatibility **but routing uses agent_id**」
—— **明知有 project_id 却不用**。

## 修（本文件守的判据，全部落状态）

知识在**子代理产生的那一刻**就存在（父的 `project_id`）⇒ 按「状态判据」纪律，
它在那个边界被写进 `AgentRouter` 的**瞬态身份表**（`register_transient`），
而不是运行期去解析 id 的字面形状（那是文本判据，换前缀即失效）。消费侧
`event_audit._resolve_project_id` 是**唯一的源选定点**（正式路由 → 瞬态身份
→ 显式参数 → 有声失败）。

## 覆盖（缺一即假绿）

- **A 正向**：登记瞬态身份后，`sub-*` 事件**真落 `agent_events` 表**（查表，不看测试绿）。
- **B 反向**：不登记 + `project_id=""` ⇒ **写口一次都没被调用**（硬状态）
  且**有声**报 `write_failed`（不静默丢）。
- **C 兜底**：id 不可解析 + 显式 `project_id` 真值 ⇒ 照样落库。
- **D 零污染**：瞬态身份**不进** `list_active_routes` / `get_project_agent_ids`；
  `get_project_id("sub-…")` **仍是 None**（正式路由语义一字未改 ⇒ 爆炸半径为 0）。
- **E 有界**：登记超过 `_TRANSIENT_MAX` 后表长不超上限（异常路径不漏内存）。

⚠ 阳性对照（改坏必须转红）：
  · `_resolve_project_id` 去掉「瞬态身份」那一级 ⇒ **A** 红（事件落不了库）；
  · `_write` 把「解析不出」改成静默 return ⇒ **B** 的声音判据红；
  · `register_transient` 塞进 `_routes`（而非独立的 `_transient`）⇒ **D** 红。
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, patch

import pytest
import structlog.testing

from hiveweave.db import project as project_db
from hiveweave.services.agent_router import _TRANSIENT_MAX, agent_router
from hiveweave.services.event_audit import _resolve_project_id, event_audit

# 复用 0-3 那套现成夹具（建项目库 + 打桩 `meta_db.get_project_workspace`）；
# ⚠ `task_env` 必须 import 进本模块命名空间，否则 pytest 报 fixture not found。
from tests.test_idle_architecture_p0 import PROJECT_ID, task_env  # noqa: F401
from tests.test_offturn import clean_offturn  # noqa: F401

SUB_ID = "sub-exec-1-deadbeef"


@contextlib.contextmanager
def _capture_tasks():
    """捕获 `asyncio.create_task` 的产物 —— 让 fire-and-forget 变得**确定**。

    为什么不用 `await asyncio.sleep(0)` 轮询：那是**时序判据**，随调度器实现
    与事件循环负载漂移（审计 P2-d：本文件里同一形态散落多处）。捕获后
    `gather` 是确定性的。
    """
    created: list[asyncio.Task] = []
    real = asyncio.create_task

    def _spy(coro, *a, **k):
        task = real(coro, *a, **k)
        created.append(task)
        return task

    with patch("asyncio.create_task", _spy):
        yield created


@pytest.fixture
def clean_router():
    """瞬态表/正式路由都是进程级单例 —— 每条用例前后清干净，避免串味。"""
    agent_router.reset_for_tests()
    yield
    agent_router.reset_for_tests()


async def _events_for(agent_id: str) -> list:
    rows = await project_db.query_by_project(
        PROJECT_ID,
        "SELECT agent_id, event_type FROM agent_events WHERE agent_id = ?",
        [agent_id],
    )
    return [dict(r) for r in rows]


# ── A 正向：子代理身份登记后，事件真落库 ─────────────────────


async def test_transient_identity_makes_subagent_events_land(task_env, clean_router):
    """A：`sub-*` 登记瞬态身份 ⇒ 事件**真落 `agent_events`**（查表判据）。"""
    agent_router.register_transient(SUB_ID, PROJECT_ID)

    await event_audit._write(SUB_ID, "", "llm_error.upstream", {"k": "v"})

    rows = await _events_for(SUB_ID)
    assert len(rows) == 1, f"子代理事件必须落库，实得 {rows!r}"
    assert rows[0]["event_type"] == "llm_error.upstream"
    # 事件行的 agent_id 仍是**子代理自己的 id**（不是被改写成父的）——
    # 取证要的是「谁产生的」，父归属只用于选库。
    assert rows[0]["agent_id"] == SUB_ID


async def test_unregistered_subagent_event_is_dropped_and_loud(
    task_env, clean_router
):
    """B：未登记 + `project_id=""` ⇒ **写口零调用** + 有声 `write_failed`。

    这一格就是**修前**的真实形态（阳性对照的对照面）：证明 A 的绿不是
    「随便怎么都能过」—— 少了登记这一级，事件确实落不了库。
    """
    write = AsyncMock()
    with patch.object(project_db, "execute_by_project", write):
        with structlog.testing.capture_logs() as logs:
            await event_audit._write(SUB_ID, "", "llm_retry", {"k": "v"})

    assert write.await_count == 0, "解析不出项目时**不许**乱写（不许猜库）"
    assert [e.get("event") for e in logs].count("event_audit.write_failed") == 1, (
        "两条判据都空必须**有声**失败，不得静默丢"
    )
    assert await _events_for(SUB_ID) == []


async def test_explicit_project_id_is_last_resort(task_env, clean_router):
    """C：id 不可解析（正式路由 + 瞬态表都没有）+ 显式 `project_id` ⇒ 落库。"""
    await event_audit._write("nobody-knows-me", PROJECT_ID, "worktree_rebuild", {})

    rows = await _events_for("nobody-knows-me")
    assert len(rows) == 1
    assert rows[0]["event_type"] == "worktree_rebuild"


async def test_route_wins_over_explicit_project_id(task_env, clean_router):
    """C′：两个判据都在时**以路由为准**（次序是契约，不是实现细节）。

    若次序反了，调用方传错/过期的 `project_id` 会把事件写进**别人的库** ——
    那是比丢弃更难查的故障（丢了看得见，写错看不见）。
    """
    agent_router.register_transient(SUB_ID, PROJECT_ID)

    await event_audit._write(SUB_ID, "some-other-project", "llm_retry", {})

    rows = await _events_for(SUB_ID)
    assert len(rows) == 1
    assert rows[0]["event_type"] == "llm_retry"


async def test_resolution_crash_does_not_escape_the_task(clean_router):
    """解析本身抛异常 ⇒ **不得**逃逸成「Task exception was never retrieved」。

    `_write` 是 `asyncio.create_task` 的**任务体**：逃逸的异常只会变成一句
    没人看的告警（甚至没有堆栈）。这一格是那条兜底的阳性对照。
    """
    import hiveweave.services.event_audit as ea_mod

    def _boom(*_a, **_k):
        raise RuntimeError("router exploded")

    write = AsyncMock()
    with patch.object(ea_mod, "_resolve_project_id", _boom):
        with patch.object(project_db, "execute_by_project", write):
            with structlog.testing.capture_logs() as logs:
                await event_audit._write(SUB_ID, "", "llm_retry", {})  # 不许抛

    names = [e.get("event") for e in logs]
    assert "event_audit.route_resolution_failed" in names, "解析异常必须被记名"
    assert "event_audit.write_failed" in names, "最终仍要落到有声失败"
    assert write.await_count == 0


async def test_log_resolves_before_the_transient_is_unregistered(
    task_env, clean_router
):
    """⭐ P1-a 判据：登记表的**存活窗口**必须覆盖源选定那一刻。

    生产者的瞬态身份是短命的：`tools/subagent.py::_work` 在子代理返回后
    **立刻** `unregister_transient`（同步，不让出事件循环）。若源选定被推到
    `create_task` 的任务体里做，这里的 `log()` 与 `unregister()` 之间就没有
    任何 await ⇒ 任务启动时表已摘 ⇒ **症状原地复发**。

    本用例把那个窗口**显式**压到零（`log()` 之后立刻 `unregister()`，中间
    不 await 任何东西），事件仍必须落库。
    """
    agent_router.register_transient(SUB_ID, PROJECT_ID)

    with _capture_tasks() as created:
        await event_audit.log(SUB_ID, "", "llm_retry", {"k": "v"})
        # 同步段：中间**不让出**事件循环，直接摘表（生产者的真实时序）
        agent_router.unregister_transient(SUB_ID)
    await asyncio.gather(*created)

    rows = await _events_for(SUB_ID)
    assert len(rows) == 1, (
        "源选定必须发生在 log() 的同步段；否则摘表后任务才启动 ⇒ 事件丢失"
    )


async def test_log_forwards_project_id_to_write(clean_router):
    """`log()` 过去**把 project_id 丢在地上**（docstring 自认）—— 必须真转发。

    确定性：捕获 `create_task` 产物后 `gather`，不靠 `sleep` 轮询。
    """
    captured: list[tuple] = []

    async def _rec(agent_id, project_id, event_type, payload, **kw):
        captured.append((agent_id, project_id, event_type, kw.get("resolved_pid")))

    with patch.object(event_audit, "_write", _rec):
        with _capture_tasks() as created:
            await event_audit.log("a-1", "p-1", "crash", {"reason": "x"})
        await asyncio.gather(*created)

    assert len(captured) == 1, f"必须恰好投递一次：{captured!r}"
    agent_id, project_id, event_type, resolved = captured[0]
    assert (agent_id, project_id, event_type) == ("a-1", "p-1", "crash")
    assert resolved == "p-1", "project_id 必须在**同步段**解析出来并随任务带下去"


# ── D 零污染：瞬态身份与正式路由物理隔离 ──────────────────────


async def test_transient_identity_does_not_leak_into_org_face(clean_router):
    """D：瞬态身份**不得**出现在组织面（名册/路由列表），也不改正式路由语义。

    这是"塞进 `_routes` 也能跑绿测试、但会把每个子代理变成幽灵成员"的
    那类改法的**证伪判据**。
    """
    agent_router.register_transient(SUB_ID, PROJECT_ID)

    assert agent_router.get_project_id(SUB_ID) is None, (
        "正式路由语义必须**一字不动**（爆炸半径 = 0）"
    )
    assert SUB_ID not in [r.agent_id for r in agent_router.list_active_routes()]
    assert SUB_ID not in agent_router.get_project_agent_ids(PROJECT_ID)
    # 反向：瞬态解析确实能拿到（否则上面三条靠"根本没用"也能过 = 空守卫）
    assert agent_router.resolve_transient_project_id(SUB_ID) == PROJECT_ID


async def test_unregister_transient_is_the_normal_exit(clean_router):
    """登记/注销成对：子代理出界后表里不留痕（异常路径由 E 的 FIFO 兜底）。"""
    agent_router.register_transient(SUB_ID, PROJECT_ID)
    assert agent_router.transient_count() == 1

    agent_router.unregister_transient(SUB_ID)

    assert agent_router.transient_count() == 0
    assert agent_router.resolve_transient_project_id(SUB_ID) is None


# ── F 生产者侧：登记必须发生在 spawn 边界（否则本文件其余用例全是空守卫）──
# ⚠ 审计 P1-b 实测：删掉 `tools/subagent.py` 里的 `register_transient` 一行，
#   上面 12 条**全绿**（它们直接调 `register_transient`，绕过了生产者）。
#   ⇒ 必须有这一格，否则"修好了"只证明了登记表本身能工作。


class _FakeParent:
    """`spawn_subagent_tool` 需要的最小父对象。"""

    def __init__(self, pid: str) -> None:
        self.id = "parent-1"
        self.project_id = pid

    async def _get_workspace_path(self) -> str:
        return ""


async def test_spawn_registers_transient_identity_end_to_end(
    task_env, clean_offturn, clean_router, monkeypatch
):
    """F：走**真** spawn 边界 ⇒ 子代理运行期的事件真落库，出界后身份摘干净。

    这一格同时覆盖三件事：① 登记确实发生在生产者那一步；② 子代理**运行期**
    身份可解析；③ 出界后表里不留痕（正常出口）。
    """
    from hiveweave.services.offturn import is_live_job
    from hiveweave.tools.subagent import SpawnSubagentParams, spawn_subagent_tool

    parent = _FakeParent(PROJECT_ID)
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent",
        lambda _aid: parent,
    )

    seen: dict = {}

    async def fake_run(_parent, _prompt, _desc, _timeout, _type, **kw):
        sid = kw["sub_id"]
        seen["sub_id"] = sid
        # 子代理运行期：身份必须已登记，且事件必须真落库
        seen["routed_while_running"] = _resolve_project_id(sid, "")
        await event_audit.log(sid, "", "llm_retry", {"k": "v"})
        return {"status": "ok", "content": "scout"}

    monkeypatch.setattr("hiveweave.tools.subagent._run_subagent", fake_run)

    result = await spawn_subagent_tool(
        SpawnSubagentParams(subagent_type="readonly", prompt="scout files"),
        "parent-1",
        "/tmp/ws",
    )
    assert result.success, f"前置：spawn 必须成功：{result.output!r}"

    job_id = result.extra["job_id"]
    for _ in range(200):
        if not is_live_job(job_id):
            break
        await asyncio.sleep(0.01)

    sid = seen["sub_id"]
    assert sid.startswith("sub-"), f"子代理 id 形态变了？{sid!r}"
    assert seen["routed_while_running"] == PROJECT_ID, (
        "子代理运行期必须能解析出父项目 —— 这正是生产者侧登记要保证的"
    )
    rows = await _events_for(sid)
    assert len(rows) == 1, (
        "子代理事件必须真落库（查表，不看测试绿）；"
        "注意：此时 `_work` 的 finally 早已 unregister 过"
    )
    assert agent_router.transient_count() == 0, "出界即摘（正常出口不留痕）"


async def test_timeline_reads_what_write_wrote(task_env, clean_router):
    """P2-c：读侧必须与写侧**同源** —— 写进去的必须读得出来（子代理身份）。

    写入侧修好而 `timeline()` 仍按 `agent_db.query(agent_id)` 路由，等于
    取证数据"存进去了但没人看得见"（`api/debug.py` / `api/logs.py` 都走它）。
    """
    agent_router.register_transient(SUB_ID, PROJECT_ID)
    await event_audit._write(SUB_ID, "", "llm_error.upstream", {"k": "v"})

    events = await event_audit.timeline(SUB_ID, hours=1)

    assert [e["event_type"] for e in events] == ["llm_error.upstream"]
    assert events[0]["payload"] == {"k": "v"}


# ── E 有界：异常路径不漏内存 ─────────────────────────────────


async def test_transient_registry_is_bounded(clean_router):
    """E：登记远超上限 ⇒ 表长被夹在上限内，且**最旧的先出**。"""
    for i in range(_TRANSIENT_MAX + 20):
        agent_router.register_transient(f"sub-x-{i}", PROJECT_ID)

    assert agent_router.transient_count() == _TRANSIENT_MAX
    assert agent_router.resolve_transient_project_id("sub-x-0") is None  # 最旧被逐
    assert agent_router.resolve_transient_project_id(
        f"sub-x-{_TRANSIENT_MAX + 19}"
    ) == PROJECT_ID  # 最新还在


async def test_empty_values_are_never_registered(clean_router):
    """空值不入表 —— 否则「查得到但查出来是空串」会把调用方判空逻辑变哑弹。"""
    agent_router.register_transient("", PROJECT_ID)
    agent_router.register_transient(SUB_ID, "")
    agent_router.register_transient("  ", "  ")

    assert agent_router.transient_count() == 0


async def test_clear_project_drops_transient_identities(clean_router):
    """项目删除时瞬态身份同批摘掉（不再往已删项目写事件）。"""
    agent_router.register_transient(SUB_ID, PROJECT_ID)
    agent_router.register_transient("sub-other-1", "p-other")

    agent_router.clear_project(PROJECT_ID)

    assert agent_router.resolve_transient_project_id(SUB_ID) is None
    # 反向：别的项目的瞬态身份**不许**被误伤（判据的作用域必须精确匹配）
    assert agent_router.resolve_transient_project_id("sub-other-1") == "p-other"


# ── 报错文案：负向断言必须可回源（不许指向不存在的表）────────────


async def test_error_message_does_not_point_at_a_nonexistent_table(clean_router):
    """旧文案说 "(agent not registered in Meta DB)" —— **Meta DB 没有 agents 表**。

    判据落**实现的真源**：文案里出现的路由源必须是 `AgentRouter`，且**不得**
    再声称 Meta DB 里有 agents 表（照旧文案去查库会查到不存在的表上）。
    """
    with pytest.raises(project_db.ProjectDbError) as ei:
        await project_db.get_project_db_for_agent("definitely-not-registered")

    msg = str(ei.value)
    assert "AgentRouter" in msg
    assert "agent not registered in Meta DB" not in msg
