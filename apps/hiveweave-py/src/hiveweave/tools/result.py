"""Unified tool result type.

Replaces ad-hoc dict returns with a dataclass that enforces the
{success, output, error} contract at construction time.

Usage:
    return ToolResult.ok("File written")
    return ToolResult.ok("Task created", task_id=task_id)
    return ToolResult.err("File not found")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# ── 事实位词表（L6 定词表 → L3 收口）─────────────────────────────
# 「一次工具调用为什么没有成功」有且只有四格。任何失败出口都必须能答出
# 「我属于哪一格」——答不出就不该存在（启动断言 + AST 覆盖测试把关）。
#
# 判据来源：**谁造成了这个前提**（比错误文本更本质）。
# 同一个「Working directory does not exist」在 bash 层可能是平台没建好树
# （runner_failed），在 pipeline 的 project-root 拒却是 agent 写错树
# （bad_args）—— 所以禁止按文案批量归类。
#
#   runner_failed   — 命令**从未执行**：参数注入破坏 / 方言不支持 / 权限 /
#                     审批 / 沙箱越界 / runner 自身故障。
#                     ⇒ 站在 agent 视角是「平台前提缺失」，不是你的 bug。
#   command_failed  — 命令**执行了但失败**：测试未过 / 断言失败 / 业务错误。
#                     ⇒ 跑到一半失败，可能因命令内容不对；不是平台问题。
#   bad_args        — 调用方**参数错**，平台无责：幽灵 worktree 前缀路径 /
#                     保留端口 / 参数非法。
#                     ⇒ 归因必须落到「你自己的问题」（stall → tool_failed），
#                     否则 agent 会收到「不是你的 bug」信号并反复重撞。
#   outcome_unknown — **结果未知**：已记录该调用但完成结果未持久化
#                     （孤儿步骤清扫）。可能已产生副作用 ⇒ 不许盲目重试。
FactKind = Literal[
    "runner_failed",
    "command_failed",
    "bad_args",
    "outcome_unknown",
]
FACT_KINDS: frozenset[str] = frozenset(
    ("runner_failed", "command_failed", "bad_args", "outcome_unknown")
)
# blocked=True 语义是「平台护栏拒绝」，按定义只能是平台侧成因的三格之一。
# command_failed（你自己的代码没过）与 bad_args（你自己的参数错）都不可能是
# 平台护栏拒绝 —— 把它们标成 blocked 正是 L6/L19 的病：agent 收到
# 「not a model mistake」然后原地重撞。
_BLOCKED_FACT_KINDS: frozenset[str] = frozenset(
    ("runner_failed", "outcome_unknown")
)


@dataclass
class ToolResult:
    """Unified tool return value.

    ``success``, ``output``, and ``error`` are always present.
    Extra structured fields (e.g. ``task_id``, ``alarm_id``) go in
    ``extra`` and are merged into the dict by :meth:`to_dict`.
    ``blocked`` marks platform-guard refusals (permission / sandbox /
    security rules): the platform refused to execute — not a model
    mistake — so stall detection must not treat it as model spinning (H3).
    """

    success: bool
    output: str = ""
    error: str | None = None
    blocked: bool = False
    extra: dict[str, Any] = field(default_factory=dict)
    # L3（2026-09-11）：事实位从**两个可空 bool** 收敛为**闭合判别标签**。
    # 旧写法下「在哪个分支就置哪个位」散落在 28 个构造点，两个执行器各抄
    # 一份，没有任何机制要求 blocked 必须携带事实位 —— 新增护栏分支时
    # 写得出无位的结果。现由 __post_init__ 强制。
    #
    # fact=None 表示「这一格尚未判定」（成功结果、或非 shell 类工具的普通
    # 失败）。shell 类的失败必须显式给出 fact（见 __post_init__ 的断言）。
    fact: FactKind | None = None
    injection_applied: bool | None = None
    # F7：超时分类（runner / command / wait）+ 超时毫秒。
    timeout_kind: str | None = None
    timeout_ms: int | None = None

    # ── L3 不变式（构造函数强制）──────────────────────────
    #
    # 目标（DSH `packages/AGENTS.md:14`「Enforce a decision in the operation
    # that makes it」）：**新增护栏分支时写不出违规结果**，而不是靠 28 个
    # 构造点各自记得置位、靠"对称"维持。
    #
    # 这里只强制能**机械判定**的两条；"必须命中专属失败签名"那条需要证据
    # 判据（tools/fact_positions.py），由单一漏斗 finalize_tool_result 施加
    # —— 因为证据只有在规范化点才齐全（error 文案、timeout_kind 都已定稿）。
    def __post_init__(self) -> None:
        if self.fact is not None and self.fact not in FACT_KINDS:
            raise ValueError(
                f"unknown FactKind {self.fact!r}; "
                f"expected one of {sorted(FACT_KINDS)}"
            )
        # 不变式 1：blocked=True 必须答出四格归属，且只能是平台侧成因。
        # 这条直接封死 L6 的形态 —— 「裸 {"blocked": True} 无位」写不出来。
        if self.blocked:
            if self.fact is None:
                raise ValueError(
                    "blocked result must declare its fact kind "
                    f"(one of {sorted(_BLOCKED_FACT_KINDS)}): {self!r}"
                )
            if self.fact not in _BLOCKED_FACT_KINDS:
                raise ValueError(
                    f"blocked=True cannot carry fact={self.fact!r} — "
                    "bad_args/command_failed 是调用方的责任，标成平台护栏"
                    "拒绝会让 agent 收到「不是你的 bug」信号并原地重撞 "
                    f"(L6/L19): {self!r}"
                )
        # 不变式 2：失败结果不得自相矛盾。
        if self.success and self.fact == "runner_failed":
            raise ValueError(
                "successful result cannot be runner_failed: " f"{self!r}"
            )

    # ── 只读派生属性（108 处下游引用零改动）────────────────
    #
    # 收口的是**构造**，不是**消费**。两个旧 bool 的语义**逐字保留**为派生
    # 视图 —— 特别是 `None` 必须继续表示「未判定」：
    #
    #   `run_ledger.record_step_end` 用 `None` 区分「这次没得判」与「判了是假」
    #   （SQL `COALESCE(?, runner_failed)`）—— 若把 False 当 None 传，
    #   `COALESCE(0, existing)` 会把先前写入的 1 覆盖成 0。所以：
    #     fact is None                         → None（未判定，不落库）
    #     fact == "runner_failed"              → True
    #     fact 是其余三格（含 command_failed） → False
    #
    # 注：`fact == "runner_failed"` 是唯一置真条件，与旧代码逐点等价
    # （旧 28 处构造点里 runner_failed=True 恰好对应本格）。

    @property
    def runner_failed(self) -> bool | None:
        """命令是否从未执行。None = 尚未判定（旧语义，勿改）。"""
        if self.fact is None:
            return None
        return self.fact == "runner_failed"

    @property
    def command_failed(self) -> bool | None:
        """命令是否执行了但失败。None = 尚未判定（旧语义，勿改）。"""
        if self.fact is None:
            return None
        return self.fact == "command_failed"


    @classmethod
    def ok(cls, output: str = "", **extra: Any) -> "ToolResult":
        """Build a success result with optional structured fields."""
        return cls(success=True, output=output, error=None, extra=extra)

    @classmethod
    def err(
        cls, message: str, fact: FactKind | None = None, **extra: Any
    ) -> "ToolResult":
        """Build an error result. ``success`` is always ``False``.

        Optional structured fields (e.g. ``gates``, ``actions``) go in
        ``extra`` and are merged into the dict by :meth:`to_dict` — they
        are observable for telemetry/future consumers (TEST19 ④).

        ``fact`` 是 L3 的四格事实位（runner_failed / command_failed /
        bad_args / outcome_unknown）。普通工具失败可留 None；**shell 类
        失败必须给**（由 ``tools/fact_positions.py`` 的启动断言把关）。
        注意 ``bad_args`` 走这里而**不是** :meth:`blocked_err` —— 参数错
        是调用方的责任，标成平台护栏拒绝会让 agent 原地重撞（L6）。
        """
        return cls(
            success=False, output="", error=message, extra=extra, fact=fact
        )

    @classmethod
    def blocked_err(
        cls,
        message: str,
        fact: FactKind = "runner_failed",
        **extra: Any,
    ) -> "ToolResult":
        """Build a platform-guard refusal result (permission/sandbox/security).

        ``success`` is always ``False`` and text semantics match :meth:`err`;
        ``blocked=True`` tells stall detection this was a platform refusal,
        not a model mistake (H3). Named ``blocked_err`` (not ``blocked``)
        to avoid colliding with the ``blocked`` dataclass field.

        ``fact`` 默认 ``runner_failed``（命令从未执行 —— 覆盖绝大多数护栏
        出口：权限 / 沙箱 / 方言 / 封印）。平台前提缺失类（cwd 不存在、
        幽灵树）若要更精确可传 ``fact="outcome_unknown"`` 之外的对应格，
        但 **blocked 只接受平台侧成因的两格**（见 ``__post_init__``）。
        """
        return cls(
            success=False,
            output="",
            error=message,
            extra=extra,
            blocked=True,
            fact=fact,
        )

    # ── serialization ────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Convert to the legacy dict format expected by the rest of the system."""
        d: dict[str, Any] = {
            "success": self.success,
            "output": self.output,
            "error": self.error,
        }
        d.update(self.extra)
        # extra 不得覆盖 blocked（平台护栏标记不是工具自由字段）
        d["blocked"] = self.blocked
        # L3：fact 是权威事实位。旧调用方可能残留 `runner_failed=` 关键字
        # （现在会落进 extra）—— 权威值由 fact 派生，故先清掉 extra 里的
        # 陈迹，再由下方派生循环重写，保证**永不出现两个来源打架**。
        for _legacy in ("runner_failed", "command_failed"):
            d.pop(_legacy, None)
        if self.fact is not None:
            d["fact"] = self.fact
        # F4/F7：事实位随 dict 透传（仅非 None —— None 表示「未确定」，
        # 上层不应据此落库臆断的归因）。
        for _k in (
            "runner_failed", "command_failed", "injection_applied",
            "timeout_kind", "timeout_ms",
        ):
            _v = getattr(self, _k, None)
            if _v is not None:
                d[_k] = _v
        return d

    def __repr__(self) -> str:
        if self.success:
            return f"ToolResult(ok, output={self.output[:60]!r}...)"
        _f = f", fact={self.fact}" if self.fact else ""
        if self.blocked:
            return f"ToolResult(blocked{_f}, error={self.error!r})"
        return f"ToolResult(err{_f}, error={self.error!r})"


