"""T16 一期 auto 回合出口模式：``wait_on`` —— 唯一合法的中途挂起工具。

commit_turn 下架后（``HIVEWEAVE_TURN_EXIT_MODE=auto``），模型侧只保留本
工具作为合法挂起出口：refs 从严校验（每项必须指向**存在且非终态**的任务/
agent/可命名事实/user 的未收口 question），校验失败回执带处方（不当挂起
就地拒绝）；合法 ⇒ 登记既有 wait 契约（``wait_contract_service
.replace_waits``，agent_waits 路径与事件唤醒链原样）。挂起后模型直接停止
输出 —— 平台在自然停出口按 wait 契约推断 phase=waiting 并生成收口
（``services/turn_exit_auto.py``）。

T38（2026-10-01）：agent/task ref 认**短号** —— 全平台回执文化用
short_id（A6xx）与 8 位任务前缀，``_validate_ref`` 原先只查全 id 导致
11 连拒（A1 短 id 死锁族，同 T29 commit_turn 预检）。短号命中即通过，
挂起契约落**完整 id**（唤醒链按完整 id 精确匹配）。

T39（2026-10-01）：kind 集**增 user** —— ref = 一道**真实发出且未收口**
的 question 的 question_id（存在性弱校验：用户答复不可预验）。question
轮内超时（STAYS PENDING）后，这是唯一合法的「继续等用户答复」姿势；
question 答复/裁决到达时由 ``question.wake_question_waiters`` 唤醒。

legacy 模式执行面拒绝（工具不下架注册表 —— 注册是全局的；legacy 下模型
工具面本就看不见它，幻觉调用也只会拿到明确回执而非静默成功）。

新工具 schema 纪律（skills/prompt-authoring §3）：只写 pydantic 一处，
不进 TOOL_PARAM_SCHEMAS 手写层。
"""

from __future__ import annotations

from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator

from hiveweave.tools.base import tool
from hiveweave.tools.result import ToolResult

log = structlog.get_logger(__name__)


WAIT_ON_TOOL_DESCRIPTION = (
    "Declare what you are waiting for (unfinished tasks, agents, a user's"
    " answer to a question you asked, or future facts) and end the turn"
    " safely: the platform parks this turn and wakes you when any waited"
    " ref resolves. Every ref must point to an existing, non-terminal"
    " object; invalid refs reject the whole batch with a remedy. This is"
    " the ONLY legal mid-turn pause — there is no other way to end a turn"
    " with unfinished work."
)


class WaitOnItem(BaseModel):
    """单条等待引用：{kind, ref, note?}。kind 集合刻意窄于 commit_turn ——
    timer/external（bg 任务走 job_kill/后台链）不进本工具的合法面；
    user（T39）只认 question 的 question_id，不是通用暂停。"""

    model_config = ConfigDict(populate_by_name=True)

    kind: Literal["task", "agent", "fact", "user"] = Field(
        description=(
            "task = 任务 id（存在且未终态；8 位前缀短号可用）；"
            "agent = 同项目在编 agent（非自己、未归档；A6xx 短号可用，"
            "落账自动转完整 id）；user = 你真实发出且仍未收口的 question"
            " 的 question_id（等该题的用户答复：question 轮内超时后仍想"
            "合法等答复就用它；答复到达或 TTL 到点会唤醒你；不是通用"
            "暂停）；fact = 你命名的待验证事实名（如 merge_landed，"
            "发布时以 [FACT_OBSERVED] 唤醒）。"
        ),
    )
    ref: str = Field(description="被等待对象的 id/名字/短号（非空）")
    note: str | None = Field(
        default=None, description="Optional. 为什么等 / 满足条件是什么。"
    )


class WaitOnParams(BaseModel):
    """wait_on 参数：refs 必填（至少一条）。"""

    model_config = ConfigDict(populate_by_name=True)

    refs: list[WaitOnItem | dict[str, Any] | str] = Field(
        description=(
            "等待对象列表，每项 {kind: task|agent|fact|user, ref: \"<id>\","
            " note?: str}（裸字符串按 task 等待）。每项都必须指向存在且"
            "非终态的对象（user = 未收口的 question_id）；校验失败整批"
            "拒绝并给处方。"
        ),
    )
    note: str | None = Field(
        default=None, description="Optional. 整批等待的说明。"
    )

    @field_validator("refs", mode="before")
    @classmethod
    def _coerce_refs(cls, v: Any) -> Any:
        """入口归一：裸字符串 → {kind: task, ref}；dict 群体单键解包。

        与 commit_turn waiting_on 的归一口径一致（``_coerce_wait_item``
        单源复用），模型发 ``refs: "task-123"`` 不再吃 pydantic 裸报。
        """
        if isinstance(v, str):
            from hiveweave.tools.turn_tools import _coerce_wait_item

            out = _coerce_wait_item(v)
            return [out] if out else []
        if isinstance(v, dict):
            from hiveweave.tools.turn_tools import _coerce_wait_item

            out = _coerce_wait_item(v)
            if out is None:
                return []
            return out if isinstance(out, list) else [out]
        if isinstance(v, (list, tuple)):
            from hiveweave.tools.turn_tools import _coerce_wait_item

            items: list[Any] = []
            for it in v:
                out = _coerce_wait_item(it)
                if out is None:
                    continue
                if isinstance(out, list):
                    items.extend(out)
                else:
                    items.append(out)
            return items
        return v


