"""Delivery contract — 普通代码任务的轻量 slice contract(收敛自交付契约规格)。

背景(s3-clone_04 复盘 + 多 Agent 调研 2026-08-25):Coordinator → Executor 的
交接物是自由文本 description,接口/文件预期全靠 Executor 临场理解 → 级联幻觉
与返工源头。收敛方案:复用既有 ``contract_json``(slice contract)机制,而不是
再造一套纯文本模板 + description 解析。

复用链路(全部现成):
- schema/parse/validate 与 slice_status 状态机 → ``services/task_contract.py``
- submit 时 machine pre-run → ``run_machine_acceptance``(manual_review 一律
  deferred,不阻塞普通任务流转)
- 测试凭证机器验证 → ``attestation_service.verify_ids(kinds=["test_run"])``
- ready-gate 只被 ``inputs[]`` / ``depends_on`` 触发 → 默认契约无 inputs,
  普通 dispatch 任务零侵入

回执流向:executor 把「实现摘要 / 测试证据」填进
``evidence.delivery_contract = {summary, test}``,submit preflight 校验回执
完整性 + 测试凭证 id 机器验证。

#14(修前双向可绕):``test`` 原按**自由文本前缀**判「是否有测试证据」——
写 ``N/A—任意理由`` 即放行(理由非空即可)。现改为**显式字段 + 平台状态**:

- ``test: "test_run:<凭证 id>"`` —— 机器核验(调用方 ``verify_ids``);
- 确无测试证据 ⇒ 走**平台 waiver 行**(coordinator ``waive_attestation``,
  落 ``tool_attestations``、可审计、有终身上限),并显式声明
  ``evidence_kind="not_applicable"`` + ``not_applicable_reason`` 留痕。

⚠ ``not_applicable`` 的权威是**平台 waiver 行**,不是自述字段:调用方在进
本检查前已用 ``has_valid_waiver`` 短路(``tools/tasks/submit.py``),因此走到
:func:`delivery_contract_missing` 的 ``not_applicable`` 一律判缺——裸写
``N/A—随便什么`` 不再放行。
"""

from __future__ import annotations

import time
from typing import Any

import structlog

log = structlog.get_logger(__name__)

DELIVERY_CONTRACT_TYPE = "delivery_contract"

# 回执必填字段:executor 提交前必须回填(空白/占位视为缺失)。
REQUIRED_REPLY_FIELDS = ("summary", "test")

# 占位类前缀:视为「还没填」(对齐规格"占位符不留")。
_PLACEHOLDER_LEADS = ("<", "[tbd]", "[todo]", "pending", "(待填)", "占位")

# 测试证据的唯一合法形态:``test_run:<attestation_id>`` —— 机器验证(推荐)。
_TEST_EVIDENCE_PREFIX = "test_run:"

# #14:测试证据的**显式字段**(不再用自由文本前缀判"有没有测试证据")。
#   evidence_kind = "test_run"       —— 配 test_run:<凭证 id>(机器核验);
#   evidence_kind = "not_applicable" —— 配 not_applicable_reason,且**必须**
#                                      有本任务的平台 waiver 行(调用方短路);
#   evidence_kind = "not_applicable" 且已进本检查 ⇒ 本任务**无**有效 waiver
#   ⇒ 判缺(fail-closed)。
EVIDENCE_KIND_FIELD = "evidence_kind"
EVIDENCE_KIND_TEST_RUN = "test_run"
EVIDENCE_KIND_NOT_APPLICABLE = "not_applicable"
NOT_APPLICABLE_REASON_FIELD = "not_applicable_reason"

# 拒绝文案里的字段名(调用方把字段名原样拼进回执,故把处方写进字段名)。
_TEST_REQUIRED_HINT = (
    "test(需 'test_run:<凭证id>' 由平台机器核验;确无测试证据须由上级 "
    "`waive_attestation` 落平台 waiver 行后显式声明 evidence_kind="
    "'not_applicable' + not_applicable_reason —— 裸写 N/A—原因 不再放行)"
)
_NOT_APPLICABLE_HINT = (
    "test(evidence_kind='not_applicable' 只由**平台 waiver 行**批准,本任务"
    "当前无有效 waiver:先 coordinator `waive_attestation`,并填非空 "
    "not_applicable_reason)"
)


