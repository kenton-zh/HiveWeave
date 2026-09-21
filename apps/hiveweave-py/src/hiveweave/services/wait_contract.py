"""Wait Contract — persisted waiting_on from commit_turn (P0 Hard Gates Phase 2).

Active waits gate wake policy: waiting_human only wakes on matching events.
Default TTLs + clear_expired → WAIT_TIMEOUT; SCC cycle break for agent↔agent waits.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from collections.abc import Callable
from typing import Any

import aiosqlite
import structlog

from hiveweave.config import settings
from hiveweave.db import meta as meta_db
from hiveweave.db import project as project_db
from hiveweave.db.project import (
    ProjectDbError,
    ensure_project_db,
    get_workspace_write_lock,
)
from hiveweave.services.turn_result import WaitingOnItem

log = structlog.get_logger(__name__)

# ── Locked writes（per-workspace 写锁纪律，TEST18 审计 S1）─────────────
from hiveweave.db.project import execute_by_project, execute_transaction_by_project


def _item_kind_ref(item: Any) -> tuple[str, str]:
    if isinstance(item, WaitingOnItem):
        return str(item.kind or "external"), str(item.ref or "")
    if isinstance(item, dict):
        return str(item.get("kind") or "external"), str(item.get("ref") or "")
    return "external", ""


def _item_note(item: Any) -> str | None:
    if isinstance(item, WaitingOnItem):
        note = item.note
    elif isinstance(item, dict):
        note = item.get("note")
    else:
        return None
    return str(note) if note is not None else None


def _item_expires_at(item: Any) -> int | None:
    if isinstance(item, dict) and item.get("expires_at") is not None:
        try:
            return int(item["expires_at"])
        except (TypeError, ValueError):
            return None
    return None


def _dedup_waiting_on(waiting_on: list[Any]) -> list[Any]:
    """审计 #11：同 agent 同 kind+ref 的 waiting_on 去重。

    保留首条；expires_at 取组内最早（仅 dict 项可携带，若可比）；
    note 拼接去重（首条优先）。ref 为空的项原样透传（插入循环本就跳过）。
    """
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    passthrough: list[Any] = []
    for it in waiting_on or []:
        kind, ref = _item_kind_ref(it)
        key = (str(kind).lower(), str(ref).strip())
        if not key[1]:
            passthrough.append(it)
            continue
        if key not in groups:
            groups[key] = {
                "item": it,
                "expires_at": _item_expires_at(it),
                "notes": [n for n in [_item_note(it)] if n],
            }
            order.append(key)
            continue
        g = groups[key]
        exp = _item_expires_at(it)
        if exp is not None and (
            g["expires_at"] is None or exp < g["expires_at"]
        ):
            g["expires_at"] = exp
        note = _item_note(it)
        if note and note not in g["notes"]:
            g["notes"].append(note)
    out: list[Any] = list(passthrough)
    for key in order:
        g = groups[key]
        it = g["item"]
        note = " | ".join(g["notes"]) or None
        if g["expires_at"] is None and note == _item_note(it):
            out.append(it)
            continue
        if isinstance(it, WaitingOnItem):
            merged: dict[str, Any] = {
                "kind": it.kind,
                "ref": it.ref,
                "note": note,
            }
            if g["expires_at"] is not None:
                merged["expires_at"] = g["expires_at"]
            out.append(merged)
        elif isinstance(it, dict):
            it = dict(it)
            it["note"] = note
            if g["expires_at"] is not None:
                it["expires_at"] = g["expires_at"]
            out.append(it)
        else:
            out.append(it)
    return out


def looks_unbounded_external(kind: str, ref: str) -> bool:
    """Native bg job refs — no 30-minute wait TTL."""
    if str(kind or "").lower() != "external":
        return False
    r = (ref or "").strip()
    return r.startswith(("bg-bash-", "bg-sub-"))


async def _meeting_frozen_wait_ids(project_id: str, conn: Any) -> set[str]:
    """团队开会 hold 冻结：held agents 的全部 active wait ids。

    单一卡点：meetings.hold.held_agent_ids（规格 §hold「hold 期间不得
    clear_expired」）。fail-open — 导入/查询异常按无冻结处理。
    """
    try:
        from hiveweave.services.meetings.hold import held_agent_ids

        held = held_agent_ids(project_id)
        if not held:
            return set()
        placeholders = ",".join("?" for _ in held)
        cur = await conn.execute(
            "SELECT id FROM agent_waits "
            f"WHERE cleared_at IS NULL AND agent_id IN ({placeholders})",
            sorted(held),
        )
        rows = await cur.fetchall()
        await cur.close()
        return {str(r["id"]) for r in rows or []}
    except Exception:
        return set()


def _should_hold_live_offturn_wait(row: dict) -> bool:
    """True when this wait still names a live bg job."""
    kind = str(row.get("kind") or "").lower()
    ref = str(row.get("ref") or "")
    aid = str(row.get("agentId") or row.get("agent_id") or "")
    try:
        from hiveweave.services.offturn import (
            has_live_jobs_for_agent,
            is_live_job,
        )

        if kind == "external" and is_live_job(ref, agent_id=aid or None):
            return True
        if kind == "task" and aid and has_live_jobs_for_agent(aid):
            return True
    except Exception:
        pass
    return False


async def _execute_rowcount(
    project_id: str, sql: str, params: list[Any] | None = None
) -> int:
    """同纪律单语句写 + 返回 rowcount（execute_by_project 不返回 rowcount）。"""
    workspace = await meta_db.get_project_workspace(project_id)
    if not workspace:
        raise ProjectDbError(f"Workspace not found for project {project_id}")
    lock = await get_workspace_write_lock(workspace)
    async with lock:
        conn = await ensure_project_db(workspace)
        try:
            cur = await conn.execute(sql, params or [])
            n = cur.rowcount or 0
            await conn.commit()
            await cur.close()
            return n
        except Exception:
            try:
                await conn.rollback()
            except Exception:
                pass
            raise


_migrated: set[tuple[str, int]] = set()

# Default wake_on events by waiting kind
DEFAULT_WAKE_ON: dict[str, list[str]] = {
    "user": ["user_message", "task_transition", "timeout"],
    "agent": ["ask_reply", "message_from_ref", "timeout"],
    "task": ["task_transition", "timeout", "message_from_ref"],
    "timer": ["alarm", "timeout", "message_from_ref", "ask_reply", "user_message"],
    "external": [
        "external",
        "timeout",
        "message_from_ref",
        "ask_reply",
        "user_message",
    ],
    # L3 按事实唤醒（2026-09-08）：ref = "<fact_kind>[:<subject 子串>]"，
    # 事实发布时由 fact_bus 订阅方（game_time）匹配投递 [FACT_OBSERVED]。
    "fact": ["fact", "timeout", "message_from_ref"],
}

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS agent_waits (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    ref TEXT NOT NULL,
    wake_on TEXT NOT NULL DEFAULT '[]',
    expires_at INTEGER,
    obligation_version TEXT,
    phase TEXT,
    note TEXT,
    created_at INTEGER NOT NULL,
    cleared_at INTEGER
)
"""


# 39 审计 P1-1（确认洪水折叠去抖）：kind=agent 等待被同 ref 唤醒后该冷却窗内
# 的同 ref 消息只入 inbox 不再唤醒（折叠）——TTL 兜底钟保证等待方永不饿死。
WAIT_FOLD_COOLDOWN_MS = int(
    os.environ.get("HIVEWEAVE_WAIT_FOLD_COOLDOWN_MS", "300000") or "300000"
)
"""同 ref 消息折叠冷却窗（默认 5 分钟）：冷却窗内的后续消息只入 inbox，
等待方由 TTL 超时或冷却窗后的下一条消息唤醒。"""


def default_ttl_ms(kind: str, agent_id: str | None = None) -> int:
    """Default wait TTL. Agent waits get ±20% deterministic jitter (TEST11 #1d)."""
    k = (kind or "external").lower()
    if k == "user":
        return int(settings.wait_ttl_user_ms)
    if k == "task":
        return int(settings.wait_ttl_task_ms)
    if k == "timer":
        return int(settings.wait_ttl_timer_ms)
    if k == "agent":
        base = int(settings.wait_ttl_agent_ms)
        if agent_id:
            # Deterministic ±20% jitter by agent_id hash — desyncs simultaneous
            # TTL wakes so partners don't re-wait in lockstep.
            h = int(hashlib.md5(agent_id.encode()).hexdigest()[:8], 16)
            factor = 0.8 + (h % 401) / 1000.0  # [0.8, 1.2]
            return max(60_000, int(base * factor))
        return base
    return int(settings.wait_ttl_external_ms)


