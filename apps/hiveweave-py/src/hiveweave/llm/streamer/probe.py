"""P1-3 Phase 0 · prompt prefix drift probe（纯观测，零行为影响）。

来源：r2/r3 平台问题报告——全场命中率 87.44% < 95% 红线，47.4%（27/57）的
run 首请求 ``cache_read=0``，零命中 input 合计 1,742,881 tokens。R3 复核根因：
**run 边界 = 缓存边界**。本探针对同一 agent 相邻两次 run 的首请求 messages
做逐段指纹对比，把「首请求零命中」分化为可行动的漂移源分布。

漂移分类（``verdict``，compare_and_record 输出）：

- ``no_baseline``       无对比基准（agent 首个 run / 后端重启后首个 run）
- ``model_changed``     ``model_id@base_url`` 变化（换缓存域；不同模型本无
                        前缀可比，属配置责任，仅记录）
- ``identity_drift``    System1 身份段字节变化（异常——identity 应纯静态）
- ``compacted_drift``   compacted 摘要段变化（伴随 compaction/prune 属预期
                        ——摘要只在压缩触发时变更；无压缩事件时异常）
- ``history_rewritten`` 上次 history 不是本次 history 的前缀（中段被改写
                        ——append-only 纪律被破坏，或 prune/compaction
                        中段替换未伴随摘要变更）
- ``prefix_stable``     前缀全对齐（前缀命中条件全部满足）

最终分类（``final``，report_cache_readout 输出，联合首请求 usage）：

- ``hit_ok``                 首请求 cache_read > 0（命中）
- ``cache_window_expired``   前缀对齐但 cache_read=0 → provider 缓存窗口
                             过期/驱逐（滑动窗口约 5-10min，平台侧不可修
                             ——gap_s 用于判断是否超出窗口）
- ``drift_zero_hit``         有漂移且 cache_read=0 → 漂移实锤，平台侧可修

设计约束：

- 纯内存 per-agent 基准，不落库、不阻塞主链路（调用方 best-effort）。
  重启后基准自然丢失（重启本身也使 provider 缓存失效，no_baseline 即
  重启场景的豁免表达）。
- **前缀包含而非全等**：相邻 run 间 history 必然增长（上一 run 的对话
  已 append 进库），全等判据永远不成立。缓存命中的正确判据是「上次
  首请求的 history 序列在本次请求中原样作为前缀重现」。
- hash 用 sha256 截断 16 hex（观测指纹，非安全用途）。
- 对比基准按消息序列而非段字节：system 身份/摘要段单独 hash；对话主体
  （全部非 system 消息）逐条 hash 后做**前缀包含**判定。布局约定见
  ``agents/agent.py:_build_messages``：
  ``[System1][System compacted?][history...][System2?][user...]``。
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# per-agent 上次首请求指纹基准与上次 verdict（探针运行态，纯内存）
_last_request: dict[str, dict[str, Any]] = {}
_last_verdict: dict[str, dict[str, Any]] = {}


def reset_probe(agent_id: str | None = None) -> None:
    """清空探针基准（测试隔离用）。agent_id 为 None 时全量重置。"""
    if agent_id is None:
        _last_request.clear()
        _last_verdict.clear()
    else:
        _last_request.pop(agent_id, None)
        _last_verdict.pop(agent_id, None)


def _h(data: Any) -> str:
    """sha256 截断 16 hex —— 观测指纹。"""
    if isinstance(data, str):
        raw = data.encode("utf-8")
    else:
        raw = json.dumps(
            data, sort_keys=True, ensure_ascii=False, default=str
        ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _split_segments(
    messages: list[dict],
) -> tuple[str, str, list[dict]]:
    """提取前缀敏感段。

    前缀命中只由两类内容决定：
    - system 身份/摘要段：S1 = 首个 system；compacted = 紧跟 S1 的 system
      （仅当其不是尾部最后一个 system——尾部最后一个 system 是 System2
      context 动态段，history 为空时 C/S2 位置不可区分，保守不对比以
      避免把 S2 的逐轮正常变化误报为 compacted_drift；该漏报仅在
      「history 空且有摘要」的窄角下发生，此时本无前缀可比）。
    - 对话主体（dialog）：全部非 system 消息的逐条序列。上一 run 的
      user/assistant/tool 消息会 append 进本次 history（system 被
      append_turn 过滤），故「上次首请求的 dialog 序列是本次 dialog
      序列的前缀」即前缀包含不变式的正确判据——history 与本次 user
      消息在 role 上不可区分，无需（也无法）切出 history 边界。

    System2 等尾部 system 变化不影响前缀命中，不参与对比。
    """
    sys_idx = [
        i for i, m in enumerate(messages) if m.get("role") == "system"
    ]
    s1_hash = _h(messages[sys_idx[0]].get("content") or "") if sys_idx else ""
    compacted_hash = ""
    if (
        len(sys_idx) >= 2
        and sys_idx[1] == 1
        and sys_idx[1] != sys_idx[-1]
    ):
        compacted_hash = _h(messages[sys_idx[1]].get("content") or "")
    dialog = [m for m in messages if m.get("role") != "system"]
    return s1_hash, compacted_hash, dialog


def fingerprint_messages(
    messages: list[dict], *, model_key: str
) -> dict[str, Any]:
    """计算首请求指纹（纯函数，无副作用）。"""
    s1_hash, compacted_hash, dialog = _split_segments(messages)
    return {
        "model_key": model_key,
        "identity_hash": s1_hash,
        "compacted_hash": compacted_hash,
        "dialog_hashes": [_h(m) for m in dialog],
        "dialog_len": len(dialog),
        "ts": time.time(),
    }


def compare_and_record(
    agent_id: str,
    messages: list[dict],
    *,
    model_key: str,
    now: float | None = None,
) -> dict[str, Any]:
    """与该 agent 上次 run 首请求指纹对比，更新基准，返回漂移分类。

    在每次 run 的首请求组装完成后调用（`_run_llm` 中 `_build_messages`
    之后）。返回 dict 可直接展开进结构化日志。
    """
    fp = fingerprint_messages(messages, model_key=model_key)
    if now is not None:
        fp["ts"] = now
    prev = _last_request.get(agent_id)
    _last_request[agent_id] = fp

    if prev is None:
        verdict: dict[str, Any] = {
            "verdict": "no_baseline",
            "drifts": [],
            "gap_s": None,
            "prev_dialog_len": None,
            "dialog_len": fp["dialog_len"],
        }
        _last_verdict[agent_id] = verdict
        return verdict

    drifts: list[str] = []
    if prev["model_key"] != fp["model_key"]:
        drifts.append("model_changed")
    if prev["identity_hash"] != fp["identity_hash"]:
        drifts.append("identity_drift")
    # compacted 仅在两侧都被识别为独立摘要段时对比：识别状态翻转
    # （如 run1 无 S2 使 C 被排除、run2 出现 S2 后同一 C 被计入）是
    # 切分歧义而非真实漂移，不报。摘要从无到有（首次压缩）伴随
    # history 截断，由 history_rewritten + context marker 组合呈现。
    if (
        prev["compacted_hash"]
        and fp["compacted_hash"]
        and prev["compacted_hash"] != fp["compacted_hash"]
    ):
        drifts.append("compacted_drift")
    prev_d = prev["dialog_hashes"]
    cur_d = fp["dialog_hashes"]
    if prev_d != cur_d[: len(prev_d)]:
        # 上次首请求的对话主体未原样作为本次前缀重现 —— 中段被改写
        # 或上 run 的对话未正常落库追加。
        drifts.append("history_rewritten")

    verdict = {
        "verdict": "+".join(drifts) if drifts else "prefix_stable",
        "drifts": drifts,
        "gap_s": round(max(0.0, fp["ts"] - prev["ts"]), 1),
        "prev_dialog_len": prev["dialog_len"],
        "dialog_len": fp["dialog_len"],
    }
    _last_verdict[agent_id] = verdict
    return verdict


def report_cache_readout(
    agent_id: str,
    *,
    input_tokens: int,
    cache_read: int,
    cache_creation: int,
) -> dict[str, Any] | None:
    """首请求 usage 回读后调用（`token_meter.record_rounds` 之后）。

    将请求前指纹 verdict 与首请求 cache_read 联合，合成可行动的最终
    分类并输出结构化日志。verdict 为**一次性消费**（读取后清除）：
    空响应重试循环不会用同一指纹重复报告；无基准或 verdict 已消费时
    返回 None。
    """
    last = _last_verdict.pop(agent_id, None)
    if last is None:
        return None
    if cache_read and cache_read > 0:
        final = "hit_ok"
    elif last["verdict"] == "prefix_stable":
        final = "cache_window_expired"
    else:
        final = "drift_zero_hit"
    result: dict[str, Any] = {
        **last,
        "final": final,
        "input_tokens": input_tokens,
        "cache_read": cache_read,
        "cache_creation": cache_creation,
    }
    log = logger.bind(agent_id=agent_id)
    log.info(
        "prompt_prefix_probe_result",
        **result,
    )
    return result


def clear_verdict(agent_id: str) -> None:
    """清除该 agent 待消费的 verdict（compare 阶段失败时由接线方调用）。

    防止 compare 失败后 report_cache_readout 用上一 run 的陈旧 verdict
    与本次 cache_read 合成错误分类。
    """
    _last_verdict.pop(agent_id, None)
