"""Org hire/dismiss invariants — hard gates (标本兼治之本).

P0 Hard Gates extensions:
- HR cannot be a parent
- Flower name must not equal role / contain job-title literals
- At most one primary coordinator under the same parent
"""

from __future__ import annotations

import re
from typing import Any

from hiveweave.services.policy import is_test_engineer_role

# Span of control (直属 ≤5-7) is prompt-level IRON guidance only
# (prompts/coordinator.py) — NO code hard cap on headcount (2026-08-29):
# the old MAX_DIRECT_REPORTS=7 hire gate stranded QA hires (TEST_DSH_36)
# and froze HR when the 8th report was rejected. Beyond the guidance the
# system still succeeds but returns span_advisory() — add a coordinator
# layer and regroup leaves by functional module. B3 修法①② (2026-09-28):
# the advisory now uses TWO anchors — direct reports AND subtree size
# (含 indirect) — and is evaluated on coordinator hires too (adding a
# mid-layer is exactly the moment to check whether layers suffice).
SPAN_GUIDANCE = 7

_RESERVED_FLOWER_NAMES = frozenset({
    "归零", "知远",
})

# Names that look like job titles (forbidden as 花名)
_JOB_TITLE_LITERALS = (
    "工程师", "负责人", "测试", "架构师", "经理", "专员",
    "engineer", "manager", "lead", "architect", "director",
    "ceo", "hr", "qa",
)


