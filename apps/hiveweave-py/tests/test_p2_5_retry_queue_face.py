"""P2-5（§10.5）尾项：`request_code_audit` 的「已入队、稍后自动重试」承诺**可核对**。

## 两条独立缺口（2026-09-22 实测，均已回源）

- **缺口①（回执侧）**：`audit_retry.enqueue_failed_audit` 的**首次入队 INSERT 路径**
  只 `log.info`，**没有任何通知**（更新路径耗尽 `:249` / 成功 `:540` / 兜底 `:583` /
  作废 `:608` 四处都有）。而承诺文案在 `tools/code_audit.py` 里 ⇒ **承诺与回执缺一边**：
  agent 只能靠"再调一次看结果"自证，或干脆不信。
- **缺口②（可核对侧）**：`services/platform_state.py` 此前 grep `retry|queue|breaker|audit`
  = **0 命中** ⇒ 队列实况**在 agent 能看到的任何面上都不存在**。

## 本文件守什么（全状态判据）

- **A 逐字段可回查**：`epistemology.verified` 含 key `audit_retry.queued`，其
  `attempts` / `next_retry_at` 与 `SELECT … WHERE id = ?` **逐字段相等**；顶层镜像
  `retry_queue.total` 与真实 pending 行数相等。
- **B 「空」≠「不知道」**：从未入队过（`audit_retry` 表尚未惰性创建）⇒ verified **空列表**，
  **不得**落 unknown（把空说成不知道会让 agent 白跑一次核对）。
- **C 首次入队真回执**：入队后 `SELECT COUNT(*) FROM inbox WHERE
  idempotency_key = 'audit_retry:<id>:0'` == **1**（真查表，不是看 mock 被调过）。
- **D 作用域精确**：同一 `diff_hash` 第二次入队走 UPDATE 路径 ⇒ 该幂等键**仍只有 1 行**
  （"首次"就是首次，不许每次都发）。
- **E 次序契约**：入队回执**必须早于**耗尽通知（`test_audit_epic_fixes` 以 `[-1]`
  取末条断言耗尽文案 —— 本条是那个隐式依赖的**显式**断言）。

⚠ 阳性对照（改坏必须转红）：
  · `audit_retry` 首插路径去掉 `_notify_agent` ⇒ **C** 红；
  · `platform_state` 把该条固定成 `[]` ⇒ **A** 红；
  · 把"表不存在"改判 unknown ⇒ **B** 红。

⚠ **表名**：本仓正典是 `inbox`（`db/schema.py:191`，`idempotency_key` 在 `:207`）——
`dsh-migration-worklist-20260921.md` 前文写的 `inbox_messages` **是错的**（§12.5 已自查更正）。
"""

from __future__ import annotations

import time
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import audit_retry as retry_module
from hiveweave.services.audit_retry import enqueue_failed_audit
from hiveweave.services.platform_state import build_platform_state
from hiveweave.services.org import OrgService

# 复用既有夹具（建临时 workspace + 打桩 `meta.get_project_workspace`）；
# ⚠ 必须 import 进本模块命名空间，否则 pytest 报 fixture not found。
from tests.test_platform_state_p02 import ps_env  # noqa: F401


async def _make_agent(pid: str) -> str:
    agent = await OrgService().create_agent(
        {
            "id": f"p25-agent-{uuid.uuid4().hex[:8]}",
            "project_id": pid,
            "name": "砚台",
            "role": "审计核对工程师",
            "permission_type": "executor",
            "status": "active",
        },
        bootstrap=True,
    )
    return agent["id"]


def _verified_entry(snap: dict, key: str) -> dict | None:
    for row in snap["epistemology"]["verified"]:
        if row.get("key") == key:
            return row
    return None


async def _insert_pending_row(pid: str, agent_id: str) -> str:
    """直插一行 pending（不经 enqueue 路径 —— 本组要孤立验「读侧」）。"""
    await retry_module.ensure_schema(pid)
    rid = str(uuid.uuid4())
    now = int(time.time() * 1000)
    await project_db.execute_by_project(
        pid,
        "INSERT INTO audit_retry "
        "(id, agent_id, task_id, request_json, diff_hash, attempts, "
        "next_retry_at, status, created_at, updated_at) "
        "VALUES (?, ?, NULL, '{}', 'dh-x', 2, ?, 'pending', ?, ?)",
        [rid, agent_id, now + 60_000, now, now],
    )
    return rid