# ── L3/L4 单一漏斗（2026-09-11）────────────────────────────────
#
# `bash.py` 的 15 处 shell 出口直接返回**裸字典**（历史原因：这些路径在
# `_shell_tool_result` 之前就 return 了）。裸字典绕过 `ToolResult` 类型，
# 于是 `fact` 的派生属性（runner_failed / command_failed）不会自动展开。
#
# ⚠ 危害形态（2026-09-17 更正）：早先这里写作「下游 `result["runner_failed"]`
# 直接 KeyError」。第三轮审计复核当前 `src/` 的消费者**全用 `.get()`**，
# KeyError **不可复现**；当时（2026-09-11）确有 `[]` 下标消费者，随手改成了
# `.get()`，但**注释没跟着改** ⇒ 留下一条"找不到罪犯的罪状"。
# 现役的真实危害是**静默误归因**：派生键缺失 ⇒
#   · `tool_loop.py:1388` 的 `if _rf.get("runner_failed")` 不成立 ⇒
#     agent 少一句「命令未执行」的归因提示（把平台问题当成自己的）；
#   · `streaming.py:340/420` 落库 `None`（"未判定"）而非 `False`（"已判定为
#     非"）—— 与 `COALESCE` 要区分这两者的设计冲突。
# 漏斗依然必须，只是**理由要说成后一条**。
#
# 修法不是「每处手写两个键」（那正是「约束写在调用方看得见的地方」的
# 复发），而是**唯一漏斗**：所有裸字典出口经此函数收口，fact → 派生键
# 的展开只有一处实现。
#
# 为什么不让裸字典直接改 `ToolResult`：这些 return 点埋在上千行的长函数
# 里，且部分路径的 `extra` 键集与 ToolResult 的构造签名不一一对应；
# 一次性重写风险大、也无法分片验证。本漏斗是**同一语义的类型安全边界**，
# L3 的 AST 覆盖测试会枚举所有 shell 出口强制它们经过这里。