def build_default_contract(task_id: str) -> dict[str, Any]:
    """为普通代码任务生成轻量 delivery 契约。

    - 无 ``inputs`` / ``deliverables`` → 不触发 slice ready-gate,single 任务
      的 start/流转行为与原状一致。
    - 两个 manual_review 验收子句只是「结构化声明验收关注点」——机器
      acceptance 对 manual_review 一律 deferred,真正落点在于 preflight 对
      ``evidence.delivery_contract`` 回执的检查(见 :func:`delivery_contract_missing`
      与 submit.py 的接线)。
    - ``id`` 用 ``dc-<task 前 8 位>``，每个 task 唯一稳定。
    """
    short = str(task_id or "")[:8] or "dc"
    return {
        "id": f"dc-{short}",
        "slice_type": DELIVERY_CONTRACT_TYPE,
        "slice_status": "ready",
        "inputs": [],
        "deliverables": [],
        "acceptance": [
            {
                "id": "dc-summary",
                "type": "manual_review",
                "note": (
                    "实现摘要：提交前回填到 evidence.delivery_contract.summary——"
                    "实际改了什么、与预期的偏差（不可留占位符）"
                ),
            },
            {
                "id": "dc-test",
                "type": "manual_review",
                "note": (
                    "测试证据：提交前回填到 evidence.delivery_contract.test——"
                    "写 test_run:<凭证 id>（平台机器验证）；确无测试证据时，先由"
                    "上级 coordinator waive_attestation 落平台 waiver 行，再写 "
                    "evidence_kind='not_applicable' + not_applicable_reason="
                    "（裸写 N/A—原因 不再放行）"
                ),
            },
        ],
    }


def parse_delivery_contract(task: dict[str, Any] | None) -> dict[str, Any] | None:
    """解析任务行的 contract_json,仅返回 delivery_contract 类型契约。

    其它 slice 契约(协调者自建的)返回 None —— preflight 不干预。
    """
    if not task:
        return None
    try:
        from hiveweave.services.task_contract import parse_contract

        c = parse_contract(task.get("contract_json"))
    except Exception:
        return None
    if not c:
        return None
    if str(c.get("slice_type") or "").strip() != DELIVERY_CONTRACT_TYPE:
        return None
    return c


def _is_placeholder(value: str) -> bool:
    v = value.strip()
    if not v:
        return True
    low = v.lower()
    return any(low.startswith(p) for p in _PLACEHOLDER_LEADS)


def delivery_contract_missing(evidence: dict[str, Any]) -> list[str]:
    """回执缺失/占位的字段名列表(空 list = 通过)。

    ``evidence`` 为 submit 的 evidence dict,期望含 ``delivery_contract``
    子对象 ``{summary, test[, evidence_kind, not_applicable_reason]}``。
    ⚠ ``evidence_kind`` 与 ``test`` **同级**(都在 ``delivery_contract`` 里),
    不是 ``test`` 的子对象 —— 调用方把 ``test`` 当字符串读并走
    ``test_run:<id>`` 机器核验,dict 形态的 ``test`` 会让该分支失效(故本门
    对嵌套形态 fail-closed 判缺)。

    #14 判据(不再是自由文本):
    - ``summary``:非占位;
    - ``test``:必须是 ``test_run:<凭证 id>``(由调用方机器核验) —— 其余一切
      自由文本(含 ``N/A—任意理由``)判缺;
    - ``evidence_kind="not_applicable"``:权威是**平台 waiver 行**,调用方已
      在进本检查前用 ``has_valid_waiver`` 短路 ⇒ 走到这里即代表本任务无有效
      waiver,故一律判缺(处方写进返回的字段名,调用方原样拼进回执)。
    """
    rc = evidence.get("delivery_contract")
    if not isinstance(rc, dict):
        return list(REQUIRED_REPLY_FIELDS)
    missing: list[str] = []
    sval = rc.get("summary")
    if not (isinstance(sval, str) and not _is_placeholder(sval)):
        missing.append("summary")
    tval = rc.get("test")
    tstr = tval.strip() if isinstance(tval, str) else ""
    kind = str(rc.get(EVIDENCE_KIND_FIELD) or "").strip().lower()
    if parse_test_evidence_attestation_id(tstr):
        pass  # test_run:<id> —— 凭证由调用方 verify_ids 机器核验
    elif kind == EVIDENCE_KIND_NOT_APPLICABLE:
        reason = str(rc.get(NOT_APPLICABLE_REASON_FIELD) or "").strip()
        missing.append(
            _NOT_APPLICABLE_HINT if len(reason) >= 2 else _TEST_REQUIRED_HINT
        )
    else:
        missing.append(_TEST_REQUIRED_HINT)
    return missing


def test_evidence_is_na(value: str) -> bool:
    """``value`` 是否为旧式 ``N/A`` 自由文本声明。

    ⚠ **不再构成放行判据**(#14):自由文本前缀曾是唯一开关,写
    ``N/A—任意理由`` 即过。现保留本函数仅供**观测/透出**(调用方 diagnostics
    与既有测试),门禁一律以 :func:`delivery_contract_missing` 为准。
    """
    low = value.strip().lower()
    return low.startswith("n/a") or low.startswith("na —") or low.startswith("na-")


