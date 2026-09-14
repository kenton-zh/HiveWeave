"""fixplan-16items §三 #13 的契约测试：UNKNOWN 的归属必须**显式**、样本必须留痕。

三件事：

1. **两处相反默认钉在一起**。本仓同一个输入类（「没有 status、文案也不认识」）
   在**两条路径**上给了相反的默认：

   | 路径 | 默认 | 为什么 |
   |---|---|---|
   | `llm/retry.py::classify_http_error`（上游给了错误体但我们不认识） | **Permanent**（不重试） | 重试多半还是同样的拒绝 |
   | `llm/streamer/http_stream.py` 传输层裸错误（连接层没给任何结构化信息） | **Retryable**（重试） | 多半是瞬态；判永久会秒死（TEST_DSH_47 #8） |

   两处相反是**有意**的，但过去只写在各处注释里、没有一个地方把它们放在一起看
   ⇒ 谁改了其中一处，另一处的理由不会出现在他眼前。本测试用 **AST** 同时钉住
   两侧，任一侧被改即转红并提示去看另一侧。

   ⚠ 用 AST 而不是文本子串（本仓钦定：守卫禁用文本子串断言）—— 断言的是
   「`if is_region_unavailable_error(...)` 之后那条默认分支抛的是哪个异常类」。

2. **fallback 未命中 ⇒ 必留样本**（`llm/unknown_error_samples.py`）。
   「静默降级」才是病灶：上游换措辞/换语言后分类退化成 UNKNOWN 而无人知晓。

3. **样本绝不含密钥**、缓冲有界、`note/flush` 绝不抛。
"""

from __future__ import annotations

import ast
import json
import pathlib

import pytest

from hiveweave.llm.error_codes import ErrorCode, classify_error, is_retryable_code
from hiveweave.llm.retry import (
    UNKNOWN_SIGNAL_IS_RETRYABLE,
    PermanentError,
    RetryableError,
    classify_http_error,
)
from hiveweave.llm.unknown_error_samples import (
    _MAX_SAMPLES,
    clear_unknown_samples,
    flush_unknown_samples,
    note_unknown_sample,
    recent_unknown_samples,
    unknown_sample_total,
)

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "hiveweave"
_HTTP_STREAM = _SRC / "llm" / "streamer" / "http_stream.py"


@pytest.fixture(autouse=True)
def _clean_samples():
    clear_unknown_samples()
    yield
    clear_unknown_samples()


# ── 1. 两处相反默认 ──────────────────────────────────────────────


def _calls(node: ast.AST, func_name: str) -> bool:
    return any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == func_name
        for n in ast.walk(node)
    )


def _transport_raw_default_exc() -> tuple[str | None, list[tuple[int, str]]]:
    """「无 http_status 的传输层裸错误」默认分支抛出的异常类名。

    返回 ``(类名, 全部候选)``。找法（纯 AST）：定位 ``if is_region_unavailable_error(...)``
    这个 ``If`` 节点，然后**沿同一 body 列表向后找第一条 ``Raise``** ——
    源码的语义是「region 时 Permanent，否则走默认分支」，默认分支就是那条 Raise。

    ⚠ 为什么"向后找第一条 Raise"而不是"取紧邻的下一条语句"：
    第一版用 i+1，结果**我自己往中间插了一行 ``note_unknown_sample(...)``
    就把这条守卫打红了**（`i+1` 不再是 Raise ⇒ 0 候选 ⇒ 红）。守卫转红是对的，
    但根因是定位方式太脆 —— 一条与判定无关的语句不该让它红。

    ⚠ 但仍然**不接受语义不明的语句**：向后扫描时一旦遇到控制流语句
    （``If``/``Try``/``For``/``While``/``Return``/``With``/``Match``）就停止并
    放弃 —— 否则「在中间插入第二个 region 守卫 + raise」这类**诱饵**会让守卫
    匹配到错误的分支而**假绿**（审计实测过这条）。
    ⇒ 加诱饵 = 0 候选 = 转红；加无害语句（如一条 note 调用）= 仍能定位。
    """
    tree = ast.parse(_HTTP_STREAM.read_text(encoding="utf-8"))
    opaque = (ast.If, ast.Try, ast.For, ast.While, ast.Return, ast.With, ast.AsyncWith)
    if hasattr(ast, "Match"):
        opaque = (*opaque, ast.Match)  # type: ignore[attr-defined]
    cands: list[tuple[int, str]] = []
    for parent in ast.walk(tree):
        body = getattr(parent, "body", None)
        if not isinstance(body, list):
            continue
        for i, stmt in enumerate(body):
            if not isinstance(stmt, ast.If) or not _calls(
                stmt.test, "is_region_unavailable_error"
            ):
                continue
            for nxt in body[i + 1 :]:
                if isinstance(nxt, ast.Raise):
                    exc = nxt.exc
                    if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name):
                        cands.append((nxt.lineno, exc.func.id))
                    break
                if isinstance(nxt, opaque):
                    break  # 语义不明 ⇒ 放弃，不猜
                if not isinstance(nxt, (ast.Expr, ast.Assign, ast.AnnAssign)):
                    break
    return (cands[0][1] if len(cands) == 1 else None), cands


