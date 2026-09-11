"""同因连拒记忆（45 轮 P1「拒绝无记忆」）。

45 轮实锤：同一 agent 对同一形态的拒绝连撞（账本拒 2×/50 秒同文案、
降级终验拒 3×（两条出路已给仍连撞）、edit_file 同文件 4 败夹 3 成）——
每次拒绝都从活状态现拼文案，agent 看不到「这已经是第 N 次」，于是原样
重试。本模块提供进程内 per-签名 计数：第 2 次起在拒绝文案上追加事实位
标注，逼模型换路。

- 键 = (agent_id, ``failure_signature.signature_of(error_text)``)（空白归一
  前缀，与 F10 失败签名同源；三类拒绝文案均超 24 字符门槛）。签名含上下文
  （gate code / 文件路径），天然按因计数——不同 gate、不同文件各算各的；
  agent_id 并键防跨 agent 误标（patch 的 edit_file 点拿不到 agent_id，
  退化为进程级共享，见函数 docstring）。
- 进程内内存即可：连拒发生在 50 秒~turn 级窗口，重启丢失无碍，不建表。
- 容量有界：超 ``_MAX_KEYS`` 淘汰最旧一半，防长驻进程病态增长。
"""

from __future__ import annotations

import threading
import time

from hiveweave.services.failure_signature import signature_of

_MAX_KEYS = 500
_MAX_LAST_ERROR = 120

_lock = threading.Lock()
_counts: dict[str, dict] = {}
# entry: {count, tool, last_error, last_ts}


def repeat_rejection_notice(
    tool_name: str, error_text: str, agent_id: str | None = None
) -> str:
    """登记一次拒绝；返回**提示正文**（首次拒绝返回 ""）。

    ⚠️ **与 ``annotate_repeat_rejection`` 的区别（批次 4 附项 2026-09-11）**：
    本函数返回的是**独立通道的正文**（不带前导换行），调用方应把它交给
    ``services.health_notice.deliver_notice``，而**不是**拼进工具回执的
    ``error``。把提示拼进 `error` 会让回执对「工具返回了什么」撒谎
    （DSH 设计笔记 2026-07-08-repeat-tool-guard.md:58 明确否决），
    且与真错误同格 ⇒ 被习得性跳读（R7 恶化项的机制）。
    ``annotate_repeat_rejection`` 是**旧接口**，仍返回 `"\\n\\n" + 正文`
    的形式以便尚未迁移的调用点保持行为不变；新代码一律用本函数。

    计数键 = (agent_id, 签名前 160 字符)。签名含上下文（gate code / 文件
    路径），天然按因计数；agent_id 并入键防跨 agent 误标（「已第 N 次」
    是对当前 agent 的事实陈述，audit P2-1）。patch 的 edit_file 调用点
    拿不到 agent_id，退化为进程级共享（签名含文件路径，误标面小）。

    第 2 次起返回形如::

        [REPEAT REJECTION #2 via submit_task] 同因拒绝已第 2 次。
        上次拒绝摘要: …。同一写法反复被拒说明改写无效——按上文
        RETRY[...] 标记或出路步骤换路执行，勿原样重试。
    """
    sig = signature_of(error_text or "")
    if not sig:
        return ""
    key = f"{agent_id or ''}|{sig}"
    now = time.time()
    with _lock:
        entry = _counts.get(key)
        if entry is None:
            if len(_counts) >= _MAX_KEYS:
                stale = sorted(
                    _counts.items(), key=lambda kv: kv[1]["last_ts"]
                )[: _MAX_KEYS // 2]
                for k, _ in stale:
                    del _counts[k]
            entry = {
                "count": 0,
                "tool": tool_name,
                "last_error": "",
                "last_ts": now,
            }
            _counts[key] = entry
        entry["count"] += 1
        prev_error = entry["last_error"]
        entry["last_error"] = (error_text or "")[:_MAX_LAST_ERROR]
        entry["last_ts"] = now
        count = entry["count"]
    if count < 2:
        return ""
    prev = f"上次拒绝摘要: {prev_error}。" if prev_error else ""
    return (
        f"[REPEAT REJECTION #{count} via {tool_name}] 同因拒绝已第 "
        f"{count} 次。{prev}同一写法反复被拒说明原样改写无效——"
        "按上文 RETRY[...] 标记或出路步骤换路执行，勿原样重试。"
    )


def annotate_repeat_rejection(
    tool_name: str, error_text: str, agent_id: str | None = None
) -> str:
    """**旧接口**：登记一次拒绝并返回可 `+=` 到文案上的标注（首次返回 ""）。

    新代码请改用 ``repeat_rejection_notice`` + `health_notice.deliver_notice`
    —— 见该函数 docstring 的通道说明。本函数保留给小范围、拿不到 agent_id、
    且提示必须与拒绝文案同处的场景（如 `edit_file` 返回裸字符串）。
    """
    notice = repeat_rejection_notice(tool_name, error_text, agent_id=agent_id)
    return f"\n\n{notice}" if notice else ""


def rejection_count(
    error_text: str, agent_id: str | None = None
) -> int:
    """只读查询当前同因计数（测试/遥测用）。"""
    sig = signature_of(error_text or "")
    if not sig:
        return 0
    key = f"{agent_id or ''}|{sig}"
    with _lock:
        entry = _counts.get(key)
        return int(entry["count"]) if entry else 0


def reset_for_tests() -> None:
    with _lock:
        _counts.clear()
