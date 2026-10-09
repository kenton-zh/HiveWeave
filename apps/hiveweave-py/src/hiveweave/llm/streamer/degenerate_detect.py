"""N3 批28-2 施工1（2026-10-08）：流内退化检测器（自研，无上游参照）。

病灶实锤（第六轮）：
- failover 免费档模型输出坍塌 —— Robert 两次 out=128000 全是 token soup
  （「Hmm. OK. Let's act…OK. Hmm.」循环，thinking 巨块 470k 字符）；
- DSH 星野把 ``</longcat_tool_call>`` 标签原文当正文连吐。

平台此前对流内**内容**零刹车：idle/首块看门狗只看「有没有 chunk」，
不看 chunk 内容是否坍塌 —— 失控流可以稳定吐 chunk 烧满 128k 输出
（20+ 分钟）后正常收口，lease/预算全部越界（批28-2 施工2 的病灶）。

设计：**流式累积检测器** —— 对 assistant 正文/thinking 的增量 delta 维护
~2k 字符滑动窗口，纯函数式判定（无 IO、每次 feed O(窗口)），命中返回
退化类别，由 http_stream 经批28-1 的 abort 管道掐流（aborted ⇒ run 按
interrupted 语义收口）。与 length 恢复的协同：退化 abort 优先于 length
帽 —— 失控流在烧满输出上限**之前**就该被掐（tool_loop 的 aborted 分支
排在 length 分支之前，结构上已保证）。

判据（两类）：
- **a) 短语循环（loop）**：总累积 ≥ ``MIN_TOTAL_CHARS`` 后，取窗口尾部
  ``TAIL_CHARS``（200）字符 tail，两选一命中：
  1. ``tail == pattern*k``：pattern ≤ ``PATTERN_MAX_CHARS``（16）字符且
     重复 k ≥ ``LOOP_MIN_REPEATS``（8）次（tail 长 200 时 p≤16 结构上
     已保证 k ≥ 12.5 ——「Hmm. Hmm. …」式短单元循环）；
  2. tail 在窗口内（重叠）出现 ≥ ``LOOP_TAIL_MIN_OCCURRENCES``（5）次
     （覆盖 >16 字符的长单元循环，如「Hmm. OK. Let me act…」整句循环；
     用**重叠**计数：循环单元（如 42 字符）短于 needle（200）时，
     非重叠 count 会把同一循环段里的出现折叠掉 —— 纯周期文本的非重叠
     计数 ≈ N/200 而重叠 ≈ N/周期，后者才是「出现次数」的直觉语义）。
     达阈值即短路（最多 5 次 C 级 find，热路径开销可忽略）。
- **b) 标签泄漏（tag_leak）**：窗口内出现「XML 工具标签原文」
  （``</longcat_tool_call>`` / ``</:tool-call>`` 等）≥ ``TAG_LEAK_MIN_HITS``
  （2）次 —— 工具标签属于结构化 tool_calls 字段，正文里原文连吐 =
  上游解析层已失守（DSH 星野形态）。在**窗口**内计数而非增量计数：
  标签可能被网络分片切成两个 delta，窗口计数天然自愈。

误伤边界（判据为什么打不到正常输出）：
- 暖机门槛 4k：总累积（正文+thinking 合计）不足 4k 字符不检测 —— 正常
  短回复零成本，也不会被开头重复的修辞（排比/感叹）误伤；
- a-1 要求 tail **整段** 被 ≤16 字符单元精确平铺（整除 + 逐字相等）：
  自然语言/代码的尾部 200 字符恰好构成单一短周期整倍长的概率近零
  （良性重复 —— 分隔线、缩进、括号闭合 —— 有相位/长度噪声，不满足
  精确平铺；纯单字符长跑如 200 个换行本身即是退化形态，掐掉合理）；
- a-2 要求 2k 窗口内 ≥5 次逐字重复**同一** 200 字符块（≥1k 字符纯
  复写）：正常代码/文档里的重复单元（import 块、测试参数表、正则量词
  ``{2,}`` 列举）要么单元 <200 字符、要么带变化（名字/数字/相位不同），
  不构成同一 200 字符 needle 的 5 连出现。已知残余边界：≥2k 字符的
  **常数平铺**（如全零矩阵 dump「0, 0, 0…」）会被 a-2 命中 —— 平台
  本就把巨型常量 dump 视为病理输出，且阈值 env 可调/可一键关闭；
- b 的标签是厂商私有 wire 格式，正常正文一次都不该出现；
- 防御纵深：``HIVEWEAVE_DEGENERATE_DETECT=0`` 总开关一键关闭（默认开）；
  ``HIVEWEAVE_DEGENERATE_LOOP_REPEATS`` 调高循环判据的最低重复次数
  （调到 >12 时等价于收窄 a-1 的 pattern 长度上限，更保守）。

per-request 一次：命中即闩锁（``feed`` 冻结，后续 delta 一律 None）——
abort 后本请求结束（tool_loop aborted 分支收口），无需再检。
"""
from __future__ import annotations

import os

#: 退化类别（feed 返回值 / abort reason 后缀）。
DEGENERATE_KIND_LOOP = "loop"
DEGENERATE_KIND_TAG_LEAK = "tag_leak"

