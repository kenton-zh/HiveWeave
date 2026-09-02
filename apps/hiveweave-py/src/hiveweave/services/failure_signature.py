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

#: 签名条目保留上限（P2 复审 2026-08-30）：签名单调累积，compaction/
#: archive 只碰 scope='agent'，永不清理签名行。get_project_memories
#: LIMIT 100，签名逼近上限会物理挤掉合法 constitution 条目。写入时
#: 顺带裁剪到该上限（只删最老签名行，best-effort）。
_SIGNATURE_MAX_ROWS = 50


async def _trim_signature_rows(project_id: str) -> None:
    """删最老签名行到 _SIGNATURE_MAX_ROWS 以内（幂等 best-effort）。"""
    try:
        from hiveweave.db import project as project_db
        from hiveweave.services.memory import get_workspace_write_lock
        from hiveweave.db import meta as meta_db

        workspace = await meta_db.get_project_workspace(project_id)
        if not workspace:
            return
        lock = await get_workspace_write_lock(workspace)
        async with lock:
            conn = await project_db.ensure_project_db(workspace)
            try:
                await conn.execute("BEGIN IMMEDIATE")
                cur = await conn.execute(
                    "SELECT COUNT(*) AS n FROM memories "
                    "WHERE scope = 'project' AND type = 'failure_signature'"
                )
                row = await cur.fetchone()
                await cur.close()
                total = int(row["n"] or 0) if row else 0
                excess = total - _SIGNATURE_MAX_ROWS
                if excess > 0:
                    cur = await conn.execute(
                        "SELECT id FROM memories "
                        "WHERE scope = 'project' AND type = 'failure_signature' "
                        "ORDER BY created_at ASC, rowid ASC LIMIT ?",
                        [excess],
                    )
                    ids = [r["id"] for r in await cur.fetchall()]
                    await cur.close()
                    if ids:
                        ph = ",".join("?" * len(ids))
                        await conn.execute(
                            f"DELETE FROM memories WHERE id IN ({ph})", ids
                        )
                await conn.commit()
                # P3（边界审计 2026-08-30）：trim 删行后失效 project 记忆
                # 缓存 —— 否则 get_project_memories 30s 快照仍含已删签名，
                # known_signature_hint 30s 内对已删签名仍返回 shared-fix。
                try:
                    from hiveweave.services.memory import MemoryService

                    MemoryService.invalidate(
                        project_id, scope="project"
                    )
                except Exception:
                    pass
            except Exception:
                try:
                    await conn.rollback()
                except Exception:
                    pass
                raise
    except Exception as e:
        log.warning("failure_signature.trim_failed", error=str(e))


