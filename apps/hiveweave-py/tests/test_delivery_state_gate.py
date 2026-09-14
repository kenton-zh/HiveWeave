"""fixplan #8：CEO 出口门禁从「8 词词表」改成「算出来的状态位」。

## 背景（为什么删掉词表）

前身 `_ceo_exit_assertion_block` 对消息正文做 8 词中英子串匹配（`交付完成`
/`全部完成`/`ship ready`…），命中才查账本。09-14 实测该形态**整体失效**：
CEO 把终验报告全改成「记录之X（不做完工判断）」后**顺利发出**；换语言
（整段法文/西文）同理。且工具描述把触发条件写给了模型（绕过说明书）。

## 现形态（本文件要钉住的）

- 判据 = `project_meta.delivery_state` 状态位（`NULL`=未标记 / `complete` /
  `blocked`），唯一写者 = `mark_delivery_complete`，其内部跑三条**状态查询**
  （未解决 FAIL 终验 / approved 未 closed / 自己未读人工消息）；
- 消息出口（`message_user` **与** `send_message(to=用户)`）**零文本判断、
  不拦截**，只把真实状态挂 `chat_messages.metadata` ⇒ 谎报在用户侧一眼可辨。

用户钦定判据：**"'done' is a computed state, not a declared one"**
（「完成」是算出来的状态，不是声明出来的状态）。

## 本文件钉七件事（对应详案 §六 验收）

1. ★ **反措辞守卫**：同一语义 ≥5 种表达（中/英/法/西/中性「记录之X」+
   否定句 + 复述旧拒绝文案）⇒ 回执与 `metadata` **逐字相同**；
2. **不可绕**：账本干净但不标记、用法文宣布完成 ⇒ 消息**发得出去**，
   但徽章是「未标记完工」；
3. **状态真实**：故意留 1 个 approved 未 closed ⇒ 标记**被拒**并列出该项，
   且落库为 `blocked` + policy code；
4. **正向对照**：清干净 ⇒ 标记成功 ⇒ 徽章 `complete` + 时间戳；
   把状态位手动清回 NULL ⇒ 徽章**必须**回到「未标记」（阳性对照）；
5. **否定句/引用不再有副作用**（并入 1，单列断言）；
6. **老项目兼容**：无状态位 ⇒ 显示「未标记」而**不是**「未完成」；
7. **不改正文**：`content` 与传入逐字一致（状态只走 metadata）+ **侧门**
   （`send_message(recipients=["user"])`）挂同一份徽章。

另钉两条结构网：正典 DDL/迁移形态（三列、**无 DEFAULT**）与「旧词表已被
物理删除」（AST，不用文本子串 —— 本仓钦定测试守卫禁用子串断言）。
"""

from __future__ import annotations

import ast
import pathlib
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.db.schema import PROJECT_DB_TABLES
from hiveweave.tools import misc_tools as mt
from hiveweave.tools import orchestration_tools as ot
from hiveweave.tools.misc_tools import (
    DELIVERY_STATE_BLOCKED,
    DELIVERY_STATE_COMPLETE,
    DELIVERY_STATE_UNMARKED,
    MarkDeliveryCompleteParams,
    MessageUserParams,
    delivery_badge_metadata,
    mark_delivery_complete_tool,
    message_user_tool,
)

PROJECT_ID = "test-delivery-project"
AGENT_ID = "delivery-ceo-uuid"

# 同一语义的 ≥5 种表达（详案 §六.1 钦定口径）+ 两种实测绕过形态。
# ⚠ 旧词表下：①②命中、③④⑦（法/西/中性）与⑤⑥绕过 ⇒ 回执不同。
#    现形态下：七条必须**逐字相同**。
_ASSERTION_PHRASINGS = [
    ("中文", "交付完成：全部任务已收口。"),
    ("英文", "All done — the delivery is complete and shipped."),
    ("法文", "Tout est terminé : la livraison est complète, rien ne reste."),
    ("西班牙文", "Todo está terminado: la entrega está completa."),
    ("中性（实测绕过形态）", "记录之三（不做完工判断）：产物已归档。"),
    ("否定句", "我不宣称全部完成，也不宣称交付完成。"),
    (
        "复述旧拒绝文案",
        "message_user rejected（账本一致性核验）: 发布『全部完成』类交付结论时"
        "项目账本仍不干净——1 个 approved 未 closed 任务。",
    ),
]


