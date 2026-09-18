"""团队开会 — 盲评简报形状（docs/spec/team-meeting.md §规格验收 C）。

用唯一标记（canary）探测：参会者简报不含同事 speech 原文；第 2/3 轮含
direction 仍不含原文；议题 2 轮 1 含议题 1 的 topic_result。主席简报含
全部 speech（含弃权）。
"""

from __future__ import annotations

import asyncio

from hiveweave.services.meetings import orchestrator, prompts
from hiveweave.services.meetings.service import (
    ABSTAIN_BUDGET_EXHAUSTED,
    ABSTAIN_RECOVERED,
    MAX_ROUNDS,
    meeting_service,
)
from tests.meeting_env import (
    insert_agent,
    meeting_env,
    reset_meeting_state,
)

PID = "mtg-blind-1"
CANARY = "CANARY-SPEECH-_DO_NOT_LEAK_"


def _scripted_runner(pid: str, meeting_id_box: dict, log: list):
    """参会者按名册顺序发言（唯一 canary）；主席按 (topic, round) 决策。"""

    async def fake_runner(agent, *, tool_profile, briefing, allow_continue, timeout_s):
        meeting_id = meeting_id_box.get("id")
        if tool_profile == "participant":
            n = len([e for e in log if e["profile"] == "participant"])
            content = f"{CANARY}-{n}"
            log.append({"profile": tool_profile, "briefing": briefing})
            return {"action": "speak", "content": content}
        log.append({
            "profile": tool_profile,
            "briefing": briefing,
            "allow_continue": allow_continue,
        })
        meeting = await meeting_service.get_meeting(pid, meeting_id)
        t, r = meeting["topic_index"], meeting["round_index"]
        if (t, r) == (0, 1):
            return {
                "action": "continue",
                "direction": "DIR-1 focus on imports only",
            }
        return {"action": "conclude", "result": f"CONCLUSION-T{t + 1}-R{r}"}

    return fake_runner


async def test_participant_briefings_are_blind_to_colleague_speeches():
    """参会者简报：不含任何 speech canary；r=2 含 direction；议题 2 含议题 1 结论。"""
    async with meeting_env(PID) as env:
        pid = env["project_id"]
        for aid in ("chair-1", "p-1", "p-2"):
            await insert_agent(pid, aid)
        entries: list = []
        box: dict = {}
        orchestrator.set_runner_fn(_scripted_runner(pid, box, entries))

        async def fake_start():
            return None

        meeting = await meeting_service.create_meeting(
            pid, "chair-1", "imports", ["topic-1", "topic-2"],
            ["p-1", "p-2"],
        )
        box["id"] = meeting["id"]
        await orchestrator._orchestrate(pid, meeting["id"])
        meeting = await meeting_service.get_meeting(pid, meeting["id"])
        assert meeting["status"] == "concluded"
        assert len(meeting["topic_results"]) == 2

        participant_briefs = [
            e["briefing"] for e in entries if e["profile"] == "participant"
        ]
        chair_briefs = [
            e["briefing"] for e in entries if e["profile"] == "chair"
        ]
        assert participant_briefs and chair_briefs

        # C：参会者简报不含同事（含自己的上一轮）speech 原文
        for b in participant_briefs:
            assert CANARY not in b, f"speech leaked into participant brief: {b}"
        # r=1 无 direction；r=2 含 direction 仍不含原文
        assert "DIR-1" not in participant_briefs[0]
        later = [b for b in participant_briefs if "DIR-1" in b]
        assert later, "round-2 briefings must contain the chair direction"
        for b in later:
            assert CANARY not in b
        # 议题 2 轮 1 含议题 1 的 topic_result，不含议题 1 speech
        t2_briefs = [b for b in participant_briefs if "CONCLUSION-T1" in b]
        assert t2_briefs
        for b in t2_briefs:
            assert CANARY not in b
        # 主席简报含本轮全部 speech（含弃权）
        assert sum(CANARY in b for b in chair_briefs) >= 1
        # 金丝雀：speech 标记不得出现在任何平台组装文本中
        all_briefs = participant_briefs + chair_briefs
        assert all(prompts.SPEECH_MARKER not in b for b in all_briefs)


