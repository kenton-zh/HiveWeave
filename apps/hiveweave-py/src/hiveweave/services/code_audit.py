"""Code-audit domain core behind the ``request_code_audit`` tool.

Agent-level (key=agent_id) in-memory change ledger + worktree diff
collection (incl. untracked files) + one-shot LLM audit + attestation +
report file.

The HTTP path is the same one-shot sub-call as the legacy review suites
(``agents/agent.py:_oneshot_llm``). Model pick (2026-08-16): use a
**teammate's currently resolved model** whose vendor ``model_id`` differs
from the author's. Same callback stack — not a second LLM runtime. If
the live team only has one family, fall back to the author's own model
(do not invent unused catalog / backup slots).

Soft-gate by design: nothing here raises or blocks — every step degrades
to a soft-fail dict with a machine-readable ``reason``.
"""
from __future__ import annotations

import asyncio
import json
import re
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import structlog

from hiveweave.services.model import NoModelConfiguredError

log = structlog.get_logger(__name__)

# 41+08 双跑 P0-2：审计 LLM 无并发整形——15 人并发下 llm_failed 19.5%
# （另一项目同期仅 4%）。闸门限流 + 失败不重试风暴。
_AUDIT_LLM_CONCURRENCY = max(1, int(os.environ.get("HIVEWEAVE_AUDIT_LLM_CONCURRENCY", "2") or "2"))
_audit_llm_gate: asyncio.Semaphore | None = None


# ── 三态契约（TEST_DSH_55 取证，2026-09-14）────────────────────────────
# 为什么必须有三态：34 次调用全报 success ⇒ ``run_steps.status`` 全
# ``'completed'``，其中 11 次（32%）结果文本写的却是「审计未执行: llm_failed」
# ⇒ agent 读到「成功」却拿不到凭证，**重发 11 次**。旧文案里写着「无需循环
# 重试」——说不服，因为它看到的**信号**是成功。
#
# 判据（借 deepseek-harness ``SubagentResult`` 三条判据，**不借**其字段名/
# 枚举值；对位物是我们自己的 ``success`` / ``fact`` / 结果契约）：
#   ① 非 completed 的终止原因 ⇒ 不得当作成功上报；
#   ② 诊断信息与产出分离（走 error + 结构化字段，不塞进 output 让消费者猜）；
#   ③ 未知终止原因按失败处理（fail-closed）。
AUDIT_STATE_READY = "ready"
AUDIT_STATE_ACCEPTED_PENDING = "accepted_pending"
AUDIT_STATE_FAILED = "failed"

#: 第三态的语义标记：等待期间 agent 的动作是「等通知」，不是「重发」。
#: 由工具壳写进结果契约（``wait_for_notice``），门禁 / UI / 后续消费者直接
#: 读这个信号，无需解析回执文本。
AUDIT_WAIT_FOR_NOTICE = True


def audit_outcome_state(result: dict) -> str:
    """三态判据**唯一入口**：``run_code_audit`` 的返回 dict → 状态。

    - ``audited=True`` **且有凭证** ⇒ ready
    - 已入队且未耗尽（含"耗尽后人工重试 → 新一轮重试序列"）⇒ accepted_pending
    - 其余 ⇒ failed。**fail-closed**（判据③）：判定不出的终局一律按失败，
      绝不回落到「当成功」。

    ⚠ ``ready`` **必须同时要求 `attestation_id` 存在**（独立审计 P0，2026-09-14）：
    有出口会 ``audited=True`` 但**不发凭证** —— 典型是 ``ROLLED_BACK``
    （diff 已回滚，回执自己写着「不发新的 PASS 凭证」）。只判 ``audited``
    会让它落到 ready ⇒ ``success=True`` ⇒ ``round_made_progress`` 判真
    （``doom_loop.py``）⇒ ``tool_loop`` 清零全部 stall 计数 ⇒ **同源无限重发**
    ——正是本批要治的病，在另一个出口原样存在。
    依据：三个真发凭证的出口（PASS / 缓存命中 / 常规）**都带 `attestation_id`**，
    故该条件不会误伤正常路径（守卫见 tests/test_code_audit_rolled_back_state.py）。
    """
    if result.get("audited") and result.get("attestation_id"):
        return AUDIT_STATE_READY
    if result.get("retry_queued") and not result.get("retry_exhausted"):
        return AUDIT_STATE_ACCEPTED_PENDING
    return AUDIT_STATE_FAILED


def audit_failure_fact(result: dict) -> str:
    """``failed`` 态的事实位判据**唯一入口**：``reason`` → 四格词表成员。

    只服务 ``AUDIT_STATE_FAILED``（``ready`` / ``accepted_pending`` 由
    :func:`audit_outcome_state` 处置，不进这里）。返回值是
    ``tools/result.py`` 的 ``FactKind`` 字面值；此处**不 import** ``FactKind``
    —— ``services`` 不依赖 ``tools``（模块 docstring 的既有拓扑），翻译由
    工具壳 ``_soft_fail_result`` 完成。

    判据：**兜底 except 的触发点在 ``run_code_audit`` 执行之后**
    （``reason == "error"`` 的出口），审计可能已部分跑过 / 已有副作用 ⇒ 标
    ``outcome_unknown``（「结果未知 · 不许盲目重试」）。若标 ``runner_failed``
    （=「命令从未执行」），下游会读成「无副作用，可安全重试」⇒ **副作用双发**
    ——这正是 ``tools/result.py`` 词表与 ``fact_positions.py`` 兜底格
    （同为 ``outcome_unknown``）反复钉的纪律。

    其余 ``failed`` 原因（``no_worktree`` / ``no_callback`` / ``no_model`` /
    ``llm_failed`` 未入队 / 重试耗尽）都是**平台前提缺失**：审计从未产出结论
    ⇒ ``runner_failed``（「站在 agent 视角不是你的 bug」）。
    """
    reason = str(result.get("reason") or "")
    if reason == "error":
        return "outcome_unknown"
    return "runner_failed"


def _get_audit_gate() -> asyncio.Semaphore:
    global _audit_llm_gate
    if _audit_llm_gate is None:
        _audit_llm_gate = asyncio.Semaphore(_AUDIT_LLM_CONCURRENCY)
    return _audit_llm_gate

# s3-clone_06 P0-1：45s 帽是「审计为软门」时代的取舍——那时超时只意味着
# 「没审计也放行」，宁可快失败。P0-3（fail-loud）之后语义反转：超时 =
# 提交被门禁拦下或走显式 waive 流程，代价比多等 60 秒高得多。实测本项目
# 成功调用耗时 27–41s、失败全部精确停在 452xx ms（撞 45s 顶），说明审计
# 本就贴着上限跑，上游一抖即全灭（34 次 27 次 llm_failed = 79%）。
#
# **实际可达上限（审计 [2]，勿再自欺）**：本值只是外层 ``asyncio.wait_for``
# 的帽，真正决定成败的是 agents.agent._review_llm_post_with_retry：
#   首读固定 ``_REVIEW_LLM_READ_TIMEOUT_MAX_S``（90s）→ 重试窗
#   ``_REVIEW_LLM_RETRY_WINDOW_S``（45s）→ 首读超时后 remaining = 45-90 < 0
#   直接上抛，第二次尝试根本不会发生。
# 因此 > 90s 的配置**不可达**（调 env 到 300 也只跑 90s）。这里夹到
# agent 侧的真实帽子（取不到则按 90 兜底），并在提示文案里用有效值，
# 杜绝"改了配置没变化"的假象。想真正放宽须改 agent 侧首读帽/重试窗。
def _timeout_from_env(name: str, default: int) -> int:
    """环境变量读取超时秒数——非法值回退默认，绝不在**模块导入期**抛异常。

    ``int(os.environ.get(...))`` 遇到 "60s"/"abc"/"" 会 ValueError，而这是
    模块级常量：一炸就是整个 code_audit 服务不可用（连带 submit 门禁）。
    """
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        log.warning("code_audit.bad_timeout_env", key=name, value=raw, default=default)
        return default
    return value if value > 0 else default


