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
import json
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


def _deep_key_sort(value: Any) -> Any:
    """递归按 key 排序 dict（list 保序，元素递归排序）。

    只做**键顺序**归一，不做任何值归一 —— 顺序不同不得洗白（批次 4 纪律）。
    """
    if isinstance(value, dict):
        return {
            k: _deep_key_sort(value[k])
            for k in sorted(value.keys(), key=lambda x: str(x))
        }
    if isinstance(value, list):
        return [_deep_key_sort(v) for v in value]
    return value


def canonicalize(args: Any) -> str:
    """参数规范化：深 key-sort 后 JSON stringify（无空白）。

    与 DSH ``canonicalize`` 同义：**只有键顺序被归一**，值本身（含字符串内
    空白、大小写、等价但不相同的写法）一律保留 —— 顺序不同不得洗白。
    ``None`` / 非 dict 输入按自身 stringify（``default=str`` 兜底）。
    """
    try:
        return json.dumps(
            _deep_key_sort(args if args is not None else {}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except Exception:  # noqa: BLE001 — 不可序列化对象降级为 str 快照
        return str(args)


def call_identity(tool_name: str, args: Any) -> str:
    """**调用身份** —— 待回填问题的身份键（批次 4 #11）。

    ``tool_name::{canonicalize(args)}``。绑定的是**操作身份**，不是"工具"：
    ``(agent_id, tool_name)`` 会把「工具的下一次成功」当成「同一个问题的解」
    ——实测 ``read_file docs/spec.md``（不存在）回填到 ``{filePath:
    scripts/main.gd, offset: 440}``，条目从"镜子"恶化成"错解"。

    硬性不变式（入口）：
    1. pending 键必须含本函数返回值 —— 不许退化为 ``(agent_id, tool_name)``；
    2. 判定与存储用**同一个** ``canonicalize``（顺序不同 = 不同调用，不洗白）；
    3. 一次失败只对应一个身份，回填必须在同身份的成功上兑现。
    """
    return f"{(tool_name or '').strip()}::{canonicalize(args)}"


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
        # 件1（回填处置 2026-09-05）：rehit 时旧条目若已回填「已验证解法:」行，
        # 原样携带到新内容 —— 否则本次覆写会把刚回填的解法冲掉（回填白做，
        # hint 恢复也随之失效）。
        carried_solution_line: str | None = None
        prev_meta: dict = {}
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
                    prev_meta = m.get("metadata") or {}
                    for _line in (m.get("content") or "").splitlines():
                        if _line.startswith(_SOLUTION_LINE_PREFIX):
                            carried_solution_line = _line
                            break
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
        if carried_solution_line:
            _lines = content.split("\n")
            _at = len(_lines)  # 兜底：找不到根因行则追加末尾（绝不插首行前，
            # 否则破坏 [失败签名] 首行匹配）
            for _i, _ln in enumerate(_lines):
                if _ln.startswith("根因提示:"):
                    _at = _i + 1
                    break
            _lines.insert(_at, carried_solution_line)
            content = "\n".join(_lines)
        now_ms = int(time.time() * 1000)
        module_id = make_module_id(project_id, sig)
        metadata = {
            "kind": "failure_signature",
            "signature": sig,
            "tool_name": tool_name or "",
            "first_hit_at_ms": now_ms,
        }
        if preexisting:
            # 回填溯源字段随 rehit 保留（solved_at/solution_tool + 状态位）
            for _k in ("solved_at_ms", "solution_tool", "solution_status"):
                if _k in (prev_meta or {}):
                    metadata[_k] = prev_meta[_k]
            metadata["hit_count"] = int((prev_meta or {}).get("hit_count") or 1) + 1
        else:
            # #16-② 状态位显式落 ``none``：机检口径是
            # ``content LIKE '%已验证解法:%' AND solution_status != 'verified'``
            # 必须为 0（「有解法行」与「状态位」不许分叉）。新条目没有解法行，
            # 但写 ``none`` 让"该字段存在且语义明确"可被断言，而不是靠"字段
            # 缺失"推断 —— 缺失既可能是"还没回填"，也可能是"写入方忘了写"。
            metadata["hit_count"] = 1
            metadata.setdefault("solution_status", SOLUTION_STATUS_NONE)
        # 首撞者写入 metadata（自指抑制门的读取源）：此前只在列里存最新
        # 撞到者、metadata 不落 → 首撞者信息在 rehit 后丢失，门退化为
        # 永远放行（hint 又指回自己刚写的条目）。首撞者须跨 rehit 稳定。
        metadata["source_agent_id"] = (
            (prev_meta or {}).get("source_agent_id") if preexisting else None
        ) or agent_id
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
            metadata=metadata,
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

    ⚠ 位的**读取与优先级**由 ``tools/fact_positions.fact_from_bits()`` **单一
    实现**（#15，2026-09-14：判据只许有一份，免得两处各自演化成"哪份才是漏的"）。
    本函数只负责把事实位翻成**给人看的一句话**（含方言专项文案）。
    """
    try:
        # 延迟 import：`services` → `tools` 的方向只在调用期发生，避免模块级环。
        from hiveweave.tools.fact_positions import fact_from_bits

        kind = fact_from_bits(result)
        if kind == "runner_failed":
            if result.get("dialect_failed"):
                return (
                    "runner_failed: shell 方言不兼容 —— 命令从未执行。"
                    "改写为 pwsh 写法（见错误原文的等价表）或直接调 pwsh 工具；"
                    "不要用不同的 unix flag 重试"
                )
            return "runner_failed: 命令未执行（执行器/方言/权限/审批）"
        if kind == "command_failed":
            return "command_failed: 命令执行了但失败（业务/测试未过）"
        # #15：`bad_args` 被 L6 从 blocked 改判出来后（保留端口等）**没有对应文案**
        # ⇒ 归因退回空串，撞坑 Agent 拿不到方向。补上它。
        # 判据读**事实位**而非文本：位是声明出来的状态，与措辞/语言无关
        # （``bad_args`` 不会出现在布尔位里 —— 布尔位只有 dialect/runner/command，
        # 故这里直接读 ``fact``）。
        if str(result.get("fact") or "") == "bad_args" or kind == "bad_args":
            return (
                "bad_args: 调用方参数错（平台无责）—— 换个参数即可通过，"
                "别用同一组参数重试"
            )
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

    判定看两处：①「已验证解法:」行（backfill_solution 回填，非空即有解，
    hint 恢复广播）；②「根因提示:」行：缺失 / 空 / 等于占位值 → 无信息量。
    未来若新增结构化解法字段，应在此一并纳入判定。
    """
    has_root_cause = False
    for line in (content or "").splitlines():
        if line.startswith(_SOLUTION_LINE_PREFIX):
            if line.split(":", 1)[1].strip():
                return True
            continue
        if line.startswith("根因提示:"):
            value = line.split(":", 1)[1].strip()
            has_root_cause = bool(value) and value != _ROOT_CAUSE_PLACEHOLDER
    return has_root_cause


# ── 解法回填（42 轮实测处置 2026-09-05）──────────────────
# 18/18 hint 无效的根因：条目常由失败者自己刚写（first_hit 晚 0.008-30s），
# 且只有错误原文没有解法。本节提供「问题解决 → 回填」的写回通道：
# executor 在失败时记 pending，同 agent 同工具随后一次成功即回填成功参数
# 摘要作为解法（纯机械，无 LLM 总结）。

#: 已验证解法行前缀 —— backfill_solution 追加、_signature_has_solution 认可。
_SOLUTION_LINE_PREFIX = "已验证解法:"

#: 解法状态位（#16-②）—— 条目 metadata 上的唯一权威判定：
#: ``none`` = 无已验证解法（占位根因，镜子条目）；``verified`` = 已被
#: backfill_solution 回填并经身份校验。机检口径：
#: ``content LIKE '%已验证解法:%' AND solution_status != 'verified'`` 必须为 0
#: —— 即「有解法行」与「状态位」不允许分叉。
SOLUTION_STATUS_NONE = "none"
SOLUTION_STATUS_VERIFIED = "verified"
_SOLUTION_STATUSES = frozenset({SOLUTION_STATUS_NONE, SOLUTION_STATUS_VERIFIED})

#: 解法文本长度下限 —— 低于此阈值视为无实质内容，不回填（防噪音）。
_MIN_SOLUTION_LEN = 8


def _is_substantive_solution(solution: str | None) -> bool:
    """只接受实质解法：非空、达长度阈值、且非占位文案。

    占位判定语义与 _signature_has_solution 一致 —— 防把「见错误原文」
    当解法回填（回填了等于没回填，还会让 hint 误恢复广播）。
    """
    value = (solution or "").strip()
    if not value or len(value) < _MIN_SOLUTION_LEN:
        return False
    return value != _ROOT_CAUSE_PLACEHOLDER


def _has_verified_solution_line(content: str) -> bool:
    """条目是否已含「已验证解法:」行（幂等回填判定用）。"""
    for line in (content or "").splitlines():
        if line.startswith(_SOLUTION_LINE_PREFIX):
            return True
    return False


def _first_line_matches_tool(first_line: str, tool_name: str) -> bool:
    """首行 ``[失败签名] tool=<name> | <sig>`` 是否属于该工具（#11-(c)）。

    同签名不同工具**必须能区分**：``signature_of`` 会截断长错误文本，
    两条不同错误可能落到同一签名；此时按签名定位会先把解法填进「不是它的」
    条目（错解比无解更贵 —— ``_signature_has_solution`` 会认它并对全员
    广播「先读它」）。``tool_name`` 为空时**不做二次筛选**（调用方没给出
    可供筛选的身份，此时按签名定位是唯一可选行为；不假装做了校验）。
    解析失败（首行没有 ``tool=`` 段）同样放行 —— 历史条目可能早于该格式。
    """
    name = (tool_name or "").strip()
    if not name:
        return True
    head = (first_line or "").split("|", 1)[0]
    marker = "tool="
    idx = head.find(marker)
    if idx < 0:
        return True
    got = head[idx + len(marker):].strip()
    return got == name


async def repair_solution_status(project_id: str) -> int:
    """把「已有解法行但状态位没跟上」的历史条目**补齐**状态位（#16-②）。

    这条路径独立于 ``backfill_solution`` 存在，因为后者有两条合法早退：
    ① 传入的解法不实质（``_is_substantive_solution`` 拒）→ 直接 return；
    ② 条目已有解法行 → 幂等跳过。
    只有 ② 会补状态位，而**最需要补的那批条目恰恰是「已有解法行」的**——
    它们多数由旧代码写入，回填当时还没有状态位字段。若只能靠"再回填一次
    实质解法"触发，那已经被占位拒绝的条目永远补不上，机检口径
    ``content LIKE '%已验证解法:%' AND solution_status != 'verified'``
    会长期非零而无从修复。

    Returns 补齐的条目数。best-effort：任何异常只记日志返回 0。
    """
    if not project_id:
        return 0
    fixed = 0
    try:
        from hiveweave.services.memory import MemoryService

        memory_service = MemoryService()
        for m in (await memory_service.get_project_memories(project_id)) or []:
            if m.get("type") != "failure_signature":
                continue
            content = m.get("content") or ""
            if not _has_verified_solution_line(content):
                continue
            metadata = dict(m.get("metadata") or {})
            if metadata.get("solution_status") == SOLUTION_STATUS_VERIFIED:
                continue
            if not m.get("module_id"):
                continue
            metadata["solution_status"] = SOLUTION_STATUS_VERIFIED
            await memory_service.save_memory(
                agent_id=_SIGNATURE_WRITER,
                project_id=project_id,
                scope="project",
                content=content,
                type="failure_signature",
                module_id=m.get("module_id"),
                source_agent_id=m.get("source_agent_id"),
                metadata=metadata,
            )
            fixed += 1
        if fixed:
            log.info("failure_signature.solution_status_repaired", count=fixed)
    except Exception as e:  # noqa: BLE001 — 补齐 best-effort，不阻断任何主流程
        log.warning("failure_signature.status_repair_failed", error=str(e))
    return fixed


async def backfill_solution(
    signature_key: str,
    tool_name: str,
    solution: str,
    *,
    project_id: str | None = None,
) -> bool:
    """问题解决后把解法回填进既有失败签名条目（R7 恶化项处置）。

    ``signature_key`` = 规范化失败签名（``signature_of`` 的返回值；executor
    在失败时存入 pending，同 agent **同调用身份**随后一次成功后原样带回
    —— 身份由 ``call_identity`` 定义，见其 docstring 的三条不变式）。
    ``project_id`` 定位 per-project DB（executor 在失败/成功两侧都拿得到，
    随 pending 传递；keyword-only 以保持三参位置调用契约）。

    定位 = 同 project、scope='project'、type='failure_signature'、首行
    **同时**含该签名与 ``tool=<tool_name>``（#11-(c) 写入侧补校验：签名相撞
    时必须指向正确条目，不许把解法填进别条；与 ``known_signature_hint``
    同一签名匹配语义）。回填 = 在「根因提示:」行后插入「已验证解法:
    <solution>」行（UPDATE 整条 content，不加行数，50 行裁剪逻辑不受影响）；
    经 MemoryService.save_memory 的固定写入方 upsert 落库，写锁与缓存失效
    沿用既有路径。**同时**把 ``metadata.solution_status`` 置 ``verified``
    （#16-② 解法必填校验：有解法行 ⇒ 状态位必须为 verified，机检见
    ``SOLUTION_STATUS_VERIFIED`` 注释）。

    只接受实质解法（``_is_substantive_solution``）；条目已有解法行时幂等
    跳过（并补写状态位，修占位期的不一致）。best-effort：任何失败只记日志
    返回 False，绝不影响工具执行。
    """
    sig = (signature_key or "").strip()
    if not sig or not project_id:
        return False
    if not _is_substantive_solution(solution):
        log.info(
            "failure_signature.backfill_rejected_non_solution",
            tool=tool_name,
            sig=sig[:60],
        )
        return False
    try:
        from hiveweave.services.memory import MemoryService

        memory_service = MemoryService()
        target = None
        for m in (await memory_service.get_project_memories(project_id)) or []:
            if m.get("type") != "failure_signature":
                continue
            fl = (m.get("content") or "").split("\n", 1)[0]
            if fl.startswith("[失败签名]") and (
                f"| {sig}" in fl or sig[:48] in fl
            ):
                # #11-(c)：签名相撞时按首行 tool= 二次定位 —— 解法必须落到
                # 与本次成功调用**同工具**的条目上（否则是"错解"，比没有
                # 解更贵：_signature_has_solution 会认它并对全员广播）。
                if not _first_line_matches_tool(fl, tool_name):
                    continue
                target = m
                break
        if target is None:
            log.info(
                "failure_signature.backfill_no_entry",
                sig=sig[:60],
                tool=tool_name,
            )
            return False
        if not target.get("module_id"):
            # save_memory 的 upsert 键依赖 module_id —— 缺失会 INSERT 成新行
            log.warning("failure_signature.backfill_missing_module_id")
            return False
        content = target.get("content") or ""
        metadata = dict(target.get("metadata") or {})
        if _has_verified_solution_line(content):
            # 幂等跳过，不重复追加；但补齐状态位（历史条目可能只有解法行
            # 没有 solution_status —— 机检口径要求两者不许分叉）。
            if metadata.get("solution_status") != SOLUTION_STATUS_VERIFIED:
                metadata["solution_status"] = SOLUTION_STATUS_VERIFIED
                await memory_service.save_memory(
                    agent_id=_SIGNATURE_WRITER,
                    project_id=project_id,
                    scope="project",
                    content=content,
                    type="failure_signature",
                    module_id=target.get("module_id"),
                    source_agent_id=target.get("source_agent_id"),
                    metadata=metadata,
                )
            return True
        lines = content.splitlines()
        solution_line = f"{_SOLUTION_LINE_PREFIX} {solution.strip()}"
        insert_at = None
        for i, line in enumerate(lines):
            if line.startswith("根因提示:"):
                insert_at = i + 1
                break
        if insert_at is None:
            lines.append(solution_line)
        else:
            lines.insert(insert_at, solution_line)
        metadata["solved_at_ms"] = int(time.time() * 1000)
        metadata["solution_tool"] = tool_name or ""
        metadata["solution_status"] = SOLUTION_STATUS_VERIFIED
        await memory_service.save_memory(
            agent_id=_SIGNATURE_WRITER,
            project_id=project_id,
            scope="project",
            content="\n".join(lines),
            type="failure_signature",
            module_id=target.get("module_id"),
            source_agent_id=target.get("source_agent_id"),
            metadata=metadata,
        )
        log.info(
            "failure_signature.solution_backfilled",
            project_id=project_id,
            tool=tool_name,
            sig=sig[:60],
        )
        return True
    except Exception as e:
        log.warning("failure_signature.backfill_failed", error=str(e))
        return False

# ══════════════════════════════════════════════════════════════════
# #16-③ · 组织级升级（distinct_hitters 梯度）
# ══════════════════════════════════════════════════════════════════
#
# **病因**：`metadata.hit_count` 计的是**次数**不是**不同 agent 数**，且
# **无论撞几次只更新条目、从不通知** ⇒ 「N 人各撞一遍」在数据可见、行为
# 无反应 —— R7 = 50:17 / 51:11 就是后果。
#
# **判据来源（重要）**：DSH 的 `repeat-tool-reminder` chain 是
# **per-agent、会话内、WeakMap**，README 明写 "chains stay isolated per
# agent" ⇒ **它不做跨 agent，是因为它的 agents 不共享工作区与任务池**
# （那是它的边界，不是它的判断）。
# 我们有 **CEO → 中层 → 叶子** 的编制 + 共享一个项目与任务池 ⇒
# 「同一堵墙被 N 个不同 agent 各撞一遍」是**真实且可行动的组织级信号**。
#
# **可借用 / 不可借用**：
# - 梯度**形状**可借用（DSH 通用消息设计）：升序阈值数组、首档轻推不点名、
#   后档详细、**只在精确命中阈值时发、越顶静默**、幂等。
# - **聚合维度与受众必须自研**：计数维度 = `distinct_hitters`（不同 agent
#   集合，不是"同 agent 连续重复"）；受众 = 本项目全员 + CEO/上级。
#
# **投递**：走 `health_notice.deliver_notice` 的 platform-reserved 通道 ——
# 绝不 `result["error"] +=`（见 health_notice 模块 docstring 的三问）。

#: 组织级升级阈值（**升序**，必须保持升序 —— 有结构化断言守）。
#: 只在 ``distinct_hitters`` **精确等于**某一档时发；越过最高档静默。
DISTINCT_HITTERS_THRESHOLDS: tuple[int, ...] = (3, 5, 8)

#: `distinct_hitters` 集合上限（有界 —— 防 metadata 无界膨胀）。
#: 到顶后**不再新增**，但计数继续（`distinct_hitter_count` 记真实数）。
_MAX_DISTINCT_HITTERS = 32

#: metadata 里 distinct_hitters 的键名。
_HITTERS_KEY = "distinct_hitters"
_HITTERS_OVERFLOW_KEY = "distinct_hitter_count"
_ORG_ESCALATED_AT_KEY = "org_escalated_at_ms"
_ORG_ESCALATED_TIERS_KEY = "org_escalated_tiers"


def _assert_thresholds_ascending() -> None:
    """结构化断言：阈值必须升序且互不相同（import 期执行）。

    梯度靠"精确等于某档"判定；非升序会让"越顶静默"失去意义（后一档先于
    前一档触发），且 `==` 判定会跳过中间档。这条断言是**唯一**能阻止有人
    把 `(3, 5, 8)` 改成 `(5, 3, 8)` 的机制。
    """
    ts = DISTINCT_HITTERS_THRESHOLDS
    assert ts, "organization escalation thresholds must not be empty"
    assert all(isinstance(t, int) and t > 0 for t in ts), ts
    assert list(ts) == sorted(ts), f"thresholds must be ascending: {ts}"
    assert len(set(ts)) == len(ts), f"thresholds must be distinct: {ts}"


_assert_thresholds_ascending()


def merge_distinct_hitters(prev_meta: dict, agent_id: str) -> tuple[list[str], int]:
    """把本次撞到者并入 `distinct_hitters`；返回 ``(集合, 真实总数)``。

    集合**有界**（`_MAX_DISTINCT_HITTERS`）：到顶后不再新增，但真实总数
    记在第二个返回值里（`distinct_hitter_count`），阈值判定用真实数 ——
    这样"集合被截断"不会被误读成"撞的人变少了"。
    """
    raw = prev_meta.get(_HITTERS_KEY) or []
    hitters: list[str] = [str(h) for h in raw if str(h or "").strip()]
    overflow = int(prev_meta.get(_HITTERS_OVERFLOW_KEY) or 0)
    aid = (agent_id or "").strip()
    if aid and aid not in hitters:
        if len(hitters) < _MAX_DISTINCT_HITTERS:
            hitters.append(aid)
        else:
            overflow += 1
    # 真实总数 = 集合大小 + 被截断溢出的数量（去重后不可知，故只作为下界）
    return hitters, len(hitters) + overflow


def escalation_tier(distinct_count: int) -> int | None:
    """``distinct_hitters`` 精确命中哪一档；未命中返回 ``None``。

    **只在精确相等时返回** —— 这是"越顶静默"的实现：从 5 跳到 8 时，
    ``distinct_count==5`` 已发过，``==6/7`` 不命中，``==8`` 再发一次。
    """
    ts = DISTINCT_HITTERS_THRESHOLDS
    for idx, t in enumerate(ts):
        if distinct_count == t:
            return idx
    return None


def build_org_escalation_text(
    *,
    sig: str,
    tool_name: str,
    tier_idx: int,
    distinct_count: int,
    entry_hint: str = "",
) -> str:
    """构造组织级升级正文。

    **首档轻推不点名**（对齐 DSH 的 gentle 档：只说"有人在重复撞同一堵墙、
    先分析上次结果"），后档 detailed（点名工具、人数、并指向共享条目）。
    点名是个有代价的动作 —— 首档就点名会让叶子把平台提示读成"点名批评"，
    进而不读；后档人数已经说明这不是个人问题，点名才不伤信任。
    """
    ts = DISTINCT_HITTERS_THRESHOLDS
    if tier_idx == 0:
        return (
            f"【组织级信号】本项目的 agent 们正在重复撞同一堵墙 —— "
            f"已有 {distinct_count} 个不同 agent 撞到同一个失败。"
            "（这是**团队的**问题，不是某个人的。）"
            "在重试之前，先分析上一次的结果：同一写法原样重试不会通过。"
        )
    tail = f" 共享条目: {entry_hint}" if entry_hint else ""
    if tier_idx == len(ts) - 1:
        head = (
            f"【组织级信号 · 最高档】{distinct_count} 个不同 agent 反复撞同一堵墙"
        )
    else:
        head = f"【组织级信号】{distinct_count} 个不同 agent 撞同一堵墙"
    return (
        f"{head}（工具 {tool_name}，签名 {sig[:60]}）。"
        "这已不是个别 agent 的写法问题 —— 需要有人（协调者/CEO）决定"
        "是修平台、改流程，还是把这条路彻底封掉。"
        f"单靠各自换路重试已证明无效。{tail}"
    )


async def note_distinct_hitter(
    *,
    project_id: str | None,
    signature_key: str,
    tool_name: str,
    agent_id: str,
    entry_hint: str = "",
) -> str:
    """把撞到者并入条目的 `distinct_hitters`，命中梯度则返回**升级正文**。

    返回 ``""`` 表示本次不发（首撞/未命中档/已发过该档/条目不存在）。
    升级只改 metadata，**不改动条目正文** —— 它是通知，不是内容变更。

    幂等：``metadata.org_escalated_tiers`` 记录已发过的档位（列表），
    同档只发一次（进程重启后仍成立 —— 状态在 metadata 里，不在内存）。

    best-effort：任何异常只记日志返回 ""（绝不阻断工具执行）。
    """
    sig = (signature_key or "").strip()
    aid = (agent_id or "").strip()
    if not project_id or not sig or not aid:
        return ""
    try:
        from hiveweave.services.memory import MemoryService

        memory_service = MemoryService()
        target = None
        for m in (await memory_service.get_project_memories(project_id)) or []:
            if m.get("type") != "failure_signature":
                continue
            fl = (m.get("content") or "").split("\n", 1)[0]
            if fl.startswith("[失败签名]") and (
                f"| {sig}" in fl or sig[:48] in fl
            ):
                if not _first_line_matches_tool(fl, tool_name):
                    continue
                target = m
                break
        if target is None or not target.get("module_id"):
            return ""

        prev_meta = dict(target.get("metadata") or {})
        hitters, distinct_count = merge_distinct_hitters(prev_meta, aid)
        tier_idx = escalation_tier(distinct_count)
        already = [int(t) for t in (prev_meta.get(_ORG_ESCALATED_TIERS_KEY) or [])]
        fire = tier_idx is not None and tier_idx not in already

        metadata = dict(prev_meta)
        metadata[_HITTERS_KEY] = hitters
        metadata[_HITTERS_OVERFLOW_KEY] = max(0, distinct_count - len(hitters))
        if fire:
            metadata[_ORG_ESCALATED_TIERS_KEY] = already + [tier_idx]
            if not metadata.get(_ORG_ESCALATED_AT_KEY):
                metadata[_ORG_ESCALATED_AT_KEY] = int(time.time() * 1000)

        # 只在**有必要**时写库：撞到者已在集合里且没有新档要打 → 免写。
        _changed = (aid not in (prev_meta.get(_HITTERS_KEY) or [])) or fire
        if _changed:
            await memory_service.save_memory(
                agent_id=_SIGNATURE_WRITER,
                project_id=project_id,
                scope="project",
                content=target.get("content") or "",
                type="failure_signature",
                module_id=target.get("module_id"),
                source_agent_id=target.get("source_agent_id"),
                metadata=metadata,
            )

        if not fire:
            return ""
        log.info(
            "failure_signature.org_escalated",
            project_id=project_id,
            sig=sig[:60],
            tool=tool_name,
            tier=tier_idx,
            distinct=distinct_count,
        )
        return build_org_escalation_text(
            sig=sig,
            tool_name=tool_name,
            tier_idx=tier_idx,
            distinct_count=distinct_count,
            entry_hint=entry_hint,
        )
    except Exception as e:  # noqa: BLE001 — 升级通知 best-effort
        log.warning("failure_signature.org_escalation_failed", error=str(e))
        return ""
