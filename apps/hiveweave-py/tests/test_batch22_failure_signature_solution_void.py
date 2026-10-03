"""批22 · P1-10：失败签名「只记错不记解」闭环收口回归。

病灶（三轮在册）：R7 s21=53 条签名 —— 签名条目只有错误形态，读侧注入
（agents/agent.py 5a-2 → recent_failure_memories_hint）只能告诉「你将撞什么」，
不能告诉「已知怎么解」；且被证实为环境噪声的签名（qoder 报告的「平台自管
提示文本」类）无退场通道，永久占坑。

本批修法（failure_signature.py）：
  a) void 事实位（metadata.voided_at / voided_reason；签名存储 = memories 行，
     无独立表 —— 「加列」按本模块 metadata 字段扩展模式落位）；
  b) 公开 API record_failure_solution(module_id, solution) + lessons 归档时
     按签名（全签名 / 全项目唯一前缀）自动挂接（attach_lesson_to_signatures）；
  c) 读侧注入文案带「已知解法:…」；voided 条目不再注入/不再广播；
  d) void 通道 void_failure_signature（幂等、rehit 不复活）。

「先失败后同工具成功 → 参数差异沉淀为解法」的写侧启发（executor
pending-success 回填）批42 前已落位（_f10_pending_success_backfill），本批
验证其与读侧注入的端到端闭环。
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.services import failure_signature as fs
from hiveweave.tools import executor as exec_mod

_ERROR = "Error: Command blocked: [unattended mode] something long enough"


class _FakeSharedSpace:
    """内存版项目共享空间（照 test_signature_solution_backfill 同款）。"""

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


@pytest.fixture
def clear_pending():
    exec_mod._PENDING_SOLUTIONS.clear()
    yield
    exec_mod._PENDING_SOLUTIONS.clear()


def _entry_content(sig: str, source: str = "agent-A") -> str:
    return (
        f"[失败签名] tool=bash | {sig}\n"
        f"根因提示: 见错误原文\n"
        f"原文尾: {sig[-40:]}\n"
        f"首个撞到的 Agent: {source}"
    )


def _seed_entry(
    space: _FakeSharedSpace,
    sig: str,
    *,
    tool: str = "bash",
    solution_line: str | None = None,
    metadata: dict | None = None,
) -> dict:
    content = _entry_content(sig)
    meta = {"signature": sig, "tool_name": tool, **(metadata or {})}
    if solution_line:
        content += f"\n{solution_line}"
        meta.setdefault("solution_status", fs.SOLUTION_STATUS_VERIFIED)
    space.rows.append(
        {
            "id": f"mem-{len(space.rows) + 1}",
            "agent_id": fs._SIGNATURE_WRITER,
            "scope": "project",
            "module_id": fs.make_module_id("proj", sig, tool),
            "type": "failure_signature",
            "content": content,
            "source_agent_id": "agent-A",
            "metadata": meta,
        }
    )
    return space.rows[-1]


async def _hint_for_failed_tool(
    tool: str = "bash",
    *,
    project_id: str = "proj",
    agent_id: str = "agent-B",
) -> str:
    """hint 读侧：桩掉 run_steps 查询（只回一个最近失败工具）后取注入文案。"""

    async def _fake_query(aid, sql, params=None, **kw):
        return [{"tool_name": tool, "last_at": int(time.time() * 1000)}]

    with patch("hiveweave.db.project.query", _fake_query):
        return await fs.recent_failure_memories_hint(agent_id, project_id)


# ── ① 失败 → 同工具成功 ⇒ 签名带解法 且 读侧注入含之（端到端闭环）──


async def test_fail_then_success_backfills_solution_and_hint_reports_it(
    space, clear_pending
):
    sig = fs.signature_of(_ERROR)
    assert sig
    with patch(
        "hiveweave.db.meta.get_agent_project_id", AsyncMock(return_value="proj")
    ), patch(
        "hiveweave.tools.executor.deliver_notice", AsyncMock(return_value=True)
    ):
        # 1) 失败：写签名条目 + 记 pending（调用身份键）
        await exec_mod._f10_result_hooks(
            {"success": False, "output": "", "error": _ERROR},
            "bash",
            {"command": "ls --unix-only"},
            "agent-A",
        )
        assert len(space.rows) == 1
        # 2) 同 agent 同工具**同调用**随后一次成功 → 回填成功参数摘要
        await exec_mod._f10_pending_success_backfill(
            {"success": True, "output": "file.txt", "error": None},
            "bash",
            {"command": "ls --unix-only"},
            "agent-A",
        )
    content = space.rows[0]["content"]
    assert "同参重试:" in content, "签名条目应带解法（回声形态）"
    assert space.rows[0]["metadata"]["solution_status"] == (
        fs.SOLUTION_STATUS_RETRIED_OK
    )
    # 3) 读侧注入：带「已知怎么解」的信息（回声保持中性述实，不冒充解法）
    hint = await _hint_for_failed_tool("bash")
    assert hint.startswith("[团队失败记忆]")
    assert "tool=bash" in hint
    assert "同参重试曾成功" in hint


async def test_verified_solution_shows_known_solution_label_in_hint(space):
    """已验证解法 ⇒ 注入文案带「已知解法:」标签（P1-10 c）。"""
    sig = fs.signature_of(_ERROR)
    _seed_entry(space, sig)
    ok = await fs.record_failure_solution(
        "proj", fs.make_module_id("proj", sig, "bash"), "改用 pwsh 管道写法"
    )
    assert ok is True
    assert "已验证解法: 改用 pwsh 管道写法" in space.rows[0]["content"]
    hint = await _hint_for_failed_tool("bash")
    assert "已知解法: 改用 pwsh 管道写法" in hint


# ── ② voided 签名不再注入 / 不再广播 ─────────────────────────────


async def test_voided_signature_not_injected_and_not_broadcast(space):
    sig = fs.signature_of(_ERROR)
    _seed_entry(space, sig, solution_line="已验证解法: 改用 pwsh 管道写法")
    # 前置：void 之前照常注入 + 广播
    assert "改用 pwsh 管道写法" in (await _hint_for_failed_tool("bash"))
    notice = await fs.known_signature_notice(
        "proj", _ERROR, agent_id="agent-B", tool_name="bash"
    )
    assert notice is not None

    ok = await fs.void_failure_signature(
        "proj", sig, "bash", reason="平台自管提示文本，非 agent 可行动失败"
    )
    assert ok is True
    meta = space.rows[0]["metadata"]
    assert int(meta.get("voided_at") or 0) > 0
    assert "平台自管提示文本" in str(meta.get("voided_reason") or "")
    # 读侧：hint 不注入、notice 不广播
    assert await _hint_for_failed_tool("bash") == ""
    assert (
        await fs.known_signature_notice(
            "proj", _ERROR, agent_id="agent-B", tool_name="bash"
        )
        is None
    )


async def test_void_is_idempotent_and_rejects_unknown_signature(space):
    sig = fs.signature_of(_ERROR)
    _seed_entry(space, sig)
    assert await fs.void_failure_signature("proj", sig, "bash") is True
    first_voided_at = space.rows[0]["metadata"]["voided_at"]
    # 幂等：重复 void 原样 True，不覆盖时间戳
    assert await fs.void_failure_signature("proj", sig, "bash") is True
    assert space.rows[0]["metadata"]["voided_at"] == first_voided_at
    # 未知签名 → False
    assert (
        await fs.void_failure_signature("proj", "no-such-signature-anywhere", "bash")
        is False
    )


async def test_rehit_does_not_resurrect_voided_signature(space, clear_pending):
    """同签名复撞不得把人工作废的条目复活（void 跨 rehit 继承）。"""
    sig = fs.signature_of(_ERROR)
    _seed_entry(space, sig, metadata={"voided_at": 1, "voided_reason": "噪声"})
    with patch(
        "hiveweave.db.meta.get_agent_project_id", AsyncMock(return_value="proj")
    ):
        await exec_mod._f10_result_hooks(
            {"success": False, "output": "", "error": _ERROR},
            "bash",
            {},
            "agent-B",
        )
    meta = space.rows[0]["metadata"]
    assert int(meta.get("voided_at") or 0) > 0, "rehit 后 void 事实位必须保留"
    assert meta.get("hit_count") == 2, "rehit 计数照常（void 只挡读侧）"
    assert await _hint_for_failed_tool("bash") == ""


# ── ③ 无解法签名行为不变（P2-1 判据回归）────────────────────────


async def test_no_solution_signature_behavior_unchanged(space):
    sig = fs.signature_of(_ERROR)
    _seed_entry(space, sig)
    assert not fs._is_voided(space.rows[0]["metadata"])
    # 纯镜子条目（无解法）：不注入、不广播 —— 与批22 前一致
    assert await _hint_for_failed_tool("bash") == ""
    assert (
        await fs.known_signature_notice(
            "proj", _ERROR, agent_id="agent-B", tool_name="bash"
        )
        is None
    )


# ── ④ lessons 关联挂接 ──────────────────────────────────────────


async def test_lesson_attach_by_full_signature(space):
    sig = fs.signature_of(_ERROR)
    _seed_entry(space, sig)
    n = await fs.attach_lesson_to_signatures(
        "proj",
        f"撞到失败签名「{sig}」，后发现是 unix-only 方言问题",
        root_cause="pwsh 受限 shell 不认 unix 管道",
        fix="改用 pwsh 的 Select-Object 管道写法",
    )
    assert n == 1
    assert "已验证解法: 改用 pwsh 的 Select-Object 管道写法" in space.rows[0]["content"]


async def test_lesson_attach_by_unique_prefix(space):
    sig = fs.signature_of(_ERROR)
    assert len(sig) > 48
    _seed_entry(space, sig)
    # lesson 只引用了前 48 字符（截断引用形态）
    n = await fs.attach_lesson_to_signatures(
        "proj", f"见签名前缀 {sig[:48]} 的坑", fix="改用 pwsh 管道写法"
    )
    assert n == 1
    assert "已验证解法: 改用 pwsh 管道写法" in space.rows[0]["content"]


async def test_lesson_attach_ambiguous_prefix_is_skipped(space):
    """前缀塌缩（两条目共享前 48 字符）⇒ 不猜，全不挂（错解比无解更贵）。"""
    sig_a = fs.signature_of(_ERROR)
    sig_b = sig_a + " with a different tail"
    _seed_entry(space, sig_a)
    _seed_entry(space, sig_b, tool="pwsh")
    n = await fs.attach_lesson_to_signatures(
        "proj", f"那个 {sig_a[:48]} 的坑", fix="某个解法文本"
    )
    assert n == 0
    for row in space.rows:
        assert "已验证解法:" not in row["content"]


async def test_lesson_attach_requires_fix(space):
    """root_cause 是「为什么」不是「怎么解」—— 无 fix 不挂。"""
    sig = fs.signature_of(_ERROR)
    _seed_entry(space, sig)
    n = await fs.attach_lesson_to_signatures(
        "proj", f"关于 {sig}", root_cause="方言不兼容", fix=""
    )
    assert n == 0
    assert "已验证解法:" not in space.rows[0]["content"]


async def test_lesson_attach_skips_voided_entry(space):
    sig = fs.signature_of(_ERROR)
    _seed_entry(space, sig, metadata={"voided_at": 1})
    n = await fs.attach_lesson_to_signatures(
        "proj", f"关于 {sig}", fix="改用 pwsh 管道写法"
    )
    assert n == 0
    assert "已验证解法:" not in space.rows[0]["content"]


async def test_lesson_attach_idempotent_when_verified_line_exists(space):
    sig = fs.signature_of(_ERROR)
    _seed_entry(space, sig, solution_line="已验证解法: 已有权威解法保持原样")
    n = await fs.attach_lesson_to_signatures(
        "proj", f"关于 {sig}", fix="另一个解法不应覆盖"
    )
    assert n == 1  # backfill 幂等成功
    assert "已有权威解法保持原样" in space.rows[0]["content"]
    assert "另一个解法不应覆盖" not in space.rows[0]["content"]


async def test_record_failure_solution_rejects_unknown_or_voided_entry(space):
    sig = fs.signature_of(_ERROR)
    _seed_entry(space, sig)
    # 未知 module_id → False
    assert (
        await fs.record_failure_solution("proj", "mem-no-such-id", "某个解法文本")
        is False
    )
    # voided 条目 → 拒绝回填（作废即冻结）
    await fs.void_failure_signature("proj", sig, "bash")
    assert (
        await fs.record_failure_solution(
            "proj", fs.make_module_id("proj", sig, "bash"), "某个解法文本"
        )
        is False
    )
    assert "已验证解法:" not in space.rows[0]["content"]


# ── 集成：LessonService.save_lesson 自动挂接（真实 per-project DB）───

PROJECT_ID = "b22-proj-0001"
AGENT_ID = "b22-exec-0001"


@pytest.fixture(autouse=True)
async def _real_meta(tmp_path, monkeypatch):
    from hiveweave.db import meta as meta_db

    monkeypatch.setattr(
        meta_db.app_settings,
        "meta_db_path",
        str(tmp_path / "meta" / "hiveweave.db"),
    )
    await meta_db.close_meta_db()
    await meta_db.init_meta_db()
    yield
    await meta_db.close_meta_db()


@pytest.fixture
async def seeded_project(tmp_path):
    from hiveweave.db import meta as meta_db
    from hiveweave.db import project as project_db

    ws = str(tmp_path / "ws")
    now = int(time.time() * 1000)
    await meta_db.execute(
        "INSERT INTO projects (id, name, workspace_path, created_at) "
        "VALUES (?, ?, ?, ?)",
        [PROJECT_ID, "b22-fs-test", ws, now],
    )
    conn = await project_db.ensure_project_db(ws)
    cur = await conn.execute(
        "INSERT INTO agents (id, short_id, project_id, name, role, status, "
        "model_id, created_at) VALUES (?, 'B22', ?, 'B22Exec', 'executor', "
        "'active', 'gpt-x', ?)",
        [AGENT_ID, PROJECT_ID, now],
    )
    await cur.close()
    await conn.commit()
    yield conn


async def test_save_lesson_auto_attaches_to_signature(seeded_project):
    """lessons 归档通道端到端：save_lesson 内容含签名 ⇒ 条目自动获得解法行。"""
    from hiveweave.services.lessons import LessonService
    from hiveweave.services.memory import MemoryService

    error = (
        "Error: Command blocked: [unattended mode] real project failure "
        "signature wording for lesson attach integration"
    )
    rec = await fs.record_failure_signature(
        project_id=PROJECT_ID,
        agent_id=AGENT_ID,
        tool_name="bash",
        error=error,
        attribution="",
    )
    assert rec["written"] and rec["sig"]
    sig = rec["sig"]

    mem_id = await LessonService().save_lesson(
        PROJECT_ID,
        AGENT_ID,
        lesson=f"撞到失败签名「{sig}」—— unix-only 管道在受限 shell 不认",
        root_cause="pwsh 受限 shell 不认 unix 管道",
        fix="改用 pwsh 的 Select-Object 管道写法",
    )
    assert mem_id, "lesson 应过质量门归档"

    mems = await MemoryService().get_project_memories(PROJECT_ID)
    entry = next(m for m in mems if m.get("type") == "failure_signature")
    assert "已验证解法: 改用 pwsh 的 Select-Object 管道写法" in entry["content"]
    assert entry["metadata"]["solution_status"] == fs.SOLUTION_STATUS_VERIFIED