async def _resolve_task_ref(project_id: str, ref: str) -> tuple[str | None, bool]:
    """T38：任务短号（UUID 前缀）→ 完整 id。

    与 ``wait_contract._task_party_map`` 的 ``id[:8]`` 索引同款口径。
    返回 (resolved | None, ambiguous)：唯一命中才通过；0 命中或 ≥2 命中
    （ambiguous=True）都拒绝。``substr`` 比较（非 LIKE）—— ref 里的
    ``%``/``_`` 不当通配符。
    """
    from hiveweave.db import project as project_db

    try:
        conn = await project_db.get_project_db_by_project_id(project_id)
        cur = await conn.execute(
            "SELECT id FROM tasks WHERE substr(lower(id), 1, ?) = lower(?)",
            [len(ref), ref],
        )
        rows = await cur.fetchall()
        await cur.close()
    except Exception as e:  # noqa: BLE001 — 解析失败按未命中处理（走拒绝处方）
        log.debug("wait_on_task_prefix_resolve_failed", ref=ref, error=str(e))
        return None, False
    ids = [str(r["id"]) for r in rows or []]
    if len(ids) == 1:
        return ids[0], False
    if len(ids) > 1:
        return None, True
    return None, False


async def _validate_ref(
    project_id: str, agent_id: str, item: WaitOnItem
) -> tuple[str | None, str]:
    """单条 ref 从严校验。返回 (None, resolved_ref) = 合法（resolved_ref
    为落账用的完整 id，T38 短号命中时与输入不同）；否则 (病因一句, "")。"""
    kind = str(item.kind)
    ref = str(item.ref or "").strip()
    if not ref:
        return "ref is empty", ""

    if kind == "task":
        from hiveweave.services.task import TaskService
        from hiveweave.services.tasks.constants import TERMINAL_STATUSES

        t = None
        try:
            t = await TaskService().get_task(project_id, ref)
        except Exception as e:
            return f"task lookup failed ({e}) — cannot verify, refusing", ""
        # get_task 自带前缀归一（require_task_id）——落账一律取任务**真实
        # 完整 id**，短号/前缀输入不得原样进 wait 契约。
        resolved = str((t or {}).get("id") or "") or ref
        if not t:
            # T38：回执文化里的任务短号（8 位 UUID 前缀）→ 完整 id。
            prefix_hit, ambiguous = await _resolve_task_ref(project_id, ref)
            resolved = prefix_hit or ""
            if ambiguous:
                return (
                    f"task prefix '{ref}' matches multiple tasks — use the "
                    "full id from the task receipt",
                    "",
                )
            if not resolved:
                return f"task '{ref}' does not exist in this project", ""
            try:
                t = await TaskService().get_task(project_id, resolved)
            except Exception as e:
                return f"task lookup failed ({e}) — cannot verify, refusing", ""
            if not t:
                return f"task '{ref}' does not exist in this project", ""
            resolved = str(t.get("id") or "") or resolved
        status = str(t.get("status") or "")
        if status in TERMINAL_STATUSES or t.get("is_archived"):
            return (
                f"task '{ref}' is terminal/archived (status={status or '?'}) "
                "— nothing to wait on",
                "",
            )
        return None, resolved

    if kind == "agent":
        if ref == agent_id:
            return (
                "waiting on yourself is not a wait — just stop instead",
                "",
            )
        # 「同项目在编」+ 口径统一：wait_on 与 notify_agent 用同一权威解析
        # 链（OrgService.resolve_agent_ref：id → short_id → 花名 exact →
        # 唯一前缀 → role）。S19 实测 12h 内 花名/短号 81 连拒（notify 明示
        # 接受、wait_on 只认全 UUID 的双口径）由此收口；命中 ⇒ 落账完整 id。
        from hiveweave.services.org import OrgService, _SHORT_ID_RE

        try:
            match = await OrgService().resolve_agent_ref(project_id, ref)
        except Exception as e:
            return f"agent lookup failed ({e}) — cannot verify, refusing", ""
        if match is None:
            # 精确报因：resolve_agent_ref 只匹配在编 —— 花名/短号命中但已
            # archived 的目标在这里区分（归档者永远无法唤醒你）。
            try:
                agents = await OrgService().list_agents(project_id)
            except Exception:
                agents = []
            inp = (ref or "").strip()
            norm = "A" + inp[1:].upper() if _SHORT_ID_RE.match(inp) else None
            archived_hit = next(
                (
                    a
                    for a in agents
                    if (a.get("status") or "") == "archived"
                    and (
                        str(a.get("id") or "") == ref
                        or (
                            norm is not None
                            and str(a.get("short_id") or "").upper() == norm
                        )
                        or ((a.get("name") or "").strip().lower() == inp.lower())
                    )
                ),
                None,
            )
            if archived_hit is not None:
                return (
                    f"agent '{ref}' is archived — it can never wake you",
                    "",
                )
            return (
                f"agent '{ref}' does not exist — valid refs: full UUID "
                "(from tool receipts), short_id (A6xx), or 花名 "
                "(see check_agent_status / read_roster)",
                "",
            )
        resolved = str(match.get("id") or "")
        if not resolved:
            return f"agent '{ref}' does not exist", ""
        if resolved == agent_id:
            return (
                "waiting on yourself is not a wait — just stop instead",
                "",
            )
        if (match.get("status") or "") == "archived":
            return f"agent '{ref}' is archived — it can never wake you", ""
        return None, resolved

    # T39：user = 等一道**真实发出过**的 question 的用户答复。红线：
    # 必须关联存在且未收口的 question（存在性弱校验 —— 用户答复不可预验，
    # 只验「这道题存在且还等得到答复」），防止 wait_on(user) 当万能暂停。
    if kind == "user":
        from hiveweave.db import project as project_db

        try:
            conn = await project_db.get_project_db_by_project_id(project_id)
            cur = await conn.execute(
                "SELECT status FROM questions WHERE id = ?", [ref]
            )
            row = await cur.fetchone()
            await cur.close()
        except Exception as e:
            return f"question lookup failed ({e}) — cannot verify, refusing", ""
        if not row:
            return (
                f"question '{ref}' does not exist — wait_on(user) must "
                "reference the question_id of a question you actually asked "
                "(the id is in your question tool receipt); it is not a "
                "generic pause",
                "",
            )
        q_status = str(row[0] or "")
        if q_status == "answered":
            return (
                f"question '{ref}' is already answered — nothing to wait on",
                "",
            )
        if q_status == "timed_out":
            return (
                f"question '{ref}' was already auto-adjudicated to its "
                "default option — no user answer is coming; nothing to "
                "wait on",
                "",
            )
        # pending / cancelled：用户答复不可预验 —— 存在性弱校验到此为止。
        return None, ref

    # fact：等待的是**未来**才发布的事实名 —— 无法预验存在性，从严校验
    # 退为「非空可读名字」。发布时 [FACT_OBSERVED] 唤醒链原样。
    if len(ref) > 120:
        return f"fact name '{ref[:24]}…' too long (<=120 chars)", ""
    return None, ref