async def test_result_body_contains_only_conclusions():
    """平台组装只含 topic_results，不拼接 utterances（金丝雀预言二）。"""
    results = [
        {"title": "t1", "result": "use asyncio"},
        {"title": "t2", "result": "no canary here"},
    ]
    body = prompts.result_body(results)
    assert "use asyncio" in body
    assert CANARY not in body
    assert prompts.SPEECH_MARKER not in body


async def test_runner_fn_never_exceeds_parallel_cap():
    """注入 runner_fn 断言 max(in_flight)<=8（规格 G）。"""
    async with meeting_env(PID) as env:
        pid = env["project_id"]
        roster = ["chair-1"] + [f"p-{i}" for i in range(20)]
        for aid in roster:
            await insert_agent(pid, aid)
        in_flight = 0
        max_in_flight = 0

        async def fake_runner(agent, *, tool_profile, briefing, allow_continue, timeout_s):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            await asyncio.sleep(0.005)
            in_flight -= 1
            if tool_profile == "participant":
                return {"action": "abstain", "content": "skip"}
            return {"action": "conclude", "result": "done"}

        reset_meeting_state()
        orchestrator.set_runner_fn(fake_runner)
        meeting = await meeting_service.create_meeting(
            pid, "chair-1", "wide", ["only-topic"], roster[1:]
        )
        await orchestrator._orchestrate(pid, meeting["id"])
        assert max_in_flight <= 8, max_in_flight
        final = await meeting_service.get_meeting(pid, meeting["id"])
        assert final["status"] == "concluded"
        # 未发言人（全员 abstain）也进了主席简报（含弃权）
        utters = await meeting_service.get_utterances(
            pid, meeting["id"], topic_index=0, round_index=1,
            roles=("abstain",),
        )
        assert len(utters) >= len(roster) - 1


async def test_chair_briefing_separates_incomplete_from_abstain():
    """⭐ 2026-09-18：「被掐断而未完成」不得渲染成「弃权/无异议」。

    病灶（PLATFORM-ISSUES §8.5 的 #3 路径）：参会者被 8 轮预算切断时，
    ``_outcome_from`` 若走普通 abstain，主席简报会把「**没来得及说话**」
    与「听了、没意见」渲染成同一行 ``（弃权/未发言）`` ⇒ 主席据此推进决策。
    这是「用正常状态掩盖异常状态」的又一实例。

    判据取**结构化列** ``abstain_reason``；断言同样只对结构化事实与平台
    自生成的固定标签，不去匹配自由 content 文案（用户 09-14 钦定：禁文本
    子串判据 —— 这里匹配的 "未表态" 是 prompts.py 里的固定标签，不是模型
    可自由变化的输出）。

    阳性对照（本测试的守卫必须能转红）：把 prompts.chair_briefing 里
    ``reason in ABSTAIN_INCOMPLETE_REASONS`` 的分支改回无条件
    ``（弃权/未发言）``，本测试必须失败。已实测：断言①②③ 均转红。
    """
    async with meeting_env(PID) as env:
        pid = env["project_id"]
        # chair 默认参会，第 1 轮与 p-1/p-2 一起发言 ⇒ 名册 3 人。
        for aid in ("chair-1", "p-1", "p-2"):
            await insert_agent(pid, aid)
        briefings: list[str] = []
        part_calls: list[str] = []
        box: dict = {}

        async def fake_runner(agent, *, tool_profile, briefing, allow_continue, timeout_s):
            if tool_profile == "participant":
                # 按调用序分流（并发 gather 但 batch 顺序稳定）。
                # 第 1 个参会者正常发言，其余被轮次预算切断。
                part_calls.append(briefing)
                if len(part_calls) == 1:
                    return {"action": "speak", "content": "NORMAL-SPEECH"}
                return {
                    "action": "abstain",
                    "content": "budget exhausted before the turn finished",
                    "abstain_reason": ABSTAIN_BUDGET_EXHAUSTED,
                }
            briefings.append(briefing)
            return {"action": "conclude", "result": "done"}

        reset_meeting_state()
        orchestrator.set_runner_fn(fake_runner)
        meeting = await meeting_service.create_meeting(
            pid, "chair-1", "budget-cut", ["only-topic"], ["p-1", "p-2"]
        )
        box["id"] = meeting["id"]
        await orchestrator._orchestrate(pid, meeting["id"])

        rows = await meeting_service.get_utterances(
            pid, meeting["id"], topic_index=0, round_index=1,
            roles=("abstain",),
        )
        # 名册 3 人，1 人发言 ⇒ 2 人弃权
        assert len(rows) == 2, rows
        # ① 结构化事实位本身必须落库（所有下游渲染的唯一依据）
        for r in rows:
            assert r["abstain_reason"] == ABSTAIN_BUDGET_EXHAUSTED, dict(r)

        # ② 主席简报必须把「未完成」与「弃权」分开渲染
        assert briefings, "chair briefing was never produced"
        text = briefings[0]
        assert ABSTAIN_BUDGET_EXHAUSTED in text, text
        assert "未表态" in text, text
        assert "请勿当作弃权" in text, text
        # 必须提示主席可以再给一轮，而不是直接收口。
        # ⚠ 断言文本必须是**新增提示段特有**的串（2026-09-18 审计 P1-1）：
        # 原先断 `"continue_meeting_round"` 是**空断言** —— 该词在简报里
        # 出现 3 次（含每轮都有的收尾决策段 `prompts.py:181`），把新增提示
        # 整段删掉测试照样绿。实测：删除该段后 6 passed。改断 "再给一轮"。
        assert "考虑用 continue_meeting_round 再给一轮" in text, text
        # ③ 同轮正常发言者不受影响（不能把所有人都打成「未完成」）
        assert "NORMAL-SPEECH" in text, text