CODE_AUDIT_LLM_TIMEOUT_S = _timeout_from_env("HIVEWEAVE_CODE_AUDIT_TIMEOUT_S", 120)


def effective_audit_timeout_s() -> float:
    """审计实际能等到的秒数 = 请求值 ∩ agent 侧首读帽。

    agent 侧重试助手的首读帽是硬顶（见上注）：外层 wait_for 再大也没用。
    这里**运行时**读取该帽子（不在模块导入期 import agents，避免
    services ↔ agents 循环导入），取不到时按 90 兜底。
    """
    try:
        from hiveweave.agents.agent import (  # noqa: PLC0415 — 见上：避免循环导入
            _REVIEW_LLM_READ_TIMEOUT_MAX_S as cap,
        )

        return float(min(CODE_AUDIT_LLM_TIMEOUT_S, cap))
    except Exception:  # noqa: BLE001 — best-effort，取不到用兜底值
        return float(min(CODE_AUDIT_LLM_TIMEOUT_S, 90))

# One-shot review callback contract — same shape as
# ``agents/agent.py:_review_llm_callback`` / tools/review.py ReviewLLMCallback.
# Declared locally (not imported from tools.review) to avoid a tools↔services
# package import cycle.
ReviewLLMCallback = Callable[[str, str], Awaitable[str]]
# (model_config, system, user) -> text. Same HTTP path, caller-chosen config.
OneshotLLMCallback = Callable[[dict, str, str], Awaitable[str]]

CODE_AUDIT_LINE_THRESHOLD = 20
CODE_AUDIT_KIND = "code_audit"
CODE_AUDIT_POLICY = (
    "[CODE AUDIT POLICY] If your code edits total more than 20 lines, "
    "call request_code_audit(taskId=...) BEFORE submit_task."
)
CODE_AUDIT_REMINDER = (
    "[CODE AUDIT REMINDER] Your code edits exceed 20 lines and no fresh "
    "code audit attestation exists. Call request_code_audit(taskId=...) "
    "before submit_task to get a second-pass LLM audit of your worktree diff."
)
# P0-3（fail-loud）后 llm_failed 不再"静默放行"：code_audit 仍是必需证据，
# submit 会被门禁拦下。审计 epic（42 轮报告）：llm_failed 全是分钟级上游
# 暂态，旧文案教 agent「反复失败就请 coordinator waive」直接导致 16 次
# llm_failed → 全部走 waive、6 次级联。新口径：平台自动排队重试并回填
# 通知，agent 等待即可；多次自动重试仍失败才考虑 waive（真实人工决策）。
CODE_AUDIT_REMINDER_LLM_FAILED = (
    "[CODE AUDIT REMINDER] Code audit was attempted but the LLM call failed "
    "(llm_failed). This is NOT missing evidence on your side — the platform "
    "auto-enqueues a background retry and will notify you in your inbox "
    "when it succeeds (audit takes 30-90s on real diffs; timeout cap "
    "{timeout_s}s). Wait for the retry notice instead of looping "
    "request_code_audit. Only if repeated auto-retries still fail, a "
    "coordinator may run waive_attestation(taskId=..., reason=...) — a "
    "real human decision, not your default escape. Silently submitting "
    "will be rejected."
)
CODE_AUDIT_REMINDER_ATTEMPTED = (
    "[CODE AUDIT REMINDER] Code audit was attempted but did not produce an "
    "attestation ({reason}). Retry request_code_audit(taskId=...) once; if it "
    "still fails, submit stays BLOCKED until a coordinator runs "
    "waive_attestation(taskId=..., reason=...)."
)
_SOFT_FAIL_ATTEMPT_REASONS = frozenset({"llm_failed", "no_model", "no_callback"})

_DIFF_CAP_BYTES = 50 * 1024
_DIFF_CAP_MARKER = "\n... [diff truncated at 50KB] ...\n"
_TOP_ISSUES_MAX = 5
_TOP_ISSUE_MAX_CHARS = 120
_ISSUE_PARSE_CAP = 100


# ── Agent-level in-memory ledger ─────────────────────────────

_ledger: dict[str, int] = {}
_ledger_ts: dict[str, float] = {}
# Last request_code_audit attempt that did not produce an attestation.
# Separate from the line ledger so llm_failed is not mistaken for "never audited".
_last_attempt: dict[str, dict[str, Any]] = {}


def record_change(agent_id: str, lines: int) -> None:
    """Accumulate edited-line counts for an agent (values clamped at >= 0)."""
    if lines <= 0:
        return
    _ledger[agent_id] = max(0, _ledger.get(agent_id, 0)) + lines
    _ledger_ts[agent_id] = time.time()


def get_unaudited_lines(agent_id: str) -> int:
    """Ledger value for an agent (0 when absent)."""
    return max(0, _ledger.get(agent_id, 0))


def get_last_change_ts(agent_id: str) -> float:
    """Epoch-seconds timestamp of the last recorded edit (0 when absent)."""
    return _ledger_ts.get(agent_id, 0.0)


def record_audit_attempt(
    agent_id: str, reason: str, task_id: str | None = None
) -> None:
    """Record a soft-fail audit attempt (no attestation created)."""
    if not agent_id:
        return
    _last_attempt[agent_id] = {
        "ts": time.time(),
        "reason": reason,
        "task_id": task_id,
    }


def get_last_audit_attempt(agent_id: str) -> dict[str, Any] | None:
    """Copy of the last soft-fail attempt, or None."""
    rec = _last_attempt.get(agent_id)
    return dict(rec) if rec else None


def _task_ref_match(left: str | None, right: str | None) -> bool:
    """Same task id, including dashed vs compact / prefix stubs."""
    if not left or not right:
        return False
    a = str(left).replace("-", "").strip().lower()
    b = str(right).replace("-", "").strip().lower()
    if not a or not b:
        return False
    if a == b:
        return True
    return len(a) >= 8 and len(b) >= 8 and (a.startswith(b) or b.startswith(a))


def code_audit_soft_fail_covers(agent_id: str, task_id: str | None) -> bool:
    """True when this agent already attempted audit on *task_id* and soft-failed.

    **只表示"有过失败尝试记录"**，不代表放行 —— P0-3（fail-loud）之后本谓词
    服务于 ``code_audit_soft_fail_pending`` 的**拦截**判定：命中即说明该
    agent 在本任务上审计没跑成，submit 会被门禁拦下（出路：重试到成功，或
    coordinator 显式 waive）。旧 docstring「do not block submit」是 fail-loud
    之前的语义残留，勿再据此推断（审计 [1]）。
    """
    rec = get_last_audit_attempt(agent_id)
    if not rec:
        return False
    if rec.get("reason") not in _SOFT_FAIL_ATTEMPT_REASONS:
        return False
    rec_tid = rec.get("task_id")
    if task_id and rec_tid:
        return _task_ref_match(str(task_id), str(rec_tid))
    # Unbound attempt (0/N active tasks at request_code_audit) covers
    # this agent's next submit. Strict match only when both ids exist.
    if task_id and not rec_tid:
        return True
    return not rec_tid