_DERIVED_FACT_KEYS: tuple[str, ...] = ("runner_failed", "command_failed")


def finalize_fact_dict(d: dict[str, Any]) -> dict[str, Any]:
    """裸字典出口的统一收口：把权威 `fact` 展开为派生的布尔键。

    - `fact is None`（未确定）→ **不写派生键**，保持「未确定」语义
      （`run_ledger.record_step_end` 靠 COALESCE 区分 None 与 False）。
    - `blocked=True` 却无 `fact` → 按 L6 语义回落 `runner_failed`
      （平台护栏类出口的默认成因就是「命令从未执行」）。
    - 调用方残留的 `runner_failed=`/`command_failed=` 裸键一律**以 fact 为准**
      清除后重写，杜绝两个来源打架。

    幂等：可重复调用。
    """
    if not isinstance(d, dict):
        return d
    fact = d.get("fact")
    if fact is None and d.get("blocked"):
        # 与 `_shell_tool_result` 的 blocked 回落同口径
        fact = "runner_failed"
        d["fact"] = fact
    for legacy in _DERIVED_FACT_KEYS:
        d.pop(legacy, None)
    if fact is not None:
        if fact not in FACT_KINDS:
            raise ValueError(
                f"unknown FactKind {fact!r} in raw tool dict; "
                f"expected one of {sorted(FACT_KINDS)}"
            )
        d["runner_failed"] = fact == "runner_failed"
        d["command_failed"] = fact == "command_failed"
    return d
