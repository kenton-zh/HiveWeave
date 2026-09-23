r"""P1-5（用户选 **E**）：跨轮的上游重试预算**可重建**，不再"每开一轮就满血"。

## 病灶（实读，2026-09-22 对表 HEAD）

主循环的上游重试预算是 `agents/agent.py` 的 `self._main_upstream_attempt`：
- `:1421`（旧）**无条件** `= 0` —— 每开一轮归零；
- `:1589` 判 `… < _MAIN_LOOP_STREAM_RETRIES`（默认 2）；`:1595` 自增；`:1598` 退避；
- 而它是**挂在 live agent 对象上的内存属性**（全仓 grep 只在本文件出现）。

⇒ "每轮 ≤2 次"的真相是"**每一轮** ≤2 次"，而轮可以无限开（进程重启 / 被重新唤醒 /
中断恢复都会开新轮）⇒ **同一个活的累计重试没有上界**（真实刹车是时间与预算，不是重试次数）。

## 选 **E** 的含义

**只改清零时机**：本轮**承接了被中断的 run**（`interrupted_run_id` 非空）⇒ 从那一行
`agent_runs.upstream_retry_attempt` **续算**；否则（真正的**新工作**）才归零。
不做血缘链（A/B/C），也不把预算改成速率限制（D）。

## 本文件守什么（全状态判据：DB 行 / PRAGMA / 盘上值）

- **A 迁移形态**：`upstream_retry_attempt` **无 DEFAULT**（NULL = 老行未记录，与 0 不同形）。
- **B 写读同一**：写口落库、读口读回**逐字段相等**（不是"调用了就算"）。
- **C 续跑承接**：`interrupted_run_id` 非空 ⇒ 起点 = 库里值（**跨轮累计**）。
  ⚠ 这一格就是本条的**主判据**：旧形态起点**恒为 0** ⇒ 该断言必红。
- **D 新工作拿满**：`interrupted_run_id` 为 None ⇒ 起点 0（**不误伤新工作**）。

⚠ 阳性对照（改坏必须转红）：把 `_resume_upstream_attempt` 改回 `return 0`（= 旧行为）
⇒ **C** 红而 D 仍绿（证明两格各自有牙齿，不是一起动）。
"""

from __future__ import annotations

import time

import pytest

from hiveweave.agents.agent import Agent
from hiveweave.db import project as project_db
from hiveweave.services.run_ledger import run_ledger

# 复用既有夹具：`task_env` 建项目库 + 打桩 workspace；`ledger_env` 额外把
# agent→workspace 塞进 `project_db` 缓存（`run_ledger` 走
# `get_project_db_for_agent` ⇒ 不塞会拿「agent not registered in Meta DB」）。
# ⚠ 两个都要 import 进本模块命名空间，只 import 后者会报 fixture not found。
from tests.test_git_hardening_consumer import ledger_env  # noqa: F401
from tests.test_idle_architecture_p0 import EXEC, PROJECT_ID, task_env  # noqa: F401


@pytest.fixture
async def p15_env(ledger_env):  # noqa: F811
    """`ledger_env` + **把连接真正建出来**。

    ⚠ `get_project_db_for_agent` 的缓存快路径要求 **`ws in _cache`**（不只是
    `_agent_cache[agent]`）；只看 agent 缓存会**落到 Meta DB 查询**并抛
    「No project found for agent_id=…」。`ensure_project_db` 正是把连接放进 `_cache`
    的那一步。
    """
    from hiveweave.db.project import ensure_project_db

    await ensure_project_db(ledger_env["workspace"])
    yield ledger_env


class _AgentStub:
    """只带 `_resume_upstream_attempt` 所需属性的最小替身。

    ⚠ 目的是**测真方法体**（`Agent._resume_upstream_attempt` 的原函数），不是复刻它 ——
    构造一个完整 Agent 要拉起一大片依赖，而该方法的输入只有 `self.id` 与
    `self._run_ledger`。
    """

    def __init__(self) -> None:
        self.id = EXEC
        self._run_ledger = run_ledger


