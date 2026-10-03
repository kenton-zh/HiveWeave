"""Durable Run Ledger — persists agent execution steps for recovery and audit.

Three tables:
- agent_activations: who was woken, by what event
- agent_runs: each execution of chat(), with budget and status
- run_steps: each LLM request, tool call, tool result — written incrementally

Key design:
- Steps are written immediately after each tool completes (not batched)
- On timeout/error, run is marked interrupted; steps survive
- On next activation, interrupted runs generate a checkpoint summary
- chat_messages and conversation_turns remain as UI/semantic views;
  run_steps is the audit trail
"""

import asyncio
import hashlib
import json
import sqlite3
import time
import uuid
from typing import Any

import structlog

from hiveweave.db import project as project_db

log = structlog.get_logger()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _short_hash(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]


# ── agent_runs 事实位列的懒迁移（2026-09-11 批次 1）────────────────
# 键 = (workspace, 连接世代)：与 tasks / inbox 的补列**同族**（库整代重建后
# 旧标记必须失效，否则补列被静默跳过、下游 no such column）。
_FACT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("empty_stream", "INTEGER DEFAULT 0"),
    ("cache_verdict", "TEXT"),
    # report TEST_DSH_54 #6：`final` 分类早已落库（cache_verdict），但
    # **哪一段前缀漂了**（compacted_drift / history_rewritten）只在日志，
    # 平台日志文件一停就永远答不出。明细落库，字面回答该问题。
    ("cache_drifts", "TEXT"),
    # report TEST_DSH_54 #2：run 级孤儿计数事实位 —— 让"run 报 completed
    # 但里面有从未执行/结果未知的步骤"可被机器读出，而不必改 completed 枚举。
    ("orphan_steps", "INTEGER DEFAULT 0"),
    # P1-5（2026-09-22，用户选 E）：**跨轮**上游重试累计。
    # ⚠ 与 `db/schema.py` 的正典 DDL **双登记点**（其一遗漏 ⇒ 新旧库行为分叉，
    #   同 P0-3 的教训）；**无 DEFAULT**（NULL = 老行未记录，与 0 不同形）。
    ("upstream_retry_attempt", "INTEGER"),
    # 批 D 第 2 步（2026-09-26）：stall 收口归因落 run 层。
    # 病灶：`tool_loop` 收口时算出的 `stall_reason`（tool_failed / blocked /
    # readonly / no_progress…）只进日志与返回值，`agent_runs` 无此列 ——
    # 「这个 agent 为什么反复被停泊」跨 run 不可机检（第 3 步验收的前置事实位）。
    # ⚠ 同 `upstream_retry_attempt` 的双登记点纪律（`db/schema.py` 正典 DDL
    #   同步登记 agent_runs 列）；TEXT 无 DEFAULT（NULL = 该 run 非 stall 收口）。
    ("stall_reason", "TEXT"),
)
_fact_columns_ready: set[tuple[str, int]] = set()