def test_unknown_default_is_explicit_not_by_omission():
    """UNKNOWN 的归属是**显式决策**（计划 #13：「不许默认」）。"""
    assert UNKNOWN_SIGNAL_IS_RETRYABLE is False, (
        "UNKNOWN_SIGNAL_IS_RETRYABLE 被改成了 True —— 这是允许的，但**必须同时**"
        "核对 llm/streamer/http_stream.py 传输层裸错误分支（那里的默认是相反的"
        "Retryable，理由不同：连接层没给结构化信息 ⇒ 多半瞬态）。"
        "两处一起改，别只改一处。"
    )
    err = classify_http_error(None, "unrecognized provider error")
    assert isinstance(err, PermanentError), (
        "无 status + 文案不认识 ⇒ classify_http_error 应给 PermanentError（不盲目重试）。"
        "要改成 Retryable 请先读 UNKNOWN_SIGNAL_IS_RETRYABLE 的注释。"
    )
    assert is_retryable_code(ErrorCode.UNKNOWN) is False, (
        "UNKNOWN 的可重试归属不得靠「不在列表里」的省略 —— 必须与 "
        "UNKNOWN_SIGNAL_IS_RETRYABLE 同源。"
    )


def test_transport_raw_branch_still_defaults_to_retryable():
    """反向一侧：传输层裸错误默认**重试**（与上面相反，且是有意的）。

    这条与上一条**成对**：把两侧放在同一个文件里，改任一侧都会看到另一侧。
    """
    got, cands = _transport_raw_default_exc()
    assert len(cands) == 1, (
        "期望 `http_stream.py` 里**恰好一处**「is_region_unavailable_error 守卫 + "
        f"紧随的 raise」，实际 {len(cands)} 处：{cands}。\n"
        "多出来的一处会让本测试选错分支（假绿/假红）—— 请确认该文件里到底有幾处\n"
        "地域 fast-fail 守卫，并相应更新本测试的定位方式（不要放宽成 `cands[0]`）。"
    )
    assert got == "RetryableError", (
        "llm/streamer/http_stream.py 的「无 http_status 传输层裸错误」默认分支"
        "不再是 RetryableError 了。\n"
        "⚠ 这一处的默认与 llm/retry.py::UNKNOWN_SIGNAL_IS_RETRYABLE **故意相反**：\n"
        "  · 这里 = 连接层压根没给结构化信息 ⇒ 多半瞬态 ⇒ 重试（TEST_DSH_47 #8 实测：\n"
        "    判永久会把 7s 快死拖成 476s 慢死）；\n"
        "  · 那里 = 上游给了错误体但我们不认识 ⇒ 重试多半还是同样的拒绝。\n"
        "若确实要改这一处，请把两处的注释与 tests/test_unknown_error_classification_contract.py "
        "一起改，不要只动一边。"
    )


# ── 2. fallback 未命中 ⇒ 留样本 ─────────────────────────────────


def test_http_error_fallback_miss_records_sample():
    before = unknown_sample_total()
    err = classify_http_error(None, "quelque chose d'inconnu", provider="p", model="m")
    assert isinstance(err, PermanentError)
    assert unknown_sample_total() == before + 1, "fallback 未命中必须留样本（fail-loud）"
    samples = recent_unknown_samples()
    assert samples[-1]["source"] == "classify_http_error"
    assert samples[-1]["provider"] == "p"
    assert samples[-1]["model"] == "m"
    assert "inconnu" in samples[-1]["body_preview"], "样本必须带**原始文案**，否则无法重建判据"


