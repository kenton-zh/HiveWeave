"""批次 4 附项（2026-09-11）：平台提示的**独立投递通道**。

病因（fixplan §5 附项）：`[SELF REPEAT]` / `[REPEAT REJECTION]` / `[shared fix]`
三类提示此前全部 `+=` 拼进 `result["error"]`，于是

1. 工具回执对「工具返回了什么」撒谎（DSH 设计笔记
   `2026-07-08-repeat-tool-guard.md:58` 明确否决这种做法）；
2. 提示与真错误同格 → 被习得性跳读（R7 恶化项 50:17 / 51:11 的机制）。

修复 = 走 inbox 的 **platform-reserved** 消息类型。本文件守三件事：
① 保留类型注册是否完整（伪造面）；② 投递失败是否**不阻断**工具执行；
③ 回执 `error` 字段是否确实不再含提示。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.services import health_notice as hn
from hiveweave.services import wake_policy as wp


# ── ① 保留类型注册（伪造面）─────────────────────────────


def test_platform_notice_is_reserved():
    """`platform_notice` 必须是平台保留类型 —— 否则任何 agent 都能伪造。"""
    assert wp.PLATFORM_NOTICE_MESSAGE_TYPE in wp.PLATFORM_RESERVED_MESSAGE_TYPES
    assert wp.is_platform_reserved_inbox_identity(
        message_type=wp.PLATFORM_NOTICE_MESSAGE_TYPE
    )
    # 大小写归一（inbox 落库会 lower）
    assert wp.is_platform_reserved_inbox_identity(
        message_type=wp.PLATFORM_NOTICE_MESSAGE_TYPE.upper()
    )


def test_arbitrary_type_is_not_reserved():
    """普通类型不受保护 —— 守住"保留"这个词的边界。"""
    assert not wp.is_platform_reserved_inbox_identity(message_type="normal")
    assert not wp.is_platform_reserved_inbox_identity(
        message_type="system_notice_x"
    )


def test_notice_type_is_not_mistaken_for_human():
    """平台提示**不得**被判成人类消息 —— 否则会污染人-机边界语义。"""
    assert not wp.is_human_inbox_identity(
        message_type=wp.PLATFORM_NOTICE_MESSAGE_TYPE, from_agent_id="system"
    )


# ── ② 投递语义 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_deliver_uses_trusted_platform_and_reserved_type():
    """投递必须带 `trusted_platform=True` + 保留类型（否则被 inbox 硬拒）。"""
    svc = MagicMock()
    svc.send_message = AsyncMock(return_value={"id": "m1"})
    with patch(
        "hiveweave.services.inbox.InboxService", return_value=svc
    ):
        ok = await hn.deliver_notice(
            "agent-A", "你刚撞过同一堵墙", kind=hn.KIND_SELF_REPEAT
        )
    assert ok is True
    kw = svc.send_message.await_args.kwargs
    assert kw["trusted_platform"] is True
    assert kw["message_type"] == wp.PLATFORM_NOTICE_MESSAGE_TYPE
    assert kw["to_agent_id"] == "agent-A"
    assert kw["from_agent_id"] == "system"
    assert kw["message"].startswith(f"[{hn.KIND_SELF_REPEAT}] ")


@pytest.mark.asyncio
async def test_deliver_failure_does_not_raise():
    """投递失败**不抛**（三问 ③：不阻塞）—— 工具执行不受提示通道影响。"""
    svc = MagicMock()
    svc.send_message = AsyncMock(side_effect=RuntimeError("boom"))
    with patch("hiveweave.services.inbox.InboxService", return_value=svc):
        ok = await hn.deliver_notice("agent-A", "text", kind=hn.KIND_SELF_REPEAT)
    assert ok is False  # 调用方据此只记日志


@pytest.mark.asyncio
async def test_empty_text_not_delivered():
    """空正文不投递（防刷屏）。"""
    svc = MagicMock()
    svc.send_message = AsyncMock()
    with patch("hiveweave.services.inbox.InboxService", return_value=svc):
        assert await hn.deliver_notice("a", "   ", kind="X") is False
    svc.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_long_text_truncated():
    """超长正文截断并留痕（inbox 正文不该被提示撑爆）。"""
    svc = MagicMock()
    svc.send_message = AsyncMock(return_value={"id": "m1"})
    with patch("hiveweave.services.inbox.InboxService", return_value=svc):
        await hn.deliver_notice("a", "x" * 5000, kind="X")
    body = svc.send_message.await_args.kwargs["message"]
    assert "已截断" in body
    assert len(body) < 5000


def test_combine_pending_text_drops_empties():
    assert hn.combine_pending_text(None, "", "A", "  ", "B") == "A\n\nB"
    assert hn.combine_pending_text(None, "") == ""  # 全空 → 调用方跳过投递
    assert hn.combine_pending_text("only") == "only"


# ── ③ 回执干净（真断言：error 不再被污染）──────────────


@pytest.mark.asyncio
async def test_executor_error_field_stays_clean():
    """`_f10_result_hooks` 走过提示路径后，`error` 必须**逐字等于**真错误。

    这是本批最容易回退的点：任何人重新写一句 `result["error"] += …`
    都会让这个断言变红。
    """
    from hiveweave.tools import executor as exec_mod

    err = "Error: Command blocked: [unattended mode] something long enough"
    delivered: list[tuple] = []

    async def _fake_deliver(agent_id, text, *, kind, **kw):
        delivered.append((agent_id, text, kind, kw))
        return True

    with patch("hiveweave.tools.executor.deliver_notice", _fake_deliver), patch(
        "hiveweave.db.meta.get_agent_project_id", AsyncMock(return_value=None)
    ):
        result = {"success": False, "output": "", "error": err}
        out = await exec_mod._f10_result_hooks(result, "bash", {"a": 1}, "agent-A")

    assert out["error"] == err  # 逐字相等 —— 一个字符都不许多
    assert "REPEAT" not in out["error"]
    assert "shared fix" not in out["error"]


def test_no_result_error_concatenation_left_for_notices():
    """AST 守卫：不得再出现把**提示**拼进 `result["error"]` 的写法。

    只扫 `executor.py`（提示聚合的唯一现场）。判据是 AST 结构而非文本 ——
    文档字符串里提到 `result["error"] +=` 不该让本用例变红，而真写一句
    拼接必须让它变红。
    """
    import ast
    from pathlib import Path

    import hiveweave.tools.executor as exec_mod

    src = Path(exec_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)

    offenders: list[int] = []
    for node in ast.walk(tree):
        # 形态 1：result["error"] += ...
        if isinstance(node, ast.AugAssign):
            tgt = node.target
            if (
                isinstance(tgt, ast.Subscript)
                and isinstance(tgt.slice, ast.Constant)
                and tgt.slice.value == "error"
            ):
                offenders.append(node.lineno)
        # 形态 2：result["error"] = f"{result['error']}\n\n{hint}"
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if not (
                    isinstance(tgt, ast.Subscript)
                    and isinstance(tgt.slice, ast.Constant)
                    and tgt.slice.value == "error"
                ):
                    continue
                # 右值里若出现对同一字段的读取 → 是在"追加"
                for sub in ast.walk(node.value):
                    if (
                        isinstance(sub, ast.Subscript)
                        and isinstance(sub.slice, ast.Constant)
                        and sub.slice.value == "error"
                    ):
                        offenders.append(node.lineno)
                        break

    assert not offenders, (
        f"executor.py 中仍有把内容拼进 result['error'] 的写法（行 {offenders}）"
        " —— 提示必须走 health_notice.deliver_notice（批次 4 附项）"
    )