async def _ensure_fact_columns(agent_id: str) -> None:
    """给**存量库**补 agent_runs 的事实位列（新库由正典 DDL 直接建）。

    失败**不标记**、下次重试；只把 `duplicate column` 当正常幂等路径。
    """
    key = await project_db.schema_marker_key_for_agent(agent_id)
    if key in _fact_columns_ready:
        return
    pending = False
    for col, ddl in _FACT_COLUMNS:
        try:
            await project_db.execute(
                agent_id, f"ALTER TABLE agent_runs ADD COLUMN {col} {ddl}"
            )
        except sqlite3.OperationalError as exc:
            if "duplicate column" in str(exc).lower():
                continue  # 列已存在 —— 正常幂等路径
            pending = True
            log.warning(
                "run_ledger.fact_column_migration_failed",
                column=col,
                error=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 — 非 OperationalError 同样重试
            pending = True
            log.warning(
                "run_ledger.fact_column_migration_failed",
                column=col,
                error=str(exc),
            )
    if not pending:
        _fact_columns_ready.add(key)


def _summary_from_reason(result_summary: str, error_reason: str) -> str:
    """空摘要兜底：取 error_reason 首个非空行，截 200 字符。"""
    summary = (result_summary or "").strip()
    if summary:
        return summary
    for _line in (error_reason or "").splitlines():
        if _line.strip():
            return _line.strip()
    return (error_reason or "")[:200]


_UPSTREAM_DEATH_TEXT_NEEDLES = (
    "regionerror",
    "region error",
    "not available in your country",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "gateway time-out",
    "internal server error",
)


def _looks_upstream_death_text(error_text: str) -> bool:
    """错误文案是否像上游/区域类死亡（占位行标注用；窄 needle，非判定权威）。

    权威判定是 ``llm/retry.is_upstream_death``（组3，吃异常对象）；这里只有
    error_reason 文本可用，仅用于给占位行打 ``:upstream`` 标签 —— 打错标签
    不影响占位行的补账语义。
    """
    t = (error_text or "").lower()
    return any(n in t for n in _UPSTREAM_DEATH_TEXT_NEEDLES)


class RunLedger:
    """Per-project run ledger service.

    All methods are async and operate on the per-project DB for the given agent.
    Errors are logged but never raised — the ledger is best-effort and must not
    block agent execution.
    """

    async def create_activation(
        self,
        agent_id: str,
        trigger_type: str,
        # I15（批 6，2026-09-27）：「没填」必须是 NULL，不是空串 —— 空串让
        # `IS NOT NULL AND LENGTH(TRIM(col))>0` 判据把「没填」读成「填了」。
        # 缺省 None（唯一调用方 agents/agent.py 走 _activation_trigger_fields
        # 显式传值），落库原样 NULL。
        trigger_source: str | None = None,
        trigger_detail: str = "",
        inbox_msg_ids: list[str] | None = None,
        interrupted_run_id: str | None = None,
        checkpoint_summary: str | None = None,
    ) -> str:
        """Create an activation record when an agent is woken."""
        # M3 孤儿步骤清扫：run 已结束（status != 'running'）却仍 status='running'
        # 的 run_steps 是 record_step_end 写回丢失的孤儿（slack-clone_03 实测
        # 6 行跨 17h 残留），统一标 error 收尾。安全边界：仍 running 的 run
        # 被排除——其工具循环可能正在执行，步骤仍会被补 end；已结束 run 的
        # 工具循环已死，不可能再补写。选 error 而非 interrupted：
        # interrupted 保留恢复语义（generate_checkpoint 会为中断 run 生成
        # 摘要），孤儿步骤是永不收尾的悬挂项，标 error 使其被如实计入
        # 失败分支而非误报为进行中。best-effort，失败不影响激活创建。
        # L5（2026-09-11）：孤儿步骤**结果未知**是第三种事实位，不再只靠自由
        # 文本表达。分野照 DSH `packages/core/session/src/repair.ts:14-18`
        # —— 两个具名恢复码按「有没有 tool/call 事件」区分：
        #   本清扫（run 已死、步骤仍 running）⇒ 调用**已记录但结果未持久化**
        #     ⇒ outcome_unknown=1（DSH TOOL_OUTCOME_UNKNOWN）
        #   startup_sweep（上次进程被杀造成的孤儿）⇒ 调用**从未开始**
        #     ⇒ not_started=1（DSH TOOL_NOT_STARTED），见下方另一条 UPDATE
        # 文案照抄 DSH `repair.ts:106` 原文（含 "Do not retry blindly."）——
        # agent 拿到的是**可执行的重试判据**，不是一句「被清扫了」。
        #
        # ⚠ 2026-09-12 修正（report TEST_DSH_54 #2）：上面这条"结果未知"的
        # 判据此前**不成立** —— 它被无条件用在所有孤儿步骤上，而
        # `record_step_start`（INSERT）发生在 `execute()` 之前，所以
        # "run 已死、步仍 running" 里混着两种完全不同的东西：
        #   · started=1 → 真的执行过 ⇒ 结果未知，可能已有副作用（本分支）
        #   · started=0 → **从未派发** ⇒ 必然无副作用 ⇒ 不该收副作用警告
        # 实测后果：116 个从未执行的步骤（含 submit_task×7 /
        # update_task_status×12）被要求"先核实外部状态"，CEO 的交付消息
        # 因此重发；`not_started` 则全库为 0。现在按 started 分流。
        # 存量行 started IS NULL（无法判定）→ 保守归入 outcome_unknown，
        # 宁可少给"可安全重试"，不可错给。
        try:
            await project_db.execute(
                agent_id,
                "UPDATE run_steps SET status = 'error', ended_at = ?, error = ?, "
                "outcome_unknown = 1 "
                "WHERE run_id IN (SELECT id FROM agent_runs "
                "WHERE agent_id = ? AND status != 'running') "
                "AND status = 'running' "
                "AND (started = 1 OR started IS NULL)",
                [
                    _now_ms(),
                    "orphan step swept: run ended while step running. "
                    "The tool call was interrupted after it was recorded, but no "
                    "result was durably recorded. Its outcome is unknown. Decide "
                    "whether to retry from the tool semantics: retry only if the "
                    "operation is read-only or idempotent; if it may have side "
                    "effects, first verify external state or ask the user. "
                    "Do not retry blindly.",
                    agent_id,
                ],
            )
        except Exception as e:
            log.warning("run_ledger.orphan_step_sweep_failed", agent_id=agent_id, error=str(e))

        # 从未派发的孤儿（started=0）—— 与上一条**分开**：调用没发出去，
        # 必然没有副作用，可直接重试。把它与"结果未知"混成一档，就是
        # report #2 那 116 条误导指令的来源（也让 not_started 全库为 0）。
        try:
            await project_db.execute(
                agent_id,
                "UPDATE run_steps SET status = 'error', ended_at = ?, error = ?, "
                "not_started = 1 "
                "WHERE run_id IN (SELECT id FROM agent_runs "
                "WHERE agent_id = ? AND status != 'running') "
                "AND status = 'running' "
                "AND started = 0",
                [
                    _now_ms(),
                    "orphan step never started: the run ended before this tool "
                    "call was dispatched, so it never executed and had no side "
                    "effect. Retry it if it is still needed.",
                    agent_id,
                ],
            )
        except Exception as e:
            log.warning(
                "run_ledger.orphan_step_not_started_sweep_failed",
                agent_id=agent_id, error=str(e),
            )

        # run 级事实位 orphan_steps（report TEST_DSH_54 #2 的收口条件）。
        # 为什么不改终态枚举：下游 UI 与回归脚本都按 `completed` 统计，
        # 改枚举是破坏性的。用事实位回答同一个问题 —— 「这个 run 报完成，
        # 但里面有没有从未执行/结果未知的步骤」在 run 级**可被机器读出**，
        # 而不是只能下钻到 step 级才发现（CEO 交付消息丢失正是这么发生的）。
        try:
            await _ensure_fact_columns(agent_id)
            for _row in await project_db.query(
                agent_id,
                "SELECT run_id, COUNT(*) FROM run_steps "
                "WHERE (outcome_unknown = 1 OR not_started = 1) "
                "AND run_id IN (SELECT id FROM agent_runs "
                "WHERE agent_id = ? AND status != 'running') "
                "GROUP BY run_id",
                [agent_id],
            ):
                await self.set_run_fact(
                    agent_id, _row[0], orphan_steps=int(_row[1] or 0)
                )
        except Exception as e:
            log.debug("run_ledger.orphan_run_fact_failed", error=str(e))

        # F7 补出口（TEST_DSH_50/51：timeout_kind 在真超时+悬挂上实测 50% / 0%）。
        # 上面 swept 的孤儿步骤，其 run 是被整轮兜底（HARD_TOTAL_TIMEOUT_S + 30
        # 的 asyncio.wait_for）掐断的 —— 那是**整轮级超时**，与工具自身声明的
        # 超时（`Command timed out after Ns` → timeout_kind='command'）不是一回事。
        # 只对「run 的 error_reason 明确是总超时」的行置位，避免把
        # startup_sweep / cancel 造成的孤儿误标成超时。
        # 注：`schema.py` 里 F7 原注释的取值域是 (runner/command/wait)，本处新增
        # `turn`（整轮兜底），注释已同步。
        # ⚠ 只匹配 `orphan step swept%`（**执行过的**那一类）。审计 2026-09-12
        # 指出：放宽到 `orphan step %` 会让「从未派发」（not_started）的步骤也
        # 被贴 timeout_kind='turn'，把"这一步跑没跑"与"run 为什么死"两个轴混在
        # 同一个位上。从未派发的步骤其成因已由 not_started 精确表达，
        # run 级死因在 agent_runs.error_reason —— 不需要借这个位。
        try:
            await project_db.execute(
                agent_id,
                "UPDATE run_steps SET timeout_kind = 'turn' "
                "WHERE status = 'error' "
                "AND error LIKE 'orphan step swept%' "
                "AND timeout_kind IS NULL "
                "AND run_id IN (SELECT id FROM agent_runs "
                "WHERE agent_id = ? AND (error_reason LIKE '%请求总超时%' "
                "OR error_reason LIKE '%total timeout%'))",
                [agent_id],
            )
        except Exception as e:
            log.warning(
                "run_ledger.orphan_step_timeout_kind_failed",
                agent_id=agent_id, error=str(e),
            )

        # A3 事实位接线（批 3，2026-09-28）：上一步只盖了「run 死于整轮总超时」
        # 的行；其余被本清扫判死的步骤（run 死于上游死亡 / cancel / error，以及
        # 下面的 startup_sweep 路径）同样顶着 status='error'，却与
        # record_step_end 写下的真失败（status='failed'）在九个分类位上全 NULL
        # 同形。补盖 `timeout_kind='swept'`（取值域新增值，`db/schema.py` F7
        # 注释已同步）——语义是**步骤级**的「这一行的终态是清扫员写的，不是
        # 执行器写的」，与 'turn'（也是被清扫、但 run 死因明确是总超时）构成
        # 两级归属。⚠ 必须排在上一条 'turn' UPDATE **之后**：那条靠
        # `timeout_kind IS NULL` 抢先占格，本条只收剩下的行（顺序颠倒会把
        # 总超时行误盖成 'swept'）。not_started 行也盖：它们同样是清扫写的
        # 终态（成因已由 not_started 位精确表达，两个位正交不冲突）。
        try:
            await project_db.execute(
                agent_id,
                "UPDATE run_steps SET timeout_kind = 'swept' "
                "WHERE status = 'error' "
                "AND error LIKE 'orphan step %' "
                "AND timeout_kind IS NULL "
                "AND run_id IN (SELECT id FROM agent_runs "
                "WHERE agent_id = ? AND status != 'running')",
                [agent_id],
            )
        except Exception as e:
            log.warning(
                "run_ledger.orphan_step_swept_kind_failed",
                agent_id=agent_id, error=str(e),
            )
        activation_id = str(uuid.uuid4())
        now = _now_ms()
        try:
            await project_db.execute(
                agent_id,
                "INSERT INTO agent_activations "
                "(id, agent_id, trigger_type, trigger_source, trigger_detail, "
                "inbox_msg_ids, interrupted_run_id, checkpoint_summary, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    activation_id,
                    agent_id,
                    trigger_type,
                    trigger_source,
                    trigger_detail,
                    json.dumps(inbox_msg_ids or []),
                    interrupted_run_id,
                    checkpoint_summary,
                    now,
                ],
            )
        except Exception as e:
            log.warning("run_ledger.create_activation_failed", agent_id=agent_id, error=str(e))
        return activation_id

    async def get_upstream_attempt(self, agent_id: str, run_id: str) -> int:
        """读某 run 的**上游重试累计**（P1-5）。fail-open → 0。

        用于"续跑时承接上一 run 的计数"：`agents/agent.py` 在**每轮开头**问它，
        而不再无条件把内存计数器清零。

        fail-open 的理由：**读不到不该让 agent 起不来**；但方向必须是**保守**的 ——
        读不到就 0（= 给满额度），这与"少给额度把正常工作卡死"相比，前者只是回到
        改动前的行为，后者是新故障面。
        """
        rid = str(run_id or "").strip()
        if not rid:
            return 0
        try:
            await _ensure_fact_columns(agent_id)
            rows = await project_db.query(
                agent_id,
                "SELECT upstream_retry_attempt FROM agent_runs WHERE id = ?",
                [rid],
            )
            if not rows:
                return 0
            return int(rows[0]["upstream_retry_attempt"] or 0)
        except Exception as e:  # noqa: BLE001
            log.debug("run_ledger.get_upstream_attempt_failed",
                      agent_id=agent_id, run_id=rid, error=str(e))
            return 0

    async def set_upstream_attempt(
        self, agent_id: str, run_id: str, value: int
    ) -> None:
        """写**当前 run** 的上游重试累计（P1-5）。best-effort，失败只告警。

        写当前 run（不是上一 run）：本 run 若随后被中断/失败，**下一个 turn 就
        能从这一行把它承接走** —— 这就是"可重建的持久承载"。
        ⚠ 不 merge/不清零：只覆盖为调用方给的值（调用方持有唯一权威计数）。
        """
        rid = str(run_id or "").strip()
        if not rid:
            return
        try:
            await _ensure_fact_columns(agent_id)
            await project_db.execute(
                agent_id,
                "UPDATE agent_runs SET upstream_retry_attempt = ? WHERE id = ?",
                [int(value), rid],
            )
        except Exception as e:  # noqa: BLE001
            log.warning("run_ledger.set_upstream_attempt_failed",
                        agent_id=agent_id, run_id=rid, error=str(e))

    async def is_latest_run(self, agent_id: str, run_id: str) -> bool:
        """该 run 是否**本 agent 最新的一行**（P1-5 承接判据）。fail-open → **False**。

        为什么要这一问（P1-5 自查实测的 **P0**）：`status='interrupted'` **全仓
        没有任何地方清除**（只在 `:711` / `:1053` 置位），而
        `find_interrupted_run` 只按 `ended_at DESC LIMIT 1` 取"最新一条 interrupted"
        ⇒ **一条陈旧的中断 run 会一直当选**。若承接只看"`interrupted_run_id` 非空"，
        那么"某轮成功续跑之后，之后每一轮都会继续承接那条陈旧的计数" ⇒
        计数一旦达上限，**该 agent 永久不再重试** —— 把"重试无上界"换成"重试被永久关闭"。

        ⇒ 承接的**正确作用域**是"**正在续跑**"，即那条被中断的 run 就是本 agent
        最后一个 run（其后没有任何 run 被创建过）。三种场景逐一验证：
        - 中断后**还没跑过** ⇒ 它就是最后一个 ⇒ 承接 ✓（链接续）
        - 续跑那轮**成功**了 ⇒ 其后有新 run ⇒ **不承接** ✓（陈旧 run 不再当选）
        - 续跑那轮**又中断** ⇒ 新的那条才是"最新 interrupted"且是最后一个 ⇒ 承接 ✓

        fail-open → False 的方向是**保守**的：不承接 = 归零 = 拿满预算 = 退回改动前
        行为；反过来（读不到就当"已承接"）会凭空扣额度。
        """
        rid = str(run_id or "").strip()
        if not rid:
            return False
        try:
            await _ensure_fact_columns(agent_id)
            # ⚠ 先判"这一行到底在不在"：不存在时子查询是 NULL，`started_at >= NULL`
            # 是 NULL ⇒ 一行都不匹配 ⇒ 会**误返回 True**（与 docstring 的"fail-open
            # → False"相反，且方向危险：把不存在的 run 当作"正在续跑"）。
            if not await project_db.query(
                agent_id, "SELECT id FROM agent_runs WHERE id = ?", [rid]
            ):
                return False
            # ⚠ `id != ?` 是**必须的**：同一毫秒内建的两行 `started_at` 相等，
            # 若子查询/外查询用 `>=` 又不排自己，就会把自己算成"更新的那一行"。
            # `>=` + 排除自己 = 同刻的其他行也算"更新" ⇒ 方向保守（不承接）。
            newer = await project_db.query_one(
                agent_id,
                "SELECT 1 AS newer FROM agent_runs "
                "WHERE agent_id = ? AND id != ? AND started_at >= "
                "(SELECT started_at FROM agent_runs WHERE id = ?) LIMIT 1",
                [agent_id, rid, rid],
            )
            return newer is None
        except Exception as e:  # noqa: BLE001
            log.debug("run_ledger.is_latest_run_failed",
                      agent_id=agent_id, run_id=rid, error=str(e))
            return False

    async def create_run(
        self,
        agent_id: str,
        activation_id: str,
        budget_llm_calls: int = 50,
        budget_tool_calls: int = 100,
        budget_elapsed_ms: int = 600_000,
    ) -> str:
        """Create a run record when _run_llm starts."""
        # 保证事实位列存在（存量库迁移）—— 放在 try 之外：即便 INSERT 失败，
        # 列也必须已就位，否则回归脚本的 R11/R3 查询会 no such column。
        await _ensure_fact_columns(agent_id)
        run_id = str(uuid.uuid4())
        now = _now_ms()
        lease_expires = now + budget_elapsed_ms
        try:
            await project_db.execute(
                agent_id,
                "INSERT INTO agent_runs "
                "(id, agent_id, activation_id, status, lease_expires_at, "
                "budget_llm_calls, budget_tool_calls, budget_elapsed_ms, "
                "actual_llm_calls, actual_tool_calls, started_at) "
                "VALUES (?, ?, ?, 'running', ?, ?, ?, ?, 0, 0, ?)",
                [
                    run_id,
                    agent_id,
                    activation_id,
                    lease_expires,
                    budget_llm_calls,
                    budget_tool_calls,
                    budget_elapsed_ms,
                    now,
                ],
            )
            # Link activation to run
            await project_db.execute(
                agent_id,
                "UPDATE agent_activations SET run_id = ?, consumed_at = ? WHERE id = ?",
                [run_id, now, activation_id],
            )
        except Exception as e:
            log.warning("run_ledger.create_run_failed", agent_id=agent_id, error=str(e))
        return run_id

    async def record_step_start(
        self,
        agent_id: str,
        run_id: str,
        step_index: int,
        step_type: str,
        tool_name: str | None = None,
        tool_call_id: str | None = None,
        tool_args_hash: str | None = None,
        tool_args_excerpt: str | None = None,
    ) -> str | None:
        """Record the start of a step (LLM round or tool call).

        ``tool_args_excerpt``：P2-1 工具参数原文摘录（截断 200 字符）——
        120s 超时命令此前只存 hash，事后不可考。
        """
        step_id = str(uuid.uuid4())
        now = _now_ms()
        try:
            # `started` 显式写 0（不依赖列默认值）：迁移来的老库该列是
            # `INTEGER`（无 DEFAULT，存量行为 NULL）——必须由 INSERT 自己定值，
            # 否则新行也会是 NULL，被清扫保守地当成"可能已执行"。
            await project_db.execute(
                agent_id,
                "INSERT INTO run_steps "
                "(id, run_id, step_index, step_type, tool_name, tool_call_id, "
                "tool_args_hash, tool_args_excerpt, status, started_at, started) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, 0)",
                [
                    step_id,
                    run_id,
                    step_index,
                    step_type,
                    tool_name,
                    tool_call_id,
                    tool_args_hash,
                    tool_args_excerpt,
                    now,
                ],
            )
        except Exception as e:
            log.warning("run_ledger.record_step_start_failed", agent_id=agent_id, error=str(e))
            return None
        return step_id

    async def mark_step_started(self, agent_id: str, step_id: str) -> None:
        """把步骤标成「**已开始执行**」——必须在 ``execute()`` 前一刻调用。

        为什么需要这个位（report TEST_DSH_54 #2/#9，2026-09-12）：
        ``record_step_start`` 只证明"平台记录了这次调用"，它在 ``execute()``
        *之前*发生 —— 所以一行 ``status='running'`` 无法区分
        「从未派发」与「执行中被整轮超时掐死」。清扫侧只能一律记
        ``outcome_unknown``（"结果未知，先核实外部状态"），于是 116 个**从未
        执行**的步骤拿到了副作用警告，`not_started` 全库为 0。

        本方法补的就是那个缺失的输入：置位后，孤儿步骤才真的是"结果未知"；
        未置位的孤儿则是"从未开始"⇒ 必然无副作用 ⇒ 可直接重试。

        best-effort：失败只记日志（漏置位会让该行退回保守的 outcome_unknown，
        宁可少警告，不可错给"可安全重试"）。
        """
        if not step_id:
            return
        try:
            await project_db.execute(
                agent_id,
                "UPDATE run_steps SET started = 1 WHERE id = ?",
                [step_id],
            )
        except Exception as e:
            log.debug("run_ledger.mark_step_started_failed", error=str(e))

    async def record_step_end(
        self,
        agent_id: str,
        step_id: str,
        status: str = "completed",
        result_hash: str | None = None,
        result_size: int | None = None,
        error: str | None = None,
        result_excerpt: str | None = None,
        *,
        runner_failed: bool | None = None,
        command_failed: bool | None = None,
        injection_applied: bool | None = None,
        timeout_kind: str | None = None,
        timeout_ms: int | None = None,
        enforcement: str | None = None,
        git_hardened: bool | None = None,
        executed: bool | None = None,
        denied_by: str | None = None,
        blocked_by_environment: bool | None = None,
        sealed_by: str | None = None,
        exception_type: str | None = None,
        is_platform_bug: bool | None = None,
        exit_code: int | None = None,
        output_empty: bool | None = None,
        truncated: bool | None = None,
        # I1（2026-09-27）：卷余量护栏三正交位 —— 互相独立，绝不嵌套。
        # `disk_bytes_written` 是**卷级近似**（同卷可用空间下降量），单位字节；
        # `disk_limit_hit` = 触顶并被强制中止；`proc_tree_reaped` = kill 后确认树消失。
        disk_bytes_written: int | None = None,
        disk_limit_hit: bool | None = None,
        proc_tree_reaped: bool | None = None,
    ) -> None:
        """Record the end of a step.

        result_excerpt: TEST10 观测性修复 — 截断 2KB 的结果摘录。
        此前 run_steps 只存 result_hash/size，conversation 裁剪后
        约 12% 的工具结果在 DB 中完全不可找回，审计/排障无据可查。

        F4（平台修复计划 2026-08-30）：三组正交事实位 + F7 超时分类。
        ``runner_failed`` / ``command_failed`` / ``injection_applied`` /
        ``timeout_kind`` / ``timeout_ms`` 均 best-effort 落库；
        None = 不写（保持既有缺省），调用方只在能确定时传值 —— 未确定
        不得臆断，宁可留空也不给错误归因（对齐 DSH「致命证据优先于拒绝」）。

        ``enforcement``（#1 治本，2026-09-14）：本条命令**实际**走的执行面
        （``confined`` / ``native``，由 `acl_sandbox.entry.spawn_agent_command`
        无条件盖戳）。它回答的是「这次调用有没有被沙箱约束」——在改造前
        这个问题**无法从账本回答**：沙箱路由是每个工具自己的约定，漏接不产生
        任何信号（`start_dev_server` 从未接线而照样跑）。
        ⚠ 非 spawn 类工具（write_file 等）与遗留行一律 NULL = 「不适用/未判定」，
        不要回填成 ``native``（那会把"没这条信息"说成"确认无沙箱"）。

        ``git_hardened``（0-3，2026-09-16）：该次 spawn 的 env **实际**带没带
        平台的 git 加固配置（`HIVEWEAVE_GIT_HARDENED`）。它回答的是
        「git 自毁时那次调用在不在加固环境里」——#23 的 22 次
        `external diff died` 全发生在 agent 自己的 shell 里，而事后无从归因。
        None = 不适用/未判定（非 spawn 工具、spawn 失败未执行）⇒
        **不要回填成 0**，那会把"没这条信息"说成"确认未加固"。

        ``executed``（F5，2026-09-17）：命令**到底有没有启动**。
        ``False`` = 判定说 confined、而执行函数自己声明进程没起来
        （`PwshUnavailableError` 这类"受限 shell 缺失"）—— 此时
        ``enforcement`` 会落 NULL（见 `agents/streaming.py`），**必须靠本列
        才能把这个状态捞出来**：否则它与"非 spawn 工具"（同样 NULL）
        在数据里同形，「宣告了沙箱却没跑」从此不可查。
        ⚠ 同样**无 DEFAULT、未知留 NULL** —— ``None`` ≠ 「没启动」。
        """
        now = _now_ms()
        if result_excerpt and len(result_excerpt) > 2048:
            result_excerpt = result_excerpt[:2048] + "…[truncated]"
        try:
            # Calculate duration from started_at — SELECT 失败只影响 duration，
            # 降级 now 不重试
            try:
                rows = await project_db.query(
                    agent_id,
                    "SELECT started_at FROM run_steps WHERE id = ?",
                    [step_id],
                )
                started_at = rows[0]["started_at"] if rows else now
            except Exception:
                started_at = now
            duration = now - started_at if started_at else 0
            sql = (
                "UPDATE run_steps SET status = ?, result_hash = ?, "
                "result_size = ?, result_excerpt = ?, error = ?, "
                "ended_at = ?, duration_ms = ? "
                "WHERE id = ?"
            )
            params = [status, result_hash, result_size, result_excerpt, error,
                      now, duration, step_id]
            # F4 事实位 / F7 超时分类 —— 允许为主更新字段拼接。
            # enforcement 同批拼接：它是**观测字段**（不是归因），值为闭合枚举
            # 或 NULL；不参与 COALESCE 组合语义（一条步骤只可能走一条路）。
            if any(v is not None for v in (
                runner_failed, command_failed, injection_applied,
                timeout_kind, timeout_ms, enforcement, git_hardened,
                executed, denied_by, blocked_by_environment, sealed_by,
                exception_type, is_platform_bug, exit_code, output_empty,
                truncated,
                disk_bytes_written, disk_limit_hit, proc_tree_reaped,
            )):
                sql = (
                    "UPDATE run_steps SET status = ?, result_hash = ?, "
                    "result_size = ?, result_excerpt = ?, error = ?, "
                    "ended_at = ?, duration_ms = ?, "
                    "runner_failed = COALESCE(?, runner_failed), "
                    "command_failed = COALESCE(?, command_failed), "
                    "injection_applied = COALESCE(?, injection_applied), "
                    "timeout_kind = COALESCE(?, timeout_kind), "
                    "timeout_ms = COALESCE(?, timeout_ms), "
                    "enforcement = COALESCE(?, enforcement), "
                    "git_hardened = COALESCE(?, git_hardened), "
                    "executed = COALESCE(?, executed), "
                    # P0-3：同款 COALESCE —— 否则既有真值会被后来的 NULL 覆写。
                    "denied_by = COALESCE(?, denied_by), "
                    "blocked_by_environment = COALESCE(?, blocked_by_environment), "
                    "sealed_by = COALESCE(?, sealed_by), "
                    # 批 D 第 2 步：异常出口事实位 + 命令三正交位（同款 COALESCE
                    # —— None = 未确定，保留既有值；False 必须写成 0 不与 None 混同）。
                    "exception_type = COALESCE(?, exception_type), "
                    "is_platform_bug = COALESCE(?, is_platform_bug), "
                    "exit_code = COALESCE(?, exit_code), "
                    "output_empty = COALESCE(?, output_empty), "
                    "truncated = COALESCE(?, truncated), "
                    # I1：卷余量三正交位（同款 COALESCE —— None = 未观测，
                    # 保留既有值；False 必须写成 0，不与 None 混同）。
                    "disk_bytes_written = COALESCE(?, disk_bytes_written), "
                    "disk_limit_hit = COALESCE(?, disk_limit_hit), "
                    "proc_tree_reaped = COALESCE(?, proc_tree_reaped) "
                    "WHERE id = ?"
                )
                params = [
                    status, result_hash, result_size, result_excerpt, error,
                    now, duration,
                    # None = 未确定 → 传 SQL NULL，COALESCE 保留既有值
                    # （否则 COALESCE(0, existing) 会覆盖先前写入的 1）。
                    None if runner_failed is None else (1 if runner_failed else 0),
                    None if command_failed is None else (1 if command_failed else 0),
                    None if injection_applied is None else (1 if injection_applied else 0),
                    timeout_kind,
                    timeout_ms,
                    enforcement,
                    None if git_hardened is None else (1 if git_hardened else 0),
                    # F5：False **必须**写成 0（不能与 None 混同）——
                    # 「确认没启动」正是本列存在的理由。
                    None if executed is None else (1 if executed else 0),
                    denied_by,
                    None if blocked_by_environment is None else (
                        1 if blocked_by_environment else 0
                    ),
                    sealed_by,
                    # 批 D 第 2 步：同款 None=未确定 / False→0 纪律。
                    exception_type,
                    None if is_platform_bug is None else (1 if is_platform_bug else 0),
                    exit_code,
                    None if output_empty is None else (1 if output_empty else 0),
                    None if truncated is None else (1 if truncated else 0),
                    # I1：卷余量三正交位。`disk_bytes_written` 是字节数（原样传，
                    # 不转 bool）；后两者同「None=未观测 / False→0」纪律。
                    disk_bytes_written,
                    None if disk_limit_hit is None else (1 if disk_limit_hit else 0),
                    None if proc_tree_reaped is None else (1 if proc_tree_reaped else 0),
                    step_id,
                ]
            # M3 有界重试：仅对 sqlite3.OperationalError（锁竞争/瞬断，db 层
            # busy_timeout=5s 之后的第二道保险）重试 2 次，50/100ms 退避。
            # ProjectDbError（workspace 驱逐等）重试无意义，直接交给外层
            # 统一告警（绝不外抛）。耗尽后抛给外层统一告警。
            last_error: Exception | None = None
            for attempt in range(3):
                try:
                    await project_db.execute(agent_id, sql, params)
                    last_error = None
                    break
                except sqlite3.OperationalError as e:
                    last_error = e
                    if attempt < 2:
                        await asyncio.sleep(0.05 * (2 ** attempt))
                except Exception as e:
                    last_error = e
                    break
            if last_error is not None:
                raise last_error
        except Exception as e:
            log.warning("run_ledger.record_step_end_failed", agent_id=agent_id, error=str(e))

    async def increment_llm_calls(self, agent_id: str, run_id: str) -> None:
        """Increment the LLM call counter for a run."""
        try:
            await project_db.execute(
                agent_id,
                "UPDATE agent_runs SET actual_llm_calls = actual_llm_calls + 1 WHERE id = ?",
                [run_id],
            )
        except Exception as e:
            log.warning("run_ledger.increment_llm_calls_failed", error=str(e))

    async def increment_tool_calls(self, agent_id: str, run_id: str) -> None:
        """Increment the tool-call counter for a run (BUG-7)."""
        try:
            await project_db.execute(
                agent_id,
                "UPDATE agent_runs SET actual_tool_calls = actual_tool_calls + 1 "
                "WHERE id = ?",
                [run_id],
            )
        except Exception as e:
            log.warning("run_ledger.increment_tool_calls_failed", error=str(e))

    async def set_run_fact(self, agent_id: str, run_id: str, **facts: Any) -> None:
        """写 run 级**事实位**（best-effort，不影响主流程）。

        2026-09-11 批次 1：`empty_stream` / `cache_verdict` 两个事实位的落库口。
        2026-09-12（report TEST_DSH_54 #2）：新增 `orphan_steps` —— 让
        "run 报 completed 但里面有从未执行/结果未知的步骤"在 run 级可读，
        而不必改 `completed` 终态枚举（下游 UI 与回归脚本按它统计）。

        **为什么需要它**：回归清单的 R11 / R3 两条判据此前**只能靠日志猜** ——
        · R11 分不清「usage=0 是丢账」还是「usage=0 是正确记账（0 chunk 无 token
          可记）」⇒ 只能把这类标成"未排除"（漏报）或放宽判据（错报）；
        · R3 把 `hit_ok` / `cache_window_expired` / `drift_zero_hit` 混成一个
          命中率数字 ⇒ 「provider 缓存窗口过期」与「平台自己改写了前缀」被当成
          同一件事，而只有后者是平台侧可修的。
        把事实落库后，口径才能在**判定**层收窄，而不是在**解释**层打补丁。
        """
        allowed = {
            "empty_stream", "cache_verdict", "cache_drifts", "orphan_steps",
            # 批 D 第 2 步：stall 收口归因（agents/completion.py 写入）。
            "stall_reason",
        }
        cols = {k: v for k, v in facts.items() if k in allowed and v is not None}
        if not cols:
            return
        try:
            await _ensure_fact_columns(agent_id)
            sets = ", ".join(f"{k} = ?" for k in cols)
            await project_db.execute(
                agent_id,
                f"UPDATE agent_runs SET {sets} WHERE id = ?",
                [*cols.values(), run_id],
            )
        except Exception as e:
            log.warning("run_ledger.set_run_fact_failed", error=str(e))

    async def _finalize_orphan_steps(
        self, agent_id: str, run_id: str, run_status: str
    ) -> None:
        """批20 修复3（N1）：run 落终态时同点收口该 run 未终态的 run_steps。

        实测（DSH browse 步）：步 00:31 开始、父 run 00:48 completed，步至今
        status='running' —— 运行期没有步级收口，只有 create_activation 的
        M3 清扫（下次激活才跑）与 startup sweep 兜底；agent 不再激活 ⇒
        孤儿步永久悬挂，agent_activity 的「未收口步骤」也一直显示它在跑
        工具。挂在 complete_run / interrupt_run / error_run 这个**单一咽喉**
        （勿在 agents/completion.py 等调用方散改）：

        - status='interrupted' + ended_at 非空 ⇒ 开放步判据（``ended_at IS
          NULL``）立即解除；
        - error 前缀 ``run_<status>: step orphaned at finalize`` —— 归因是
          「run 落终态时步骤没等到自己的 end」，不是工具自身失败（不匹配
          ``orphan step %`` 模式，不会蹭到 M3 清扫的 timeout_kind 分类）；
        - timeout_kind='swept'：与 startup sweep 同语义（schema.py F7 注释
          取值域）—— 这一行终态是收口员写的，不是执行器写的。直接 SET
          安全：record_step_end 写 timeout_kind 时必写终态，running 行不
          可能有值；
        - 幂等：已终态步（completed/failed/error/interrupted）不动；
        - 性能：单条 indexed UPDATE（idx_run_steps_run 前缀 run_id）；
        - 不双写：本方法置 'interrupted' 后，M3 清扫 / startup sweep 的
          ``status = 'running'`` 谓词不再命中这些行。

        best-effort：失败不影响 run 终态本身（只记 warning）。
        """
        try:
            await project_db.execute(
                agent_id,
                "UPDATE run_steps SET status = 'interrupted', ended_at = ?, "
                "error = ?, timeout_kind = 'swept' "
                "WHERE run_id = ? AND status NOT IN "
                "('completed', 'failed', 'error', 'interrupted')",
                [
                    _now_ms(),
                    f"run_{run_status}: step orphaned at finalize",
                    run_id,
                ],
            )
        except Exception as e:
            log.warning(
                "run_ledger.finalize_orphan_steps_failed",
                agent_id=agent_id, run_id=run_id, error=str(e),
            )

    async def complete_run(
        self,
        agent_id: str,
        run_id: str,
        result_summary: str = "",
    ) -> None:
        """Mark a run as completed."""
        now = _now_ms()
        try:
            await project_db.execute(
                agent_id,
                "UPDATE agent_runs SET status = 'completed', ended_at = ?, "
                "result_summary = ? WHERE id = ?",
                [now, result_summary[:500], run_id],
            )
        except Exception as e:
            log.warning("run_ledger.complete_run_failed", error=str(e))
            return
        # 批20 修复3：run 已终态 ⇒ 步级立即收口（run UPDATE 成功才收，
        # 保持「run 终态 ⇒ 步收口」的单向蕴含）
        await self._finalize_orphan_steps(agent_id, run_id, "completed")

    async def interrupt_run(
        self,
        agent_id: str,
        run_id: str,
        reason: str,
        checkpoint_data: dict | None = None,
        result_summary: str = "",
    ) -> None:
        """Mark a run as interrupted (timeout/error/cancel).

        Preserves all completed steps for recovery.
        """
        now = _now_ms()
        checkpoint_json = json.dumps(checkpoint_data, ensure_ascii=False) if checkpoint_data else None
        summary = _summary_from_reason(result_summary, reason)
        try:
            await project_db.execute(
                agent_id,
                "UPDATE agent_runs SET status = 'interrupted', ended_at = ?, "
                "error_reason = ?, result_summary = ?, checkpoint_data = ? WHERE id = ?",
                [now, reason[:500], summary[:500], checkpoint_json, run_id],
            )
        except Exception as e:
            log.warning("run_ledger.interrupt_run_failed", error=str(e))
            return
        # 批20 修复3：同 complete_run —— run 终态 ⇒ 步级立即收口
        await self._finalize_orphan_steps(agent_id, run_id, "interrupted")

    async def error_run(
        self,
        agent_id: str,
        run_id: str,
        error_reason: str,
        result_summary: str = "",
    ) -> None:
        """Mark a run as errored.

        TEST_DSH_47 #2: error runs previously left ``result_summary`` NULL,
        making idle/400-class deaths invisible to token/wall-clock tax
        accounting. Always land a one-line summary alongside the reason.

        R11 占位行（TEST_DSH_63 批3 组4，2026-09-19）：run 实际死亡、
        ``actual_llm_calls > 0`` 且 ``llm_usage`` 无任何行时插入一条全 0 占位
        usage 行 —— 「declared > 0 必须有账」恢复 0 断口（DSH_63 实测：其一
        run actual_llm_calls=1 却零账）。占位行可机检：
        - 行侧：``request_type IS NULL`` + ``provider IS NULL`` + 全 0 token
          + ``duration_ms = 0``（真实请求行 request_type/provider 均非空）；
        - run 侧：``result_summary`` **前置** ``[llm_usage_placeholder]`` 标记
          （上游死亡时为 ``[llm_usage_placeholder:upstream]``）—— 前置而非
          追加，因为 summary 截 500 字符，追加式标记可能被截掉丢机检位。
          回归判据用 run↔usage 行 join 即可区分「真 0 账（丢账）」与
          「占位（上游死亡，token 未回传）」。
        best-effort：占位失败不改变 error 标记本身。
        """
        now = _now_ms()
        summary = _summary_from_reason(result_summary, error_reason)
        try:
            await project_db.execute(
                agent_id,
                "UPDATE agent_runs SET status = 'error', ended_at = ?, "
                "error_reason = ?, result_summary = ? WHERE id = ?",
                [now, error_reason[:500], summary[:500], run_id],
            )
            # 批20 修复3：run 终态 ⇒ 步级立即收口（finalize 自吞异常，
            # 不影响下方占位补账；UPDATE 失败则不收 —— 保持「run 终态
            # ⇒ 步收口」的单向蕴含）
            await self._finalize_orphan_steps(agent_id, run_id, "error")
        except Exception as e:
            log.warning("run_ledger.error_run_failed", error=str(e))
        try:
            await self._insert_placeholder_usage_for_dead_run(
                agent_id, run_id, error_reason, now
            )
        except Exception as e:  # noqa: BLE001 — 占位是补账，绝不拦主流程
            log.warning("run_ledger.placeholder_usage_failed", error=str(e))

    async def _insert_placeholder_usage_for_dead_run(
        self,
        agent_id: str,
        run_id: str,
        error_reason: str,
        died_at_ms: int,
    ) -> None:
        """零账死亡 run 的 R11 占位 usage 行（判据见 :meth:`error_run`）。"""
        if not run_id:
            return
        rows = await project_db.query(
            agent_id,
            "SELECT actual_llm_calls, result_summary FROM agent_runs "
            "WHERE id = ?",
            [run_id],
        )
        if not rows:
            return
        if int(rows[0]["actual_llm_calls"] or 0) <= 0:
            return  # declared=0 ⇒ 无账可补（R11 判据不涉及）
        cur = await project_db.query(
            agent_id,
            "SELECT COUNT(*) AS c FROM llm_usage WHERE run_id = ?",
            [run_id],
        )
        if cur and int(cur[0]["c"] or 0) > 0:
            return  # 已有账 —— 占位只补「零账」断口
        model_id = None
        try:
            mrows = await project_db.query(
                agent_id,
                "SELECT model_id FROM agents WHERE id = ?",
                [agent_id],
            )
            if mrows:
                model_id = mrows[0]["model_id"]
        except Exception as e:  # noqa: BLE001 — model 拿不到就 NULL
            log.debug("run_ledger.placeholder_model_lookup_failed", error=str(e))
        project_id = None
        try:
            from hiveweave.db import meta as meta_db

            project_id = await meta_db.get_agent_project_id(agent_id)
        except Exception:  # noqa: BLE001
            project_id = None
        upstream = _looks_upstream_death_text(error_reason)
        marker = (
            "[llm_usage_placeholder:upstream]"
            if upstream
            else "[llm_usage_placeholder]"
        )
        prev_summary = str(rows[0]["result_summary"] or "")
        if marker not in prev_summary:
            # 标记放最前：result_summary 截 500 字符，追加式标记可能被
            # 截掉而丢机检位；前置标记永不丢（正文被截断可接受）。
            new_summary = (
                f"{marker} | {prev_summary}" if prev_summary else marker
            )
            await project_db.execute(
                agent_id,
                "UPDATE agent_runs SET result_summary = ? WHERE id = ?",
                [new_summary[:500], run_id],
            )
        await project_db.execute(
            agent_id,
            "INSERT INTO llm_usage (id, agent_id, project_id, run_id, task_id, "
            "model_id, request_type, provider, input_tokens, output_tokens, "
            "cache_read_tokens, cache_creation_tokens, total_tokens, "
            "duration_ms, cold_start, creation_unreported, created_at) "
            "VALUES (?, ?, ?, ?, NULL, ?, NULL, NULL, 0, 0, 0, 0, 0, 0, 0, 0, ?)",
            [str(uuid.uuid4()), agent_id, project_id, run_id, model_id, died_at_ms],
        )
        log.warning(
            "run_ledger.placeholder_usage_inserted",
            agent_id=(agent_id or "")[:12],
            run_id=(run_id or "")[:8],
            model_id=str(model_id or "")[:40],
            upstream=upstream,
        )

    async def find_interrupted_run(self, agent_id: str) -> dict | None:
        """Find the most recent interrupted run for an agent."""
        try:
            rows = await project_db.query(
                agent_id,
                "SELECT id, agent_id, activation_id, started_at, ended_at, "
                "error_reason, checkpoint_data, actual_llm_calls, actual_tool_calls "
                "FROM agent_runs WHERE agent_id = ? AND status = 'interrupted' "
                "ORDER BY ended_at DESC LIMIT 1",
                [agent_id],
            )
            if rows:
                r = rows[0]
                return {
                    "run_id": r["id"],
                    "agent_id": r["agent_id"],
                    "activation_id": r["activation_id"],
                    "started_at": r["started_at"],
                    "ended_at": r["ended_at"],
                    "error_reason": r["error_reason"],
                    "checkpoint_data": r["checkpoint_data"],
                    "actual_llm_calls": r["actual_llm_calls"],
                    "actual_tool_calls": r["actual_tool_calls"],
                }
        except Exception as e:
            log.warning("run_ledger.find_interrupted_run_failed", error=str(e))
        return None

    async def get_run_steps(self, agent_id: str, run_id: str) -> list[dict]:
        """Get all steps for a run (for checkpoint generation)."""
        try:
            rows = await project_db.query(
                agent_id,
                "SELECT step_index, step_type, tool_name, tool_call_id, "
                "tool_args_hash, status, result_hash, result_size, "
                "result_excerpt, error, "
                "started_at, ended_at, duration_ms "
                "FROM run_steps WHERE run_id = ? ORDER BY step_index ASC",
                [run_id],
            )
            return [dict(r) for r in rows]
        except Exception as e:
            log.warning("run_ledger.get_run_steps_failed", error=str(e))
            return []

    async def generate_checkpoint(self, agent_id: str, run_id: str) -> str:
        """Generate a human-readable checkpoint summary from interrupted run steps."""
        steps = await self.get_run_steps(agent_id, run_id)
        if not steps:
            return "No steps recorded before interruption."

        lines = []
        tool_calls = [s for s in steps if s["step_type"] == "tool_call" and s["status"] == "completed"]
        llm_rounds = [s for s in steps if s["step_type"] == "llm_request"]

        lines.append(f"Interrupted run had {len(llm_rounds)} LLM round(s) and {len(tool_calls)} completed tool call(s).")
        lines.append("Completed tool calls:")
        for s in tool_calls:
            tn = s.get("tool_name") or "unknown"
            dur = s.get("duration_ms") or 0
            lines.append(f"  - {tn} ({dur}ms) result_hash={s.get('result_hash', 'n/a')}")

        # 清扫写入的孤儿步骤状态为 'error'（区别于失败 'failed' 与恢复
        # 语义 'interrupted'），checkpoint 统计必须同样纳入，否则孤儿步骤
        # 在摘要里静默缺失（审计 P2）。
        failed = [s for s in steps if s["status"] in ("failed", "error")]
        if failed:
            lines.append(f"Failed steps: {len(failed)}")
            for s in failed:
                lines.append(f"  - {s.get('tool_name', s['step_type'])}: {s.get('error', 'unknown')}")

        summary = "\n".join(lines)
        log.info("run_ledger.checkpoint_generated", agent_id=agent_id, run_id=run_id, steps=len(steps))
        return summary

    async def get_step_count(self, agent_id: str, run_id: str) -> int:
        """Get the total number of steps for a run."""
        try:
            rows = await project_db.query(
                agent_id,
                "SELECT COUNT(*) as cnt FROM run_steps WHERE run_id = ?",
                [run_id],
            )
            return rows[0]["cnt"] if rows else 0
        except Exception:
            return 0

    async def check_budget(
        self, agent_id: str, run_id: str
    ) -> tuple[bool, str]:
        """Check if the run has exceeded its call budgets.

        Wall-clock elapsed is not a stop condition here: long coding is
        expected to outlive 10 minutes across budget-checked slices (the
        streamer-level turn wrap handles per-slice wall clock; see
        ``hiveweave.llm.streamer.constants``).
        """
        try:
            rows = await project_db.query(
                agent_id,
                "SELECT actual_llm_calls, actual_tool_calls, "
                "budget_llm_calls, budget_tool_calls "
                "FROM agent_runs WHERE id = ?",
                [run_id],
            )
            if not rows:
                return False, ""
            r = rows[0]
            llm = r["actual_llm_calls"]
            tools = r["actual_tool_calls"]
            if llm >= r["budget_llm_calls"]:
                return True, f"llm_calls {llm} >= {r['budget_llm_calls']}"
            if tools >= r["budget_tool_calls"]:
                return True, f"tool_calls {tools} >= {r['budget_tool_calls']}"
            return False, ""
        except Exception as e:
            log.debug("run_ledger.check_budget_failed", error=str(e))
            return False, ""

    async def extend_elapsed_budget(
        self, agent_id: str, run_id: str, extra_ms: int
    ) -> None:
        """Credit back elapsed budget for a subagent spawn.

        Subagent runs synchronously inside the parent's turn; its wall-clock
        time must not starve the parent's own budget. Shifting started_at
        earlier by extra_ms gives the parent back that window.
        """
        if extra_ms <= 0:
            return
        try:
            await project_db.execute(
                agent_id,
                "UPDATE agent_runs SET started_at = started_at - ? WHERE id = ?",
                [extra_ms, run_id],
            )
        except Exception as e:
            log.warning("run_ledger.extend_budget_failed",
                        agent_id=agent_id, error=str(e))