CODE_AUDIT_SOFT_FAIL_EVIDENCE_KEY = "code_audit_soft_fail"


def evidence_has_code_audit_soft_fail(evidence: dict | None) -> bool:
    """True when submit stamped a structured code-audit soft-fail on evidence.

    ⚠️ 安全警示（F1 修复）：``evidence`` 在 HTTP 路径是**客户端原文**
    （``api/tasks.py`` 的 ``TaskSubmit.evidence``），任何能调 submit/approve 的
    调用方都能伪造 ``{"code_audit_soft_fail": {"reason": "llm_failed"}}`` 骗过
    门禁。因此本函数**不得**再作为任何门禁的放行判据使用——门禁侧一律走
    服务端复核 ``resolve_soft_fail_kind``（读 ``tool_attestations`` 平台落库
    行）。此处保留为纯判读工具（UI/兼容 + 回归测试的可观测面），不参与放行。
    """
    if not isinstance(evidence, dict):
        return False
    stamp = evidence.get(CODE_AUDIT_SOFT_FAIL_EVIDENCE_KEY)
    if not isinstance(stamp, dict):
        return False
    return str(stamp.get("reason") or "") in _SOFT_FAIL_ATTEMPT_REASONS


async def drop_code_audit_kind_if_soft(
    needed: frozenset[str] | None,
    project_id: str,
    *,
    agent_id: str | None = None,
    task_id: str | None = None,
    evidence: dict | None = None,
) -> tuple[frozenset[str] | None, bool]:
    """Approve/HTTP-time compat: drop ``CODE_AUDIT_KIND`` only on a
    **server-verified** platform fact row.

    两条同构事实位，**都只认 ``tool_attestations`` 平台落库行**：

    1. ``code_audit_soft_fail``（F1）——submit 门禁判定软失败时由平台签发。
       此前本分支读客户端 ``evidence`` 盖章（``evidence_has_code_audit_soft_fail``）：
       HTTP 路径的 ``evidence`` 是 ``api/tasks.py`` 的 ``TaskSubmit.evidence``
       **客户端原文**，任何调用方自带
       ``{"code_audit_soft_fail": {"reason": "llm_failed"}}`` 即可跳过 code_audit
       门（llm_failed / no_model / no_callback 三值全中）⇒ 零 attestation 过门。
       现改为服务端复核 ``resolve_soft_fail_kind(project_id, task_id)``。evidence
       盖章仍由 submit 写入（UI/兼容用），但**不再是门禁判据**。
    2. ``attestation_impossible``（报告 Layer 6 第 4 行）——
       ``resolve_impossible_kind(project_id, task_id)`` 服务端复核。

    **fail-closed**：拿不到平台事实（无行 / 查询失败 / 解析不出）⇒ 不剔除、
    保持原门禁。``project_id`` 是**必需参数**——复核在漏斗内部完成，调用方无法
    「忘记传参」退回旧洞。``agent_id`` / ``evidence`` 仅为签名兼容保留，不参与
    判定。

    **F3**：剔除的 kind 固定为 ``CODE_AUDIT_KIND``，要求解析出的 need **恰等于**
    ``CODE_AUDIT_KIND`` 才剔除——不把「剔哪个 kind」交给事实行内容。

    P0-3 (TEST_DSH_38) fail-loud: an in-memory llm_failed attempt never drops the
    kind at submit.
    """
    if not needed or CODE_AUDIT_KIND not in needed:
        return needed, False
    # Lazy import: avoid a services↔services module-level import cycle
    # (attestation.py lazily imports this module too).
    from hiveweave.services.attestation import (
        resolve_impossible_kind,
        resolve_soft_fail_kind,
    )

    soft_need = await resolve_soft_fail_kind(project_id, task_id)
    if soft_need == CODE_AUDIT_KIND:
        return frozenset(k for k in needed if k != CODE_AUDIT_KIND), True
    impossible_need = await resolve_impossible_kind(project_id, task_id)
    if impossible_need == CODE_AUDIT_KIND:
        return frozenset(k for k in needed if k != CODE_AUDIT_KIND), True
    return needed, False


def code_audit_soft_fail_pending(
    needed: frozenset[str] | None,
    agent_id: str | None,
    task_id: str | None,
) -> bool:
    """Submit-time detection: code_audit required and only soft-fail coverage.

    True means the submit gate must NOT silently pass — the agent must retry
    request_code_audit until an attestation exists, or obtain an explicit
    coordinator waive_attestation (logged, 24h expiry).
    """
    if not needed or CODE_AUDIT_KIND not in needed:
        return False
    if not agent_id:
        return False
    return code_audit_soft_fail_covers(agent_id, task_id)


def code_audit_submit_reminder(agent_id: str) -> str:
    """Submit-time reminder when edits exceed the threshold and no fresh attestation.

    llm_failed / no_model / no_callback are worded as attempted-but-failed,
    not "never audited".
    HTTP retry already lives in ``agents.agent._review_llm_post_with_retry`` —
    do not double-retry here.
    """
    attempt = _last_attempt.get(agent_id)
    reason = (attempt or {}).get("reason")
    if reason == "llm_failed":
        # 报**有效**帽（≤ agent 侧首读帽），不报配置值——否则 Agent 会以为
        # 还有余量而盲目重试。
        _eff = effective_audit_timeout_s()
        _shown = int(_eff) if float(_eff).is_integer() else _eff
        return CODE_AUDIT_REMINDER_LLM_FAILED.format(timeout_s=_shown)
    if reason in _SOFT_FAIL_ATTEMPT_REASONS:
        return CODE_AUDIT_REMINDER_ATTEMPTED.format(reason=reason)
    return CODE_AUDIT_REMINDER


def reset_ledger(agent_id: str) -> None:
    """Clear the ledger entry and last audit attempt for an agent."""
    _ledger.pop(agent_id, None)
    _ledger_ts.pop(agent_id, None)
    _last_attempt.pop(agent_id, None)


def ledger_snapshot() -> dict[str, int]:
    """Test helper — copy of the current ledger state."""
    return dict(_ledger)


# ── Line counting (parameter heuristic, language-agnostic) ───

