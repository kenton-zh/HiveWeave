"""Failure-signature broadcast (F10 — 平台修复计划 2026-08-30).

背景（r4 + 四轮报告）：平台建了三层共享空间，四轮下来**零使用**。可机检的
后果 = 同一失败签名被多个 Agent 各自独立撞到：``unix-only…`` 被 4 个
Agent 撞到、``No worktree branch…`` 被 2 个 Agent 撞到（回归 R7）。

方案：某 Agent 撞到**新**失败签名时，连同根因提示写入项目共享空间
（scope='project' 的 memories，type='failure_signature'），供同项目其他
Agent 在同类调用**前**检索（工具调用前置检查注入 —— 与 F8 的重复检测共用
签名哈希，不依赖 Agent 主动去查）。

设计约束：
- 签名 = 失败 error 的规范化前缀（截断 + 空白归一），至少 12 字符才可写
  （太短的 error 缺信息量，写进去只会制造噪音）。
- 只写「新」签名：DB 里已存在同签名条目（同 project）则只更新时间戳，
  不重复新增（save_memory upsert 由 module_id 保证）。
- best-effort：共享空间写失败只记日志，绝不影响工具执行。
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any

import structlog

log = structlog.get_logger(__name__)

#: 签名长度下限 —— 低于此阈值不写入共享空间（info 不足，易误伤）。
_MIN_SIG_LEN = 24
#: 签名截断上限（存储友好 + 前缀可检索）。
_MAX_SIG_LEN = 160

_WS_RE = re.compile(r"\s+")


def signature_of(error: str | None) -> str | None:
    """规范化失败签名：空白归一 + 截断；None = 无有效信息（不广播）。"""
    if not error or not error.strip():
        return None
    sig = _WS_RE.sub(" ", error.strip())
    if len(sig) < _MIN_SIG_LEN:
        return None
    return sig[:_MAX_SIG_LEN]


def make_module_id(project_id: str, sig: str) -> str:
    """memories.module_id —— 按 (project, sig) 稳定，upsert 保证去重。

    把 project_id 纳入哈希，防止不同项目撞同一签名文本时跨项目去重
    （save_memory 的 upsert 键是 (agent_id, scope, module_id)）。
    """
    return (
        f"failure_sig::{project_id}::"
        f"{hashlib.sha256(sig.encode('utf-8', errors='replace')).hexdigest()[:16]}"
    )


#: 失败签名行使用固定写入方 —— save_memory 的 upsert 键是
#: (agent_id, scope, module_id)。若用真实 agent_id，同签名被 Agent B
#: 再次撞到时会因 upsert 键不同而**再插一行**（跨 agent 去重失效，且放大
#: 共享空间膨胀）。固定写入方使 upsert 键 (fixed, project, sig) 对全项目
#: 稳定 —— 同签名只存一行，首个撞到的 agent 记在 source_agent_id 字段。
_SIGNATURE_WRITER = "__failure_signature_pool__"


async def record_failure_signature(
    *,
    project_id: str | None,
    agent_id: str,
    tool_name: str,
    error: str | None,
    attribution: str = "",
) -> bool:
    """把新失败签名写入项目共享空间（R7 → 0 的可机检支撑）。

    Returns True 当写入/更新发生（含已存在仅刷新）。best-effort。
    """
    if not project_id:
        return False
    sig = signature_of(error)
    if sig is None:
        return False
    try:
        from hiveweave.services.memory import MemoryService

        memory_service = MemoryService()
        content = (
            f"[失败签名] tool={tool_name or '?'} | {sig}\n"
            f"根因提示: {attribution or '见错误原文'}\n"
            f"首个撞到的 Agent: {agent_id}"
            f"（撞到该签名后请先检索本项目共享空间是否已有解法）"
        )
        now_ms = int(time.time() * 1000)
        module_id = make_module_id(project_id, sig)
        # scope='project' 是团队共享层 —— 全员可见（Project Constitution 注入）。
        # agent_id 用固定写入方：跨 agent 去重（同一签名全项目只存一行）。
        mem_id = await memory_service.save_memory(
            agent_id=_SIGNATURE_WRITER,
            project_id=project_id,
            scope="project",
            content=content,
            type="failure_signature",
            module_id=module_id,
            source_agent_id=agent_id,
            metadata={
                "kind": "failure_signature",
                "signature": sig,
                "tool_name": tool_name or "",
                "first_hit_at_ms": now_ms,
            },
        )
        log.info(
            "failure_signature.broadcast",
            project_id=project_id,
            agent_id=agent_id[:12],
            tool=tool_name,
            sig=sig[:60],
            mem_id=mem_id,
        )
        return True
    except Exception as e:
        log.warning("failure_signature.broadcast_failed", error=str(e))
        return False


async def known_signature_hint(
    project_id: str | None, error: str | None
) -> str | None:
    """同项目共享空间里是否已有该失败签名 —— 供工具调用前置检查注入。

    Returns ``"[shared fix] …"`` 提示文案或 None（未命中/不可用）。
    只读、best-effort —— 查询失败仅返回 None，绝不阻断工具执行。
    """
    if not project_id:
        return None
    sig = signature_of(error)
    if sig is None:
        return None
    try:
        from hiveweave.services.memory import MemoryService

        memory_service = MemoryService()
        mems = await memory_service.get_project_memories(project_id)
        prefix = f"[失敗签名]" if False else "[失败签名]"
        for m in mems or []:
            if m.get("type") != "failure_signature":
                continue
            content = m.get("content") or ""
            first_line = content.split("\n", 1)[0] if content else ""
            if not first_line.startswith("[失败签名]"):
                continue
            # 签名行格式：`[失败签名] tool=xxx | <sig>`
            if f"| {sig}" in first_line or sig[:48] in first_line:
                return (
                    "[shared fix] 团队共享空间已有该失败签名条目 —— 先读它，"
                    "别重复撞同一个坑。"
                )
        return None
    except Exception:
        return None