async def record_failure_signature(
    *,
    project_id: str | None,
    agent_id: str,
    tool_name: str,
    error: str | None,
    attribution: str = "",
) -> bool:
    """把新失败签名写入项目共享空间（R7 → 0 的可机检支撑）。

    Returns ``{"written": bool, "preexisting": bool, "preexisting_source": str|None}``：
    ``preexisting``=该签名在本次失败**之前**已存在（39 审计 P1-3：首撞者不该收
    "先读它"自指提示——executor 据此门控 hint）；``preexisting_source``=首撞者。
    best-effort。
    """
    if not project_id:
        return False
    sig = signature_of(error)
    if sig is None:
        return {"written": False, "preexisting": False, "preexisting_source": None}
    try:
        from hiveweave.services.memory import MemoryService

        memory_service = MemoryService()
        # 39 审计 P1-3（签名自指 8 连发）：首撞者不该收到"先读它"提示——
        # 条目内容就是自己 2 秒前写的错误原文。先查签名是否**早已存在**及
        # 其首撞者，调用方（executor）据此门控 hint：只给"别人的坑"发提示。
        preexisting = False
        preexisting_source: str | None = None
        try:
            for m in (await memory_service.get_project_memories(project_id)) or []:
                if m.get("type") != "failure_signature":
                    continue
                fl = (m.get("content") or "").split("\n", 1)[0]
                if fl.startswith("[失败签名]") and (
                    f"| {sig}" in fl or sig[:48] in fl
                ):
                    preexisting = True
                    preexisting_source = (
                        str((m.get("metadata") or {}).get("source_agent_id") or "")
                        or None
                    )
                    break
        except Exception:  # noqa: BLE001 — 前查失败按"新签名"处理
            pass
        content = (
            f"[失败签名] tool={tool_name or '?'} | {sig}\n"
            f"根因提示: {attribution or '见错误原文'}\n"
            f"原文尾(含等价写法/修复线索): {(error or '').strip()[-320:]}\n"
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
        # P2 复审：写入顺带裁剪最老签名行到上限（防单调累积挤掉合法
        # constitution；best-effort 不阻断）。
        try:
            await _trim_signature_rows(project_id)
        except Exception as trim_err:  # noqa: BLE001 — 裁剪 best-effort
            log.warning("failure_signature.trim_failed", error=str(trim_err))
        return {
            "written": True,
            "preexisting": preexisting,
            "preexisting_source": preexisting_source,
        }
    except Exception as e:
        log.warning("failure_signature.broadcast_failed", error=str(e))
        return {"written": False, "preexisting": False, "preexisting_source": None}


async def known_signature_hint(
    project_id: str | None, error: str | None, agent_id: str | None = None
) -> str | None:
    """同项目共享空间里是否已有该失败签名 —— 供工具调用前置检查注入。

    Returns ``"[shared fix] …"`` 提示文案或 None（未命中/不可用/自指）。

    **自指抑制（2026-09-01，s3-clone_06）**：F10 的 hook 是「先写签名、后取提
    示」——同一次失败写入的条目会被自己立刻命中，而该条目内容只有错误原文 +
    占位根因（``见错误原文``）。提示它去「先读它」等于指它读自己刚写的一面镜子，
    零信息量，且因为看起来在工作而极难被发现（TEST_DSH_38 实测 18/18 失败步
    全部收到该提示，dev server 同一堵墙连撞 3 次）。

    因此：命中的签名条目**必须携带超出错误原文的信息**（根因提示非占位）才广播。
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
        for m in mems or []:
            if m.get("type") != "failure_signature":
                continue
            content = m.get("content") or ""
            first_line = content.split("\n", 1)[0] if content else ""
            if not first_line.startswith("[失败签名]"):
                continue
            # 签名行格式：`[失败签名] tool=xxx | <sig>`
            if f"| {sig}" in first_line or sig[:48] in first_line:
                if not _signature_has_solution(content):
                    log.debug(
                        "failure_signature.hint_suppressed_self_reference",
                        agent_id=(agent_id or "")[:12],
                        sig=sig[:60],
                    )
                    return None
                return (
                    "[shared fix] 团队共享空间已有该失败签名条目 —— 先读它，"
                    "别重复撞同一个坑。"
                )
        return None
    except Exception:
        return None


def attribution_of(result: dict) -> str:
    """从工具回执推导一句话归因（供共享签名条目使用）。

    判定顺序按「信息量从具体到笼统」——s3-clone_06 P0-3：方言不兼容必须先
    于 blocked 判定，否则"bash 写法在受限 shell 不认"会被报成"平台护栏拒绝
    （权限/沙箱/安全）"，把撞坑 Agent 指向错误的排查方向（DSH postmortem
    0004：宽泛签名 → 误归因，同构缺陷）。
    """
    try:
        if result.get("dialect_failed"):
            return (
                "runner_failed: shell 方言不兼容 —— 命令从未执行。"
                "改写为 pwsh 写法（见错误原文的等价表）或直接调 pwsh 工具；"
                "不要用不同的 unix flag 重试"
            )
        if result.get("runner_failed"):
            return "runner_failed: 命令未执行（执行器/方言/权限/审批）"
        if result.get("command_failed"):
            return "command_failed: 命令执行了但失败（业务/测试未过）"
        if result.get("blocked"):
            return "blocked: 平台护栏拒绝（权限/沙箱/安全）"
    except Exception:  # noqa: BLE001 — 归因是旁支，绝不能因它挂掉工具回执
        return ""
    return ""


# ``record_failure_signature`` 写入根因提示时的占位值 —— 表示「没有可用根因，
# 去看错误原文」。条目停留在占位状态 = 它只是错误原文的副本（自指镜子）。
_ROOT_CAUSE_PLACEHOLDER = "见错误原文"


def _signature_has_solution(content: str) -> bool:
    """签名条目是否携带超出错误原文的信息（可指导下一步动作）。

    判定只看「根因提示:」那一行：缺失 / 空 / 等于占位值 → 无信息量。
    未来若新增结构化解法字段，应在此一并纳入判定。
    """
    for line in (content or "").splitlines():
        if not line.startswith("根因提示:"):
            continue
        value = line.split(":", 1)[1].strip()
        return bool(value) and value != _ROOT_CAUSE_PLACEHOLDER
    return False