async def _resume(interrupted_run_id: str | None) -> int:
    # ⚠ 必须是 async：测试跑在事件循环里，`asyncio.run()` 会直接抛
    # "cannot be called from a running event loop"。
    return await Agent._resume_upstream_attempt(_AgentStub(), interrupted_run_id)


async def _insert_run(run_id: str, status: str, attempt: int | None) -> None:
    """直插一行 `agent_runs`（`attempt=None` ⇒ 保留 NULL，不写 0）。"""
    now = int(time.time() * 1000)
    if attempt is None:
        await project_db.execute(
            EXEC,
            "INSERT INTO agent_runs (id, agent_id, status, started_at) "
            "VALUES (?, ?, ?, ?)",
            [run_id, EXEC, status, now],
        )
    else:
        await project_db.execute(
            EXEC,
            "INSERT INTO agent_runs "
            "(id, agent_id, status, started_at, upstream_retry_attempt) "
            "VALUES (?, ?, ?, ?, ?)",
            [run_id, EXEC, status, now, attempt],
        )


# ── A 迁移形态：无 DEFAULT ────────────────────────────────


async def test_column_has_no_default(p15_env) -> None:
    """A：`upstream_retry_attempt` **无 DEFAULT** ⇒ 老行是 NULL（不是 0）。

    「未知=NULL」与「明确 0 次」必须**不同形** —— 带 DEFAULT 会让升级前的存量行
    被回填成 0（= 谎称"从没重试过"），方向恰好是**放宽预算**（同 P0-3 的教训）。
    """
    rows = await project_db.query(EXEC, "PRAGMA table_info(agent_runs)")
    cols = {r["name"]: r for r in rows}
    assert "upstream_retry_attempt" in cols, "列必须已被迁移出来"
    assert cols["upstream_retry_attempt"]["dflt_value"] is None, (
        "该列**不得**带 DEFAULT（否则老行会被回填成 0，把'未知'说成'0 次'）"
    )


# ── B 写口 → 读口 ────────────────────────────────────────


async def test_write_then_read_round_trips(p15_env) -> None:
    """B：写口落库、读口读回**同一个值**（不是"调用了就算"）。"""
    await _insert_run("run-b", "running", None)
    assert await run_ledger.get_upstream_attempt(EXEC, "run-b") == 0, (
        "NULL 读作 0（保守：拿满额度），但**列本身仍是 NULL**"
    )
    await run_ledger.set_upstream_attempt(EXEC, "run-b", 2)
    assert await run_ledger.get_upstream_attempt(EXEC, "run-b") == 2
    # 盘上真值（不经读口）
    rows = await project_db.query(
        EXEC, "SELECT upstream_retry_attempt AS v FROM agent_runs WHERE id = ?",
        ["run-b"],
    )
    assert int(rows[0]["v"]) == 2


# ── C / D 清零时机（本条的核心）────────────────────────────


async def test_resume_inherits_previous_run_budget(p15_env) -> None:
    """⭐ C（主判据）：续跑**承接**被中断 run 的累计 ⇒ 预算不重新满血。

    构造：上一 run 被中断、且已用掉全部预算（attempt == 上限）。
    ⇒ 本轮的起点必须是**上限**，于是 `起点 < 上限` **不成立** ⇒ 该 turn **不会**再重试。
    **旧形态起点恒为 0** ⇒ 这条必红（`assert 起点 >= 上限` 失败）。
    """
    from hiveweave.agents.agent import _MAIN_LOOP_STREAM_RETRIES

    await _insert_run(
        "run-interrupted", "interrupted", _MAIN_LOOP_STREAM_RETRIES
    )
    resumed = await _resume("run-interrupted")

    assert resumed == _MAIN_LOOP_STREAM_RETRIES, (
        f"必须承接上一 run 的计数，实得 {resumed}（旧形态恒为 0）"
    )
    assert not (resumed < _MAIN_LOOP_STREAM_RETRIES), (
        "预算已被上一轮用尽 ⇒ 本轮必须**不再重试**（同一逻辑工作的累计有上界）"
    )