# ── A + 顶层镜像：逐字段可回查 ─────────────────────────────


async def test_retry_queue_face_matches_db_field_by_field(ps_env):
    """A：verified 条目与 DB 行**逐字段**相等（不是"看起来有队列"）。"""
    pid = ps_env["project_id"]
    agent_id = await _make_agent(pid)
    rid = await _insert_pending_row(pid, agent_id)

    snap = await build_platform_state(agent_id=agent_id, project_id=pid)

    entry = _verified_entry(snap, "audit_retry.queued")
    assert entry is not None, "platform_state 必须能看见审计重试队列（承诺可核对）"
    assert entry["epistemic"] == "verified"
    assert len(entry["value"]) == 1
    assert entry["value"][0]["id"] == rid, "id 必须**完整**可回查（截断则回查失败）"

    rows = await project_db.query_by_project(
        pid,
        "SELECT attempts, next_retry_at FROM audit_retry WHERE id = ?",
        [rid],
    )
    assert len(rows) == 1
    assert entry["value"][0]["attempts"] == int(rows[0]["attempts"])
    assert entry["value"][0]["next_retry_at"] == rows[0]["next_retry_at"]

    # 顶层镜像 = 同一份数据（权威仍是 epistemology）
    assert snap["retry_queue"]["total"] == 1
    assert snap["retry_queue"]["pending"] == entry["value"]


async def test_retry_queue_only_lists_own_pending_rows(ps_env):
    """A′：只列**本 agent** 的 **pending** 行（作用域精确，不越界也不漏）。"""
    pid = ps_env["project_id"]
    mine = await _make_agent(pid)
    other = await _make_agent(pid)
    mine_rid = await _insert_pending_row(pid, mine)
    await _insert_pending_row(pid, other)
    # 本 agent 的 exhausted 行不该出现（那已不是"待重试"）
    await project_db.execute_by_project(
        pid,
        "UPDATE audit_retry SET status = 'exhausted' WHERE agent_id = ?",
        [mine],
    )

    snap = await build_platform_state(agent_id=mine, project_id=pid)

    entry = _verified_entry(snap, "audit_retry.queued")
    assert entry is not None
    assert entry["value"] == [], "exhausted 行不算待重试"
    assert snap["retry_queue"]["total"] == 0

    # 反向：别的 agent 的行确实在库里（否则上面那条靠"根本没插进去"也能过）
    all_rows = await project_db.query_by_project(
        pid, "SELECT id FROM audit_retry WHERE status = 'pending'"
    )
    assert {r["id"] for r in all_rows} != {mine_rid}


# ── B：「空」≠「不知道」 ────────────────────────────────────


async def test_never_enqueued_reports_empty_not_unknown(ps_env):
    """B：从未入队（表都没建）⇒ verified **空列表**，不得落 unknown。"""
    pid = ps_env["project_id"]
    agent_id = await _make_agent(pid)

    tables = await project_db.query_by_project(
        pid,
        "SELECT name FROM sqlite_master WHERE type='table' AND name='audit_retry'",
    )
    assert tables == [], "前置：本用例起点的库里不该有 audit_retry 表"

    snap = await build_platform_state(agent_id=agent_id, project_id=pid)

    entry = _verified_entry(snap, "audit_retry.queued")
    assert entry is not None, "空队列也要可核对（说'空'也是答案）"
    assert entry["epistemic"] == "verified"
    assert entry["value"] == []
    assert not [
        r for r in snap["epistemology"]["unknown"]
        if r.get("key") == "audit_retry.queued"
    ], "把'空'说成'不知道'会让 agent 白跑一次核对"


