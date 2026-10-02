"""「用户行动诉求 → 无任务落地」SLA 监控（T10）。

S15 事故：用户 12:40 发返修要求（Goals Workbook 更新进 chat），执行者
只读不动 1 小时+，期间无任何告警 —— 休眠中层机制（
``TaskService.has_dormant_idle_team``）管的是「有团队没派活」，管不到
「用户诉求没被转成任何任务」。本模块补这一层：

- 候选：用户**直发**消息（``chat_messages.role='user' AND is_background=0``
  —— trigger digest / busy 兜底落库均带 ``is_background=1``，天然排除）
  且命中行动诉求启发式 :func:`is_action_directive`；
- 落地证据（闭式，消息 ``created_at`` 之后任一即算落地）：
  ① 项目内新任务（``tasks``）② 新 dispatch（``handoffs``）
  ③ 收件 agent 的新执行回合（``agent_runs``）。
  注意「收件 agent 有过 run」即算已被消费 —— 「消费了但没产出任务」的
  残余缺口归任务 dwell 时钟（``_nudge_stale_ledger``）与沉默看门狗，
  本监控不重复报警（轻量、互补不重叠）。
- SLA：``HIVEWEAVE_USER_DIRECTIVE_SLA_MIN``（默认 20 分钟）无证据 ⇒
  给收件 agent 投 platform_notice（幂等键 ``user-directive:{msg_id}:n1``，
  wake=1，inbox watcher ≤5s 唤醒）；
  超 2×SLA 仍无 ⇒ 升级再提醒一次（键 ``:n2``，收件 agent + 项目 CEO）。
  一条消息至多两条 notice，此后不再打扰。
"""

from __future__ import annotations

import json
import os
import re
import time

import structlog

log = structlog.get_logger(__name__)

# 扫描窗口：只看最近 24h 的直发用户消息（更老的诉求不是本监控的对象）。
_SCAN_HORIZON_MS = 24 * 60 * 60 * 1000

#: notice 正文预览截断
_PREVIEW_LIMIT = 60

_NOTICE_L1 = (
    "[USER DIRECTIVE PENDING] 用户在 {elapsed_min} 分钟前直接向你提出行动诉求"
    "（“{preview}”），至今项目内没有产生新的任务、派工或执行回合。"
    "若该诉求需要落地：立即 dispatch_task 拆解派工；若不需要落地或信息不足："
    "用 message_user 回复用户说明情况，不得静默。"
)
_NOTICE_L2 = (
    "[USER DIRECTIVE ESCALATED] 用户诉求（“{preview}”）已 {elapsed_min} 分钟"
    "无任何落地（无新任务/派工/执行回合），此为升级提醒。请立即介入：接手处理、"
    "或明确阻塞原因并回复用户。"
)

# ── 行动诉求启发式 ───────────────────────────────────────────
# 判定顺序：白名单 → 动词 → 疑问句豁免 → 长度。动词只用双字及以上的
# 明确词，不用「做/改/修/写/加/建」这类单字（「怎么做」「改天」误报）。

#: 纯寒暄/确认/表情 —— 无论多长都不是行动诉求（白名单，防误报第一道）
_WHITELIST_RE = re.compile(
    r"^(?:你好|您好|hi|hello|hey|嗨|哈喽|谢谢|多谢|感谢|thanks|thank you|thx|"
    r"辛苦|辛苦了|辛苦啦|收到|好的|好嘞|好哒|ok|okay|嗯|嗯嗯|哦|噢|喔|"
    r"哈哈|哈哈哈|嘿嘿|嘻嘻|厉害|不错|加油|晚安|早安|中午好|下午好|"
    r"👍|🎉|🙏|😀|😄|😅|👌|❤️)"
    r"[\s!！.。,，~～?？]*$",
    re.IGNORECASE,
)

#: 含「实义内容」（字母/数字/汉字）——纯标点/表情直接排除
_HAS_CONTENT_RE = re.compile(r"[\w\u4e00-\u9fff]")