def count_change_lines(tool_name: str, params: dict) -> int:
    """Estimate edited lines from tool params (no output-text parsing).

    write_file → content line count; edit_file → max(old, new);
    apply_patch → sum per op (add: content; update: max(old, new); delete: 0).
    Unknown tool / missing values → 0.
    """
    def _split_lines(value: object) -> int:
        if value is None:
            return 0
        return len(str(value).splitlines())

    def _first(params: dict, *keys: str) -> object:
        for key in keys:
            value = params.get(key)
            if value is not None:
                return value
        return None

    if not params:
        return 0
    if tool_name == "write_file":
        return _split_lines(params.get("content"))
    if tool_name == "edit_file":
        return max(
            _split_lines(_first(params, "old_string", "oldString", "old_str", "oldText", "search")),
            _split_lines(_first(params, "new_string", "newString", "new_str", "newText", "replacement")),
        )
    if tool_name == "apply_patch":
        total = 0
        for patch in params.get("patches") or []:
            if not isinstance(patch, dict):
                continue
            op = (patch.get("op") or "").strip().lower()
            if op == "add":
                total += _split_lines(patch.get("content"))
            elif op == "update":
                total += max(
                    _split_lines(_first(patch, "old_string", "oldString", "old_str", "oldText", "search")),
                    _split_lines(_first(patch, "new_string", "newString", "new_str", "newText", "replacement")),
                )
            # delete / unknown ops count 0
        return total
    return 0


# ── Worktree diff collection ─────────────────────────────────

async def _run_git(args: list[str], worktree_path: str) -> tuple[bool, str]:
    """Lazy-imported ``_git`` (patchable at the package level, no cycles)."""
    from hiveweave.services.git_worktree import _git

    try:
        return await _git(args, worktree_path)
    except Exception as exc:
        log.warning("code_audit.git_failed", error=str(exc))
        return False, ""


async def _resolve_base_branch_lazy(worktree_path: str) -> str | None:
    """Resolve the default base branch (main → master). Fail-open → None."""
    try:
        from hiveweave.services.git_worktree import _resolve_base_branch

        return await _resolve_base_branch(worktree_path)
    except Exception as exc:  # noqa: BLE001 — soft-fail contract
        log.warning("code_audit.base_branch_failed", error=str(exc))
        return None


async def collect_worktree_diff(worktree_path: str) -> str:
    """Collect the worktree diff: <base>...HEAD, HEAD (uncommitted), untracked.

    Base branch is resolved at runtime (main → master fallback) so repos
    whose default branch is not ``main`` still get their committed branch
    changes audited. Untracked files are listed via
    ``git ls-files --others --exclude-standard`` and their content read
    directly — otherwise brand-new files would render an empty diff and get
    auto-PASSed. Total output capped at ~50KB with a truncation marker.
    Empty-safe; never raises.
    """
    parts: list[str] = []
    budget = _DIFF_CAP_BYTES

    base = await _resolve_base_branch_lazy(worktree_path)
    if base:
        ok, out = await _run_git(["diff", f"{base}...HEAD"], worktree_path)
        if ok and out:
            parts.append(f"== diff {base}...HEAD ==")
            parts.append(out)
    ok, out = await _run_git(["diff", "HEAD"], worktree_path)
    if ok and out:
        parts.append("== diff HEAD (uncommitted) ==")
        parts.append(out)

    ok, files = await _run_git(
        ["ls-files", "--others", "--exclude-standard"], worktree_path
    )
    if ok and files:
        for rel in files.splitlines():
            rel = rel.strip()
            if not rel:
                continue
            header = f"== untracked {rel} =="
            path = Path(worktree_path) / rel
            try:
                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read(max(0, budget))
            except OSError as exc:
                parts.append(f"== untracked {rel} (unreadable: {exc}) ==")
                continue
            parts.append(header)
            parts.append(content)
            budget -= len((header + "\n\n" + content).encode("utf-8", errors="replace"))
            if budget <= 0:
                break

    text = "\n\n".join(parts)
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) > _DIFF_CAP_BYTES:
        keep = _DIFF_CAP_BYTES - len(_DIFF_CAP_MARKER.encode("utf-8"))
        text = encoded[:keep].decode("utf-8", errors="replace") + _DIFF_CAP_MARKER
    return text


# ── Audit prompt (platform English constants) ────────────────

def build_audit_prompt(
    diff: str,
    task_id: str | None,
    acceptance_criteria: list[str] | None = None,
    appeal_notes: str | None = None,
) -> tuple[str, str]:
    """Build (system, user) prompt pair for the audit call.

    Platform English constants only (protocol, language-agnostic). The
    model's reply MUST start with ``VERDICT: PASS`` or ``VERDICT: ISSUES``.

    审计 epic P0-2（42 轮报告）：审计 LLM 此前只喂 diff 与裸任务 ID，规格
    强制的行为（如默认 admin token）被反复标 [high]，合法实现只能豁免。
    这里注入两节上下文（取不到就跳过该节）：
      - 任务验收标准：规格/验收标准要求的行为不是缺陷，对照判断。
      - 作者申诉：仅参考，审计须独立核实，不因申诉自动放行。
    """
    system = (
        "You are a second-pass code reviewer auditing an agent's worktree "
        "diff. Assess correctness, security, and obvious defects. "
        "Behavior that the task spec / acceptance criteria explicitly "
        "requires is NOT a defect — judge against the acceptance criteria "
        "when they are provided. "
        "Your reply MUST start with exactly one line: 'VERDICT: PASS' or "
        "'VERDICT: ISSUES'. When the verdict is ISSUES, follow with one "
        "line per problem in the form: <file>:<line> [<severity>] <one-line "
        "reason>, severity in high/medium/low. Do not use markdown fences."
    )
    context = (
        f"task context: {task_id}" if task_id else "task context: not provided"
    )
    sections = [context]
    criteria = [
        str(c).strip() for c in (acceptance_criteria or []) if str(c).strip()
    ]
    if criteria:
        bullets = "\n".join(f"- {c}" for c in criteria[:20])
        sections.append(
            "任务验收标准（spec/acceptance criteria 要求的行为不是缺陷，"
            f"对照判断）:\n{bullets}"
        )
    appeal = str(appeal_notes or "").strip()
    if appeal:
        sections.append(
            "作者申诉（仅参考，审计须独立核实，不因申诉自动放行）:\n"
            + appeal[:4000]
        )
    return system, "\n\n".join(sections) + f"\n\nworktree diff:\n{diff}"


async def load_task_acceptance_criteria(
    project_id: str, task_id: str | None
) -> list[str] | None:
    """任务验收标准（tasks.acceptance_criteria JSON 列，list[str]）。

    审计 epic P0-2：给审计 LLM 对照规格判断。Fail-open：任务不存在 /
    解析失败 / 非列表 → None（prompt 跳过该节）。绝不 raise。
    """
    if not task_id:
        return None
    try:
        from hiveweave.services.task import TaskService

        row = await TaskService().get_task(project_id, str(task_id))
        raw = (row or {}).get("acceptance_criteria")
        if isinstance(raw, str):
            raw = json.loads(raw)
        if not isinstance(raw, list):
            return None
        out = [str(x) for x in raw if str(x or "").strip()]
        return out or None
    except Exception:  # noqa: BLE001 — 审计 prompt 上下文是 best-effort
        return None


def _parse_verdict(text: str) -> str:
    """First line must be ``VERDICT: PASS``; anything else is ISSUES."""
    lines = (text or "").strip().splitlines()
    if not lines:
        return "ISSUES"
    first = lines[0].strip().upper()
    return "PASS" if first.startswith("VERDICT: PASS") else "ISSUES"


def _parse_issues(text: str) -> list[str]:
    """Issue lines after the verdict line (bullets stripped, capped)."""
    issues: list[str] = []
    for line in (text or "").splitlines()[1:]:
        stripped = line.strip().lstrip("-*•").strip()
        if not stripped or stripped.upper().startswith("VERDICT"):
            continue
        issues.append(stripped[:_TOP_ISSUE_MAX_CHARS])
        if len(issues) >= _ISSUE_PARSE_CAP:
            break
    return issues