# Singleton
run_ledger = RunLedger()


async def sweep_stale_agent_runs(
    workspace_path: str | None,
    *,
    close_reason: str = "startup_sweep: stale running run from prior process",
) -> int:
    """E16 (复盘 P2)：启动收尾 sweep —— 清算上次进程残留的 running runs。

    Agent 长驻服务在 turn 中途被杀时，agent_runs 会残留 ``status='running'``
    的孤儿行（无 ended_at），污染查询/统计并掩盖真实收尾语义。启动时把
    workspace 内所有仍为 running 的 run 归并为 ``interrupted``（预留步骤，
    与现有 interrupt_run 语义一致，供恢复/审计读取）。返回清理行数。

    I8（批 7，2026-09-27）：
    · ``close_reason`` 参数化（审计 P2-2）—— 下班收尾 / activate 回收复用
      本函数时，error_reason 不得再谎称「prior process」（同状态不同来路，
      恢复 Trail 语义必须可分）。
    · ``result_summary`` 对齐 ``interrupt_run``（审计 P2-3）：空值补
      close_reason —— 同状态两种行的 summary 可空性不再分叉（下游
      ``IS NOT NULL`` 判据不再读反）。
    · ⚠ **全程持 per-workspace 写锁**（审计 P1-2，``db/project.py`` 共享
      连接纪律）：sweep 的 UPDATE 与 commit 之间，持锁写者（game time tick
      等）的 error-path rollback 会把未提交的 interrupted **静默卷回
      running**（函数仍返回 N ⇒ 收尾/回收验收假绿）。与 create_run 同锁
      串行化后，SELECT→UPDATE 的 TOCTOU 一并关闭。
    · 失败可观测（审计 P2-1）：内部失败升 warning —— 「回收失败」与
      「扫了没有」在日志层必须可分，否则验收①静默假绿。

    批 2 A7（2026-09-28）：被本 sweep 收口的 run 正是 R11「interrupted
    零账」的主力来源（实测 5 条 interrupted 里 3 条来自这类）—— 中断时
    per-round usage 还在**上个进程的内存 sink**（``agent._pending_usage``）
    里，随进程死亡蒸发，token 本体不可补记。按 ``error_run`` 的 R11 占位行
    同款口径补**机检占位**（仅 ``actual_llm_calls > 0`` 且 ``llm_usage``
    零行才补），把「零账 = 进程死亡、token 不可考」变成可 join 查出的
    事实，而不是静默断口。见
    ``_insert_placeholder_usage_for_swept_runs``。
    """
    if not workspace_path:
        return 0
    now = _now_ms()
    lock = await project_db.get_workspace_write_lock(workspace_path)
    async with lock:
        return await _sweep_stale_agent_runs_locked(
            workspace_path, now, close_reason)