#: 行动动词（中文双字及以上；英文按词边界）
_ACTION_VERBS = (
    "开发", "修复", "实现", "交付", "部署", "更新", "修改", "重构", "优化",
    "排查", "完成", "上线", "联调", "添加", "新增", "删除", "移除", "迁移",
    "调试", "编写", "补充", "改进", "完善", "验证", "校验", "返修", "集成",
    "配置", "安装", "评审", "发布", "落地", "跟进", "处理", "执行", "解决",
    "测试", "整一个", "修一下", "改一下", "写一个", "加一个", "补一下",
)
_EN_VERB_RE = re.compile(
    r"\b(?:fix|fixes|fixed|implement|deployment|deploy|update|refactor|"
    r"deliver|build|add|remove|debug|write|optimize|complete|release|ship|"
    r"migrate|test|verify|resolve|address|review|merge|rework|patch|please)\b",
    re.IGNORECASE,
)

#: 长度阈值（字）：超过即视为行动诉求（除非白名单/疑问句豁免）
_LENGTH_THRESHOLD = 50


def is_action_directive(content: str | None) -> bool:
    """用户消息是否含行动诉求（启发式，纯函数）。

    白名单（寒暄/确认/表情）→ False；含行动动词 → True；无动词的
    疑问句 → False；长度 >50 字 → True；其余 → False。
    """
    text = (content or "").strip()
    if not text or not _HAS_CONTENT_RE.search(text):
        return False
    if _WHITELIST_RE.match(text):
        return False
    if any(v in text for v in _ACTION_VERBS) or _EN_VERB_RE.search(text):
        return True
    if "？" in text or "?" in text:
        return False
    return len(text) > _LENGTH_THRESHOLD


def _sla_minutes() -> int:
    """SLA 分钟数（调用时读 env，测试可 monkeypatch）。"""
    raw = (os.environ.get("HIVEWEAVE_USER_DIRECTIVE_SLA_MIN") or "").strip()
    try:
        value = int(raw) if raw else 20
    except ValueError:
        value = 20
    return max(1, value)


def _idempotency_key(msg_id: str, level: str) -> str:
    return f"user-directive:{msg_id}:{level}"


async def _has_landing_evidence(
    conn, since_ms: int, agent_id: str
) -> bool:
    """消息之后项目内是否出现过 新任务/新 dispatch/收件 agent 新 run。"""
    cur = await conn.execute(
        "SELECT "
        "(SELECT COUNT(*) FROM tasks WHERE created_at > ?) AS t, "
        "(SELECT COUNT(*) FROM handoffs WHERE created_at > ?) AS h, "
        "(SELECT COUNT(*) FROM agent_runs "
        " WHERE agent_id = ? AND started_at > ?) AS r",
        [since_ms, since_ms, agent_id, since_ms],
    )
    row = await cur.fetchone()
    await cur.close()
    if row is None:
        return False
    return bool(row["t"] or row["h"] or row["r"])


async def _key_exists(conn, to_agent_id: str, key: str) -> bool:
    cur = await conn.execute(
        "SELECT 1 FROM inbox WHERE to_agent_id = ? AND idempotency_key = ? "
        "LIMIT 1",
        [to_agent_id, key],
    )
    row = await cur.fetchone()
    await cur.close()
    return row is not None


async def _ceo_id(conn) -> str | None:
    """项目根（CEO）：per-project DB 里 parent_id IS NULL 的 active agent。"""
    cur = await conn.execute(
        "SELECT id FROM agents "
        "WHERE parent_id IS NULL AND status = 'active' AND id != '' "
        "ORDER BY created_at ASC LIMIT 1"
    )
    row = await cur.fetchone()
    await cur.close()
    return str(row["id"]) if row else None


def _preview(content: str | None) -> str:
    text = (content or "").replace("\n", " ").strip()
    if len(text) > _PREVIEW_LIMIT:
        return text[:_PREVIEW_LIMIT] + "…"
    return text


