"""Doom-loop thresholds and progress helpers."""
from __future__ import annotations

import json

DOOM_LOOP_DEFAULT_LIMIT = 3
"""默认 doom loop 阈值 — 同一工具+同一参数连续 N 次中断。"""

TOOL_FAIL_STALL_LIMIT = 2
"""工具执行失败轮的独立 stall 上限（DSH_33 归因修复）。

刻意与 ``TOOL_LOOP_STALL_LIMIT`` 同值：本次只修**归因**（谁失败了），
不动**容忍度**（第几轮收口）—— 收口时机与修复前逐轮一致，只有文案与
``stall_reason`` 改为如实指向工具层。要调容忍度请单独论证。
"""

BLOCKED_STALL_LIMIT = 3
"""平台护栏拒绝轮（blocked）的独立 stall 上限（H3）。

护栏拒绝（permission/sandbox/security "Command blocked" 等）是平台拒绝
执行，不是模型空转 —— 全 blocked 轮不累计普通 stall_count。但连续
BLOCKED_STALL_LIMIT 轮全被拒说明当前方案不可执行，仍应收口兜底。
取值对齐 DOOM_LOOP_DEFAULT_LIMIT=3 的容忍度（同参 blocked 会先被
doom loop 同参阈值拦住；此处兜底"换着参数试、全被拒"的情形）。
"""

GATE_REJECT_STALL_LIMIT = 12
"""submit 门禁按设计拒收轮（gate_reject）的独立 stall 上限（39 审计 P2-1）。

门禁拒收（冒烟门/审计/契约 prerun 的「正常打回」）是流程回执，不是工具
故障也不是模型空转——不计入 tool_fail 连败。但无限故意撞门仍需兜底：
连续 GATE_REJECT_STALL_LIMIT 轮全被拒说明当前方案不可执行，收口交给
普通 stall_count/预算闸口。取值放宽到 12：负样本演练一轮 3 次拒收 +
正常打回若干次都应留有余量。
"""

STALL_REASON_NO_PROGRESS = "no_progress"
STALL_REASON_TOOL_FAILED = "tool_failed"
STALL_REASON_RUNNER_FAILED = "runner_failed"
STALL_REASON_BLOCKED = "blocked"
STALL_REASON_READONLY = "readonly"
# 39 审计 P2-1：submit 门禁按设计拒收（负样本演练/正常打回）——正交于
# tool_failed，不进连续失败计数（演练语义：被拒 = 成功）。
STALL_REASON_GATE_REJECT = "gate_reject"
#: 门禁按设计拒收的工具集合——它们的失败是「流程回执」而非「工具故障」。
GATE_REJECT_TOOLS = frozenset({"submit_task"})
"""Stall 归因（正交事实位）—— 收口时「谁没动」的唯一口径。

TEST_DSH_33 实测：52 次 tool-loop stall 中 47 次（90.4%）末尾两轮工具
status=failed —— 护栏判定正确，但文案把「工具执行失败」报成「模型无
进展」，模型据此反省自己空转，而真正该改的是工具用法。归因必须与计数
分离：``no_progress``（模型真没动/纯重复）、``tool_failed``（工具执行
返回失败）、``blocked``（平台护栏拒绝）、``readonly``（只读轮询）。

``runner_failed``（执行器/运行时自身故障，如 [No tool executor]）当前
与 ``tool_failed`` 同口径计数——两者都「不是模型空转」，且对模型的建议
一致；保留独立取值位，供后续把运行时故障单独上报/重试时无需改契约。
"""

# ── 只读/幂等轮询工具豁免集合 ──────────────────────────────
# 实锤事故（生产日志）：归零/拾光/潮汐各被 doom 杀过一次 ——
# 「Doom loop detected: tool get_tasks called 3+ times with same args」。
# agent 没有订阅机制，轮询 get_tasks / read_file 是它获取状态的唯一手段，
# 按默认阈值 3 判定 doom 属于误杀。
# 以 tools/executor.py 实际注册的工具名为准（勿写 service 层方法名）：
DOOM_LOOP_READONLY_TOOLS: frozenset[str] = frozenset({
    # 任务账本 / 状态轮询 — agent 获取任务与进度的唯一通道
    "get_tasks",
    # 文件探索
    "read_file",
    "list_files",
    "grep",
    "search_files",
    # 组织 / 角色 / 技能查询
    "read_charter",
    "read_goals",
    "read_memory",
    "read_roster",
    "read_skill",
    "list_available_skills",
    "view_org_chart",
    "list_subordinates",
    "check_agent_status",
    "get_platform_state",
    "list_agent_templates",
    # 日志 / 告警查询
    "read_work_logs",
    "list_alarms",
    # 科学计算 — 纯函数幂等，心算错误代价高于重复计算
    "calculate",
    # worktree 只读查询
    "git_worktree_list",
    "git_worktree_status",
})
"""只读/幂等工具集合 — 同参重复调用不按 doom 阈值计数，只受保险丝约束。"""

