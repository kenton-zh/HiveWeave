"""团队开会 — 结构化简报纯函数（docs/spec/team-meeting.md）。

铁律（金丝雀两套预言）：
- 参会简报**从不**拷贝 ``role=speech`` 行 —— 盲评的根。只含：
  会议标题、已收口议题结论、当前议题、``r/3``、（r≥2 时）主席 direction。
- RESULT 正文只含各题结论，从不拼接 utterances / speech。
- ``SPEECH_MARKER`` 是审计金丝雀：平台组装的简报 / RESULT 中出现该标记
  即为过程泄漏（规格 E 节验收）。

本模块全部是纯函数，不做 IO、不做状态迁移 —— 测试直接喂 dict 断言形状。
"""

from __future__ import annotations

from typing import Any

from hiveweave.services.meetings.service import (
    ABSTAIN_INCOMPLETE_REASONS,
    ABSTAIN_WRITTEN_BY_PLATFORM,
)

# 盲评金丝雀标记：speech 行的平台内部分类标记，绝不进简报 / RESULT。
SPEECH_MARKER = "role=speech"

MEETING_RESULT_TAG = "[MEETING RESULT]"
MEETING_ABORTED_TAG = "[MEETING ABORTED]"

# abort 结构化原因（规格 §回岗协议）：枚举之外仅 assembly_timeout/internal
# 兜底（见 orchestrator，报告决策点）。
ABORT_REASONS = frozenset({
    "chair_timeout",
    "chair_dismissed",
    "off_duty",
    "roster_lt2",
    "assembly_timeout",
    "internal",
})

_MAX_DIRECTION_CHARS = 4000
_MAX_RESULT_CHARS = 6000


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + " …[truncated]"


def participant_briefing(
    *,
    meeting_title: str,
    topic_title: str,
    round_index: int,
    max_rounds: int,
    direction: str | None,
    concluded_results: list[dict[str, Any]],
    attendee_name: str = "",
) -> str:
    """参会者会务简报（盲评）。

    只含：会议标题 / 已收口议题结论 / 当前议题 / ``r/3`` /（r≥2）主席
    direction。``direction`` 是主席注入的下一轮方向 —— 主席可以把自己
    看到的同事原文粘进去（规格接受通道，平台不扫描文案）；平台自身
    **从不**把 ``role=speech`` 行拷进本简报。
    """
    r = max(1, int(round_index))
    lines: list[str] = ["## 团队会议（盲评发言）"]
    if meeting_title:
        lines.append(f"会议：{_clip(meeting_title, 200)}")
    if attendee_name:
        lines.append(f"你以 {attendee_name} 的身份参会。")
    if concluded_results:
        lines.append("")
        lines.append("### 已收口议题的结论（不再讨论）")
        for item in concluded_results:
            title = _clip(str(item.get("title") or ""), 200)
            result = _clip(str(item.get("result") or ""), _MAX_RESULT_CHARS)
            lines.append(f"- 议题「{title}」结论：{result}")
    lines.append("")
    lines.append("### 当前议题")
    lines.append(_clip(topic_title, 2000))
    lines.append("")
    lines.append(f"本轮：第 {r}/{int(max_rounds)} 轮")
    if r >= 2:
        if direction:
            lines.append("")
            lines.append("### 主席下一轮方向")
            lines.append(_clip(direction, _MAX_DIRECTION_CHARS))
        else:
            lines.append("")
            lines.append("### 主席下一轮方向")
            lines.append("（主席本轮未给出具体方向；请补充或修正你的观点。）")
    lines.append("")
    lines.append(
        "请只从你自己的职责角度发言：用 speak_in_meeting 工具提交你的发言"
        "（一次、非空）。你看不到其他参会者的发言原文 —— 独立判断，不要"
        "揣测别人会说什么。"
    )
    text = "\n".join(lines)
    # 平台保证：简报里不得出现 speech 分类标记（金丝雀自检，纯函数层兜底）。
    # 显式 raise 而非 assert——-O 下 assert 失明会把泄漏原文发给参会者。
    if SPEECH_MARKER in text:
        raise ValueError("participant briefing leaked speech marker")
    return text