def test_http_error_identified_cases_do_not_record_sample():
    """已识别的情形**不该**刷样本（否则样本量变成噪音、指标失去意义）。"""
    before = unknown_sample_total()
    assert isinstance(classify_http_error(503, "whatever"), RetryableError)  # status 层
    assert isinstance(classify_http_error(402, "insufficient_quota"), PermanentError)  # 4xx
    assert isinstance(
        # 状态码不可重试但文案命中可重试模式 ⇒ 走 fallback **命中**
        classify_http_error(400, "upstream server error, please retry"),
        RetryableError,
    )
    assert unknown_sample_total() == before, (
        "以上三种都是**已识别**（status 命中或 fallback 命中），不应记样本。"
    )


def test_error_code_unknown_records_sample_and_context_does_not():
    before = unknown_sample_total()
    assert classify_error(None, "unrecognized provider error") is ErrorCode.UNKNOWN
    assert unknown_sample_total() == before + 1
    assert recent_unknown_samples()[-1]["source"] == "classify_error"

    before2 = unknown_sample_total()
    assert classify_error(None, "context length exceeded") is ErrorCode.CONTEXT_WINDOW
    assert unknown_sample_total() == before2, "fallback 命中（CONTEXT）不应记样本"


# ── 4. 每个样本点都必须有 flush 覆盖（审计实测的 D 缺口）─────────


def _enclosing_funcs(tree: ast.AST) -> dict[int, ast.FunctionDef]:
    """``id(子节点) → 最近的外层函数``。"""
    out: dict[int, ast.FunctionDef] = {}
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for child in ast.walk(func):
            out[id(child)] = func
    return out


def test_every_sample_site_in_streamer_has_a_flush_in_its_function():
    """结构网：`note_unknown_sample` 的每个调用点，其**所在函数**里必须有 flush。

    为什么要有这条：审计实测的真缺口 —— 图像能力短语表的 note 在
    `_stream_single_round`，而唯一的 flush 在**另一个方法** `_do_streaming_request`
    的 `except` 里 ⇒ 该样本要等本 agent **下一次**流异常才被顺带带走，
    若此后不再报错就**永不落库**（只剩日志）。

    这条网把「加了 note 忘了 flush」变成机械可检，而不是靠人记得两个方法的分界。
    """
    tree = ast.parse(_HTTP_STREAM.read_text(encoding="utf-8"))
    owners = _enclosing_funcs(tree)
    offenders: list[str] = []
    sites = 0
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "note_unknown_sample"
        ):
            continue
        sites += 1
        func = owners.get(id(node))
        if func is None:
            offenders.append(f"line {node.lineno}: 不在任何函数内")
            continue
        has_flush = any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "flush_unknown_samples"
            for n in ast.walk(func)
        )
        if not has_flush:
            offenders.append(f"line {node.lineno}: 所在函数 {func.name}() 里没有 flush")
    assert sites >= 2, (
        f"只找到 {sites} 个 note_unknown_sample 调用点 —— 定位方式可能已失效"
        "（函数被改名/搬走？），棘轮会静默失效。"
    )
    assert not offenders, (
        "这些样本点所在函数里没有 flush_unknown_samples ⇒ 样本可能永不落库：\n  "
        + "\n  ".join(offenders)
    )


# ── 5. 删裸 "429" 之后的边界行为 ───────────────────────────────