# P0-3（2026-10-02）：运行时租约 sweep 的收口来路文案（落 agent_runs.error_reason）。
# 与 startup_sweep / off_duty_close / activate_reclaim 同族 —— 同状态不同来路，
# 恢复 Trail 语义必须可分（I8 批 7 审计 P2-2 的参数化纪律延伸）。
RUN_LEASE_SWEEP_CLOSE_REASON = (
    "run_lease_sweep: running run lease expired in live process "
    "(owner died without close)"
)


async def sweep_expired_run_leases(
    workspace_path: str | None,
    *,
    now: int | None = None,
    exempt_run_ids: set[str] | frozenset[str] | None = None,
) -> int:
    """P0-3：运行时租约 sweep —— 进程存活期间收口悬挂的 ``running`` run。

    病灶：``agent_runs.lease_expires_at``（``create_run`` 写入）此前**只写
    不读** —— 进程内 run 死亡（收口路径漏掉 / asyncio task 静默消失 /
    OOM kill）后，running 行一直悬挂到下次重启才被 startup sweep 收口
    （实测 5 条悬挂 ≈9 小时）。本函数由 game_time 周期 tick（30s 槽位，
    ``GameTimeService._sweep_expired_run_leases``）调用，把 startup sweep
    的收口语义搬到运行时。

    - 判据：``status='running'`` 且 ``lease_expires_at`` 已过期（NULL 视为
      老行，交还给启动 sweep，不在运行时误收）。收口语义与启动 sweep
      **同源复用**（``_sweep_running_rows_locked``）：interrupted + 孤儿
      run_step 按 started 事实位分流 + R11 零账占位补账。
    - **防误杀**（与启动 sweep 的唯一判据差异）：启动时进程内必然无活
      run，startup sweep 无条件全收；运行时不行 —— 进程内仍在跑的 run
      不能动。豁免 = 调用方传入的 ``exempt_run_ids``（进程内活跃注册表：
      PROCESSING 中 agent 的 ``_current_run_id``，game_time 侧按项目汇总）。
      ⚠ 预算语义：``check_budget`` 不按墙钟停（长任务预期活得比
      ``budget_elapsed_ms`` 久），健康长 run 的 lease 普遍已过期 —— 注册表
      豁免是唯一防线，不得在 DB 侧另行收窄判据。
    - 性能：``idx_agent_runs_status`` 命中后按 lease 收窄的单查询；无过期
      行时零写（只 SELECT）。全程持 per-workspace 写锁（与 create_run /
      startup sweep 同锁串行化，见 ``sweep_stale_agent_runs`` 的 I8 注释）。

    与 startup sweep 的语义一致性：usage 补录**同源**（批 2 A7 占位行，
    见 ``_insert_placeholder_usage_for_swept_runs``）—— 两处都只补「零账 =
    死亡、token 不可考」的机检占位，不补真实 token。运行时 sweep **不做**
    close_running_runs 那样的内存 pending usage 预 flush：它面向的悬挂 run
    的活账要么已被恢复路径接走、要么随死亡协程蒸发，中途抢 sink 反而可能
    孤儿化活 run 的后续轮次（活 run 靠注册表豁免根本不会被本函数碰到）。

    Returns 收口行数；任何失败只 warning（tick 路径绝不能炸）。
    """
    if not workspace_path:
        return 0
    ts = int(now) if now is not None else _now_ms()
    lock = await project_db.get_workspace_write_lock(workspace_path)
    async with lock:
        return await _sweep_expired_run_leases_locked(
            workspace_path, ts, frozenset(exempt_run_ids or ()),
        )