async def test_unreadable_queue_is_unknown_and_mirror_does_not_claim_empty(
    ps_env, monkeypatch
):
    """B′：**读不出来** ≠ 空队列 —— 镜像也不得把它说成"空"。

    与 B 是一对：B 管"空要说成空"，本格管"不知道要说成不知道"。两者的失败方向
    相反（把空说成不知道 ⇒ 白跑一次核对；把不知道说成空 ⇒ **误判承诺没兑现**），
    故必须各有守卫。
    """
    pid = ps_env["project_id"]
    agent_id = await _make_agent(pid)

    async def _boom(*_a, **_k):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(project_db, "query_by_project", _boom)

    snap = await build_platform_state(agent_id=agent_id, project_id=pid)

    assert _verified_entry(snap, "audit_retry.queued") is None
    unknown = [
        r for r in snap["epistemology"]["unknown"]
        if r.get("key") == "audit_retry.queued"
    ]
    assert len(unknown) == 1, "读失败必须落在 unknown（不许静默）"
    # ⭐ 这一条是"自相矛盾"的守卫：不知道时镜像**不许**回空队列
    assert snap["retry_queue"]["pending"] is None
    assert snap["retry_queue"]["total"] is None


async def test_truncation_keeps_total_honest(ps_env):
    """A″：回传被截断时，`total` 必须是**真全量**、且 `truncated` 明示。

    判据来自审计 P2-1 的实测反例：若拿截断后的 `len(rows)` 当 `total`，
    第 6 条起会被静默吞掉而 agent 看不出队列还有活 —— "截断后误判规模"
    正是本条要防的那件事。
    """
    from hiveweave.services.platform_state import _RETRY_QUEUE_MAX

    pid = ps_env["project_id"]
    agent_id = await _make_agent(pid)
    over = _RETRY_QUEUE_MAX + 3
    for _ in range(over):
        await _insert_pending_row(pid, agent_id)

    snap = await build_platform_state(agent_id=agent_id, project_id=pid)

    entry = _verified_entry(snap, "audit_retry.queued")
    assert entry is not None
    assert len(entry["value"]) == _RETRY_QUEUE_MAX, "回传条数受上限约束（体积分层）"
    mirror = snap["retry_queue"]
    assert mirror["total"] == over, (
        f"total 必须是真全量 {over}，不得是截断后的 {_RETRY_QUEUE_MAX}"
    )
    assert mirror["truncated"] is True, "截断必须显式标志，别让 agent 从数字去猜"
    # 反向：未超限时不得谎报截断（否则这个标志就成了恒真的噪音）
    snap2 = await build_platform_state(agent_id=agent_id, project_id=pid)
    assert snap2["retry_queue"]["truncated"] is True  # 同一状态，仍应 True
    other = await _make_agent(pid)
    await _insert_pending_row(pid, other)
    snap3 = await build_platform_state(agent_id=other, project_id=pid)
    assert snap3["retry_queue"]["truncated"] is False
    assert snap3["retry_queue"]["total"] == 1


# ── C + D：首次入队回执（真查 inbox 表）────────────────────


async def test_first_enqueue_writes_inbox_receipt_row(ps_env):
    """C：首次入队 ⇒ `inbox` 里**真的**多一行带该幂等键的回执。"""
    pid = ps_env["project_id"]
    agent_id = await _make_agent(pid)

    out = await enqueue_failed_audit(pid, agent_id, None, "dh-receipt")
    assert out is not None, "前置：入队本身必须成功"

    pending = await project_db.query_by_project(
        pid, "SELECT id FROM audit_retry WHERE status = 'pending'"
    )
    assert len(pending) == 1
    rid = str(pending[0]["id"])

    rows = await project_db.query_by_project(
        pid,
        "SELECT COUNT(*) AS n FROM inbox WHERE idempotency_key = ?",
        [f"audit_retry:{rid}:0"],
    )
    assert int(rows[0]["n"]) == 1, (
        "入队承诺必须落成**可查的行**（不是只 log 一句 / 只叫一次 mock）"
    )


