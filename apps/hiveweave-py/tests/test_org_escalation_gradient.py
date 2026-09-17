"""#16-③ 组织级升级（`distinct_hitters` 梯度）单元测试。

**病因**：`metadata.hit_count` 计的是**次数**不是**不同 agent 数**，且无论
撞几次只更新条目、从不通知 ⇒「N 人各撞一遍」在数据可见、行为无反应 ——
R7 = 50:17 / 51:11 就是后果。

**判据来源**：DSH 的 chain 是 per-agent、会话内、WeakMap（README 明写
"chains stay isolated per agent"）⇒ 它不做跨 agent 是因为**它的 agents 不
共享工作区与任务池**（边界，不是判断）。我们有 CEO→中层→叶子 + 共享项目与
任务池，所以"同一堵墙被 N 个不同 agent 各撞一遍"是真实可行动的组织级信号。

本文件守四件事：
① 精确命中阈值才发（越顶静默）；② 同档幂等（重启也成立）；
③ 计数维度是 distinct（不同 agent），不是次数；④ 集合有界但计数不丢。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from hiveweave.services import failure_signature as fs

_ERROR = "Error: Command blocked: [unattended mode] something long enough"


class _FakeSharedSpace:
    """内存版项目共享空间（与 test_signature_solution_backfill 同形）。"""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    async def get_project_memories(self, project_id: str) -> list[dict]:
        return [dict(r) for r in self.rows]

    async def save_memory(
        self,
        *,
        agent_id: str,
        project_id: str,
        scope: str,
        content: str,
        type: str = "fact",
        module_id: str | None = None,
        source_agent_id: str | None = None,
        metadata: dict | None = None,
        **_: object,
    ) -> str:
        for r in self.rows:
            if (
                r.get("agent_id") == agent_id
                and r.get("scope") == scope
                and r.get("module_id") == module_id
            ):
                r.update(
                    content=content,
                    type=type,
                    source_agent_id=source_agent_id,
                    metadata=metadata,
                )
                return r["id"]
        mid = f"mem-{len(self.rows) + 1}"
        self.rows.append(
            {
                "id": mid,
                "agent_id": agent_id,
                "project_id": project_id,
                "scope": scope,
                "module_id": module_id,
                "type": type,
                "content": content,
                "source_agent_id": source_agent_id,
                "metadata": metadata or {},
            }
        )
        return mid


@pytest.fixture
def space():
    fake = _FakeSharedSpace()
    with patch("hiveweave.services.memory.MemoryService", return_value=fake):
        yield fake


def _seed(space, *, tool: str = "bash", meta: dict | None = None) -> str:
    sig = fs.signature_of(_ERROR)
    assert sig
    space.rows.append(
        {
            "id": "mem-1",
            "agent_id": fs._SIGNATURE_WRITER,
            "scope": "project",
            "module_id": fs.make_module_id("proj", sig, tool),  # 批 C：module_id 三元组含 tool
            "type": "failure_signature",
            "content": (
                f"[失败签名] tool={tool} | {sig}\n"
                "根因提示: 见错误原文\n"
                "原文尾: 尾\n"
                "首个撞到的 Agent: agent-0"
            ),
            "source_agent_id": "agent-0",
            "metadata": meta or {},
        }
    )
    return sig


async def _hit(space, agent: str, *, tool: str = "bash") -> str:
    sig = _seed(space) if not space.rows else fs.signature_of(_ERROR)
    return await fs.note_distinct_hitter(
        project_id="proj",
        signature_key=sig or "",
        tool_name=tool,
        agent_id=agent,
    )


# ── ① 梯度形状：精确命中，越顶静默 ──────────────────


def test_thresholds_are_ascending():
    ts = fs.DISTINCT_HITTERS_THRESHOLDS
    assert list(ts) == sorted(ts)
    assert len(set(ts)) == len(ts)


def test_escalation_tier_only_fires_on_exact_hit():
    assert fs.escalation_tier(1) is None
    assert fs.escalation_tier(2) is None
    assert fs.escalation_tier(3) == 0
    assert fs.escalation_tier(4) is None  # 越顶静默
    assert fs.escalation_tier(5) == 1
    assert fs.escalation_tier(6) is None
    assert fs.escalation_tier(7) is None
    assert fs.escalation_tier(8) == 2
    assert fs.escalation_tier(9) is None  # 最高档之上不再发
    assert fs.escalation_tier(999) is None


def test_first_tier_does_not_name_anyone():
    """首档轻推**不点名** —— 点名会让叶子把提示读成"批评"进而不读。"""
    txt = fs.build_org_escalation_text(
        sig="sig", tool_name="bash", tier_idx=0, distinct_count=3
    )
    assert "3" in txt
    # 不该出现具体工具名（首档只说"有一堵墙被反复撞"）
    assert "bash" not in txt


def test_later_tiers_are_detailed():
    txt = fs.build_org_escalation_text(
        sig="sig", tool_name="bash", tier_idx=1, distinct_count=5
    )
    assert "bash" in txt
    assert "5" in txt


def test_top_tier_marked():
    txt = fs.build_org_escalation_text(
        sig="sig", tool_name="bash", tier_idx=2, distinct_count=8
    )
    assert "最高档" in txt


# ── ② 计数维度是 distinct（不是次数）──────────────


def test_merge_counts_distinct_agents_not_hits():
    meta: dict = {}
    hitters, n = fs.merge_distinct_hitters(meta, "a")
    assert (hitters, n) == (["a"], 1)
    # 同一 agent 再撞：集合不变、总数不变（这是与 hit_count 的关键差别）
    hitters, n = fs.merge_distinct_hitters({fs._HITTERS_KEY: hitters}, "a")
    assert (hitters, n) == (["a"], 1)
    hitters, n = fs.merge_distinct_hitters({fs._HITTERS_KEY: hitters}, "b")
    assert (hitters, n) == (["a", "b"], 2)


def test_hitter_set_bounded_but_count_keeps_going():
    """集合截断后**总数继续涨** —— 不许把"截断"读成"人变少了"。"""
    meta: dict = {}
    for i in range(fs._MAX_DISTINCT_HITTERS + 5):
        hitters, n = fs.merge_distinct_hitters(meta, f"agent-{i}")
        meta = {fs._HITTERS_KEY: hitters, fs._HITTERS_OVERFLOW_KEY: n - len(hitters)}
    assert len(hitters) == fs._MAX_DISTINCT_HITTERS
    assert n == fs._MAX_DISTINCT_HITTERS + 5


# ── ③ 端到端：第 3 个不同 agent 触发；同档幂等 ──────


@pytest.mark.asyncio
async def test_third_distinct_agent_triggers_first_tier(space):
    _seed(space)
    assert await _hit(space, "agent-1") == ""  # 第 1 人
    assert await _hit(space, "agent-2") == ""  # 第 2 人
    txt = await _hit(space, "agent-3")  # 第 3 人 → 首档
    assert txt and "组织级信号" in txt
    assert space.rows[0]["metadata"]["org_escalated_at_ms"] > 0
    assert space.rows[0]["metadata"]["distinct_hitters"] == [
        "agent-1",
        "agent-2",
        "agent-3",
    ]


@pytest.mark.asyncio
async def test_same_agent_repeat_never_escalates(space):
    """**同一个 agent 撞 10 次**不触发组织级信号 —— 计数维度是 distinct。

    这是与 `hit_count` 的核心分野：旧口径下"一个人撞 3 次"会被误判成
    "团队撞了 3 次"，于是升级链指向了错的病因。
    """
    _seed(space)
    for _ in range(10):
        assert await _hit(space, "agent-1") == ""
    assert "org_escalated_tiers" not in space.rows[0]["metadata"]


@pytest.mark.asyncio
async def test_tier_is_idempotent(space):
    """同档只发一次（即使继续来新 agent）。"""
    _seed(space)
    for a in ("a1", "a2"):
        await _hit(space, a)
    assert await _hit(space, "a3") != ""  # 首档


@pytest.mark.asyncio
async def test_escalation_state_survives_restart(space):
    """幂等状态在 **metadata** 里，不在内存 ⇒ 进程重启后不重发。

    若把"已发过"记在进程内集合，重启就会把所有档重发一遍 —— 提示的
    信噪比正是 R7 恶化项要修的东西。
    """
    _seed(space)
    for a in ("a1", "a2"):
        await _hit(space, a)
    assert await _hit(space, "a3") != ""
    tiers_after = list(space.rows[0]["metadata"][fs._ORG_ESCALATED_TIERS_KEY])
    # 新 agent 但不命中下一档（4 人）→ 不发；已发档位不变
    assert await _hit(space, "a4") == ""
    assert (
        space.rows[0]["metadata"][fs._ORG_ESCALATED_TIERS_KEY] == tiers_after
    )


@pytest.mark.asyncio
async def test_fifth_and_eighth_agent_fire_later_tiers(space):
    _seed(space)
    fired: list[tuple[int, str]] = []
    for i in range(1, 9):
        txt = await _hit(space, f"agent-{i}")
        if txt:
            fired.append((i, txt))
    assert [i for i, _ in fired] == [3, 5, 8]
    assert "最高档" in fired[-1][1]


@pytest.mark.asyncio
async def test_no_entry_no_escalation(space):
    """条目不存在 → 不造条目、不发信号（升级不负责建档）。"""
    assert (
        await fs.note_distinct_hitter(
            project_id="proj",
            signature_key=fs.signature_of(_ERROR) or "",
            tool_name="bash",
            agent_id="a1",
        )
        == ""
    )
    assert space.rows == []


@pytest.mark.asyncio
async def test_wrong_tool_entry_not_counted(space):
    """同签名、异工具 ⇒ 不并入（与 #11-(c) 的定位纪律一致）。"""
    _seed(space, tool="read_file")
    assert await _hit(space, "a1", tool="bash") == ""
    assert fs._HITTERS_KEY not in space.rows[0]["metadata"]