async def test_new_work_still_gets_full_budget(p15_env) -> None:
    """⭐ D（反向，防误伤）：**新工作**必须拿满预算（起点 0）。

    与 C 是一对：C 管"续跑不许满血"，D 管"新活不许被上一轮的历史扣额度"。
    两者的失败方向相反，故必须各有守卫。
    """
    from hiveweave.agents.agent import _MAIN_LOOP_STREAM_RETRIES

    # 库里**存在**一条用尽预算的中断 run —— 但只要本次不是承接它，就不该被它影响。
    await _insert_run(
        "run-interrupted-old", "interrupted", _MAIN_LOOP_STREAM_RETRIES
    )

    assert await _resume(None) == 0, "没有承接对象（新工作）⇒ 起点必须是 0"
    assert 0 < _MAIN_LOOP_STREAM_RETRIES, "新工作必须拿满预算（能重试）"


async def test_resume_of_unknown_run_fails_open_to_zero(p15_env) -> None:
    """E：承接对象读不到（行不存在/列缺失）⇒ 起点 0（**保守**）。

    方向说明：读不到就 0 = 回到改动前的行为；反过来（读不到就当"预算已尽"）
    会把正常工作卡死，那是**新故障面**。
    """
    assert await _resume("no-such-run-id") == 0


async def test_stale_interrupted_run_is_not_carried_over(p15_env) -> None:
    """⭐⭐ **P0 防线**：**陈旧**的中断 run **不得**被当成"正在续跑"。

    机制（自查实测）：`status='interrupted'` 全仓**无人清除**（只在
    `run_ledger.py` 两处置位），而 `find_interrupted_run` 只按 `ended_at DESC LIMIT 1`
    取"最新一条 interrupted" ⇒ **一条陈旧的中断 run 会一直当选**。若承接只看
    "`interrupted_run_id` 非空"，则一次**成功**续跑之后，**之后每一轮**都会继续
    承接那条陈旧计数 ⇒ 计数达上限后**该 agent 永久不再重试** —— 把"重试无上界"
    换成"重试被永久关闭"。

    构造：中断 run（计数已满）**之后又有一行更新的 run** ⇒ 不许承接。
    反向对照：见 `test_resume_inherits_previous_run_budget`（那条 run 就是最新一行
    ⇒ 必须承接）。两格合起来才钉住"承接 = 正在续跑，而不是'存在一条中断 run'"。
    """
    from hiveweave.agents.agent import _MAIN_LOOP_STREAM_RETRIES

    # 陈旧的中断 run（计数已满——若被承接就会永久锁死重试）
    await _insert_run("run-stale-interrupted", "interrupted", _MAIN_LOOP_STREAM_RETRIES)
    # 其后又跑过一轮（无论结局如何：它才是"最后一个 run"）
    await project_db.execute(
        EXEC,
        "UPDATE agent_runs SET started_at = started_at - 100000, "
        "ended_at = started_at WHERE id = ?",
        ["run-stale-interrupted"],
    )
    await _insert_run("run-after-it", "completed", 0)

    got = await _resume("run-stale-interrupted")
    assert got == 0, (
        f"陈旧的中断 run 不得被承接（实得 {got}）—— 否则计数满后会永久不再重试"
    )


# ── F / G / H ⭐ 生产者侧守卫：归零点必须**真的**走 helper，且**在同一作用域内** ──
# ⚠ 为什么必须单列：上面 A–E **全部直接调** `Agent._resume_upstream_attempt`
#   ⇒ 只证"这个 helper 行为对"，**证不了"归零点用了它"**。把归零点改回无条件
#   `= 0`，A–E **一条都不会红**（与 D67-1 的 P1-b 同款空守卫：只测机制、不测接线）。
# 判据用 **AST**（用户 09-14 钦定：测试守卫禁用文本子串）。
#
# ⚠⚠ 2026-09-23 线上实锤 —— **本条守卫自己的失效史，必须留痕**：
#   原 G 只比**全文件行号序**。P1-5 落地时把求值写进了 `chat()`（它在 `_run_llm`
#   **上方**、行号更小）⇒ 文件序成立、**G 全绿**，可 `_run_llm` 根本看不见那个
#   局部变量 ⇒ **每个 turn 在首请求前 `NameError`，整个平台停摆**（用户截图 +
#   dist 日志 `agent.py:1441 in _run_llm`）。那一次 A–H 共 8 条全绿。
#   ⇒ 教训一：**跨函数的行号序不是时序**。G 现在同时要求"同一函数作用域"。
#   ⇒ 教训二：唯一**执行** `_run_llm` 的 test_main_loop_retry.py 当时不在末次
#      回归范围内 ⇒ 新增 H 把那一格职责钉进本条。
#   ⚠ 可复用判据：**判"两条线接上了"时，先问"这两条线在不在同一个作用域"**；
#      跨作用域的"先后关系"是文本判据，能骗过守卫（本条即标本）。