# ── #12：severity 解析（fail-safe 的判据来源）─────────────────────
#
# ⚠ 这两条**仍然是文本判据** —— 计划 §三 #12 已澄清：真正止血的是
# **fail-safe 语义**（"未知 ⇒ 按 high 拦"），不是"把 `[high]` 换成 `SEVERITY:`"
# 这个形式本身。形式的价值只在于：`None` 成了**明确的"未知"**，从而让调用方
# 能把"未知"当 high，而不是像旧的 `"[high]" in issue` 那样把"未知"静默当
# "不是 high"（fail-open）。
_SEVERITY_PREFIX_RE = re.compile(
    r"SEVERITY\s*[:：]\s*(high|medium|low)\b", re.IGNORECASE
)
#: 旧括号形态 —— **影子期保留兼容**：此刻真闸门用的还是它（`"[high]" in issue`），
#: 一旦让模型改写新前缀而闸门还没切，闸门就瞎了。切"真拦"时一并退场。
#: ⚠ **只认 ASCII 方括号**，与旧判据的"已识别集合"**严格一致**：
#: 全角 `【high】` / 中文 `高` / 法文 一律**落 `unparsed`**，由 fail-safe 兜住 ——
#: 那正是旧判据漏掉、我们要量的那一批。若在这里宽容地认下全角形态，
#: `unparsed` 就会**少算**，shadow 的结论跟着偏乐观。
_SEVERITY_BRACKET_RE = re.compile(r"\[(high|medium|low)\]", re.IGNORECASE)


def parse_issue_severity(issue: str) -> str | None:
    """解析一条 issue 的 severity；**解析不出 ⇒ None**（"未知"，交调用方 fail-safe）。

    判定顺序（**只认首个**，其后文本一律忽略）：
      ① 显式前缀 ``SEVERITY:high|medium|low``（全/半角冒号皆可）；
      ② 旧括号形态 ``[high]`` / ``【high】``（兼容期）；
      ③ 都没有 ⇒ ``None``。

    ⚠ ①命中即返回 —— 这正是计划要的冲突规则：``SEVERITY:low … 【high】`` 按 **low**，
      后面的 `【high】` 不再影响判定（`severity_conflict()` 会把这次"忽略后文"报出来）。
    """
    text = issue or ""
    m = _SEVERITY_PREFIX_RE.search(text)
    if m:
        return m.group(1).lower()
    m = _SEVERITY_BRACKET_RE.search(text)
    if m:
        return m.group(1).lower()
    return None


#: **宽口径**的 severity 标记 —— 只给 :func:`severity_conflict` 用，**不参与识别**。
#: 为什么两个模式职责不同：
#:   · 识别集（`_SEVERITY_PREFIX_RE` / `_SEVERITY_BRACKET_RE`）要**严格** ——
#:     它决定 `unparsed`，宽一分就少算一分真实的"未知率"；
#:   · 冲突检测要**宽** —— 它的职责是"**注意到并上报**后文里还有别的 severity 说法
#:     被忽略了"，而不是替谁做判定。宽口径漏报，日志就变成沉默。
#: 计划 §三 #12 举的例子正是全角 `【high】`，所以这里必须含全角与中文档位。
_SEVERITY_ANY_MARKER_RE = re.compile(
    r"[\[【(（]\s*(high|medium|low|高|中|低)\s*[\]】(）)]", re.IGNORECASE
)


def severity_conflict(issue: str) -> bool:
    """该条是否"首个 SEVERITY: 前缀与后文标记**不一致**"（**只用于留痕**）。

    ⚠ 用**宽口径** `_SEVERITY_ANY_MARKER_RE` 扫后文：目的是把"我们忽略了后文"
    这件事**报出来**（验收④要求有日志），不是再做一次判定。判定早已由
    :func:`parse_issue_severity` 的"只认首个前缀"定下。
    """
    text = issue or ""
    m = _SEVERITY_PREFIX_RE.search(text)
    if not m:
        return False
    prefix_level = m.group(1).lower()
    rest = text[m.end():]
    b = _SEVERITY_ANY_MARKER_RE.search(rest)
    return bool(b and b.group(1).lower() != prefix_level)


def count_issue_severities(issues: list[str]) -> dict[str, int]:
    """→ ``{high, medium, low, unparsed, conflicts}``。

    ``unparsed`` = **fail-safe 会新拦下来**的那一批（旧判据对它们视而不见）。
    """
    out = {"high": 0, "medium": 0, "low": 0, "unparsed": 0, "conflicts": 0}
    for issue in issues:
        level = parse_issue_severity(issue)
        if level is None:
            out["unparsed"] += 1
        else:
            out[level] += 1
        if severity_conflict(issue):
            out["conflicts"] += 1
    return out


def _parse_cached_issues(raw: object) -> list[str]:
    """audit_cache.top_issues（JSON 数组字符串或 list）→ 非空 issue 行列表。

    审计 epic P0-1：缓存回放原结论的 issue 行。任何解析失败 → 空列表
    （不阻断缓存复用，仅少回放明细）。
    """
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except Exception:  # noqa: BLE001 — 脏数据退化为空
            return []
        items = parsed if isinstance(parsed, list) else []
    else:
        return []
    return [str(x).strip() for x in items if str(x or "").strip()]


# ── Peer model pick (live team, different vendor model_id) ───

def model_family_key(config: dict | None) -> str:
    """Vendor model family: ``llm_models.model_id``, case-insensitive.

    Same weights behind two API keys / two DB rows still count as one
    family — the point of a peer audit is a different model, not a
    different quota.
    """
    if not config:
        return ""
    return (config.get("model_id") or "").strip().lower()


def select_peer_audit_model(
    author_family: str,
    author_tier: str,
    peers: list[dict[str, Any]],
) -> dict | None:
    """Pick one teammate config whose family ≠ author.

    ``peers`` items: ``{"tier": "management"|"executor", "config": dict}``.
    Prefer a different model tier (management ↔ executor); if several
    remain, lexicographic ``model_id`` then DB ``id`` so the choice is
    stable across calls. Returns None when the live team has no other
    family.
    """
    family = (author_family or "").strip().lower()
    if not family:
        return None
    distinct: list[dict[str, Any]] = []
    for peer in peers:
        cfg = peer.get("config") if isinstance(peer, dict) else None
        key = model_family_key(cfg)
        if not key or key == family:
            continue
        distinct.append(peer)
    if not distinct:
        return None
    other_tier = [
        p for p in distinct
        if (p.get("tier") or "") != author_tier
    ]
    pool = other_tier or distinct
    pool.sort(
        key=lambda p: (
            model_family_key(p.get("config")),
            (p.get("config") or {}).get("id") or "",
        )
    )
    return pool[0].get("config")