async def _sweep_expired_run_leases_locked(
    workspace_path: str, now: int, exempt_run_ids: frozenset[str],
) -> int:
    """``sweep_expired_run_leases`` 的锁内实现：构建租约判据后走公共核心。"""
    run_filter = "lease_expires_at IS NOT NULL AND lease_expires_at < ?"
    filter_params: list = [now]
    if exempt_run_ids:
        run_filter += " AND id NOT IN (%s)" % ", ".join(
            "?" * len(exempt_run_ids)
        )
        filter_params.extend(sorted(exempt_run_ids))
    return await _sweep_running_rows_locked(
        workspace_path, now, RUN_LEASE_SWEEP_CLOSE_REASON,
        run_filter=run_filter,
        filter_params=filter_params,
        step_note_prefix="run_lease_sweep",
        step_origin="a run that died in this process without close "
        "(lease expired)",
        log_event="run_lease_sweep",
        steps_fail_event="run_lease_sweep_steps_failed",
        fail_event="run_lease_sweep_failed",
        per_row_log_event="run_lease_sweep_reaped",
    )


async def _sweep_stale_agent_runs_locked(
    workspace_path: str, now: int, reason: str,
) -> int:
    """startup sweep 的锁内入口（P0-3 提取后成为公共核心的薄壳）。

    启动时进程内必然没有活 run（agent_manager 还是空的），所以无条件
    收尾全部 running 行 —— 判据收窄与注册表豁免只在运行时租约 sweep
    （``sweep_expired_run_leases``）需要。
    """
    return await _sweep_running_rows_locked(workspace_path, now, reason)


