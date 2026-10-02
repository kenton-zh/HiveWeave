"""T1 审查/审计派单化（单轨）— submit 门触发的内部「代码审计任务」铸造。

**休眠状态（批3，2026-09-29）：``SUBMIT_AUDIT_GATE=off``（默认）时本模块
的铸造链路整体休眠** —— submit 主短路在 ``tools/tasks/submit.py``（preflight
把 code_audit 从必需 kind 里剥掉 ⇒ ``audit_gate_pending`` 恒 False，不调用
本模块）；``mint_audit_task`` 入口另有防御性 flag 短路（off ⇒ 直接
not-dispatched，不查重、不派单）。``HIVEWEAVE_SUBMIT_AUDIT_GATE=on`` 一键
回切旧门，本模块恢复原职责。T18 的卡判别 / reviewer 解析 / 凭证回写
（``rebind_audit_attestations_on_approve``）**不受 flag 影响**——已在途的
历史审计卡收口路径始终可用。

用户钦定单轨（2026-09-29）：**submit 永不同步跑审计 LLM 调用**。submitter
不再在自己回合内跑 ``request_code_audit``（600s 同步等待）——当 diff 触发
审计门（policy 要求 code_audit 凭证且未满足，即 >20 行等既有判定）时，平台
在此铸造一条**内部审计任务**派给审查池，submit 本身仍被既有门拒绝（门逻辑
零改动），拒绝回执自带 waiting_on 处方。

角色分工（全部复用既有机制，不新写状态机）：
- 审计者是**普通 agent run**：claim → start → 用既有 ``request_code_audit``
  （新增 authorId 代审参数）落凭证（attestation 绑定 submitter + 源任务）→
  submit 审计任务；
- 唤醒链：审计任务 ``creator_id = submitter`` ⇒ 审计者 submit 时既有
  ``[TASK SUBMITTED] wake=True`` 直推 + ``kind=task`` 等待契约（task_transition
  通道，``merge_landed`` 先例同族）唤醒 requester ⇒ requester 重提源任务，
  ``find_recent_for_agent`` 自动附凭证 ⇒ 门开；
- 卡死自愈白拿：审计任务是普通任务 ⇒ task dwell 时钟（超时改派/自动收口）
  + 沉默看门狗天然适用。

单兵项目（78 形态，CEO 自己 submit）无中层：qa_lead 建项自带，直接派；
qa_lead 归档/不存在/恰为 submitter 时回退 submitter 的 parent coordinator；
回退链走空 ⇒ **显式**返回 ``no_reviewer_available``（调用方回执点名，不静默吞，
原 attestation 门与 waive 出口保持）。

幂等：同一 (submitter, 源任务) 已有**进行中**的审计任务（created/claimed/
running/blocked）⇒ 复用不重铸；已 submitted/reviewing（结论已交付，typically
ISSUES 需返工）⇒ 铸新任务审新 diff。⚠ **故意不设 parent_task_id**：
``close.py::_maybe_close_umbrella_parent`` 会在子任务关闭时归档 running 父任务
——源任务正处于 submitted 待审，设父子关系会被该级联误归档。源任务关联只存
``evidence.source_task_id``。
"""

from __future__ import annotations

import json
import time
from typing import Any

import structlog

log = structlog.get_logger(__name__)

#: 系统前缀（对齐 is_verify_task 的「系统前缀」惯例）：防 agent 自由 tag
#: 混淆，也是幂等查重的第一道过滤。
AUDIT_TASK_TITLE_PREFIX = "SYSTEM AUDIT: "

#: evidence 里指向被审源任务的键（幂等查重判据；不用 parent_task_id，
#: 理由见模块 docstring）。
AUDIT_TASK_SOURCE_KEY = "source_task_id"

#: 审计任务固定 tag（mint 契约；与标题系统前缀同为卡片判别标记）。
AUDIT_TASK_TAG = "system-audit"