async def resolve_peer_audit_model(
    project_id: str,
    author_id: str,
) -> tuple[dict | None, str]:
    """Resolve (config, source) for the audit one-shot.

    ``source`` is ``"peer"`` when a live teammate's resolved model has a
    different vendor ``model_id``, else ``"own"``. Config is the author's
    model when no peer family exists. ``(None, "own")`` only if the
    author themselves has no resolvable model.
    """
    from hiveweave.services.model import ModelService
    from hiveweave.services.org import OrgService
    from hiveweave.services.policy import model_tier_for_agent

    org = OrgService()
    ms = ModelService()
    agents = await org.list_agents(project_id)
    author = next((a for a in agents if a.get("id") == author_id), None)
    if author is None:
        author = await org.get_agent(author_id)
    if not author:
        return None, "own"

    author_tier = model_tier_for_agent(author)
    author_cfg = await ms.resolve_model(
        tier=author_tier, preferred=author.get("model_id"),
    )
    if not author_cfg:
        return None, "own"

    author_family = model_family_key(author_cfg)
    peers: list[dict[str, Any]] = []
    for agent in agents:
        if agent.get("id") == author_id:
            continue
        if (agent.get("status") or "").lower() == "archived":
            continue
        try:
            tier = model_tier_for_agent(agent)
            cfg = await ms.resolve_model(
                tier=tier, preferred=agent.get("model_id"),
            )
        except Exception as exc:  # noqa: BLE001 — skip one teammate, keep picking
            log.debug(
                "code_audit.peer_teammate_unresolved",
                teammate_id=agent.get("id"),
                error=str(exc),
            )
            continue
        if not cfg or model_family_key(cfg) == author_family:
            continue
        peers.append({"tier": tier, "config": cfg})

    chosen = select_peer_audit_model(author_family, author_tier, peers)
    if chosen:
        log.info(
            "code_audit.peer_model",
            agent_id=author_id,
            author_model=author_cfg.get("model_id"),
            audit_model=chosen.get("model_id"),
        )
        return chosen, "peer"
    log.info(
        "code_audit.peer_model_unavailable",
        agent_id=author_id,
        author_model=author_cfg.get("model_id"),
        teammate_count=sum(
            1 for a in agents
            if a.get("id") != author_id
            and (a.get("status") or "").lower() != "archived"
        ),
    )
    return author_cfg, "own"


async def _invoke_audit_llm(
    project_id: str,
    agent_id: str,
    system: str,
    user: str,
    call_llm: ReviewLLMCallback | None,
    oneshot_llm: OneshotLLMCallback | None,
) -> tuple[str | None, dict]:
    """Run the audit completion. Returns (text, meta) or a soft-fail dict.

    When ``oneshot_llm`` is wired, pick a live-team peer model first.
    ``call_llm`` (author's own review callback) is the fallback used by
    tests and when peer resolve fails.
    """
    chosen: dict | None = None
    source = "own"
    if oneshot_llm is not None:
        try:
            chosen, source = await resolve_peer_audit_model(
                project_id, agent_id,
            )
        except Exception as exc:  # noqa: BLE001 — fall back to own callback
            log.warning(
                "code_audit.peer_resolve_failed",
                agent_id=agent_id,
                error=str(exc),
            )
            chosen, source = None, "own"

    # 用**有效**帽（请求值 ∩ agent 侧首读帽），参见 effective_audit_timeout_s
    _timeout_s = effective_audit_timeout_s()
    try:
        if oneshot_llm is not None and chosen:
            # Verdict must land in ``content`` (``VERDICT: PASS|ISSUES`` on
            # line 1). Thinking models often spend the budget on
            # reasoning_content and return empty content → false llm_failed.
            cfg = dict(chosen)
            cfg["supports_thinking"] = False
            cfg["default_reasoning_effort"] = None
            text = await asyncio.wait_for(
                oneshot_llm(cfg, system, user),
                timeout=_timeout_s,
            )
        elif call_llm is not None:
            text = await asyncio.wait_for(
                call_llm(system, user),
                timeout=_timeout_s,
            )
            source = "own"
            chosen = None
        elif oneshot_llm is not None:
            return None, {"audited": False, "reason": "no_model"}
        else:
            return None, {"audited": False, "reason": "no_callback"}
    except asyncio.TimeoutError:
        log.warning(
            "code_audit.llm_timeout",
            agent_id=agent_id,
            timeout_s=_timeout_s,
        )
        return None, {
            "audited": False,
            "reason": "llm_failed",
            "audit_upstream_unavailable": True,
        }
    except NoModelConfiguredError as exc:
        log.warning("code_audit.no_model", agent_id=agent_id, error=str(exc))
        return None, {"audited": False, "reason": "no_model"}
    except Exception as exc:  # noqa: BLE001 — soft-fail contract
        log.warning("code_audit.llm_failed", agent_id=agent_id, error=str(exc))
        return None, {
            "audited": False,
            "reason": "llm_failed",
            "audit_upstream_unavailable": True,
        }
    if not text:
        # Empty text is as fatal as a raised exception, but was previously
        # swallowed with no log at all — the 6/6 llm_failed in TEST_DSH_35
        # came through this branch with an empty `error` field.
        # Reason stays "llm_failed": _SOFT_FAIL_ATTEMPT_REASONS (:64) is a
        # whitelist and drives the non-blocking wording at :136/:203/:210.
        log.warning(
            "code_audit.empty_text",
            agent_id=agent_id,
            project_id=project_id,
            provider=(chosen or {}).get("provider"),
            model_id=(chosen or {}).get("model_id"),
            source=source,
        )
        return None, {
            "audited": False,
            "reason": "llm_failed",
            "audit_upstream_unavailable": True,
        }
    return text, {
        "audit_model_id": (chosen or {}).get("model_id"),
        "audit_model_source": source,
    }


# ── Audit runner ─────────────────────────────────────────────

