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
                        中段替换未伴随摘要变更；末位单点失配见
                        ``tail_hint_drift``）
- ``tail_hint_drift``    末位单点失配且 history 仍在增长 —— 本仓实测的
                        exit_hint 假阳签名（发送版末位 user 带 exit_hint、
                        落库版不带 ⇒ 不一致点恰在 len(prev)-1），单列分类
                        避免与真·中段改写混报
- ``prefix_stable``     前缀全对齐（前缀命中条件全部满足）

最终分类（``final``，report_cache_readout 输出，联合首请求 usage）：

- ``hit_ok``                 命中率 ≥ ``_HIT_OK_MIN_RATIO``（真命中）
- ``near_zero_hit``          cache_read > 0 但命中率低于阈值（形式命中，2026-09-22
                             增，report TEST_DSH_66 Q2：旧判据 `> 0` 把 77 条
                             cache_read≈113 / input≈4.9万 判绿）
- ``cold_start``             **没有可读的缓存域**（no_baseline / model_changed）
                             ⇒ cache_read=0 是必然，非平台责任（2026-09-12 新增，
                             report TEST_DSH_54 #6：此前与 drift 混成一档）
- ``cache_window_expired``   前缀对齐但 cache_read=0 → provider 缓存窗口
                             过期/驱逐（滑动窗口约 5-10min，平台侧不可修
                             ——gap_s 用于判断是否超出窗口）
- ``drift_zero_hit``         前缀**真漂移**且 cache_read=0 → 漂移实锤，平台侧可修

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
#: P1-1②另半：**本 run 内是否见过前缀漂移**（粘性；`reset_probe` 清）。
#: 为什么需要它：探针只对比**相邻 run 的首请求** ⇒ run 内部（prune/摘要改写之后）的
#: 漂移从不进任何桶；于是「本 run 自己动的刀」造成的零命中会被归成
#: `cache_window_expired`（**平台侧不可修**）—— 根因判错，正是本条要治的病。
_inner_drift: dict[str, dict[str, Any]] = {}

#: Q2（TEST_DSH_66）：`hit_ok` 的**命中率下界**（比例判据，不是存在性判据）。
#: 判据 = cache_read / (input_tokens + cache_read)。
#:
#: ⚠ 分母口径（**审计 A1 复核实证，2026-09-22**）。判据写死 `input + cache_read`
#: 的前提是「落库的 `input_tokens` 是**未命中桶**」，此前提已逐环验证：
#:   ① 写路径必经 `llm/util.normalize_usage`（`streamer/tool_loop.py:725`），
#:      对非 anthropic 且 `cache_read>0` 走 `input = prompt_tokens - cache_read`
#:      （`util.py:156-159`）—— 实测 `{input_tokens:133036, cached:113}` ⇒
#:      归一化为 `input=132923, cache_read=113`，确实减过。
#:   ② `total_tokens = input + output`（**1456/1456 行**成立，且
#:      `cache_read` 不进 total）—— 独立佐证 input 已剥离命中。
#:   ③ 故现场 `cache_read > input` 占 94%（1354/1456）**不是**口径异常，
#:      而是「长稳定前缀 + 少量新增」的正常签名（input p50=875 对
#:      cache_read p50≈50k）。⚠ 曾据此把口径判为双计，方向是反的。
#: ⇒ 已有同族工具 `util.cache_hit_percent(input, cr, cw, input_inclusive=)`
#:    用于 **DeepSeek 系**（`provider_input_inclusive` 为真时 input 即分母）。
#:    本探针只面对 `openai-responses`（现场 1456/1456），故不退化为通用调用；
#:    **若将来接入 input-inclusive 的 provider，此处必须改用该工具**。
#:    （「两处同源」的正例：`regression_check.py:299` 的 R3 用 `i+cr+cc`，
#:     与本判据同口径。）
#:
#: ⚠ 审计 ②-5 后由 0.5 下调为 0.05（2026-09-22）。依据现场库全量分布
#: （1441 条 cache_read>0 的请求，TEST_DSH_66）——**存在一个巨大的空档**：
#:   * 目标病灶 77 条：ratio < 0.05（cr 恒为 113，input 10k~133k）；
#:     其中 ratio ∈ [0.0008, 0.0016] 11 条、∈ (0.0016, 0.0112] 66 条
#:   * 次低的一群从 **0.2332** 起（16497/54236，真实的部分命中）
#:   * 0.0112 ~ 0.2332 之间 **一条都没有**（实测 0 条）
#: ⇒ 阈值取在这个空档里的任何值都同样能摘出 77 条；取 0.5 会**额外**把
#: 0.23~0.50 的 **10** 条真实部分命中误判成 `near_zero_hit`
#: （实测：0.5 ⇒ near_zero 87 条；0.05 ⇒ 77 条，差 10 条）。
#: 取 0.05 落在空档下沿：紧贴病灶群上界（0.0112）留 4.5 倍余量，
#: 同时不碰任何真实命中。
#: 这也是本仓纪律「阈值要落在**数据空档**里，而不是落在'看起来像一半'的
#: 整数上」——0.5 是猜的，0.05 是量出来的。
#: ⚠ **0.05 是单次事故拟合的**：若后续分布变化（空档消失），应重新按分布取，
#: 不要照抄这个数。
_HIT_OK_MIN_RATIO = 0.05