DOOM_LOOP_READONLY_FUSE = 15
"""只读工具保险丝阈值 — 同参连续调用超过 15 次仍熔断。

豁免不等于放任：只读轮询每次往返都烧 token（请求+响应全量进上下文），
15 次同参连续调用已远超正常轮询节奏，熔断防 token 烧钱。
"""

DOOM_LOOP_TOOL_LIMITS: dict[str, int] = {
    # Status polling — tighter than generic readonly (TEST3/TEST4 poll storms)
    "check_agent_status": 5,
    "get_tasks": 6,
    # 审查工具 — 中容忍，重试可能是 LLM 纠正输出格式
    "run_code_review": 6,
    "run_security_audit": 6,
    "run_tests": 6,
    "run_perf_audit": 6,
    "run_full_review": 6,
    # 有成本审计 — 每次调用烧一次 LLM（父 agent 自己的模型），容忍有限重试
    "request_code_audit": 4,
    # H5 harness — one case may need retries; mid tolerance
    "game_run_case": 6,
    "game_run_case_main": 6,
    # 每轮强制出口 — 被出口闸门拒收后必须重试；同参指纹才计数
    # （井字棋实测：CEO 首条指令即撞 doom，无任何正常输出）
    "commit_turn": 8,
    # 幂等写入 — 中容忍，覆盖写入无害但不应无限重复
    "write_file": 8,
    "save_charter": 8,
    "save_goals": 8,
    "save_memory": 8,
    "update_roster": 8,
    "todowrite": 8,
    "mark_read": 8,
    "write_work_log": 8,
    "update_goals": 8,
    # 外发消息 — 低容忍，避免刷屏
    "send_message": 5,
    "question": 4,
    # 副作用工具 — 最低容忍，防止真实损害
    "bash": 3,
    "bash_main": 3,
    "python_script": 3,
    "job_kill": 3,
    "start_dev_server": 3,
    "stop_dev_server": 3,
    "apply_patch": 3,
    "spawn_subagent": 3,
    "websearch": 3,
    "execute_code": 3,
    # Paid Ark media — keep tight; retries burn quota
    "generate_image": 3,
}
"""Per-tool doom loop thresholds（写类/副作用工具）。不同工具不同限制：

- 审查工具 (6次): LLM 可能在纠正输出格式，需要更多尝试
- 幂等写入 (8次): 覆盖写入无害，但不应无限重复
- 外发消息 (4-5次): 避免对其他 agent 或用户造成骚扰
- 副作用工具 (3次): 真实命令执行，严格限制防止损害

只读工具不在此表 —— 见 DOOM_LOOP_READONLY_TOOLS + DOOM_LOOP_READONLY_FUSE。
"""


def doom_loop_limit(tool_name: str) -> int:
    """返回工具的 doom 阈值：显式表优先，只读工具走保险丝，其余默认 3。"""
    if tool_name in DOOM_LOOP_TOOL_LIMITS:
        return DOOM_LOOP_TOOL_LIMITS[tool_name]
    if tool_name in DOOM_LOOP_READONLY_TOOLS:
        return DOOM_LOOP_READONLY_FUSE
    return DOOM_LOOP_DEFAULT_LIMIT


def round_made_progress(
    tool_calls: list[dict],
    *,
    error_ids: set[str] | None = None,
    duplicate_ids: set[str] | None = None,
) -> bool:
    """True if this tool-loop round advanced work (DESIGN-2 stall counter).

    A round counts as progress when at least one non-readonly tool succeeded
    (not in error_ids / duplicate_ids). Pure readonly polling = no progress.
    """
    errs = error_ids or set()
    dups = duplicate_ids or set()
    for tc in tool_calls:
        name = tc.get("name") or ""
        tid = tc.get("id") or ""
        if name in DOOM_LOOP_READONLY_TOOLS:
            continue
        if tid in errs or tid in dups:
            continue
        return True
    return False


def _canonical_tool_args(args: object) -> str:
    """Canonical fingerprint for tool-call arguments (str-JSON or dict).

    P0-1：readonly stall 只用「参数是否重复」判 polling —— 读不同文件/
    不同参数轮差异必须体现在指纹里，否则合法阅读流会被误判为空转。
    """
    if isinstance(args, dict):
        try:
            return json.dumps(args, sort_keys=True, ensure_ascii=False)
        except Exception:
            return repr(args)
    if isinstance(args, str):
        try:
            obj = json.loads(args)
            return json.dumps(obj, sort_keys=True, ensure_ascii=False)
        except Exception:
            return args
    return repr(args)


def readonly_fingerprint(tc: dict) -> tuple[str, str] | None:
    """``(tool_name, canonical_args)`` for a readonly tool call, else ``None``."""
    name = tc.get("name") or ""
    if name not in DOOM_LOOP_READONLY_TOOLS:
        return None
    return name, _canonical_tool_args(tc.get("arguments"))


