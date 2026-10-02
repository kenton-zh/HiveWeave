"""TokenMeter — per-agent LLM token metering service.

每行 llm_usage = 一次 LLM 请求，归属 agent/run/task/project 四级，覆盖
主对话 / 压缩 / 子代理三条调用路径。所有方法 best-effort：错误仅记日志、
绝不阻塞主流程（与 run_ledger 一致）。

写入路径:
- record_rounds: 主对话 / 子代理，带 usage_rounds 列表（每轮一条）
- record_compaction: 压缩调用（绕过 Streamer，单条）

聚合路径:
- agent_summary / project_by_agent / daily_summary / run_summary / platform_overview

缓存量程（P2-⑨）: ``cache_creation_tokens`` 只有 Anthropic 系上报，OpenAI 系
线上根本不带该字段。因此聚合结果附 ``cache_creation_scope``
（reported/unreported/mixed）+ ``cache_hit_basis``，让命中率能说清分母。
逐行量程位（42 轮 P2-9）：llm_usage.creation_unreported=1 表示该行
provider 未回传 cache 写入（0 是「无数据」），0 = 上游真回传（含真 0）；
行数不足 MIN_CACHE_HIT_SAMPLE_ROWS 时聚合命中率输出「样本不足」而非数值。

记账覆盖率口径（45 轮 P2 观察，2026-09-06 文档化）: ``llm_usage 行数 /
agent_runs.actual_llm_calls`` 可**合法地大于 100%**（45 轮实测
115.64%/109.27%）——子代理的 usage round 归父账（record_rounds 带
request_type="subagent"、落父 run），而 agent_runs 的 actual_llm_calls
只按子代理自身 run 计数；多代理并行时分子含跨 run 归属行、分母是各 run
自报调用数，口径不同不可当「漏账/重账」判据用。覆盖率判据（R9）只用于
下界检查（<50% = 账本断裂），上界无意义。
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.db import project as project_db
from hiveweave.llm.util import (
    cache_hit_percent,
    provider_reports_cache_creation,
)

log = structlog.get_logger()

#: 聚合 SQL 共用的 provider 收集列 — 能力位在 Python 侧按单一判据现算，
#: 不把 "anthropic" 之类的字面量复写进 SQL。
_PROVIDERS_AGG = "GROUP_CONCAT(DISTINCT provider) AS providers"

#: llm_usage 行 INSERT 列集 — 主写路径与补录回放共用同一常量，杜绝两处漂移。
_USAGE_INSERT_ROUNDS = (
    "INSERT INTO llm_usage "
    "(id, agent_id, project_id, run_id, task_id, model_id, "
    "request_type, provider, input_tokens, output_tokens, "
    "cache_read_tokens, cache_creation_tokens, total_tokens, "
    "duration_ms, cold_start, creation_unreported, "
    "timeout_layer, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_USAGE_INSERT_COMPACTION = (
    "INSERT INTO llm_usage "
    "(id, agent_id, project_id, run_id, task_id, model_id, "
    "request_type, provider, input_tokens, output_tokens, "
    "cache_read_tokens, cache_creation_tokens, total_tokens, "
    "duration_ms, creation_unreported, created_at) "
    "VALUES (?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

# ── P1-11: llm_usage 写失败补录（spill sidecar + 回放）────────────
# 实测：usage_recorder_failed（disk I/O error / closed database）各 1 次
# —— 写失败即永久丢，重试层（_execute_write_with_retry）只覆盖 lock/busy。
# 修法 = 缓冲 + 补录：非 lock 类写失败 ⇒ 待写行落 JSONL sidecar（append
# 语义天然幂等安全）；下次 record 入口先回放，成功后删除。行数上限防膨胀，
# 超限丢最旧。回放失败不阻塞主写入；sidecar 写失败只 log（不再丢主流程）。
USAGE_SPILL_FILENAME = "llm_usage_spill.jsonl"
USAGE_SPILL_MAX_LINES = 10_000

#: 补录回放用 OR IGNORE：按行 id（主键）幂等 —— 回放成功但 sidecar 删除
#: 失败时，下次重放不会重复计账、也不会因主键冲突炸掉整批回放。
#: 由 INSERT 常量派生（单一来源，杜绝列集两处漂移）。
_USAGE_REPLAY_SQL = {
    "rounds": _USAGE_INSERT_ROUNDS.replace(
        "INSERT INTO ", "INSERT OR IGNORE INTO ", 1
    ),
    "compaction": _USAGE_INSERT_COMPACTION.replace(
        "INSERT INTO ", "INSERT OR IGNORE INTO ", 1
    ),
}

#: cache 写入量程：全部上报 / 全部不上报 / 混合。
CACHE_SCOPE_REPORTED = "reported"
CACHE_SCOPE_UNREPORTED = "unreported"
CACHE_SCOPE_MIXED = "mixed"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _row_get(row: Any, key: str) -> Any:
    """读聚合行的某列 — sqlite3.Row 无 .get()，缺列时返回 None。"""
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def cache_creation_scope(providers: Any) -> str:
    """按聚合内出现过的 provider 判定 cache 写入量程。

    ``providers`` 为 GROUP_CONCAT 的 CSV（可能为 None/空）。无 provider 记录
    时按 unreported 处理 —— 未知不得当成「确实为 0」。
    """
    names = [
        p.strip() for p in str(providers or "").split(",") if p and p.strip()
    ]
    if not names:
        return CACHE_SCOPE_UNREPORTED
    flags = {provider_reports_cache_creation(p) for p in names}
    if flags == {True}:
        return CACHE_SCOPE_REPORTED
    if flags == {False}:
        return CACHE_SCOPE_UNREPORTED
    return CACHE_SCOPE_MIXED


#: 命中率分母口径说明 — 与 cache_creation_scope 一一对应。
_CACHE_HIT_BASIS = {
    CACHE_SCOPE_REPORTED: "input+cache_read+cache_creation",
    CACHE_SCOPE_UNREPORTED:
        "input+cache_read (cache_creation not reported by provider)",
    CACHE_SCOPE_MIXED:
        "input+cache_read+cache_creation (partial: mixed providers)",
}

#: 命中率最小样本量（42 轮 P2-9）：usage 行数低于此值时分母缺分量，
#: 命中率只是乐观上限 —— 输出「样本不足」而非数值。
MIN_CACHE_HIT_SAMPLE_ROWS = 20

INSUFFICIENT_SAMPLE_NOTE = "样本不足"
"""cache_hit_percent 在 usage 行数 < MIN_CACHE_HIT_SAMPLE_ROWS 时的文案。"""


def _with_cache_scope(row: dict[str, Any]) -> dict[str, Any]:
    """把 providers CSV 换成量程字段 + 命中率（含分母口径说明）。

    39 审计 P1-2：DeepSeek 系 prompt_tokens = hit + miss（input-inclusive），
    分母必须用 input 本身；exclusive 系（Anthropic）分母 = input+read+creation。
    混合场景取保守口径（disjoint 公式，命中率只低不高）。
    """
    providers_csv = row.pop("providers", None)
    scope = cache_creation_scope(providers_csv)
    row["cache_creation_scope"] = scope
    # 乐观上限标注（审计 #7）：cache_creation 未全量上报时分母缺分量，
    # 命中率只可能是乐观上限 —— 与 cache_creation_scope 同 schema 一并输出。
    row["cache_hit_optimistic"] = scope != CACHE_SCOPE_REPORTED
    names = [
        p.strip() for p in str(providers_csv or "").split(",") if p and p.strip()
    ]
    from hiveweave.llm.util import provider_input_inclusive

    inclusive = bool(names) and all(provider_input_inclusive(p) for p in names)
    # 样本不足判据（42 轮 P2-9）：行数 < 阈值时不给数值 —— 双项目实测
    # cache_creation 全 0 时 94.77/95.27% 均为乐观上限，小样本数字只会
    # 误导归因。给出文案而非 None，让报表/终端直读可辨。
    if int(row.get("llm_calls") or 0) < MIN_CACHE_HIT_SAMPLE_ROWS:
        row["cache_hit_percent"] = INSUFFICIENT_SAMPLE_NOTE
    else:
        row["cache_hit_percent"] = cache_hit_percent(
            int(row.get("input_tokens") or 0),
            int(row.get("cache_read_tokens") or 0),
            int(row.get("cache_creation_tokens") or 0),
            input_inclusive=inclusive,
        )
    row["cache_hit_basis"] = (
        "cache_read/input (provider prompt includes cache hit+miss)"
        if inclusive
        else _CACHE_HIT_BASIS[scope]
    )
    return row


# ── P1-11: spill sidecar 机制（写失败缓冲 + 下次补录）──────────────


async def _workspace_for_agent(agent_id: str) -> str | None:
    """解析 agent 所在 workspace（sidecar 落点 = ``<ws>/.hiveweave/``）。

    best-effort：路由链任一环失败返回 None（此时无处落 sidecar，只能
    靠既有 error 日志 + agent_events 告警留痕）。
    """
    try:
        project_id = await meta_db.get_agent_project_id(agent_id)
        if not project_id:
            return None
        return await meta_db.get_project_workspace(project_id)
    except Exception:
        return None


def _usage_spill_path(workspace: str) -> Path:
    return Path(workspace) / ".hiveweave" / USAGE_SPILL_FILENAME


def _spill_usage_rows(workspace: str, kind: str, rows: list[list[Any]]) -> None:
    """待写 usage 行落 JSONL sidecar（append；写失败只 log，绝不再丢主流程）。"""
    if not workspace or not rows:
        return
    path = _usage_spill_path(workspace)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = _now_ms()
        with open(path, "a", encoding="utf-8") as f:
            for row in rows:
                f.write(
                    json.dumps(
                        {"v": 1, "kind": kind, "params": row, "spilled_at": stamp},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
    except Exception as exc:
        log.error(
            "token_meter.usage_spill_write_failed",
            workspace=workspace,
            kind=kind,
            n=len(rows),
            error=str(exc),
        )
        return
    _trim_usage_spill(path)


def _trim_usage_spill(path: Path) -> None:
    """sidecar 行数上限防膨胀：超限丢最旧并 log.error（账已断，膨胀会拖垮磁盘）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return
    except Exception as exc:
        log.warning(
            "token_meter.usage_spill_trim_read_failed", path=str(path),
            error=str(exc),
        )
        return
    if len(lines) <= USAGE_SPILL_MAX_LINES:
        return
    dropped = len(lines) - USAGE_SPILL_MAX_LINES
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines[-USAGE_SPILL_MAX_LINES:])
        log.error(
            "token_meter.usage_spill_overflow_dropped",
            path=str(path),
            dropped=dropped,
            cap=USAGE_SPILL_MAX_LINES,
        )
    except Exception as exc:
        log.error(
            "token_meter.usage_spill_trim_write_failed",
            path=str(path),
            error=str(exc),
        )


