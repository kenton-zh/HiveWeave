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

TEST_DSH_70 P0-1/P1-2（2026-09-25 定案）四点收紧：

- **三态回执**（P0-1a）：拒收回执区分「未声明（没有任何 ids）/ 形状错（声明
  存在但解析失败，回显收到的原始形态摘要）/ 未核验（声明合法但未通过核验）」
  ——71 轮实测 35/35 条声明全是 ``{"item":[…]}`` 形状错、全部被静默丢弃后以
  同一文案拒收，agent 无从自我纠正。见 :class:`CoverageAssessment` +
  :func:`format_acceptance_coverage_verdict`。
- **ids_raw 单键解包**（P0-1b）：dict 声明缺 ``attestation_ids`` 时，若**恰好
  一个键**且值是 str/list/tuple 则解包采用（正是 ``{"item":[…]}`` 的修复）；
  多键 dict **禁止** values() 展平（歧义，按形状错拒收 + 说明）；一切解析失败
  落 ``acceptance_coverage_shape_error`` 日志 + 进回执，不许静默 None。
- **凭证↔条目绑定诊断**（P0-1c）：声明未通过核验时，回执逐条点名「哪条凭证
  对不上哪个条目、为何」（不存在 / kind 不符 / 未绑本任务 / 核验失败）。
- **子门消费 waiver 对齐主门**（P1-2）：只要存在未覆盖条目就查平台 waiver 行
  （与主 attestation 门的 ``has_valid_waiver`` 短路同口径，不再以「agent 声明了
  not_applicable_reason」为查 waiver 的前置）；但**不对形状错生效** —— 形状错
  是 agent 自己的输入病，豁免不该掩盖。同步形态
  :func:`uncovered_acceptance_items` 保持旧语义（waiver 只盖「非空理由」声明）。

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


@dataclass(frozen=True)
class CoverageShapeError:
    """一条无法解析的覆盖声明（P0-1a：形状错必须点名 + 回显形态，不许静默丢）。"""

    item_id: str
    detail: str

    def line(self) -> str:
        who = f"条目{self.item_id}" if self.item_id else "（未指明条目）"
        return f"{who} ← {self.detail}"