def _agent_ast():
    import ast
    import pathlib

    import hiveweave

    src = (pathlib.Path(hiveweave.__file__).resolve().parent
           / "agents" / "agent.py")
    return ast.parse(src.read_text(encoding="utf-8"))


def _enclosing_function(tree, target):
    """包住 `target` 的**最内层**函数节点（嵌套时取行跨度最小的那个）。"""
    import ast

    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(child is target for child in ast.walk(node)):
                found.append(node)
    if not found:
        return None
    return min(found, key=lambda n: n.end_lineno - n.lineno)


def _calls_named(tree, attr: str):
    """返回属主属性名等于 `attr` 的 **Call 节点**列表。"""
    import ast

    return [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == attr
    ]


def test_reset_point_really_calls_the_resume_helper() -> None:
    """F：`_main_upstream_attempt` 的**赋值点**必须是对 helper 的 await 调用。

    ⚠ 这里**不得**再给 `ast.Name`（局部变量）开后门：那个分支正是 2026-09-23
    那次事故的逃生口 —— 右值写成 `restored_upstream_attempt` 也照样绿，而那个名字
    在另一个函数里。形态锁死为**内联 await**（右值不许是字面量、也不许是名字）。
    """
    import ast

    tree = _agent_ast()

    assigns = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(
            isinstance(t, ast.Attribute) and t.attr == "_main_upstream_attempt"
            for t in n.targets
        )
    ]
    assert assigns, (
        "找不到 `_main_upstream_attempt` 的赋值点 —— 属性被改名了？"
        "（守卫的作废条件必须显式暴露，不能静默恒绿）"
    )

    literals = [ast.dump(n.value)[:80] for n in assigns
                if isinstance(n.value, ast.Constant)]
    assert not literals, f"归零点被写死了（归零时机回归）：{literals}"

    bare_names = [n.value.id for n in assigns if isinstance(n.value, ast.Name)]
    assert not bare_names, (
        f"归零点的右值是不经内联的裸名字 {bare_names} —— 这正是 2026-09-23 的病灶"
        f"（`restored_upstream_attempt` 写在 `chat()` 里，`_run_llm` 看不见 ⇒ 每轮"
        f"NameError）。必须内联 `await self._resume_upstream_attempt(...)`"
    )

    def _is_resume_call(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Await)
            and isinstance(node.value, ast.Call)
            and getattr(node.value.func, "attr", "") == "_resume_upstream_attempt"
        )

    assert all(_is_resume_call(n.value) for n in assigns), (
        "归零点的右值不是 `await self._resume_upstream_attempt(...)` —— 形态变了，"
        "本条守卫需重新表述（不许静默放过）"
    )

    # `+= 1` 是 AugAssign、不在上面集合里；显式确认它**仍然存在**（自增点没被删）
    assert any(
        isinstance(n, ast.AugAssign)
        and isinstance(n.target, ast.Attribute)
        and n.target.attr == "_main_upstream_attempt"
        for n in ast.walk(tree)
    ), "自增点消失了 —— 计数器不再累加，本条的主判据失去前提"