async def _sweep_running_rows_locked(
    workspace_path: str,
    now: int,
    reason: str,
    *,
    run_filter: str = "",
    filter_params: list | None = None,
    step_note_prefix: str = "startup_sweep",
    step_origin: str = "prior process",
    log_event: str = "startup_sweep",
    steps_fail_event: str = "startup_sweep_steps_failed",
    fail_event: str = "startup_sweep_failed",
    per_row_log_event: str | None = None,
) -> int:
    """P0-3 提取的 sweep 收尾核心 —— startup / 运行时租约 sweep 共用。

    把当前连接可见的、满足 ``status='running'``（可叠加 *run_filter*）的
    agent_runs 归并为 ``interrupted``：孤儿 run_step 按 started 事实位分流、
    R11 零账占位补账（批 2 A7）。调用方必须**已持有 per-workspace 写锁**
    （``db/project.py`` 共享连接纪律，见 ``sweep_stale_agent_runs`` 的 I8
    注释 —— 锁内 SELECT→UPDATE 的 TOCTOU 一并关闭）。

    参数化点（I8「同状态不同来路，恢复 Trail 语义必须可分」的延伸）：
    - ``run_filter`` / ``filter_params``：叠加在 ``status='running'`` 之后
      的 WHERE 收窄（可含占位符，参数按序追加在各语句参数尾部）。startup
      不收窄（无条件全收）；运行时租约 sweep 收窄到「lease 已过期且不在
      豁免集」。
    - ``step_note_prefix`` / ``step_origin``：孤儿 step error 文案的来路段；
      正文（可执行的重试判据）保持 DSH 原文不动。
    - ``log_event`` / ``steps_fail_event`` / ``fail_event`` /
      ``per_row_log_event``：日志事件名（落日志时统一挂 ``run_ledger.``
      前缀）。``per_row_log_event`` 仅供运行时 sweep 逐行落 run_id /
      agent_id / 悬挂时长（启动 sweep 沿用聚合计数，行为不变）。
    """
    extra_where = f" AND {run_filter}" if run_filter else ""
    base_where = f"status = 'running'{extra_where}"
    fparams = list(filter_params or [])
    # 孤儿 step 文案：来路段参数化，正文照抄 DSH `repair.ts:106` 原文
    # （含 "Do not retry blindly."）—— agent 拿到的是可执行的重试判据。
    dead_step_text = (
        f"{step_note_prefix}: orphan step from {step_origin}. The tool call "
        "was interrupted while it was running, so it may have taken "
        "effect. Its outcome is unknown. Decide whether to retry from "
        "the tool semantics: retry only if the operation is read-only "
        "or idempotent; if it may have side effects, first verify "
        "external state. Do not retry blindly."
    )
    not_started_step_text = (
        f"{step_note_prefix}: orphan step from {step_origin}. The tool call "
        "was never dispatched, so it never executed and had no side "
        "effect. Retry it if it is still needed."
    )
    try:
        conn = await project_db.ensure_project_db(workspace_path)
        cursor = await conn.execute(
            "SELECT id, agent_id, actual_llm_calls, result_summary, "
            "started_at, lease_expires_at "
            f"FROM agent_runs WHERE {base_where}",
            fparams,
        )
        rows = await cursor.fetchall()
        await cursor.close()
        # L5（2026-09-11）：本 sweep 处理的是"上次进程死亡留下的孤儿步骤"。
        #
        # ⚠ 2026-09-12 修正（report TEST_DSH_54 #2）：原文假定这些步骤
        # "调用**从未开始**"——那在多进程下**不成立**。进程被杀时，正在
        # execute() 里的步骤同样是 running，而它可能已经产生了副作用
        # （写文件 / 发消息 / dispatch）。把这一类也标成 not_started 并附
        # "Retry it if it is still needed"，就是一次**副作用双发的邀请**。
        # 现在用同一个 started 事实位分流：
        #   started=0 → 从未派发 ⇒ 必然无副作用 ⇒ not_started（可直接重试）
        #   started=1 / NULL → 可能已执行 ⇒ outcome_unknown（先核实外部状态）
        try:
            # A3 事实位接线（批 3，2026-09-28）：`timeout_kind = 'swept'` ——
            # 与 create_activation 的孤儿清扫同语义（见该处长注释）：本清扫写的
            # 终态行此前在九个分类位上全 NULL，与真失败（record_step_end 的
            # status='failed'）靠 error 文案才能区分。直接 SET 不用 COALESCE 是
            # 安全的：timeout_kind 只有 record_step_end 会写，而它写位的同时
            # 必写终态 —— 一行还是 status='running' 就不可能已有值。
            await conn.execute(
                "UPDATE run_steps SET status = 'error', ended_at = ?, error = ?, "
                "outcome_unknown = 1, timeout_kind = 'swept' "
                f"WHERE run_id IN (SELECT id FROM agent_runs WHERE {base_where}) "
                "AND status = 'running' "
                "AND (started = 1 OR started IS NULL)",
                [now, dead_step_text, *fparams],
            )
            await conn.execute(
                "UPDATE run_steps SET status = 'error', ended_at = ?, error = ?, "
                "not_started = 1, timeout_kind = 'swept' "
                f"WHERE run_id IN (SELECT id FROM agent_runs WHERE {base_where}) "
                "AND status = 'running' "
                "AND started = 0",
                [now, not_started_step_text, *fparams],
            )
            await conn.commit()
        except Exception as e:
            log.warning(
                f"run_ledger.{steps_fail_event}",
                workspace=str(workspace_path), error=str(e),
            )
        cursor = await conn.execute(
            "UPDATE agent_runs SET status = 'interrupted', ended_at = ?, "
            "error_reason = ?, "
            "result_summary = COALESCE(NULLIF(result_summary, ''), ?) "
            f"WHERE {base_where}",
            [now, reason, reason, *fparams],
        )
        await cursor.close()
        # I8 (批7)：收尾 UPDATE 必须显式 commit —— 本函数共享连接（aiosqlite
        # 默认 deferred 事务），后续写者的 rollback（实例：`_ensure_fact_columns`
        # 的 duplicate-column 幂等路径在 `_execute_write_with_retry` 里
        # conn.rollback()）会把这条未提交的 interrupted 静默卷回 running。
        # 此前只 commit 了 run_steps 段，启动路径靠后续写者的 commit 侥幸落盘。
        # （审计 P1-2 后本函数已全程持 workspace 写锁，此处 commit 是锁内
        # 事务的正常收口。）
        await conn.commit()
        # P0-3：运行时租约 sweep 逐行落悬挂明细（run_id / agent_id / 悬挂
        # 时长），风格随 run_ledger 现有 orphan/interrupted 平铺字段日志。
        # overdue_ms = 距 lease 到期多久；hung_ms = 该 running 行悬挂总时长
        # （自 started_at 起）。startup sweep 不逐行（沿用聚合计数）。
        if per_row_log_event and rows:
            for r in rows:
                lease_ts = r["lease_expires_at"]
                started_ts = r["started_at"]
                log.info(
                    per_row_log_event,
                    workspace=str(workspace_path),
                    run_id=r["id"],
                    agent_id=r["agent_id"],
                    overdue_ms=(
                        max(0, now - int(lease_ts))
                        if lease_ts is not None else None
                    ),
                    hung_ms=(
                        max(0, now - int(started_ts))
                        if started_ts is not None else None
                    ),
                )
        # 批 2 A7：R11 占位补账。必须在拿到 swept 行明细后、仍持锁时用
        # **本连接**直写 —— 复用 ``_insert_placeholder_usage_for_dead_run``
        # 会经 ``project_db.execute`` 再取同一把 per-workspace 锁
        # （asyncio.Lock 不可重入 ⇒ 死锁），且启动期 AgentRouter 可能尚未
        # 重建（按 agent_id 路由不可靠）。口径与 dead-run 占位完全一致。
        await _insert_placeholder_usage_for_swept_runs(
            conn, rows, reason, now, workspace_path
        )
        if rows:
            log.info(
                f"run_ledger.{log_event}",
                workspace=str(workspace_path),
                primary_count=len(rows),
            )
        return len(rows)
    except Exception as e:
        log.warning(f"run_ledger.{fail_event}",
                    workspace=str(workspace_path), error=str(e))
        return 0


