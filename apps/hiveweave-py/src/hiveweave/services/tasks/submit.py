"""Submit task + evidence workspace resolution."""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import TYPE_CHECKING, Any

import structlog

from .db import _conn, _ensure_schema, _execute, _execute_tx, _query
from .acceptance import (
    acceptance_coverage_kinds,
    assess_acceptance_coverage_verified,
    format_acceptance_coverage_verdict,
)
from .verify import normalize_verdict, verdict_evidence_gaps

log = structlog.get_logger(__name__)

# A4-2（D76 实测，批 2）：submit in-flight 防抖 —— 同任务并发的第二次
# submit 在这把 per-task 锁上等待，拿到锁后重读状态走幂等出口，不再
# 双转移 / 双写 evidence。进程内锁即可：读改写在单事件循环内由此串行化。
_SUBMIT_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}
_SUBMIT_LOCKS_MAX = 512


def _task_submit_lock(project_id: str, task_id: str) -> asyncio.Lock:
    key = (project_id, task_id)
    lock = _SUBMIT_LOCKS.get(key)
    if lock is None:
        if len(_SUBMIT_LOCKS) >= _SUBMIT_LOCKS_MAX:
            # 只清未被持有的锁（持锁中的条目绝不动）；单线程事件循环内
            # 这段无 await，无并发窗口。
            for stale in [
                k for k, v in _SUBMIT_LOCKS.items() if not v.locked()
            ]:
                _SUBMIT_LOCKS.pop(stale, None)
                if len(_SUBMIT_LOCKS) < _SUBMIT_LOCKS_MAX:
                    break
        lock = _SUBMIT_LOCKS.setdefault(key, asyncio.Lock())
    return lock


# ── 批27-1 N1（d90 实录）：submitted 重提「证据感知」指纹 ─────────────
# 根因：A4-1 幂等分支把「证据未变」假设套到了所有 submitted 重提上——它
# 只对并发双发成立；QA 反馈后修码重提时新 commit 被无声丢弃，approve 证据
# 门核旧 files_changed ⇒ 自动推翻 reviewer 批准。修法：幂等出口先比指纹。
# 指纹 = commit + files_changed + attestation id 集（显式门禁字段，不做
# dict 全等——summary/test_output 等非门禁字段不参与防抖判定）。
_EVIDENCE_FP_KEYS = ("commit", "files_changed", "attestation_ids")


def parse_stored_evidence(raw: Any) -> dict:
    """tasks.evidence 列（dict / JSON str / None）→ dict（容错解析）。"""
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _fp_component(value: Any) -> tuple:
    """单指纹字段归一：commit 大小写不敏感；列表去空/去重/排序。"""
    if isinstance(value, list):
        return tuple(sorted({str(x).strip() for x in value if str(x).strip()}))
    text = str(value or "").strip().lower()
    return (text,) if text else ()


def evidence_fingerprint_changed(old_ev: dict, new_ev: Any) -> bool:
    """submitted 重提是否带「明确不同的证据」。

    只对**本次提交入参里已有的指纹字段**判等：入参未带的字段视为未变
    （files_changed 常由工具层 preflight 从 ``git diff main...HEAD`` 自动
    回填——缺席 ≠ 清空，不得误判成「证据变了」而抹掉库内清单）。全部
    可比字段都相同（含两边都无可比字段）⇒ False 走幂等出口；任一不同
    ⇒ True 走证据更新路径。
    """
    if not isinstance(new_ev, dict):
        return False
    for key in _EVIDENCE_FP_KEYS:
        new_val = new_ev.get(key)
        if not new_val:
            continue
        if _fp_component(new_val) != _fp_component(
            (old_ev or {}).get(key)
        ):
            return True
    return False