def chair_briefing(
    *,
    meeting_title: str,
    topic_title: str,
    round_index: int,
    max_rounds: int,
    speeches: list[dict[str, Any]],
    concluded_results: list[dict[str, Any]],
    allow_continue: bool,
) -> str:
    """主持简报 —— 含本轮全部 speech（含弃权），只给主席。"""
    r = max(1, int(round_index))
    lines: list[str] = ["## 团队会议（主持决策）"]
    if meeting_title:
        lines.append(f"会议：{_clip(meeting_title, 200)}")
    lines.append(f"当前议题：{_clip(topic_title, 2000)}")
    lines.append(f"本轮：第 {r}/{int(max_rounds)} 轮")
    if concluded_results:
        lines.append("")
        lines.append("### 已收口议题的结论")
        for item in concluded_results:
            title = _clip(str(item.get("title") or ""), 200)
            result = _clip(str(item.get("result") or ""), _MAX_RESULT_CHARS)
            lines.append(f"- 议题「{title}」结论：{result}")
    lines.append("")
    lines.append(f"### 本轮发言（{len(speeches)} 人，含弃权）")
    incomplete = 0
    platform_written = 0
    for s in speeches:
        name = str(s.get("agent_name") or s.get("agent_id") or "?")
        role = str(s.get("role") or "speech")
        content = str(s.get("content") or "").strip()
        reason = str(s.get("abstain_reason") or "").strip()
        if role == "abstain" or not content:
            # ⭐ 2026-09-18：三分「主动弃权」/「未完成」/「平台代写」。
            # 判据取结构化列 `abstain_reason`，**不读 content 文案**。
            #   - 未完成（预算切断/超时/异常）：跑过但被切断 ⇒ 加轮可救；
            #   - 平台代写（dismiss/泵恢复/无实例）：从未表态 **且人不在场**
            #     ⇒ 加轮救不回，**不得**提示主席加轮（那会误导它空转）；
            #   - 其余 → 真弃权（听了、没意见），可安全视为无异议。
            if reason in ABSTAIN_INCOMPLETE_REASONS:
                incomplete += 1
                lines.append(
                    f"- {name}：⚠ 未完成（{reason}）—— **未表态**，"
                    "其意见不在本轮发言中，请勿当作弃权或无异议"
                )
            elif reason in ABSTAIN_WRITTEN_BY_PLATFORM:
                platform_written += 1
                lines.append(
                    f"- {name}：⚠ 未参会（{reason}）—— **未表态**，"
                    "本条为平台代写，请勿当作弃权或无异议"
                )
            else:
                lines.append(f"- {name}：（弃权/未发言）{content}")
        else:
            lines.append(f"- {name}：{_clip(content, _MAX_RESULT_CHARS)}")
    # ⚠ 只在**还能加轮**时提加轮建议（2026-09-18 审计 P1-2）。
    # 第 3 轮 `allow_continue=False`：`tools_for_profile` 已把
    # `continue_meeting_round` 从主席白名单剔除（`runner.py:71`）⇒ 这时
    # 建议加轮是**死指令**（模型物理上调不到），还会与下方「必须收口、
    # continue 已不可用」自相矛盾。只报事实，不给做不到的建议。
    if incomplete and allow_continue:
        lines.append("")
        lines.append(
            f"⚠ 本轮有 {incomplete} 人未完成发言（预算切断/超时/异常）——"
            "他们的意见**没有进入本轮**。若该议题依赖他们的判断，"
            "考虑用 continue_meeting_round 再给一轮，而不是直接收口。"
        )
    elif incomplete:
        lines.append("")
        lines.append(
            f"⚠ 本轮有 {incomplete} 人未完成发言（预算切断/超时/异常）——"
            "他们的意见**没有进入本轮**。本轮已是最后一轮、无法再加轮，"
            "收口时请把这一点写进结论（例如注明某方意见缺失），"
            "不要当作他们已表态。"
        )
    if platform_written:
        lines.append("")
        lines.append(
            f"⚠ 另有 {platform_written} 人未参会（平台代写弃权）——"
            "他们**从未表态**，且加轮无法让他们发言。"
            "收口时请基于到场者的意见，不要假设他们同意。"
        )
    lines.append("")
    if allow_continue:
        lines.append(
            "请决策：用 continue_meeting_round(direction=…) 注入下一轮方向，"
            "或用 conclude_topic(result=…) 收口本议题。第 3 轮必须收口。"
        )
    else:
        lines.append(
            "这是最后一轮（3/3）：必须用 conclude_topic(result=…) 收口本议题，"
            "continue 已不可用。"
        )
    return "\n".join(lines)