@tool(
    "wait_on",
    "Suspend this turn legally while waiting on tasks/agents/a pending "
    "question's user answer/facts. Each ref must point to an existing "
    "non-terminal object (short ids allowed: A6xx agents, 8-char task "
    "prefixes; kind=user takes the question_id of a question you actually "
    "asked and that is still unresolved). After a successful wait_on, STOP "
    "calling tools and stop outputting — the platform commits the turn as "
    "waiting and the wake chain (task progress / agent reply / user answer "
    "/ [FACT_OBSERVED]) will wake you. This is the ONLY legal mid-turn "
    "suspension; hard work must finish or escalate before stopping.",
    requires_workspace=False,
    security_level="standard",
)
async def wait_on_tool(
    params: WaitOnParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """校验 refs → 登记 wait 契约 → 回执指示停止。"""
    from hiveweave.config import turn_exit_mode

    if turn_exit_mode() != "auto":
        return ToolResult.err(
            "wait_on requires HIVEWEAVE_TURN_EXIT_MODE=auto. In legacy mode "
            "end turns with commit_turn instead.",
            remedy=(
                "legacy 模式用 commit_turn(phase='waiting'/'blocked', "
                "waiting_on=[...]) 挂起。"
            ),
        )

    from hiveweave.db import meta as meta_db
    from hiveweave.services.wait_contract import wait_contract_service

    project_id = ""
    try:
        project_id = await meta_db.get_agent_project_id(agent_id) or ""
    except Exception:
        project_id = ""
    if not project_id and ctx is not None:
        project_id = str(getattr(ctx, "project_id", "") or "")
    if not project_id:
        return ToolResult.err(
            "wait_on: cannot resolve project for this agent",
            fact="runner_failed",
        )

    # 归一化 + 逐条校验（从严：一条坏 → 整批拒绝 + 处方，不当挂起）
    items: list[WaitOnItem] = []
    for raw in params.refs:
        if isinstance(raw, WaitOnItem):
            items.append(raw)
        elif isinstance(raw, dict):
            try:
                items.append(WaitOnItem.model_validate(raw))
            except Exception as e:
                return ToolResult.err(
                    f"wait_on rejected: invalid ref entry {raw!r}: {e}",
                    fact="bad_args",
                    remedy=(
                        '每项形状 {"kind": "task|agent|fact|user", '
                        '"ref": "<id>"}'
                    ),
                )
        else:
            return ToolResult.err(
                f"wait_on rejected: ref entry must be object/str, got "
                f"{type(raw).__name__}",
                fact="bad_args",
                remedy='{"refs": [{"kind": "task", "ref": "<taskId>"}]}',
            )
    if not items:
        return ToolResult.err(
            "wait_on rejected: refs is empty — nothing to wait on",
            fact="bad_args",
            remedy=(
                "有具体等待对象就逐条列出；没有就直接停止输出"
                "（平台按 done_slice 收口）。"
            ),
        )

    # 归一化 + 逐条校验（从严：一条坏 → 整批拒绝 + 处方，不当挂起）；
    # T38：合法项落账用 resolved 完整 id（短号输入在此归一）。
    waits: list[dict[str, Any]] = []
    problems: list[str] = []
    for i, item in enumerate(items, 1):
        err, resolved = await _validate_ref(project_id, agent_id, item)
        if err:
            problems.append(f"refs[{i}] ({item.kind}/{item.ref}): {err}")
            continue
        waits.append(
            {
                "kind": item.kind,
                "ref": resolved,
                **({"note": item.note} if item.note else {}),
            }
        )
    if problems:
        return ToolResult.err(
            "wait_on rejected — 不当挂起。每项 ref 必须指向存在且非终态的"
            "任务/agent/未收口 question/可命名事实（短号可用，落账转完整"
            " id）：\n- " + "\n- ".join(problems[:8]),
            fact="bad_args",
            remedy=(
                "修正在场的 ref（用 get_tasks / read_roster 查真实 id；"
                "user 只认你发出过的 question 的 question_id）；"
                "或删掉该条。处方：等任务完成用 kind=task"
                "（ref=任务 id/8 位短号，任务须存在且未终态）；"
                "等他人先 ask/question 发问拿到 question_id，"
                "再以 kind=user 等用户答复；"
                "等自己命名的未来事件用 kind=fact（如 merge_landed）。"
                "若无物可等，直接停止输出即可 —— "
                "平台会按 done_slice 收口。"
            ),
        )

    obligations: list[dict] = []
    try:
        from hiveweave.services.task import TaskService

        obligations = await TaskService().get_open_work_obligations(
            project_id, agent_id
        )
    except Exception as e:
        log.debug("wait_on_obligations_lookup_failed", error=str(e))

    created = await wait_contract_service.replace_waits(
        project_id,
        agent_id,
        waits,
        phase="waiting",
        obligations=obligations,
    )
    log.info(
        "wait_on_registered",
        agent_id=agent_id,
        refs=[(w["kind"], w["ref"]) for w in waits],
        created=len(created or []),
    )
    ref_blob = ", ".join(f"{w['kind']}:{w['ref']}" for w in waits[:8])
    receipt = (
        f"Wait registered ({ref_blob}). Do NOT call any more tools and stop "
        "outputting now — the platform will commit this turn as phase=waiting "
        "and the wake chain will resume you when the wait is satisfied "
        "(or at TTL). If you keep working instead, finish and stop; do not "
        "re-register the same wait."
    )
    if any(w["kind"] == "user" for w in waits):
        receipt += (
            " user 等待做的是存在性弱校验（用户答复不可预验）：该 question"
            " 的答复落库（或被自动裁决）时你会被唤醒；到 TTL 仍无答复也会"
            "被 [WAIT_TIMEOUT] 唤醒。"
        )
    return ToolResult.ok(receipt, waits=waits)
