"""TEST_DSH_64 #2/#5/#9（2026-09-19）修复钉子：签名定位精确化 + spawn 示例 + 横幅三事实位。

背景（报告 #2 P1 错解广播，两轮深挖定谳）：``sig[:48]`` 前缀子串探测曾在
record 预存扫描 / hint 命中 / backfill 定位 / hitter 计数四位点使用 —— 两条
不同错误共享前 48 字符（截断+归一造出的塌缩）时，A 的解法会广播给 B、回填
会「落错行」。终审修法：
1. record 预存扫描 / hint 命中 → metadata ``(signature, tool_name)`` 元组
   精确等值；hitter 计数**保留前缀**（组织升级要「同族墙」语义，防 R7 倒退）。
2. backfill → module_id 精确等值直定位（pending 携带 / 三元组重算，同源）。
3. 同参重试成功 = 构造性事实 → ``retried_ok`` 回声，不冒充「已验证解法」。
4. hint 空态显式化（不再 None / 不再指路读池子）。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.services import failure_signature as fs
from hiveweave.services import offturn as ot
from hiveweave.tools import executor as exec_mod

_ERROR_A = (
    "Error: deployment failed at stage assemble with target directory "
    "build-out for module alpha-worker"
)
_ERROR_B = (
    "Error: deployment failed at stage assemble with target directory "
    "build-out for module beta-worker"
)


class _FakeSharedSpace:
    """内存版项目共享空间：模拟 MemoryService 的读 + 固定写入方 upsert。"""

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
    with patch(
        "hiveweave.services.memory.MemoryService", return_value=fake
    ):
        yield fake


def _seed_row(
    space: _FakeSharedSpace,
    sig: str,
    tool: str,
    *,
    meta: dict | None = None,
    extra_line: str = "",
    module_id: str | None = None,
    fl_tool: str | None = None,
) -> None:
    """按写侧真实形态种一条签名条目（metadata 元组齐全）。"""
    content = (
        f"[失败签名] tool={fl_tool or tool} | {sig}\n"
        "根因提示: 见错误原文\n"
        f"原文尾: {sig[-30:]}\n"
        "首个撞到的 Agent: agent-0"
    )
    if extra_line:
        content = f"{content}\n{extra_line}"
    space.rows.append(
        {
            "id": f"mem-{len(space.rows) + 1}",
            "agent_id": fs._SIGNATURE_WRITER,
            "scope": "project",
            "module_id": module_id or fs.make_module_id("proj", sig, tool),
            "type": "failure_signature",
            "content": content,
            "source_agent_id": "agent-0",
            "metadata": {
                "signature": sig,
                "tool_name": tool,
                "solution_status": fs.SOLUTION_STATUS_NONE,
                **(meta or {}),
            },
        }
    )


# ── ① sig[:48] 塌缩：record/hint 互不串 ──────────────────────────────


def _collapsed_pair() -> tuple[str, str]:
    """构造共享前 48 字符但全文不同的两条签名（塌缩现场）。"""
    sig_a = fs.signature_of(_ERROR_A)
    sig_b = fs.signature_of(_ERROR_B)
    assert sig_a and sig_b
    assert sig_a != sig_b, "测试前置：两条错误必须是不同签名"
    assert sig_a[:48] == sig_b[:48], (
        "测试前置：两条签名必须共享前 48 字符（前缀塌缩现场）"
    )
    return sig_a, sig_b


@pytest.mark.asyncio
async def test_prefix_collapse_does_not_merge_record(space):
    """①a 塌缩对：第二条 record **不得**被判成 rehit（旧前缀判据会误并）。"""
    sig_a, sig_b = _collapsed_pair()
    rec_a = await fs.record_failure_signature(
        project_id="proj", agent_id="agent-A", tool_name="bash",
        error=_ERROR_A, attribution="",
    )
    assert rec_a["written"] and rec_a["module_id"]
    rec_b = await fs.record_failure_signature(
        project_id="proj", agent_id="agent-B", tool_name="bash",
        error=_ERROR_B, attribution="",
    )
    # 旧判据 sig_b[:48] in fl_a ⇒ preexisting=True（错并一条）；元组等值 ⇒ False
    assert rec_b["preexisting"] is False
    assert len(space.rows) == 2  # 两条独立条目并存


@pytest.mark.asyncio
async def test_prefix_collapse_hint_does_not_carry_foreign_solution(space):
    """①b 塌缩对：A 条目的已验证解法**不得**串投给撞 B 错误的人。"""
    sig_a, sig_b = _collapsed_pair()
    _seed_row(
        space, sig_a, "bash",
        extra_line="已验证解法: 删掉 alpha-worker 的旧构建目录后重跑",
        meta={"solution_status": fs.SOLUTION_STATUS_VERIFIED},
    )
    _seed_row(space, sig_b, "bash")  # B 自己的条目（真实流程里 B 首撞时已写入）
    hint_b = await fs.known_signature_hint(
        "proj", _ERROR_B, agent_id="agent-B", tool_name="bash"
    )
    assert hint_b is not None and "[shared fix]" in hint_b
    assert "alpha-worker 的旧构建目录" not in hint_b  # 别条的解法不串投
    assert "暂无已验证解法" in hint_b  # B 条目自己的空态
    # 对照组：撞 A 错误的人**应该**拿到解法（定位没被误伤）
    hint_a = await fs.known_signature_hint(
        "proj", _ERROR_A, agent_id="agent-C", tool_name="bash"
    )
    assert hint_a and "alpha-worker 的旧构建目录" in hint_a


def test_hitter_count_keeps_prefix_family_semantics(space):
    """①c hitter 计数**保留**前缀「同族墙」语义（深挖定谳，防 R7 倒退）。

    note_distinct_hitter 不在收口范围：塌缩对里撞 sig_b 仍计入 sig_a 条目的
    distinct_hitters（组织升级要的是「同一堵墙被 N 人撞」，同族即同墙）。
    """
    sig_a, sig_b = _collapsed_pair()
    _seed_row(space, sig_a, "bash")

    async def _hit():
        return await fs.note_distinct_hitter(
            project_id="proj",
            signature_key=sig_b,  # 塌缩对里的另一条签名
            tool_name="bash",
            agent_id="agent-Z",
        )

    result = asyncio.run(_hit())
    assert result == ""  # 首撞未达档位，无升级正文
    hitters = space.rows[0]["metadata"].get("distinct_hitters")
    assert hitters == ["agent-Z"], (
        "hitter 计数必须仍按前缀族语义计入塌缩对条目"
    )


# ── ② backfill 按 module_id 落到自己的行 ─────────────────────────────


@pytest.mark.asyncio
async def test_backfill_lands_by_module_id_not_prefix_twin(space):
    """② 塌缩对 + pending 携带 module_id：回填只落**自己的**行。

    旧实现按 ``sig[:48]`` 前缀扫描 + 最老行优先 ⇒ 会落进先写入的塌缩兄弟行
    （「落错行」根因）。新实现按 module_id 精确等值。
    """
    sig_a, sig_b = _collapsed_pair()
    mid_a = fs.make_module_id("proj", sig_a, "bash")
    mid_b = fs.make_module_id("proj", sig_b, "bash")
    _seed_row(space, sig_a, "bash", module_id=mid_a)  # 先写（旧逻辑会选中它）
    _seed_row(space, sig_b, "bash", module_id=mid_b)

    # 携带 module_id（pending 形态）
    ok = await fs.backfill_solution(
        sig_b, "bash", "升级 beta-worker 依赖后重跑", project_id="proj",
        module_id=mid_b,
    )
    assert ok is True
    by_id = {r["id"]: r for r in space.rows}
    target = next(r for r in space.rows if r["module_id"] == mid_b)
    twin = next(r for r in space.rows if r["module_id"] == mid_a)
    assert "已验证解法:" in target["content"]
    assert "已验证解法:" not in twin["content"]  # 兄弟行不被污染

    # 不带 module_id：按 (project, tool, sig) 重算 module_id，同样精确落行
    ok2 = await fs.backfill_solution(
        sig_a, "bash", "清空 alpha 构建缓存后重跑", project_id="proj"
    )
    assert ok2 is True
    assert "已验证解法:" in twin["content"]

    # 携带的 module_id 与 (sig, tool) 不匹配时**不**跨行乱落：
    # 签名对不上 A 的行 → 该 module_id 在池中无行 → 拒绝
    ok3 = await fs.backfill_solution(
        sig_a, "bash", "绝不落到任何行", project_id="proj", module_id="mod-ghost"
    )
    assert ok3 is False


@pytest.mark.asyncio
async def test_record_rehit_requires_full_metadata_tuple(space):
    """①d 旧行缺 metadata 字段（signature/tool_name）⇒ 视为新条目（搁浅语义）。"""
    sig = fs.signature_of(_ERROR_A)
    space.rows.append(
        {
            "id": "mem-old",
            "agent_id": fs._SIGNATURE_WRITER,
            "scope": "project",
            "module_id": "legacy-mid",
            "type": "failure_signature",
            "content": (
                f"[失败签名] tool=bash | {sig}\n"
                "根因提示: 见错误原文\n原文尾: x\n首个撞到的 Agent: agent-0"
            ),
            "source_agent_id": "agent-0",
            "metadata": {"source_agent_id": "agent-0"},  # 缺 signature/tool_name
        }
    )
    rec = await fs.record_failure_signature(
        project_id="proj", agent_id="agent-B", tool_name="bash",
        error=_ERROR_A, attribution="",
    )
    assert rec["preexisting"] is False  # 不匹配旧行
    assert len(space.rows) == 2  # 另起新行（批 C 拍板接受的搁浅）


@pytest.mark.asyncio
async def test_carry_gate_applies_to_entry_first_line(space):
    """⑤ 携带门施于条目首行 fl：metadata 对但 fl tool= 脏 ⇒ 不算 rehit。"""
    sig = fs.signature_of(_ERROR_A)
    _seed_row(space, sig, "bash", fl_tool="read_file")  # fl 与 metadata 矛盾
    rec = await fs.record_failure_signature(
        project_id="proj", agent_id="agent-B", tool_name="bash",
        error=_ERROR_A, attribution="",
    )
    assert rec["preexisting"] is False


# ── ③ retried_ok 不被当 verified 广播 ────────────────────────────────


@pytest.mark.asyncio
async def test_retried_ok_echo_is_not_broadcast_as_verified(space):
    """③ 同参回声：状态 retried_ok、写「同参重试:」行，hint 不携带解法。"""
    sig = fs.signature_of(_ERROR_A)
    mid = fs.make_module_id("proj", sig, "bash")
    _seed_row(space, sig, "bash", module_id=mid)
    ok = await fs.backfill_solution(
        sig, "bash",
        "此前有 Agent 以完全相同参数重试成功（说明环境/代码已变化）",
        project_id="proj",
        module_id=mid,
        status=fs.SOLUTION_STATUS_RETRIED_OK,
    )
    assert ok is True
    row = space.rows[0]
    assert "同参重试:" in row["content"]
    assert "已验证解法:" not in row["content"]  # 机检不变式：回声不占解法行
    assert row["metadata"]["solution_status"] == fs.SOLUTION_STATUS_RETRIED_OK

    hint = await fs.known_signature_hint(
        "proj", _ERROR_A, agent_id="agent-B", tool_name="bash"
    )
    assert hint and "同参重试曾成功" in hint
    assert "已验证解法:" not in hint  # 不冒充已验证解法广播

    # 修状态位路径**不得**把回声条目抬成 verified
    assert await fs.repair_solution_status("proj") == 0
    assert space.rows[0]["metadata"]["solution_status"] == (
        fs.SOLUTION_STATUS_RETRIED_OK
    )

    # 之后真解法到来可升级：写「已验证解法:」行 + 状态翻 verified，回声保留
    ok2 = await fs.backfill_solution(
        sig, "bash", "改用 pwsh -Command 重跑", project_id="proj", module_id=mid,
    )
    assert ok2 is True
    row = space.rows[0]
    assert "已验证解法:" in row["content"] and "同参重试:" in row["content"]
    assert row["metadata"]["solution_status"] == fs.SOLUTION_STATUS_VERIFIED
    hint2 = await fs.known_signature_hint(
        "proj", _ERROR_A, agent_id="agent-C", tool_name="bash"
    )
    assert hint2 and "改用 pwsh -Command 重跑" in hint2

    # 回声幂等：已有回声行时再回填同状态不重复追加
    echo_count_before = row["content"].count("同参重试:")
    ok3 = await fs.backfill_solution(
        sig, "bash", "又一条同参回声也不该重复追加进条目", project_id="proj",
        module_id=mid,
        status=fs.SOLUTION_STATUS_RETRIED_OK,
    )
    assert ok3 is True
    assert space.rows[0]["content"].count("同参重试:") == echo_count_before


@pytest.mark.asyncio
async def test_verified_solution_not_downgraded_by_late_echo(space):
    """③b 已有真解法的条目：迟到的同参回声不得覆盖/降级。"""
    sig = fs.signature_of(_ERROR_A)
    mid = fs.make_module_id("proj", sig, "bash")
    _seed_row(
        space, sig, "bash", module_id=mid,
        extra_line="已验证解法: 改用 pwsh 写法",
        meta={"solution_status": fs.SOLUTION_STATUS_VERIFIED},
    )
    ok = await fs.backfill_solution(
        sig, "bash", "同参回声不该进门", project_id="proj", module_id=mid,
        status=fs.SOLUTION_STATUS_RETRIED_OK,
    )
    assert ok is True  # 幂等语义（条目已有更权威的解）
    row = space.rows[0]
    assert "同参回声不该进门" not in row["content"]
    assert row["metadata"]["solution_status"] == fs.SOLUTION_STATUS_VERIFIED


# ── ④ 空态文案 ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hint_returns_explicit_empty_state_not_none(space):
    """④ 命中条目但无 verified 解法 ⇒ 显式空态（不再 None/不再指路读池子）。"""
    sig = fs.signature_of(_ERROR_A)
    _seed_row(space, sig, "bash")  # status=none，纯镜子条目
    hint = await fs.known_signature_hint(
        "proj", _ERROR_A, agent_id="agent-B", tool_name="bash"
    )
    assert hint is not None, "空态必须显式返回，不得 None"
    assert "暂无已验证解法" in hint
    assert "先读它" not in hint  # 不发不可执行指令


@pytest.mark.asyncio
async def test_hint_requires_tool_name_and_exact_tool(space):
    """⑤b hint 二元组门：缺工具名不广播；异工具行不串。"""
    sig = fs.signature_of(_ERROR_A)
    _seed_row(space, sig, "bash")
    # 缺 tool_name ⇒ 无法构成条目身份 ⇒ 不广播（54 轮串投缺陷的倒车禁门）
    assert await fs.known_signature_hint("proj", _ERROR_A, agent_id="agent-B") is None
    # 异工具：read_file 撞同签名文本 ⇒ bash 行不投给它
    assert await fs.known_signature_hint(
        "proj", _ERROR_A, agent_id="agent-B", tool_name="read_file"
    ) is None


# ── ⑥ spawn description 首行示例（双份不漂移）────────────────────────


def test_spawn_subagent_type_description_leads_with_example():
    """⑥ subagent_type 描述首行 = 字面示例；两份 schema 不漂移；不加默认值。"""
    from hiveweave.tools.executor import TOOL_PARAM_SCHEMAS
    from hiveweave.tools.subagent import SpawnSubagentParams

    d_field = SpawnSubagentParams.model_fields["subagent_type"].description or ""
    d_schema = (
        TOOL_PARAM_SCHEMAS["spawn_subagent"]["properties"]["subagent_type"]
        .get("description")
        or ""
    )
    assert d_field == d_schema, "两份 subagent_type description 漂移了"
    assert d_field.startswith("必填"), "首行必须是必填示例（pydantic 后的体内文案对缺参是死代码）"
    assert "subagent_type='write'" in d_field
    assert "'readonly'" in d_field and "'audit'" in d_field
    assert '{"subagent_type": "write", "prompt": "..."}' in d_field
    assert "REQUIRED (no default)" not in d_field  # 旧文案已替换
    # 有意不加默认值：缺参案例同样缺 prompt，default 救不了（终审拍板）
    assert SpawnSubagentParams.model_fields["subagent_type"].is_required()


# ── ⑦ DONE_TRUNCATED 横幅查重 + 三事实位 ─────────────────────────────


def test_truncated_banner_dedup_and_verify_pointer():
    """⑦ 横幅追加前查重（正文已含标记就不再加）；横幅自带验货指路。"""
    from hiveweave.tools.subagent import (
        _SUBAGENT_TRUNCATED_MARKER,
        _append_truncated_banner,
    )

    once = _append_truncated_banner("partial output")
    assert once.count(_SUBAGENT_TRUNCATED_MARKER) == 1
    assert "git_worktree_status" in once and "read_file" in once
    twice = _append_truncated_banner(once)  # _run_subagent 已加过、_work 再来
    assert twice == once, "横幅查重失效（会出现横幅双份回执）"


def test_offturn_truncated_envelope_declares_complete_body():
    """⑦ 信封侧：正文未截断时显式声明「正文即完整输出」+ 验货指路。"""
    prefix = ot.OFFTURN_STATE.SUBAGENT_DONE_TRUNCATED.prefix
    body = ot._format_body(prefix, "job-1", "hello", "/ws", "agent-1", "subagent")
    assert body.startswith(f"{prefix} job=job-1")
    assert "完整输出" in body
    assert "git_worktree_status" in body
    # 非 TRUNCATED 终态（DONE）不附加该声明
    done_body = ot._format_body(
        ot.OFFTURN_STATE.SUBAGENT_DONE.prefix, "job-1", "hello", "/ws",
        "agent-1", "subagent",
    )
    assert "完整输出" not in done_body


def test_offturn_overflow_envelope_has_spill_path_and_verify_pointer():
    """⑦ 信封侧：溢出时附 spill 路径 + 验货指路（第二事实位）。"""
    prefix = ot.OFFTURN_STATE.SUBAGENT_DONE_TRUNCATED.prefix
    big = "x" * (ot._INBOX_CHARS + 100)
    with patch(
        "hiveweave.tools.executor.ToolExecutor._save_tool_output_file",
        return_value="/tmp/spill/path.txt",
    ):
        body = ot._format_body(prefix, "job-1", big, "/ws", "agent-1", "subagent")
    assert "…(truncated)" in body
    assert "/tmp/spill/path.txt" in body
    assert "git_worktree_status" in body
    # bash 回执：验货指路降为 read_file（无 worktree 语义）
    with patch(
        "hiveweave.tools.executor.ToolExecutor._save_tool_output_file",
        return_value="/tmp/spill/path.txt",
    ):
        bash_body = ot._format_body(
            ot.OFFTURN_STATE.BASH_DONE.prefix, "job-2", big, "/ws",
            "agent-1", "bash",
        )
    assert "read_file 验货" in bash_body
    assert "git_worktree_status" not in bash_body


# ── 跨组契约：stream_idle ⇒ upstream ─────────────────────────────────


def test_stream_idle_error_classifies_upstream():
    """跨组契约：错误文本/error_code 含 stream_idle ⇒ upstream（可原样重试）。"""
    from hiveweave.tools.subagent import _subagent_failure_class

    assert _subagent_failure_class("stream_idle: no events for 75s") == "upstream"
    assert _subagent_failure_class("x", None, "stream_idle") == "upstream"
    assert _subagent_failure_class("Request failed (stream_idle)") == "upstream"
    # 关键词兜底保持
    assert _subagent_failure_class("Upstream SSL EOF") == "upstream"
    # 非 idle 的普通文本仍是 logic；证据不足仍是 unknown
    assert _subagent_failure_class("invalid prompt shape") == "logic"
    assert _subagent_failure_class(None) == "unknown"
    assert _subagent_failure_class("", None, "") == "unknown"
