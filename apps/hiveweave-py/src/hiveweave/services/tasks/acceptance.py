"""VERIFY 验收清单（acceptance_criteria）覆盖门 —— **状态判据**版（#14）。

修前（双向可绕）：把 ``acceptance_criteria`` 的**原文**与 evidence 自由文本做
CN/EN 正则 + 归一子串比对 ⇒ 抄条目原文即"覆盖"（假通过）、换个措辞写即
"未覆盖"（真交付被误拒）；`N/A: <理由>` 也只要理由非空即豁免。

现判据 = **结构化 id 集合包含 + 平台可核验凭证**，与措辞/语言无关：

- ``acceptance_criteria`` 每条有**稳定 id**（dict 的 ``id``/``key``/``ref``，
  否则按 1-based 序号 ``"1"``…``"N"`` —— 与既有 ``条目N`` 文案一致）。
  id 由**平台侧**确定，agent 不能自选（声明了不存在的 id 视为无效声明）。
- evidence 用 ``acceptance_coverage`` 逐条声明覆盖关系：::

      "acceptance_coverage": {
        "1": {"attestation_ids": ["<test_run 凭证 id>"]},
        "2": {"not_applicable_reason": "本环境无边界数据集"}
      }

- 条目判「已覆盖」当且仅当满足其一：
  ① 声明了该 id，且其 ``attestation_ids`` 中至少一条经
     ``attestation_service.verify_ids(kinds=['test_run'], task_id=…)`` **核验通过**
     （本任务、kind 正确、未过期、exit_code=0、stdout_hash 齐备）；
  ② 声明了 ``not_applicable_reason``（≥2 字符）**且本任务存在有效平台 waiver 行**
     （``has_valid_waiver``：coordinator 签发、落 ``tool_attestations``、
     ``MAX_WAIVERS_PER_TASK`` 终身上限、返修退役）。

⇒ 抄原文 / 换措辞 / 裸写 ``N/A: <理由>`` **都不再构成覆盖**；「声明了 id 但没测」
也过不了（声明必须锚在 test_run 凭证上）。纯机械判定，不做语义判断；本模块只
负责**判定与点名**，拒绝动作在调用方。

⚠ **接线要求**：凭证核验需要 project/task 上下文，故权威入口是 async 的
:func:`uncovered_acceptance_items_verified`。同步的
:func:`uncovered_acceptance_items` **不做核验**，未传 ``verified_ids`` 时一律判
未覆盖（fail-closed）——门禁调用点必须用 async 版（或自己核验后传
``verified_ids``），不得靠自由文本兜底。

``check_evidence_verifiable`` 对 VERIFY 的既有跳过逻辑不受影响（那是 approve 侧
路径证据校验，语义不同）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# 覆盖声明的 evidence 字段名（结构化，唯一判据来源）。
COVERAGE_EVIDENCE_KEY = "acceptance_coverage"

# 条目声明「已覆盖」时所引用的凭证 kind（平台机器核验，不是自述）。
DEFAULT_COVERAGE_KINDS: tuple[str, ...] = ("test_run",)

ATTESTATION_IDS_FIELD = "attestation_ids"
NOT_APPLICABLE_REASON_FIELD = "not_applicable_reason"

# 声明里的 id 键（平台侧来源；dict 形态的 acceptance_criteria 用）。
_ID_KEYS = ("id", "key", "ref")

_WS_RE = re.compile(r"\s+")


def _norm(text: Any) -> str:
    return _WS_RE.sub(" ", str(text or "")).strip().lower()


@dataclass(frozen=True)
class AcceptanceItem:
    """一条验收条目：**平台侧稳定 id** + 原文。"""

    id: str
    text: str


def parse_acceptance_plan(criteria: Any) -> list[AcceptanceItem]:
    """``acceptance_criteria`` → 结构化条目（id + 原文），按原顺序。

    接受 list[str] / list[dict{text,...}] / JSON 字符串 / 单条字符串。
    dict 形态取 ``text``（退化 description / criterion / 整体字符串化），
    并取 ``id``/``key``/``ref`` 作为条目 id；缺 id 时用 1-based 序号。
    """
    if criteria is None:
        return []
    items: Any
    if isinstance(criteria, str):
        try:
            parsed = json.loads(criteria)
        except Exception:
            items = [criteria]
        else:
            items = parsed if isinstance(parsed, list) else [parsed]
    elif isinstance(criteria, (list, tuple)):
        items = list(criteria)
    else:
        items = [criteria]
    out: list[AcceptanceItem] = []
    for raw in items:
        explicit_id = ""
        if isinstance(raw, dict):
            for k in _ID_KEYS:
                v = str(raw.get(k) or "").strip()
                if v:
                    explicit_id = v
                    break
            text = (
                raw.get("text")
                or raw.get("description")
                or raw.get("criterion")
                or ""
            )
            text = str(text or "").strip() or str(raw).strip()
        else:
            text = str(raw or "").strip()
        if text:
            out.append(
                AcceptanceItem(
                    id=explicit_id or str(len(out) + 1), text=text
                )
            )
    return out


def parse_acceptance_items(criteria: Any) -> list[str]:
    """``acceptance_criteria`` → 非空条目文本 list（形状兼容保留）。"""
    return [it.text for it in parse_acceptance_plan(criteria)]


def parse_acceptance_coverage(
    evidence: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """``evidence.acceptance_coverage`` → ``{item_id: claim}``。

    claim = ``{"attestation_ids": [str…], "not_applicable_reason": str}``。
    支持 dict 形态（``{"1": {...}}`` 或 ``{"1": ["<id>"]}``）与 list 形态
    （``[{"id": "1", "attestation_ids": [...]}]``）。形状不对的声明**一律忽略**
    （忽略 = 未覆盖，fail-closed）；无声明 → 空 dict。
    """
    if not isinstance(evidence, dict):
        return {}
    raw = evidence.get(COVERAGE_EVIDENCE_KEY)
    if raw is None:
        return {}

    def _claim(value: Any) -> dict[str, Any] | None:
        if isinstance(value, str):
            v = value.strip()
            return {ATTESTATION_IDS_FIELD: [v]} if v else None
        if isinstance(value, (list, tuple)):
            ids = [str(x).strip() for x in value if str(x or "").strip()]
            return {ATTESTATION_IDS_FIELD: ids} if ids else None
        if isinstance(value, dict):
            ids_raw = value.get(ATTESTATION_IDS_FIELD)
            if isinstance(ids_raw, str):
                ids_raw = [ids_raw]
            ids = (
                [str(x).strip() for x in ids_raw if str(x or "").strip()]
                if isinstance(ids_raw, (list, tuple))
                else []
            )
            reason = str(value.get(NOT_APPLICABLE_REASON_FIELD) or "").strip()
            if not ids and not reason:
                return None
            return {
                ATTESTATION_IDS_FIELD: ids,
                NOT_APPLICABLE_REASON_FIELD: reason,
            }
        return None

    out: dict[str, dict[str, Any]] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            item_id = str(k or "").strip()
            claim = _claim(v)
            if item_id and claim:
                out[item_id] = claim
    elif isinstance(raw, (list, tuple)):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            item_id = ""
            for k in _ID_KEYS:
                v = str(entry.get(k) or "").strip()
                if v:
                    item_id = v
                    break
            claim = _claim(entry)
            if item_id and claim:
                out[item_id] = claim
    return out


def _label(item: AcceptanceItem) -> str:
    return f"条目{item.id}: {item.text}"


def _declared_na_reasons(
    claims: dict[str, dict[str, Any]], known_ids: set[str]
) -> bool:
    """声明里是否有「已知条目 + 非空 not_applicable_reason」（决定是否查 waiver）。"""
    for item_id, claim in claims.items():
        if item_id not in known_ids:
            continue
        if len(str(claim.get(NOT_APPLICABLE_REASON_FIELD) or "").strip()) >= 2:
            return True
    return False


async def _any_attestation_verified(
    project_id: str,
    attestation_ids: list[str],
    *,
    task_id: str | None,
    expected_agent_id: str | None,
    kinds: tuple[str, ...],
) -> bool:
    """``attestation_ids`` 中是否**至少一条**是本任务、kind ∈ ``kinds`` 的有效凭证。

    ⚠ ``verify_ids`` 的 ``expected_kinds`` 是 **AND 语义**（"ALL required kinds
    must be present"，且是**跨整个 id 列表**求和）⇒ 不能用它表达"kinds 里任一
    即可"（单 id 传 4 种 kind 会必然失败）。故这里：先自己读行判 ``kind ∈ kinds``
    （OR），再让 ``verify_ids`` 以 ``expected_kinds=None`` 兜「存在/未过期/年龄/
    agent 与 task 绑定/stdout_hash 齐备/exit_code=0」全部其余维度。

    ⚠ **额外收紧（有意，比全局策略更严）**：``verify_ids`` →
    ``check_attestation_reuse_binding`` 允许**同 agent 跨任务复用**（无 commit_hash
    时直接放行）。但验收覆盖的语义是"**本任务**的条目被验证过"，跨任务凭证正是
    「声明了 id 但实际没测本任务」的通道 ⇒ 这里再核一次行上的 ``task_id``
    （canonical 相等），并 fail-closed 要求 ``task_id`` 非空（验收门只服务有任务
    行的 VERIFY）。
    """
    if not attestation_ids or not task_id:
        return False
    from hiveweave.services.attestation import (
        attestation_service,
        canonical_task_id,
    )

    want = await canonical_task_id(project_id, task_id)
    if not want:
        return False
    allowed_kinds = frozenset(kinds)
    for aid in attestation_ids:
        row = await attestation_service.get(project_id, aid)
        if not row:
            continue
        if str(row.get("kind") or "") not in allowed_kinds:
            continue
        ok, _err = await attestation_service.verify_ids(
            project_id,
            [aid],
            task_id=task_id,
            expected_agent_id=expected_agent_id,
        )
        if not ok:
            continue
        row_task = str(row.get("task_id") or "")
        if not row_task:
            continue
        if (await canonical_task_id(project_id, row_task)) != want:
            continue
        return True
    return False


async def _task_has_valid_waiver(project_id: str, task_id: str | None) -> bool:
    if not task_id:
        return False
    from hiveweave.services.attestation import has_valid_waiver

    try:
        return bool(await has_valid_waiver(project_id, str(task_id)))
    except Exception:  # noqa: BLE001 — 拿不到 waiver 事实即不豁免（fail-closed）
        return False


async def acceptance_coverage_kinds(task: dict[str, Any] | None) -> tuple[str, ...]:
    """任务行 → 本条验收条目据以核验的**执行凭证 kind** 集合。

    #14 的另一半是"真交付被误拒"：只认 ``test_run`` 会让 **UI(`browse_e2e`) /
    文档(`doc_review`) 类条目永远拿不到覆盖**（本仓 VERIFY 的默认 policy 是
    soft 的 ``coordinator_review``，UI 项目 QA 常常只有 browse_e2e/视觉凭证）。

    取法：
    - policy 明确要求执行类 kind（`docs_only`→doc_review / `ui_browser_e2e`→
      browse_e2e / `generic_tests`→test_run …）⇒ **用该集合**（与 attestation
      门同口径，更严）；
    - soft（`coordinator_review` = None）/ 未知 policy / 要求的是非执行类
      （如 `code_audit`）⇒ 回落**全部执行类 kind**（= ``WAIVER_EVIDENCE_KINDS``
      —— 平台自己定义的"执行证据"集合），并 **fail-loud 留日志**（soft 用
      info：这是 VERIFY 的常态；未知/不可用用 warning）。
    任何异常 ⇒ 回落 :data:`DEFAULT_COVERAGE_KINDS` + warning（不静默）。
    """
    try:
        from hiveweave.services.attestation import (
            WAIVER_EVIDENCE_KINDS,
            ledger_policy_id,
            required_attestation_kinds,
        )

        execution_kinds = tuple(sorted(WAIVER_EVIDENCE_KINDS))
        policy_id = ledger_policy_id(task or {})
        needed = required_attestation_kinds(policy_id)
        if needed:
            usable = tuple(
                sorted(k for k in needed if k in WAIVER_EVIDENCE_KINDS)
            )
            if usable:
                return usable
            log.warning(
                "acceptance_coverage_policy_kinds_not_execution_evidence",
                policy_id=policy_id,
                kinds=sorted(needed),
            )
        else:
            log.info(
                "acceptance_coverage_policy_soft_all_execution_kinds",
                policy_id=policy_id,
                kinds=list(execution_kinds),
            )
        return execution_kinds
    except Exception as e:  # noqa: BLE001 — 取不到就回落且留痕（不静默）
        log.warning("acceptance_coverage_kinds_lookup_failed", error=str(e))
        return DEFAULT_COVERAGE_KINDS


async def uncovered_acceptance_items_verified(
    project_id: str,
    task_id: str | None,
    criteria: Any,
    evidence: dict[str, Any] | None,
    *,
    expected_agent_id: str | None = None,
    kinds: tuple[str, ...] = DEFAULT_COVERAGE_KINDS,
) -> list[str]:
    """**权威覆盖门**：返回未覆盖条目标签（``条目N: <原文>``）；空 = 全覆盖。

    覆盖判据见模块 docstring（声明 id + 平台核验凭证；不适用条目 = 非空理由 +
    本任务有效平台 waiver 行）。未知 id 的声明单独点名（无效声明）。
    """
    items = parse_acceptance_plan(criteria)
    if not items:
        return []
    known = {it.id for it in items}
    claims = parse_acceptance_coverage(evidence)
    if not claims:
        return [_label(it) for it in items]

    waived = False
    if _declared_na_reasons(claims, known):
        waived = await _task_has_valid_waiver(project_id, task_id)

    covered: set[str] = set()
    for item_id, claim in claims.items():
        if item_id not in known:
            continue
        if await _any_attestation_verified(
            project_id,
            list(claim.get(ATTESTATION_IDS_FIELD) or []),
            task_id=task_id,
            expected_agent_id=expected_agent_id,
            kinds=kinds,
        ):
            covered.add(item_id)
            continue
        if waived and len(
            str(claim.get(NOT_APPLICABLE_REASON_FIELD) or "").strip()
        ) >= 2:
            covered.add(item_id)

    gaps = [_label(it) for it in items if it.id not in covered]
    unknown = sorted(i for i in claims if i not in known)
    if unknown:
        gaps.append(
            "无效覆盖声明（未知条目 id）：" + ", ".join(unknown)
            + f"；本任务有效 id：{sorted(known, key=lambda x: (len(x), x))}"
        )
    return gaps


def uncovered_acceptance_items(
    criteria: Any,
    evidence: dict[str, Any] | None,
    *,
    verified_ids: set[str] | frozenset[str] | None = None,
    waived: bool = False,
) -> list[str]:
    """同步形态（**本函数不做任何凭证核验**）。

    ``verified_ids`` = 调用方**已经核验过**的凭证 id 集合（本任务 + kind 正确 +
    未过期）；``waived`` = 调用方已确认本任务存在有效平台 waiver 行。

    二者都缺省时**一律判未覆盖**（fail-closed）——自由文本不再是判据。
    ⚠ 门禁应直接用 :func:`uncovered_acceptance_items_verified`（自含核验），
    本函数只服务已自行核验过的调用方与纯形状测试。
    """
    items = parse_acceptance_plan(criteria)
    if not items:
        return []
    known = {it.id for it in items}
    claims = parse_acceptance_coverage(evidence)
    verified = set(verified_ids or ())
    covered: set[str] = set()
    for item_id, claim in claims.items():
        if item_id not in known:
            continue
        if any(
            a in verified for a in claim.get(ATTESTATION_IDS_FIELD) or []
        ):
            covered.add(item_id)
            continue
        if waived and len(
            str(claim.get(NOT_APPLICABLE_REASON_FIELD) or "").strip()
        ) >= 2:
            covered.add(item_id)
    gaps = [_label(it) for it in items if it.id not in covered]
    unknown = sorted(i for i in claims if i not in known)
    if unknown:
        gaps.append(
            "无效覆盖声明（未知条目 id）：" + ", ".join(unknown)
            + f"；本任务有效 id：{sorted(known, key=lambda x: (len(x), x))}"
        )
    if gaps and verified_ids is None:
        # 自诊断：无核验结果 = 调用点没接权威门（#14 要求核验锚在
        # tool_attestations 凭证上）。这是**接线故障**，不是条目没写清楚。
        gaps.append(
            "⚠ 验收覆盖门未接线：调用方未提供任何凭证核验结果"
            "（应用 uncovered_acceptance_items_verified；或传 "
            "verified_ids=已核验凭证 id 集合）"
        )
    return gaps


def format_acceptance_coverage_error(
    missing: list[str], kinds: tuple[str, ...] | None = None
) -> str:
    """E1 验收清单缺覆盖的拒绝文案（点名缺哪几条 + 处方）。

    ⚠ **处方里的凭证 kind 必须按本任务的 policy 渲染**（F1，2026-09-17）。
    原先这里把 `test_run` 写死成通用例子，而"照抄处方"正是 agent 最自然的
    行为 ⇒ **UI 任务（policy=`ui_browser_e2e`）的 agent 挂上 test_run 后被
    attestation 门拒**（`'test_run' not in expected ['browse_e2e']`），
    表现为"两道门互相矛盾、无解"。

    实证（TEST_DSH_61，任务 `309e2489`）：
      · 05:58:35 第一次 submit → 被本门拒（未声明 acceptance_coverage）
      · 06:00:14 第二次按处方补挂 `test_run`（`node -e` 静态解析）→
        被 attestation 门拒（expected=['browse_e2e']）
      · 该任务当时**已有 24 条 `browse_e2e` 凭证**（全 exit=0，含
        goto/snapshot/screenshot/click/eval）—— 正确做法本就在手边，
        是**处方把人指错了路**。

    本函数属 #16「说明书式披露」同族：平台给出的示例被当成规格照搬。
    修法 = 按 `kinds` 渲染**本任务真正认的**凭证类型，并在多 kind 时
    说明用什么工具产出（browse_e2e 由 `browse` 工具产出，test_run 由
    `pwsh`/`run_command` 带 `testEvidence=true` 产出）。
    """
    _ks = tuple(k for k in (kinds or ()) if k)
    if not _ks:
        _ks = ("test_run",)
    # 逐 kind 给出「怎么产出」的可操作指引 —— 只报 kind 名等于把问题
    # 原样退还给 agent。
    _HOW: dict[str, str] = {
        "test_run": "跑测试命令（`pwsh`/`run_command`，带 testEvidence=true）",
        "browse_e2e": "用 `browse` 工具做真实浏览器交互（goto/snapshot/"
                      "click/screenshot，exit_code=0）",
        "doc_review": "由 reviewer 对该文档落 `doc_review` 凭证",
        "code_audit": "`request_code_audit` 唤起的审计凭证",
        "manual_review": "由 reviewer 落的 `manual_review` 凭证",
    }
    _hint_lines = "\n".join(
        f"  - `{k}`：{_HOW.get(k, '平台认可的该 kind 凭证')}" for k in _ks
    )
    _example = (
        f'{{"1": {{"attestation_ids": ["<{_ks[0]} 凭证 id>"]}}}}'
    )
    _multi = (
        f"（本任务认这 {len(_ks)} 类：" + "、".join(f"`{k}`" for k in _ks) + "）"
        if len(_ks) > 1
        else f"（本任务只认 `{_ks[0]}`）"
    )
    return (
        "SUBMIT REJECTED (verify acceptance checklist): 任务带 "
        "acceptance_criteria，verdict evidence 未体现对以下 "
        f"{len(missing)} 条的覆盖：\n- "
        + "\n- ".join(missing)
        + f"\n处方：在 verdict evidence 加 `acceptance_coverage`，逐条按 id 声明覆盖"
        "（条目N → id 见上方清单），形如 " + _example
        + "——凭证由平台核验"
        f"（**必须属于本任务要求的凭证类型**{_multi}、"
        "未过期、成功）：\n" + _hint_lines
        + "\n⚠ **不要照搬上面的 kind 名**：它是按本任务的 policy 渲染的；"
        "换成别的 kind（例如给纯 UI 任务挂 test_run）会被 attestation 门拒。"
        "确不适用的条目先由 "
        "coordinator `waive_attestation` 落平台 waiver 行，再写 "
        '{"id": …, "not_applicable_reason": "<理由>"}。'
        "**照抄条目原文、换措辞、或裸写 `N/A: <理由>` 都不算覆盖**。"
        "acceptance_criteria 为空的任务不受此门影响。"
    )