def reset_probe(agent_id: str | None = None) -> None:
    """清空探针基准（测试隔离用）。agent_id 为 None 时全量重置。"""
    if agent_id is None:
        _last_request.clear()
        _last_verdict.clear()
        _inner_drift.clear()
    else:
        _last_request.pop(agent_id, None)
        _last_verdict.pop(agent_id, None)
        _inner_drift.pop(agent_id, None)


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
    model_key: str | None = None,
    now: float | None = None,
    slot: str = "run_first",
) -> dict[str, Any]:
    """与该 agent 上次 run 首请求指纹对比，更新基准，返回漂移分类。

    在每次 run 的首请求组装完成后调用（`_run_llm` 中 `_build_messages`
    之后）。返回 dict 可直接展开进结构化日志。

    ``slot``（P1-1②另半，2026-09-21）：``"run_first"``（默认，run 首请求）·
    ``"run_inner"``（**run 内每次请求**，由 `tool_loop` 发起）。区别在于**记账去向**：
    - ``run_first``：写 `_last_verdict`（供 `report_cache_readout` 一次性消费）；
    - ``run_inner``：**不覆盖**首请求的 verdict，只在**真漂移**时把粘性标记
      `_inner_drift` 立起来 ⇒ 让「本 run 自己改写过前缀」这件事能被归因。
    ⚠ `model_key` 在 run 内路径可为 None ⇒ 复用基准里的那个（streamer 拿不到 model_key）。
    """
    prev = _last_request.get(agent_id)
    if model_key is None:
        model_key = str((prev or {}).get("model_key") or "")
    fp = fingerprint_messages(messages, model_key=model_key)
    if now is not None:
        fp["ts"] = now
    if slot == "run_first" or prev is None:
        _last_request[agent_id] = fp

    if prev is None:
        verdict: dict[str, Any] = {
            "verdict": "no_baseline",
            "drifts": [],
            "gap_s": None,
            "prev_dialog_len": None,
            "dialog_len": fp["dialog_len"],
            "first_mismatch_index": None,
        }
        if slot == "run_first":
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
    first_mismatch_index: int | None = None
    if prev_d != cur_d[: len(prev_d)]:
        # 上次首请求的对话主体未原样作为本次前缀重现 —— 中段被改写
        # 或上 run 的对话未正常落库追加。
        # issue-5 §3.2 ④(b)：算出**首个不一致位置**。本仓库实测的
        # `history_rewritten` 假阳签名是「不一致点恒在末位」
        # （发送版 user 消息带 exit_hint、落库版不带 ⇒ len(prev_d)-1）。
        # 落了这个下标就能一眼区分「末位单点 ≠ 中段改写」，不必再靠推理。
        limit = min(len(prev_d), len(cur_d))
        first_mismatch_index = limit  # cur 比 prev 短 ⇒ 在 cur 耗尽处首次不一致
        for i in range(limit):
            if prev_d[i] != cur_d[i]:
                first_mismatch_index = i
                break
        # L7-c（TEST_DSH_62 观测批）：末位单点且 history 仍在增长 ⇒ exit_hint
        # 假阳签名，改报 tail_hint_drift 与真·中段改写分流。增长判据是签名
        # 的固有特征：假阳场景下 run2 的 history 必然 ⊇ run1 dialog（追加
        # 了 assistant 回复与新 user）⇒ len(cur) > len(prev)；对话蒸发 /
        # 截断（cur 不比 prev 长）即便失配落在末位也是真丢失，仍报
        # history_rewritten —— 那正是探针要抓的观测价值。
        if (
            first_mismatch_index == len(prev_d) - 1
            and len(cur_d) > len(prev_d)
        ):
            drifts.append("tail_hint_drift")
        else:
            drifts.append("history_rewritten")

    verdict = {
        "verdict": "+".join(drifts) if drifts else "prefix_stable",
        "drifts": drifts,
        "gap_s": round(max(0.0, fp["ts"] - prev["ts"]), 1),
        "prev_dialog_len": prev["dialog_len"],
        "dialog_len": fp["dialog_len"],
        "first_mismatch_index": first_mismatch_index,
    }
    if slot == "run_first":
        _last_verdict[agent_id] = verdict
    elif drifts:
        # 本 run 内**见过**漂移 ⇒ 立粘性标记（供 `report_cache_readout` 归因），
        # **不覆盖**首请求的 verdict（那个必须留给「相邻 run 对比」的语义）。
        _inner_drift[agent_id] = verdict
    return verdict