class SubmitMixin:
    """submit_task / _resolve_evidence_workspace."""

    if TYPE_CHECKING:
        require_task_id: Any
        get_task: Any
        _persist_contract_json: Any
        _transition: Any
        _is_verify_task: Any
        emit_task_event: Any

    async def submit_task(self, project_id: str, task_id: str,
                          evidence: dict) -> dict:
        """Submit a task (running → submitted). Sets evidence (JSON) + submitted_at.

        A4（D76 实测，批 2）幂等 + 防抖，批27-1 N1 证据感知分流：
        - ``submitted → submitted`` 且**证据指纹相同**（commit / files_changed
          / attestation id 集，见 :func:`evidence_fingerprint_changed`）= 幂等
          成功：返回 ``{"idempotent": True, ...}``，不改状态 / 不覆盖既有
          evidence / 不重建义务 / 不重跑门禁（不产生新凭证要求）——不再抛
          ``Illegal transition`` 逼 agent 把任务移回 running 绕路。
        - 指纹**不同**（QA 反馈后修码重提）= 证据更新：状态保持 submitted，
          复用落库尾段写回新 evidence + 重激活 review 义务，并给 reviewer 发
          一次 ``[TASK RESUBMITTED]``；返回 ``{"resubmitted": True, ...}``。
        - 同任务并发提交由 per-task 锁串行化（in-flight 防抖）：后到者
          拿到锁后重读状态，走幂等出口，不二次转移。
        assignee-only 的权限语义在工具层强制；本层按状态判定（API / merge
        auto-submit 等调用方同样受益）。返回
        ``{"idempotent": bool, "task_id": ..., "status": "submitted"}``；
        既有调用方均忽略返回值，dict 为新增信息面。

        BUG-P1b: 保留既有 evidence.merged_by —— VERIFY spawn 时写入的
        合并人标记是 review_task 独立审门排除合并人的唯一依据，submit
        整体覆盖 evidence 会让该门禁失效。

        TEST11 #3: on submit, pin ``reviewer_id`` (default creator_id) so the
        designated reviewer has obligations from the submitted window onward —
        not only after they call start_review.

        Slice P0: if ``contract_json`` present, L0 machine clauses must pass
        against the assignee worktree (or project root) before transition.
        """
        task_id = await self.require_task_id(project_id, task_id)
        async with _task_submit_lock(project_id, task_id):
            result = await self._submit_task_locked(
                project_id, task_id, evidence
            )
        # N1：RESUBMITTED 通知在**锁外**发 —— trigger_coordinator 可能驱动
        # reviewer 一整轮 turn（与工具层首提路径的 post-lock 唤醒同语义），
        # 拿着 per-task 锁做这件事会卡死同任务的并发提交。
        if isinstance(result, dict) and result.get("resubmitted"):
            notify = result.pop("_resubmit_notify", None)
            if notify:
                await self._notify_evidence_resubmitted(
                    project_id, task_id, notify
                )
        return result

    async def _submit_task_locked(self, project_id: str, task_id: str,
                                  evidence: dict) -> dict:
        task = await self.get_task(project_id, task_id)

        # A4-1（D76）+ 批27-1 N1：submitted → submitted 重提，按证据分流。
        # 指纹相同（并发双发/防抖）= 幂等成功，门禁判定整体跳过：首次提交
        # 已验过一次，重复提交不产生新凭证要求。
        # 指纹不同（QA 反馈后修码重提）= 证据更新：状态保持 submitted
        # （证据刷新不是状态迁移，不走 _transition/门禁），复用落库尾段把
        # 新 evidence 写回 + 给 reviewer 发一次 [TASK RESUBMITTED]——否则
        # 新 commit 无声丢失，approve 证据门核旧 files_changed 会自动推翻
        # 已有批准（d90：批准被推翻 + 33min 抖动）。
        if (task or {}).get("status") == "submitted":
            old_ev = parse_stored_evidence((task or {}).get("evidence"))
            if not evidence_fingerprint_changed(old_ev, evidence):
                log.info(
                    "submit_task_idempotent",
                    project_id=project_id,
                    task_id=task_id,
                )
                return {
                    "idempotent": True,
                    "task_id": task_id,
                    "status": "submitted",
                    "submitted_at": task.get("submitted_at"),
                    "note": "already submitted (idempotent)",
                }
            log.info(
                "submit_task_evidence_resubmit",
                project_id=project_id,
                task_id=task_id,
            )
            tail = await self._write_submission_evidence(
                project_id, task_id, evidence
            )
            # 通知信息随返回值带出，由 submit_task 在锁外发送
            # （trigger_coordinator 可能驱动 reviewer 整轮 turn，不可持锁）。
            return {
                "idempotent": True,
                "resubmitted": True,
                "task_id": task_id,
                "status": "submitted",
                "submitted_at": tail.get("submitted_at"),
                "note": (
                    "evidence updated on submitted task (resubmitted); "
                    "reviewer notified"
                ),
                "_resubmit_notify": {
                    "reviewer_id": str(
                        tail.get("reviewer_id")
                        or (task or {}).get("creator_id")
                        or ""
                    ),
                    "old_commit": str((old_ev or {}).get("commit") or ""),
                    "new_commit": str(
                        (evidence or {}).get("commit") or ""
                    ),
                    "title": str((task or {}).get("title") or ""),
                },
            }

        # E1 verdict gate：终验（VERIFY / milestoneVerify）任务 evidence 必须
        # 带强制判定字段，否则硬拒提交（transition 之前拦截）。
        if task and self._is_verify_task(task):
            self._validate_verdict_evidence(evidence)
            # 任务6（门禁智能化包）/#14：VERIFY 任务带非空 acceptance_criteria 时，
            # verdict evidence 必须用 `acceptance_coverage` **逐条按 id 声明覆盖**，
            # 且声明锚在平台可核验的执行凭证上（本任务 + 正确 kind + 未过期 +
            # exit_code=0）——判据是"id 集合包含 + 凭证核验"，与措辞/语言无关；
            # 不适用的条目须先由 coordinator/CEO waive_attestation 落平台 waiver 行。
            # 缺覆盖 → 拒绝并列出缺哪几条 + 处方。清单为空的任务不受影响；
            # check_evidence_verifiable 的 VERIFY 跳过保持不动。
            _cov_kinds = await acceptance_coverage_kinds(task)
            # TEST_DSH_70 P0-1a：三态评估（未声明/形状错/未核验），回执按态
            # 分解 + 逐条凭证绑定诊断；P1-2：waiver 在门内对齐主门消费。
            _coverage = await assess_acceptance_coverage_verified(
                project_id,
                task_id,
                task.get("acceptance_criteria"),
                evidence,
                expected_agent_id=str(task.get("assignee_id") or "") or None,
                kinds=_cov_kinds,
            )
            if _coverage.has_gaps:
                # F1：处方必须按本任务 policy 渲染 kind（否则 agent 照抄
                # `test_run` 示例 → 撞 attestation 门）。
                # 批21 修复1（T37 范式）：查本任务已 attest 的合格凭证 id
                # 实时填进可照抄模板（照抄即过同一张门）；查不到 fail-open。
                _cov_ids: list[str] = []
                try:
                    from ..attestation import attestation_service as _att_svc

                    _cov_ids = await _att_svc.find_recent_for_agent(
                        project_id,
                        agent_id=str(task.get("assignee_id") or "") or "",
                        task_id=task_id,
                        kinds=list(_cov_kinds),
                    )
                except Exception as e:  # noqa: BLE001 — 模板尽力填充
                    log.debug(
                        "submit_coverage_template_lookup_failed", error=str(e)
                    )
                raise ValueError(
                    format_acceptance_coverage_verdict(
                        _coverage, _cov_kinds, credential_ids=_cov_ids
                    )
                )
            # E5 断流收口纪律：降级中提交 verdict=FAIL 属「waiver 型就地
            # 收口」——必须续跑重验或升级 coordinator，不许抢在续跑前
            # 用 FAIL 提交替豁免收口（复盘终验三连打断后 waiver 收口）。
            if (
                isinstance(evidence, dict)
                and normalize_verdict(evidence.get("verdict")) == "FAIL"
                and self._is_degraded_assignee(task)
            ):
                # 45 轮 P1「拒绝无记忆」①②：machine-readable 出路标记 +
                # 同因连拒计数（45 轮降级终验 3 连拒同文案）。
                from hiveweave.services.rejection_memory import (
                    repeat_rejection_notice, rejection_count,
                )

                msg = (
                    "SUBMIT REJECTED (degraded verify): 你所在 turn 刚被断流/"
                    "打断（降级中）且正提交 FAIL 终验——禁止就地收口。可执行"
                    "两步：① 续跑完成这一轮（正常完成一轮后平台自动清除降级"
                    "标志），完成重新验证后再提交；② 或显式升级 coordinator/"
                    "CEO。 RETRY[action=resume_turn_then_resubmit|"
                    "alt=escalate_coordinator]"
                )
                # 计数**必须照旧登记**（annotate 的副作用就是计数）：把它的
                # 返回值丢掉即可 —— 提示改走独立通道，不再污染拒绝文案本身。
                _agent_for_notice = str(task.get("assignee_id") or "") or None
                _repeat_notice = repeat_rejection_notice(
                    "submit_task", msg, agent_id=_agent_for_notice
                )
                if _repeat_notice and _agent_for_notice:
                    from hiveweave.services.health_notice import (
                        KIND_REPEAT_REJECTION,
                        deliver_notice,
                    )

                    # 通道与回执物理分离：模型在自己的下一轮读到它，而
                    # 拒绝文案保持纯粹可取证（批次 4 附项三问 ②）。
                    await deliver_notice(
                        _agent_for_notice,
                        _repeat_notice,
                        kind=KIND_REPEAT_REJECTION,
                        project_id=project_id,
                        wake=False,
                    )
                # 46/11 #8 平台动作：第 2 次连拒时自动把 VERIFY 回队
                # running——QA 获得 fresh turn 重验再提交，不再卡死在
                # 「提交→拒→提交」循环（356min 项目 8 连拒烧 VERIFY 的
                # 实证解法；文案教育已证无效，改为平台代动作）。
                repeat_n = rejection_count(
                    msg, agent_id=str(task.get("assignee_id") or "") or None
                )
                if repeat_n >= 2:
                    try:
                        # submitted→running 是 _TRANSITIONS 合法迁移；
                        # assignee 保持原 QA（duty 不变），fresh turn 解除
                        # 降级后即可重验。
                        await self._transition(
                            project_id, task_id, "running",
                            actor_id="system",
                            reason_code="degraded_verify_auto_requeue",
                        )
                        log.info(
                            "degraded_verify_auto_requeued",
                            project_id=project_id,
                            task_id=task_id,
                            repeat_n=repeat_n,
                        )
                    except Exception as e:
                        log.warning(
                            "degraded_verify_requeue_failed",
                            project_id=project_id,
                            task_id=task_id,
                            error=str(e),
                        )
                raise ValueError(msg)

        # SUBMITTED MACHINE PRE-RUN (slice-driven L0)
        if task and task.get("contract_json"):
            from hiveweave.services.task_contract import (
                ensure_slice_status,
                format_prerun_failure,
                parse_contract,
                run_machine_acceptance,
            )

            contract = parse_contract(task.get("contract_json"))
            if contract:
                ws_root = await self._resolve_evidence_workspace(
                    project_id, task
                )
                prerun = run_machine_acceptance(
                    contract, workspace_root=ws_root
                )
                contract = dict(contract)
                # s3-clone_07 GAP：service_smoke 异步条款——同步文件条款通过后，
                # 启动服务跑交付级冒烟（真协议客户端）。失败 = 拒绝提交。
                # 这是 07 轮 0/22 事故的对应门：内部门禁全绿 ≠ 交付物可用。
                smoke_clause = None
                for _c in contract.get("acceptance") or []:
                    if isinstance(_c, dict) and _c.get("type") == "service_smoke":
                        smoke_clause = _c
                        break
                if smoke_clause and prerun.passed:
                    from hiveweave.services.smoke_gate import (
                        run_service_smoke_clause,
                    )

                    smoke_result, smoke_freeze = await run_service_smoke_clause(
                        smoke_clause,
                        workspace_root=str(ws_root),
                        frozen=contract.get("smoke_freeze"),
                    )
                    prerun.results.append(smoke_result)
                    prerun.passed = prerun.passed and smoke_result.passed
                    if smoke_freeze:
                        contract["smoke_freeze"] = smoke_freeze
                contract["machine_pre_run"] = {
                    **prerun.to_dict(),
                    "at_ms": int(time.time() * 1000),
                    "workspace": str(ws_root),
                }
                if not prerun.passed:
                    await self._persist_contract_json(
                        project_id, task_id, contract
                    )
                    raise ValueError(format_prerun_failure(prerun))
                contract = ensure_slice_status(contract, "submitted")
                await self._persist_contract_json(
                    project_id, task_id, contract
                )
                if isinstance(evidence, dict):
                    evidence = dict(evidence)
                    evidence["machine_pre_run"] = contract["machine_pre_run"]

        await self._transition(project_id, task_id, "submitted",
                               actor_id=(task or {}).get("assignee_id"))
        await self._write_submission_evidence(project_id, task_id, evidence)
        return {"idempotent": False, "task_id": task_id, "status": "submitted"}

    async def _write_submission_evidence(
        self, project_id: str, task_id: str, evidence: dict
    ) -> dict:
        """提交落库尾段（首提 / submitted 态证据更新重提共用）。

        批27-1 N1：从 ``_submit_task_locked`` 尾段原样抽出（行为不变）——
        门禁连拒清零 + merged_by/审计溯源键保护 + reviewer 钉定 +
        evidence/submitted_at 写回 + ``submitted`` 事件 + review 义务激活。
        重提路径不重走 ``_transition``/门禁（状态已是 submitted），但落库与
        义务语义必须与首提一致（reviewer 看到的必须是新证据）。

        Returns ``{"agent_id", "reviewer_id", "submitted_at"}``（重提通知
        与返回值消费 ``reviewer_id`` / ``submitted_at``）。
        """
        # TEST_DSH_70 P1-2：提交成功 ⇒ 门禁连拒计数清零（只有「连续」被拒
        # 才累计；出口护栏见 services/tasks/gate_reject_guard.py）。
        from .gate_reject_guard import reset_gate_rejections

        reset_gate_rejections(project_id, task_id)
        # T18 bug1/bug2：SYSTEM AUDIT 卡判别/reviewer 解析/溯源键保护
        # （懒 import 防环，与本文件其余 services 间引用同风格）。
        from .audit_dispatch import (
            AUDIT_TASK_EVIDENCE_KEYS,
            is_system_audit_card,
            resolve_card_reviewer,
        )

        if isinstance(evidence, dict) and "merged_by" not in evidence:
            rows0 = await _query(
                project_id,
                "SELECT evidence, tags, title FROM tasks WHERE id = ?",
                [task_id],
            )
            if rows0 and rows0[0]["evidence"]:
                try:
                    prev = rows0[0]["evidence"]
                    prev = json.loads(prev) if isinstance(prev, str) else dict(prev)
                except (json.JSONDecodeError, TypeError):
                    prev = {}
                if isinstance(prev, dict) and prev.get("merged_by"):
                    evidence = dict(evidence)
                    evidence["merged_by"] = prev["merged_by"]
                # T18 bug2：SYSTEM AUDIT 卡的 mint 溯源键不得被提交 evidence
                # 整体覆盖 —— approve 收口的凭证回写靠 source_task_id /
                # submitter_id 找回源任务；丢了它回写就落空（S16 门纹丝不动
                # 的成因之一）。新 evidence 的同名键仍以提交者为准。
                if isinstance(prev, dict) and is_system_audit_card(
                    {"tags": rows0[0]["tags"], "title": rows0[0]["title"]}
                ):
                    evidence = dict(evidence)
                    for _pk in AUDIT_TASK_EVIDENCE_KEYS:
                        if _pk in prev and _pk not in evidence:
                            evidence[_pk] = prev[_pk]
        now_ms = int(time.time() * 1000)
        # Pin reviewer at submit: existing column wins; evidence.reviewer_id
        # only fills when column is empty (non-VERIFY). VERIFY always → creator.
        meta_rows = await _query(
            project_id,
            "SELECT assignee_id, creator_id, reviewer_id, tags, title, kind, evidence "
            "FROM tasks WHERE id = ?",
            [task_id],
        )
        agent_id = meta_rows[0]["assignee_id"] if meta_rows else None
        reviewer_id = None
        if meta_rows:
            creator_id = meta_rows[0]["creator_id"]
            existing_reviewer = meta_rows[0]["reviewer_id"]
            # ⚠ 这里的 draft 是「中间视图」：字段缺一个，下游判定就静默走错分支。
            # `kind` 必须带上 —— #11 后 `_is_verify_task` 只读 kind；漏掉它会让
            # VERIFY 的「reviewer 钉 creator」规则**永不生效**（reviewer 可自审）。
            draft = {
                "tags": meta_rows[0]["tags"],
                "title": meta_rows[0]["title"],
                "kind": meta_rows[0]["kind"],
            }
            if self._is_verify_task(draft):
                reviewer_id = creator_id
            elif is_system_audit_card(draft):
                # T18 bug1：SYSTEM AUDIT 卡的 reviewer 不得用旧默认
                # creator_id —— 卡的 creator 恰是被审 submitter（executor 无
                # review_task ⇒ 卡死 submitted）。改走 review-权角色链：
                # 源任务 reviewer/creator → qa_lead → 卡 assignee 的 parent；
                # 全败才回退 creator（旧行为）并留日志。
                _ev: dict = {}
                try:
                    _raw_ev = meta_rows[0]["evidence"]
                    _ev = json.loads(_raw_ev) if isinstance(_raw_ev, str) else (_raw_ev or {})
                except (json.JSONDecodeError, TypeError):
                    _ev = {}
                try:
                    _card_rev, _card_chain = await resolve_card_reviewer(
                        project_id,
                        card_assignee_id=str(agent_id or ""),
                        submitter_id=str((_ev or {}).get("submitter_id") or creator_id or ""),
                        source_task_id=str((_ev or {}).get("source_task_id") or "") or None,
                    )
                except Exception as e:  # noqa: BLE001 — 解析失败退旧默认
                    log.warning(
                        "audit_card_reviewer_resolve_failed",
                        task_id=task_id,
                        error=str(e),
                    )
                    _card_rev = None
                if _card_rev is None:
                    log.warning(
                        "audit_card_reviewer_fallback_creator",
                        task_id=task_id,
                        creator_id=str(creator_id or "")[:8],
                    )
                reviewer_id = str((_card_rev or {}).get("id") or "") or creator_id
            elif existing_reviewer:
                reviewer_id = existing_reviewer
            elif isinstance(evidence, dict) and evidence.get("reviewer_id"):
                reviewer_id = str(evidence["reviewer_id"])
            else:
                reviewer_id = creator_id
        if reviewer_id:
            await _execute(
                project_id,
                "UPDATE tasks SET evidence = ?, submitted_at = ?, "
                "reviewer_id = ?, updated_at = ? WHERE id = ?",
                [json.dumps(evidence), now_ms, reviewer_id, now_ms, task_id],
            )
        else:
            await _execute(
                project_id,
                "UPDATE tasks SET evidence = ?, submitted_at = ?, updated_at = ? "
                "WHERE id = ?",
                [json.dumps(evidence), now_ms, now_ms, task_id],
            )
        await self.emit_task_event(
            project_id,
            task_id,
            "submitted",
            agent_id=agent_id,
            summary=f"[submitted] task {task_id[:8]}",
        )

        # TEST6 S11: activate/ensure review obligation on submit
        # (owner = pinned reviewer; idempotent with dispatch-time create).
        try:
            owner = reviewer_id or (meta_rows[0]["creator_id"] if meta_rows else None)
            if owner:
                from hiveweave.services.obligation import ObligationLedger

                await ObligationLedger().create(
                    project_id,
                    str(owner),
                    "review",
                    task_id=task_id,
                    context={
                        "source": "submit",
                        "assignee_id": agent_id,
                        "activated": True,
                    },
                )
        except Exception as e:
            log.warning(
                "submit_review_obligation_failed",
                task_id=task_id,
                error=str(e),
            )

        return {
            "agent_id": agent_id,
            "reviewer_id": reviewer_id,
            "submitted_at": now_ms,
        }

    async def _notify_evidence_resubmitted(
        self,
        project_id: str,
        task_id: str,
        notify: dict,
    ) -> None:
        """证据更新重提 ⇒ 给 reviewer 发一次 ``[TASK RESUBMITTED]``。

        由 ``submit_task`` 在 per-task 锁**释放后**调用（本函数会
        trigger_coordinator，可能驱动 reviewer 一整轮 turn）。防抖：本通知
        只在指纹变化路径组装；同指纹重提走幂等出口没有 ``_resubmit_notify``
        载荷，故同一指纹至多发一次。reviewer 取落库尾段钉定结果，回退
        creator。通知失败不回滚证据落库（观测面失败同语义）。
        """
        reviewer_id = str(notify.get("reviewer_id") or "")
        if not reviewer_id:
            return
        old_commit = str(notify.get("old_commit") or "")
        new_commit = str(notify.get("new_commit") or "")
        if old_commit != new_commit:
            detail = (
                f"commit {old_commit[:12] or '∅'}→{new_commit[:12] or '∅'}"
            )
        else:
            detail = "files_changed/attestations 更新"
        title = str(notify.get("title") or "").split("\n")[0][:60]
        try:
            from hiveweave.services.inbox import InboxService

            await InboxService().send_message(
                from_agent_id="system",
                to_agent_id=reviewer_id,
                message=(
                    f"[TASK RESUBMITTED] Task '{title}' 证据已更新"
                    f"（{detail}）。此前评审结论（若有）基于旧证据，请重新 "
                    f"review_task(taskId='{task_id}', "
                    f"decision='approve'/'rework')。"
                ),
                message_type="task",
                priority="normal",
                task_id=task_id,
                wake=True,
            )
            from hiveweave.agents.trigger import trigger_coordinator

            await trigger_coordinator(reviewer_id)
            log.info(
                "submit_evidence_resubmit_notified",
                project_id=project_id,
                task_id=task_id,
                reviewer_id=reviewer_id[:8],
                detail=detail,
            )
        except Exception as e:
            log.warning(
                "submit_resubmit_notify_failed",
                task_id=task_id,
                reviewer_id=reviewer_id[:8],
                error=str(e),
            )

    @staticmethod
    def _validate_verdict_evidence(evidence: dict) -> None:
        """E1: 终验任务 evidence 硬校验 —— verdict 强制判定字段。

        verdict ∈ {PASS, FAIL}；verdict=FAIL 时 blocking_issues 必须为非空
        list。缺失或非法 → ValueError，点名缺什么（对齐 SUBMIT PRE-RUN
        FAILED 硬拒风格）。非终验任务无需这些字段，由调用方按谓词筛选。
        判定逻辑单源在 ``verify.verdict_evidence_gaps``（工具层聚合预检
        复用同一份，文案保持一致）。
        """
        gaps = verdict_evidence_gaps(evidence)
        if gaps:
            raise ValueError(
                "SUBMIT VERDICT REJECTED (verify task): " + "；".join(gaps)
            )

    @staticmethod
    def _is_degraded_assignee(task: dict | None) -> bool:
        """E5: 提交者（assignee）是否处于断流降级标志。

        惰性 import 规避 agents→services 循环依赖；registry 读取失败按
        False 处理（fail-open，不误伤正常提交）。
        """
        if not task:
            return False
        agent_id = str(task.get("assignee_id") or "")
        if not agent_id:
            return False
        try:
            from hiveweave.agents.recovery import is_degraded

            return is_degraded(agent_id)
        except Exception:
            return False

    async def _resolve_evidence_workspace(
        self, project_id: str, task: dict
    ) -> str:
        """Prefer assignee write worktree; fall back to project root."""
        from hiveweave.db import meta as meta_db

        project_ws = await meta_db.get_project_workspace(project_id) or ""
        assignee_id = task.get("assignee_id")
        if not assignee_id:
            return project_ws
        try:
            from hiveweave.services.org import OrgService

            agent = await OrgService().get_agent(str(assignee_id))
            wt = (agent or {}).get("workspace_path") or ""
            if wt:
                from pathlib import Path

                if Path(wt).is_dir():
                    return wt
        except Exception as e:
            log.debug(
                "evidence_workspace_fallback",
                task_id=(task.get("id") or "")[:12],
                error=str(e),
            )
        return project_ws