# ── timer wait 目标时刻语义（42 轮报告 P2-8）──────────────────────────
# 旧行为：commit_turn(waiting_on=[{kind:timer, ref:<目标时刻>}]) 的
# expires_at 一律 = created + wait_ttl_timer_ms(15min)。agent 设 4h 后的
# 目标（如 15:00Z）会在 15min 时被 [WAIT_TIMEOUT] 假装「目标已到」唤醒
# （实测 2 次虚假唤醒，提前 3.9h/12.6h）。新语义：
#   A. 目标 ≤ created+TTL → expires_at = 目标时刻（按目标排队）；
#   B. 目标 > created+TTL → 不再假装：expires_at 封顶 TTL，note 里打
#      wakeup_reason=ttl_cap 标记，唤醒文案明说目标未到、请续等或改用
#      schedule_alarm。
# 解析不了的 ref（quota_reset / alarm-<uuid> 等平台内部 timer）：P0-B
# （2026-09-13）起**也走退避阶梯**并标 wakeup_reason=ttl_expire。原先直接退基础
# TTL ⇒ 阶梯对它们完全不可达（TEST_DSH_55 实测：同一张 quota_reset 票每 15min
# 醒一次、连醒 19 次、19 个回合全部必败）。

_WAKEUP_REASON_TAG = "[wakeup_reason="
_TIME_ONLY_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?Z?$")


def parse_timer_target_ms(*values: Any) -> int | None:
    """Best-effort parse a timer wait target (epoch s/ms, ISO-8601, or HH:MM[Z]).

    语义与 tools.tasks.lifecycle._parse_wake_at_ms 对齐（epoch 秒自动升到
    毫秒、naive ISO 按 UTC）；额外支持纯时刻 ``HH:MM(:SS)(Z)``（agent 常写
    "15:00Z"）——取 UTC 今天该时刻，已过则取明天（下一个未来发生点）。
    全部失败返回 None（调用方退回默认 TTL）。
    """
    import datetime as _dt

    for value in values:
        if value is None:
            continue
        if isinstance(value, int):
            v = value
            if 0 < v < 10**11:
                v *= 1000
            return v if v > 0 else None
        text = str(value).strip()
        if not text:
            continue
        try:
            v = int(text)
            if 0 < v < 10**11:
                v *= 1000
            return v if v > 0 else None
        except ValueError:
            pass
        m = _TIME_ONLY_RE.match(text)
        if m:
            now_dt = _dt.datetime.now(_dt.timezone.utc)
            cand = now_dt.replace(
                hour=int(m.group(1)),
                minute=int(m.group(2)),
                second=int(m.group(3) or 0),
                microsecond=0,
            )
            if cand <= now_dt - _dt.timedelta(minutes=1):
                cand += _dt.timedelta(days=1)
            return int(cand.timestamp() * 1000)
        try:
            dt = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_dt.timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    return None


def _iso_utc(target_ms: int) -> str:
    import datetime as _dt

    return (
        _dt.datetime.fromtimestamp(target_ms / 1000, _dt.timezone.utc)
        .isoformat()
    )


def _mark_wait_note(note: str | None, reason: str, target_ms: int | None) -> str:
    """Append a machine-parseable wakeup_reason tag to a wait note.

    ``target_ms=None`` 用于「**无目标 ref** 的 TTL 到期」（P0-B，2026-09-13）：
    这类票（``quota_reset`` / ``alarm-<uuid>``）本就没有目标时刻可写，标记只带
    reason，供 :meth:`_timer_timeout_rounds` 计入退避轮次。
    """
    base = str(note or "").strip()
    if target_ms is None:
        tag = f"{_WAKEUP_REASON_TAG}{reason}]"
    else:
        tag = f"{_WAKEUP_REASON_TAG}{reason} target={_iso_utc(target_ms)}]"
    return f"{base} | {tag}" if base else tag


def wait_wakeup_reason(wait: dict) -> str | None:
    """Parse the ``[wakeup_reason=…]`` tag from a wait row's note (None 无标记)."""
    note = str(wait.get("note") or "")
    i = note.find(_WAKEUP_REASON_TAG)
    if i < 0:
        return None
    rest = note[i + len(_WAKEUP_REASON_TAG):]
    reason = rest.split("]", 1)[0].split(" ", 1)[0].strip()
    return reason or None


def wait_target_iso(wait: dict) -> str | None:
    """Parse the target timestamp from a wakeup_reason tag (ISO-8601 UTC)."""
    note = str(wait.get("note") or "")
    i = note.find("target=")
    if i < 0:
        return None
    rest = note[i + len("target="):]
    return rest.split("]", 1)[0].split(" ", 1)[0].strip() or None


# ── B 方案（2026-09-11）：timer 长挂账指数退避 ────────────────────────────
# 病：目标远超 TTL 的 timer 等待，每次 ttl_cap 唤醒后「原样续等」都重置成
# 15min —— 目标 7 天 ≈ 672 次零产出空转（实测 雾屿/a728cba5：15:44 挂账，
# 16:00 被 ttl_cap 唤醒，16:01 原样续等两轮，无任何产出）。
# 药：同一 (agent, timer, ref) **真正超时过**的轮次越多，续等 TTL 越长
# （默认 15min → 1h → 6h → 24h 饱和）。
#   - 只数「真超时」行（cleared_at >= expires_at）：被 replace_waits 提前
#     清掉的行不算，所以频繁重挂不会虚增档位。
#   - 退避**永不越过目标**：目标一旦落入退避窗就转按目标排队并标
#     target_reached，因此只会少醒、不会漏醒。
#   - 事件唤醒不受影响：timer 的 wake_on 仍含 message_from_ref / ask_reply /
#     user_message，退避只放大「始终没人理」时的兜底间隔。
WAIT_TIMER_BACKOFF_HISTORY_MS = 30 * 24 * 60 * 60 * 1000
# 退避窗上限：防止 env 配出超大乘数导致 now+eff_ttl 溢出 SQLite int64。
# 那条路径只捕 ProjectDbError，OverflowError 会让等待**静默不落库**。
WAIT_TIMER_BACKOFF_MAX_MS = 30 * 24 * 60 * 60 * 1000
_DEFAULT_TIMER_BACKOFF_MULTIPLIERS: tuple[int, ...] = (1, 4, 24, 96)


def _timer_backoff_multipliers() -> tuple[int, ...]:
    """解析 ``HIVEWEAVE_WAIT_TIMER_BACKOFF_MULTIPLIERS``（逗号分隔正整数）。"""
    raw = str(getattr(settings, "wait_timer_backoff_multipliers", "") or "")
    out: list[int] = []
    for part in raw.split(","):
        try:
            v = int(part.strip())
        except ValueError:
            continue
        if v > 0:
            out.append(v)
    return tuple(out) or _DEFAULT_TIMER_BACKOFF_MULTIPLIERS


def timer_backoff_ttl_ms(base_ttl_ms: int, level: int) -> int:
    """第 ``level`` 轮续等的 TTL（level=0 即基础 TTL）；末档饱和。

    结果 clamp 到 ``[base, WAIT_TIMER_BACKOFF_MAX_MS]``。
    """
    base = max(1, int(base_ttl_ms))
    muls = _timer_backoff_multipliers()
    idx = min(max(int(level), 0), len(muls) - 1)
    return max(base, min(base * muls[idx], WAIT_TIMER_BACKOFF_MAX_MS))


def _timer_rounds_key(ref: str) -> str:
    """退避档位的归一键：绝对时刻 ref 归一成目标 epoch ms。

    agent 重挂时 ref 可能变形（``Z`` vs ``+00:00``、精度不同），按原串计数会
    历史归零、退避失效 —— 归一成目标时刻即稳定。纯 ``HH:MM`` 这类**相对**
    时刻每次解析结果都变，按原串计（归一会恒不匹配）。
    """
    text = str(ref or "").strip()
    if not text:
        return ""
    if _TIME_ONLY_RE.match(text):
        return f"r:{text}"
    ms = parse_timer_target_ms(text)
    return f"t:{ms}" if ms is not None else f"r:{text}"


async def _conn(project_id: str) -> aiosqlite.Connection:
    return await project_db.get_project_db_by_project_id(project_id)


# kind=task 等待的"已满足"终态集合：任务一旦处于这些状态，任何
# task_transition 唤醒事件都不会再来（没有后继转换），等待即僵尸。
_TASK_WAIT_SATISFIED_STATUSES = frozenset({"approved", "closed", "cancelled"})