def test_carry_over_is_evaluated_before_create_run() -> None:
    """⭐⭐ **时序守卫**（P0 防线）：**给归零点供值的那次** `_resume_upstream_attempt`
    调用，必须在 `create_run` **之前**，且**在同一个函数作用域内**。

    两条约束都是踩过坑才加上的，缺一不可：

    * **作用域**（2026-09-23 线上事故）：求值被写进 `chat()`（行号更小、文件序成立）
      而消费在 `_run_llm` ⇒ 跨函数局部量不可见 ⇒ **每个 turn 首请求前 `NameError`**，
      平台停摆，而当时 8 条守卫全绿。⇒ **跨函数的行号序不是时序**。
    * **绑到赋值的右值**（2026-09-23 审计实测 M5）：原先比的是"**存在**一次
      `_resume_upstream_attempt` 调用排在 `create_run` 之前"。审计构造出一个
      **不被使用的诱饵调用**放在 `create_run` 前、真赋值挪到其后 ⇒ F/G/H **三条全绿**，
      而运行期承接恒为 0（静默退回旧的"每轮归零"）。
      ⇒ **存在性判据必须绑到承重对象上**；"某处有个同名调用"证明不了任何事。

    为什么非要问时序：`_resume_upstream_attempt` 内部的 `is_latest_run` 问的是
    "那条中断 run 是不是本 agent 最后一个 run"。**若在 `create_run` 之后求值**，
    "最后一个 run"永远是**本轮刚建的那一条** ⇒ 恒 False ⇒ **承接永不生效**，
    而且**行为类测试全绿** —— 它们构造的"库里只有那条中断 run"这一状态在生产里
    **不可达**（生产那一刻必有本轮的新 run）。
    """
    import ast

    tree = _agent_ast()

    assigns = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(
            isinstance(t, ast.Attribute) and t.attr == "_main_upstream_attempt"
            for t in n.targets
        )
    ]
    assert assigns, "找不到 `_main_upstream_attempt` 的赋值点 —— 本条守卫失去前提"

    # ⭐ 关键：供值者 = **赋值右值里**的那次调用，不是"全文件任意同名调用"。
    producers = []
    for a in assigns:
        inner = a.value.value if isinstance(a.value, ast.Await) else a.value
        if (isinstance(inner, ast.Call)
                and getattr(inner.func, "attr", "") == "_resume_upstream_attempt"):
            producers.append((a, inner))
    assert producers, (
        "没有任何一个 `_main_upstream_attempt` 赋值点的右值来自 "
        "`_resume_upstream_attempt` —— 归零点与 helper 脱钩了（F 会先报形态，"
        "这里报绑定）"
    )

    create_calls = _calls_named(tree, "create_run")
    assert create_calls, "找不到 `create_run` 的调用点 —— 本条守卫失去前提"

    ok = []
    for a, inner in producers:
        af = _enclosing_function(tree, a)
        for c in create_calls:
            cf = _enclosing_function(tree, c)
            if af is not None and af is cf and inner.lineno < c.lineno:
                ok.append((inner, c, af))

    if not ok:
        detail = []
        for a, inner in producers:
            af = _enclosing_function(tree, a)
            for c in create_calls:
                cf = _enclosing_function(tree, c)
                detail.append(
                    f"供值调用@{inner.lineno} 在 "
                    f"{af.name if af else '<module>'}() 内；"
                    f"create_run@{c.lineno} 在 {cf.name if cf else '<module>'}() 内"
                )
        raise AssertionError(
            "给归零点供值的 `_resume_upstream_attempt` 调用**没有**满足"
            "「同一函数作用域 且 排在 create_run 之前」—— 承接会静默失效"
            "（要么跨作用域 ⇒ NameError，要么位置在后 ⇒ is_latest_run 恒 False）。"
            "实况：" + " | ".join(detail)
        )