#: mint 写进卡 evidence 的溯源键全集（T18 bug2）：卡被 submit 时提交
#: evidence 会整体覆盖旧行，这组键必须原样保留——approve 收口的凭证回写
#: 靠 source_task_id / submitter_id 找回源任务。
AUDIT_TASK_EVIDENCE_KEYS: tuple[str, ...] = (
    AUDIT_TASK_SOURCE_KEY,
    "submitter_id",
    "worktree",
    "base_branch",
    "head_commit",
    "dispatched_by",
)

#: 进行中（可复用）的审计任务状态；submitted/reviewing = 结论已交付。
_AUDIT_TASK_OPEN_STATUSES = ("created", "claimed", "running", "blocked")


def _norm_task_ref(ref: str | None) -> str:
    return str(ref or "").replace("-", "").strip().lower()


def is_system_audit_card(task: dict[str, Any] | None) -> bool:
    """任务行是否 SYSTEM AUDIT 卡（mint 契约双标记：tag 或标题系统前缀）。

    tags 兼容 str（DB 原文 JSON）/ list 两种形态；坏 tags 按无 tag 处理，
    标题前缀仍可判。
    """
    if not isinstance(task, dict):
        return False
    if str(task.get("title") or "").startswith(AUDIT_TASK_TITLE_PREFIX):
        return True
    raw = task.get("tags")
    try:
        tags = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except Exception:  # noqa: BLE001 — 坏 tags 不影响标题判
        return False
    if not isinstance(tags, (list, tuple, set)):
        return False
    return AUDIT_TASK_TAG in {str(t) for t in tags}


def _audit_policy_id() -> str:
    """审计任务自身的 policy：``coordinator_review``（无必需凭证 kind）。

    ⚠ 审计任务**绝不能**解析出 code_audit policy——否则审计任务自己的 submit
    又要求 code_audit 凭证，无限递归。显式传 policy_id（经 create_task 白名单
    校验），tags 也不带 audit/ui/test/doc token。
    """
    return "coordinator_review"


async def find_open_audit_task(
    project_id: str, source_task_id: str, submitter_id: str
) -> dict[str, Any] | None:
    """同 (submitter, 源任务) 进行中的审计任务（幂等判据）。

    查询口径：标题系统前缀 + creator=submitter + source=system + 非终态
    （approved/closed/cancelled 之外），再在 Python 侧比对
    ``evidence.source_task_id``（量小，标题前缀已把候选收敛到个位数）。
    查询失败返回 None（fail-open ⇒ 调用方走铸造，create_task 侧
    source=system 不触发查重门，最坏多铸一条，不会丢审计）。
    """
    from .db import _query

    try:
        rows = await _query(
            project_id,
            "SELECT id, title, status, assignee_id, evidence FROM tasks "
            "WHERE title LIKE ? AND creator_id = ? AND source = 'system' "
            "AND COALESCE(is_archived, 0) = 0 "
            "ORDER BY created_at DESC LIMIT 12",
            [f"{AUDIT_TASK_TITLE_PREFIX}%", str(submitter_id)],
        )
    except Exception as e:  # noqa: BLE001 — fail-open，见 docstring
        log.warning(
            "audit_task_dedup_lookup_failed",
            project_id=project_id,
            source_task_id=str(source_task_id)[:8],
            error=str(e),
        )
        return None
    want = _norm_task_ref(source_task_id)
    for r in rows or []:
        row = dict(r)
        status = str(row.get("status") or "")
        if status not in _AUDIT_TASK_OPEN_STATUSES:
            continue
        raw_ev = row.get("evidence")
        try:
            ev = json.loads(raw_ev) if isinstance(raw_ev, str) else (raw_ev or {})
        except Exception as e:  # noqa: BLE001 — 坏 evidence 行当空处理
            log.debug("audit_task_evidence_unparseable", task_id=row.get("id"), error=str(e))
            ev = {}
        if _norm_task_ref(str((ev or {}).get(AUDIT_TASK_SOURCE_KEY) or "")) == want:
            return row
    return None


