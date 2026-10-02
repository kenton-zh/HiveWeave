"""ask(reply_required) 无回复升级 SLA（T21，S16 牧云/霜叶实测）。

S16 事故：霜叶向牧云发 reply_required ask（二选一决策），牧云反刍卡死
3 小时不答，霜叶无限等待；霜叶跨级直接求救被指挥链 ACL 硬禁（
「禁止跨级沟通」）—— 一人卡死整条线，平台没有任何上报出口。

本模块补这一层（与 ``user_directive_sla`` / ``obligation`` 同族的轻量
对账式扫描，由 game tick 驱动）：

- 对象：项目内**仍未关闭**的 ask 回复合约（``inbox.expect_report = 1``
  且 ``reply_contract_id`` 非空，且该合约 id 未出现在任何 ``reply_to``
  ——关闭判据与 ``inbox.get_outstanding_ask_recipients`` 同构，纯结构化，
  不扫文案）。
- 升级节奏（两段式）：ask 超过 ``HIVEWEAVE_ASK_ESCALATION_MIN``（env，
  默认 30 分钟）未获回复 ⇒ 向 **asker 的组织 parent** 投 ESCALATION
  notice（wake=1，内容含 ask 摘要与「你的下属在等待决策」）；再超一倍
  时长 ⇒ 再升一级（parent 的 parent…），直到 CEO（根）为止。平台
  system 身份投递，不过指挥链 ACL（升级/看门狗路径豁免，见 org_span
  模块注释）。
- 幂等：显式 ``idempotency_key = ask-esc:{contract_id}:L{level}``（inbox
  有 (to_agent_id, idempotency_key) 部分唯一索引兜底），发送前先查键；
  每个合约每轮扫描至多补发**一档**（掉线补扫不轰炸）。
- asker 本人是根（CEO）或已归档 ⇒ 无升级对象，跳过；合约关闭/超
  扫描窗口 ⇒ 不再升级。
"""

from __future__ import annotations

import os
import time

import structlog

from hiveweave.db import project as project_db

log = structlog.get_logger(__name__)

#: 扫描窗口：只看最近 48h 的 ask 合约（更老的合约不是本监控的对象）。
_SCAN_HORIZON_MS = 48 * 60 * 60 * 1000

#: ask 摘要预览截断
_PREVIEW_LIMIT = 80

#: notice 正文头部（固定字面量，便于按名检索）
_HEADER = "[ASK ESCALATION]"


def _sla_ms() -> int:
    """升级 SLA（毫秒）。env ``HIVEWEAVE_ASK_ESCALATION_MIN``，默认 30 分钟。

    调用时读 env（测试可 monkeypatch）；非法值回落默认并钳到 ≥1 分钟。
    """
    raw = (os.environ.get("HIVEWEAVE_ASK_ESCALATION_MIN") or "").strip()
    try:
        minutes = int(raw) if raw else 30
    except ValueError:
        minutes = 30
    return max(1, minutes) * 60 * 1000


def escalation_key(contract_id: str, level: int) -> str:
    """幂等键：同一 ask 合约的同一档升级只投一次。"""
    return f"ask-esc:{contract_id}:L{level}"


def _build_notice(
    *,
    asker_label: str,
    target_label: str,
    preview: str,
    elapsed_min: int,
    level: int,
    chain_left: int,
    asker_id: str,
    target_id: str,
) -> str:
    """升级 notice 正文：事实（谁在等谁、等了多久、ask 说的什么）+ 处方。"""
    tier = f"L{level}" + (
        f"（再 {(chain_left - 1)} 级到顶）" if chain_left > 1 else "（已到顶）"
    )
    # 完整 id 随行（AGENTS 铁律：public ids whole，不截断）——上级据此
    # 可直接 send_message / 派单，不必再猜 8 位前缀。
    return (
        f"{_HEADER} {tier} 你的下属 {asker_label} 在 {elapsed_min} 分钟前"
        f"向 {target_label} 发出要求回复的 ask，至今未获回复 —— "
        f"你的下属在等待决策。ask 摘要：「{preview}」。"
        f"请介入：向 {target_label} 核实并推动其回复，或直接给出裁决；"
        f"若该 ask 已无意义，让 {asker_label} 释放等待"
        "（回复对方或换用 notify）。本通知为平台升级，回复对象是 "
        f"{asker_label} 本人。\nids: asker={asker_id} target={target_id}"
    )