async def test_reset_point_actually_receives_carried_budget() -> None:
    """⭐⭐ H（接线**实测**）：真的执行 `_run_llm`，看承接值是否落到计数器上。

    这一格是 F/G 都缺的：A–E 直调 helper（不碰接线）、F/G 只读 AST（不看运行）。
    只有**真的执行** `_run_llm` 才能区分"名字接上了"与"名字接上了但**值**没过来"。
    2026-09-23 的 NameError 就是被同型用例（test_main_loop_retry）抓到的。

    ⚠⚠ 为什么 `is_latest_run` 要打**位置敏感**的桩（2026-09-23 审计 M5 之后补）：
    最初这里把 `is_latest_run` 打桩成**恒 True** ⇒ 它对"求值点在 `create_run`
    之后"这种错误**完全免疫**（那个形态下 F/G 也绿 ⇒ 三条一起被诱饵骗过）。
    现在用**行为**复刻生产语义：`create_run` 落库之后，"那条中断 run 是不是最后
    一个"必然为 False ⇒ 位置错**当即可观测**，不再只能靠 AST 猜。

    双向断言：承接 id ⇒ 落库值；新工作（None）⇒ 0。只断一边的话，"恒定读某处"
    这类错误接线仍能蒙混过关。
    """
    from unittest.mock import AsyncMock, patch

    from tests.test_main_loop_retry import _FakeStreamerFactory, _OK, _prepared_agent

    order: list[str] = []

    def _is_latest(*_a, **_k):
        order.append("is_latest_run")
        # 生产语义：本轮 `create_run` 一旦落库，"那条中断 run 是最后一个"即不成立。
        return "create_run" not in order

    def _create_run(*_a, **_k):
        order.append("create_run")
        return "run-new"

    # ── 方向一：承接 ⇒ 起点 = 库里那一行的累计 ──
    agent = _prepared_agent()
    # ⚠ `_prepared_agent`（轻量夹具）不含 `_current_activation_id`；而 `create_run`
    # 的**取参表达式**会先读它 ⇒ 不设则 `create_run` 根本没被调到，
    # `order` 里就永远没有 `create_run`（H 的前提静默消失）。生产里 __init__ 会设。
    agent._current_activation_id = None
    agent._run_ledger.is_latest_run = AsyncMock(side_effect=_is_latest)
    agent._run_ledger.create_run = AsyncMock(side_effect=_create_run)
    agent._run_ledger.get_upstream_attempt = AsyncMock(return_value=7)
    with patch("hiveweave.agents.agent.Streamer",
               _FakeStreamerFactory([dict(_OK)])):
        await agent._run_llm("hi", {}, interrupted_run_id="run-interrupted")

    assert "create_run" in order, "夹具没跑到 create_run —— H 的前提没了"
    assert order.index("is_latest_run") < order.index("create_run"), (
        f"承接判定被排在 `create_run` 之后：{order} —— is_latest_run 必为 False，"
        f"承接静默失效（AST 守卫可能被诱饵调用骗过，这条是行为兜底）"
    )
    assert agent._main_upstream_attempt == 7, (
        f"承接值必须真的落到 `_main_upstream_attempt`（实得 "
        f"{getattr(agent, '_main_upstream_attempt', '<未赋值>')}；作用域断了会 "
        f"NameError、位置错了这里会是 0）"
    )

    # ── 方向二：新工作 ⇒ 起点必须是 0（不许被上面的 7 泄漏进来）──
    order.clear()
    fresh = _prepared_agent()
    fresh._current_activation_id = None
    fresh._run_ledger.is_latest_run = AsyncMock(side_effect=_is_latest)
    fresh._run_ledger.create_run = AsyncMock(side_effect=_create_run)
    fresh._run_ledger.get_upstream_attempt = AsyncMock(return_value=7)
    with patch("hiveweave.agents.agent.Streamer",
               _FakeStreamerFactory([dict(_OK)])):
        await fresh._run_llm("hi", {}, interrupted_run_id=None)

    assert fresh._main_upstream_attempt == 0, (
        f"新工作不得承接 —— 起点必须是 0（实得 {fresh._main_upstream_attempt}）"
    )