async def test_chair_briefing_keeps_true_abstain_as_abstain():
    """反向对照：真·主动弃权（无 abstain_reason）仍走旧渲染路径。

    没有这条，「区分」可能被实现成「把所有 abstain 都标成未完成」——
    那会把主席推向无谓加轮，同样是失真。
    """
    async with meeting_env(PID) as env:
        pid = env["project_id"]
        for aid in ("chair-1", "p-1"):
            await insert_agent(pid, aid)
        briefings: list[str] = []

        async def fake_runner(agent, *, tool_profile, briefing, allow_continue, timeout_s):
            if tool_profile == "participant":
                # 结构化原因留空 = 真弃权
                return {"action": "abstain", "content": "no opinion"}
            briefings.append(briefing)
            return {"action": "conclude", "result": "done"}

        reset_meeting_state()
        orchestrator.set_runner_fn(fake_runner)
        meeting = await meeting_service.create_meeting(
            pid, "chair-1", "true-abstain", ["only-topic"], ["p-1"]
        )
        await orchestrator._orchestrate(pid, meeting["id"])

        rows = await meeting_service.get_utterances(
            pid, meeting["id"], topic_index=0, round_index=1,
            roles=("abstain",),
        )
        # 名册 2 人（chair-1 + p-1），都弃权
        assert len(rows) == 2, rows
        # 真弃权：不写原因 ⇒ 下游不得把它当「未完成」
        for r in rows:
            assert not (r["abstain_reason"] or ""), dict(r)

        text = briefings[0]
        assert "弃权/未发言" in text, text
        assert "未表态" not in text, text
        # 全员真弃权 ⇒ 不该出现「有人未完成」的加轮提示
        assert "consider_continue" not in text and "未完成发言" not in text, text