# ── 夹具：真实 per-project DB + mock meta 路由 ──────────────────


@pytest.fixture
async def env():
    """真实 tmp 工作区 + 真 per-project DB；meta 路由与 OrgService 用 mock。

    清理照抄 `tests/test_task_kind_field.py` 的 Windows-safe 形态：**先关
    缓存连接再删目录**（打开的文件句柄会挡住 tempdir 删除）。
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        async def fake_get_agent_project_id(aid: str):
            return PROJECT_ID if aid.startswith("delivery-") else None

        def fake_org_service():
            svc = MagicMock()
            svc.get_agent = AsyncMock(return_value={
                "id": AGENT_ID,
                "role": "ceo",
                "permission_type": "ceo",
                "project_id": PROJECT_ID,
            })
            return svc

        project_db._agent_cache.pop(AGENT_ID, None)

        with patch("hiveweave.db.meta.get_project_workspace",
                   fake_get_project_workspace), \
             patch("hiveweave.db.meta.get_agent_project_id",
                   fake_get_agent_project_id), \
             patch("hiveweave.services.org.OrgService", fake_org_service):
            yield {"project_id": PROJECT_ID, "workspace": workspace_path,
                   "agent_id": AGENT_ID}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
        project_db._agent_cache.pop(AGENT_ID, None)


async def _insert_task(env, task_id: str, status: str, *,
                       verdict: str | None = None) -> None:
    conn = await project_db.get_project_db_by_project_id(env["project_id"])
    evidence = "{}" if verdict is None else f'{{"verdict": "{verdict}"}}'
    await conn.execute(
        "INSERT INTO tasks (id, project_id, title, description, creator_id, "
        "status, created_at, updated_at, evidence, is_archived) "
        "VALUES (?,?,?,?,?,?,?,?,?,0)",
        [task_id, env["project_id"], "t", "", "a1", status, 1, 1, evidence],
    )
    await conn.commit()


async def _insert_unread(env, row_id: str, from_id: str) -> None:
    conn = await project_db.get_project_db_by_project_id(env["project_id"])
    await conn.execute(
        "INSERT INTO inbox (id, from_agent_id, to_agent_id, message, read, "
        "created_at) VALUES (?,?,?,?,0,1)",
        [row_id, from_id, env["agent_id"], "请确认"],
    )
    await conn.commit()


async def _delivery_rows(env) -> list[dict]:
    conn = await project_db.get_project_db_by_project_id(env["project_id"])
    cur = await conn.execute(
        "SELECT project_id, delivery_state, delivered_at, delivery_snapshot "
        "FROM project_meta WHERE project_id = ?",
        [env["project_id"]],
    )
    rows = [dict(r) for r in await cur.fetchall()]
    await cur.close()
    return rows


# ── 1. ★ 反措辞守卫（本方案的核心判据） ─────────────────────────


@pytest.fixture
def sent():
    """捕获 message_user / send_message 落库的 payload 与 WebSocket 推送。"""
    saved: list[dict] = []
    chat_cls = MagicMock()
    chat_cls.return_value.save_message = AsyncMock(
        side_effect=lambda attrs: saved.append(attrs) or {"id": "m1"}
    )
    bus = MagicMock()
    bus.publish_chat_message = AsyncMock()
    with patch("hiveweave.services.chat_message.ChatMessageService", chat_cls), \
         patch("hiveweave.realtime.event_bus.status_event_bus", bus):
        yield SimpleNamespace(saved=saved, bus=bus)


@pytest.mark.asyncio
async def test_anti_wording_guard_identical_metadata_across_languages(env, sent):
    """★ 同一语义 7 种表达 ⇒ 回执与 metadata **逐字相同**。

    这是「永远不能用文案判断意图」的直接守卫：只要结果里出现任何随措辞
    变化的字节（回执文案、metadata 字段、拦/放），本条即红。
    """
    # 账本故意留脏：若还残留任何文本判据，命中词表的那几条会走不同分支。
    await _insert_task(env, "approved-open", "approved")
    await _insert_unread(env, "unread-1", "agent-x")

    outcomes = []
    for label, text in _ASSERTION_PHRASINGS:
        before = len(sent.saved)
        result = await message_user_tool(
            MessageUserParams(message=text), env["agent_id"], "/ws", None
        )
        # ⚠ 先断言"发得出去"再比对 metadata —— 否则被拦时会在
        # `sent.saved[-1]` 上抛 IndexError，把"措辞改变了行为"这个**真原因**
        # 冒充成"列表越界"（阳性对照实测踩到过）。
        assert result.success is True, (
            f"{label} 的消息被拦下了 —— 本出口**不得拦截**（详案 §三 方案 A）："
            f"{result.error}"
        )
        assert len(sent.saved) == before + 1, f"{label} 的消息没有落库"
        outcomes.append((label, result, sent.saved[-1]))

    first_label, first_result, first_payload = outcomes[0]
    assert first_result.success is True, first_result.error
    assert set(first_payload["metadata"]) == {
        "delivery_state", "delivery_blockers",
    }, "未标记徽章的字段集合应恰为 {delivery_state, delivery_blockers}"
    assert first_payload["metadata"]["delivery_state"] == DELIVERY_STATE_UNMARKED
    assert first_payload["metadata"]["delivery_blockers"], (
        "账本有 approved 未 closed + 未读 ⇒ 徽章必须带上阻塞项"
    )
    for label, result, payload in outcomes[1:]:
        assert result == first_result, (
            f"回执随措辞变了：{label} vs {first_label}\n"
            f"  {result}\n  {first_result}"
        )
        assert payload["metadata"] == first_payload["metadata"], (
            f"metadata 随措辞变了：{label} vs {first_label}\n"
            f"  {payload['metadata']}\n  {first_payload['metadata']}"
        )
        assert payload["content"] == dict(_ASSERTION_PHRASINGS)[label], (
            f"正文被改写了（不得插文本）：{label}"
        )
    # 全部消息**都发出去了**（不拦：真实状态与正文并排）
    assert len(sent.saved) == len(_ASSERTION_PHRASINGS)


@pytest.mark.asyncio
async def test_negation_and_quoted_old_rejection_have_no_side_effect(env, sent):
    """否定句 / 复述旧拒绝文案 ⇒ 与普通消息行为**逐字一致**（词表已下线）。"""
    await _insert_task(env, "approved-open", "approved")

    plain = await message_user_tool(
        MessageUserParams(message="进度：3/7 完成。"), env["agent_id"], "/ws", None
    )
    plain_meta = sent.saved[-1]["metadata"]
    for label, text in _ASSERTION_PHRASINGS[5:]:
        result = await message_user_tool(
            MessageUserParams(message=text), env["agent_id"], "/ws", None
        )
        assert result == plain, f"{label} 的回执与普通消息不同"
        assert sent.saved[-1]["metadata"] == plain_meta, f"{label} 的 metadata 不同"


# ── 2. 不可绕：法文宣布完成 + 不标记 ⇒ 发得出去但徽章是「未标记」 ──


@pytest.mark.asyncio
async def test_clean_ledger_unmarked_state_still_badges_unmarked(env, sent):
    """账本**干净**、不调 mark、用法文宣布完成 ⇒ 发得出去 + 徽章「未标记」。"""
    await message_user_tool(
        MessageUserParams(message="Tout est terminé, la livraison est complète."),
        env["agent_id"], "/ws", None,
    )
    payload = sent.saved[-1]
    assert payload["metadata"]["delivery_state"] == DELIVERY_STATE_UNMARKED
    assert payload["metadata"]["delivery_blockers"] == [], (
        "账本干净 ⇒ 阻塞项为空，但状态仍是未标记（**绕过了检查 ≠ 骗到了用户**）"
    )


# ── 3. 状态真实：approved 未 closed ⇒ 标记被拒并列出该项 ──────────


@pytest.mark.asyncio
async def test_mark_rejected_lists_blocker_and_persists_blocked(env):
    await _insert_task(env, "approved-open", "approved")
    await _insert_unread(env, "unread-1", "agent-x")

    result = await mark_delivery_complete_tool(
        MarkDeliveryCompleteParams(), env["agent_id"], "/ws", None
    )
    assert result.success is False
    assert "未达完成" in (result.error or "")
    assert "approved 未 closed" in (result.error or "")
    assert "未读人工消息" in (result.error or "")

    rows = await _delivery_rows(env)
    assert len(rows) == 1
    assert rows[0]["delivery_state"] == DELIVERY_STATE_BLOCKED
    snap = rows[0]["delivery_snapshot"]
    assert "LEDGER_APPROVED_OPEN" in snap and "INBOX_UNREAD_HUMAN" in snap, (
        "拒绝原因必须落成 policy code（可断言/可统计/可路由），不只是文案"
    )


@pytest.mark.asyncio
async def test_fail_verdict_is_a_blocker(env):
    await _insert_task(env, "fail-verify", "running", verdict="FAIL")
    result = await mark_delivery_complete_tool(
        MarkDeliveryCompleteParams(), env["agent_id"], "/ws", None
    )
    assert result.success is False
    assert "FAIL 终验" in (result.error or "")


@pytest.mark.asyncio
async def test_archived_and_closed_tasks_are_not_blockers(env):
    """反面对照：closed 任务与 archived 的 approved 都不算阻塞（判据不放大）。"""
    await _insert_task(env, "closed-ok", "closed")
    await _insert_task(env, "approved-archived", "approved")
    conn = await project_db.get_project_db_by_project_id(env["project_id"])
    await conn.execute("UPDATE tasks SET is_archived = 1 WHERE id = 'approved-archived'")
    await conn.commit()

    result = await mark_delivery_complete_tool(
        MarkDeliveryCompleteParams(), env["agent_id"], "/ws", None
    )
    assert result.success is True, result.error


@pytest.mark.asyncio
async def test_mark_tool_accepts_no_text_parameter():
    """结构网：标记工具**没有任何参数** ⇒ agent 无法用措辞影响判定。

    这是与旧词表最根本的区别（旧形态的输入就是自由文本），也是
    "a gate the agent can write to is not a gate" 的落点。
    """
    assert MarkDeliveryCompleteParams.model_fields == {}, (
        "mark_delivery_complete 不能有参数 —— 任何入参都可能成为绕过面"
    )


@pytest.mark.asyncio
async def test_system_copies_do_not_count_as_unread(env):
    """系统副本（from='system'/'用户'）不是 CEO 的账（TEST_DSH_32 P3 原判据）。"""
    await _insert_unread(env, "sys-1", "system")
    await _insert_unread(env, "usr-1", "用户")
    result = await mark_delivery_complete_tool(
        MarkDeliveryCompleteParams(), env["agent_id"], "/ws", None
    )
    assert result.success is True, result.error


# ── 4. 正向对照：标记成功 ⇒ 徽章变 complete；清回 NULL ⇒ 回到未标记 ──


@pytest.mark.asyncio
async def test_positive_control_complete_then_cleared_back_to_unmarked(env, sent):
    """正向对照 + **反向阳性对照**（详案 §六.4 明确要求后者）。"""
    result = await mark_delivery_complete_tool(
        MarkDeliveryCompleteParams(), env["agent_id"], "/ws", None
    )
    assert result.success is True, result.error

    await message_user_tool(
        MessageUserParams(message="交付完成"), env["agent_id"], "/ws", None
    )
    meta = sent.saved[-1]["metadata"]
    assert meta["delivery_state"] == DELIVERY_STATE_COMPLETE
    assert meta.get("delivery_at"), "完成徽章必须带时间戳"
    assert "delivery_blockers" not in meta

    rows = await _delivery_rows(env)
    assert rows[0]["delivered_at"] == meta["delivery_at"]

    # ★ 阳性对照：把状态位手动清回 NULL ⇒ 徽章**必须**回到「未标记」
    #   （若哪一步把 NULL 读成"已完成"，本条即红 —— 这正是老项目误伤的形态）
    conn = await project_db.get_project_db_by_project_id(env["project_id"])
    await conn.execute(
        "UPDATE project_meta SET delivery_state = NULL, delivered_at = NULL, "
        "delivery_snapshot = NULL WHERE project_id = ?",
        [env["project_id"]],
    )
    await conn.commit()
    await message_user_tool(
        MessageUserParams(message="交付完成"), env["agent_id"], "/ws", None
    )
    meta2 = sent.saved[-1]["metadata"]
    assert meta2["delivery_state"] == DELIVERY_STATE_UNMARKED
    assert "delivery_at" not in meta2


@pytest.mark.asyncio
async def test_complete_state_is_re_verified_by_mark_tool(env):
    """再标记一次且账本已脏 ⇒ 状态**降回** blocked（状态位不是一贴永逸的贴纸）。"""
    assert (await mark_delivery_complete_tool(
        MarkDeliveryCompleteParams(), env["agent_id"], "/ws", None
    )).success is True
    await _insert_task(env, "approved-open", "approved")
    result = await mark_delivery_complete_tool(
        MarkDeliveryCompleteParams(), env["agent_id"], "/ws", None
    )
    assert result.success is False
    assert (await _delivery_rows(env))[0]["delivery_state"] == DELIVERY_STATE_BLOCKED


# ── 5. 老项目兼容（无状态位 ⇒ 「未标记」，不是「未完成」） ─────────


@pytest.mark.asyncio
async def test_legacy_project_without_state_shows_unmarked_not_incomplete(env, sent):
    """老项目 ⇒ unmarked，**绝不显示"未完成"**（详案 §六.6，避免误伤）。

    两种并存形态都要覆盖：
      · 没有 project_meta 行（更早的老库）；
      · 有行但三列为 NULL（**真实形态**：项目创建时就建了行，交付三列由
        ALTER 补上 ⇒ 存量行恒 NULL）。第二条是"NULL 被误读成已完成/未完成"
        最容易漏掉的路径。
    """
    assert await _delivery_rows(env) == [], "前置：本项目还没有 project_meta 行"
    badge = await delivery_badge_metadata(env["agent_id"])
    assert badge["delivery_state"] == DELIVERY_STATE_UNMARKED
    assert badge["delivery_state"] not in (DELIVERY_STATE_BLOCKED, DELIVERY_STATE_COMPLETE)

    # 形态二：行存在、三列为 NULL（老项目 ALTER 之后的真实读数）
    conn = await project_db.get_project_db_by_project_id(env["project_id"])
    await conn.execute(
        "INSERT INTO project_meta (project_id, updated_at) VALUES (?, 1)",
        [env["project_id"]],
    )
    await conn.commit()
    rows = await _delivery_rows(env)
    assert (rows[0]["delivery_state"], rows[0]["delivered_at"],
            rows[0]["delivery_snapshot"]) == (None, None, None)
    badge2 = await delivery_badge_metadata(env["agent_id"])
    assert badge2["delivery_state"] == DELIVERY_STATE_UNMARKED


@pytest.mark.asyncio
async def test_unmigrated_old_db_reads_as_unmarked_without_raising(env, sent):
    """列还没补上的老库（懒迁移未跑）⇒ 读失败必须**降级为 unmarked**，不得抛。

    这是老项目最先撞到的形态（DB 建得早、`project_meta` 还没有交付三列）。
    徽章是观测面，读不到就显示"未标记"，**绝不能因为它把消息发送带崩**。
    """
    conn = await project_db.get_project_db_by_project_id(env["project_id"])
    await conn.execute("DROP TABLE project_meta")
    await conn.execute(
        "CREATE TABLE project_meta (project_id TEXT PRIMARY KEY, updated_at INTEGER)"
    )
    await conn.execute(
        "INSERT INTO project_meta (project_id, updated_at) VALUES (?, 1)",
        [env["project_id"]],
    )
    await conn.commit()

    badge = await delivery_badge_metadata(env["agent_id"])
    # ⚠ 先断言"徽章拿得到"——否则读失败时会在下标取键上抛 TypeError，
    # 把"读失败没降级"这个真原因冒充成"NoneType 不可下标"。
    assert badge is not None, (
        "老库读取失败必须降级为「未标记」，而不是让徽章整个消失（fail-soft）"
    )
    assert badge["delivery_state"] == DELIVERY_STATE_UNMARKED

    result = await message_user_tool(
        MessageUserParams(message="交付完成"), env["agent_id"], "/ws", None
    )
    assert result.success is True, "徽章读取失败不得影响消息发送"
    assert sent.saved[-1]["metadata"]["delivery_state"] == DELIVERY_STATE_UNMARKED


def test_new_db_has_the_three_columns_without_default():
    """正典 DDL：三列在**建表**里（新库直接完整），且**不带 DEFAULT**。

    ⚠ 「不带 DEFAULT」是判据本身：语义是「NULL = 未标记（未知）」，带
    DEFAULT 会让 SQLite 把升级前的存量行回填成那个值，让"未知"伪装成
    "已判定"（09-12 `run_steps.started` 实测教训）。用真库再跑一遍方向。
    """
    ddl = next(
        d for d in PROJECT_DB_TABLES
        if "CREATE TABLE IF NOT EXISTS project_meta" in d
    )
    for col in ("delivery_state", "delivered_at", "delivery_snapshot"):
        assert col in ddl, f"正典 DDL 缺列 {col}（新库会靠 ALTER 补 = 迁移有起点无终点）"
    assert "delivery_state TEXT" in ddl and "delivery_state TEXT DEFAULT" not in ddl, (
        "delivery_state 不得带 DEFAULT"
    )
    for col in ("delivered_at", "delivery_snapshot"):
        assert f"{col} TEXT DEFAULT" not in ddl, f"{col} 不得带 DEFAULT"


def test_lazy_migration_exists_for_legacy_dbs():
    """存量库懒迁移在册（ALTER … ADD COLUMN，幂等由建表循环吞异常保证）。"""
    for col in ("delivery_state", "delivered_at", "delivery_snapshot"):
        assert any(
            f"ALTER TABLE project_meta ADD COLUMN {col} " in d
            for d in PROJECT_DB_TABLES
        ), f"缺 {col} 的懒迁移"


@pytest.mark.asyncio
async def test_sqlite_does_not_backfill_legacy_rows(env):
    """★ 判据方向**跑出来**：把列补进一张老表 ⇒ 存量行的值必须是 NULL。

    （读代码会以为"没写就是 NULL"，但 09-12 实测过 SQLite 会给存量行回填
    DEFAULT 值 —— 方向只能实测。）
    """
    conn = await project_db.get_project_db_by_project_id(env["project_id"])
    await conn.execute("DROP TABLE project_meta")
    await conn.execute(
        "CREATE TABLE project_meta (project_id TEXT PRIMARY KEY, updated_at INTEGER)"
    )
    await conn.execute(
        "INSERT INTO project_meta (project_id, updated_at) VALUES (?, 1)",
        [env["project_id"]],
    )
    await conn.commit()
    for col in ("delivery_state", "delivered_at", "delivery_snapshot"):
        await conn.execute(f"ALTER TABLE project_meta ADD COLUMN {col} TEXT")
    await conn.commit()
    cur = await conn.execute(
        "SELECT delivery_state, delivered_at, delivery_snapshot "
        "FROM project_meta WHERE project_id = ?",
        [env["project_id"]],
    )
    row = await cur.fetchone()
    await cur.close()
    assert tuple(row) == (None, None, None), (
        f"存量行被回填了（说明形如「未知」的列带了 DEFAULT）：{tuple(row)}"
    )


# ── 6. 侧门：send_message(recipients=[用户]) 必须挂同一份徽章 ──────


def test_user_recipient_identity_is_structured_not_a_private_alias_table():
    """身份判定收口到 `wake_policy._USER_IDS`，**不再有私有别名表**。"""
    for canonical in ("user", "human", "operator", "用户"):
        assert ot.is_user_recipient(canonical), f"{canonical} 应算人类用户"
    for not_user in ("boss", "老板", "owner", "the user", "", None, 42, "用户2"):
        assert not ot.is_user_recipient(not_user), (
            f"{not_user!r} 不是权威身份集合的成员，不得被当成人类用户"
        )


@pytest.mark.asyncio
async def test_side_door_send_message_attaches_the_same_badge(env, sent):
    """★ 侧门：`send_message(recipients=["user"])` 的 metadata 与 message_user 一致。

    修了 message_user 不堵这条 = 没修（CEO 换个出口发完工结论，用户侧
    一个徽章都看不到）。
    """
    await _insert_task(env, "approved-open", "approved")

    direct = await message_user_tool(
        MessageUserParams(message="交付完成"), env["agent_id"], "/ws", None
    )
    direct_meta = sent.saved[-1]["metadata"]

    side = await ot._send_message_core(
        env["agent_id"], ["user"], "交付完成", "normal", False,
        SimpleNamespace(inbox=object(), org=object()),
    )
    assert side.success is True, side.error
    side_meta = sent.saved[-1].get("metadata")
    # ⚠ 先断言"挂了徽章"再比内容 —— 否则会以 KeyError 收场，把"侧门没挂徽章"
    # 这个**真原因**冒充成"取键失败"（阳性对照实测踩到过）。
    assert side_meta is not None, (
        "侧门消息没有交付状态徽章 —— 侧门成立，验收②（走侧门必须也被堵死）不成立"
    )
    assert side_meta == direct_meta, (
        "两条出口的交付状态徽章不一致 —— 侧门成立，本条的验收②不成立"
    )
    # WebSocket 推送必须同样带徽章（否则前端实时渲染看不到）
    ws_payload = sent.bus.publish_chat_message.call_args.kwargs["message"]
    assert ws_payload["metadata"] == side_meta
    assert direct.success is True


@pytest.mark.asyncio
async def test_non_ceo_sender_gets_no_badge(env, sent):
    """非 CEO 的消息不带徽章（徽章语义 = CEO 的交付声明是否被核验）。

    反面对照：不能因为"sender 不是 CEO"就把徽章写成 unmarked —— 那会给
    用户一个错误的信号（他没有在声明交付）。
    """
    def non_ceo_org():
        svc = MagicMock()
        svc.get_agent = AsyncMock(return_value={
            "id": "delivery-exec-uuid", "role": "executor",
            "permission_type": "executor", "project_id": PROJECT_ID,
        })
        return svc

    with patch("hiveweave.services.org.OrgService", non_ceo_org):
        badge = await delivery_badge_metadata("delivery-exec-uuid")
    assert badge is None, "非 CEO 不得带交付徽章"

    # 且该消息照常发出、payload 里没有 metadata 键（行为与改动前一致）
    with patch("hiveweave.services.org.OrgService", non_ceo_org):
        result = await message_user_tool(
            MessageUserParams(message="交付完成"), "delivery-exec-uuid", "/ws", None
        )
    assert result.success is True
    assert "metadata" not in sent.saved[-1]


@pytest.mark.asyncio
async def test_non_ceo_cannot_mark_delivery_complete(env):
    """非 CEO 调标记工具 ⇒ 被拒（权威门按**调用者**判，不按文本）。"""
    def non_ceo_org():
        svc = MagicMock()
        svc.get_agent = AsyncMock(return_value={
            "id": "delivery-exec-uuid", "role": "executor",
            "permission_type": "executor", "project_id": PROJECT_ID,
        })
        return svc

    with patch("hiveweave.services.org.OrgService", non_ceo_org):
        result = await mark_delivery_complete_tool(
            MarkDeliveryCompleteParams(), "delivery-exec-uuid", "/ws", None
        )
    assert result.success is False
    assert "仅 CEO" in (result.error or "")
    assert await _delivery_rows(env) == [], "被拒不得写入任何状态位"


# ── 7. 结构网：旧词表已被物理删除 ───────────────────────────────


def test_old_text_needle_table_is_gone():
    """旧词表与旧判据函数**必须物理消失**（不是"不再调用"）。

    留着定义等于留着一张随时可以被重新接上的判据表 —— 本仓的复发机制
    就是"往词表里加词"。用 AST 而不是文本子串（本仓钦定：测试守卫禁用
    子串断言；且 docstring/注释里点名它是**解释性**的，不算违规）。
    """
    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "hiveweave"
    banned = {"_COMPLETION_ASSERT_NEEDLES", "_ceo_exit_assertion_block"}
    offenders: list[str] = []
    for py in sorted(src.rglob("*.py")):
        rel = py.relative_to(src).as_posix()
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in banned:
                offenders.append(f"{rel}:{node.lineno} ({node.id})")
            elif isinstance(node, ast.Attribute) and node.attr in banned:
                offenders.append(f"{rel}:{node.lineno} ({node.attr})")
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in banned:
                        offenders.append(f"{rel}:{node.lineno} (定义 {target.id})")
    assert not offenders, (
        "旧词表/旧判据仍在运行时源码里：\n  " + "\n  ".join(offenders)
        + "\n判定应落在 project_meta.delivery_state（状态位），不是消息文本。"
    )
    assert not hasattr(mt, "_COMPLETION_ASSERT_NEEDLES")
    assert not hasattr(mt, "_ceo_exit_assertion_block")


def test_no_textual_judgment_on_the_message_body_in_exit_paths():
    """出口路径**不得**再对消息正文做判定：AST 扫 `message_user_tool` /
    `_send_message_core` 的函数体，断言没有任何对 message 文本的比较/包含。

    这是「零文本判断」的结构网。⚠ 它只覆盖这两个函数体（已知边界）：
    `tools/question.py` 等其它写 chat_messages 的出口不在扫描面内，
    见交付说明「残余风险」。
    """
    def body_calls_the_badge(fn_node: ast.AST) -> bool:
        for n in ast.walk(fn_node):
            if isinstance(n, ast.Name) and n.id in {
                "delivery_badge_metadata", "delivery_badge_line",
            }:
                return True
        return False

    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "hiveweave"
    targets = {
        ("tools/misc_tools.py", "message_user_tool"),
        ("tools/orchestration_tools.py", "_send_message_core"),
    }
    seen: set[tuple[str, str]] = set()
    for rel, fn_name in targets:
        tree = ast.parse((src / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name == fn_name:
                seen.add((rel, fn_name))
                assert body_calls_the_badge(node), (
                    f"{rel}::{fn_name} 没有调用交付状态徽章 helper —— "
                    "该出口的用户看不到真实交付状态"
                )
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Name) and sub.id in {
                        "user_aliases", "_COMPLETION_ASSERT_NEEDLES",
                    }:
                        raise AssertionError(
                            f"{rel}::{fn_name} 仍引用旧判据 {sub.id}"
                        )
    assert seen == targets, f"结构网没扫到预期函数（改名了？）：{targets - seen}"


# ── 8. 模型真正看到的那份手册 ───────────────────────────────────
#
# 09-14 实测（本轮新增证据）：`executor.get_tool_description` /
# `get_tool_schema_for_llm` 是 **TOOL_PARAM_SCHEMAS 优先**，`@tool` 描述只是
# 兜底。⇒ 只改 `misc_tools.py` 的 `@tool` 描述**等于没改**：模型手里仍是旧
# 契约（"说 交付完成 就会触发门禁、会被 REJECTED"）。下面三条对**模型可见的
# 那一份**断言，而不是对源码文本断言（本仓钦定：禁用文本子串找源码）。

# 旧 8 词词表（`git show HEAD:...misc_tools.py` 取回）——它们不得再出现在
# 任何模型可见的描述里。
_LEGACY_TRIGGER_WORDS = (
    "交付完成", "全部完成", "已完成全部", "交付完毕",
    "发布完成", "圆满完成", "全部搞定", "ship ready",
)


def test_model_visible_message_user_description_drops_the_trigger_contract():
    """`message_user` 给模型看的那份描述里，不得再出现旧触发词表，
    也不得把「消息会被拒」当行为契约讲给模型（旧文案原文含 REJECTED）。"""
    from hiveweave.tools import executor as ex

    visible = ex.get_tool_description("message_user")
    assert visible, "message_user 必须有非空描述（否则模型不知道有这个出口）"

    hits = [w for w in _LEGACY_TRIGGER_WORDS if w.lower() in visible.lower()]
    assert not hits, (
        "模型可见的 message_user 描述里仍有旧触发词 "
        f"{hits} —— 这等于把闸门的触发条件写给模型（绕过说明书）；"
        f"实际描述：{visible!r}"
    )
    assert "REJECTED" not in visible, (
        "模型可见的描述仍在宣称消息会被拒绝 —— 现形态不拦消息，"
        f"只并排渲染平台算出来的徽章；实际描述：{visible!r}"
    )


def test_model_visible_schema_has_mark_delivery_complete_with_no_inputs():
    """新工具必须出现在**同一张**模型可见表里，且 `properties` 为空 ——
    任何入参都可能变成"声明式完工"的绕过面。"""
    from hiveweave.tools import executor as ex

    schema = ex.get_tool_schema_for_llm("mark_delivery_complete")
    assert schema.get("properties") == {}, (
        f"mark_delivery_complete 不得接受任何入参，实际：{schema!r}"
    )
    assert not schema.get("required"), f"不得有必填项，实际：{schema!r}"
    desc = ex.get_tool_description("mark_delivery_complete")
    assert desc, "mark_delivery_complete 必须有模型可见描述（否则 CEO 不会用它）"


def test_mark_tool_has_a_capability_decision_not_silent_release():
    """能力位必须**有裁决**（映射或显式豁免）。未映射 = `tool_hard_deny`
    一律放行（见 `services/tool_capability_check` 模块说明：这就是
    start_dev_server 那次漏映射的绕门机制）⇒ 一个 CEO 专属的闸门工具若未
    映射，等于**所有家族**都能自己把交付标成完成，闸门作废。"""
    from hiveweave.services.policy import TOOL_CAPABILITY
    from hiveweave.services.tool_capability_check import EXEMPT_TOOLS

    name = "mark_delivery_complete"
    assert name in TOOL_CAPABILITY or name in EXEMPT_TOOLS, (
        f"{name} 既无能力映射也不在豁免集 ⇒ 未映射工具对全家族硬门放行，"
        "CEO 专属闸门会变成人人可写"
    )
    if name in TOOL_CAPABILITY:
        from hiveweave.services.policy import Capability

        assert Capability.DOC_WRITE in TOOL_CAPABILITY[name], (
            "该闸门的能力位应与「CEO 文档权」对齐（DOC_WRITE 仅 ceo 家族持有）"
        )
