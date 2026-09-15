"""VERIFY 串行锁（#11 迁移后：判定读 `tasks.kind`，**不读标题**）。

## 这个文件是从 `test_verify_title_prefix.py` 重写来的

原文件钉的是**标题形态识别**（`is_verify_title` 认不认 `【VERIFY: x】` /
全角冒号 / 小写 / 未锚定…）与基于它的串行锁。`#11`（2026-09-14）把那套**文本
判据整体删除**，改用 `tasks.kind` 字段 —— 于是：

- 那批「标题形态」用例**失去了被测对象**；
- 而文件顶部 `from ...verify import is_verify_title` 使**整个测试套 collect
  失败**（`Interrupted: 1 error during collection`）：一次会话的全量测试因此
  一步都没跑（2026-09-14 实测）。这比"少一个用例"严重得多 —— 它是**筛查面
  整体失效**，且不会有任何用例报红。

## 保留下来仍然有效的性质

**VERIFY 串行锁**：同一时刻只允许一个 in-flight VERIFY（后来者的 claim 必须被
拒），以及 `_in_flight_verify_task` 的锁持有者识别。

原来用「括号标题 vs plain 标题」两种形态交叉验证判据覆盖面；现在判定与标题
**无关**，故改成「kind=verify vs 普通任务」交叉，并**反向**钉住最关键的一条：

> 一个**标题长得像 VERIFY、但 kind 为空**的普通任务，**不得**参与串行锁。

旧的文本判据在这里会把它当成 VERIFY ⇒ 它只要起个这样的名字就能挡住别人的
正常验收 claim（#11 的伪造面）。这条是"#11 真的修好了"的行为侧证据。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from hiveweave.services.task import TaskService
from hiveweave.services.tasks.verify import VERIFY_KIND
from hiveweave.tools.tasks.create import CreateTaskParams
from hiveweave.tools.tasks.verify_spawn import _in_flight_verify_task

from tests.test_idle_architecture_p0 import COORD, EXEC, task_env  # noqa: F401


async def _make_verify(pid, ts, title):
    """系统 spawn 的 VERIFY：**显式 kind**（读 kind，不看标题）。"""
    return await ts.create_task(
        pid,
        title,
        "verify",
        creator_id=COORD,
        assignee_id=EXEC,
        tags=["verify", "mandatory"],
        source="system",
        kind=VERIFY_KIND,
    )


async def _make_plain(pid, ts, title):
    """普通任务：`kind` 留空（**哪怕标题长得像 VERIFY**）。"""
    return await ts.create_task(
        pid, title, "desc", creator_id=COORD, assignee_id=EXEC, source="agent"
    )


@pytest.mark.asyncio
async def test_in_flight_verify_is_detected_by_kind(task_env):
    """kind=verify 的 claimed 任务必须被认成锁持有者（标题可以完全不像 VERIFY）。"""
    ts = TaskService()
    pid = task_env["project_id"]
    va = await _make_verify(pid, ts, "收口演练")   # 标题无 VERIFY 前缀
    await ts.claim_task(pid, va, EXEC)

    blocker = await _in_flight_verify_task(pid)
    assert blocker is not None, "kind=verify 的 in-flight 必须被认出来"
    assert blocker["id"] == va


@pytest.mark.asyncio
async def test_second_verify_claim_is_blocked(task_env):
    """串行锁：已有 in-flight VERIFY 时，第二个 VERIFY 的 claim 必须被拒。"""
    ts = TaskService()
    pid = task_env["project_id"]
    va = await _make_verify(pid, ts, "第一轮验收")
    await ts.claim_task(pid, va, EXEC)

    vb = await _make_verify(pid, ts, "第二轮验收")
    with pytest.raises(ValueError, match="VERIFY"):
        await ts.claim_task(pid, vb, EXEC)
    assert (await ts.get_task(pid, vb))["status"] == "created"


@pytest.mark.asyncio
async def test_title_looking_verify_does_not_hold_the_lock(task_env):
    """★ 反向：#11 的核心 —— **标题不再参与判定**，普通任务挡不住验收。

    伪造面（改造前）：agent 建一个标题为「【VERIFY: x】」的**普通任务**并 claim，
    旧的文本判据会把它当成 in-flight VERIFY ⇒ 真实验收的 claim 被挡
    （"我自己起个名字就占住锁"）。现在判定读 kind，这个任务与锁无关。

    回滚探针：把 `_in_flight_verify_task` 改回按标题判定（或让
    `is_verify_task` 回退到文本判据）即转红。
    """
    ts = TaskService()
    pid = task_env["project_id"]

    fake = await _make_plain(pid, ts, "【VERIFY: M4 后端消息与互动 API】验收对象：互动面板")
    await ts.claim_task(pid, fake, EXEC)

    assert await _in_flight_verify_task(pid) is None, (
        "★ 标题长得像 VERIFY 的普通任务**不得**成为锁持有者 —— "
        "否则「起个名字」就能占用串行锁（#11 的文本判据伪造面）"
    )

    # 而且真实验收照常可 claim（没被那个普通任务挡住）
    real = await _make_verify(pid, ts, "收口演练")
    await ts.claim_task(pid, real, EXEC)
    assert (await ts.get_task(pid, real))["status"] == "claimed"


@pytest.mark.asyncio
async def test_fullwidth_and_lowercase_titles_are_ordinary_tasks(task_env):
    """全角冒号/小写 `verify:` 标题一律是**普通任务**（不再是判据，也不该被拒）。

    改造前这些形态在伪造门里是**双向**的坑：小写 `verify: x` 被拒（连普通任务
    都建不了），全角 `VERIFY：x` 在某些路径被认成 VERIFY。现在两者都只是文本。
    """
    ts = TaskService()
    pid = task_env["project_id"]
    for t in ("VERIFY：M4 收口", "verify: x", "【verify: x】"):
        tid = await _make_plain(pid, ts, t)
        task = await ts.get_task(pid, tid)
        assert task is not None, f"普通任务不得因标题被拒：{t!r}"
        assert not task.get("kind"), f"普通任务的 kind 必须留空：{t!r}"


@pytest.mark.asyncio
async def test_reassign_keeps_verify_queued_but_promotes_plain_task(task_env):
    """改派：VERIFY 保持 `created` 排队；普通任务 `created → claimed`。

    `reassign_task` 里有两处 `_is_verify_task`（created / blocked 分支），它读的
    是 `get_task` 的宽查询行。这条用**同一入口的两种任务**做对照 —— 若判定读不到
    `kind`（例如某天有人把 `get_task` 换成窄 SELECT），两者会**行为相同**，
    于是"改派 VERIFY 会绕过串行锁"这个 P0 就静默复活。

    为什么这条特别值得钉：改派走的是 `created → claimed` 的**旁路**，
    而串行锁在 `claim_task` 里 —— 旁路一旦误把 VERIFY 变成 claimed，就等于
    在锁外面拿到了 in-flight。
    """
    ts = TaskService()
    pid = task_env["project_id"]

    verify_id = await _make_verify(pid, ts, "排队中的验收")
    plain_id = await _make_plain(pid, ts, "普通实现任务")

    fresh_verify = await ts.reassign_task(
        pid, verify_id, new_assignee_id="agent-b", reassigned_by=COORD
    )
    fresh_plain = await ts.reassign_task(
        pid, plain_id, new_assignee_id="agent-b", reassigned_by=COORD
    )

    assert fresh_verify["status"] == "created", (
        "★ VERIFY 改派必须**保持 created 排队**（否则绕过串行锁直接 in-flight）"
    )
    assert fresh_plain["status"] == "claimed", (
        "普通任务改派仍应 assign=claim（确认没有把正常路径改死）"
    )


def test_agent_facing_create_tool_has_no_kind_param():
    """★ 工具边界守卫：`create_task`（agent 唯一入口）**不暴露 `kind`**。

    这是「伪造门」在 #11 之后的替代品：旧实现用正则检查**标题**（文本判据 ⇒
    改措辞即绕过，且误伤「起名像 VERIFY 的普通任务」）；现在 agent 拿不到这个
    字段 —— 它不是工具参数，只能由平台内部路径（`verify_spawn` /
    `tools/tasks/{create,dispatch}.py` 的 milestone 分支）显式写。

    验收问句（本仓钦定）：**能否写一个绕过它的调用方？** —— agent 侧不能：
    它只能经工具参数表达意图，而这里没有这个参数。

    ⚠ 这条用 **pydantic 字段名 + AST 双检查**：
      · 字段名检查挡「直接加了 model 字段」；
      · AST 检查挡「字段藏在 `**kwargs` / `extra="allow"` 之类形态里」。
    """
    names = set(CreateTaskParams.model_fields)
    assert "kind" not in names, (
        f"create_task 工具暴露了 kind 参数 —— agent 就能伪造 VERIFY（#11 复发）：{sorted(names)}"
    )
    assert CreateTaskParams.model_config.get("extra") != "allow", (
        "extra='allow' 会让未声明字段（如 kind）照样透传 —— 等于刚补的边界是漏的"
    )

    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "hiveweave"
    tree = ast.parse((src / "tools/tasks/create.py").read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        # 工具函数的入参签名里不得出现 kind（位置/关键字/`**kwargs` 都算）
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            args = list(node.args.args) + list(node.args.kwonlyargs)
            if any(a.arg == "kind" for a in args):
                offenders.append(f"{node.name}:{node.lineno} 形参")
            if node.args.kwarg is not None:
                offenders.append(f"{node.name}:{node.lineno} **{node.args.kwarg.arg}")
    assert not offenders, (
        f"create_task 工具把 kind 透传进了调用链：{offenders} —— "
        "kind 只能由平台内部路径写，见 crud.create_task 的 docstring"
    )