async def test_second_enqueue_same_diff_does_not_resend_first_receipt(ps_env):
    """D：同 `diff_hash` 第二次入队走 UPDATE 分支 ⇒ `:0` 回执**终态只有 1 行**。

    ⚠ **这一条只是"终态"判据**：它**证不了**"我们没有重复发" —— inbox 自己的
    `idempotency_key` 判重也会把第二封吞掉。要证"不重复发"的是下一条 D2。
    （实测：把更新路径也改成发 `:0` ⇒ 本条**仍绿**、D2 转红 —— 这正是两者的
    分工，不是冗余。）
    """
    pid = ps_env["project_id"]
    agent_id = await _make_agent(pid)

    assert await enqueue_failed_audit(pid, agent_id, None, "dh-twice") is not None
    second = await enqueue_failed_audit(pid, agent_id, None, "dh-twice")
    assert second is not None and second["attempts"] == 2, (
        "前置：第二次必须落在**更新**分支（否则本条断言的不是'首次'）"
    )

    rows = await project_db.query_by_project(
        pid,
        "SELECT COUNT(*) AS n FROM inbox WHERE idempotency_key LIKE 'audit_retry:%:0'",
    )
    assert int(rows[0]["n"]) == 1, "「首次入队」的终态作用域就是首次"


async def test_first_receipt_is_sent_exactly_once(ps_env):
    """D2：**发送侧**计数 —— `:0` 回执只发一次（D 证不到的正是这一半）。

    判据是"投递次数"，故必须用记录型 mock 拦在 `InboxService.send_message`，
    不能查表（查表会被 inbox 自身判重掩盖）。
    """
    pid = ps_env["project_id"]
    agent_id = await _make_agent(pid)

    send = AsyncMock(return_value={"should_wake": True})
    with patch("hiveweave.services.inbox.InboxService") as IS:
        IS.return_value.send_message = send
        await enqueue_failed_audit(pid, agent_id, None, "dh-once")
        await enqueue_failed_audit(pid, agent_id, None, "dh-once")

    keys = [str(c.kwargs.get("idempotency_key", "")) for c in send.await_args_list]
    zero_keys = [k for k in keys if k.endswith(":0")]
    assert len(zero_keys) == 1, (
        f"`:0` 回执只能发一次（更新路径不该重发首封），实得 {keys!r}"
    )


async def test_insert_path_always_writes_attempts_one(ps_env):
    """C′：`:0` 幂等键**不撞号**的依据 —— INSERT 路径上 `attempts` 恒为 1。

    `:0` 的唯一性论证依赖「首插必 attempts=1，而更新路径的尾段是 attempts，
    故更新路径发不出 `:0`」。这条不变式此前**无测试钉住**（审计 P2-4）——
    若哪天首插改成 `attempts=0`，`audit_retry:<id>:0` 就会与**首插自身**的键
    重合语义、且与更新路径撞号，而既有用例仍会绿。
    """
    pid = ps_env["project_id"]
    agent_id = await _make_agent(pid)

    out = await enqueue_failed_audit(pid, agent_id, None, "dh-attempts")
    assert out is not None
    assert out["attempts"] == 1, "首插必须 attempts=1（`:0` 键的语义前提）"

    rows = await project_db.query_by_project(
        pid, "SELECT attempts FROM audit_retry WHERE status = 'pending'"
    )
    assert [int(r["attempts"]) for r in rows] == [1]


# ── E：次序契约（入队回执早于耗尽通知）────────────────────


async def test_first_receipt_precedes_exhaustion_notice(ps_env):
    """E：通知次序 = [`…:0`（入队）, …, `…:N`（耗尽）] —— 显式钉住那个隐式依赖。"""
    pid = ps_env["project_id"]
    agent_id = await _make_agent(pid)

    send = AsyncMock(return_value={"should_wake": True})
    with patch("hiveweave.services.inbox.InboxService") as IS:
        IS.return_value.send_message = send
        for _ in range(retry_module.MAX_ATTEMPTS):
            await enqueue_failed_audit(pid, agent_id, None, "dh-order")

    keys = [
        str(c.kwargs.get("idempotency_key", ""))
        for c in send.await_args_list
    ]
    assert keys, "全过程必须至少发过一次通知"
    assert keys[0].endswith(":0"), f"第一条必须是入队回执，实得 {keys!r}"
    assert keys[-1].endswith(f":{retry_module.MAX_ATTEMPTS}"), (
        f"最后一条必须是耗尽通知（既有测试以 [-1] 取它），实得 {keys!r}"
    )