async def test_carried_budget_actually_brakes_the_retry() -> None:
    """⭐ I（承接到**刹车**）：承接来的值必须真的被**重试判据**消费。

    第二轮审计实测：把刹车判据（`agent.py` 那段
    `… and self._main_upstream_attempt < _MAIN_LOOP_STREAM_RETRIES`）换成**另一个
    新计数器**（从 0 起）⇒ F/G/H **三条全绿** —— 因为三条都只问"值有没有落到那个
    属性"，没有一条问"那个属性有没有被用来刹车"。承接算得再准，若不约束重试次数，
    P1-5 等于没做。

    构造：承接值 = 上限（库里的累计已用尽）⇒ 判据 `值 < 上限` 不成立 ⇒
    **上游类错误一次都不许重试**。刹车若读别的计数器（起点 0）就会重试一次
    ⇒ `factory.calls == 2` ⇒ 本格转红。
    """
    from unittest.mock import AsyncMock, patch

    from hiveweave.agents.agent import _MAIN_LOOP_STREAM_RETRIES
    from tests.test_main_loop_retry import (
        _ERROR_IDLE,
        _FakeStreamerFactory,
        _OK,
        _prepared_agent,
    )

    agent = _prepared_agent()
    agent._current_activation_id = None
    agent._run_ledger.is_latest_run = AsyncMock(return_value=True)
    agent._run_ledger.get_upstream_attempt = AsyncMock(
        return_value=_MAIN_LOOP_STREAM_RETRIES
    )
    agent._run_ledger.check_budget = AsyncMock(return_value=(False, ""))

    factory = _FakeStreamerFactory([dict(_ERROR_IDLE), dict(_OK)])
    with patch("hiveweave.agents.agent.Streamer", factory), patch(
        "hiveweave.llm.retry.compute_backoff",
        lambda attempt, retry_after_ms=None: 0,
    ):
        await agent._run_llm("hi", {}, interrupted_run_id="run-interrupted")

    assert factory.calls == 1, (
        f"预算已被上一轮用尽 ⇒ 本轮**不许**再重试（实得 {factory.calls} 次）—— "
        f"若刹车读的不是 `_main_upstream_attempt`，承接就只是个没人看的数字"
    )


async def test_increment_is_persisted_to_the_run_row() -> None:
    """⭐ J（自增落库）：重试时 `+= 1` 必须**写回本 run 那行**。

    第二轮审计实测：删掉那次 `set_upstream_attempt(...)` 调用 ⇒ F/G/H 加宽集
    **260 条全绿**（`test_main_loop_retry.py` 的 `_run_ledger` 是 `AsyncMock`，
    调用被执行但**无任何断言**）⇒ 列恒 NULL、承接恒 0、P1-5 静默退回旧行为。
    这属于"接了线没人看"，与本次事故同族：**值算对了，但没人验证它落盘**。

    构造：新工作（起点 0）＋ 一次上游类错误 ⇒ 必须重试，且写口被以
    `(agent_id, 本 run 的 id, 1)` 调用过。`create_run` 返回的 id 由本用例钉住，
    这样"写到了**别的** run 行上"也会被这条抓住。
    """
    from unittest.mock import AsyncMock, patch

    from tests.test_main_loop_retry import (
        _ERROR_IDLE,
        _FakeStreamerFactory,
        _OK,
        _prepared_agent,
    )

    agent = _prepared_agent()
    agent._current_activation_id = None
    agent._run_ledger.is_latest_run = AsyncMock(return_value=True)
    agent._run_ledger.get_upstream_attempt = AsyncMock(return_value=0)
    agent._run_ledger.check_budget = AsyncMock(return_value=(False, ""))
    agent._run_ledger.create_run = AsyncMock(return_value="run-this-turn")

    factory = _FakeStreamerFactory([dict(_ERROR_IDLE), dict(_OK)])
    with patch("hiveweave.agents.agent.Streamer", factory), patch(
        "hiveweave.llm.retry.compute_backoff",
        lambda attempt, retry_after_ms=None: 0,
    ):
        await agent._run_llm("hi", {}, interrupted_run_id=None)

    assert factory.calls == 2, "新工作拿满预算 ⇒ 上游类错误必须重试一次"
    agent._run_ledger.set_upstream_attempt.assert_awaited_once_with(
        agent.id, "run-this-turn", 1
    )