def test_evidence_reason(value: str) -> str:
    """提取旧式 ``N/A`` 声明的原因部分(分隔符之后)。

    裸 ``N/A``(无分隔符/分隔符后为空或仍是 n/a)视为无原因 → 空串。
    合法例:``N/A — 仓库无测试基建``、``n/a- 只有手工冒烟``。

    ⚠ 与 :func:`test_evidence_is_na` 同:#14 后**不再参与放行判定**,仅供
    诊断/透出;放行要 ``test_run:<凭证 id>`` 或平台 waiver 行。
    """
    v = str(value or "").strip()
    for sep in ("—", "-", ":", ","):
        if sep in v:
            after = str(v).split(sep, 1)[1].strip()
            if after and after.lower() != "n/a":
                return after
    return ""


def parse_test_evidence_attestation_id(value: str) -> str | None:
    """从测试证据提取待机器验证的 test_run 凭证 id。

    唯一合法形态:``test_run:<id>``。裸 id / N/A / 其它格式返回 None
    (调用方按"无法验证"提示,不猜格式)。注:即便裸 id 被接受也会被
    submit 侧 ``verify_ids(expected_kinds=['test_run'], task_id=…)`` 强约束,
    但收紧到带前缀可让提示文案与实际解析一致。
    """
    v = str(value or "").strip()
    if not v or test_evidence_is_na(v):
        return None
    if not v.lower().startswith(_TEST_EVIDENCE_PREFIX):
        return None
    v = v[len(_TEST_EVIDENCE_PREFIX):].strip()
    return v or None


async def has_successful_test_run(
    project_id: str, task_id: str, *, task: dict[str, Any] | None = None
) -> bool:
    """该任务是否存在任一成功(exit_code=0)的 test_run 凭证(任务级 pooling)。

    供回执一致性检查(R1)使用:executor 在测试证据写 ``N/A`` 声明"跑不了
    测试"时,若库里有该任务的成功 test_run 凭证,声明与机器事实矛盾——
    应引导其引用凭证而非否认存在(S3 复盘"软信号未成闸"的护栏之一)。

    T2.4: 与 ``verify_ids`` 口径对齐 —— 任务 policy 的 required kinds 不含
    ``test_run`` 时（如 ``docs_only`` / ``ui_browser_e2e`` 只认文档/浏览
    凭证），库里有成功 test_run 也**不构成矛盾**（被 policy 排除的 kind
    不得参与矛盾判定；此前两门禁对同一凭证给相反评价：attestation 门不认
    它、这里又拿它打回 N/A 声明）。policy 未要求 test_run 的情形
    （soft / 未知 policy → ``_unknown_policy``，与 ``verify_ids`` 的
    fail-close 同向）同样不构成矛盾；拿不到任务行时跳过 policy 预检、
    直接看凭证库（fail-open 不误伤）。

    fail-open:任何查询失败返回 False(视为无凭证,不误伤正常提交)。
    """
    try:
        from hiveweave.db import meta as meta_db
        from hiveweave.db.project import ensure_project_db
        from hiveweave.services.attestation import (
            canonical_task_id,
            ledger_policy_id,
            required_attestation_kinds,
        )

        # T2.4 policy 口径预检：task 行由调用方透传（submit 预检本来就有），
        # 兜底自查一次；拿不到 → 旧行为。
        if not isinstance(task, dict):
            try:
                from hiveweave.services.task import TaskService

                task = await TaskService().get_task(project_id, task_id)
            except Exception as te:
                log.debug(
                    "delivery_contract_task_lookup_failed", error=str(te)
                )
                task = None
        if isinstance(task, dict):
            try:
                policy_id = ledger_policy_id(task)
                kinds = required_attestation_kinds(policy_id)
                if kinds is not None and "test_run" not in kinds:
                    log.info(
                        "delivery_contract_test_run_policy_excluded",
                        project_id=project_id, task_id=str(task_id or ""),
                        policy_id=policy_id,
                    )
                    return False
            except Exception as pe:
                log.debug(
                    "delivery_contract_policy_lookup_failed", error=str(pe)
                )

        tid = await canonical_task_id(project_id, task_id) or str(task_id or "")
        workspace_path = await meta_db.get_project_workspace(project_id)
        if not workspace_path:
            return False
        conn = await ensure_project_db(str(workspace_path))
        now = int(time.time() * 1000)
        cur = await conn.execute(
            "SELECT 1 FROM tool_attestations WHERE project_id = ? "
            "AND task_id = ? AND kind = 'test_run' AND exit_code = 0 "
            "AND expires_at > ? LIMIT 1",
            [project_id, tid, now],
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            log.info(
                "delivery_contract_no_successful_test_run",
                project_id=project_id, task_id=tid,
            )
        return row is not None
    except Exception as e:
        log.warning("delivery_contract_test_run_lookup_failed", error=str(e))
        return False