def _shape_summary(value: Any, limit: int = 160) -> str:
    """原始声明的**形态摘要**（回执回显用；紧凑 JSON，截断封顶）。"""
    try:
        text = json.dumps(value, ensure_ascii=False, default=repr)
    except Exception:
        text = repr(value)
    text = _WS_RE.sub(" ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


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


def _unwrap_single_key_ids(
    value: dict[str, Any], *, extra_known: tuple[str, ...] = ()
) -> list[str] | None:
    """P0-1b：``{"item": ["<id>", …]}`` 单键声明解包。

    dict 入参缺 ``attestation_ids`` 时，若**恰好一个未知键**且其值是
    str/list/tuple，则解包采用 —— 这正是 TEST_DSH_70 实测 35/35 条声明全中的
    ``{"item":[…]}`` 形状。多键 dict **禁止** values() 展平（歧义）⇒ 返回
    ``None`` 交由调用方按形状错拒收并说明。
    """
    known = (ATTESTATION_IDS_FIELD, NOT_APPLICABLE_REASON_FIELD, *extra_known)
    unknown = [k for k in value if k not in known]
    if len(unknown) != 1:
        return None
    inner = value[unknown[0]]
    # 只解包 str/list/tuple（与顶层裸串 id 归一口径一致）；int/dict 等
    # 一律不解包 —— 保持修前 fail-closed（`{"foo": 1}` 仍是坏形状）。
    if not isinstance(inner, (str, list, tuple)):
        return None
    raw_ids = inner if isinstance(inner, (list, tuple)) else [inner]
    return [str(x).strip() for x in raw_ids if str(x or "").strip()]


def _claim_ex(
    value: Any, *, extra_known: tuple[str, ...] = ()
) -> tuple[dict[str, Any] | None, str | None]:
    """声明值 → ``(claim, 形状错详情)``。形状错时 claim=None 且 detail 非空。

    修前（P0-1）：dict 声明的 ``ids_raw`` 只认 str/list/tuple，其余一律
    ``return None`` **无日志** —— ``{"item":[…]}`` 全被静默丢弃。
    """
    if isinstance(value, str):
        v = value.strip()
        if v:
            return {ATTESTATION_IDS_FIELD: [v]}, None
        return None, "空字符串声明（没有任何凭证 id）"
    if isinstance(value, (list, tuple)):
        ids = [str(x).strip() for x in value if str(x or "").strip()]
        if ids:
            return {ATTESTATION_IDS_FIELD: ids}, None
        return None, "空列表声明（没有任何凭证 id）"
    if isinstance(value, dict):
        reason = str(value.get(NOT_APPLICABLE_REASON_FIELD) or "").strip()
        ids_raw = value.get(ATTESTATION_IDS_FIELD)
        if ids_raw is None:
            if reason:
                return (
                    {ATTESTATION_IDS_FIELD: [], NOT_APPLICABLE_REASON_FIELD: reason},
                    None,
                )
            unwrapped = _unwrap_single_key_ids(value, extra_known=extra_known)
            if unwrapped is not None:
                if unwrapped:
                    return (
                        {
                            ATTESTATION_IDS_FIELD: unwrapped,
                            NOT_APPLICABLE_REASON_FIELD: "",
                        },
                        None,
                    )
                return None, "单键声明解包后没有任何凭证 id"
            keys = sorted(str(k) for k in value)
            if len(keys) > 1:
                return None, (
                    "dict 声明缺 `attestation_ids`/`not_applicable_reason`，且"
                    f"有 {len(keys)} 个未知键（多键歧义，禁 values() 展平）："
                    f"收到键 {keys}"
                )
            return None, (
                "dict 声明缺 `attestation_ids`/`not_applicable_reason`："
                f"收到 {_shape_summary(value)}"
            )
        if isinstance(ids_raw, str):
            ids = [ids_raw.strip()] if ids_raw.strip() else []
        elif isinstance(ids_raw, (list, tuple)):
            ids = [str(x).strip() for x in ids_raw if str(x or "").strip()]
        elif isinstance(ids_raw, dict):
            unwrapped = _unwrap_single_key_ids(ids_raw)
            if unwrapped is None:
                return None, (
                    "attestation_ids 是 dict 且无法单键解包（多键歧义，禁 "
                    f"values() 展平）：收到 {_shape_summary(ids_raw)}"
                )
            ids = unwrapped
        else:
            return None, (
                f"attestation_ids 类型非法（{type(ids_raw).__name__}）："
                f"{_shape_summary(ids_raw)}"
            )
        if not ids and not reason:
            return None, "声明既没有可用凭证 id 也没有 not_applicable_reason"
        return (
            {ATTESTATION_IDS_FIELD: ids, NOT_APPLICABLE_REASON_FIELD: reason},
            None,
        )
    return None, (
        f"声明类型非法（{type(value).__name__}，期望 str/list/dict）："
        f"{_shape_summary(value)}"
    )


def parse_acceptance_coverage_ex(
    evidence: dict[str, Any] | None,
) -> tuple[dict[str, dict[str, Any]], list[CoverageShapeError]]:
    """:func:`parse_acceptance_coverage` 的**带形状错回执**形态（P0-1a/b）。

    返回 ``(claims, shape_errors)``。形状错的声明不进 claims（fail-closed，
    与修前一致），但**不再静默**：逐条落 ``acceptance_coverage_shape_error``
    warning 日志并以 :class:`CoverageShapeError` 返回给调用方（回执三态分解
    的「形状错」态就吃这里）。
    """
    if not isinstance(evidence, dict):
        return {}, []
    raw = evidence.get(COVERAGE_EVIDENCE_KEY)
    if raw is None:
        return {}, []
    errors: list[CoverageShapeError] = []

    def _bad(item_id: str, detail: str) -> None:
        errors.append(CoverageShapeError(item_id=item_id, detail=detail))
        # P0-1：解析失败必须有日志，不许静默 None。
        log.warning(
            "acceptance_coverage_shape_error",
            item_id=item_id or None,
            detail=detail,
        )

    out: dict[str, dict[str, Any]] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            item_id = str(k or "").strip()
            claim, err = _claim_ex(v)
            if err is not None:
                _bad(item_id, err)
                continue
            if item_id and claim:
                out[item_id] = claim
            else:
                _bad(item_id, "条目 id 为空")
    elif isinstance(raw, (list, tuple)):
        for idx, entry in enumerate(raw, start=1):
            if not isinstance(entry, dict):
                _bad(
                    "",
                    f"list 形态第 {idx} 项不是对象：{_shape_summary(entry)}",
                )
                continue
            item_id = ""
            for k in _ID_KEYS:
                v = str(entry.get(k) or "").strip()
                if v:
                    item_id = v
                    break
            claim, err = _claim_ex(entry, extra_known=_ID_KEYS)
            if err is not None:
                _bad(item_id, err)
                continue
            if item_id and claim:
                out[item_id] = claim
            else:
                _bad(item_id, "list 形态声明缺条目 id（id/key/ref）")
    else:
        _bad(
            "",
            f"acceptance_coverage 顶层类型非法（{type(raw).__name__}，"
            f"期望 dict/list）：{_shape_summary(raw)}",
        )
    return out, errors


def parse_acceptance_coverage(
    evidence: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """``evidence.acceptance_coverage`` → ``{item_id: claim}``。

    claim = ``{"attestation_ids": [str…], "not_applicable_reason": str}``。
    支持 dict 形态（``{"1": {...}}`` 或 ``{"1": ["<id>"]}``）与 list 形态
    （``[{"id": "1", "attestation_ids": [...]}]``）。形状不对的声明**不进
    结果**（未覆盖，fail-closed）——但不再静默：见
    :func:`parse_acceptance_coverage_ex`（形状错清单 + 日志）。无声明 → 空 dict。
    """
    claims, _errors = parse_acceptance_coverage_ex(evidence)
    return claims


def _label(item: AcceptanceItem) -> str:
    return f"条目{item.id}: {item.text}"


async def _attestation_binding_notes(
    project_id: str,
    attestation_ids: list[str],
    *,
    task_id: str | None,
    expected_agent_id: str | None,
    kinds: tuple[str, ...],
) -> list[str]:
    """P0-1c：逐条凭证给出「对不上哪个条目/为何对不上」的可校验诊断。

    返回空 list = **至少一条**凭证对本条目核验通过（覆盖成立）；非空 = 逐条
    失败原因（回执原样点名，agent 才能对症下药 —— 修前这里返回裸 bool，
    失败原因全被 ``continue`` 吞掉）。

    核验口径（与修前逐维一致，只加**诊断输出**，不放宽任何维度）：

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
    if not attestation_ids:
        return ["声明没有挂任何凭证 id（attestation_ids 为空）"]
    if not task_id:
        return ["声明缺任务上下文（task_id 为空），无法核验"]
    from hiveweave.services.attestation import (
        attestation_service,
        canonical_task_id,
    )

    want = await canonical_task_id(project_id, task_id)
    if not want:
        return [f"任务 {task_id} 无法 canonical 化，凭证核验不可进行"]
    allowed_kinds = frozenset(kinds)
    notes: list[str] = []
    for aid in attestation_ids:
        row = await attestation_service.get(project_id, aid)
        if not row:
            notes.append(f"凭证 {aid}：不存在（库中无此 id）")
            continue
        kind = str(row.get("kind") or "")
        if kind not in allowed_kinds:
            notes.append(
                f"凭证 {aid}（kind={kind or '?'}）：不在本任务认的凭证类型 "
                f"{sorted(allowed_kinds)} 内"
            )
            continue
        ok, _err = await attestation_service.verify_ids(
            project_id,
            [aid],
            task_id=task_id,
            expected_agent_id=expected_agent_id,
        )
        if not ok:
            notes.append(f"凭证 {aid}（{kind}）：核验失败 —— {_err}")
            continue
        row_task = str(row.get("task_id") or "")
        if not row_task:
            notes.append(f"凭证 {aid}（{kind}）：未绑定任何任务")
            continue
        if (await canonical_task_id(project_id, row_task)) != want:
            notes.append(
                f"凭证 {aid}（{kind}）：绑定的是别的任务"
                f"（task={row_task[:12]}…），与本条目所在任务不符"
            )
            continue
        return []  # 任一条通过 ⇒ 本条目覆盖成立
    return notes


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
      —— 平台自己定义的"执行证据"集合），并 **fail-loud 留日志**（P2-7，
      TEST_DSH_70 批3：回落日志统一升 warning，且带上 policy_id 与**实际回落
      集合**，让"回落与 F1 打架"的形状可被事后统计）。

    ⚠ 为什么**不从回落集合里剔 kind**（P2-7 的另一半修法，本仓裁定不采）：
    回落只在「policy 没声明执行类 kind」时发生（soft / 未知 / 声明的全是
    code_audit 这类非执行类）——此时没有"本 policy 声明的执行 kind"可对齐；
    往外剔（如剔 test_run）会直接改变门的接受面，且 TEST_DSH_70 本轮该路径
    **未触发**（零实战样本），剔除的判据无从校准；全剔更会让覆盖门无解。
    故只做 warning + 记账，不改行为。

    任何异常 ⇒ 回落 :data:`DEFAULT_COVERAGE_KINDS` + warning（不静默）。
    """
    try:
        from hiveweave.services.attestation import (
            WAIVER_EVIDENCE_KINDS,
            ledger_policy_id,
            required_attestation_kinds,
        )
        from hiveweave.services.telemetry import telemetry

        execution_kinds = tuple(sorted(WAIVER_EVIDENCE_KINDS))
        policy_id = ledger_policy_id(task or {})
        needed = required_attestation_kinds(policy_id)
        if needed:
            usable = tuple(
                sorted(k for k in needed if k in WAIVER_EVIDENCE_KINDS)
            )
            if usable:
                return usable
            telemetry.bump("acceptance_coverage_nonexecution_fallback")
            log.warning(
                "acceptance_coverage_policy_kinds_not_execution_evidence",
                policy_id=policy_id,
                kinds=sorted(needed),
                fallback_kinds=list(execution_kinds),
            )
        else:
            telemetry.bump("acceptance_coverage_soft_fallback")
            log.warning(
                "acceptance_coverage_policy_soft_all_execution_kinds",
                policy_id=policy_id,
                kinds=list(execution_kinds),
                fallback_kinds=list(execution_kinds),
            )
        return execution_kinds
    except Exception as e:  # noqa: BLE001 — 取不到就回落且留痕（不静默）
        log.warning("acceptance_coverage_kinds_lookup_failed", error=str(e))
        return DEFAULT_COVERAGE_KINDS


@dataclass(frozen=True)
class CoverageAssessment:
    """覆盖门的**三态结构化结论**（P0-1a）——回执与平铺缺口行的共同来源。

    - ``undeclared``：没有任何声明的条目标签（未声明态）；
    - ``unverified``：声明合法但未通过核验的条目标签（未核验态）；
    - ``unverified_ids``：与 ``unverified`` 同序平行的**条目 id** ——
      回执渲染凭证诊断必须按 id 结构对取，不能从标签字符串反解
      （条目 id 自身含 ``:`` 时反解会错位，诊断静默丢失）；
    - ``shape_errors``：声明存在但无法解析（形状错态，含原始形态摘要）；
      **waiver 不掩盖此态**（形状错是 agent 自己的输入病）；
    - ``unknown_ids``：声明指向不存在条目的无效声明；
    - ``credential_notes``：未核验条目 → 逐条凭证失败诊断（P0-1c 绑定）；
    - ``waived``：本任务是否存在有效平台 waiver 行（P1-2 子门对齐主门消费）；
    - ``gap_lines``：平铺缺口行（条目原序，形状错行内联在该条目处）——
      :func:`uncovered_acceptance_items_verified` 的兼容出口。
    """

    undeclared: list[str]
    unverified: list[str]
    unverified_ids: list[str]
    shape_errors: list[CoverageShapeError]
    unknown_ids: list[str]
    credential_notes: dict[str, list[str]]
    waived: bool
    gap_lines: list[str]

    @property
    def has_gaps(self) -> bool:
        return bool(self.gap_lines)


async def assess_acceptance_coverage_verified(
    project_id: str,
    task_id: str | None,
    criteria: Any,
    evidence: dict[str, Any] | None,
    *,
    expected_agent_id: str | None = None,
    kinds: tuple[str, ...] = DEFAULT_COVERAGE_KINDS,
) -> CoverageAssessment:
    """**权威覆盖门的三态评估**（TEST_DSH_70 P0-1a/b/c + P1-2 waiver 对齐）。

    判据见模块 docstring。返回 :class:`CoverageAssessment`；``has_gaps`` 为
    False 即全覆盖（含 waiver 兜住的情形）。
    """
    items = parse_acceptance_plan(criteria)
    if not items:
        return CoverageAssessment([], [], [], [], [], {}, False, [])
    known = {it.id for it in items}
    claims, shape_errors = parse_acceptance_coverage_ex(evidence)
    shape_by_id = {se.item_id: se.detail for se in shape_errors}
    shape_invalid_ids = set(shape_by_id) & known

    covered: set[str] = set()
    credential_notes: dict[str, list[str]] = {}
    for item_id, claim in claims.items():
        if item_id not in known:
            continue
        ids = list(claim.get(ATTESTATION_IDS_FIELD) or [])
        notes = await _attestation_binding_notes(
            project_id,
            ids,
            task_id=task_id,
            expected_agent_id=expected_agent_id,
            kinds=kinds,
        )
        if not notes:
            covered.add(item_id)
        elif ids:
            # 只给挂了 id 的声明出凭证诊断；纯 NA 声明的「未核验」语义由
            # 豁免/waiver 判定解释，凭证诊断对它是噪声。
            credential_notes[item_id] = notes

    undeclared = [
        _label(it)
        for it in items
        if it.id not in claims and it.id not in shape_invalid_ids
    ]
    unverified = [
        _label(it) for it in items if it.id in claims and it.id not in covered
    ]
    unverified_ids = [
        it.id for it in items if it.id in claims and it.id not in covered
    ]

    # P1-2 子门对齐主门消费 waiver：主 attestation 门是 `has_valid_waiver`
    # 直接短路（不要求 agent 先声明什么），子门此前却以「agent 声明了
    # not_applicable_reason」为查 waiver 的前置 —— 71 轮实测 CEO 的 waiver
    # 因此全无效（声明全是形状错，查都没查）。对齐 = 存在未覆盖条目即查
    # waiver 行；命中则未声明/未核验条目全部兜住。**形状错除外**：那是
    # agent 自己的输入病，豁免不该掩盖（修好形状后 waiver 才接手）。
    waived = False
    if undeclared or unverified:
        waived = await _task_has_valid_waiver(project_id, task_id)
        if waived:
            undeclared, unverified, unverified_ids = [], [], []
            covered |= known - shape_invalid_ids

    unknown = sorted(i for i in claims if i not in known)

    gap_lines: list[str] = []
    for it in items:
        if it.id in covered:
            continue
        if it.id in shape_invalid_ids:
            gap_lines.append(
                f"{_label(it)} —— ⚠ 覆盖声明形状错（无法解析）："
                f"{shape_by_id[it.id]}"
            )
        else:
            gap_lines.append(_label(it))
    if unknown:
        gap_lines.append(
            "无效覆盖声明（未知条目 id）：" + ", ".join(unknown)
            + f"；本任务有效 id：{sorted(known, key=lambda x: (len(x), x))}"
        )
    return CoverageAssessment(
        undeclared=undeclared,
        unverified=unverified,
        unverified_ids=unverified_ids,
        shape_errors=shape_errors,
        unknown_ids=unknown,
        credential_notes=credential_notes,
        waived=waived,
        gap_lines=gap_lines,
    )


async def uncovered_acceptance_items_verified(
    project_id: str,
    task_id: str | None,
    criteria: Any,
    evidence: dict[str, Any] | None,
    *,
    expected_agent_id: str | None = None,
    kinds: tuple[str, ...] = DEFAULT_COVERAGE_KINDS,
) -> list[str]:
    """覆盖门兼容出口：返回未覆盖条目行（空 = 全覆盖）。

    ⚠ 门禁调用点应改用 :func:`assess_acceptance_coverage_verified`（三态
    结构化结论，配 :func:`format_acceptance_coverage_verdict` 出「未声明/
    形状错/未核验」分解回执）。本函数保留给既有调用方与形状测试，行为
    与修前逐字兼容（除形状错声明现在会内联点名 + 落日志）。
    """
    assessment = await assess_acceptance_coverage_verified(
        project_id,
        task_id,
        criteria,
        evidence,
        expected_agent_id=expected_agent_id,
        kinds=kinds,
    )
    return list(assessment.gap_lines)


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
    ⚠ 门禁应直接用 :func:`assess_acceptance_coverage_verified`（自含核验 +
    waiver 对齐 + 三态结论），本函数只服务已自行核验过的调用方与纯形状测试。

    ⚠ 豁免语义与权威门**有意不同**（反措辞棘轮，2026-09-19）：本函数里
    waiver 只盖「非空 not_applicable_reason」的合法声明；waiver 全量对齐
    （未声明/未核验条目也兜住、形状错除外）只在权威 async 门生效。
    形状错声明（P0-1）同样不进覆盖，且**内联点名 + 回显形态摘要**。
    """
    items = parse_acceptance_plan(criteria)
    if not items:
        return []
    known = {it.id for it in items}
    claims, shape_errors = parse_acceptance_coverage_ex(evidence)
    shape_by_id = {se.item_id: se.detail for se in shape_errors}
    shape_invalid_ids = set(shape_by_id) & known
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
    gaps: list[str] = []
    for it in items:
        if it.id in covered:
            continue
        if it.id in shape_invalid_ids:
            gaps.append(
                f"{_label(it)} —— ⚠ 覆盖声明形状错（无法解析）："
                f"{shape_by_id[it.id]}"
            )
        else:
            gaps.append(_label(it))
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


def _coverage_kinds_or_default(kinds: tuple[str, ...] | None) -> tuple[str, ...]:
    _ks = tuple(k for k in (kinds or ()) if k)
    return _ks or ("test_run",)


def _prescription_block(_ks: tuple[str, ...]) -> str:
    """三态回执共用的处方块（单一来源；F1：kind 按本任务 policy 渲染）。"""
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
        f"\n处方：submit_task 传 **`acceptanceCoverage` 参数**（不是 verdict "
        "文本、不是文件），逐条按 id 声明覆盖"
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
    )


def format_acceptance_coverage_verdict(
    assessment: CoverageAssessment,
    kinds: tuple[str, ...] | None = None,
) -> str:
    """P0-1a 三态分解回执：未声明 / 形状错 / 未核验 各说各的话。

    修前三态共用一句「未体现对以下 N 条的覆盖」，35/35 形状错声明被静默
    丢弃后 agent 收到的是「你没声明」——无从自我纠正（37 次拒收、110′
    拉锯的根因）。形状错态必须回显**收到的原始形态摘要**；未核验态必须
    逐条点名「哪条凭证对不上哪个条目」（P0-1c 绑定诊断）。
    """
    _ks = _coverage_kinds_or_default(kinds)
    sections: list[str] = []
    if assessment.shape_errors:
        lines = "\n".join(f"- {se.line()}" for se in assessment.shape_errors)
        sections.append(
            f"【形状错】有 {len(assessment.shape_errors)} 条覆盖声明**存在但无法"
            "解析**，平台未采用（不是让你补声明，是让你把下面的形状改对；"
            "注意多键对象歧义、不接受 values 展平）：\n" + lines
            + '\n  标准形状：{"<条目id>": {"attestation_ids": ["<凭证id>"]}}'
            ' 或 {"<条目id>": {"not_applicable_reason": "<理由>"}}'
            '（单键简写 {"<条目id>": ["<凭证id>"]} 也认）。'
        )
    if assessment.undeclared:
        sections.append(
            f"【未声明】以下 {len(assessment.undeclared)} 条**没有任何**覆盖声明"
            "（evidence.acceptance_coverage 缺失或为空）：\n- "
            + "\n- ".join(assessment.undeclared)
        )
    if assessment.unverified:
        # 凭证诊断按 (id, 标签) 结构对取 —— 不从标签字符串反解条目 id
        # （id 自身含 ":" 时 split 反解会错位，诊断静默丢失）。
        _note_for: dict[str, str] = {}
        for _iid, _line in zip(
            assessment.unverified_ids, assessment.unverified, strict=False
        ):
            _notes = assessment.credential_notes.get(_iid) or []
            if _notes:
                _note_for[_line] = "\n".join(
                    f"    · {n}" for n in _notes
                )
        lines = "\n".join(
            f"- {line}" + (f"\n{_note_for[line]}" if line in _note_for else "")
            for line in assessment.unverified
        )
        sections.append(
            f"【未核验】以下 {len(assessment.unverified)} 条声明合法但**未通过"
            "平台核验**（凭证与本条目对不上，逐条原因如下）：\n" + lines
        )
    if assessment.unknown_ids:
        sections.append(
            "【无效声明】声明指向不存在的条目 id："
            + ", ".join(assessment.unknown_ids)
        )
    if assessment.waived:
        sections.append(
            "（本任务已有有效平台 waiver 行：除形状错外，其余条目已由 waiver "
            "兜住——先把上面的形状错改对即可通过。）"
        )
    head = (
        "SUBMIT REJECTED (verify acceptance checklist): 任务带 "
        "acceptance_criteria，verdict evidence 的覆盖声明未通过验收门，"
        f"共 {sum(1 for s in sections)} 类问题：\n"
    )
    return head + "\n".join(sections) + _prescription_block(_ks)


def format_acceptance_coverage_error(
    missing: list[str], kinds: tuple[str, ...] | None = None
) -> str:
    """E1 验收清单缺覆盖的拒绝文案（点名缺哪几条 + 处方）——**旧接口**。

    ⚠ 门禁调用点应改用 :func:`format_acceptance_coverage_verdict`（吃
    :class:`CoverageAssessment`，出三态分解回执）。本函数保留给既有调用方
    与文案测试：入参仍是平铺缺口行，按「未声明」态渲染。

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

    TEST_DSH_70 P0-3：修前文案末行的「acceptance_criteria 为空的任务不受
    此门影响」是把**降档配方印在拒收回执上**（第 6 张空清单单由此一次通过
    终验）——已删除（文本判据铁律：不把绕过配方教给 agent）。
    """
    _ks = _coverage_kinds_or_default(kinds)
    return (
        "SUBMIT REJECTED (verify acceptance checklist): 任务带 "
        "acceptance_criteria，verdict evidence 未体现对以下 "
        f"{len(missing)} 条的覆盖：\n- "
        + "\n- ".join(missing)
        + _prescription_block(_ks)
    )