async def check_user_directive_sla(
    project_id: str, *, now_ms: int | None = None
) -> int:
    """扫描未落地的用户行动诉求并按 SLA 投递提醒。

    由 game_time tick（STALL_CHECK_TICKS 槽位，~2min）周期调用；
    也可单独调用。幂等由 inbox ``idempotency_key`` 保证。

    Returns:
        本次新投递的 notice 条数（幂等去重的不计）。
    """
    from hiveweave.db.project import get_project_db_by_project_id
    from hiveweave.services.inbox import InboxService
    from hiveweave.services.off_duty import is_project_off_duty
    from hiveweave.services.system_state import system_state

    if system_state.paused():
        return 0
    if await is_project_off_duty(project_id):
        return 0

    now = now_ms if now_ms is not None else int(time.time() * 1000)
    sla_min = _sla_minutes()
    sla_ms = sla_min * 60 * 1000

    conn = await get_project_db_by_project_id(project_id)
    cur = await conn.execute(
        "SELECT id, agent_id, content, metadata, created_at FROM chat_messages "
        "WHERE role = 'user' AND is_background = 0 AND created_at > ? "
        "ORDER BY created_at ASC",
        [now - _SCAN_HORIZON_MS],
    )
    rows = await cur.fetchall()
    await cur.close()

    inbox = InboxService()
    sent = 0
    for row in rows:
        msg = dict(row)
        msg_id = str(msg.get("id") or "")
        recipient = str(msg.get("agent_id") or "")
        created_at = int(msg.get("created_at") or 0)
        if not msg_id or not recipient or not created_at:
            continue
        # metadata.source 防御：system 落库的 role=user 行不是用户直发
        meta_raw = msg.get("metadata")
        try:
            meta = json.loads(meta_raw) if isinstance(meta_raw, str) else (
                meta_raw if isinstance(meta_raw, dict) else {}
            )
        except (json.JSONDecodeError, TypeError):
            meta = {}
        if (meta or {}).get("source") == "system":
            continue
        if not is_action_directive(str(msg.get("content") or "")):
            continue

        elapsed = now - created_at
        if await _has_landing_evidence(conn, created_at, recipient):
            continue

        key1 = _idempotency_key(msg_id, "n1")
        key2 = _idempotency_key(msg_id, "n2")
        # n1 存在性按**本次调用前**判定：升级梯子一档一档走，即便消息
        # 一次跳过 2×SLA（如后端重启），也先 n1、下个 tick 再 n2。
        had_n1 = await _key_exists(conn, recipient, key1)
        elapsed_min = int(elapsed / 60000)
        params = {
            "elapsed_min": elapsed_min,
            "preview": _preview(str(msg.get("content") or "")),
        }

        # ① 超 SLA 且未提醒 → 提醒收件 agent（幂等）。
        # 单条消息投递失败不得中断整轮扫描（如收件 agent 已 archived 时
        # send_message 抛 ValueError）——按条隔离，下个 tick 自然重试。
        if elapsed >= sla_ms and not had_n1:
            try:
                await inbox.send_message(
                    from_agent_id="system",
                    to_agent_id=recipient,
                    message=_NOTICE_L1.format(**params),
                    message_type="platform_notice",
                    priority="urgent",
                    wake=True,
                    trusted_platform=True,
                    idempotency_key=key1,
                )
                sent += 1
                log.warning(
                    "user_directive_sla_breach",
                    project_id=project_id,
                    agent_id=recipient,
                    message_id=msg_id,
                    elapsed_min=elapsed_min,
                    sla_min=sla_min,
                )
            except Exception as e:
                log.warning(
                    "user_directive_sla_notice_failed",
                    project_id=project_id,
                    agent_id=recipient,
                    message_id=msg_id,
                    error=str(e),
                )
                continue

        # ② 超 2×SLA 且一级提醒已投 → 升级：收件 agent + 项目 CEO
        if (
            elapsed >= 2 * sla_ms
            and had_n1
            and not await _key_exists(conn, recipient, key2)
        ):
            try:
                await inbox.send_message(
                    from_agent_id="system",
                    to_agent_id=recipient,
                    message=_NOTICE_L2.format(**params),
                    message_type="platform_notice",
                    priority="urgent",
                    wake=True,
                    trusted_platform=True,
                    idempotency_key=key2,
                )
                sent += 1
            except Exception as e:
                log.warning(
                    "user_directive_sla_escalate_failed",
                    project_id=project_id,
                    agent_id=recipient,
                    message_id=msg_id,
                    error=str(e),
                )
                continue
            try:
                ceo = await _ceo_id(conn)
                if ceo and ceo != recipient:
                    await inbox.send_message(
                        from_agent_id="system",
                        to_agent_id=ceo,
                        message=_NOTICE_L2.format(**params),
                        message_type="platform_notice",
                        priority="urgent",
                        wake=True,
                        trusted_platform=True,
                        idempotency_key=f"{key2}:{ceo}",
                    )
                    sent += 1
            except Exception as e:
                log.warning(
                    "user_directive_sla_escalate_ceo_failed",
                    project_id=project_id, error=str(e),
                )
            log.warning(
                "user_directive_sla_escalated",
                project_id=project_id,
                agent_id=recipient,
                message_id=msg_id,
                elapsed_min=elapsed_min,
                sla_min=sla_min,
            )
    return sent