async def _replay_usage_spill(agent_id: str) -> None:
    """回放 sidecar 里上次写失败的 usage 行（best-effort，失败不阻塞主写入）。

    - 回放走 INSERT OR IGNORE（按行 id 幂等）：回放成功但 sidecar 删除失败
      时，下次重放不重复计账。
    - 坏 JSON 行跳过不炸（不可解析的行本就无账可补），随成功回放一并清除。
    - 回放失败（DB 仍不可用 / 路由失败）保留 sidecar，下次 record 入口再试。
    """
    ws = await _workspace_for_agent(agent_id)
    if not ws:
        return
    path = _usage_spill_path(ws)
    if not path.exists():
        return
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as exc:
        log.warning(
            "token_meter.usage_spill_read_failed", path=str(path), error=str(exc)
        )
        return
    statements: list[tuple[str, list[Any]]] = []
    bad = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            kind = rec["kind"]
            params = rec["params"]
            if kind not in _USAGE_REPLAY_SQL or not isinstance(params, list):
                raise ValueError(f"bad spill record kind={kind!r}")
            statements.append((_USAGE_REPLAY_SQL[kind], params))
        except Exception:
            bad += 1  # 坏行跳过，不炸
    if not statements:
        if bad:
            # 全是坏行：不可补录，清掉防每次 record 都空转
            try:
                path.unlink()
                log.error(
                    "token_meter.usage_spill_dropped_corrupt",
                    path=str(path), n=bad,
                )
            except Exception as exc:
                log.warning(
                    "token_meter.usage_spill_cleanup_failed",
                    path=str(path), error=str(exc),
                )
        return
    try:
        await project_db.execute_transaction(agent_id, statements)
    except Exception as exc:
        log.warning(
            "token_meter.usage_spill_replay_failed",
            agent_id=agent_id,
            n=len(statements),
            error=str(exc),
        )
        return  # 保留 sidecar，下次再试
    try:
        path.unlink()
    except Exception as exc:
        # 删除失败只留痕：重放按行 id 幂等，不会重复计账
        log.warning(
            "token_meter.usage_spill_cleanup_failed",
            path=str(path), error=str(exc),
        )
    log.info(
        "token_meter.usage_spill_replayed",
        agent_id=agent_id,
        n=len(statements),
        skipped_bad=bad,
    )