WINDOW_CHARS = 2048
"""滑动窗口字符数（~2k）：判据 a-2 的出现计数范围。"""

MIN_TOTAL_CHARS = 4096
"""暖机门槛：总累积（正文+thinking）不足此数不检测（正常短输出零误伤）。"""

TAIL_CHARS = 200
"""循环判据取样的尾部长度。"""

PATTERN_MAX_CHARS = 16
"""a-1 循环单元的长度上限（「短 token 序列」）。"""

LOOP_MIN_REPEATS = 8
"""a-1 循环单元的最低重复次数（env ``HIVEWEAVE_DEGENERATE_LOOP_REPEATS``）。"""

LOOP_TAIL_MIN_OCCURRENCES = 5
"""a-2 tail 在窗口内的最低出现次数。"""

TAG_LEAK_MIN_HITS = 2
"""标签泄漏判据的最低出现次数。"""

LEAKED_TOOL_TAGS: tuple[str, ...] = (
    "</longcat_tool_call>",
    "</:tool-call>",
)
"""已知「XML 工具标签原文泄漏」形态（DSH longcat / tool-call 方言）。"""

_DISABLE_VALUES = ("0", "false", "off", "no")


def degenerate_detect_enabled() -> bool:
    """总开关：``HIVEWEAVE_DEGENERATE_DETECT`` ∈ {0,false,off,no} ⇒ 关
    （默认开）。每次构造检测器时读一次（per-request 粒度，feed 热路径
    零 env 解析）。"""
    raw = (os.environ.get("HIVEWEAVE_DEGENERATE_DETECT") or "").strip().lower()
    return raw not in _DISABLE_VALUES


def loop_repeat_threshold() -> int:
    """循环判据最低重复次数（env 可调；非法/非正值回退默认 8）。"""
    raw = (os.environ.get("HIVEWEAVE_DEGENERATE_LOOP_REPEATS") or "").strip()
    if not raw:
        return LOOP_MIN_REPEATS
    try:
        value = int(raw)
    except ValueError:
        return LOOP_MIN_REPEATS
    return value if value > 0 else LOOP_MIN_REPEATS


class DegenerateDetector:
    """单条 LLM 流的退化累积检测器（每请求一个实例，非线程安全 ——
    事件泵在事件循环内单点消费，无并发 feed）。"""

    __slots__ = ("_enabled", "_min_repeats", "_window", "_total", "_latched")

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        min_repeats: int | None = None,
    ) -> None:
        # 显式参数只供测试注入；生产路径每请求读一次 env。
        self._enabled = (
            degenerate_detect_enabled() if enabled is None else bool(enabled)
        )
        self._min_repeats = (
            loop_repeat_threshold() if min_repeats is None
            else max(1, int(min_repeats))
        )
        self._window = ""
        self._total = 0
        self._latched = False

    @property
    def total_chars(self) -> int:
        """累计喂入字符数（观测/测试用）。"""
        return self._total

    def feed(self, text_delta: str) -> str | None:
        """喂入一个正文/thinking 增量；命中返回退化类别，否则 None。

        命中后闩锁：后续 feed 一律 None（per-request 一次 —— 调用方经
        abort 管道掐流，本请求即告终结）。
        """
        if self._latched or not self._enabled or not text_delta:
            return None
        self._total += len(text_delta)
        self._window = (self._window + text_delta)[-WINDOW_CHARS:]
        if self._total < MIN_TOTAL_CHARS:
            return None
        window = self._window
        # a) 短语循环（先判：两大病灶的主力形态）
        if self._loop_hit(window):
            self._latched = True
            return DEGENERATE_KIND_LOOP
        # b) 标签泄漏（窗口内计数：跨 delta 分片的标签自愈）
        tag_hits = 0
        for tag in LEAKED_TOOL_TAGS:
            tag_hits += window.count(tag)
            if tag_hits >= TAG_LEAK_MIN_HITS:
                self._latched = True
                return DEGENERATE_KIND_TAG_LEAK
        return None

    # ── 判据 a：短语循环 ─────────────────────────────────────────

    def _loop_hit(self, window: str) -> bool:
        tail = window[-TAIL_CHARS:]
        if len(tail) < TAIL_CHARS:
            return False  # 窗口未满（暖机期刚过）—— 不判
        # a-1：tail == pattern*k（pattern ≤16，k ≥ 最低重复次数）。
        # repeats = 200//p 随 p 单调递减，跌破阈值即可短路。
        for period in range(1, PATTERN_MAX_CHARS + 1):
            if len(tail) % period:
                continue
            repeats = len(tail) // period
            if repeats < self._min_repeats:
                break  # p 更大 repeats 只会更小
            if tail == tail[:period] * repeats:
                return True
        # a-2：tail 在窗口内（重叠）出现 ≥5 次（>16 字符长单元循环）。
        # 达阈值短路：最多 LOOP_TAIL_MIN_OCCURRENCES 次 C 级 find。
        occurrences = 0
        pos = window.find(tail)
        while pos != -1:
            occurrences += 1
            if occurrences >= LOOP_TAIL_MIN_OCCURRENCES:
                return True
            pos = window.find(tail, pos + 1)
        return False
