"""#12：`code_audit` severity 解析改 fail-safe + shadow 观测（验收 ①②③④）。

设计背景见 `fixplan-16items-2026-09-14.md` §三 #12。要点：
- **fail-safe**：severity **解析不出 ⇒ 视为 high（拦门）**。旧判据 `"[high]" in issue`
  是 **fail-open** 的 → 把"写成了别的形态"静默当成"不是 high" ⇒ 换标点/换语言即放行。
- **冲突规则**：`SEVERITY:low … 【high】` ⇒ **只认首个 `SEVERITY:` 前缀**，后文忽略。
- **切「真拦」（2026-09-16）**：闸门自本日起用 fail-safe 结论。
  shadow 观测期（09-15 ~ 09-16）的"只记录不拦"已结束 —— 本文件**相应改过一次断言**：
  原来钉的是"真闸门必须仍按 legacy（不拦）"，现在钉的是"**闸门必须拦**"
  （判据 = 审计凭证的 `exit_code`，那是下游 `verify_ids` 真正消费的字段）。
  改的是断言而不是放宽：旧断言只查算出来的 `legacy_blocking` 字段，
  该字段切换后**仍是 False**（现为对照值）⇒ 旧断言在新世界里**恒绿、什么也不看**。

⚠ 仍然**没有**钉的部分（如实登记）：`verdict` 仍由 LLM 输出**首行文本**
（`_parse_verdict` 的 `startswith("VERDICT: PASS")`）决定 ⇒ 首行写 PASS 时
issue 行再乱也不拦。那是 `#12` 的**已知残余**（治本要等结构化输出），
本条只把"止血"从"记录"推进到"拦门"。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
import structlog.testing

from hiveweave.services.code_audit import (
    count_issue_severities,
    parse_issue_severity,
    severity_conflict,
)
from hiveweave.services.code_audit import run_code_audit

# `env` 是 fixture，导入到本模块即生效；其余是打桩与常量。
from tests.test_audit_epic_fixes import (  # noqa: F401
    AGENT_ID,  # noqa: F401
    PROJECT_ID,  # noqa: F401
    _run_audit_patches,  # noqa: F401
    env as audit_env,  # noqa: F401  —— fixture（改名避免与测试参数同名触发 F811）
)


# ── 验收①：fail-safe —— 认不出的写法必须被判为 high（而不是"不是 high"）──


@pytest.mark.parametrize(
    "issue",
    [
        "src/a.py:1 【high】未校验输入",          # 全角括号（旧 `"[high]"` 判据会漏）
        "src/a.py:1 【高】未校验输入",             # 中文 severity（会漏）
        "src/a.py:1 SÉVÉRITÉ:élevé — entrée non validée",  # 整段法文（会漏）
        "src/a.py:1 输入未校验",                   # 压根没写 severity（会漏）
        "src/a.py:1 <high> 未校验输入",            # 尖括号（会漏）
    ],
)
def test_unparsable_severity_is_treated_as_high_by_failsafe(issue: str):
    """★ 验收①：这些写法**旧判据一律漏掉**（fail-open），fail-safe 必须算成 high。"""
    assert parse_issue_severity(issue) is None, "这些形态本就解析不出 severity"
    counts = count_issue_severities([issue])
    assert counts["unparsed"] == 1
    # fail-safe 的判定式：high + unparsed —— 与 run_code_audit 内保持一致。
    assert counts["high"] + counts["unparsed"] == 1, "解析不出 ⇒ 必须计入 fail-safe 的 high"


def test_old_substring_judgment_would_miss_them():
    """把"旧判据为什么是 fail-open"钉成可执行断言。

    旧判据 = `"[high]" in issue.lower()`。上面那批形态里 `【high】` 是**全角**括号，
    所以旧判据数到 0 ⇒ 不拦门。**这就是洞本身**，不是测试的瑕疵。
    """
    issues = ["src/a.py:1 【high】未校验输入", "src/a.py:1 输入未校验"]
    legacy_high = sum(1 for i in issues if "[high]" in i.lower())
    assert legacy_high == 0, "旧判据对全角【high】与无标记形态都漏 —— 这正是要修的"
    counts = count_issue_severities(issues)
    assert counts["high"] + counts["unparsed"] == 2, "fail-safe 把两条都算进来"


# ── 验收②：明确标 low ⇒ 不拦 ──


def test_explicit_low_is_not_failsafe_high():
    """验收②：明确 `SEVERITY:low` ⇒ 不拦（fail-safe 只兜"未知"，不兜"已知是 low"）。"""
    issues = ["src/a.py:9 SEVERITY:low 命名风格建议"]
    assert parse_issue_severity(issues[0]) == "low"
    counts = count_issue_severities(issues)
    assert counts == {"high": 0, "medium": 0, "low": 1, "unparsed": 0, "conflicts": 0}
    assert counts["high"] + counts["unparsed"] == 0


def test_medium_is_not_high_but_is_counted():
    """medium 记录但不进 fail-safe 的 high（门禁语义仍是「有 high 才拦」）。"""
    counts = count_issue_severities(["src/a.py:3 SEVERITY:medium 可维护性"])
    assert counts["medium"] == 1
    assert counts["high"] + counts["unparsed"] == 0


# ── 验收④：冲突只认首个 SEVERITY: 前缀，其后文本忽略 ──


def test_conflict_takes_the_first_prefix_and_ignores_the_rest():
    """验收④：`SEVERITY:low` + `【high】` ⇒ 按 **low**，且把"忽略了后文"标出来。"""
    issue = "src/a.py:5 SEVERITY:low 仅命名建议 【high】但其实这里也说了别的问题"
    assert parse_issue_severity(issue) == "low", "只认首个 SEVERITY: 前缀，后文一律忽略"
    assert severity_conflict(issue) is True, "不一致要被标出来（供日志留痕）"
    counts = count_issue_severities([issue])
    assert counts["low"] == 1 and counts["high"] == 0
    assert counts["conflicts"] == 1
    assert counts["high"] + counts["unparsed"] == 0, "不因后文的【high】而拦"


def test_no_conflict_when_suffix_agrees_or_absent():
    assert severity_conflict("x SEVERITY:high 真 bug") is False
    assert severity_conflict("x SEVERITY:high 真 bug [high]") is False, "一致不算冲突"
    assert severity_conflict("x 【low】 无前缀") is False, "无前缀就无从谈冲突"


def test_prefix_wins_over_legacy_bracket_in_either_order():
    """新旧形态同时在时，**前缀优先**（避免靠"谁先出现"这种偶然顺序定判据）。"""
    assert parse_issue_severity("x [high] y SEVERITY:low") == "low"
    assert parse_issue_severity("x SEVERITY:low y [high]") == "low"


# ── ③ + shadow：端到端 —— **记录**但**不拦**，且两者不一致要被量出来 ──


def _intercept_event(logs: list, name: str) -> dict | None:
    for entry in logs:
        if entry.get("event") == name:
            return entry
    return None


@pytest.mark.asyncio
async def test_gate_now_blocks_unparsable_severity_and_shadow_keeps_the_comparison(
    audit_env,  # noqa: F811 —— fixture 参数，不是重复定义
):
    """★ 端到端（**切真拦的闸门级验收**）：`【high】` 的 issue ⇒ **闸门真的拦**。

    ⚠ **本用例 2026-09-16 改过语义**（#12：`legacy_blocking` → `shadow_blocking`）。
    切换前它断言「真闸门必须仍按 legacy（不拦）」—— 那时是 shadow 观测期。
    现在必须拦。**改的是断言，不是放宽**：旧断言只查算出来的 `legacy_blocking`
    字段，那字段切换后**仍是 `False`**（它现在是对照值）⇒ 旧断言在新世界里
    **恒绿、什么也不看**。

    闸门级判据是 **审计凭证的 `exit_code`** —— 那才是"拦没拦"的状态；
    shadow payload 里的字段只说明"切换前会怎么做"。
    """

    async def call_llm(system: str, user: str) -> str:
        # 全角括号 —— 旧判据漏、fail-safe 兜。
        return "VERDICT: ISSUES\nsrc/a.py:1 【high】未校验用户输入\n"

    result, gates, logs = await _run_audit_capturing(call_llm)

    assert isinstance(result, dict)
    assert result.get("audited") is True, f"审计本身应完成：{result}"

    # ★ 闸门级判据：exit_code=1 = 拦门
    assert gates, "没抓到审计凭证的 create 调用 ⇒ 无法判定闸门"
    assert gates[-1]["exit_code"] == 1, (
        "闸门没拦 —— 切真拦没生效（fail-safe 要求「档位解析不出 ⇒ 视为 high」）："
        f"{gates[-1]}"
    )

    # 观测契约照旧（这些字段是「切换前会怎么做」的对照，删了就没法回答
    # 「这条为什么新被拦」）
    shadow = _intercept_event(logs, "code_audit_severity_shadow")
    assert shadow is not None, (
        "没看到 code_audit_severity_shadow 事件 ⇒ shadow 观测没接线（"
        f"实际事件：{[e.get('event') for e in logs]}）"
    )
    assert shadow["legacy_high"] == 0, "旧判据对【high】漏（＝fail-open 的实证）"
    assert shadow["unparsed"] == 1
    assert shadow["failsafe_high"] == 1
    assert shadow["shadow_blocking"] is True, "fail-safe 判定应为「该拦」"
    assert shadow["legacy_blocking"] is False, (
        "legacy 判据本应漏掉这条（那正是切换的理由）；它变成 True 说明 legacy "
        "判据被改过 —— 会掩盖「为什么要切」的证据。⚠ 它现在只是**对照值**，"
        "不代表闸门行为（闸门行为看上面对 exit_code 的断言）"
    )
    assert shadow["would_flip"] is True, "这条正是切换后新被拦下的样本"
    # ⚠ 第二轮复审 P2：上面只断 6 个键，剩下 4 个**拼错名字就会静默出货**
    # （观测字段错名 ⇒ 数据看着有、指标算错）。故断言完整键集合 + 剩余取值。
    # structlog 自己会往条目里加 `event`/`log_level`，比较时剔除。
    payload_keys = set(shadow) - {"event", "log_level"}
    assert payload_keys == {
        "verdict", "issues_total", "legacy_high", "failsafe_high", "unparsed",
        "unparsed_ratio", "conflicts", "legacy_blocking", "shadow_blocking",
        "would_flip",
    }, f"shadow 事件的键集合变了：{sorted(payload_keys)}"
    assert shadow["verdict"] == "ISSUES"
    assert shadow["issues_total"] == 1
    assert shadow["unparsed_ratio"] == 1.0
    assert shadow["conflicts"] == 0


async def _run_audit_capturing(call_llm):
    """跑一次 `run_code_audit`，**同时**抓「闸门实际给的 exit_code」与日志。

    为什么需要它：`run_code_audit` 的返回值里**没有** exit_code —— 它落在
    **审计凭证**（`attestation_service.create(..., exit_code=…)`）上。
    只断 shadow payload 会漏掉"闸门到底拦没拦"（payload 是算出来的对照值）。
    因此这里包一层 create：记录 kwargs 后**照常调用真实现**（不改变行为）。

    ⚠ 补丁打在**源模块的单例**上（`services.attestation.attestation_service`），
    不是 `services.code_audit` 上的名字 —— 后者是函数内 `from … import` 的
    **局部绑定**，模块级没有这个属性（实测 `AttributeError`）。
    """
    from hiveweave.services.attestation import attestation_service as _svc

    gates: list[dict] = []
    real_create = _svc.create

    async def _spy(*args, **kwargs):
        gates.append(kwargs)
        return await real_create(*args, **kwargs)

    p_wt, p_git, p_save = _run_audit_patches()
    with (
        p_wt, p_git, p_save,
        patch.object(_svc, "create", new=_spy),
        structlog.testing.capture_logs() as logs,
    ):
        result = await run_code_audit(PROJECT_ID, AGENT_ID, call_llm=call_llm)
    return result, gates, logs


@pytest.mark.asyncio
async def test_run_code_audit_actually_goes_through_shadow_decision(
    audit_env,  # noqa: F811 —— fixture 参数，不是重复定义
    monkeypatch,
):
    """正向对照：`run_code_audit` 必须**经由**唯一判定点 `shadow_decision`。

    为什么必须有这条（第二轮复审 P2）：
    `test_failsafe_composition_stays_wide`（在 `test_anti_wording_matrix.py`）
    只断言 `shadow_decision()` **自己**的结论 —— 如果有人在 `run_code_audit`
    里**重新内联**一份判据（本仓最忌讳的"同一事实两处判"），那条守卫**仍然全绿**。
    这里把 `shadow_decision` 换成哨兵：一旦它没被调用（＝判据被内联复制），
    或用例改走别的判定路径，本用例即转红。
    """
    import hiveweave.services.code_audit as _ca

    calls: list[tuple[str, list[str]]] = []
    _real = _ca.shadow_decision

    def _sentinel(verdict: str, issues: list[str]):
        calls.append((verdict, list(issues)))
        return _real(verdict, issues)

    monkeypatch.setattr(_ca, "shadow_decision", _sentinel)

    async def call_llm(system: str, user: str) -> str:
        return "VERDICT: ISSUES\nsrc/a.py:1 [medium] 命名风格\n"

    p_wt, p_git, p_save = _run_audit_patches()
    with p_wt, p_git, p_save:
        result = await run_code_audit(PROJECT_ID, AGENT_ID, call_llm=call_llm)

    assert result.get("audited") is True, f"审计本身应完成：{result}"
    assert calls, (
        "run_code_audit 没有走 shadow_decision ⇒ 判据被内联复制了"
        "（同一事实两处判 ⇒ 切真拦时会有一处漏改）"
    )
    assert calls[0][0] == "ISSUES"


# ── ★ 切「真拦」后的闸门级验收 ②④ 与「缓存不得复述策略」 ──────────────


@pytest.mark.asyncio
async def test_gate_lets_explicit_low_through(audit_env):  # noqa: F811
    """★ 验收②：明确 `SEVERITY:low` ⇒ **不拦**（闸门 exit_code=0）。

    这条与上面那条是一对：没有它，"闸门永远拦"也能让上一条绿 ——
    那是把闸门关死冒充修缺陷。
    """

    async def call_llm(system: str, user: str) -> str:
        return "VERDICT: ISSUES\nsrc/a.py:1 SEVERITY:low 命名风格\n"

    result, gates, logs = await _run_audit_capturing(call_llm)

    assert result.get("audited") is True, result
    assert gates, gates
    assert gates[-1]["exit_code"] == 0, (
        f"明确 low 不该拦门（fail-safe 只兜「解析不出」）：{gates[-1]}")


@pytest.mark.asyncio
async def test_gate_treats_conflict_by_first_prefix_and_logs_it(audit_env):  # noqa: F811
    """★ 验收④：`SEVERITY:low … 【high】` ⇒ 按 low（不拦）+ **有日志说明忽略了后文**。"""

    async def call_llm(system: str, user: str) -> str:
        return "VERDICT: ISSUES\nsrc/a.py:1 SEVERITY:low 但这段在喊【high】严重\n"

    result, gates, logs = await _run_audit_capturing(call_llm)

    assert result.get("audited") is True, result
    assert gates[-1]["exit_code"] == 0, (
        "冲突用例应按**首个前缀**（low）处理 ⇒ 不拦；"
        f"若这里被拦，说明冲突规则退化成'看到 high 就拦'：{gates[-1]}"
    )
    ign = _intercept_event(logs, "code_audit_severity_conflict_ignored")
    assert ign is not None, (
        "冲突被静默吸收 —— 必须有日志说明「忽略了后文」"
        f"（实际事件：{[e.get('event') for e in logs]}）"
    )
    assert ign["conflicts"] == 1, ign


@pytest.mark.asyncio
async def test_cached_row_decision_is_recomputed_under_current_policy(
    audit_env,  # noqa: F811
    monkeypatch,
):
    """★ **缓存只回放事实，不复述策略**（切真拦的关键副作用，实测出来的）。

    现场：缓存行的 `exit_code` 是**写它那一刻的策略**算出来的。shadow 期写下的
    行里，`【high】` 那批 `exit_code=0`（那时真闸门按 legacy 不拦）⇒ 若直接回放，
    **切换会被缓存静默抵消**：同一份 diff（新策略下"该拦"）照旧拿到放行凭证。

    判据：喂一条 shadow 期风格的缓存行（`exit_code=0` + `【high】` issue），
    命中后**新凭证的 exit_code 必须是 1**，且留下 `..._decision_recomputed` 日志。

    桩法说明：只桩**存储层**（`audit_cache_lookup`）—— 判定与凭证创建都走真实现。
    """
    from unittest.mock import AsyncMock

    from hiveweave.services.attestation import attestation_service as _svc

    stale_row = {
        "verdict": "ISSUES",
        "exit_code": 0,                      # ← shadow 期写的（legacy 不拦）
        "top_issues": '["src/a.py:1 【high】未校验用户输入"]',
        "source_attestation_id": "att-old-1",
        "attestation_id": "att-old-1",
    }

    async def _never_called_llm(system: str, user: str) -> str:  # pragma: no cover
        raise AssertionError("命中缓存不该烧 LLM")

    p_wt, p_git, p_save = _run_audit_patches()
    gates: list[dict] = []
    real_create = _svc.create

    async def _spy(*args, **kwargs):
        gates.append(kwargs)
        return await real_create(*args, **kwargs)

    # ⚠ `monkeypatch.setattr` 返回 None ⇒ **不能**放进 `with (...)` 元组
    monkeypatch.setattr(
        _svc, "audit_cache_lookup", AsyncMock(return_value=stale_row)
    )
    with (
        p_wt, p_git, p_save,
        patch.object(_svc, "create", new=_spy),
        structlog.testing.capture_logs() as logs,
    ):
        result = await run_code_audit(
            PROJECT_ID, AGENT_ID, call_llm=_never_called_llm
        )

    assert result.get("audited") is True, result
    reuse = [g for g in gates if "cached-reuse" in str(g.get("command_or_url") or "")]
    assert reuse, f"没走到缓存复用路径：{[g.get('command_or_url') for g in gates]}"
    assert reuse[-1]["exit_code"] == 1, (
        "缓存行是旧策略写的（exit_code=0），但新策略下这份 diff 该拦 ⇒ "
        f"必须按当前策略重算，不能回放旧决定：{reuse[-1]}"
    )
    recomputed = _intercept_event(logs, "code_audit.cached_decision_recomputed")
    assert recomputed is not None, (
        "重算这件事必须有状态留痕（否则'缓存抵消了切换'没人看得见）："
        f"{[e.get('event') for e in logs]}"
    )
    assert recomputed["stored_exit"] == 0 and recomputed["recomputed_exit"] == 1, recomputed


@pytest.mark.asyncio
async def test_cached_row_with_missing_facts_stays_blocking(
    audit_env,  # noqa: F811
    monkeypatch,
):
    """★ **事实缺失 ≠ 事实为空**（审计 M-1，实测复现的阻断项）。

    存量缓存行是升级前写的、`top_issues` 为 **NULL**（`tools/code_audit.py:114` 自陈）。
    `_parse_cached_issues(None) → []` ⇒ 若直接复算，`failsafe_high=0` ⇒
    **重算成放行** —— 而该行原本 `exit_code=1`（旧 `[high]` 判据拦下的）在切换前是
    **拦**。那等于"重算"这一步**新开了一个 fail-open**，方向正是 #12 要消灭的。
    ⇒ 事实不足以复算时按 fail-safe 兜（拦），并留 `cached_facts_incomplete` 痕迹。
    """
    from unittest.mock import AsyncMock

    from hiveweave.services.attestation import attestation_service as _svc

    stale_row = {
        "verdict": "ISSUES",
        "exit_code": 1,          # ← 旧判据拦下的
        "top_issues": None,      # ← 存量行：事实缺失（不是"没有 issue"）
        "attestation_id": "att-old-2",
    }

    async def _never_called_llm(system: str, user: str) -> str:  # pragma: no cover
        raise AssertionError("命中缓存不该烧 LLM")

    p_wt, p_git, p_save = _run_audit_patches()
    gates: list[dict] = []
    real_create = _svc.create

    async def _spy(*args, **kwargs):
        gates.append(kwargs)
        return await real_create(*args, **kwargs)

    monkeypatch.setattr(
        _svc, "audit_cache_lookup", AsyncMock(return_value=stale_row)
    )
    with (
        p_wt, p_git, p_save,
        patch.object(_svc, "create", new=_spy),
        structlog.testing.capture_logs() as logs,
    ):
        result = await run_code_audit(
            PROJECT_ID, AGENT_ID, call_llm=_never_called_llm
        )

    assert result.get("audited") is True, result
    reuse = [g for g in gates if "cached-reuse" in str(g.get("command_or_url") or "")]
    assert reuse, f"没走到缓存复用路径：{[g.get('command_or_url') for g in gates]}"
    assert reuse[-1]["exit_code"] == 1, (
        "事实缺失被当成了「事实为空」⇒ 把一条本该拦的缓存行重算成放行 —— "
        f"这是新开的 fail-open，必须按 fail-safe 兜：{reuse[-1]}"
    )
    incomplete = _intercept_event(logs, "code_audit.cached_facts_incomplete")
    assert incomplete is not None, (
        "「事实缺失」必须与「策略重算」分开留痕（否则事后分不清是哪一种）："
        f"{[e.get('event') for e in logs]}"
    )