def runner_overlay(tool_profile: str) -> str:
    """会务回合 overlay 系统提示：过程遗忘 + 工具边界。

    本回合不进记忆：runner 不 append_turn / 不写 work_log / 不 ACK inbox。
    """
    if tool_profile == "chair":
        decision_line = (
            "你是本场会议的主席：你自己的发言已在参会阶段提交过，本回合"
            "只需读完全部发言后用 continue_meeting_round / conclude_topic "
            "做出主持决策，不要再调 speak_in_meeting（会以发言结束回合、"
            "决策作废）。"
        )
    else:
        decision_line = "用 speak_in_meeting 提交你的发言。"
    return (
        "## 团队会议会务回合\n"
        "这是一场平台组织的团队会议的会务回合，**不进入你的个人记忆与主聊"
        "历史**：结束后你只会收到 [MEETING RESULT]（或 [MEETING ABORTED]）。\n"
        f"{decision_line}\n"
        "禁止调用 write_memory / write_work_log / send_message / write_file "
        "等任何写入类工具（平台硬拒）；不要试图向同伴发消息 —— 你看不到他们"
        "的发言原文，他们也看不到你的。发言/决策完成即结束本回合。\n"
        "除了会务工具，你只有只读检索工具可用于核实事实。"
    )


def result_body(topic_results: list[dict[str, Any]]) -> str:
    """RESULT 正文 —— 只含各题结论，不拼接 utterances（金丝雀自检）。"""
    lines = ["团队会议已结束。各议题结论如下："]
    for item in topic_results or []:
        title = _clip(str(item.get("title") or ""), 200)
        result = _clip(str(item.get("result") or ""), _MAX_RESULT_CHARS)
        lines.append(f"- 「{title}」：{result}")
    text = "\n".join(lines)
    if SPEECH_MARKER in text:
        raise ValueError("RESULT body leaked speech marker")
    return text


def aborted_body(reason: str, meeting_title: str = "") -> str:
    """abort 通知正文 —— 结构化原因，不编造结论。"""
    why = reason if reason in ABORT_REASONS else "internal"
    head = "团队会议已中止（未形成结论）。"
    if meeting_title:
        head = f"团队会议「{_clip(meeting_title, 200)}」已中止（未形成结论）。"
    reason_text = {
        "chair_timeout": "chair_timeout（主席两轮未在时限内做出决策）",
        "chair_dismissed": "chair_dismissed（主席已被解散/归档）",
        "off_duty": "off_duty（项目下班/停用）",
        "roster_lt2": "roster_lt2（在册存活参会者不足 2 人）",
        "assembly_timeout": "assembly_timeout（集合超时，无法全员就位）",
        "internal": "internal（平台内部错误）",
    }.get(why, why)
    return f"{head}\n原因：{reason_text}\n你的原岗位工作（等待/任务/积压消息）不受影响，现已解冻。"
