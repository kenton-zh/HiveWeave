"""件1（42 轮 R7 恶化项处置 2026-09-05）单元测试：失败签名解法回填。

背景：hint 18/18 无效 —— 条目只有错误原文没有解法，且常由失败者自己刚写。
修复 = backfill_solution 写回 + executor「失败记 pending → 同 agent 同工具
首次成功即回填成功参数摘要」+ _signature_has_solution 认可解法行。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.services import failure_signature as fs
from hiveweave.tools import executor as exec_mod

_ERROR = "Error: Command blocked: [unattended mode] something long enough"


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


# ── 失败 → 同 agent 同工具成功 → 条目含解法行 + hint 恢复 ──


@pytest.mark.asyncio
async def test_fail_then_success_backfills_solution_and_restores_hint(
    space, clear_pending
):
    sig = fs.signature_of(_ERROR)
    assert sig

    with patch(
        "hiveweave.db.meta.get_agent_project_id", AsyncMock(return_value="proj")
    ):
        # 1) 失败：写入签名条目 + 记 pending
        fail_result = {"success": False, "output": "", "error": _ERROR}
        await exec_mod._f10_result_hooks(
            fail_result, "bash", {"command": "ls --unix-only"}, "agent-A"
        )
        assert len(space.rows) == 1
        # #11：pending 键含**调用身份**（不是裸工具名）
        _key = exec_mod._pending_solutions_key(
            "agent-A", "bash", {"command": "ls --unix-only"}
        )
        assert _key in exec_mod._PENDING_SOLUTIONS
        assert (  # 旧两元键形态已退役 —— 它让"同工具"冒充"同问题"
            "agent-A",
            "bash",
        ) not in exec_mod._PENDING_SOLUTIONS
        assert not fs._signature_has_solution(space.rows[0]["content"])

        # 2) 同 agent 同工具**同调用**随后一次成功 → 回填成功参数摘要
        #    （#11：身份是「操作」不是「工具」，参数必须一致才会兑现）
        ok_result = {"success": True, "output": "file.txt", "error": None}
        await exec_mod._f10_pending_success_backfill(
            ok_result, "bash", {"command": "ls --unix-only"}, "agent-A"
        )
        assert _key not in exec_mod._PENDING_SOLUTIONS  # pending 已消费

    content = space.rows[0]["content"]
    assert "已验证解法:" in content
    assert "ls --unix-only" in content
    assert fs._signature_has_solution(content)
    # 解法行插在根因行之后
    lines = content.splitlines()
    assert lines[1].startswith("根因提示:")
    assert lines[2].startswith("已验证解法:")

    # 3) hint 恢复广播：别人撞同一签名能拿到 [shared fix]
    hint = await fs.known_signature_hint("proj", _ERROR, agent_id="agent-B")
    assert hint and "[shared fix]" in hint


@pytest.mark.asyncio
async def test_preexisting_hint_goes_to_other_agent_not_self(space):
    """preexisting 既有语义回归：带解法条目 → hint 给别人；自指按**内容量**门控。

    ⚠️ **行为变更（批次 4 附项，2026-09-11，有意）**：hint 不再拼进
    `result["error"]`，改走独立 platform_notice 通道 —— `error` 字段只保留
    工具的真错误（此前拼接会让回执对"工具返回了什么"撒谎，并与真错误同格
    导致被跳读）。所以本用例断言的是**通道去向**，不是 error 文本。

    ⚠️ **行为变更（TEST_DSH_62 P7 断链3，2026-09-18，有意）**：删掉
    executor 侧 `pre_source != agent_id` 的按人自指门 —— 本人复撞自己首撞、
    但条目已带已验证解法时**同样收到 hint**（hint 自带解法原文，自指命中 =
    直接拿到解法，不再是"读自己 2 秒前写的镜子"）。镜子条目（占位根因、
    无解法行）仍被 hint 内部的 _signature_has_solution 内容量门拦下。
    """
    sig = fs.signature_of(_ERROR)
    space.rows.append(
        {
            "id": "mem-1",
            "agent_id": fs._SIGNATURE_WRITER,
            "scope": "project",
            "module_id": fs.make_module_id("proj", sig, "bash"),  # 批 C：module_id 三元组含 tool
            "type": "failure_signature",
            "content": _entry_content(sig) + "\n已验证解法: 改用 pwsh 写法重试",
            "source_agent_id": "agent-A",
            "metadata": {"source_agent_id": "agent-A"},
        }
    )
    delivered: list[tuple[str, str, dict]] = []

    async def _fake_deliver(agent_id, text, *, kind, **kw):
        delivered.append((agent_id, text, kw))
        return True

    with patch(
        "hiveweave.tools.executor.deliver_notice", _fake_deliver
    ), patch(
        "hiveweave.db.meta.get_agent_project_id", AsyncMock(return_value="proj")
    ):
        # 别人（agent-B）撞坑 → 收到 [shared fix]，且 hint 携带解法原文
        result_b = {"success": False, "output": "", "error": _ERROR}
        await exec_mod._f10_result_hooks(result_b, "bash", {}, "agent-B")
        assert result_b["error"] == _ERROR  # 回执只含真错误，未被污染
        assert any("[shared fix]" in t for _, t, _ in delivered), delivered
        assert any("改用 pwsh 写法重试" in t for _, t, _ in delivered), delivered

        delivered.clear()
        # 首撞者（agent-A）自己再撞 → 条目已带解法 ⇒ 同样收到 hint
        # （P7 断链3：按人门控收窄为按内容门控）
        result_a = {"success": False, "output": "", "error": _ERROR}
        await exec_mod._f10_result_hooks(result_a, "bash", {}, "agent-A")
        assert result_a["error"] == _ERROR
        assert any("[shared fix]" in t for _, t, _ in delivered), delivered


# ── 占位 / 无实质解法不回填 ──


@pytest.mark.asyncio
async def test_placeholder_solution_not_backfilled(space):
    sig = fs.signature_of(_ERROR)
    space.rows.append(
        {
            "id": "mem-1",
            "agent_id": fs._SIGNATURE_WRITER,
            "scope": "project",
            "module_id": fs.make_module_id("proj", sig, "bash"),  # 批 C：module_id 三元组含 tool
            "type": "failure_signature",
            "content": _entry_content(sig),
            "source_agent_id": "agent-A",
            "metadata": {},
        }
    )
    before = space.rows[0]["content"]

    ok = await fs.backfill_solution(
        sig, "bash", "见错误原文", project_id="proj"
    )
    assert ok is False
    assert space.rows[0]["content"] == before  # 内容未被污染

    # 过短解法同样拒绝
    ok_short = await fs.backfill_solution(sig, "bash", "pwd", project_id="proj")
    assert ok_short is False
    assert space.rows[0]["content"] == before


@pytest.mark.asyncio
async def test_success_without_pending_no_backfill(space, clear_pending):
    """无 pending 的成功不误回填（共享空间零写入）。"""
    ok_result = {"success": True, "output": "done", "error": None}
    await exec_mod._f10_pending_success_backfill(
        ok_result, "bash", {"command": "pwsh -Command ls"}, "agent-X"
    )
    assert space.rows == []


@pytest.mark.asyncio
async def test_backfill_idempotent_when_solution_line_exists(space):
    sig = fs.signature_of(_ERROR)
    solved = _entry_content(sig) + "\n已验证解法: 已有解法保持原样"
    space.rows.append(
        {
            "id": "mem-1",
            "agent_id": fs._SIGNATURE_WRITER,
            "scope": "project",
            "module_id": fs.make_module_id("proj", sig, "bash"),  # 批 C：module_id 三元组含 tool
            "type": "failure_signature",
            "content": solved,
            "source_agent_id": "agent-A",
            "metadata": {},
        }
    )
    ok = await fs.backfill_solution(
        sig, "bash", "另一个解法不应覆盖", project_id="proj"
    )
    assert ok is True  # 幂等跳过
    assert "已有解法保持原样" in space.rows[0]["content"]
    assert "另一个解法不应覆盖" not in space.rows[0]["content"]


def test_signature_has_solution_recognizes_verified_line():
    base = _entry_content("sig-longer-than-twelve-characters")
    assert not fs._signature_has_solution(base)  # 占位根因 → 无解
    assert fs._signature_has_solution(base + "\n已验证解法: 改用 pwsh 写法")
    # 空解法行不认
    assert not fs._signature_has_solution(base + "\n已验证解法:")


# ── P1-2：敏感值脱敏（键名匹配 + 递归）──────────────────


@pytest.mark.asyncio
async def test_sensitive_args_redacted_before_backfill(space, clear_pending):
    """command/env/authorization 命中键名的值不落原文，只落已隐藏占位。"""
    sig = fs.signature_of(_ERROR)
    _args = {
        "command": "curl -H 'Authorization: Bearer sk-abc123' https://x",
        "env": {"API_TOKEN": "sk-secret-value", "HOME": "/home/u"},
        "headers": {"Authorization": "Bearer sk-leak-me"},
        "path": "a.txt",
    }
    with patch(
        "hiveweave.db.meta.get_agent_project_id", AsyncMock(return_value="proj")
    ):
        await exec_mod._f10_result_hooks(
            {"success": False, "output": "", "error": _ERROR},
            "bash",
            _args,
            "agent-A",
        )
        await exec_mod._f10_pending_success_backfill(
            {"success": True, "output": "ok", "error": None},
            "bash",
            _args,
            "agent-A",
        )
    content = space.rows[0]["content"]
    assert "已验证解法:" in content  # 回填本身成功
    # 键名命中的值 → 已隐藏占位（env 整个 dict、headers.Authorization 值）
    assert "已隐藏" in content
    # 敏感值原文绝不出现
    assert "sk-secret-value" not in content
    assert "sk-leak-me" not in content
    assert "/home/u" not in content  # env 整值已隐藏，内层兄弟值不外泄
    assert "API_TOKEN" not in content
    # 未命中键名的顶层参数值保留（其余值保留，审计 P1-2 语义）
    assert "a.txt" in content
    assert "curl -H" in content


def test_redact_recurses_nested_containers():
    args = {
        "command": "deploy.sh",
        "config": {
            "api_key": "sk-xyz",
            "nested": [{"PASSWORD": "hunter2"}, {"note": "keep"}],
        },
        "authority": "kept? auth substring matches",
    }
    out = exec_mod._redact_for_shared_solution(args)
    assert out["command"] == "deploy.sh"
    assert out["config"]["api_key"].startswith("<已隐藏")
    assert out["config"]["nested"][0]["PASSWORD"].startswith("<已隐藏")
    assert out["config"]["nested"][1]["note"] == "keep"
    assert out["authority"].startswith("<已隐藏")  # auth 子串命中（保守）


# ── P1-3：空参数解法不回填（防镜子条目换马甲）──────────


@pytest.mark.asyncio
async def test_empty_args_success_does_not_backfill(space, clear_pending):
    """tool_args={} → 解法无信息量，消费 pending 即弃，不回填不重开自指闸。"""
    sig = fs.signature_of(_ERROR)
    with patch(
        "hiveweave.db.meta.get_agent_project_id", AsyncMock(return_value="proj")
    ):
        await exec_mod._f10_result_hooks(
            {"success": False, "output": "", "error": _ERROR},
            "bash",
            {"command": "ls"},
            "agent-A",
        )
        await exec_mod._f10_pending_success_backfill(
            {"success": True, "output": "ok", "error": None}, "bash", {}, "agent-A"
        )
    assert space.rows and "已验证解法:" not in space.rows[0]["content"]
    assert not fs._signature_has_solution(space.rows[0]["content"])
    # #11-(d)：空参数成功**不许消费**真 pending（占位身份 `tool::{}`
    # 是共享的，拿它 pop 会吃掉别人的真 pending）。
    assert exec_mod._PENDING_SOLUTIONS  # 真 pending 仍在，未被空参数成功消费


@pytest.mark.asyncio
async def test_all_empty_values_do_not_backfill(space, clear_pending):
    """只有空串/None 参数 → 同样视为无实质解法。

    #11-(d)：这类失败**不记 pending**（占位身份 `tool::{}`），所以回填侧
    也没有可消费的对象 —— 两条防线各自独立成立。
    """
    with patch(
        "hiveweave.db.meta.get_agent_project_id", AsyncMock(return_value="proj")
    ):
        await exec_mod._f10_result_hooks(
            {"success": False, "output": "", "error": _ERROR},
            "bash",
            {"note": "", "flag": None},
            "agent-A",
        )
        assert not exec_mod._PENDING_SOLUTIONS  # 全空参数不记 pending
        await exec_mod._f10_pending_success_backfill(
            {"success": True, "output": "ok", "error": None},
            "bash",
            {"note": "", "flag": None},
            "agent-A",
        )
    assert space.rows and "已验证解法:" not in space.rows[0]["content"]


# ── P1-3 的边界：**部分**为空的参数仍算有实质内容 ──


@pytest.mark.asyncio
async def test_partial_empty_values_still_backfill(space, clear_pending):
    """只要有**一个**非空参数值即算有身份 —— 不许因兄弟键为空整体放弃。

    这是 #11 与 P1-3 的交界：P1-3 要挡的是「全空 / 空 dict」，不是「含空值
    的正常调用」。若把判据写成"所有值都非空"，绝大多数真实调用（可选参数
    留空）都会被误挡，回填静默失效。
    """
    _args = {"command": "ls -la", "timeout": None, "note": ""}
    with patch(
        "hiveweave.db.meta.get_agent_project_id", AsyncMock(return_value="proj")
    ):
        await exec_mod._f10_result_hooks(
            {"success": False, "output": "", "error": _ERROR},
            "bash",
            _args,
            "agent-A",
        )
        await exec_mod._f10_pending_success_backfill(
            {"success": True, "output": "ok", "error": None}, "bash", _args, "agent-A"
        )
    assert "已验证解法:" in space.rows[0]["content"]
    assert "ls -la" in space.rows[0]["content"]


# ── 备注①：pending 过期不兑现 ─────────────────────────


@pytest.mark.asyncio
async def test_expired_pending_is_discarded_not_backfilled(space, clear_pending):
    """超 TTL 的 pending 即使随后成功也不兑现（与 TTL 注释一致）。"""
    sig = fs.signature_of(_ERROR)
    space.rows.append(
        {
            "id": "mem-1",
            "agent_id": fs._SIGNATURE_WRITER,
            "scope": "project",
            "module_id": fs.make_module_id("proj", sig, "bash"),  # 批 C：module_id 三元组含 tool
            "type": "failure_signature",
            "content": _entry_content(sig),
            "source_agent_id": "agent-A",
            "metadata": {},
        }
    )
    exec_mod._PENDING_SOLUTIONS[
        exec_mod._pending_solutions_key(
            "agent-A", "bash", {"command": "pwsh -Command ls"}
        )
    ] = {
        "project_id": "proj",
        "sig": sig,
        "ts": 0,  # 远古时间戳 → 必然超 TTL
    }
    await exec_mod._f10_pending_success_backfill(
        {"success": True, "output": "ok", "error": None},
        "bash",
        {"command": "pwsh -Command ls"},
        "agent-A",
    )
    assert "已验证解法:" not in space.rows[0]["content"]
    assert not exec_mod._PENDING_SOLUTIONS  # 已消费即弃


# ── #11：调用身份（不许"同工具"冒充"同问题"）────────────────


@pytest.mark.asyncio
async def test_different_args_do_not_backfill_each_other(space, clear_pending):
    """#11 核心反例：失败在 args-A，成功在 args-B ⇒ **不许**回填。

    这是旧键 `(agent_id, tool_name)` 的实际危害形态：实测
    `read_file docs/spec.md`（不存在）写 pending，随后
    `read_file {filePath: scripts/main.gd, offset: 440}`（成功）把后者参数
    摘要回填了进去 —— 条目从"镜子"恶化成"错解"，而
    `_signature_has_solution` 会认它并对全员广播「先读它」。
    """
    with patch(
        "hiveweave.db.meta.get_agent_project_id", AsyncMock(return_value="proj")
    ):
        await exec_mod._f10_result_hooks(
            {"success": False, "output": "", "error": _ERROR},
            "read_file",
            {"filePath": "docs/spec.md"},
            "agent-A",
        )
        assert space.rows  # 条目已写
        pending_before = dict(exec_mod._PENDING_SOLUTIONS)
        assert pending_before  # pending 已记（有实质参数）

        # 同 agent 同工具、**不同参数**的成功 → 不是同一个问题的解
        await exec_mod._f10_pending_success_backfill(
            {"success": True, "output": "ok", "error": None},
            "read_file",
            {"filePath": "scripts/main.gd", "offset": 440},
            "agent-A",
        )

    assert "已验证解法:" not in space.rows[0]["content"]
    # pending 未被别人消费（错配的成功不许顺手吃掉真 pending）
    assert exec_mod._PENDING_SOLUTIONS == pending_before


@pytest.mark.asyncio
async def test_same_args_reordered_still_matches(space, clear_pending):
    """键顺序不同 = **同一次调用**（canonicalize 的深 key-sort）⇒ 必须回填。"""
    args_a = {"command": "ls -la", "cwd": "/tmp"}
    args_b = {"cwd": "/tmp", "command": "ls -la"}  # 仅键序不同
    with patch(
        "hiveweave.db.meta.get_agent_project_id", AsyncMock(return_value="proj")
    ):
        await exec_mod._f10_result_hooks(
            {"success": False, "output": "", "error": _ERROR},
            "bash",
            args_a,
            "agent-A",
        )
        await exec_mod._f10_pending_success_backfill(
            {"success": True, "output": "ok", "error": None},
            "bash",
            args_b,
            "agent-A",
        )

    assert "已验证解法:" in space.rows[0]["content"]
    assert not exec_mod._PENDING_SOLUTIONS


@pytest.mark.asyncio
async def test_empty_args_success_does_not_consume_others_pending(
    space, clear_pending
):
    """#11-(d)：空参数成功**不动** pending（占位身份不许吃掉真 pending）。

    旧实现在空参数时已 pop 完再判 P1-3 —— 于是「空参数的成功」会把同 agent
    同工具**别人的**真 pending 顺手消费掉。现在先判空再 pop。
    """
    with patch(
        "hiveweave.db.meta.get_agent_project_id", AsyncMock(return_value="proj")
    ):
        await exec_mod._f10_result_hooks(
            {"success": False, "output": "", "error": _ERROR},
            "bash",
            {"command": "ls -la"},
            "agent-A",
        )
        assert exec_mod._PENDING_SOLUTIONS  # 真 pending 在
        await exec_mod._f10_pending_success_backfill(
            {"success": True, "output": "ok", "error": None}, "bash", {}, "agent-A"
        )

    assert exec_mod._PENDING_SOLUTIONS  # 未被空参数成功消费
    assert "已验证解法:" not in space.rows[0]["content"]


def test_call_identity_key_order_and_shape():
    """身份 = `tool::<canonical json>`；只有键序被归一，值不被洗白。"""
    a = fs.call_identity("bash", {"b": 1, "a": 2})
    b = fs.call_identity("bash", {"a": 2, "b": 1})
    assert a == b  # 键序归一
    assert a.startswith("bash::")
    assert a != fs.call_identity("bash", {"a": 2, "b": 3})  # 值不同 = 不同调用
    assert a != fs.call_identity("read_file", {"a": 2, "b": 1})  # 工具不同
    # 嵌套 dict 同样深排序
    assert fs.call_identity("t", {"x": {"p": 1, "q": 2}}) == fs.call_identity(
        "t", {"x": {"q": 2, "p": 1}}
    )
    # 空参数 = 占位身份（调用方须据此禁记/禁 pop，见 #11-(d)）
    assert fs.call_identity("bash", {}) == "bash::{}"


# ── #11-(c)：签名相撞时按首行 tool= 二次定位 ──────────────


def test_first_line_matches_tool_parses_marker():
    assert fs._first_line_matches_tool("[失败签名] tool=bash | sig", "bash")
    assert not fs._first_line_matches_tool("[失败签名] tool=bash | sig", "read_file")
    # 无 tool= 段（历史条目）→ 不假装做了校验
    assert fs._first_line_matches_tool("[失败签名] sig", "bash")
    # 调用方没给工具名 → 不筛选
    assert fs._first_line_matches_tool("[失败签名] tool=bash | sig", "")


@pytest.mark.asyncio
async def test_backfill_skips_entry_whose_tool_differs(space):
    """同签名、异工具的两条条目 ⇒ 解法必须落到**同工具**那条。"""
    sig = fs.signature_of(_ERROR)
    wrong = _entry_content(sig).replace("tool=bash", "tool=read_file")
    right = _entry_content(sig)
    space.rows.append(
        {
            "id": "mem-wrong",
            "agent_id": fs._SIGNATURE_WRITER,
            "scope": "project",
            "module_id": "mod-wrong",
            "type": "failure_signature",
            "content": wrong,
            "source_agent_id": "agent-B",
            "metadata": {},
        }
    )
    space.rows.append(
        {
            "id": "mem-right",
            "agent_id": fs._SIGNATURE_WRITER,
            "scope": "project",
            "module_id": "mod-right",
            "type": "failure_signature",
            "content": right,
            "source_agent_id": "agent-A",
            "metadata": {},
        }
    )
    ok = await fs.backfill_solution(sig, "bash", "改用 pwsh 写法", project_id="proj")
    assert ok is True
    by_id = {r["id"]: r["content"] for r in space.rows}
    assert "已验证解法:" in by_id["mem-right"]
    assert "已验证解法:" not in by_id["mem-wrong"]  # 错条目不污染


# ── #16-②：solution_status 状态位不许与解法行分叉 ──────────


@pytest.mark.asyncio
async def test_solution_status_set_on_backfill(space):
    sig = fs.signature_of(_ERROR)
    space.rows.append(
        {
            "id": "mem-1",
            "agent_id": fs._SIGNATURE_WRITER,
            "scope": "project",
            "module_id": fs.make_module_id("proj", sig, "bash"),  # 批 C：module_id 三元组含 tool
            "type": "failure_signature",
            "content": _entry_content(sig),
            "source_agent_id": "agent-A",
            "metadata": {},
        }
    )
    assert (
        await fs.backfill_solution(sig, "bash", "改用 pwsh 写法", project_id="proj")
        is True
    )
    meta = space.rows[0]["metadata"]
    assert meta["solution_status"] == fs.SOLUTION_STATUS_VERIFIED
    assert meta["solution_tool"] == "bash"


@pytest.mark.asyncio
async def test_solution_status_repaired_on_idempotent_skip(space):
    """历史条目只有解法行、没有状态位 ⇒ 幂等跳过时**补写**状态位。"""
    sig = fs.signature_of(_ERROR)
    space.rows.append(
        {
            "id": "mem-1",
            "agent_id": fs._SIGNATURE_WRITER,
            "scope": "project",
            "module_id": fs.make_module_id("proj", sig, "bash"),  # 批 C：module_id 三元组含 tool
            "type": "failure_signature",
            "content": _entry_content(sig) + "\n已验证解法: 老条目留下的行",
            "source_agent_id": "agent-A",
            "metadata": {},  # 无 solution_status（占位期产物）
        }
    )
    assert (
        await fs.backfill_solution(
            sig, "bash", "改用 pwsh 写法重试", project_id="proj"
        )
        is True
    )
    assert "老条目留下的行" in space.rows[0]["content"]
    assert "改用 pwsh 写法重试" not in space.rows[0]["content"]  # 幂等不覆盖
    assert space.rows[0]["metadata"]["solution_status"] == (
        fs.SOLUTION_STATUS_VERIFIED
    )


@pytest.mark.asyncio
async def test_repair_solution_status_fixes_legacy_entries(space):
    """独立补齐路径：**解法不实质**（被 backfill 拒）的历史条目也能补状态位。

    这是 ``repair_solution_status`` 存在的理由 —— 机检口径必须能收敛到 0，
    不能只能靠"再回填一次实质解法"（被占位拒的条目永远补不上）。
    """
    sig = fs.signature_of(_ERROR)
    legacy = _entry_content(sig) + "\n已验证解法: 历史遗留行"
    space.rows.append(
        {
            "id": "mem-1",
            "agent_id": fs._SIGNATURE_WRITER,
            "scope": "project",
            "module_id": fs.make_module_id("proj", sig, "bash"),  # 批 C：module_id 三元组含 tool
            "type": "failure_signature",
            "content": legacy,
            "source_agent_id": "agent-A",
            "metadata": {"source_agent_id": "agent-A"},  # 无状态位
        }
    )
    # 被占位拒的解法：backfill 不作修复
    assert (
        await fs.backfill_solution(sig, "bash", "见错误原文", project_id="proj")
        is False
    )
    assert "solution_status" not in space.rows[0]["metadata"]

    # 补齐路径照修
    assert await fs.repair_solution_status("proj") == 1
    assert space.rows[0]["metadata"]["solution_status"] == (
        fs.SOLUTION_STATUS_VERIFIED
    )
    assert space.rows[0]["content"] == legacy  # 内容一个字节不动
    # 幂等：再跑一次不重复修
    assert await fs.repair_solution_status("proj") == 0


@pytest.mark.asyncio
async def test_repair_solution_status_skips_entries_without_solution_line(space):
    """无解法行的条目**不该**被补成 verified —— 那是伪造"已解决"。"""
    sig = fs.signature_of(_ERROR)
    space.rows.append(
        {
            "id": "mem-1",
            "agent_id": fs._SIGNATURE_WRITER,
            "scope": "project",
            "module_id": fs.make_module_id("proj", sig, "bash"),  # 批 C：module_id 三元组含 tool
            "type": "failure_signature",
            "content": _entry_content(sig),
            "source_agent_id": "agent-A",
            "metadata": {},
        }
    )
    assert await fs.repair_solution_status("proj") == 0
    assert "solution_status" not in space.rows[0]["metadata"]


@pytest.mark.asyncio
async def test_new_entry_carries_solution_status_none(space):
    """新条目显式落 `none` —— 机检口径据此判定，不靠"字段缺失"推断。"""
    with patch(
        "hiveweave.db.meta.get_agent_project_id", AsyncMock(return_value="proj")
    ):
        await exec_mod._f10_result_hooks(
            {"success": False, "output": "", "error": _ERROR},
            "bash",
            {"command": "ls"},
            "agent-A",
        )
    assert space.rows[0]["metadata"]["solution_status"] == fs.SOLUTION_STATUS_NONE


@pytest.mark.asyncio
async def test_rehit_preserves_solution_status(space):
    """rehit（同签名再撞）必须**保留**状态位 —— 否则解法行与状态位分叉。"""
    sig = fs.signature_of(_ERROR)
    space.rows.append(
        {
            "id": "mem-1",
            "agent_id": fs._SIGNATURE_WRITER,
            "scope": "project",
            "module_id": fs.make_module_id("proj", sig, "bash"),  # 批 C：module_id 三元组含 tool
            "type": "failure_signature",
            "content": _entry_content(sig) + "\n已验证解法: 已回填的解法",
            "source_agent_id": "agent-A",
            "metadata": {
                "source_agent_id": "agent-A",
                "solution_status": fs.SOLUTION_STATUS_VERIFIED,
                "solution_tool": "bash",
                "solved_at_ms": 123,
                "hit_count": 1,
            },
        }
    )
    with patch(
        "hiveweave.db.meta.get_agent_project_id", AsyncMock(return_value="proj")
    ):
        await exec_mod._f10_result_hooks(
            {"success": False, "output": "", "error": _ERROR},
            "bash",
            {"command": "ls"},
            "agent-B",
        )
    meta = space.rows[0]["metadata"]
    assert meta["solution_status"] == fs.SOLUTION_STATUS_VERIFIED
    assert meta["solution_tool"] == "bash"
    assert meta["hit_count"] == 2
    # 解法行也在（rehit 不许冲掉）
    assert "已回填的解法" in space.rows[0]["content"]


@pytest.mark.asyncio
async def test_solution_status_never_diverges_from_solution_line(space):
    """机检口径本身：不许出现「有解法行但状态位 != verified」。"""
    _args = {"command": "ls -la"}
    with patch(
        "hiveweave.db.meta.get_agent_project_id", AsyncMock(return_value="proj")
    ):
        await exec_mod._f10_result_hooks(
            {"success": False, "output": "", "error": _ERROR},
            "bash",
            _args,
            "agent-A",
        )
        await exec_mod._f10_pending_success_backfill(
            {"success": True, "output": "ok", "error": None},
            "bash",
            _args,
            "agent-A",
        )
        # 再撞一次（rehit 路径）后仍须自洽
        await exec_mod._f10_result_hooks(
            {"success": False, "output": "", "error": _ERROR},
            "bash",
            {"command": "ls"},
            "agent-B",
        )
    for r in space.rows:
        has_line = fs._has_verified_solution_line(r["content"])
        status = (r.get("metadata") or {}).get("solution_status")
        assert has_line, "本例应当已回填出解法行（否则下面的断言无意义）"
        assert status == fs.SOLUTION_STATUS_VERIFIED, (
            f"解法行与状态位分叉：{r['id']} status={status!r}"
        )


# ── TEST_DSH_62 P7 断链1：hint 携带解法原文 ──────────────


@pytest.mark.asyncio
async def test_hint_carries_solution_line_text(space):
    """hint 命中带「已验证解法:」行的条目 ⇒ 解法文本直接拼进提示文案。

    断链1：签名池对 Agent 不可达（read_memory 只读 agent 域），旧固定文案
    「先读它」是发不出去的指令 —— 命中即得解法本身才是闭环。
    """
    sig = fs.signature_of(_ERROR)
    space.rows.append(
        {
            "id": "mem-1",
            "agent_id": fs._SIGNATURE_WRITER,
            "scope": "project",
            "module_id": fs.make_module_id("proj", sig, "bash"),
            "type": "failure_signature",
            "content": _entry_content(sig) + "\n已验证解法: 改用 pwsh -Command ls 重试",
            "source_agent_id": "agent-A",
            "metadata": {"source_agent_id": "agent-A"},
        }
    )
    hint = await fs.known_signature_hint("proj", _ERROR, agent_id="agent-B")
    assert hint and hint.startswith("[shared fix]")
    assert "已验证解法:" in hint
    assert "改用 pwsh -Command ls 重试" in hint  # 解法原文逐字在场
    assert "先读它" not in hint


@pytest.mark.asyncio
async def test_hint_without_solution_line_drops_unreachable_instruction(space):
    """无解法行（仅实质根因，_signature_has_solution 的②支）⇒ 中性提示。

    原则：不给 Agent 发无法执行的指令 ——「先读它」指向一个 read_memory
    读不到的地方（签名池在 project 域），必须从文案里消失。
    """
    sig = fs.signature_of(_ERROR)
    content = (
        f"[失败签名] tool=bash | {sig}\n"
        "根因提示: runner_failed: shell 方言不兼容 —— 命令从未执行\n"
        f"原文尾: {sig[-40:]}\n"
        "首个撞到的 Agent: agent-A"
    )
    space.rows.append(
        {
            "id": "mem-1",
            "agent_id": fs._SIGNATURE_WRITER,
            "scope": "project",
            "module_id": fs.make_module_id("proj", sig, "bash"),
            "type": "failure_signature",
            "content": content,
            "source_agent_id": "agent-A",
            "metadata": {"source_agent_id": "agent-A"},
        }
    )
    hint = await fs.known_signature_hint("proj", _ERROR, agent_id="agent-B")
    assert hint and "[shared fix]" in hint
    assert "先读它" not in hint


# ── TEST_DSH_62 P7 断链2：self-repeat 键不含 run_id ──────


def test_self_repeat_fires_across_run_boundary(monkeypatch):
    """同 agent 跨 run（shell 每次调用一个 run）复撞同一签名 ⇒ 第二次必提示。

    回归钉：若有人把 run_id 并回键（TEST_DSH_47 #6 旧形态），本用例变红
    —— 旧键下两次调用分属不同 run_id，各自算首撞、零提示（实测同一 Agent
    2h13m 撞 11 次 head 全程无通知的机制）。误标由 600s 时间窗兜底，不测。
    """
    exec_mod.reset_self_repeat_hits_for_tests()
    try:
        sig = fs.signature_of(_ERROR)
        assert sig

        class _FakeAgent:
            def __init__(self, run_id: str) -> None:
                self._current_run_id = run_id

        class _FakeManager:
            """两次 get_agent 返回**不同** run_id 的 agent（模拟跨 run）。"""

            def __init__(self) -> None:
                self._seq = iter(["run-1", "run-2"])

            def get_agent(self, agent_id):
                return _FakeAgent(next(self._seq))

        import hiveweave.agents.supervisor as supervisor

        monkeypatch.setattr(supervisor, "agent_manager", _FakeManager())

        first = exec_mod._note_self_repeat_hit("agent-A", "bash", sig)
        second = exec_mod._note_self_repeat_hit("agent-A", "bash", sig)
        assert first == ""  # 首撞不提示
        assert "SELF REPEAT" in second  # 跨 run 复撞必须提示
        assert "#2" in second
    finally:
        exec_mod.reset_self_repeat_hits_for_tests()


def test_self_repeat_does_not_leak_across_agents():
    """键含 agent_id：别人的复撞记忆不得让我"被提示刚撞过"。"""
    exec_mod.reset_self_repeat_hits_for_tests()
    try:
        sig = fs.signature_of(_ERROR)
        assert exec_mod._note_self_repeat_hit("agent-A", "bash", sig) == ""
        assert exec_mod._note_self_repeat_hit("agent-B", "bash", sig) == ""
        assert "SELF REPEAT" in exec_mod._note_self_repeat_hit(
            "agent-A", "bash", sig
        )
    finally:
        exec_mod.reset_self_repeat_hits_for_tests()


# ── TEST_DSH_62 P7 断链4：rehit 继承梯度状态 + 补状态位 ──


@pytest.mark.asyncio
async def test_rehit_preserves_org_gradient_state(space):
    """rehit 重建 metadata 不得抹掉 distinct_hitters / 已发档位。

    旧实现只继承 solved_* 三键 + hit_count + source_agent_id ⇒ rehit 一次
    把梯度状态抹回零，3/5/8 组织升级档位失灵。
    """
    sig = fs.signature_of(_ERROR)
    space.rows.append(
        {
            "id": "mem-1",
            "agent_id": fs._SIGNATURE_WRITER,
            "scope": "project",
            "module_id": fs.make_module_id("proj", sig, "bash"),
            "type": "failure_signature",
            "content": _entry_content(sig),
            "source_agent_id": "agent-A",
            "metadata": {
                "source_agent_id": "agent-A",
                "hit_count": 3,
                fs._HITTERS_KEY: ["agent-A", "agent-B"],
                fs._HITTERS_OVERFLOW_KEY: 0,
                fs._ORG_ESCALATED_AT_KEY: 123456,
                fs._ORG_ESCALATED_TIERS_KEY: [0],
            },
        }
    )
    rec = await fs.record_failure_signature(
        project_id="proj",
        agent_id="agent-C",
        tool_name="bash",
        error=_ERROR,
        attribution="",
    )
    assert rec["preexisting"] is True
    meta = space.rows[0]["metadata"]
    assert meta[fs._HITTERS_KEY] == ["agent-A", "agent-B"]  # 不被抹掉
    assert meta[fs._HITTERS_OVERFLOW_KEY] == 0
    assert meta[fs._ORG_ESCALATED_TIERS_KEY] == [0]
    assert meta[fs._ORG_ESCALATED_AT_KEY] == 123456
    assert meta["hit_count"] == 4  # 计数照常累加（不受继承影响）


@pytest.mark.asyncio
async def test_rehit_inserts_solution_status_none_when_missing(space):
    """preexisting 分支重建必须显式落状态位（19 条 missing 的来源修复）。

    与 else 新签分支同语义：字段存在且语义明确，不靠"缺失"推断。
    """
    sig = fs.signature_of(_ERROR)
    space.rows.append(
        {
            "id": "mem-1",
            "agent_id": fs._SIGNATURE_WRITER,
            "scope": "project",
            "module_id": fs.make_module_id("proj", sig, "bash"),
            "type": "failure_signature",
            "content": _entry_content(sig),  # 无解法行
            "source_agent_id": "agent-A",
            "metadata": {"source_agent_id": "agent-A", "hit_count": 1},
        }
    )
    await fs.record_failure_signature(
        project_id="proj",
        agent_id="agent-B",
        tool_name="bash",
        error=_ERROR,
        attribution="",
    )
    meta = space.rows[0]["metadata"]
    assert meta["solution_status"] == fs.SOLUTION_STATUS_NONE
    assert meta["hit_count"] == 2


@pytest.mark.asyncio
async def test_rehit_carried_solution_line_backfills_verified_status(space):
    """解法行被携带（同 tool rehit）而旧 meta 缺状态位 ⇒ 补 verified。

    机检不变式：有解法行与状态位不许分叉 —— setdefault 的默认值必须随
    「是否携带解法行」走，而不是一律 none。
    """
    sig = fs.signature_of(_ERROR)
    space.rows.append(
        {
            "id": "mem-1",
            "agent_id": fs._SIGNATURE_WRITER,
            "scope": "project",
            "module_id": fs.make_module_id("proj", sig, "bash"),
            "type": "failure_signature",
            "content": _entry_content(sig) + "\n已验证解法: 改用 pwsh 写法",
            "source_agent_id": "agent-A",
            "metadata": {"source_agent_id": "agent-A", "hit_count": 1},
        }
    )
    await fs.record_failure_signature(
        project_id="proj",
        agent_id="agent-B",
        tool_name="bash",
        error=_ERROR,
        attribution="",
    )
    meta = space.rows[0]["metadata"]
    assert meta["solution_status"] == fs.SOLUTION_STATUS_VERIFIED
    # 解法行也原样保留（携带语义不回退）
    assert "改用 pwsh 写法" in space.rows[0]["content"]