async def _short_circuit_satisfied_task_waits(
    project_id: str, task_waits: list[dict]
) -> int:
    """新建的 kind=task 等待若引用的任务已处于终态 → 当场清等待并唤醒。

    07 实测：M3/M4/M5 早已 approved，凛川 20:34 才挂 task_transition 等待
    ——事件在等待创建前就已发生，唤醒永远不会触发，只能等 TTL 超时
    （僵尸 4.5h）。此函数在 wait 创建后立即核对任务状态，已终态则：
    清除等待行 + trigger_subordinate 唤醒 agent（与任务转换唤醒同路径）。
    """
    if not task_waits:
        return 0
    await _ensure_schema(project_id)
    conn = await _conn(project_id)
    if conn is None:
        return 0
    from hiveweave.agents.trigger import trigger_subordinate
    # P1-3 ①：`blocked` **不进** `_TASK_WAIT_SATISFIED_STATUSES`（它有活出口：
    # `_TRANSITIONS` 允许 blocked → running/closed，`reconcile_blocked_tasks` 会解封
    # 并续走 ⇒ `task_transition` 会来）。把 blocked 塞进那个集合会让等待/唤醒**空转**。
    # 但「**没有**自动解封路径的 blocked」是另一回事 —— 那正是僵尸：
    # `lifecycle.blocked_task_has_wake_path`（纯结构化字段：deps 非空 或 timer+wake_at）
    # 早已实现这条判据，此前**只被义务面消费**（`services/obligation.py`），
    # wait 侧不读 ⇒ 这类等待只能干等到 TTL。
    from hiveweave.services.tasks.lifecycle import blocked_task_has_wake_path

    now = int(time.time() * 1000)
    woken = 0
    for w in task_waits:
        ref = str(w.get("ref") or "")
        if not ref:
            continue
        try:
            cur = await conn.execute(
                "SELECT status, depends_on, wait_kind, wake_at FROM tasks "
                "WHERE id = ?",
                [ref],
            )
            row = await cur.fetchone()
            await cur.close()
        except Exception:  # noqa: BLE001 — 查不到按未满足处理
            continue
        status = (row[0] if row else "") or ""
        zombie_blocked = False
        if row is not None and status == "blocked":
            zombie_blocked = not blocked_task_has_wake_path(
                {
                    "depends_on": row[1],
                    "wait_kind": row[2],
                    "wake_at": row[3],
                }
            )
        if status not in _TASK_WAIT_SATISFIED_STATUSES and not zombie_blocked:
            continue
        # 审计[1]：UPDATE rowcount 守卫——与任务转换路径并发时，转换可能已清
        # 同一行并唤醒过；0 行 = 这次短路没有"清"到任何东西，不重复唤醒。
        cur = await conn.execute(
            "UPDATE agent_waits SET cleared_at = ? "
            "WHERE id = ? AND cleared_at IS NULL",
            [now, w["id"]],
        )
        cleared = cur.rowcount
        await conn.commit()
        if not cleared:
            continue
        woken += 1
        log.info(
            "task_wait_short_circuited",
            agent_id=w.get("agentId"),
            task_ref=ref,
            task_status=status,
            # P1-3 ①：标明是哪一支命中（终态 / blocked 且无自动解封路径）
            reason=("blocked_no_wake_path" if zombie_blocked else "terminal_status"),
        )
        try:
            await trigger_subordinate(str(w.get("agentId") or ""))
        except Exception as e:  # noqa: BLE001 — 唤醒失败退化为 TTL
            log.warning(
                "task_wait_short_circuit_trigger_failed",
                agent_id=w.get("agentId"),
                error=str(e),
            )
    return woken


async def _ensure_schema(project_id: str) -> None:
    """建 agent_waits 表 + 索引。

    标记键 = ``(workspace, 连接世代)``（机制见
    :func:`db.project.schema_marker_key_for_project`）：按 project_id 记忆的
    旧标记在库整代重建后会继续命中，`CREATE TABLE` 被跳过 → 下游
    `no such table: agent_waits`（与 inbox 的 TEST_DSH_52_A 同形）。
    """
    key = await project_db.schema_marker_key_for_project(project_id)
    if key in _migrated:
        return
    try:
        await execute_by_project(project_id, CREATE_SQL)
    except ProjectDbError:
        return
    try:
        await execute_by_project(
            project_id,
            "CREATE INDEX IF NOT EXISTS idx_agent_waits_agent "
            "ON agent_waits(agent_id, cleared_at)",
        )
    except Exception as exc:
        # 具名：索引缺失只影响查询性能，不影响等待的正确性（DSH AGENTS.md:122）
        log.debug("wait_contract_index_create_failed", error=str(exc))
    _migrated.add(key)


def obligation_version(obligations: list[dict]) -> str:
    parts = sorted(
        f"{t.get('id')}:{t.get('status')}" for t in (obligations or [])
    )
    raw = "|".join(parts) or "empty"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _row_to_dict(row) -> dict[str, Any]:
    d = dict(row) if not isinstance(row, dict) else row
    wake_raw = d.get("wake_on") or "[]"
    try:
        wake_on = json.loads(wake_raw) if isinstance(wake_raw, str) else list(wake_raw)
    except Exception:
        wake_on = []
    return {
        "id": d["id"],
        "agentId": d["agent_id"],
        "projectId": d["project_id"],
        "kind": d["kind"],
        "ref": d["ref"],
        "wakeOn": wake_on,
        "expiresAt": d.get("expires_at"),
        "obligationVersion": d.get("obligation_version"),
        "phase": d.get("phase"),
        "note": d.get("note"),
        "createdAt": d.get("created_at"),
        "clearedAt": d.get("cleared_at"),
    }