async def run_code_audit(
    project_id: str,
    agent_id: str,
    task_id: str | None = None,
    call_llm: ReviewLLMCallback | None = None,
    oneshot_llm: OneshotLLMCallback | None = None,
    appeal_notes: str | None = None,
) -> dict:
    """Run a code audit for an agent's worktree. Never raises.

    ``oneshot_llm`` is ``Agent._oneshot_llm(model_config, system, user)`` —
    production path; picks a live teammate's different ``model_id``.
    ``call_llm`` is the parent agent's review callback
    (``async (system, user) -> str``) — tests and fallback (author's own
    model). ``None`` for both soft-fails with ``no_callback`` once an LLM
    verdict is actually needed; the auto-PASS path never touches either.
    ``appeal_notes``（审计 epic P0-2）：作者对 finding 的申诉（认为某些
    行为系规格要求），注入 prompt 申诉节，审计独立核实不自动放行。

    Return contract (soft-fail dicts):
      - ``{"audited": False, "reason": "no_worktree" | "no_callback" | "no_model" | "llm_failed" | "error"}``
        （llm_failed 且成功入队时附 ``retry_queued`` / ``retry_attempts``）
      - ``{"audited": True, "verdict": "PASS" | "ISSUES", ...}``

    ⚠ **本层只返回数据，不构造工具结果**（模块 docstring 的既有拓扑）：返回的
    dict 由工具壳翻译成 ``ToolResult``。**``audited=False`` 一律不是成功**
    ——``audited=False`` 的出口没有任何一条能报 ``success=True``，工具壳
    （``tools/code_audit.py`` 的 ``_soft_fail_result`` 唯一漏斗）也不得再把它
    包成成功结果。缺陷（TEST_DSH_55）正是从这条缝漏出去的：34 次调用全被
    包成 ``success=True`` ⇒ ``run_steps.status`` 全 ``'completed'``，其中 11 次
    拿不到凭证的 agent 读到「成功」而重发。判据见 :func:`audit_outcome_state`
    与 :func:`audit_failure_fact`。
    """
    try:
        from hiveweave.services.worktree_review import agent_worktree_path

        worktree = await agent_worktree_path(agent_id)
        if not worktree:
            return {"audited": False, "reason": "no_worktree"}

        diff = await collect_worktree_diff(worktree)

        from hiveweave.services.attestation import (
            attestation_service,
            hash_stdout,
        )

        commit_hash: str | None = None
        ok, head = await _run_git(["rev-parse", "HEAD"], worktree)
        if ok and head:
            commit_hash = head.strip()[:40]

        # Auto-PASS without an LLM call: nothing changed and the ledger is
        # within the soft threshold.
        # TEST_DSH_32 P7（空 diff 短路）：diff 为空 = 无可审计内容——
        # 无论台账如何（编辑后回滚也算无净变更），直接 auto-PASS，
        # 不再为空 diff 烧一次 LLM（此前实锤 34s 空跑）。
        # TEST_DSH_39 P0-3（审计翻转）：空 diff ≠ 安全。同任务上一轮审计若为
        # ISSUES（exit_code=1），回滚后的 auto-PASS 会把结论 4min 内翻转成
        # PASS，且新 PASS 凭证顶掉旧 ISSUES 凭证——门禁自己跟自己打架。
        # 改为：上一轮 ISSUES 在案时发显式「回滚标注」（不发 PASS 凭证，旧
        # ISSUES 凭证继续生效），要求人工复核/重新指派/取消任务；
        # 无 ISSUES 前科时保留原 auto-PASS（P7 优化不回退）。
        if not diff.strip():
            from hiveweave.services.attestation import (
                find_latest_attestation_by_kind,
                parse_audit_verdict,
            )

            prior = None
            try:
                prior = await find_latest_attestation_by_kind(
                    project_id, agent_id=agent_id, kind=CODE_AUDIT_KIND
                )
            except Exception:  # noqa: BLE001 — 查询失败按无前科处理（P7 优化不回退）
                prior = None
            prior_issues = bool(
                prior
                and parse_audit_verdict(prior) == "ISSUES"
                and _task_ref_match(task_id, prior.get("task_id"))
            )
            if prior_issues:
                reset_ledger(agent_id)
                log.warning(
                    "code_audit.empty_diff_after_issues",
                    agent_id=agent_id,
                    task_id=task_id,
                )
                return {
                    "audited": True,
                    "verdict": "ROLLED_BACK",
                    "commit_hash": commit_hash,
                    "lines_audited": 0,
                    "auto_pass_reason": "empty_diff_after_issues",
                    "message": (
                        "[code audit] diff 为空（变更已回滚），但同任务上一轮"
                        "审计结论为 ISSUES——不发新的 PASS 凭证。可执行出口："
                        "① reassign_task 改派（现已支持花名）给其他成员重做；"
                        "② message_user 请求用户裁决；③ 与上级确认后取消任务。"
                        "不要原样重复提交。"
                    ),
                }
            attestation_id = await attestation_service.create(
                project_id,
                agent_id=agent_id,
                kind=CODE_AUDIT_KIND,
                task_id=task_id,
                exit_code=0,
                workspace=worktree,
                commit_hash=commit_hash,
                stdout_hash=hash_stdout("no changes to audit"),
            )
            # 台账同步清零（审计 P3-6）：无净变更也视为已审计口径
            reset_ledger(agent_id)
            return {
                "audited": True,
                "verdict": "PASS",
                "commit_hash": commit_hash,
                "lines_audited": 0,
                "attestation_id": attestation_id,
                "auto_pass_reason": "empty_diff",
            }

        # 40 轮报告第 4 步（diff 级缓存）：同 agent + 同 diff 哈希 = 同被审
        # 内容，审计结论可复用（霞光对 683bf23f 审 6 次纯属浪费）。key 用
        # diff 内容哈希而非 HEAD commit——HEAD 相同但工作区改动不同的两次
        # 审计不可互代（审计子代理[阻断]项：干净 worktree 的 PASS 凭证不得
        # 给新未审代码放行）。复用时仍发新凭证（门禁时效性不变），不烧 LLM。
        diff_hash = hash_stdout(diff)
        cached_hit = None
        if diff_hash:
            try:
                cached_hit = await attestation_service.audit_cache_lookup(
                    project_id, agent_id=agent_id, diff_hash=diff_hash
                )
            except Exception:  # noqa: BLE001 — 缓存查询失败退化为正常审计
                cached_hit = None
        if cached_hit is not None:
            cached_exit = int(cached_hit.get("exit_code") or 1)
            # 用缓存行存的真实判定（可能是 warnings-only 的 ISSUES+exit 0）
            verdict_cached = (
                str(cached_hit.get("verdict") or "").strip()
                or ("PASS" if cached_exit == 0 else "ISSUES")
            )
            # 审计 epic P0-1（42 轮报告）：缓存命中要回放**原结论**——
            # 旧实现回执「ISSUES / 0 行 / 0 问题」自相矛盾，agent 误判审计
            # 空转后 12 次重审赌结果。issue 列表随缓存落库（P0-1 新列），
            # 行数用当前台账值（本次会话累计未审行）。
            cached_issues = _parse_cached_issues(cached_hit.get("top_issues"))
            cached_source = str(
                cached_hit.get("source_attestation_id")
                or cached_hit.get("attestation_id")
                or ""
            ).strip()
            attestation_id = await attestation_service.create(
                project_id,
                agent_id=agent_id,
                kind=CODE_AUDIT_KIND,
                task_id=task_id,
                exit_code=cached_exit,
                workspace=worktree,
                commit_hash=commit_hash,
                stdout_hash=hash_stdout(f"cached audit reuse {diff_hash}"),
                command_or_url=f"[verdict={verdict_cached}] cached-reuse",
            )
            lines_now = get_unaudited_lines(agent_id)
            reset_ledger(agent_id)
            log.info(
                "code_audit.cached_reuse",
                agent_id=agent_id,
                diff_hash=diff_hash[:12],
                verdict=verdict_cached,
                source_attestation_id=cached_source[:8] or None,
            )
            return {
                "audited": True,
                "verdict": verdict_cached,
                "commit_hash": commit_hash,
                "issues_count": len(cached_issues),
                "top_issues": cached_issues[:_TOP_ISSUES_MAX],
                "lines_audited": lines_now,
                "attestation_id": attestation_id,
                "cached_diff_hash": diff_hash[:12],
                "cached_from_attestation_id": cached_source or None,
                "message": (
                    "[code audit] 同一 diff 内容已有审计结论 "
                    f"{verdict_cached}，本次复用未重烧 LLM。若代码有新改动，"
                    "diff 随之变化，将触发全新审计。"
                ),
            }

        # 审计 epic P0-2：prompt 注入任务验收标准（取不到跳过该节）+
        # 作者申诉（finding 级，仅参考）。
        criteria = await load_task_acceptance_criteria(project_id, task_id)
        system, user = build_audit_prompt(
            diff,
            task_id,
            acceptance_criteria=criteria,
            appeal_notes=appeal_notes,
        )
        # P0-2：并发整形——多 agent 同时触发审计时排队（默认并发 2），
        # 避免全员直打上游网关在风暴期互相放大失败。
        async with _get_audit_gate():
            text, meta = await _invoke_audit_llm(
                project_id, agent_id, system, user, call_llm, oneshot_llm,
            )
        if text is None:
            # No extra HTTP retry here: _oneshot_llm already retries once via
            # _review_llm_post_with_retry. Record the attempt so submit can
            # say "tried, LLM failed" instead of "never audited".
            reason = str(meta.get("reason") or "")
            if reason in _SOFT_FAIL_ATTEMPT_REASONS:
                record_audit_attempt(agent_id, reason, task_id)
            # 审计 epic P1-4：上游暂态失败 → 平台自动入队后台重试并回填
            # 通知，agent 等待即可，不必赌重试或直奔 waive。入队成功时在
            # 返回 dict 附 retry_queued / retry_attempts / retry_exhausted
            # （工具层改写回执文案）；入队失败（无 DB / 异常）保持原契约。
            if reason == "llm_failed" and meta.get("audit_upstream_unavailable"):
                queued: dict | None = None
                try:
                    from hiveweave.services.audit_retry import enqueue_failed_audit

                    queued = await enqueue_failed_audit(
                        project_id, agent_id, task_id, diff_hash,
                        worktree=worktree, commit_hash=commit_hash,
                        appeal_notes=appeal_notes,
                    )
                except Exception:  # noqa: BLE001 — 软失败契约，入队绝不 raise
                    queued = None
                if queued is not None:
                    meta = dict(meta)
                    meta["retry_queued"] = True
                    meta["retry_attempts"] = queued.get("attempts")
                    meta["retry_exhausted"] = bool(queued.get("exhausted"))
                    # 审计 P2：耗尽后人工重试 → 新一轮重试序列（回执措辞用）
                    meta["retry_resequence"] = bool(queued.get("resequence"))
            return meta

        verdict = _parse_verdict(text)
        issues = _parse_issues(text)
        # 41+08 双跑 P0-2（审计门 27.3%）：只有 high 级问题才锁门——
        # medium/low 记录为警告不阻塞（41 实测 24 次 ISSUES 中 4 次无 high
        # 仍被锁死）。ISSUES 判定保留诚实；门禁语义 = 「有 high 才拦」。
        #
        # ⚠ #12（2026-09-14）：**旧判据 `"[high]" in issue` 是 fail-open 的**——
        # 它把"severity 写成了别的形态（`【high】`/`高`/法文）"静默当成"不是 high"
        # ⇒ 改个标点或换语言即放行。下面同时算出 **fail-safe** 判定
        # （**解析不出 ⇒ 视为 high**），但**只记录不拦**（shadow 试运行，见模块 docstring）。
        high_count = sum(1 for i in issues if "[high]" in i.lower())
        blocking = verdict == "ISSUES" and high_count > 0
        exit_code = 1 if blocking else 0

        _counts = count_issue_severities(issues)
        _failsafe_high = _counts["high"] + _counts["unparsed"]
        shadow_blocking = verdict == "ISSUES" and _failsafe_high > 0
        _total_issues = len(issues) or 0
        _unparsed_ratio = (
            round(_counts["unparsed"] / _total_issues, 3) if _total_issues else 0.0
        )
        # 量的是**平台可控指标**（"分类一致率/误判率"），不是重试率那类上游指标。
        # `would_flip` = 上线 fail-safe 后**新增被拦**的任务数（误拦率的分母）。
        log.warning(
            "code_audit_severity_shadow",
            verdict=verdict,
            issues_total=_total_issues,
            legacy_high=high_count,
            failsafe_high=_failsafe_high,
            unparsed=_counts["unparsed"],
            unparsed_ratio=_unparsed_ratio,
            conflicts=_counts["conflicts"],
            legacy_blocking=blocking,
            shadow_blocking=shadow_blocking,
            would_flip=(shadow_blocking and not blocking),
        )
        if _counts["conflicts"]:
            # 计划 §三 #12 验收④：冲突用例要**有日志说明忽略了后文**。
            log.warning(
                "code_audit_severity_conflict_ignored",
                conflicts=_counts["conflicts"],
                rule="只认首个 SEVERITY: 前缀，其后文本一律忽略",
            )
        try:
            from hiveweave.services.event_audit import event_audit

            await event_audit.log(
                agent_id=agent_id,
                project_id=project_id,
                event_type="code_audit_severity_shadow",
                payload={
                    "verdict": verdict,
                    "issues_total": _total_issues,
                    "legacy_high": high_count,
                    "failsafe_high": _failsafe_high,
                    "unparsed": _counts["unparsed"],
                    "unparsed_ratio": _unparsed_ratio,
                    "conflicts": _counts["conflicts"],
                    "legacy_blocking": blocking,
                    "shadow_blocking": shadow_blocking,
                    "would_flip": shadow_blocking and not blocking,
                },
            )
        except Exception:  # noqa: BLE001 — 观测旁支绝不挂掉审计
            pass

        # Lazy import: tools.executor pulls the whole tool registry.
        from hiveweave.tools.executor import ToolExecutor

        report_path = ToolExecutor._save_tool_output_file(
            text, agent_id, CODE_AUDIT_KIND, worktree
        )

        attestation_id = await attestation_service.create(
            project_id,
            agent_id=agent_id,
            kind=CODE_AUDIT_KIND,
            task_id=task_id,
            exit_code=exit_code,
            workspace=worktree,
            commit_hash=commit_hash,
            stdout_hash=hash_stdout(text),
            command_or_url=(
                f"[verdict={verdict}] high={high_count}"
                if verdict == "ISSUES"
                else f"[verdict={verdict}]"
            ),
        )

        # 40 轮报告第 4 步：审计结论入缓存（同 agent + 同 diff 哈希复用）。
        # 审计 epic P0-1：同时落 issue 列表 + 原凭证 id，命中路径可回放。
        try:
            await attestation_service.audit_cache_store(
                project_id,
                agent_id=agent_id,
                diff_hash=diff_hash,
                verdict=verdict,
                exit_code=exit_code,
                attestation_id=str(attestation_id),
                top_issues=issues,
                source_attestation_id=str(attestation_id),
            )
        except Exception:  # noqa: BLE001 — 缓存写入失败不影响审计主流程
            pass

        lines_audited = get_unaudited_lines(agent_id)
        reset_ledger(agent_id)
        return {
            "audited": True,
            "verdict": verdict,
            "commit_hash": commit_hash,
            "issues_count": len(issues),
            "top_issues": issues[:_TOP_ISSUES_MAX],
            "report_path": report_path,
            "attestation_id": attestation_id,
            "lines_audited": lines_audited,
            "audit_model_id": meta.get("audit_model_id"),
            "audit_model_source": meta.get("audit_model_source"),
        }
    except Exception as exc:  # noqa: BLE001 — soft-fail contract
        log.warning("code_audit.error", agent_id=agent_id, error=str(exc))
        return {"audited": False, "reason": "error"}


# ── Shared notice helper ─────────────────────────────────────

def append_code_audit_notice(description: str) -> str:
    """Append CODE_AUDIT_POLICY to a description once (idempotent)."""
    if not description:
        return CODE_AUDIT_POLICY
    if CODE_AUDIT_POLICY in description:
        return description
    return f"{description}\n{CODE_AUDIT_POLICY}"