def round_was_readonly_only(
    tool_calls: list[dict],
    *,
    error_ids: set[str] | None = None,
    duplicate_ids: set[str] | None = None,
) -> bool:
    """True if the round had only successful readonly tools (or was empty).

    Used to apply TOOL_LOOP_READONLY_STALL_LIMIT instead of the stricter
    mutating stall limit.
    """
    if not tool_calls:
        return True
    errs = error_ids or set()
    dups = duplicate_ids or set()
    saw_ok_readonly = False
    for tc in tool_calls:
        name = tc.get("name") or ""
        tid = tc.get("id") or ""
        if tid in errs or tid in dups:
            return False
        if name not in DOOM_LOOP_READONLY_TOOLS:
            return False
        saw_ok_readonly = True
    return saw_ok_readonly


def fail_signature_for_round(
    tool_calls: list[dict],
    error_ids: set[str] | None = None,
    duplicate_ids: set[str] | None = None,
) -> tuple[str, str] | None:
    """本轮第一个失败调用 ``(tool_name, args 指纹前 60 字符)``；None = 无失败。

    P0-1（R3 收敛判据）：tool-failed stall 是否「原地踏步」—— 同工具+同参
    失败 = 同源（撞同一面墙），继续计入 2 轮快速收口；换参/换工具失败 =
    方向在变（试错），不得按 2 轮快速收口（R2/R3 实证：pytest 反复
    no-cacheprovider/basetemp/icacls 全是被试错判死）。
    """
    errs = error_ids or set()
    dups = duplicate_ids or set()
    for tc in tool_calls:
        tid = tc.get("id") or ""
        if tid in errs or tid in dups:
            name = tc.get("name") or ""
            args = _canonical_tool_args(tc.get("arguments"))
            return name, args[:60]
    return None


def classify_stall_round(
    tool_calls: list[dict],
    *,
    error_ids: set[str] | None = None,
    blocked_ids: set[str] | None = None,
    duplicate_ids: set[str] | None = None,
    seen_readonly_fingerprints: set[tuple[str, str]] | None = None,
) -> str | None:
    """本轮 stall 归因，``None`` = 本轮有进展。

    归因顺序对齐 DSH 戒律「先查 runner/tool failure 再判 denial/无进展」
    （bash-sandbox/src/index.ts:107 "Runner failure outranks denial because
    the command did not run"）：工具没跑成的轮次，既不是模型空转也不是护栏
    拒绝，必须先摘出去 —— 否则文案会让模型去反省它并没犯的错。

    P0-1：readonly 归因只在「本轮只读调用命中历史重复指纹」时返回 ——
    连续读**不同**文件/不同参数的合法只读工作流视为有进展（``None``），
    stall 阈值只对同参轮询生效（``seen_readonly_fingerprints`` 由调用方
    逐轮维护，注意调用方应在判定**之后**才把本轮新指纹并入历史）。
    """
    if round_made_progress(
        tool_calls, error_ids=error_ids, duplicate_ids=duplicate_ids
    ):
        return None
    errs = error_ids or set()
    blocked = blocked_ids or set()
    # 39 审计 P2-1：submit 门禁按设计拒收（负样本演练/正常打回）→ 正交事实位
    # gate_reject，不计入 tool_failed 连败（演练语义：被拒 = 成功）。仅当本轮
    # 全部失败都是门禁拒收时才归此类；与其它失败混轮仍归 tool_failed。
    gate_calls = [
        tc for tc in tool_calls
        if (tc.get("id") or "") in errs
        and (tc.get("name") or "") in GATE_REJECT_TOOLS
    ]
    if errs and gate_calls and len(gate_calls) == len(errs):
        return STALL_REASON_GATE_REJECT
    if errs - blocked:
        return STALL_REASON_TOOL_FAILED
    if errs and blocked >= errs:
        return STALL_REASON_BLOCKED
    if round_was_readonly_only(
        tool_calls, error_ids=error_ids, duplicate_ids=duplicate_ids
    ):
        # 空轮（无调用）保持历史语义：归 readonly 累计，与"纯只读轮询"同收口。
        if not tool_calls:
            return STALL_REASON_READONLY
        seen = seen_readonly_fingerprints
        if seen is not None:
            repeated = False
            for tc in tool_calls:
                tid = tc.get("id") or ""
                if tid in errs or tid in (duplicate_ids or set()):
                    continue
                fp = readonly_fingerprint(tc)
                if fp is not None and fp in seen:
                    repeated = True
            if not repeated:
                # 本轮只读调用的参数全部是新的 → 合法阅读/巡检，不算空转。
                return None
        return STALL_REASON_READONLY
    return STALL_REASON_NO_PROGRESS