class TokenMeter:
    """Token 计量服务（per-project best-effort 写入 + 聚合查询）。"""

    # ── 写入 ─────────────────────────────────────────────────

    async def record_rounds(
        self,
        agent_id: str,
        project_id: str | None,
        rounds: list[dict],
        model_id: str | None = None,
        provider: str | None = None,
        run_id: str | None = None,
        task_id: str | None = None,
        request_type: str = "main",
    ) -> None:
        """批量落库一轮或多轮 usage（主对话 / 子代理路径）。

        rounds 为 streamer 返回的 usage_rounds，每项含
        input/output/cache_read/cache_creation/total/duration_ms。
        """
        # P1-11：入口先补录上次写失败遗留的 sidecar 行（best-effort，
        # 失败不阻塞本次记账主流程）。
        try:
            await _replay_usage_spill(agent_id)
        except Exception as exc:  # noqa: BLE001 — 回放失败绝不挡主写入
            log.warning(
                "token_meter.usage_spill_replay_error",
                agent_id=agent_id,
                error=str(exc),
            )
        if not rounds:
            # TEST_DSH_50/51 R11（2026-09-10）：静默 return 会让「零账」在
            # 任何日志里都无迹可寻 —— 调用方刚经历 error / 整轮硬超时兜底
            # 路径时（result 由 _error_result 构造，usage_rounds 恒为空），
            # 这里什么都不写，下游 flush 也只能拿到空，整条链没有一处留痕。
            # 加一行 info：谁、哪个 run、哪类请求调了空批次。
            log.info(
                "record_rounds_empty",
                agent_id=agent_id,
                run_id=run_id,
                request_type=request_type,
            )
            return
        now = _now_ms()
        statements: list[tuple[str, list[Any]]] = []
        # F11：首请求冷启动标记 —— 该 run 第一条 usage 记录 cache_read=0 且
        # cache_creation=0（cache 全未命中，前缀重建成本无账）→ 打 cold_start=1。
        # provider 不上报 cache_creation 时（OpenAI 系），cache_read=0 且
        # input>0 即冷启动。让 r4 的「20 run 首请求零命中 / 1.7M tokens 无账」
        # 变成可查数字。
        first_round = rounds[0] if isinstance(rounds[0], dict) else None
        is_cold_start = bool(
            first_round
            and int(first_round.get("cache_read", 0) or 0) == 0
            and int(first_round.get("cache_creation", 0) or 0) == 0
            and int(first_round.get("input", 0) or 0) > 0
        )
        for i, r in enumerate(rounds):
            if not isinstance(r, dict):
                continue
            # 42 轮 P2-9 打标：normalize_usage 的 cache_creation_reported
            # 取反落列 —— 1 = provider 未回传 cache 写入（0 是「无数据」），
            # 0 = 上游真回传（含真 0）。旧路径无该位按未回传处理。
            creation_unreported = 0 if r.get("cache_creation_reported") else 1
            # 批 D 第 2 步任务 4：断流/超时归因层（"first_chunk"/"idle"/
            # "total"，批 B 在判死异常上随行）。只在能确定时写（缺键 →
            # SQL NULL），正常完成行恒 NULL —— 与事实位「未确定不落」同纪律。
            _timeout_layer = r.get("timeout_layer") or None
            statements.append((
                _USAGE_INSERT_ROUNDS,
                [
                    str(uuid.uuid4()),
                    agent_id,
                    project_id,
                    run_id,
                    task_id,
                    model_id,
                    request_type,
                    provider,
                    int(r.get("input", 0) or 0),
                    int(r.get("output", 0) or 0),
                    int(r.get("cache_read", 0) or 0),
                    int(r.get("cache_creation", 0) or 0),
                    int(r.get("total", 0) or 0),
                    int(r.get("duration_ms", 0) or 0),
                    1 if (i == 0 and is_cold_start) else 0,
                    creation_unreported,
                    _timeout_layer,
                    # P2 观测（八轮 TEST_DSH_38）：优先用 streamer 逐次记录
                    # 的真实时刻；缺失（旧路径/异常分支）才退回批量盖章时刻。
                    int(r.get("ts") or now),
                ],
            ))
        if not statements:
            return
        try:
            await project_db.execute_transaction(agent_id, statements)  # type: ignore[arg-type]
        except Exception as e:
            # 正交结果独立上报（DSH defensive-patterns:7-9 戒律）：记账失败
            # 不得随 best-effort 静默蒸发 —— 升级 error 日志 + agent_events 落
            # usage_recorder_failed 告警事件。TEST_DSH_37 P0-① 曾让 274 次调用
            # 零记账且无任何痕迹，Token 页/命中率/税率全盲。
            log.error(
                "token_meter.record_rounds_failed",
                agent_id=agent_id,
                n=len(statements),
                request_type=request_type,
                run_id=run_id,
                error=str(e),
            )
            # P1-11：非 lock 类写失败（disk I/O / closed db）⇒ 待写行落
            # JSONL sidecar，下次 record 入口回放补录。lock 类已有
            # busy_timeout + 重试层兜底，不 spill（避免锁风暴刷盘）。
            if not project_db.is_sqlite_lock_error(e):
                ws = await _workspace_for_agent(agent_id)
                if ws is not None:
                    _spill_usage_rows(
                        ws, "rounds", [params for _, params in statements]
                    )
            try:
                await project_db.execute(
                    agent_id,
                    "INSERT INTO agent_events "
                    "(id, agent_id, event_type, payload, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [
                        str(uuid.uuid4()),
                        agent_id,
                        "usage_recorder_failed",
                        json.dumps({
                            "n_rounds": len(statements),
                            "request_type": request_type,
                            "run_id": run_id,
                            "error": str(e)[:500],
                        }),
                        _now_ms(),
                    ],
                )
            except Exception as ev_err:
                log.error(
                    "token_meter.failure_event_write_failed",
                    agent_id=agent_id,
                    error=str(ev_err),
                )

    async def record_compaction(
        self,
        agent_id: str,
        model_id: str | None,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        kind: str = "conversation",
        provider: str | None = None,
        creation_unreported: int = 0,
        duration_ms: int = 0,
    ) -> None:
        """落库一次压缩 LLM 调用（绕过 Streamer，F3 修正）。

        kind: "conversation" | "memory" → request_type compaction_<kind>。
        total 口径与主/子代理一致 = input + output + cache_creation
        （cache_read 单独列示，不计入 total）。
        压缩调用无 run_id（在 run 外执行），project_id 由 agent 解析。
        ``creation_unreported``：1 = provider 未回传 cache 写入
        （与 record_rounds 同口径，调用方由 normalize_usage 的
        cache_creation_reported 取反传入）。
        ``duration_ms``（批 D 第 2 步）：调用方在 client.post 前后用
        time.monotonic() 实测传入。此前 INSERT 里是**字面量 0**（审计 §6
        第 2 步④）—— 压缩耗时占比在 Token 页恒不可见，聚合 duration_ms
        对压缩流量说谎。默认 0 保持旧调用方兼容（0 = 未计时，诚实缺省）。
        """
        try:
            project_id = await meta_db.get_agent_project_id(agent_id)
        except Exception as e:
            log.warning("token_meter.compaction_project_lookup_failed",
                        agent_id=agent_id, error=str(e))
            project_id = None
        input_tokens = int(input_tokens or 0)
        output_tokens = int(output_tokens or 0)
        cache_read_tokens = int(cache_read_tokens or 0)
        cache_creation_tokens = int(cache_creation_tokens or 0)
        total = input_tokens + output_tokens + cache_creation_tokens
        insert_params: list[Any] = [
            str(uuid.uuid4()),
            agent_id,
            project_id,
            model_id,
            f"compaction_{kind}",
            provider,
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cache_creation_tokens,
            total,
            int(duration_ms or 0),
            1 if creation_unreported else 0,
            _now_ms(),
        ]
        try:
            await project_db.execute(agent_id, _USAGE_INSERT_COMPACTION, insert_params)
        except Exception as e:
            # 与 record_rounds 同口径（正交结果独立上报）：压缩记账失败也
            # 不得静默蒸发 —— error 日志 + agent_events 落告警事件。
            log.error(
                "token_meter.record_compaction_failed",
                agent_id=agent_id,
                kind=kind,
                error=str(e),
            )
            # P1-11：非 lock 类写失败 ⇒ 行落 sidecar，下次 record_rounds
            # 入口回放补录（与 record_rounds 失败行同一份 sidecar）。
            if not project_db.is_sqlite_lock_error(e):
                ws = await _workspace_for_agent(agent_id)
                if ws is not None:
                    _spill_usage_rows(ws, "compaction", [insert_params])
            try:
                await project_db.execute(
                    agent_id,
                    "INSERT INTO agent_events "
                    "(id, agent_id, event_type, payload, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [
                        str(uuid.uuid4()),
                        agent_id,
                        "usage_recorder_failed",
                        json.dumps({
                            "n_rounds": 1,
                            "request_type": f"compaction_{kind}",
                            "run_id": None,
                            "error": str(e)[:500],
                        }),
                        _now_ms(),
                    ],
                )
            except Exception as ev_err:
                log.error(
                    "token_meter.failure_event_write_failed",
                    agent_id=agent_id,
                    error=str(ev_err),
                )

    # ── 聚合查询 ─────────────────────────────────────────────

    @staticmethod
    def _row_to_summary(r: Any) -> dict[str, Any]:
        """把聚合行转成统一响应口径（含缓存量程口径）。"""
        summary: dict[str, Any] = {
            "llm_calls": int(r["llm_calls"] or 0),
            "input_tokens": int(r["input_tokens"] or 0),
            "output_tokens": int(r["output_tokens"] or 0),
            "cache_read_tokens": int(r["cache_read_tokens"] or 0),
            "cache_creation_tokens": int(r["cache_creation_tokens"] or 0),
            "total_tokens": int(r["total_tokens"] or 0),
            "duration_ms": int(r["duration_ms"] or 0),
            "providers": _row_get(r, "providers"),
        }
        return _with_cache_scope(summary)

    async def agent_summary(
        self,
        project_id: str,
        agent_id: str,
        since: int | None = None,
        until: int | None = None,
    ) -> dict[str, Any]:
        """单 agent 的 token 汇总（可带时间窗）。"""
        sql = (
            "SELECT COUNT(*) AS llm_calls, "
            "SUM(input_tokens) AS input_tokens, "
            "SUM(output_tokens) AS output_tokens, "
            "SUM(cache_read_tokens) AS cache_read_tokens, "
            "SUM(cache_creation_tokens) AS cache_creation_tokens, "
            "SUM(total_tokens) AS total_tokens, "
            "SUM(duration_ms) AS duration_ms, "
            f"{_PROVIDERS_AGG} "
            "FROM llm_usage WHERE project_id = ? AND agent_id = ?"
        )
        params: list[Any] = [project_id, agent_id]
        if since:
            sql += " AND created_at >= ?"
            params.append(since)
        if until:
            sql += " AND created_at <= ?"
            params.append(until)
        try:
            rows = await project_db.query_by_project(project_id, sql, params)
        except Exception as e:
            log.warning("token_meter.agent_summary_failed",
                        project_id=project_id, agent_id=agent_id, error=str(e))
            return TokenMeter._row_to_summary(_empty_row())
        return TokenMeter._row_to_summary(rows[0] if rows else _empty_row())

    async def project_by_agent(
        self, project_id: str, since: int | None = None
    ) -> list[dict[str, Any]]:
        """按 agent 分组的 token 汇总（含 request_type 拆分）。"""
        sql = (
            "SELECT agent_id, request_type, "
            "COUNT(*) AS llm_calls, "
            "SUM(input_tokens) AS input_tokens, "
            "SUM(output_tokens) AS output_tokens, "
            "SUM(cache_read_tokens) AS cache_read_tokens, "
            "SUM(cache_creation_tokens) AS cache_creation_tokens, "
            "SUM(total_tokens) AS total_tokens, "
            "SUM(duration_ms) AS duration_ms, "
            f"{_PROVIDERS_AGG} "
            "FROM llm_usage WHERE project_id = ?"
        )
        params: list[Any] = [project_id]
        if since:
            sql += " AND created_at >= ?"
            params.append(since)
        sql += " GROUP BY agent_id, request_type ORDER BY total_tokens DESC"
        try:
            rows = await project_db.query_by_project(project_id, sql, params)
        except Exception as e:
            log.warning("token_meter.project_by_agent_failed",
                        project_id=project_id, error=str(e))
            return []
        return [_with_cache_scope(dict(r)) for r in rows]

    async def daily_summary(
        self, project_id: str, since_days: int = 30
    ) -> list[dict[str, Any]]:
        """按天分组的 token 汇总（近 N 天）。"""
        since = _now_ms() - since_days * 86400_000
        sql = (
            "SELECT date(created_at / 1000, 'unixepoch', 'localtime') AS day, "
            "COUNT(*) AS llm_calls, "
            "SUM(input_tokens) AS input_tokens, "
            "SUM(output_tokens) AS output_tokens, "
            "SUM(cache_read_tokens) AS cache_read_tokens, "
            "SUM(cache_creation_tokens) AS cache_creation_tokens, "
            "SUM(total_tokens) AS total_tokens, "
            f"{_PROVIDERS_AGG} "
            "FROM llm_usage WHERE project_id = ? AND created_at >= ? "
            "GROUP BY day ORDER BY day ASC"
        )
        try:
            rows = await project_db.query_by_project(
                project_id, sql, [project_id, since]
            )
        except Exception as e:
            log.warning("token_meter.daily_summary_failed",
                        project_id=project_id, error=str(e))
            return []
        return [_with_cache_scope(dict(r)) for r in rows]

    async def run_summary(self, run_id: str) -> dict[str, Any] | None:
        """单次 run 的 token 归因（跨项目扫描按 run_id 定位）。"""
        out: list[dict[str, Any]] = []
        for proj in await _list_projects():
            pid = proj["id"]
            try:
                rows = await project_db.query_by_project(
                    pid,
                    "SELECT agent_id, request_type, "
                    "COUNT(*) AS llm_calls, "
                    "SUM(input_tokens) AS input_tokens, "
                    "SUM(output_tokens) AS output_tokens, "
                    "SUM(cache_read_tokens) AS cache_read_tokens, "
                    "SUM(cache_creation_tokens) AS cache_creation_tokens, "
                    "SUM(total_tokens) AS total_tokens, "
                    "SUM(duration_ms) AS duration_ms, "
                    f"{_PROVIDERS_AGG} "
                    "FROM llm_usage WHERE run_id = ? "
                    "GROUP BY agent_id, request_type",
                    [run_id],
                )
            except Exception as e:
                log.warning("token_meter.run_scan_failed",
                            project_id=pid, run_id=run_id, error=str(e))
                continue
            for r in rows:
                out.append(_with_cache_scope({
                    "project_id": pid,
                    "project_name": proj.get("name"),
                    **dict(r),
                }))
        if not out:
            return None
        return {"run_id": run_id, "entries": out}

    async def platform_overview(
        self, since: int | None = None
    ) -> list[dict[str, Any]]:
        """跨项目聚合：遍历各 per-project DB，按 agent 汇总。"""
        out: list[dict[str, Any]] = []
        for proj in await _list_projects():
            pid = proj["id"]
            sql = (
                "SELECT agent_id, COUNT(*) AS llm_calls, "
                "SUM(input_tokens) AS input_tokens, "
                "SUM(output_tokens) AS output_tokens, "
                "SUM(cache_read_tokens) AS cache_read_tokens, "
                "SUM(cache_creation_tokens) AS cache_creation_tokens, "
                "SUM(total_tokens) AS total_tokens, "
                f"{_PROVIDERS_AGG} "
                "FROM llm_usage WHERE 1=1"
            )
            params: list[Any] = []
            if since:
                sql += " AND created_at >= ?"
                params.append(since)
            sql += " GROUP BY agent_id"
            try:
                rows = await project_db.query_by_project(pid, sql, params)
            except Exception as e:
                log.warning("token_meter.platform_scan_failed",
                            project_id=pid, error=str(e))
                continue
            for r in rows:
                out.append(_with_cache_scope({
                    "project_id": pid,
                    "project_name": proj.get("name"),
                    "agent_id": r["agent_id"],
                    "llm_calls": int(r["llm_calls"] or 0),
                    "input_tokens": int(r["input_tokens"] or 0),
                    "output_tokens": int(r["output_tokens"] or 0),
                    "cache_read_tokens": int(r["cache_read_tokens"] or 0),
                    "cache_creation_tokens": int(r["cache_creation_tokens"] or 0),
                    "total_tokens": int(r["total_tokens"] or 0),
                    "providers": _row_get(r, "providers"),
                }))
        out.sort(key=lambda x: x["total_tokens"], reverse=True)
        return out


async def _list_projects() -> list[dict[str, Any]]:
    """列出所有项目（平台级聚合用）。"""
    try:
        rows = await meta_db.query("SELECT id, name FROM projects")
    except Exception as e:
        log.warning("token_meter.list_projects_failed", error=str(e))
        return []
    return [dict(r) for r in rows]


def _empty_row() -> dict[str, Any]:
    return {
        "llm_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "total_tokens": 0,
        "duration_ms": 0,
    }


# Singleton
token_meter = TokenMeter()