async def test_chair_briefing_marks_platform_written_abstain_without_urging_reround():
    """⭐ 2026-09-18 第三类：「平台代写」弃权不得混同真弃权，也不得催主席加轮。

    区分三类（判据 = 结构化 ``abstain_reason``）：
    - 未完成（预算/超时/异常）→ 加轮**可能救回** ⇒ 可提示 continue；
    - 平台代写（dismiss/泵恢复/无实例）→ 本人从未表态且**人不在场**，
      加轮救不回 ⇒ 提示 continue 是**误导**，会让主席空转；
    - 真弃权（空 reason）→ 可视为无异议。

    本测试覆盖第二类（泵恢复路径最容易在生产里出现）。
    """
    async with meeting_env(PID) as env:
        pid = env["project_id"]
        for aid in ("chair-1", "p-1"):
            await insert_agent(pid, aid)

        briefings: list[str] = []

        async def fake_runner(agent, *, tool_profile, briefing, allow_continue, timeout_s):
            if tool_profile == "participant":
                return {"action": "speak", "content": "OK-SPEECH"}
            briefings.append(briefing)
            return {"action": "conclude", "result": "done"}

        reset_meeting_state()
        orchestrator.set_runner_fn(fake_runner)
        meeting = await meeting_service.create_meeting(
            pid, "chair-1", "recover-case", ["only-topic"], ["p-1"]
        )
        # create 出来是 assembling；_run_round 只在 collecting 下有意义
        # （新会议 round_index 从 0 起，故下面按 0 查询）。
        await meeting_service.set_status(pid, meeting["id"], "collecting")
        cur = await meeting_service.get_meeting(pid, meeting["id"])
        await orchestrator._run_round(pid, cur, recovering=True)

        rows = await meeting_service.get_utterances(
            pid, meeting["id"], topic_index=0, round_index=0,
            roles=("abstain",),
        )
        assert rows, "recovering round produced no abstain rows"
        for r in rows:
            assert r["abstain_reason"] == ABSTAIN_RECOVERED, dict(r)

        # 渲染：必须标出「未参会」，且**不得**催促加轮
        text = prompts.chair_briefing(
            meeting_title="recover-case",
            topic_title="only-topic",
            round_index=1,
            max_rounds=3,
            speeches=[
                {
                    "agent_id": "p-1",
                    "role": "abstain",
                    "content": "recovered after restart",
                    "abstain_reason": ABSTAIN_RECOVERED,
                }
            ],
            concluded_results=[],
            allow_continue=True,
        )
        assert "未参会" in text, text
        assert "未表态" in text, text
        assert "平台代写" in text, text
        # ⭐ 关键：不得出现「考虑 continue」的加轮提示（人不在场，救不回）
        assert "考虑用 continue_meeting_round" not in text, text
        # 也不得走真弃权渲染
        assert "（弃权/未发言）" not in text, text


async def test_final_round_briefing_never_urges_unavailable_continue():
    """⭐ 2026-09-18 审计 P1-2：最后一轮**不得**建议加轮。

    第 3 轮 `allow_continue=False` ⇒ `tools_for_profile` 已把
    `continue_meeting_round` 从主席白名单剔除（`runner.py:71`
    `tools.discard(...)`）。若简报仍写「考虑用 continue_meeting_round
    再给一轮」，同时下方又写「必须收口、continue 已不可用」，则**同一份
    简报自相矛盾**，且建议的是一条**死指令**（模型调不到该工具）——
    正是本仓要根除的「提示与工具/状态不一致」形态。

    本测试是 P1-2 的守卫：把 `if incomplete and allow_continue:` 改回
    `if incomplete:` 时必须转红。
    """
    text = prompts.chair_briefing(
        meeting_title="final-round",
        topic_title="only-topic",
        round_index=MAX_ROUNDS,
        max_rounds=MAX_ROUNDS,
        speeches=[
            {
                "agent_id": "p-1",
                "role": "abstain",
                "content": "budget exhausted",
                "abstain_reason": ABSTAIN_BUDGET_EXHAUSTED,
            }
        ],
        concluded_results=[],
        allow_continue=False,
    )
    # ① 事实仍要报（不能因为不能加轮就隐瞒有人没说完）
    assert ABSTAIN_BUDGET_EXHAUSTED in text, text
    assert "未表态" in text, text
    assert "最后一轮" in text, text
    # ② 但**不得**再建议加轮（否则既是死指令又自相矛盾）
    assert "考虑用 continue_meeting_round" not in text, text
    assert "再给一轮" not in text, text
    # ③ 收口段仍须明确 continue 不可用
    assert "continue 已不可用" in text, text