def _scc(graph: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan SCC. Returns components with size >= 1."""
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    result: list[list[str]] = []

    def strongconnect(v: str) -> None:
        nonlocal index
        indices[v] = index
        lowlink[v] = index
        index += 1
        stack.append(v)
        on_stack.add(v)
        for w in graph.get(v, ()):
            if w not in indices:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], indices[w])
        if lowlink[v] == indices[v]:
            comp: list[str] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            result.append(comp)

    nodes = set(graph.keys())
    for outs in graph.values():
        nodes |= outs
    for v in nodes:
        if v not in indices:
            strongconnect(v)
    return result


class WaitContractService:
    """CRUD for active agent wait contracts."""

    async def _timer_timeout_rounds(
        self, project_id: str, agent_id: str, now: int
    ) -> dict[str, int]:
        """该 agent 各 timer 目标**真正超时过**的轮次（``_timer_rounds_key`` → n，近 30 天）。

        供 B 方案指数退避取档。判据 ``cleared_at >= expires_at``：只有
        ``clear_expired`` 到点清的行算数；被 ``replace_waits`` 提前清掉的
        （agent 主动重挂 / 事件唤醒后重挂）不算 —— 否则快速连挂会虚增档位。
        分组在 Python 侧做（ref 需归一化，SQL 表达不了）。查询失败返回空
        dict（退基础 TTL，不拦主流程）。

        ⚠ **已知残留**（审计 P0-B 第 1 条，2026-09-13，**书面接受、本轮不修**）：
        ``r:`` 类键（解析不出目标的 ref，如 quota_reset）在 30 天窗口内**跨冻结
        世代继承** —— 同一 agent 若 30 天内被第二次冻结，新冻结的**首票**会直接
        沿用上次累计的档位（最坏 ×96 = 24h），而额度恢复 / 换 key **没有解除口**
        （``clear_balance_exhausted`` 只认 402）。触发条件罕见（需 30 天内两次
        冻结）；断链归零或补出口需要先建「429 额度恢复」入口，属独立议题。
        本轮收益已达成：「每次 15min、无限次」→「15m→1h→6h→24h 饱和」。
        """
        conn = await _conn(project_id)
        if conn is None:
            return {}
        cur = await conn.execute(
            "SELECT ref FROM agent_waits "
            "WHERE agent_id = ? AND kind = 'timer' "
            "AND cleared_at IS NOT NULL AND cleared_at >= ? "
            "AND expires_at IS NOT NULL AND cleared_at >= expires_at "
            "AND (note LIKE ? OR note LIKE ?)",
            [
                agent_id,
                now - WAIT_TIMER_BACKOFF_HISTORY_MS,
                # P0-B（2026-09-13）：容纳**两类**「TTL 到期」标记 ——
                # ttl_cap（有可解析目标但被封顶）与 ttl_expire（无目标 ref）。
                # 原先只认 ttl_cap ⇒ quota_reset / alarm-<uuid> 这类票永远数不到
                # 轮次 ⇒ 退避阶梯对它们不可达（19 次复读的组织侧成因）。
                # ⚠ 必须给**精确字面量**，不能用 `ttl_` 前缀：LIKE 的 `_` 是
                # 单字符通配符，`%wakeup_reason=ttl_%` 会连 `ttlXboom` 一起命中
                # （审计 P0-B 第 2 条）。
                f"%{_WAKEUP_REASON_TAG}ttl_cap target=%",
                f"%{_WAKEUP_REASON_TAG}ttl_expire]",
            ],
        )
        rows = await cur.fetchall()
        await cur.close()
        counts: dict[str, int] = {}
        for row in rows:
            key = _timer_rounds_key(str((row and row[0]) or ""))
            if key:
                counts[key] = counts.get(key, 0) + 1
        return counts

    async def replace_waits(
        self,
        project_id: str,
        agent_id: str,
        waiting_on: list[WaitingOnItem] | list[dict],
        *,
        phase: str,
        obligations: list[dict] | None = None,
        expires_at: int | None = None,
    ) -> list[dict]:
        """Clear previous active waits and insert new ones from waiting_on."""
        await _ensure_schema(project_id)

        now = int(time.time() * 1000)
        # 全有或全无：先收集语句列表（纯计算、无 await），再一次 BEGIN
        # IMMEDIATE 事务提交 — 循环内 await 会暴露"旧 wait 已清除、新 wait
        # 半插入"的窗口（TEST18 审计 S1）。
        new_external_refs: set[str] = set()
        for it in waiting_on or []:
            kind, ref = _item_kind_ref(it)
            if str(kind).lower() == "external" and ref:
                new_external_refs.add(ref)
        preserved: list[str] = []
        live_lookup_ok = False
        try:
            from hiveweave.services.offturn import live_job_ids_for_agent

            preserved = [
                jid
                for jid in live_job_ids_for_agent(agent_id)
                if jid not in new_external_refs
            ]
            live_lookup_ok = True
        except Exception:
            live_lookup_ok = False
        if not live_lookup_ok:
            # Cannot prove which external waits are dead — do not wipe them.
            statements: list[tuple[str, list[Any] | None]] = [
                (
                    "UPDATE agent_waits SET cleared_at = ? "
                    "WHERE agent_id = ? AND cleared_at IS NULL "
                    "AND kind != 'external'",
                    [now, agent_id],
                )
            ]
        elif preserved:
            placeholders = ",".join("?" * len(preserved))
            statements = [
                (
                    "UPDATE agent_waits SET cleared_at = ? "
                    "WHERE agent_id = ? AND cleared_at IS NULL "
                    f"AND NOT (kind = 'external' AND ref IN ({placeholders}))",
                    [now, agent_id, *preserved],
                )
            ]
        else:
            statements = [
                (
                    "UPDATE agent_waits SET cleared_at = ? "
                    "WHERE agent_id = ? AND cleared_at IS NULL",
                    [now, agent_id],
                )
            ]

        ver = obligation_version(obligations or [])
        created: list[dict] = []
        deduped_items = _dedup_waiting_on(list(waiting_on or []))
        batch_unbounded = any(
            looks_unbounded_external(*_item_kind_ref(it))
            for it in deduped_items
        )
        # B 退避档位：循环内不能 await（见上方「全有或全无」注释），故先
        # 一次查完该 agent 各 timer ref 的真超时轮次。
        timer_timeout_rounds: dict[str, int] = {}
        if any(
            str(_item_kind_ref(it)[0]).lower() == "timer" for it in deduped_items
        ):
            try:
                timer_timeout_rounds = await self._timer_timeout_rounds(
                    project_id, agent_id, now
                )
            except Exception:  # noqa: BLE001 — 退避是优化，查不到退基础 TTL
                timer_timeout_rounds = {}
        for item in deduped_items:
            # wkind/wref/wnote:加前缀避免与上方 _item_kind_ref 解包的 kind/ref
            # 遮蔽(mypy no-redef)。
            if isinstance(item, WaitingOnItem):
                wkind: str = item.kind
                wref = item.ref
                wnote = item.note
            else:
                wkind = str(item.get("kind") or "external")
                wref = str(item.get("ref") or "")
                wnote = item.get("note")
            if not wref:
                continue
            wake_on = list(DEFAULT_WAKE_ON.get(wkind, ["timeout"]))
            if isinstance(item, dict) and item.get("wake_on"):
                wake_on = list(item["wake_on"])
            wid = str(uuid.uuid4())
            exp = expires_at
            if isinstance(item, dict) and item.get("expires_at") is not None:
                exp = int(item["expires_at"])
            unbounded = looks_unbounded_external(wkind, wref) or (
                str(wkind).lower() == "task" and batch_unbounded
            )
            if unbounded:
                exp = None
                wake_on = [
                    w for w in wake_on if str(w).lower() != "timeout"
                ]
                if not wake_on:
                    wake_on = (
                        ["external"]
                        if str(wkind).lower() == "external"
                        else ["task_transition"]
                    )
            elif exp is None:
                ttl_ms = default_ttl_ms(wkind, agent_id)
                target_ms = None
                if str(wkind).lower() == "timer":
                    candidate = parse_timer_target_ms(wref, wnote)
                    # P2-1（审计）：目标必须晚于本条 wait 的创建时刻才采信。
                    # parse 对纯数字按 epoch —— ref 解析失败后回退 note 时，
                    # note="30" 会解析成 1970（已过时刻）→ 立即假唤醒还标
                    # target_reached（恰是要修的病）。过去时刻不采，退旧 TTL。
                    if candidate is not None and candidate > now:
                        target_ms = candidate
                if target_ms is None:
                    # P0-B（2026-09-13 TEST_DSH_55 实证）：无目标 ref
                    # （quota_reset / alarm-<uuid> 等平台内部 timer）过去**直接退
                    # 基础 TTL** —— 而退避阶梯只在「有可解析目标」分支求值 ⇒ 对
                    # quota_reset 完全不可达：同一张票每 15min 醒一次、连醒 19 次
                    # 全部必败（额度冻结窗口 8 天）。现接回阶梯：无目标也退避
                    # （15min→1h→6h→24h，末档饱和），并打 ttl_expire 供计数。
                    # ⚠ level=0 的乘数是 ×1 ⇒ 纯基础 TTL，**首票行为不变**。
                    # ⚠ 必须限定 kind=="timer"：`target_ms is None` 对
                    # agent/task/external 同样成立，而退避阶梯与 ttl_* 标记都是
                    # **timer 专有**语义 —— 越界会污染非 timer 票的 note 与参数
                    # 类型（test_locked_writers_part2 用不可绑定 note 构造中途失败
                    # 时抓到的正是这一点）。
                    if str(wkind).lower() == "timer":
                        eff_ttl = timer_backoff_ttl_ms(
                            ttl_ms,
                            timer_timeout_rounds.get(_timer_rounds_key(wref), 0),
                        )
                        exp = now + eff_ttl
                        wnote = _mark_wait_note(wnote, "ttl_expire", None)
                    else:
                        exp = now + ttl_ms
                else:
                    # P2-8：timer 有可解析目标时刻 —— ≤TTL 按目标排队；
                    # >TTL 封顶 TTL 并打 ttl_cap 标记（唤醒文案区分，
                    # 见 game_time._process_wait_contracts）。
                    # B 方案：封顶额度按「真超时轮次」指数退避
                    # （15min→1h→6h→24h）。退避窗够到目标即转按目标排队，
                    # 故只会少醒、不会漏醒。
                    eff_ttl = timer_backoff_ttl_ms(
                        ttl_ms, timer_timeout_rounds.get(_timer_rounds_key(wref), 0)
                    )
                    if target_ms <= now + eff_ttl:
                        exp = target_ms
                        wnote = _mark_wait_note(wnote, "target_reached", target_ms)
                    else:
                        exp = now + eff_ttl
                        wnote = _mark_wait_note(wnote, "ttl_cap", target_ms)
            statements.append(
                (
                    "INSERT INTO agent_waits "
                    "(id, agent_id, project_id, kind, ref, wake_on, expires_at, "
                    "obligation_version, phase, note, created_at, cleared_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                    [
                        wid,
                        agent_id,
                        project_id,
                        wkind,
                        wref,
                        json.dumps(wake_on),
                        exp,
                        ver,
                        phase,
                        wnote,
                        now,
                    ],
                )
            )
            created.append(
                {
                    "id": wid,
                    "agentId": agent_id,
                    "projectId": project_id,
                    "kind": wkind,
                    "ref": wref,
                    "wakeOn": wake_on,
                    "expiresAt": exp,
                    "obligationVersion": ver,
                    "phase": phase,
                    "note": wnote,
                    "createdAt": now,
                    "clearedAt": None,
                }
            )

        try:
            await execute_transaction_by_project(project_id, statements)
        except ProjectDbError:
            return []
        log.info(
            "wait_contracts_replaced",
            agent_id=agent_id,
            count=len(created),
            phase=phase,
            obligation_version=ver,
        )
        # 事实短路（07 报告 #5）：kind=task 的等待若引用的任务**已经**处于
        # 终态，唤醒事件永远不会再来——当场清等待并唤醒，避免僵尸等待只能
        # 靠 TTL 超时（07 实测：M3/M4/M5 已 approved，凛川仍挂等待 4.5h）。
        task_waits = [w for w in created if w["kind"] == "task"]
        if task_waits:
            try:
                await _short_circuit_satisfied_task_waits(project_id, task_waits)
            except Exception as e:  # noqa: BLE001 — 短路失败退化为 TTL 超时
                log.warning(
                    "task_wait_short_circuit_failed",
                    agent_id=agent_id,
                    error=str(e),
                )
        return created

    async def has_recent_cleared_agent_wait(
        self,
        project_id: str,
        agent_id: str,
        sender_refs: list[str],
        *,
        within_ms: int | None = None,
    ) -> bool:
        """39 审计 P1-1（确认洪水折叠去抖）：同 ref 的 kind=agent 等待在冷却窗
        内被清除过 → True（本次同 ref 消息应折叠：只入 inbox 不再唤醒）。

        ``sender_refs`` 为发送者身份变体（UUID/花名/short_id）——等待行的
        ref 存的是 commit_turn 时的原始写法（UUID 或花名都可能），审计[应修]
        要求按变体集合匹配，否则 ref=花名 的行永远查不到（假阴性）。
        best-effort：查询失败返回 False（退化为每次都唤醒——旧行为）。
        """
        await _ensure_schema(project_id)
        conn = await _conn(project_id)
        if conn is None:
            return False
        cooldown = (
            within_ms if within_ms is not None else WAIT_FOLD_COOLDOWN_MS
        )
        now = int(time.time() * 1000)
        # 身份变体集合：原始写法 + 解析后的完整 UUID + 8 位前缀
        cands: list[str] = []
        for tok in sender_refs or []:
            tok = str(tok or "").strip()
            if not tok:
                continue
            cands.append(tok)
            try:
                from hiveweave.services.org import OrgService

                resolved = await OrgService().resolve_agent(project_id, tok)
                rid = str((resolved or {}).get("id") or "")
                if rid:
                    cands.append(rid)
                    if len(rid) >= 8:
                        cands.append(rid[:8])
            except Exception:  # noqa: BLE001 — 解析失败用原始 token
                cands.append(tok)
        uniq = [x for i, x in enumerate(cands) if x and x not in cands[:i]]
        if not uniq:
            return False
        placeholders = ",".join("?" * len(uniq))
        try:
            cur = await conn.execute(
                f"SELECT COUNT(*) FROM agent_waits "
                f"WHERE agent_id = ? AND kind = 'agent' AND ref IN ({placeholders}) "
                f"AND cleared_at IS NOT NULL AND cleared_at >= ?",
                [agent_id, *uniq, now - cooldown],
            )
            row = await cur.fetchone()
            await cur.close()
            return bool(row and row[0])
        except Exception:  # noqa: BLE001 — 退化旧行为
            return False

    async def clear_waits(self, project_id: str, agent_id: str) -> int:
        await _ensure_schema(project_id)
        now = int(time.time() * 1000)
        try:
            return await _execute_rowcount(
                project_id,
                "UPDATE agent_waits SET cleared_at = ? "
                "WHERE agent_id = ? AND cleared_at IS NULL",
                [now, agent_id],
            )
        except ProjectDbError:
            return 0

    async def clear_waits_matching_ref(
        self, project_id: str, agent_id: str, ref: str
    ) -> int:
        """Clear active waits whose ref matches an off-turn / external job id.

        Sibling jobs keep their waits. Empty ref is a no-op.
        """
        needle = (ref or "").strip()
        if not needle:
            return 0
        await _ensure_schema(project_id)
        now = int(time.time() * 1000)
        try:
            return await _execute_rowcount(
                project_id,
                "UPDATE agent_waits SET cleared_at = ? "
                "WHERE agent_id = ? AND cleared_at IS NULL AND ref = ?",
                [now, agent_id, needle],
            )
        except ProjectDbError:
            return 0

    async def clear_kind_agent_waits_for_sender(
        self,
        project_id: str,
        waiter_agent_id: str,
        sender_agent_id: str,
    ) -> int:
        """Clear active kind=agent waits whose ref resolves to sender.

        Does not clear kind=external / task / timer / bash-job waits.
        Match uses the same identity resolution as event_matches_waits.
        """
        sender = (sender_agent_id or "").strip()
        if not sender:
            return 0
        await _ensure_schema(project_id)
        waits = await self.list_active(project_id, waiter_agent_id)
        matched = await matching_kind_agent_waits(
            project_id,
            waits,
            from_agent_id=sender,
        )
        ids = [str(w.get("id") or "") for w in matched if w.get("id")]
        if not ids:
            return 0
        now = int(time.time() * 1000)
        placeholders = ",".join("?" * len(ids))
        try:
            return await _execute_rowcount(
                project_id,
                f"UPDATE agent_waits SET cleared_at = ? "
                f"WHERE id IN ({placeholders}) AND agent_id = ? "
                f"AND cleared_at IS NULL AND kind = 'agent'",
                [now, *ids, waiter_agent_id],
            )
        except ProjectDbError:
            return 0

    async def list_active(self, project_id: str, agent_id: str) -> list[dict]:
        await _ensure_schema(project_id)
        conn = await _conn(project_id)
        if conn is None:
            return []
        cur = await conn.execute(
            "SELECT * FROM agent_waits "
            "WHERE agent_id = ? AND cleared_at IS NULL "
            "ORDER BY created_at DESC",
            [agent_id],
        )
        rows = await cur.fetchall()
        await cur.close()
        return [_row_to_dict(r) for r in rows]

    async def list_all_active(self, project_id: str) -> list[dict]:
        await _ensure_schema(project_id)
        conn = await _conn(project_id)
        if conn is None:
            return []
        cur = await conn.execute(
            "SELECT * FROM agent_waits WHERE cleared_at IS NULL "
            "ORDER BY created_at DESC"
        )
        rows = await cur.fetchall()
        await cur.close()
        return [_row_to_dict(r) for r in rows]

    async def backfill_null_expires(self, project_id: str) -> int:
        """Assign default TTL to legacy rows with NULL expires_at."""
        await _ensure_schema(project_id)
        conn = await _conn(project_id)
        if conn is None:
            return 0
        cur = await conn.execute(
            "SELECT id, kind, ref, agent_id, created_at FROM agent_waits "
            "WHERE cleared_at IS NULL AND expires_at IS NULL"
        )
        rows = await cur.fetchall()
        await cur.close()
        # 会议 hold 冻结：held agents 的 NULL-expiry waits 不在 hold 期间
        # 重新武装 TTL（clear 侧同样冻结；解 hold 时统一补时）。
        try:
            from hiveweave.services.meetings.hold import held_agent_ids as _held

            frozen_agents = _held(project_id)
        except Exception:
            frozen_agents = set()
        now = int(time.time() * 1000)
        statements: list[tuple[str, list[Any] | None]] = []
        n = 0
        for r in rows:
            if frozen_agents and str(r["agent_id"] or "") in frozen_agents:
                continue
            kind = str(r["kind"] or "external")
            ref = str(r["ref"] or "")
            aid = str(r["agent_id"] or "")
            if looks_unbounded_external(kind, ref):
                continue
            if str(kind).lower() == "task" and _should_hold_live_offturn_wait(
                {"kind": kind, "ref": ref, "agentId": aid}
            ):
                continue
            ttl = default_ttl_ms(kind)
            # Fresh TTL from *now* — created_at + ttl would instantly expire
            # companion task waits after a long off-turn job.
            exp = now + ttl
            statements.append(
                (
                    "UPDATE agent_waits SET expires_at = ? WHERE id = ?",
                    [exp, r["id"]],
                )
            )
            n += 1
        if n:
            await execute_transaction_by_project(project_id, statements)
        return n

    async def clear_expired(
        self, project_id: str, agent_id: str | None = None
    ) -> list[dict]:
        """Clear expired waits; return the wait dicts that were cleared.

        团队开会 hold（docs/spec/team-meeting.md）：held agents 的 wait
        冻结——clear 跳过（解 hold 时由 meetings.hold 统一补时）。
        fail-open：过滤层异常不改变既有行为。
        """
        await _ensure_schema(project_id)
        conn = await _conn(project_id)
        if conn is None:
            return []
        now = int(time.time() * 1000)
        frozen_ids = await _meeting_frozen_wait_ids(project_id, conn)
        if agent_id:
            cur = await conn.execute(
                "SELECT * FROM agent_waits "
                "WHERE agent_id = ? AND cleared_at IS NULL "
                "AND expires_at IS NOT NULL AND expires_at <= ?",
                [agent_id, now],
            )
        else:
            cur = await conn.execute(
                "SELECT * FROM agent_waits "
                "WHERE cleared_at IS NULL "
                "AND expires_at IS NOT NULL AND expires_at <= ?",
                [now],
            )
        rows = await cur.fetchall()
        await cur.close()
        candidates = [_row_to_dict(r) for r in rows]
        if agent_id:
            cur2 = await conn.execute(
                "SELECT * FROM agent_waits "
                "WHERE agent_id = ? AND cleared_at IS NULL "
                "AND expires_at IS NULL",
                [agent_id],
            )
        else:
            cur2 = await conn.execute(
                "SELECT * FROM agent_waits "
                "WHERE cleared_at IS NULL AND expires_at IS NULL"
            )
        null_rows = await cur2.fetchall()
        await cur2.close()
        for r in null_rows:
            d = _row_to_dict(r)
            kind = str(d.get("kind") or "")
            ref = str(d.get("ref") or "")
            if looks_unbounded_external(kind, ref):
                candidates.append(d)
        cleared: list[dict] = []
        seen: set[str] = set()
        for c in candidates:
            cid = str(c.get("id") or "")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            if _should_hold_live_offturn_wait(c):
                continue
            # 会议 hold 冻结：held agent 的 waits 不 clear（开会不计入等待）
            if cid in frozen_ids:
                continue
            cleared.append(c)
        if not cleared:
            return []
        ids = [c["id"] for c in cleared]
        placeholders = ",".join("?" * len(ids))
        await execute_by_project(
            project_id,
            f"UPDATE agent_waits SET cleared_at = ? "
            f"WHERE id IN ({placeholders})",
            [now, *ids],
        )
        return cleared

    async def break_wait_cycles(
        self,
        project_id: str,
        resolve_agent_id: Callable[[str], str | None],
        *,
        parent_map: dict[str, str] | None = None,
    ) -> list[dict]:
        """Detect wait SCCs (agent↔agent and task-mediated) and clear ALL members.

        ``resolve_agent_id(ref)`` maps wait.ref (花名/short_id/uuid) → agent_id.
        ``parent_map`` (agent_id → parent_id) lets the caller declare the org
        hierarchy. Edges between a superior and its subordinate are NOT deadlock
        cycles — the superior can adjudicate the subordinate, so mutual waits up
        and down one chain are lawful task dependencies, not a stuck cycle.

        TEST3: previously only cleared ``min(agent_id)``; partners stayed stuck
        until TTL. Now clear every agent in the component and return one break
        record per cycle (``memberIds`` lists everyone to notify).
        """
        active = await self.list_all_active(project_id)
        graph: dict[str, set[str]] = {}

        def _is_hierarchy(a: str, b: str) -> bool:
            """True if a and b are in a direct ancestor/descendant relation."""
            if not parent_map:
                return False

            def _is_ancestor(anc: str, desc: str) -> bool:
                cur = parent_map.get(desc)
                seen: set[str] = set()
                while cur and cur not in seen:
                    if cur == anc:
                        return True
                    seen.add(cur)
                    cur = parent_map.get(cur)
                return False

            return _is_ancestor(a, b) or _is_ancestor(b, a)

        # agent → agent edges
        for w in active:
            if (w.get("kind") or "").lower() != "agent":
                continue
            waiter = w.get("agentId") or ""
            target = resolve_agent_id(str(w.get("ref") or ""))
            if not waiter or not target or waiter == target:
                continue
            if _is_hierarchy(waiter, target):
                continue
            graph.setdefault(waiter, set()).add(target)
            graph.setdefault(target, set())

        # task → assignee/creator edges (peer-review mutual wait via kind=task)
        task_parties = await self._task_party_map(project_id)
        for w in active:
            if (w.get("kind") or "").lower() != "task":
                continue
            waiter = w.get("agentId") or ""
            ref = str(w.get("ref") or "").strip()
            if not waiter or not ref:
                continue
            parties = task_parties.get(ref.lower()) or task_parties.get(ref[:8].lower())
            if not parties:
                continue
            for other in parties:
                if other == waiter or (other and _is_hierarchy(waiter, other)):
                    continue
                if other:
                    graph.setdefault(waiter, set()).add(other)
                    graph.setdefault(other, set())

        breaks: list[dict] = []
        for comp in _scc(graph):
            if len(comp) < 2:
                continue
            members = sorted(comp)
            # TEST11 #1b: pick earliest waiter (by wait created_at) to wake first
            earliest_by_member: dict[str, int] = {}
            for w in active:
                aid = w.get("agentId") or ""
                if aid not in members:
                    continue
                created = int(w.get("createdAt") or 0)
                prev = earliest_by_member.get(aid)
                if prev is None or created < prev:
                    earliest_by_member[aid] = created
            wake_first = min(
                members,
                key=lambda m: (earliest_by_member.get(m, 0), m),
            )
            now = int(time.time() * 1000)
            placeholders = ",".join("?" * len(members))
            try:
                n = await _execute_rowcount(
                    project_id,
                    f"UPDATE agent_waits SET cleared_at = ? "
                    f"WHERE agent_id IN ({placeholders}) AND cleared_at IS NULL",
                    [now, *members],
                )
            except ProjectDbError:
                continue
            if n:
                breaks.append(
                    {
                        "breakerId": wake_first,  # asymmetric wake primary
                        "wakeFirstId": wake_first,
                        "memberIds": members,
                        "cycle": members,
                        "clearedCount": n,
                    }
                )
                log.info(
                    "wait_cycle_broken",
                    project_id=project_id,
                    members=members,
                    cycle=members,
                    wake_first=wake_first,
                    cleared=n,
                )
        return breaks

    async def _task_party_map(
        self, project_id: str
    ) -> dict[str, set[str]]:
        """Map task id / 8-char prefix → {assignee_id, creator_id}."""
        conn = await _conn(project_id)
        if conn is None:
            return {}
        out: dict[str, set[str]] = {}
        try:
            cur = await conn.execute(
                "SELECT id, assignee_id, creator_id FROM tasks "
                "WHERE COALESCE(is_archived, 0) = 0 "
                "AND status NOT IN ('closed', 'cancelled', 'archived')"
            )
            rows = await cur.fetchall()
            await cur.close()
            for r in rows:
                tid = (r["id"] or "").strip()
                if not tid:
                    continue
                parties = {
                    p for p in (r["assignee_id"], r["creator_id"]) if p
                }
                if not parties:
                    continue
                out[tid.lower()] = parties
                out[tid[:8].lower()] = parties
        except Exception as e:
            log.warning(
                "wait_cycle_task_map_failed",
                project_id=project_id,
                error=str(e),
            )
        return out


# ── 上游死亡 durable 重醒（TEST_DSH_63 批3 组4，2026-09-19）─────────────
# 病：一次 30 秒上游抖动窗（403 RegionError / 503 重试耗尽）可连杀多个 run；
# run 死后 agent 永久停摆 —— 无自动重醒、无通知。DSH 参照哲学：死亡可接受，
# 恢复靠 **durable 触发**，任务保持 claimed（不做 parked/断点续跑，整轮重放，
# 前缀缓存友好）。
# 药：复用**既有** agent_waits 机制——插一条 kind=timer 的等待行，到期由
# game_time tick 的 clear_expired → [WAIT_TIMEOUT] + watchdog trigger 唤醒
# （后端重启后 activate 路径的对账同样覆盖，天然 durable）。
# 副产品（正是任务 2 的 dwell 暂停）：_nudge_stale_ledger 的 live_wait_agents
# 把任何 active wait 的 agent 视为「合法等待」，stall 计数 / auto-submit /
# VERIFY 改派全部跳过；唤醒行被清后自然恢复。obligations.has_open_work 的
# wait 负项同样生效。⇒ 不需要任何 game_time / 义务时钟侧的新代码。
# 次数持久化：attempt 序号写进 note 的 [wakeup_reason=upstream_recovery
# attempt=N] 标记 + 行的 phase='upstream_recovery'；窗口期内的行（含已清除）
# 计数取档，不新造调度器、不加表、不加列。

UPSTREAM_RECOVERY_KIND = "timer"
"""重醒等待行的 kind —— 复用 timer 语义（wake_on 含 timeout）。"""

UPSTREAM_RECOVERY_PHASE = "upstream_recovery"
"""重醒等待行的 phase 标记（agent_waits.phase），DB 级可机检。"""

UPSTREAM_RECOVERY_ATTEMPTS = 3
"""自动重醒封顶次数；耗尽后停止重醒，交人工（ORG_ESCALATION）。"""

UPSTREAM_RECOVERY_DELAYS_MS = (60_000, 180_000, 600_000)
"""退避阶梯（第 1/2/3 次死亡的重醒延迟）。"""

UPSTREAM_RECOVERY_WINDOW_MS = 30 * 60 * 1000
"""attempt 计数窗：窗口内的重醒行（含已清除）计入退避档位，窗外归零。"""

UPSTREAM_RECOVERY_NOTE_TAG = "[wakeup_reason=upstream_recovery"
"""note 标记前缀 —— game_time._process_wait_contracts 解析出
wakeup_reason=upstream_recovery 放进 [WAIT_TIMEOUT] 的 details；
trigger.wake_source_for_pending 靠正文里的同一标记识别重醒唤醒。"""

_UPSTREAM_RECOVERY_WAKE_TEXT = "上游抖动后自动恢复,任务与上下文不变"
"""唤醒 reason 文案（钦定）。放进 ref —— [WAIT_TIMEOUT] 正文含
`Your wait (timer:<ref>) expired`，文案因此随唤醒信可达 agent。"""


def _upstream_recovery_note(attempt: int, run_id: str, target_ms: int) -> str:
    base = _UPSTREAM_RECOVERY_WAKE_TEXT
    tag = (
        f"{_WAKEUP_REASON_TAG}upstream_recovery attempt={attempt} "
        f"run={(run_id or '')[:8]} target={_iso_utc(target_ms)}]"
    )
    return f"{base} | {tag}"


async def _upstream_recovery_rows(
    project_id: str, agent_id: str, *, active_only: bool
) -> list[dict]:
    await _ensure_schema(project_id)
    conn = await _conn(project_id)
    if conn is None:
        return []
    sql = (
        "SELECT * FROM agent_waits "
        "WHERE agent_id = ? AND kind = ? AND phase = ? "
        + ("AND cleared_at IS NULL " if active_only else "")
        + "ORDER BY created_at ASC"
    )
    try:
        cur = await conn.execute(
            sql, [agent_id, UPSTREAM_RECOVERY_KIND, UPSTREAM_RECOVERY_PHASE]
        )
        rows = await cur.fetchall()
        await cur.close()
    except Exception as e:  # noqa: BLE001 — fail-open：查询失败按无行处理
        log.debug("upstream_recovery_scan_failed", error=str(e))
        return []
    return [_row_to_dict(r) for r in rows]


async def has_active_upstream_recovery(
    project_id: str, agent_id: str
) -> bool:
    """该 agent 是否有**未到期**的上游重醒等待（dwell/义务时钟暂停判据）。

    消费点：obligations.has_pending_upstream_recovery（具名读法）；
    真正的跳过发生在 game_time._nudge_stale_ledger 的 live_wait_agents
    （任何 active wait 即跳过 stall/改派/自动提交）——本谓词是其
    upstream_recovery 子集，供定向观测与未来豁免点复用。
    """
    rows = await _upstream_recovery_rows(project_id, agent_id, active_only=True)
    now = int(time.time() * 1000)
    return any(
        (w.get("expiresAt") is None or int(w.get("expiresAt") or 0) > now)
        for w in rows
    )


async def schedule_upstream_recovery_wait(
    project_id: str,
    agent_id: str,
    *,
    run_id: str = "",
    now_ms: int | None = None,
) -> dict:
    """给死于上游错误的 agent 排一次 durable 自动唤醒（幂等、封顶 3 次）。

    返回 dict：
    - ``{"scheduled": True, "attempt": n, "delay_ms": d, "wake_at": ts,
      "wait_id": id}`` —— 已插入等待行；
    - ``{"scheduled": False, "reason": "pending_exists"}`` —— 已有未触发
      的重醒等待（不叠排，保留最早一次）；
    - ``{"scheduled": False, "exhausted": True, "attempt": n}`` —— 窗口内
      已耗尽 3 次，调用方应升级人工（不再排）。

    只 INSERT 单行，**不清**该 agent 既有等待（与 replace_waits 的全清
    语义刻意不同——死亡不该抹掉它上一轮的合法停泊）。
    """
    if not project_id or not agent_id:
        return {"scheduled": False, "reason": "missing_ids"}
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    active = await _upstream_recovery_rows(
        project_id, agent_id, active_only=True
    )
    if active:
        return {"scheduled": False, "reason": "pending_exists"}
    recent = await _upstream_recovery_rows(
        project_id, agent_id, active_only=False
    )
    recent = [
        r for r in recent
        if int(r.get("createdAt") or 0) >= now - UPSTREAM_RECOVERY_WINDOW_MS
    ]
    attempt = len(recent) + 1
    if attempt > UPSTREAM_RECOVERY_ATTEMPTS:
        return {
            "scheduled": False,
            "exhausted": True,
            "attempt": len(recent),
        }
    delay_ms = UPSTREAM_RECOVERY_DELAYS_MS[
        min(attempt - 1, len(UPSTREAM_RECOVERY_DELAYS_MS) - 1)
    ]
    exp = now + delay_ms
    wid = str(uuid.uuid4())
    await _ensure_schema(project_id)
    await execute_by_project(
        project_id,
        "INSERT INTO agent_waits "
        "(id, agent_id, project_id, kind, ref, wake_on, expires_at, "
        "obligation_version, phase, note, created_at, cleared_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL)",
        [
            wid,
            agent_id,
            project_id,
            UPSTREAM_RECOVERY_KIND,
            f"{_UPSTREAM_RECOVERY_WAKE_TEXT}(attempt={attempt}"
            f"/{UPSTREAM_RECOVERY_ATTEMPTS})",
            json.dumps(DEFAULT_WAKE_ON.get(UPSTREAM_RECOVERY_KIND, ["timeout"])),
            exp,
            UPSTREAM_RECOVERY_PHASE,
            _upstream_recovery_note(attempt, run_id, exp),
            now,
        ],
    )
    log.warning(
        "upstream_recovery_wake_scheduled",
        agent_id=agent_id,
        project_id=project_id,
        run_id=(run_id or "")[:8],
        attempt=f"{attempt}/{UPSTREAM_RECOVERY_ATTEMPTS}",
        delay_s=delay_ms // 1000,
    )
    return {
        "scheduled": True,
        "attempt": attempt,
        "delay_ms": delay_ms,
        "wake_at": exp,
        "wait_id": wid,
    }


def _norm_token(value: str | None) -> str:
    return (value or "").strip().lower()


def _exact_identity_tokens(
    *,
    from_agent_id: str | None = None,
    from_agent_name: str | None = None,
    from_short_id: str | None = None,
) -> set[str]:
    tokens: set[str] = set()
    for raw in (from_agent_id, from_agent_name, from_short_id):
        t = _norm_token(raw)
        if not t:
            continue
        tokens.add(t)
        tokens.add(t.replace(" ", ""))
    return tokens


def _ref_matches_sender(
    ref: str,
    *,
    from_agent_id: str | None = None,
    from_agent_name: str | None = None,
    from_short_id: str | None = None,
) -> bool:
    """Fail-open exact match of wait.ref vs sender id / name / short_id.

    No startswith prefix — prefix coincidence must not wake or clear.
    """
    r = _norm_token(ref)
    if not r:
        return False
    tokens = _exact_identity_tokens(
        from_agent_id=from_agent_id,
        from_agent_name=from_agent_name,
        from_short_id=from_short_id,
    )
    return r in tokens or r.replace(" ", "") in tokens


def _agent_accepts_ref_exact(agent: dict, ref: str) -> bool:
    """True only when ref is exactly this agent's id, name, or short_id."""
    return _ref_matches_sender(
        ref,
        from_agent_id=agent.get("id"),
        from_agent_name=agent.get("name"),
        from_short_id=agent.get("short_id"),
    )


def _wait_not_expired(wait: dict, *, now_ms: int | None = None) -> bool:
    exp = wait.get("expiresAt") if wait.get("expiresAt") is not None else wait.get("expires_at")
    if exp is None:
        return True
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    try:
        return int(exp) > now
    except (TypeError, ValueError):
        return True


async def resolve_identity_to_agent_id(
    project_id: str | None,
    token: str,
) -> str | None:
    """Map 花名 / A100 / uuid → agents.id via OrgService.

    Prefix-only OrgService hits (unique name prefix, uuid prefix) are
    rejected so match and clear stay exact on identity fields.
    Fail-open: org errors or misses return None.
    """
    raw = (token or "").strip()
    if not raw or not project_id:
        return None
    try:
        from hiveweave.services.org import OrgService

        agent = await OrgService().resolve_agent_ref(project_id, raw)
    except Exception:
        return None
    if not agent or not _agent_accepts_ref_exact(agent, raw):
        return None
    aid = str(agent.get("id") or "").strip()
    return aid or None


async def matching_kind_agent_waits(
    project_id: str | None,
    waits: list[dict],
    *,
    from_agent_id: str | None = None,
    from_agent_name: str | None = None,
    from_short_id: str | None = None,
) -> list[dict]:
    """Active kind=agent waits whose ref is the same person as the sender."""
    if not waits:
        return []
    now = int(time.time() * 1000)
    cache: dict[str, str | None] = {}

    async def _resolved(token: str | None) -> str | None:
        raw = (token or "").strip()
        if not raw:
            return None
        key = raw.lower()
        if key not in cache:
            cache[key] = await resolve_identity_to_agent_id(project_id, raw)
        return cache[key]

    sender_id = None
    for tok in (from_agent_id, from_agent_name, from_short_id):
        sender_id = await _resolved(tok)
        if sender_id:
            break

    matched: list[dict] = []
    for w in waits:
        if str(w.get("kind") or "").lower() != "agent":
            continue
        if not _wait_not_expired(w, now_ms=now):
            continue
        ref = str(w.get("ref") or "")
        wait_id = await _resolved(ref)
        if wait_id and sender_id and wait_id == sender_id:
            matched.append(w)
            continue
        if wait_id and from_agent_id and wait_id == str(from_agent_id).strip():
            matched.append(w)
            continue
        # Fail-open: org miss → exact string equality only (no prefix).
        if _ref_matches_sender(
            ref,
            from_agent_id=from_agent_id,
            from_agent_name=from_agent_name,
            from_short_id=from_short_id,
        ):
            matched.append(w)
    return matched


async def kind_agent_wait_matches_sender(
    project_id: str | None,
    waits: list[dict],
    *,
    from_agent_id: str | None = None,
    from_agent_name: str | None = None,
    from_short_id: str | None = None,
) -> bool:
    found = await matching_kind_agent_waits(
        project_id,
        waits,
        from_agent_id=from_agent_id,
        from_agent_name=from_agent_name,
        from_short_id=from_short_id,
    )
    return bool(found)


async def event_matches_waits(
    waits: list[dict],
    *,
    event: str,
    from_agent_id: str | None = None,
    from_agent_name: str | None = None,
    from_short_id: str | None = None,
    project_id: str | None = None,
) -> bool:
    """True if any active wait accepts this wake event."""
    if not waits:
        return True  # no contract → fall back to disposition policy
    now = int(time.time() * 1000)
    agent_hits: list[dict] | None = None

    async def _agent_hits() -> list[dict]:
        nonlocal agent_hits
        if agent_hits is None:
            agent_hits = await matching_kind_agent_waits(
                project_id,
                waits,
                from_agent_id=from_agent_id,
                from_agent_name=from_agent_name,
                from_short_id=from_short_id,
            )
        return agent_hits

    for w in waits:
        if not _wait_not_expired(w, now_ms=now):
            continue
        wake_on = w.get("wakeOn") or w.get("wake_on") or []
        if isinstance(wake_on, str):
            try:
                wake_on = json.loads(wake_on)
            except Exception:
                wake_on = []
        kind = (w.get("kind") or "").lower()
        ref = w.get("ref") or ""

        if kind == "agent" and event in (
            "message_from_ref",
            "ask_reply",
            "command",
        ):
            hits = await _agent_hits()
            if any(h.get("id") == w.get("id") for h in hits):
                return True

        if event not in wake_on:
            continue

        if event == "message_from_ref":
            if kind == "agent":
                hits = await _agent_hits()
                if any(h.get("id") == w.get("id") for h in hits):
                    return True
                continue
            if _ref_matches_sender(
                ref,
                from_agent_id=from_agent_id,
                from_agent_name=from_agent_name,
                from_short_id=from_short_id,
            ):
                return True
            continue
        return True
    return False


_FULL_CLEAR_WAKE_SOURCES = frozenset({
    "", "user", "chat",
    "wait_timeout", "wait_cycle", "wait_satisfied",
})


def _is_person_sender(from_agent_id: str | None) -> bool:
    fid = (from_agent_id or "").strip()
    if not fid or fid.lower() == "system":
        return False
    from hiveweave.services.wake_policy import is_user_sender

    return not is_user_sender(fid)


def unique_agent_tokens(*groups: Any) -> list[str]:
    """Stable unique id/name tokens (order-preserving, case-insensitive)."""
    out: list[str] = []
    seen: set[str] = set()

    def _add(value: Any) -> None:
        if value is None:
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                _add(item)
            return
        text = str(value).strip()
        if not text:
            return
        key = text.lower()
        if key in seen:
            return
        seen.add(key)
        out.append(text)

    for group in groups:
        _add(group)
    return out


async def matching_sender_ids_for_waiter(
    project_id: str,
    waiter_agent_id: str,
    candidate_from_ids: list[str] | None,
) -> list[str]:
    """Subset of *candidate_from_ids* that match an active kind=agent wait."""
    pid = (project_id or "").strip()
    waiter = (waiter_agent_id or "").strip()
    tokens = unique_agent_tokens(candidate_from_ids)
    if not pid or not waiter or not tokens:
        return []
    waits = await wait_contract_service.list_active(pid, waiter)
    matched: list[str] = []
    for fid in tokens:
        if await kind_agent_wait_matches_sender(
            pid, waits, from_agent_id=fid
        ):
            matched.append(fid)
    return matched


async def apply_wake_admit_wait_clear(
    project_id: str,
    waiter_agent_id: str,
    *,
    source: str = "",
    from_agent_id: str | None = None,
    from_agent_ids: list[str] | None = None,
    trigger: bool = False,
    clear_waits: bool | None = None,
) -> str:
    """Clear waits on chat admit. Returns ``skip`` / ``scoped`` / ``all``.

    Inbox-from-person (``message_from_ref`` or a peer sender) clears only
    matching kind=agent waits. Pass **all matching senders** in
    ``from_agent_ids`` — ``from_agent_id`` is often the first inbox row,
    not the person the wait names. ``wait_satisfied`` keeps
    ``clear_waits=False`` (sibling bg-bash waits stay). Other user/timeout
    sources still full-clear.
    """
    if clear_waits is False:
        return "skip"
    src = source or ""
    senders = unique_agent_tokens(from_agent_ids, from_agent_id)
    person = src == "message_from_ref" or any(
        _is_person_sender(s) for s in senders
    )
    if person:
        if src == "message_from_ref" and not senders:
            return "skip"
        for fid in senders:
            if not _is_person_sender(fid):
                continue
            await wait_contract_service.clear_kind_agent_waits_for_sender(
                project_id, waiter_agent_id, fid
            )
        return "scoped"
    should = (
        bool(clear_waits)
        or not trigger
        or src in _FULL_CLEAR_WAKE_SOURCES
    )
    if should:
        await wait_contract_service.clear_waits(project_id, waiter_agent_id)
        return "all"
    return "skip"


async def project_id_for_agent(agent_id: str) -> str | None:
    """Best-effort agent_id → project_id (AgentRouter). Fail-open None."""
    aid = (agent_id or "").strip()
    if not aid:
        return None
    try:
        from hiveweave.db import meta as meta_db

        return await meta_db.get_agent_project_id(aid)
    except Exception:
        return None


def _fact_ref_matches(wait_ref: str, fact_kind: str, subject: str) -> bool:
    """fact 等待 ref 语义：``"<fact_kind>[:<subject 子串>]"``。

    - 无 ``:`` → 只按事实类型匹配（任意主体）
    - 有 ``:`` → 类型相同**且** subject 子串命中（大小写不敏感）
    空串/通配 ``*`` 视为只匹配类型。 """
    r = (wait_ref or "").strip()
    if not r or r == "*":
        return False  # 裸 '*' 等所有事实 = 无界等待，不合法，按不匹配处理
    kind_part, _, subject_part = r.partition(":")
    if str(kind_part or "").strip().lower() != str(fact_kind or "").strip().lower():
        return False
    want = subject_part.strip().lower()
    return not want or want in str(subject or "").lower()


async def wake_fact_waiters(
    project_id: str,
    fact_kind: str,
    subject: str,
    *,
    value: str | None = None,
    verified_by: str = "platform",
    verified_at: int | None = None,
) -> list[str]:
    """按事实唤醒（L3 完整形态，2026-09-08）：匹配 kind=fact 等待并投递。

    由 fact_bus 订阅方（game_time）在事实发布时调用；返回被唤醒的
    agent id 列表（调用方负责 watchdog 触发）。匹配的等待行就地关闭
    （cleared_at），未匹配的等待继续等（TTL 兜底饿死保险不变）。
    """
    waits = await wait_contract_service.list_all_active(project_id)
    fact_waits = [
        w for w in waits if str(w.get("kind") or "").lower() == "fact"
    ]
    if not fact_waits:
        return []

    now = int(time.time() * 1000)
    at_iso = time.strftime(
        "%Y-%m-%d %H:%M:%S", time.localtime((verified_at or now) / 1000)
    )
    woken: list[str] = []
    matched_ids: list[str] = []
    for w in fact_waits:
        aid = str(w.get("agentId") or w.get("agent_id") or "")
        wid = str(w.get("id") or "")
        if not aid or not wid:
            continue
        if not _fact_ref_matches(str(w.get("ref") or ""), fact_kind, subject):
            continue
        matched_ids.append(wid)
        if aid not in woken:
            woken.append(aid)
    if not matched_ids:
        return []

    for wid in matched_ids:
        try:
            await _execute_rowcount(
                project_id,
                "UPDATE agent_waits SET cleared_at = ? WHERE id = ? "
                "AND cleared_at IS NULL",
                [now, wid],
            )
        except Exception as e:
            log.warning("fact_wait_clear_failed", wait_id=wid, error=str(e))

    # 投递唤醒信（与 [WAIT_TIMEOUT] 同通道：system urgent + watchdog）
    try:
        from hiveweave.services.inbox import InboxService

        body = (
            f"[FACT_OBSERVED] 你等待的事实已出现：kind={fact_kind} "
            f"subject={subject} value={value or ''} "
            f"verified_by={verified_by} at={at_iso}。"
            "事实是参考上下文——行动前可自行复核。Resume work."
        )
        inbox = InboxService()
        for aid in woken:
            try:
                await inbox.send_message(
                    from_agent_id="system",
                    to_agent_id=aid,
                    message=body,
                    message_type="system",
                    priority="urgent",
                )
            except Exception as e:
                log.warning("fact_wake_notify_failed", agent_id=aid, error=str(e))
    except Exception as e:
        log.warning("fact_wake_inbox_unavailable", error=str(e))
    log.info(
        "fact_waiters_woken",
        project_id=project_id,
        fact_kind=fact_kind,
        subject=subject[:80],
        woken=len(woken),
    )
    return woken


def category_to_wake_event(
    category: str,
    *,
    from_agent_id: str | None = None,
) -> str:
    from hiveweave.services.wake_policy import is_user_sender

    if is_user_sender(from_agent_id):
        return "user_message"
    if from_agent_id == "system":
        return "timeout"
    if category == "task_transition":
        return "task_transition"
    if category == "ask":
        return "ask_reply"
    if category == "approval":
        return "task_transition"
    return "message_from_ref"


wait_contract_service = WaitContractService()
