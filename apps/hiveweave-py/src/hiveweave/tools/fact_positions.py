"""事实位归因的**证据判据**（L3，2026-09-11）。

## 为什么需要这个模块

收口（类型强制 + 单一漏斗）只解决了「**每个出口都必须回答它属于哪一格**」，
没有解决「**答案对不对**」。现状是「我在这个分支里，所以置 runner_failed」——
这是**自我声明**，不是证据。DSH 的对应判据：

> `packages/sandbox/sandbox/src/index.ts:74-88`
> 先应用 `allowedExitCodes` → 去掉 `informationalLines`（整行等值排除）→
> 逐行匹配 `fatalSignatures`。
> **Exit status alone never proves runner failure.**

⇒ 本模块把「什么文本算 runner 失败的专属签名」声明式地列出来，**顺序**也与
DSH 一致：**先判 runner（命令从未执行），再判 denial（护栏拦住了）**。
两边都不是时**不许静默归类**，而是 fail loud —— 静默归类等于把误标从
15 处搬到 1 处，还更隐蔽。

## 与 L6 的分工

`bad_args` 不是「runner 失败的一种」，是**调用方责任**：模型把路径/端口写错，
改参数就能过。判据是「**这个错误是否随调用方参数变化**」——
故它排在 runner 签名**之后**（runner 签名命中即命令根本没跑起来），
但在 timeout/denial 之前（写错路径时护栏根本没参与）。

## 为什么签名表而不用分支内联

分支内联 = 约束散在 28 个构造点里，就是本批要消灭的形态。声明式表的
另一个好处：`tests/test_fact_positions_coverage.py` 能把「表里的每一条」
与「真实错误文本」对起来，而内联的 if 无法被机械枚举。
"""

from __future__ import annotations

import re

from hiveweave.tools.result import FactKind

#: runner 失败的**专属签名**（命令从未执行）。
#:
#: 每条都对应一个已被实测观察到的出口（见各条注释的出处）。
#: 匹配是**大小写不敏感的子串**匹配（对齐 DSH 的 `fatalSignatures` 语义）。
RUNNER_FAILURE_SIGNATURES: tuple[str, ...] = (
    # ── B 组：命令安全 / 封印护栏（bash.py execute_bash / run_command）──
    "command blocked",                     # 自毁/敏感路径/.hiveweave 系统目录
    "system-level destructive command",
    "cannot access .hiveweave system directory",
    "拒绝执行",                             # eval_seal 封印工作区（中文文案）
    # ── C 组：沙箱 / cwd / 平台前提 ──
    "sandbox violation",
    "cwd must stay inside workspace",
    "working directory does not exist",
    "沙箱不可用",                           # SandboxUnavailableError（fail-closed）
    # ── D 组：方言 gate（命令没跑）──
    "not available in this shell",
    "not recognized as",
    "does not exist",
    "dialect",
    # ── E 组：spawn / runner 自身故障 ──
    "no tool executor",
    "[no tool executor]",
    "spawn",
    "failed to start",
    "cannot find the path",
    # ── A 组：审批通道（从未派发）──
    "permission",
    "approval",
    "审批",
)

#: 调用方参数错的专属签名（L6）。
#:
#: 判据：**随调用方参数变化** —— 模型换个路径/端口就能过，平台无责。
#: 注意这里**不含**泛化的 "not found"：那可能是 runner 侧前提缺失
#: （cwd 不存在），而 cwd 不存在是 `runner_failed`（见上表）。两者
#: 的区分靠**具体措辞**，这正是声明式签名表必须逐条列出而不能用
#: 通配的原因。
BAD_ARGS_SIGNATURES: tuple[str, ...] = (
    "疑似重复 worktree 前缀路径",
    "duplicate worktree prefix",
    "port",                                # dev-server 保留端口（换 3000+ 即可）
    "出界",                                 # 路径越界类参数错
)