def _active(agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [a for a in agents if (a.get("status") or "active") == "active"]


def _is_hr_agent(agent: dict[str, Any] | None) -> bool:
    if not agent:
        return False
    role = (agent.get("role") or "").strip().lower()
    return role == "hr" or "人力资源" in (agent.get("role") or "")


def _normalize_role_key(role: str) -> str:
    return re.sub(r"\s+", "", (role or "").strip().lower())


def validate_hire(
    *,
    agents: list[dict[str, Any]],
    name: str,
    role: str,
    permission_type: str,
    parent_id: str,
    bootstrap: bool = False,
) -> str | None:
    """Return an error message if hire violates invariants, else None.

    ``bootstrap=True`` skips reserved-name / HR-parent / flower-name checks
    for CEO/HR seed creation.
    """
    name = (name or "").strip()
    role = (role or "").strip()
    perm = (permission_type or "").strip().lower()
    parent_id = (parent_id or "").strip()
    active = _active(agents)

    if not name or not role:
        return "hire_agent requires non-empty name and role"

    if not bootstrap:
        # Reserved flower names (CEO identity etc.)
        if name in _RESERVED_FLOWER_NAMES:
            return (
                f"花名 '{name}' is reserved (CEO/HR name pool). "
                "Invent a unique flower-name; do not reuse the reserved pool."
            )

        # Flower name must not equal role (case-insensitive)
        if name.lower() == role.lower():
            return (
                f"花名 '{name}' must not equal role '{role}'. "
                "Use a personal flower-name, not the job title."
            )

        # Flower name must not contain job-title literals
        name_l = name.lower()
        for lit in _JOB_TITLE_LITERALS:
            if lit.lower() in name_l or lit in name:
                return (
                    f"花名 '{name}' looks like a job title (contains '{lit}'). "
                    "Use a personal flower-name e.g. 潮汐 / 墨白."
                )

    # Unique active 花名
    for a in active:
        if (a.get("name") or "").lower() == name.lower():
            return (
                f"Active agent already named '{name}' "
                f"({a.get('short_id')}, role={a.get('role')}). "
                "Use a unique 花名, or transfer_agent / dismiss the existing "
                "person first (prefer transfer over dismiss+rehire)."
            )

    # One module one owner: unique active executor role string
    if perm == "executor":
        bare = {"前端工程师", "后端工程师", "工程师", "frontend", "backend", "developer"}
        if role in bare or role.lower() in bare:
            return (
                f"Executor role '{role}' is too generic. "
                "Use module+craft form e.g. 「签到排行榜工程师」."
            )
        for a in active:
            if (
                a.get("permission_type") == "executor"
                and (a.get("role") or "") == role
            ):
                return (
                    f"Active executor already owns role '{role}' "
                    f"({a.get('name')}/{a.get('short_id')}). "
                    "One module → one owner. transfer_agent or dismiss the "
                    "existing owner before hiring a replacement."
                )

    # Resolve parent
    parent: dict[str, Any] | None = None
    if parent_id:
        for a in agents:
            if a.get("id") == parent_id or a.get("short_id") == parent_id:
                parent = a
                break

    if parent and (parent.get("status") or "") == "archived":
        return (
            f"Parent agent is archived ({parent.get('name')}). "
            "Choose an active coordinator as parentId."
        )

    # HR never has children
    if not bootstrap and parent and _is_hr_agent(parent):
        return (
            "HR cannot have subordinates (IRON RULE). "
            "Set parentId to CEO or a domain coordinator, not HR."
        )

    # Executors must not report to CEO (hire + transfer share this rule)
    if perm == "executor" and parent:
        ceo_err = _executor_under_ceo_error(parent)
        if ceo_err:
            return ceo_err

    # At most one primary coordinator under the same parent
    # (approximate domain by parent_id; exclude CEO/HR unique roles)
    if (
        not bootstrap
        and perm == "coordinator"
        and parent_id
        and role.lower() not in ("ceo", "hr")
    ):
        role_key = _normalize_role_key(role)
        for a in active:
            if (
                a.get("permission_type") == "coordinator"
                and (a.get("parent_id") or "") == (parent.get("id") if parent else parent_id)
                and _normalize_role_key(a.get("role") or "") == role_key
            ):
                return (
                    f"Parent already has coordinator with role '{role}' "
                    f"({a.get('name')}/{a.get('short_id')}). "
                    "One primary coordinator per domain/parent. "
                    "transfer_agent or dismiss before hiring a duplicate."
                )

    return None


def _executor_under_ceo_error(parent: dict[str, Any] | None) -> str | None:
    """Hard rule: executors never report directly to CEO."""
    if not parent:
        return None
    prole = (parent.get("role") or "").strip().lower()
    if prole == "ceo":
        return (
            "Executors cannot report directly to CEO (span / org design). "
            "Set parentId to a coordinator (architect / tech lead / manager). "
            "NEXT: if no coordinator exists, hire one first "
            "(permissionType=coordinator, parentId=CEO), then hire this "
            "executor with parentId=<that coordinator>. Do not ask CEO to "
            "confirm — this is the only legal remedy."
        )
    return None


def _is_business_coordinator(agent: dict[str, Any]) -> bool:
    """业务中层 = active coordinator 且不是 CEO/HR（HR 不得有下属）。"""
    perm = str(agent.get("permission_type") or "").strip().lower()
    if perm != "coordinator":
        return False
    return _normalize_role_key(agent.get("role") or "") not in ("ceo", "hr")


def count_business_coordinators(agents: list[dict[str, Any]] | None) -> int:
    return sum(1 for a in _active(agents or []) if _is_business_coordinator(a))


def _subtree_size(
    agents: list[dict[str, Any]], pid: str, exclude_id: str | None
) -> int:
    """Active descendants of ``pid`` at any depth (excluding exclude_id)."""
    children: dict[str, list[str]] = {}
    for a in _active(agents):
        par = str(a.get("parent_id") or "")
        aid = str(a.get("id") or "")
        if par and aid:
            children.setdefault(par, []).append(aid)
    seen: set[str] = set()
    stack = list(children.get(pid, []))
    while stack:
        cur = stack.pop()
        if not cur or cur in seen:
            continue
        seen.add(cur)
        stack.extend(children.get(cur, []))
    seen.discard(exclude_id or "")
    return len(seen)


def _span_subtree_text(
    parent_name: str, n: int, *, adding_coordinator: bool
) -> str:
    head = "⚠️ SPAN ADVISORY（非阻塞提示，本次操作已生效）："
    if adding_coordinator:
        return (
            f"{head}加了这个中层后 {parent_name} 辖下（含间接下属）仍有 {n} 人"
            f"（组织设计指导值 ≤{SPAN_GUIDANCE}）。人数没有硬上限，"
            "但可继续按功能模块评估是否再加一层 coordinator 中层分担；"
            "请在回报中带上这个分层评估。"
        )
    return (
        f"{head}{parent_name} 辖下（含间接下属）将达 {n} 人"
        f"（组织设计指导值 ≤{SPAN_GUIDANCE}）。人数没有硬上限，"
        "但单个中层辖下总人数偏多时，更好的结构是在其辖下再加一层 "
        "coordinator 中层、把叶子按功能模块分组挂靠；"
        "请在回报中带上这个分层建议。"
    )


def span_advisory(
    *,
    agents: list[dict[str, Any]],
    parent_id: str,
    exclude_id: str | None = None,
    adding_coordinator: bool = False,
) -> str | None:
    """Non-blocking hint when a parent would exceed the span guidance.

    B3 修法①②（2026-09-28 报告 §2-B3）：判据从「直属人数」扩成双判据——
    (a) 直属：parent 直辖 active 下属 +1 超 SPAN_GUIDANCE（原判据；
        adding_coordinator 时跳过——加中层本身就是对直属超编的救治）；
    (b) subtree：parent 辖下（含 indirect）总人数 +1 超 SPAN_GUIDANCE
        ——锚到「辖下是否臃肿」，中层直属恒 1-7 不代表辖下不臃肿；
        adding_coordinator 时同样评估（去掉旧短路：加中层那一刻恰是
        评估「加得够不够」的时刻，文案自然变成「加了这个中层后
        X 辖下仍 N 人」）。
    Hire/transfer always succeed; the caller appends the returned text to
    the tool receipt so the agent knows to layer the org. Non-blocking —
    指导值，平台不拦（TEST_DSH_36 前车之鉴，不得改硬闸）。
    """
    parent_id = (parent_id or "").strip()
    if not parent_id:
        return None
    pid = parent_id
    parent_name = parent_id
    for a in agents:
        if a.get("id") == parent_id or a.get("short_id") == parent_id:
            pid = a["id"]
            parent_name = a.get("name") or pid
            break
    # exclude_id = the moved agent in a transfer (may already sit under
    # this parent); the caller always counts it back in via +1 below.
    kids = [
        a
        for a in _active(agents)
        if (a.get("parent_id") or "") == pid and a.get("id") != exclude_id
    ]
    resulting_direct = len(kids) + 1
    resulting_subtree = _subtree_size(agents, pid, exclude_id) + 1

    notes: list[str] = []
    direct_fires = resulting_direct > SPAN_GUIDANCE and not adding_coordinator
    if direct_fires:
        notes.append(
            f"⚠️ SPAN ADVISORY（非阻塞提示，本次操作已生效）：{parent_name} "
            f"的直属将达 {resulting_direct} 人（组织设计指导值 "
            f"≤{SPAN_GUIDANCE}）。"
            "人数没有硬上限，但直属偏多时更好的结构是再加一层 coordinator 中层、"
            "把叶子按功能模块分组挂靠：先 hire_agent 一名 coordinator"
            "（permissionType=coordinator，parentId=现上级），再用 transfer_agent "
            "把相关模块的叶子挂到新中层名下。请在回报中带上这个分层建议。"
        )
    # Subtree criterion. Skip when it is the same fact as the direct note
    # (no intermediate layer → subtree == direct, already reported).
    if resulting_subtree > SPAN_GUIDANCE and not (
        direct_fires and resulting_subtree == resulting_direct
    ):
        notes.append(
            _span_subtree_text(
                parent_name, resulting_subtree, adding_coordinator=adding_coordinator
            )
        )
    if not notes:
        return None
    return "\n".join(notes)


def staffing_advisory(
    *,
    agents: list[dict[str, Any]],
    tasks: list[dict[str, Any]] | None,
) -> str | None:
    """Non-blocking hint when hiring an executor with more hands than work.

    dsh42 实证：招 6 executor 只有 4 个可做任务位，2 人 Reserve 待命
    30min，CEO 点名「职责重叠」。本提示在 hire 回执里软引导 HR/中层先看
    待派活存量再扩编。``tasks`` 传 None / 查询失败时返回 None（fail-open，
    不阻断 hire）。只统计 executor 可做的 open 任务，口径：未被 claim
    （无 assignee 的 created/pending 草稿）或**无主** blocked 停靠位 ——
    blocked 已有 assignee 是「有主的停靠位」，不是可派活存量（P2-3 审计
    修正，防存量虚高）。VERIFY 任务是 QA 岗活，不计入。
    """
    if tasks is None:
        return None
    executors = [
        a
        for a in (agents or [])
        if (a.get("status") or "active") == "active"
        and str(a.get("permission_type") or "").strip().lower() == "executor"
    ]
    exec_n = len(executors)
    if exec_n <= 0:
        return None
    is_verify = None
    try:
        from hiveweave.services.tasks.verify import is_verify_task as _ivt

        is_verify = _ivt
    except Exception:
        is_verify = None
    open_n = 0
    for t in tasks or []:
        if t.get("is_archived"):
            continue
        status = str(t.get("status") or "").strip().lower()
        try:
            if is_verify is not None and is_verify(t):
                continue  # VERIFY 是 QA 岗位，不是 executor 待派活
        except Exception:
            pass
        assigned = bool(str(t.get("assignee_id") or "").strip())
        if status == "blocked":
            # P2-3：blocked 已有主 = 有主停靠位，不计可派活存量
            if not assigned:
                open_n += 1
        elif status in ("created", "pending") and not assigned:
            open_n += 1
    if open_n >= exec_n:
        return None
    return (
        f"⚠️ STAFFING ADVISORY（非阻塞提示，本次操作已生效）："
        f"当前待派活任务 {open_n} < 在编执行者 {exec_n}，"
        f"确认是否需要扩编（Reserve 待命成本）。"
        f"优先给在编执行者派活或合并职责；确有新模块再做扩编，"
        f"并在回报中说明新增人力的任务来源。"
    )


def validate_transfer(
    *,
    agents: list[dict[str, Any]],
    agent_id: str,
    new_parent_id: str | None,
) -> str | None:
    """Return error if transfer would break org invariants, else None.

    Mirrors hire gates that apply to re-parenting (executor↛CEO, no HR
    parent). Does not re-check flower-name uniqueness.
    """
    target: dict[str, Any] | None = None
    for a in agents:
        if a.get("id") == agent_id or a.get("short_id") == agent_id:
            target = a
            break
    if not target:
        return f"Agent not found: {agent_id}"

    new_parent_id = (new_parent_id or "").strip() or None
    parent: dict[str, Any] | None = None
    if new_parent_id:
        for a in agents:
            if a.get("id") == new_parent_id or a.get("short_id") == new_parent_id:
                parent = a
                break
        if parent is None:
            return f"New parent not found: {new_parent_id}"
        if (parent.get("status") or "") == "archived":
            return (
                f"Parent agent is archived ({parent.get('name')}). "
                "Choose an active coordinator as newParentId."
            )
        if _is_hr_agent(parent):
            return (
                "HR cannot have subordinates (IRON RULE). "
                "Set newParentId to CEO or a domain coordinator, not HR."
            )

    perm = (target.get("permission_type") or "").strip().lower()
    if perm == "executor":
        ceo_err = _executor_under_ceo_error(parent)
        if ceo_err:
            return ceo_err
        # Also reject transfer to root (no parent) — that is CEO-equivalent span
        if parent is None:
            return (
                "Executors cannot be root / report to no one. "
                "Set newParentId to a coordinator (architect / tech lead / manager)."
            )

    return None


def qa_depth_advisory(
    *,
    agents: list[dict[str, Any]],
    tasks: list[dict[str, Any]] | None,
) -> str | None:
    """Milestone-VERIFY creation hint when independent leaf-QA depth is thin.

    45 轮 C 案③（42 轮 P7 实证：VERIFY 单点 QA + 串行锁 → 停摆 4.4h，
    恢复只剩补招一条）。在 milestoneVerify 创建回执里软引导：独立叶子
    QA（fam=qa）为 0、或多 VERIFY 排队只有 1 QA 时提示扩编。fail-open，
    不阻断创建；qa_lead 是 coordinator 家族（policy.py:289），天然不计入
    叶子 QA 数。
    """
    if tasks is None:
        return None
    try:
        from hiveweave.services.policy import infer_role_family

        qa_n = len([
            a
            for a in (agents or [])
            if (a.get("status") or "active") == "active"
            and infer_role_family(a) == "qa"
        ])
    except Exception:
        return None
    is_verify = None
    try:
        from hiveweave.services.tasks.verify import is_verify_task as _ivt

        is_verify = _ivt
    except Exception:
        return None
    open_verify = 0
    for t in tasks or []:
        if t.get("is_archived"):
            continue
        if str(t.get("status") or "").strip().lower() in (
            "closed",
            "cancelled",
        ):
            continue
        try:
            if is_verify(t):
                open_verify += 1
        except Exception:
            pass
    if qa_n == 0:
        return (
            " QA depth note: no active leaf QA (role family qa) in this "
            "project — only the QA lead. VERIFY runs serially on MAIN; "
            "several milestones in flight will queue on one verifier. "
            "Consider asking HR to hire a leaf test_engineer QA."
        )
    if qa_n == 1 and open_verify >= 2:
        return (
            f" QA depth note: {open_verify} open VERIFY tasks but only 1 "
            "leaf QA — the VERIFY serial lock means they queue. Consider "
            "hiring another test_engineer QA for depth."
        )
    return None


# ── B3 ③④⑤：charter 编制结构 / 项目级形态信号（2026-09-28）────────


# charter 范式枚举（B3 修法③）。与 prompts/coordinator.py 的
# Organizational Paradigm Library 六范式一一对应；存储/解析在
# services/charter.py（[org-shape] 标记行，零 schema 变更）。
CHARTER_PARADIGMS = frozenset(
    {"solo", "flat_squad", "tech_lead", "pm_architect", "pod", "pipeline"}
)


def min_business_coordinators(
    paradigm: str, pod_count: int | None = None
) -> int | None:
    """paradigm → 平台算出的最低业务中层数（B3 修法③）。

    solo/flat_squad/tech_lead/pipeline → 1；pm_architect → 2；
    pod → pod 数（未声明则取下限 2）。未知范式 → None（调用方跳过校验，
    读旧 charter 无字段时不拦）。
    """
    p = (paradigm or "").strip().lower()
    if p in ("solo", "flat_squad", "tech_lead", "pipeline"):
        return 1
    if p == "pm_architect":
        return 2
    if p == "pod":
        try:
            n = int(pod_count) if pod_count is not None else 0
        except (TypeError, ValueError):
            n = 0
        return n if n >= 1 else 2
    return None


def org_shape_signals(*, agents: list[dict[str, Any]] | None) -> list[str]:
    """项目级组织形态异常信号（B3 修法⑤，非阻塞）。

    实测形态（2026-09-28 报告 §2-B3）：83.3% 项目收敛到「恰 1 个业务
    中层」，其中 24 个项目呈「1 业务中层 + ≥6 叶子」——设计/派活/验收
    单点化，是里程碑瓶颈的高发形态。返回人类可读信号行（空列表 = 健康）；
    挂到组织视野回执（check_agent_status 全员视图）供 CEO 里程碑审查时
    参考。fail-open：输入异常返回空列表，绝不 raise。
    """
    try:
        active = _active(agents or [])
        biz = [a for a in active if _is_business_coordinator(a)]
        leaves = [
            a
            for a in active
            if str(a.get("permission_type") or "").strip().lower() == "executor"
        ]
        if len(biz) == 1 and len(leaves) >= 6:
            owner = biz[0].get("name") or biz[0].get("short_id") or "?"
            return [
                (
                    f"⚠️ ORG SHAPE SIGNAL（非阻塞）：全项目只有 1 个业务中层"
                    f"（{owner}）辖下共 {len(leaves)} 个叶子——设计/派活/验收"
                    f"单点化，是里程碑瓶颈的高发形态。里程碑审查时评估按交付面"
                    f"加层（hire coordinator + transfer 分组）；确需保持单中层"
                    f"请在 charter 写明理由（指导值，平台不拦）。"
                )
            ]
        return []
    except Exception:
        return []


def charter_shape_advisory(
    *,
    charter: dict[str, Any] | None,
    agents: list[dict[str, Any]] | None,
) -> str | None:
    """B3 修法④：faces × 中层数一致性非阻塞校验。禁止拒存，fail-open。

    触发条件（全部满足才提示）：charter 带已解析的 paradigm/faces 字段
    （services/charter.py read_charter 从 [org-shape] 标记行解析）；
    faces ≥2；该 paradigm 未声明多中层（min_business_coordinators==1）；
    现有业务中层数 M < faces。旧 charter（无字段/未知范式）直接返回
    None——读旧 charter 无字段时跳过校验。
    """
    try:
        if not isinstance(charter, dict):
            return None
        paradigm = str(charter.get("paradigm") or "").strip().lower()
        if paradigm not in CHARTER_PARADIGMS:
            return None
        faces_raw = charter.get("faces")
        if faces_raw is None:
            return None
        faces = int(faces_raw)
        if faces <= 1:
            return None
        pod_raw = charter.get("pod_count")
        pod = int(pod_raw) if pod_raw is not None else None
        if (min_business_coordinators(paradigm, pod) or 1) > 1:
            return None  # paradigm 本身声明了多中层
        m = count_business_coordinators(agents or [])
        if faces <= m:
            return None
        return (
            f"⚠️ CHARTER SHAPE ADVISORY（非阻塞提示）：charter 声明了 {faces} 个"
            f"独立交付面（paradigm={paradigm}），但当前只有 {m} 个业务中层"
            f"——若确需一人多面/合并面，请在 charter 写明合并理由；"
            f"否则按面配置中层（指导值，平台不拦）。"
        )
    except Exception:
        return None