def report_cache_readout(
    agent_id: str,
    *,
    input_tokens: int,
    cache_read: int | None,
    cache_creation: int,
) -> dict[str, Any] | None:
    """首请求 usage 回读后调用（`token_meter.record_rounds` 之后）。

    将请求前指纹 verdict 与首请求 cache_read 联合，合成可行动的最终
    分类并输出结构化日志。verdict 为**一次性消费**（读取后清除）：
    空响应重试循环不会用同一指纹重复报告；无基准或 verdict 已消费时
    返回 None。

    分类（2026-09-12 增 `cold_start`，report TEST_DSH_54 #6；
    2026-09-22 增 `near_zero_hit` 阈，report TEST_DSH_66 Q2）：

    - ``hit_ok``                 命中率 ≥ `_HIT_OK_MIN_RATIO`（真命中）
    - ``near_zero_hit``          cache_read > 0 但命中率 < 阈值 —— **形式命中、
                                 实质未命中**（provider 只回了极少量前缀）
    - ``cold_start``             **没有可读的缓存域**（`no_baseline` 首次
                                 run / `model_changed` 换了缓存域）——
                                 cache_read=0 是**必然**，与平台无关
    - ``cache_window_expired``   前缀对齐但 cache_read=0 → provider 缓存窗口
                                 过期/驱逐（平台侧不可修）
    - ``drift_zero_hit``         前缀**真漂移**且 cache_read=0 ⇒ 平台侧可修
    - ``unknown_usage``          usage 里**没有** cache_read（None）⇒ 未知，**不得**记成漂移
                                 （P1-1②：None ≠ 0；旧判据会把"没这个数"读成"零命中"）

    ⚠⚠ **Q2（TEST_DSH_66）：为什么 `cache_read > 0` 不足以判绿**。
    旧判据是**存在性**判据（"有任何一个 token 命中即 OK"），它对这个仓的
    真实故障形态**完全失效**：实测 77 条请求 `cache_read` 落在 1..500、
    而 `input_tokens` 合计 **3,793,790**（均值 ~4.9 万/条）—— 命中率
    量级 **0.2%~1%**，等于**没命中**，却被 77/77 全判 `hit_ok`
    （`GROUP BY r.cache_verdict` 只有一档）。判决与证据自相矛盾。
    ⇒ 判据必须是**比例**（命中 ÷ 可命中总量），不是存在性。

    阈值取向：`_HIT_OK_MIN_RATIO`（当前 **0.05**）。判据本身是「命中 ÷
    可命中总量」，阈值只决定分档边界。⚠ 该值**由实测空档定**，不是拍的
    （审计 ②-5）：现场 1441 条 `cache_read>0` 的分布里存在一个巨大空档 ——
    目标病灶 77 条 ratio ∈ [0.00085, 0.0112]，次低的一群从 **0.2332** 起
    （那是**真实的部分命中**），两者之间**一条都没有**。阈值落在这个空档
    内即可；取 0.05（略高于病灶上界）留出余量，且**不再误伤** 0.23~0.47
    那 10 条真实部分命中（0.5 会把它们错划进近零档）。
    ⇒ 若后续分布变化（空档消失），这个值应当**重新按分布取**，而不是照抄。

    为什么必须把 cold_start 单列：TEST_DSH_54 的 15 个 drift_zero_hit 里
    有 8 个落在各 Agent 首次活动窗口（12:03–12:50）—— 那里的 cache_read=0
    是必然（还没有缓存可读），判词却写「平台改写了前缀，可修」，会把排查
    引向错误根因。`no_baseline` / `model_changed` 此前都落进 drift 桶。
    """
    last = _last_verdict.pop(agent_id, None)
    if last is None:
        return None
    verdict_str = str(last.get("verdict") or "")
    # 可命中总量 = 本次请求读了 input_tokens + 其中已命中的 cache_read。
    # 只除 input 会让 ratio 在"命中被算进 input"与"不算进去"两种 provider
    # 口径下漂移，故显式把两者都算作分母（保守取大 ⇒ 宁可少判 green）。
    cacheable = max(0, int(input_tokens or 0)) + max(0, int(cache_read or 0))
    ratio = (cache_read / cacheable) if (cache_read and cacheable) else 0.0
    if cache_read is None:
        # ⭐ P1-1②：**未知 ≠ 零命中**（与 `executed` 同族纪律：None 不得与 0 混同）。
        # usage 里没有 cache_read（provider 不回 / 解析失败）时，旧判据
        # `cache_read and cache_read > 0` 会落进下面的 else 链 ⇒ 被记成
        # `drift_zero_hit`「平台改写了前缀、可修」—— 把排查引向错误根因
        #（与 cold_start 单列同理：TEST_DSH_54 里 8/15 条 drift_zero_hit 就是这么来的）。
        final = "unknown_usage"
    elif cache_read > 0 and ratio >= _HIT_OK_MIN_RATIO:
        final = "hit_ok"
    elif cache_read > 0:
        # 形式命中、实质未命中（Q2）：**不进**下面的 0 命中分支 ——
        # 那三档（cold_start / cache_window_expired / drift_zero_hit）
        # 都以 "cache_read == 0" 为语义前提，把 113 判成它们同样失真。
        # 单列一档才能同时保住「有字节回来」与「量级不对」两个事实。
        final = "near_zero_hit"
    elif "no_baseline" in verdict_str or "model_changed" in verdict_str:
        # 无基准 / 换缓存域 ⇒ 没有可读的缓存，零命中是必然而非漂移
        final = "cold_start"
    elif verdict_str == "prefix_stable":
        # ⭐ P1-1②另半：本 run 内见过漂移（prune/摘要改写过前缀）⇒ 零命中的成因是
        # **我们自己动的刀**，不是「缓存窗口过期（平台侧不可修）」。
        # 旧判据只看首请求的 verdict ⇒ 这类 run 内漂移永远进不了 `drift_zero_hit` 桶
        # （这就是「探针只报 2 条」的原因）。
        final = (
            "drift_zero_hit"
            if _inner_drift.get(agent_id)
            else "cache_window_expired"
        )
    else:
        final = "drift_zero_hit"
    result: dict[str, Any] = {
        **last,
        "final": final,
        "input_tokens": input_tokens,
        "cache_read": cache_read,
        "cache_creation": cache_creation,
        "cache_hit_ratio": round(ratio, 4),
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