#: `fact` 与签名的对应表（顺序即判据顺序，对齐 DSH「先 runner 再 denial」）。
#:
#: 顺序不可随意调换：`bad_args` 的 "port" 等签名较宽，若排在 runner 签名
#: 前面，会把「端口保留导致的 spawn 失败」之类的 runner 故障误判成参数错。
_SIGNATURE_ORDER: tuple[tuple[FactKind, tuple[str, ...]], ...] = (
    ("runner_failed", RUNNER_FAILURE_SIGNATURES),
    ("bad_args", BAD_ARGS_SIGNATURES),
)

_NORM_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """归一化到小写 + 单空格（签名匹配对换行/多空格不敏感）。"""
    return _NORM_RE.sub(" ", (text or "")).strip().lower()


def classify_error_text(error: str) -> FactKind | None:
    """按签名表归类错误的成因；**无签名命中时返回 None**（不猜）。

    调用方拿到 None 必须显式处理（fail loud / 保留未确定），不得默认
    归到某一格 —— 那正是「无证据归类」。
    """
    norm = _normalize(error)
    if not norm:
        return None
    for fact, signatures in _SIGNATURE_ORDER:
        for sig in signatures:
            if _normalize(sig) in norm:
                return fact
    return None


def classify_blocked_fact(
    tool_name: str,
    error: str,
    *,
    timeout_kind: str | None = None,
) -> FactKind:
    """`blocked` 结果的事实位归因 —— 单一判据入口。

    顺序严格照 DSH（`packages/sandbox/sandbox/src/index.ts:109-115`）：
    1. **先判 runner**：命令从未执行；
    2. 再判 `bad_args`：调用方参数错（平台无责）；
    3. 审批等待（`timeout_kind == "wait"`）：平台侧流程阻塞 ⇒ runner；
    4. 全不命中 ⇒ **AssertionError**（fail loud，绝不静默归类）。

    ``timeout_kind == "wait"`` 属平台侧（审批窗口等待），语义是「命令从未
    派发」，故归 runner_failed —— 这是**代码作用域归属**，不靠文案匹配
    （对齐 DSH `packages/guard/timeout-policy/src/index.ts:69-73` 的
    「a nested outer deadline reads as undefined here」同款思路）。
    """
    kind = classify_error_text(error)
    if kind is not None:
        return kind
    if timeout_kind == "wait":
        return "runner_failed"
    raise AssertionError(
        f"blocked tool result has no fact evidence: tool={tool_name!r} "
        f"error={error!r:.200} — 签名表未命中时不得静默归类；"
        f"要么补 RUNNER_FAILURE_SIGNATURES/BAD_ARGS_SIGNATURES，"
        f"要么让构造点显式声明 fact（见 fixplan 批次 2 §1.4c）"
    )


def assert_fact_complete(tool_name: str, result: dict) -> None:
    """启动/收口断言：shell 类失败结果**必须**带可用事实位。

    对「成功」「blocked=False 且无 error」放行；其余必须在四格里有答案。
    """
    if result.get("success"):
        return
    if result.get("fact") is None:
        raise AssertionError(
            f"shell tool {tool_name!r} failed without a fact position: "
            f"error={result.get('error')!r:.200} — "
            f"见 fixplan 批次 2 §1.4a（blocked 必须声明 fact）"
        )
    fact = result["fact"]
    from hiveweave.tools.result import FACT_KINDS

    if fact not in FACT_KINDS:
        raise AssertionError(
            f"shell tool {tool_name!r} declared unknown FactKind {fact!r}"
        )


#: 需要事实位收口的工具（shell 家族）。判据：这些工具的错误文本里
#: 「命令没跑起来」与「命令跑了没过」是**可区分的**，且下游 stall 归因
#: 消费该区分（`llm/streamer/doom_loop.py` / `advisory.py`）。
SHELL_SECURITY_LEVEL_TOOLS: frozenset[str] = frozenset({
    "bash", "pwsh", "run_command", "execute_bash",
    "start_dev_server", "lookup_dev_server",
})


