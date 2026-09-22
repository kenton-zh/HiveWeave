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


# ── F ⭐ 生产者侧守卫：归零点必须**真的**走 helper ───────────────
# ⚠ 为什么必须单列一条：上面 A–E **全部直接调** `Agent._resume_upstream_attempt`
#   ⇒ 它们只证明"这个 helper 行为对"，**证不了"归零点用了它"**。把 `agent.py` 的
#   归零点改回无条件 `= 0`，A–E **一条都不会红**（与 D67-1 的 P1-b 同款空守卫：
#   只测机制、不测接线）。判据用 **AST**（用户 09-14 钦定：测试守卫禁用文本子串）。


def test_reset_point_really_calls_the_resume_helper() -> None:
    """F：`_main_upstream_attempt` 的**赋值点**必须是对 helper 的 await 调用。"""
    import ast
    import pathlib

    import hiveweave

    src = (pathlib.Path(hiveweave.__file__).resolve().parent
           / "agents" / "agent.py")
    tree = ast.parse(src.read_text(encoding="utf-8"))

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

    def _is_resume_call(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Await)
            and isinstance(node.value, ast.Call)
            and getattr(node.value.func, "attr", "") == "_resume_upstream_attempt"
        )

    # 形态二选一：直接 `= await _resume_upstream_attempt(...)`，或赋一个**由它算出的局部**。
    # ⚠ 无论哪种，**右值都不许是字面量** —— 那正是"退回无条件 `= 0`"的签名。
    literals = [
        ast.dump(n.value)[:80] for n in assigns
        if isinstance(n.value, ast.Constant)
    ]
    assert not literals, (
        f"归零点被写死了（归零时机回归）：{literals}"
    )
    assert any(_is_resume_call(n.value) for n in assigns) or any(
        isinstance(n.value, ast.Name) for n in assigns
    ), "归零点的右值既不是 helper 调用也不是局部变量 —— 形态变了，本条守卫需重新表述"

    # helper **必须存在**（否则上面的"局部变量"来源无从谈起）
    assert any(
        isinstance(n, ast.Await)
        and isinstance(n.value, ast.Call)
        and getattr(n.value.func, "attr", "") == "_resume_upstream_attempt"
        for n in ast.walk(tree)
    ), "全文件找不到 `_resume_upstream_attempt` 的调用 —— 承接被摘掉了"
    # `+= 1` 是 AugAssign、不在上面集合里；显式确认它**仍然存在**（自增点没被删）
    assert any(
        isinstance(n, ast.AugAssign)
        and isinstance(n.target, ast.Attribute)
        and n.target.attr == "_main_upstream_attempt"
        for n in ast.walk(tree)
    ), "自增点消失了 —— 计数器不再累加，本条的主判据失去前提"


def test_carry_over_is_evaluated_before_create_run() -> None:
    """⭐⭐ **时序守卫**（P0 防线）：承接判定必须排在 `create_run` **之前**。

    为什么这条比 F 还重要：`_resume_upstream_attempt` 内部的 `is_latest_run` 问的是
    "那条中断 run 是不是本 agent 最后一个 run"。**若在 `create_run` 之后求值**，
    "最后一个 run"永远是**本轮刚建的那一条** ⇒ 判定**恒 False** ⇒ **承接永不生效**
    （整个 P1-5 退化成旧的"每轮归零"），而且**行为类测试全绿** —— 因为它们构造的
    "库里只有那条中断 run"这一状态，在**生产里不可达**（生产那一刻必有本轮的新 run）。

    ⇒ 判据只能用**结构**（AST 行号序），不能用"跑一遍看结果"。
    """
    import ast
    import pathlib

    import hiveweave

    src = (pathlib.Path(hiveweave.__file__).resolve().parent
           / "agents" / "agent.py")
    tree = ast.parse(src.read_text(encoding="utf-8"))

    resume_lines: list[int] = []
    create_lines: list[int] = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        attr = getattr(n.func, "attr", "")
        if attr == "_resume_upstream_attempt":
            resume_lines.append(n.lineno)
        elif attr == "create_run":
            create_lines.append(n.lineno)

    assert resume_lines, "找不到 `_resume_upstream_attempt` 的调用点（被改名/删除了？）"
    assert create_lines, "找不到 `create_run` 的调用点 —— 本条守卫失去前提"
    assert min(resume_lines) < min(create_lines), (
        f"承接判定（行 {min(resume_lines)}）必须排在 `create_run`（行 "
        f"{min(create_lines)}）**之前** —— 否则 `is_latest_run` 恒 False，"
        f"承接永不生效（P0，且行为类测试抓不到）"
    )