async def _insert_placeholder_usage_for_swept_runs(
    conn: Any,
    rows: list,
    reason: str,
    died_at_ms: int,
    workspace_path: str | None = None,
) -> None:
    """批 2 A7：sweep 收口的零账 run 补 R11 占位 usage 行（锁内、本连接直写）。

    口径对齐 :meth:`RunLedger._insert_placeholder_usage_for_dead_run`：
    - 仅 ``actual_llm_calls > 0``（declared>0 必须有账）且 ``llm_usage``
      零行的 run 才补 —— 已有账的 run 不动，declared=0 的不涉及；
    - 占位行机检位：``request_type IS NULL`` + ``provider IS NULL`` +
      全 0 token + ``duration_ms = 0``；run 侧 ``result_summary`` 前置
      ``[llm_usage_placeholder]`` 标记（前置防 500 字符截断丢位）。
      来路归因由 ``error_reason``（close_reason）承担，不新造标记词表。

    为什么不复用 dead-run 版本：它在锁外经 ``project_db.execute`` 走
    agent_id 路由（会再取本函数正持有的 per-workspace 写锁 ⇒ 死锁，
    且启动期 AgentRouter 未重建时按 agent_id 路由不可靠）。本函数由
    ``_sweep_stale_agent_runs_locked`` 在锁内用 sweep 自己的连接调用。

    best-effort：任何失败只 warning，绝不影响 sweep 主流程的返回值。
    """
    if not rows:
        return
    try:
        candidates = [
            r for r in rows if int(r["actual_llm_calls"] or 0) > 0
        ]
        if not candidates:
            return
        # model_id 按 agents 表现查（同库）；查不到就 NULL（与 dead-run 同）。
        model_ids: dict[str, Any] = {}
        for r in candidates:
            aid = r["agent_id"]
            if aid in model_ids:
                continue
            cur = await conn.execute(
                "SELECT model_id FROM agents WHERE id = ?", [aid]
            )
            mrow = await cur.fetchone()
            await cur.close()
            model_ids[aid] = mrow["model_id"] if mrow is not None else None
        # project_id：sweep 以 workspace 为键，meta 里反查一次即可
        # （llm_usage.project_id 可空，查不到就 NULL —— 与 dead-run 同口径）。
        project_id: str | None = None
        if workspace_path:
            try:
                from hiveweave.db import meta as meta_db

                prows = await meta_db.query(
                    "SELECT id FROM projects WHERE workspace_path = ?",
                    [workspace_path],
                )
                if prows:
                    project_id = prows[0]["id"]
            except Exception:  # noqa: BLE001 — 归属缺失不拦占位
                project_id = None
        marker = "[llm_usage_placeholder]"
        inserted = 0
        for r in candidates:
            run_id = r["id"]
            cur = await conn.execute(
                "SELECT COUNT(*) AS c FROM llm_usage WHERE run_id = ?",
                [run_id],
            )
            crow = await cur.fetchone()
            await cur.close()
            if crow is not None and int(crow["c"] or 0) > 0:
                continue  # 已有账 —— 占位只补「零账」断口
            prev_summary = str(r["result_summary"] or "") or reason
            if marker not in prev_summary:
                new_summary = f"{marker} | {prev_summary}"
                await conn.execute(
                    "UPDATE agent_runs SET result_summary = ? WHERE id = ?",
                    [new_summary[:500], run_id],
                )
            await conn.execute(
                "INSERT INTO llm_usage (id, agent_id, project_id, run_id, "
                "task_id, model_id, request_type, provider, input_tokens, "
                "output_tokens, cache_read_tokens, cache_creation_tokens, "
                "total_tokens, duration_ms, cold_start, creation_unreported, "
                "created_at) "
                "VALUES (?, ?, ?, ?, NULL, ?, NULL, NULL, 0, 0, 0, 0, 0, 0, 0, 0, ?)",
                [
                    str(uuid.uuid4()),
                    r["agent_id"],
                    project_id,
                    run_id,
                    model_ids.get(r["agent_id"]),
                    died_at_ms,
                ],
            )
            inserted += 1
        if inserted:
            await conn.commit()
            log.warning(
                "run_ledger.swept_run_placeholder_usage_inserted",
                count=inserted,
                reason=str(reason)[:80],
            )
    except Exception as e:  # noqa: BLE001 — 占位是补账，绝不拦 sweep 主流程
        log.warning("run_ledger.swept_run_placeholder_failed", error=str(e))