async def resolve_reviewer(
    project_id: str, submitter_id: str
) -> tuple[dict[str, Any] | None, str]:
    """审查池回退链：qa_lead → submitter 的 parent coordinator。

    Returns ``(agent, chain)``；走空返回 ``(None, "no_reviewer_available")``。
    排除：归档/停用成员、submitter 本人（不自我审计）。
    """
    from hiveweave.services.org import OrgService
    from hiveweave.services.policy import infer_role_family

    org = OrgService()

    def _alive(a: dict[str, Any] | None) -> bool:
        return bool(a) and str((a or {}).get("status") or "").lower() not in (
            "archived",
            "dismissed",
        )

    # 1) 本项目 QA lead（建项自带；单兵 78 形态的默认审查者）
    lead: dict[str, Any] | None = None
    try:
        lead = await org.get_agent_by_role(project_id, "qa_lead")
    except Exception as e:  # noqa: BLE001 — 回退链继续
        log.debug("audit_dispatch_qa_lead_lookup_failed", error=str(e))
    if _alive(lead) and str(lead.get("id")) != str(submitter_id):
        return lead, "qa_lead"

    # 2) submitter 的 parent coordinator（中层在，qa_lead 缺位时的回退）
    me: dict[str, Any] | None = None
    try:
        me = await org.resolve_agent(submitter_id)
    except Exception as e:  # noqa: BLE001
        log.debug("audit_dispatch_submitter_lookup_failed", error=str(e))
    parent_id = str((me or {}).get("parent_id") or "")
    if parent_id and parent_id != str(submitter_id):
        parent: dict[str, Any] | None = None
        try:
            parent = await org.get_agent(parent_id)
        except Exception as e:  # noqa: BLE001
            log.debug("audit_dispatch_parent_lookup_failed", error=str(e))
        if (
            _alive(parent)
            and infer_role_family(parent or {}) == "coordinator"
        ):
            return parent, "parent_coordinator"

    return None, "no_reviewer_available"