async def _escalation_chain(project_id: str, asker_id: str) -> list[str]:
    """asker 的组织上级链（parent → grandparent → … → 根），去环、滤归档。"""
    from hiveweave.services.org import OrgService

    org = OrgService()
    chain: list[str] = []
    seen: set[str] = {asker_id}
    cur = asker_id
    for _ in range(10):  # 深度上限 = 防环；组织深度远小于此
        try:
            agent = await org.get_agent(cur)
        except Exception:
            agent = None
        if not agent or agent.get("is_archived"):
            break
        parent = str(agent.get("parent_id") or "")
        if not parent or parent in seen:
            break
        try:
            parent_agent = await org.get_agent(parent)
        except Exception:
            parent_agent = None
        if not parent_agent or parent_agent.get("is_archived"):
            break
        chain.append(parent)
        seen.add(parent)
        cur = parent
    return chain


async def check_ask_escalation(project_id: str) -> list[dict]:
    """扫描未关闭 ask 合约，按 SLA 两段式向 asker 上级链投升级 notice。

    Returns list of ``{contract_id, level, escalated_to}``（本轮新投的档）。
    任何单点失败不拖垮整轮扫描（逐合约 try）。
    """
    now_ms = int(time.time() * 1000)
    sla = _sla_ms()
    try:
        rows = await project_db.query_by_project(
            project_id,
            "SELECT id, from_agent_id, to_agent_id, message, created_at, "
            "reply_contract_id FROM inbox "
            "WHERE expect_report = 1 AND reply_contract_id IS NOT NULL "
            "AND created_at >= ? AND from_agent_id != 'system'",
            [now_ms - _SCAN_HORIZON_MS],
        )
        closed_rows = await project_db.query_by_project(
            project_id,
            "SELECT DISTINCT reply_to FROM inbox WHERE reply_to IS NOT NULL",
        )
    except Exception as e:
        log.warning("ask_escalation_scan_failed", project_id=project_id,
                    error=str(e))
        return []

    closed = {
        r["reply_to"] for r in closed_rows if r["reply_to"]
    }

    escalated: list[dict] = []
    for row in rows:
        contract = str(row["reply_contract_id"] or "")
        asker = str(row["from_agent_id"] or "")
        target = str(row["to_agent_id"] or "")
        if not contract or not asker or contract in closed:
            continue
        try:
            created_at = int(row["created_at"] or 0)
        except (TypeError, ValueError):
            continue
        age_ms = now_ms - created_at
        if age_ms < sla:
            continue
        try:
            chain = await _escalation_chain(project_id, asker)
        except Exception as e:
            log.warning("ask_escalation_chain_failed", asker=asker[:8],
                        error=str(e))
            continue
        if not chain:
            continue  # asker 是根（CEO）或上级全归档 —— 无处可升
        max_due_level = min(int(age_ms // sla), len(chain))
        # 幂等 + 每轮至多补一档：找最低的「到期且未投过」的档位。
        for level in range(1, max_due_level + 1):
            key = escalation_key(contract, level)
            try:
                existing = await project_db.query_by_project(
                    project_id,
                    "SELECT id FROM inbox WHERE idempotency_key = ? LIMIT 1",
                    [key],
                )
            except Exception as e:
                log.warning("ask_escalation_key_lookup_failed",
                            contract=contract[:8], error=str(e))
                break
            if existing:
                continue
            send_to = chain[level - 1]
            preview = str(row["message"] or "").replace("\n", " ").strip()
            preview = preview[:_PREVIEW_LIMIT] or "(空)"
            msg = _build_notice(
                asker_label=asker[:8],
                target_label=target[:8] or "?",
                preview=preview,
                elapsed_min=max(1, int(age_ms // 60_000)),
                level=level,
                chain_left=len(chain) - level + 1,
                asker_id=asker,
                target_id=target,
            )
            try:
                from hiveweave.services.inbox import InboxService

                await InboxService().send_message(
                    from_agent_id="system",
                    to_agent_id=send_to,
                    message=msg,
                    message_type="escalation",
                    priority="urgent",
                    wake=True,
                    idempotency_key=key,
                )
            except Exception as e:
                log.warning("ask_escalation_notify_failed",
                            contract=contract[:8], level=level,
                            error=str(e))
                break
            escalated.append({
                "contract_id": contract,
                "level": level,
                "escalated_to": send_to,
            })
            log.warning(
                "ask_escalated",
                project_id=project_id,
                contract_id=contract[:8],
                asker=asker[:8],
                target=target[:8],
                level=level,
                escalated_to=send_to[:8],
                age_min=max(1, int(age_ms // 60_000)),
            )
            break  # 每合约每轮至多一档
    return escalated
