"""#12：`code_audit` severity 解析改 fail-safe + shadow 观测（验收 ①②③④）。

设计背景见 `fixplan-16items-2026-09-14.md` §三 #12。要点：
- **fail-safe**：severity **解析不出 ⇒ 视为 high（拦门）**。旧判据 `"[high]" in issue`
  是 **fail-open** 的 → 把"写成了别的形态"静默当成"不是 high" ⇒ 换标点/换语言即放行。
- **冲突规则**：`SEVERITY:low … 【high】` ⇒ **只认首个 `SEVERITY:` 前缀**，后文忽略。
- **shadow 试运行**：fail-safe 判定**生效但只记录不拦** —— 先量真实比例再切真拦。

⚠ 本文件**同时**钉住"旧闸门此刻仍按 legacy 判定"这一条 —— 否则 shadow 就成了偷偷
上线 fail-safe，那会让误拦率在无数据的情况下直接生效。
"""

from __future__ import annotations

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
async def test_shadow_records_failsafe_decision_without_blocking(
    audit_env,  # noqa: F811 —— fixture 参数，不是重复定义
):
    """★ 端到端：一条 `【high】` 的 issue ⇒ shadow 判定为"该拦"，但**真闸门不拦**。

    这条同时证明三件事：
      1. shadow 的判定真的被**算出来并落观测**（不是只写在注释里）；
      2. **真闸门此刻仍按 legacy** ⇒ 没有偷偷上线 fail-safe（否则误拦率会在无数据时生效）；
      3. `would_flip=True` ⇒ **这一条就是上线 fail-safe 后会新被拦下的样本**（误拦率的分母）。
    """

    async def call_llm(system: str, user: str) -> str:
        # 全角括号 —— 旧判据漏、fail-safe 兜。
        return "VERDICT: ISSUES\nsrc/a.py:1 【high】未校验用户输入\n"

    p_wt, p_git, p_save = _run_audit_patches()
    with p_wt, p_git, p_save, structlog.testing.capture_logs() as logs:
        result = await run_code_audit(PROJECT_ID, AGENT_ID, call_llm=call_llm)

    assert isinstance(result, dict)
    assert result.get("audited") is True, f"审计本身应完成：{result}"

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
        "★ shadow 期间**真闸门必须仍按 legacy**（不拦）—— 否则就是偷偷上线 fail-safe，"
        "误拦率会在没有任何数据的情况下直接生效"
    )
    assert shadow["would_flip"] is True, "这条正是上线后会新被拦下的样本"