def test_rate_limit_429_detection_boundaries():
    """裸 `"429"` 已删（它是「requested 14290 tokens」这类误报源）。

    现在文本层要认 429，需要**标签 + 独立数字**两个条件都成立：
    标签 ∈ {http,status,error,code,错误码}，数字用 `(?<!\\d)429(?!\\d)`
    （不用 `\\b429\\b`：`_` 是 word char，`\\b` 会让 `code=ERR_429` 漏掉）。

    ⚠ **已知且接受的缺口**：`ERR_429`（无标签）不再被认。代价评估：
      · **主判据不受影响** —— 真实 HTTP 429 走 `RetryableError(status=429)`
        的类型分支（`rate_limit.py:177`），与文本层无关；
      · 只有「status 丢了、只剩这份文案」时才会漏，且**该缺口现在会留下样本**
        （`unknown_error_samples`），可以在拿到真实分布后决定是否值得处理；
      · 反向（留着裸子串）的代价是「任何含 429 的数字都被当限流 ⇒ 连续错误
        计数永不递增 ⇒ agent 永不 give up」，那比漏判危险得多。
    ⇒ 这条缺口是**有意的取舍**，不是遗漏。**不要**用扩标签表的方式修它
      （那是本仓已复发两次的「补词表」形态）；要修就走状态判据。
    """
    from hiveweave.agents.helpers.rate_limit import is_rate_limit_error

    # 仍要认：语义词 needles 命中
    assert is_rate_limit_error(ValueError("HTTP 429 Too Many Requests"))
    assert is_rate_limit_error(ValueError("rate_limit exceeded"))
    # 仍要认：标签 + 独立数字
    for text in ("status_code: 429", "error 429", "错误码 429", "code=ERR_429"):
        assert is_rate_limit_error(ValueError(text)), f"应判为限流：{text!r}"

    # 必须不认（旧的裸子串会把这些全判成限流）
    for text in ("requested 14290 tokens", "task id A4291", "line 429", "boom"):
        assert not is_rate_limit_error(ValueError(text)), f"不应判为限流：{text!r}"

    # 已知缺口（有意取舍，见 docstring）：无标签的 `ERR_429` 不认
    assert not is_rate_limit_error(ValueError("ERR_429")), (
        "若这条开始返回 True，说明有人又往标签表里加了词（'err'）—— "
        "请先读本测试 docstring 的取舍说明，改成状态判据而不是扩表。"
    )


# ── 6. 脱敏 / 有界 / 不抛 Exception ─────────────────────────────


def test_sample_never_contains_secrets():
    payload = note_unknown_sample(
        source="t",
        status=None,
        body=(
            "failed: api_key=SUPERSECRETVALUE Authorization: Bearer TOPSECRETTOKEN "
            "and sk-abcdefgh12345678 tail"
        ),
    )
    blob = json.dumps(payload, ensure_ascii=False)
    for secret in ("SUPERSECRETVALUE", "TOPSECRETTOKEN", "sk-abcdefgh12345678"):
        assert secret not in blob, f"样本里泄漏了密钥：{secret}"
    assert "sk-***" in payload["body_preview"], "sk- 形态应被替换"


def test_sample_buffer_is_bounded():
    for i in range(_MAX_SAMPLES + 5):
        note_unknown_sample(source="t", body=f"unknown #{i}")
    assert len(recent_unknown_samples()) == _MAX_SAMPLES, "环形缓冲必须有界"
    assert unknown_sample_total() == _MAX_SAMPLES + 5, (
        "累计计数不应被环形缓冲截断 —— 它是「不认识的比例」的分子"
    )


def test_note_never_raises_even_on_hostile_input():
    """它挂在错误分类路径上 ⇒ 抛异常会把「分类失败」升级成「整条流炸掉」。"""

    class Hostile:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    payload = note_unknown_sample(source="t", body=Hostile())  # type: ignore[arg-type]
    assert payload == {}, "拿不到文案时应返回空 dict，而不是抛"


@pytest.mark.asyncio
async def test_flush_only_writes_this_agents_samples(monkeypatch):
    """缓冲是进程级共享的 ⇒ flush 必须按 agent 归属过滤，否则归因错位。"""
    written: list[tuple[str, dict]] = []

    async def fake_log(self, agent_id, project_id, event_type, payload=None):  # noqa: ANN001
        written.append((agent_id, payload or {}))

    import hiveweave.services.event_audit as ea

    monkeypatch.setattr(ea.event_audit, "log", fake_log.__get__(ea.event_audit))

    note_unknown_sample(source="t", body="a-unknown", agent_id="A001")
    note_unknown_sample(source="t", body="b-unknown", agent_id="B002")
    note_unknown_sample(source="t", body="orphan-unknown")  # 无归属

    n = await flush_unknown_samples("A001")
    assert n == 1, "只应写本 agent 的样本"
    assert [w[0] for w in written] == ["A001"]
    assert written[0][1]["body_preview"] == "a-unknown"

    assert [s["body_preview"] for s in recent_unknown_samples()] == [
        "b-unknown",
        "orphan-unknown",
    ], "写成功的那条应被移出缓冲，其余（含无归属的）保留"