@pytest.mark.asyncio
async def test_escalation_does_not_touch_content(space):
    """升级只改 metadata，**不改条目正文**（它是通知，不是内容变更）。"""
    _seed(space)
    before = space.rows[0]["content"]
    for a in ("a1", "a2", "a3"):
        await _hit(space, a)
    assert space.rows[0]["content"] == before


@pytest.mark.asyncio
async def test_escalation_failure_is_swallowed(space):
    """best-effort：内部异常不许抛到工具执行路径上。"""
    with patch(
        "hiveweave.services.memory.MemoryService",
        side_effect=RuntimeError("boom"),
    ):
        assert (
            await fs.note_distinct_hitter(
                project_id="proj",
                signature_key=fs.signature_of(_ERROR) or "",
                tool_name="bash",
                agent_id="a1",
            )
            == ""
        )


def test_missing_args_return_empty():
    """空 project / 空签名 / 空 agent → 不发（防止造垃圾条目）。"""
    import asyncio

    assert (
        asyncio.run(
            fs.note_distinct_hitter(
                project_id=None, signature_key="s", tool_name="bash", agent_id="a"
            )
        )
        == ""
    )
    assert (
        asyncio.run(
            fs.note_distinct_hitter(
                project_id="p", signature_key="", tool_name="bash", agent_id="a"
            )
        )
        == ""
    )
    assert (
        asyncio.run(
            fs.note_distinct_hitter(
                project_id="p", signature_key="s", tool_name="bash", agent_id=""
            )
        )
        == ""
    )