def finalize_tool_result(
    tool_name: str,
    raw: dict | object,
    *,
    judge_blocked: bool = True,
) -> dict:
    """**唯一收口**：把工具返回归一为契约 dict，并保证事实位完整。

    ⚠️ **不能只挂在 `_emit_tool_execute_after`**（`executor.py:2446`）：
    它的 docstring 明写「Pre-execution failures (args/permission/ask) never
    emit」——而 28 处事实位构造点里 **17 处（审批 + 护栏）正是 pre-execution
    失败**，恰好全在它覆盖之外。故本函数必须在**两条执行器各自的
    normalize 尾**都被调用。

    `judge_blocked=True` 时对 blocked 结果按签名表补齐事实位（无签名
    则 fail loud）；`judge_blocked=False` 用于只做形状归一、不参与
    事实位归因的调用点。
    """
    from hiveweave.tools.result import ToolResult, finalize_fact_dict

    if isinstance(raw, ToolResult):
        r = raw
    elif isinstance(raw, dict):
        # blocked 必须显式透传：进 extra 会被 ToolResult 字段恒胜覆盖抹掉
        # （审计 P2，潜伏陷阱）。
        r = ToolResult(
            success=raw.get("success", True),
            output=raw.get("output", ""),
            error=raw.get("error"),
            blocked=bool(raw.get("blocked")),
            fact=raw.get("fact"),
            extra={
                k: v
                for k, v in raw.items()
                if k not in ("success", "output", "error", "blocked", "fact")
            },
        )
    else:
        return ToolResult.ok(str(raw)).to_dict()

    # 事实位归因：blocked 却无 fact ⇒ 按签名表判；判不出来就 fail loud。
    if judge_blocked and r.blocked and r.fact is None:
        r.fact = classify_blocked_fact(
            tool_name,
            r.error or "",
            timeout_kind=getattr(r, "timeout_kind", None),
        )

    out = r.to_dict()
    if tool_name in SHELL_SECURITY_LEVEL_TOOLS or judge_blocked:
        assert_fact_complete(tool_name, out)
    # 裸字典路径可能带进陈旧的 runner_failed/command_failed —— 由 fact 统一
    return finalize_fact_dict(out)


# ── 启动断言（机械 gate，import 期执行）─────────────────────────
#
# 对齐 DSH `packages/AGENTS.md:145`：
#   「Wire mechanically checkable invariants into an executed top-level gate
#     and prove each changed acceptance path rejects an invalid case」
#
# 既有先例：`llm/streamer/constants.py:232-263` 的同款 assert 风格。
# 这里的断言在**导入期**跑 —— 任何 `import hiveweave.tools.fact_positions`
# 都会执行（`tools/__init__` 与两条执行器都会导入），所以坏结构不可能
# 静默上线。
def _assert_signature_table_wellformed() -> None:
    # 1) 两张表的每一条都非空且已归一（否则 `in` 匹配会意外命中空串）
    for _fact, _sigs in _SIGNATURE_ORDER:
        assert _fact in ("runner_failed", "bad_args"), _fact
        assert _sigs, f"FactKind {_fact!r} 的签名表为空 —— 它永远不会被命中"
        for _sig in _sigs:
            assert _sig.strip(), f"FactKind {_fact!r} 含空签名"
            assert _sig == _sig.strip(), (
                f"签名 {_sig!r} 有首尾空白 —— 匹配前会归一，声明侧也必须归一"
            )
            assert _sig.lower() == _sig, (
                f"签名 {_sig!r} 未小写 —— 匹配是大小写不敏感的，声明侧必须统一"
            )
    # 2) 顺序铁律：runner 签名必须排在 bad_args 之前（DSH「先 runner 再 denial」）
    _order = [f for f, _ in _SIGNATURE_ORDER]
    assert _order.index("runner_failed") < _order.index("bad_args"), (
        "判据顺序错：bad_args 的宽签名会吞掉 runner 故障（见 _SIGNATURE_ORDER 注释）"
    )
    # 3) `classify_error_text` 对空/无签名输入必须返回 None（不猜）
    assert classify_error_text("") is None
    assert classify_error_text("完全无关的一段文本") is None


_assert_signature_table_wellformed()