async def resolve_card_reviewer(
    project_id: str,
    *,
    card_assignee_id: str,
    submitter_id: str,
    source_task_id: str | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """SYSTEM AUDIT 卡**自身**的 reviewer 回退链（T18 bug1 修复）。

    背景：submit 钉 reviewer 的旧默认是 ``creator_id``——卡的 creator 恰是
    被审 submitter（executor 无 ``review_task`` 工具）⇒ 卡永远无法被审、
    卡死在 submitted。正确语义：卡的 reviewer 必须是**有 review 权限的
    角色**，且不得等于卡的 assignee（review_task 硬门禁自审）。

    链（逐级过滤：在职 + review family + ≠ 卡 assignee）：
    1. 源任务已钉的 reviewer（review obligation owner 的既成事实）；
    2. 源任务 creator（常规形态=中层、单兵形态=CEO——都持 REVIEW）；
    3. 项目 qa_lead（卡被回退链派给 parent coordinator 时的补充审查者）；
    4. 卡 assignee 的 parent（qa_lead 的上级通常为 CEO——保底不断链）。

    全部走空返回 ``(None, "no_card_reviewer_available")``，调用方回退旧默认
    （creator），并留日志。
    """
    from hiveweave.services.org import OrgService
    from hiveweave.services.policy import infer_role_family

    org = OrgService()

    def _eligible(a: dict[str, Any] | None) -> bool:
        if not a:
            return False
        if str((a or {}).get("status") or "").lower() in ("archived", "dismissed"):
            return False
        if str((a or {}).get("id") or "") == str(card_assignee_id or ""):
            return False  # review_task 硬门：assignee 不得自审
        return infer_role_family(a or {}) in ("qa", "coordinator", "ceo")

    # 1+2) 源任务的 review obligation owner（已钉 reviewer 优先，退 creator）
    if source_task_id:
        try:
            from hiveweave.services.task import TaskService

            src = await TaskService().get_task(project_id, str(source_task_id))
        except Exception as e:  # noqa: BLE001 — 源任务取不到继续走后续链
            log.debug("audit_card_source_lookup_failed", error=str(e))
            src = None
        for key, chain in (("reviewer_id", "source_reviewer"), ("creator_id", "source_creator")):
            cand_id = str((src or {}).get(key) or "")
            if not cand_id:
                continue
            try:
                cand = await org.get_agent(cand_id)
            except Exception as e:  # noqa: BLE001
                log.debug("audit_card_reviewer_candidate_lookup_failed", error=str(e))
                cand = None
            if _eligible(cand):
                return cand, chain

    # 3) 项目 qa_lead
    try:
        lead = await org.get_agent_by_role(project_id, "qa_lead")
    except Exception as e:  # noqa: BLE001
        log.debug("audit_card_qa_lead_lookup_failed", error=str(e))
        lead = None
    if _eligible(lead):
        return lead, "qa_lead"

    # 4) 卡 assignee 的 parent（保底：qa_lead 上级通常为 CEO）
    try:
        assignee = await org.get_agent(str(card_assignee_id or ""))
        parent_id = str((assignee or {}).get("parent_id") or "")
        parent = await org.get_agent(parent_id) if parent_id else None
    except Exception as e:  # noqa: BLE001
        log.debug("audit_card_assignee_parent_lookup_failed", error=str(e))
        parent = None
    if _eligible(parent):
        return parent, "assignee_parent"

    return None, "no_card_reviewer_available"


async def _build_payload(
    project_id: str, source_task: dict[str, Any], submitter_id: str
) -> dict[str, str]:
    """审计任务的 description 与 evidence（submitter/worktree/diff 范围/base）。

    worktree/base commit 取不到时如实写 unknown（审计工具自己也会再解析），
    不猜。
    """
    from hiveweave.services.worktree_review import agent_worktree_path

    source_task_id = str(source_task.get("id") or "")
    worktree = ""
    try:
        worktree = await agent_worktree_path(submitter_id) or ""
    except Exception:  # noqa: BLE001 — best-effort 事实收集
        worktree = ""
    base_branch = ""
    head_commit = ""
    if worktree:
        try:
            from hiveweave.services.git_worktree import (
                _git,
                _resolve_base_branch,
            )

            base_branch = await _resolve_base_branch(worktree) or ""
            ok, head = await _git(["rev-parse", "HEAD"], worktree)
            if ok and head:
                head_commit = head.strip()[:40]
        except Exception as e:  # noqa: BLE001 — best-effort 事实收集
            log.debug("audit_task_git_fact_failed", worktree=worktree, error=str(e))
            base_branch, head_commit = "", ""

    submitter_name = submitter_id
    try:
        from hiveweave.services.org import OrgService

        me = await OrgService().get_agent(submitter_id)
        if me and me.get("name"):
            submitter_name = f"{me.get('name')} ({submitter_id})"
    except Exception as e:  # noqa: BLE001 — 展示名失败用 id 兜底
        log.debug("audit_task_submitter_name_lookup_failed", submitter_id=submitter_id, error=str(e))

    evidence = {
        AUDIT_TASK_SOURCE_KEY: source_task_id,
        "submitter_id": submitter_id,
        "worktree": worktree or "unknown",
        "base_branch": base_branch or "unknown",
        "head_commit": head_commit or "unknown",
        "dispatched_by": "submit_gate",
    }
    description = (
        "[SYSTEM AUDIT — platform-dispatched code audit task]\n"
        "A submitted diff triggered the code-audit gate. You are the assigned "
        "reviewer; your deliverable is an honest audit verdict + a code_audit "
        "attestation bound to the SUBMITTER (not you).\n\n"
        f"- source task: {source_task_id}"
        f" ({str(source_task.get('title') or '')[:80]})\n"
        f"- submitter: {submitter_name}\n"
        f"- worktree to audit: {worktree or 'unknown (audit tool re-resolves)'}\n"
        f"- diff range: {base_branch or 'base'}...HEAD"
        + (f" @ {head_commit[:12]}" if head_commit else "")
        + " + uncommitted + untracked (the audit tool collects this itself)\n"
        "- acceptance: the audit RAN and landed a code_audit attestation bound "
        "to submitter + source task. verdict=ISSUES is a legitimate outcome — "
        "report it honestly.\n\n"
        "Steps:\n"
        "1. start_task on THIS audit task (pre-assigned to you, status=claimed).\n"
        "2. request_code_audit(taskId=\"" + source_task_id + "\", authorId=\""
        + submitter_id + "\") — the platform collects the submitter's worktree "
        "diff and runs the second-pass audit. llm_failed auto-enqueues a "
        "background retry; wait for the inbox notice instead of looping.\n"
        "3. submit_task on THIS audit task with testsPassed=true and "
        "summary=\"audit verdict=… attestation=<id> …\" — submitting wakes the "
        "submitter to re-submit the source task (the attestation auto-attaches "
        "there).\n"
        "4. verdict=ISSUES: still submit (honest summary listing the issues) — "
        "the submitter must fix before re-submitting.\n\n"
        "Do NOT edit the submitter's code. Large diffs may be processed across "
        "turns — submit this task only once the audit actually ran."
    )
    return {"description": description, "evidence_json": json.dumps(evidence, ensure_ascii=False)}


async def mint_audit_task(
    project_id: str, source_task: dict[str, Any], submitter_id: str
) -> dict[str, Any]:
    """铸造（或复用）内部审计任务并派给审查池。**绝不 raise**（返回契约）。

    批3 灰度：``SUBMIT_AUDIT_GATE=off``（默认）⇒ 入口直接短路返回
    ``{"dispatched": False, "reason": "submit_audit_gate_off"}`` —— 防御性
    第三道闸（主短路在 ``tools/tasks/submit.py`` 的 ``audit_gate_pending``）；
    任何绕过 submit 工具层的调用方在 off 模式下也铸不出审计任务。

    Returns::

        {"dispatched": True,  "reused": bool, "task_id": ..., "title": ...,
         "reviewer_id": ..., "reviewer_name": ..., "chain": "qa_lead"|...}
        {"dispatched": False, "reason": "no_reviewer_available" | "error"
                                     | "submit_audit_gate_off", "detail": ...}
    """
    from hiveweave.config import submit_audit_gate_enabled

    if not submit_audit_gate_enabled():
        log.debug(
            "audit_task_mint_skipped_gate_off",
            project_id=project_id,
            source_task_id=str((source_task or {}).get("id") or "")[:8],
        )
        return {"dispatched": False, "reason": "submit_audit_gate_off"}
    try:
        return await _mint_locked(project_id, source_task, submitter_id)
    except Exception as e:  # noqa: BLE001 — 铸造失败不阻断 submit 拒绝本身
        log.warning(
            "audit_task_mint_failed",
            project_id=project_id,
            source_task_id=str((source_task or {}).get("id") or "")[:8],
            error=str(e),
        )
        return {"dispatched": False, "reason": "error", "detail": str(e)[:200]}


async def _mint_locked(
    project_id: str, source_task: dict[str, Any], submitter_id: str
) -> dict[str, Any]:
    from hiveweave.services.task import TaskService

    source_task_id = str(source_task.get("id") or "")
    if not source_task_id or not submitter_id:
        return {"dispatched": False, "reason": "error", "detail": "missing ids"}

    existing = await find_open_audit_task(project_id, source_task_id, submitter_id)
    if existing:
        reviewer_id = str(existing.get("assignee_id") or "")
        log.info(
            "audit_task_reuse_open",
            project_id=project_id,
            audit_task_id=str(existing.get("id"))[:8],
            source_task_id=source_task_id[:8],
        )
        return {
            "dispatched": True,
            "reused": True,
            "task_id": str(existing.get("id")),
            "title": str(existing.get("title") or ""),
            "reviewer_id": reviewer_id,
            "reviewer_name": await _agent_display_name(reviewer_id),
            "chain": "existing",
        }

    reviewer, chain = await resolve_reviewer(project_id, submitter_id)
    if reviewer is None:
        log.warning(
            "audit_task_no_reviewer_available",
            project_id=project_id,
            source_task_id=source_task_id[:8],
            submitter_id=submitter_id[:8],
        )
        return {"dispatched": False, "reason": "no_reviewer_available"}

    reviewer_id = str(reviewer.get("id"))
    payload = await _build_payload(project_id, source_task, submitter_id)
    src_title = str(source_task.get("title") or "").strip()
    title = f"{AUDIT_TASK_TITLE_PREFIX}{src_title}"[:200]

    ts = TaskService()
    audit_id = await ts.create_task(
        project_id,
        title=title,
        description=payload["description"],
        creator_id=submitter_id,
        assignee_id=reviewer_id,
        priority=1,
        acceptance_criteria=[
            {
                "text": (
                    "审计已执行：request_code_audit 落下绑定 submitter + 源任务的 "
                    "code_audit 凭证（verdict=PASS 或如实 ISSUES）"
                ),
                "required": True,
            }
        ],
        tags=["system-audit"],
        source="system",
        # 查重已由 find_open_audit_task 按 (submitter, source_task) 精确判；
        # create_task 内置的相似标题门对 system 来源本就不生效，显式 allow
        # 防将来口径变化把系统 spawn 挡掉。
        dedup_policy="allow",
        policy_id=_audit_policy_id(),
        evidence=json.loads(payload["evidence_json"]),
    )
    log.info(
        "audit_task_minted",
        project_id=project_id,
        audit_task_id=str(audit_id)[:8],
        source_task_id=source_task_id[:8],
        submitter_id=submitter_id[:8],
        reviewer_id=reviewer_id[:8],
        chain=chain,
    )
    await _notify_reviewer(
        project_id=project_id,
        audit_task_id=str(audit_id),
        reviewer_id=reviewer_id,
        source_task_id=source_task_id,
        submitter_id=submitter_id,
    )
    return {
        "dispatched": True,
        "reused": False,
        "task_id": str(audit_id),
        "title": title,
        "reviewer_id": reviewer_id,
        "reviewer_name": str(reviewer.get("name") or reviewer_id),
        "chain": chain,
    }


async def _agent_display_name(agent_id: str) -> str:
    if not agent_id:
        return ""
    try:
        from hiveweave.services.org import OrgService

        a = await OrgService().get_agent(agent_id)
        return str((a or {}).get("name") or agent_id)
    except Exception:  # noqa: BLE001
        return agent_id


async def _notify_reviewer(
    *,
    project_id: str,
    audit_task_id: str,
    reviewer_id: str,
    source_task_id: str,
    submitter_id: str,
) -> None:
    """派单通知（task 通道、wake=1、trusted_platform、幂等键）。失败仅告警。"""
    try:
        from hiveweave.services.inbox import InboxService

        submitter_name = await _agent_display_name(submitter_id)
        await InboxService().send_message(
            from_agent_id="system",
            to_agent_id=reviewer_id,
            message=(
                f"[SYSTEM AUDIT] 新审计任务 {audit_task_id[:8]} 已派给你："
                f"源任务 {source_task_id[:8]}（submitter {submitter_name}）"
                "触发审计门。按任务描述执行：request_code_audit(taskId=\""
                f"{source_task_id}\", authorId=\"{submitter_id}\") 落凭证后 "
                "submit 该审计任务（会自动唤醒 submitter）。"
            ),
            message_type="task",
            priority="normal",
            task_id=audit_task_id,
            wake=True,
            trusted_platform=True,
            idempotency_key=f"system-audit-dispatch:{audit_task_id}",
        )
    except Exception as e:  # noqa: BLE001 — 通知丢失不回滚铸造
        log.warning(
            "audit_task_notify_reviewer_failed",
            audit_task_id=audit_task_id[:8],
            reviewer_id=reviewer_id[:8],
            error=str(e),
        )
    try:
        from hiveweave.agents.trigger import trigger_subordinate

        await trigger_subordinate(reviewer_id)
    except Exception as e:  # noqa: BLE001 — trigger 失败退化为 inbox 轮询
        log.debug("audit_task_trigger_reviewer_failed", error=str(e))


def format_dispatch_receipt_block(mint: dict[str, Any], source_task_id: str) -> str:
    """submit 拒绝回执里的派单区块（回执自解释，不进 prose——SKILL 渠道表）。

    dispatched ⇒ 「审计任务 <id> 已派给 <审查者>」 + waiting_on 处方；
    no_reviewer_available ⇒ 显式点名（不静默吞），原 attestation 门文案与
    waive 出口保持。``submit_audit_gate_off``（批3 灰度短路返回值）⇒ 空串
    —— 门已拆，回执不得再漏审计派单散文。
    """
    if not isinstance(mint, dict) or not mint:
        return ""
    if str(mint.get("reason") or "") == "submit_audit_gate_off":
        return ""
    if mint.get("dispatched"):
        tid = str(mint.get("task_id") or "")
        reviewer = str(mint.get("reviewer_name") or mint.get("reviewer_id") or "")
        rid = str(mint.get("reviewer_id") or "")
        head = (
            "[AUDIT TASK DISPATCHED] 审计任务已派给审查池"
            f"（{'复用进行中任务' if mint.get('reused') else '新建'}）"
        )
        # T16 三期补漏：收尾话术按模式分流（auto 下 commit_turn 已下架，
        # 唯一合法挂起 = wait_on）。
        from hiveweave.config import turn_exit_mode

        if turn_exit_mode() == "auto":
            closing = (
                "收尾（唯一动作）: wait_on(refs=[{\"kind\": \"task\", "
                "\"ref\": \"" + tid + "\"}]) 登记等待后停止"
                "（commit_turn 已下架）"
            )
        else:
            closing = (
                "收尾（唯一动作）: commit_turn(phase=\"waiting\", "
                "waiting_on=[{\"kind\": \"task\", \"ref\": \"" + tid + "\"}])"
            )
        lines = [
            head,
            f"- 审计任务 id: {tid}（标题 {str(mint.get('title') or '')[:60]}）",
            f"- 审查者: {reviewer}" + (f"（{rid}）" if rid and rid != reviewer else ""),
            closing,
            "凭证落地后你会被唤醒；届时重新 submit_task(taskId=\"" + source_task_id
            + "\") —— 平台自动附上审计凭证，门即开。不要自己跑 "
            "request_code_audit，也不要盲目重交。",
        ]
        return "\n".join(lines)
    reason = str(mint.get("reason") or "error")
    if reason == "no_reviewer_available":
        return (
            "[AUDIT DISPATCH FAILED] 无可用审查者（qa_lead 不存在/已归档，"
            "且 submitter 无 parent coordinator）。审计门保持生效：请协调者/"
            "CEO 先补审查池（hire QA）或对本任务 waive_attestation"
            "（真实人工决策）。"
        )
    return (
        "[AUDIT DISPATCH FAILED] 审计任务铸造失败："
        + str(mint.get("detail") or reason)[:160]
        + "。审计门保持生效；可重试 submit（会再次尝试派单）或请协调者 "
        "waive_attestation。"
    )


async def rebind_audit_attestations_on_approve(
    project_id: str, audit_task: dict[str, Any]
) -> list[str]:
    """卡 approve 时把卡上的 code_audit 凭证**回写源任务**（T18 bug2 修复）。

    S16 实战：审计者把代审凭证挂在了**卡**上（taskId=卡 id），卡被 approve
    关闭后源任务的 submit 门纹丝不动——门要的是挂在（源任务 + submitter）上
    的 code_audit 凭证。修法：approve 收口时把卡上**代审产生**的凭证按
    ``authorId=submitter`` 的既有代审绑定语义复制回（源任务， submitter）。

    只回写可开门的凭证：``kind=code_audit`` + ``agent_id=submitter``（绑定
    即「审的是 submitter 的 worktree」——run_code_audit 的绑定契约）+
    ``exit_code=0``（PASS；ISSUES 本就开不了门，返修自会重审）+ 未过期。

    幂等：源任务上已有同指纹（stdout_hash + commit_hash，NULL 安全）的
    有效 code_audit 凭证（正确代审路径落的，或本函数早前已回写的）⇒ 跳过，
    不重复。失败由调用方兜（best-effort，不阻断 approve）。

    Returns 回写出的凭证 id 列表（空 = 无可回写或全部已存在）。
    """
    from hiveweave.services.attestation import (
        CODE_AUDIT_KIND,
        _conn,
        attestation_service,
        canonical_task_id,
    )

    card_id = str((audit_task or {}).get("id") or "")
    if not card_id:
        return []
    try:
        ev = (audit_task or {}).get("evidence")
        ev = json.loads(ev) if isinstance(ev, str) else (ev or {})
    except Exception:  # noqa: BLE001 — 坏 evidence 按空处理
        ev = {}
    if not isinstance(ev, dict):
        ev = {}
    source_task_id = str(ev.get(AUDIT_TASK_SOURCE_KEY) or "")
    submitter_id = str(ev.get("submitter_id") or (audit_task or {}).get("creator_id") or "")
    if not source_task_id or not submitter_id:
        return []

    card_tid = await canonical_task_id(project_id, card_id) or card_id
    source_tid = await canonical_task_id(project_id, source_task_id) or source_task_id
    now = int(time.time() * 1000)
    conn = await _conn(project_id)
    cur = await conn.execute(
        "SELECT id, workspace, commit_hash, stdout_hash, command_or_url "
        "FROM tool_attestations WHERE project_id = ? AND task_id = ? "
        "AND kind = ? AND agent_id = ? AND exit_code = 0 "
        "AND stdout_hash IS NOT NULL AND TRIM(stdout_hash) != '' "
        "AND (expires_at IS NULL OR expires_at > ?)",
        [project_id, card_tid, CODE_AUDIT_KIND, submitter_id, now],
    )
    rows = await cur.fetchall()
    await cur.close()

    rebound: list[str] = []
    for r in rows or []:
        row = dict(r)
        stdout_hash = str(row.get("stdout_hash") or "")
        commit_hash = str(row.get("commit_hash") or "")
        # 幂等判重：源任务上已有同指纹的有效凭证 ⇒ 不重复（含本函数早前
        # 已回写的行——二次 approve/重复收口零副作用）。
        cur2 = await conn.execute(
            "SELECT id FROM tool_attestations WHERE project_id = ? "
            "AND task_id = ? AND kind = ? AND agent_id = ? AND exit_code = 0 "
            "AND (expires_at IS NULL OR expires_at > ?) "
            "AND IFNULL(stdout_hash, '') = ? AND IFNULL(commit_hash, '') = ? "
            "LIMIT 1",
            [
                project_id, source_tid, CODE_AUDIT_KIND, submitter_id, now,
                stdout_hash, commit_hash,
            ],
        )
        dup = await cur2.fetchone()
        await cur2.close()
        if dup:
            continue
        orig_id = str(row.get("id") or "")
        new_id = await attestation_service.create(
            project_id,
            agent_id=submitter_id,
            kind=CODE_AUDIT_KIND,
            task_id=source_tid,
            exit_code=0,
            workspace=row.get("workspace"),
            commit_hash=commit_hash or None,
            stdout_hash=stdout_hash,
            command_or_url=(
                f"{str(row.get('command_or_url') or '').strip()} "
                f"[audit-rebind card={card_id[:8]} att={orig_id[:8]}]"
            ).strip(),
        )
        rebound.append(new_id)
    if rebound:
        log.info(
            "audit_card_attestation_rebound",
            project_id=project_id,
            audit_task_id=card_id[:8],
            source_task_id=str(source_tid)[:8],
            submitter_id=submitter_id[:8],
            rebound=[a[:8] for a in rebound],
        )
    return rebound
