"""发布产物冒烟的**断言逻辑**回归（fixlist L7 / P0-4）。

`scripts/smoke_release.py` 只有在「机器上有 dist 产物」时才有意义（要真跑
EXE），但它的**断言逻辑**必须随时可测 —— 否则会静默退化成"总是通过"，而那种
守卫比没有更糟（它给人假的安心）。

本文件用 importlib 从文件路径加载该脚本（它在仓库根的 `scripts/` 下，不在
backend 包内），只测纯函数 `parse_selfcheck` / `assert_release_healthy`。
"""

from __future__ import annotations

import importlib.util
import pathlib

_SCRIPT = (
    pathlib.Path(__file__).resolve().parents[3] / "scripts" / "smoke_release.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("_smoke_release", _SCRIPT)
    assert spec is not None and spec.loader is not None, f"找不到冒烟脚本：{_SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_mod = _load_module()


def _output(*, ok=True, budget="{'hard_s': 1710.0, 'ceiling_s': 1800.0}",
            env_present=True, warn=False):
    lines = [
        "frozen=True",
        "data_root=D:\\x\\data",
        f"effective_budget={budget}",
        "frozen_env_file_present=" + ("True" if env_present else "False"),
    ]
    if warn:
        lines.append("WARN: frozen build without .env — tunables fall back")
    lines.append("SELFCHECK OK" if ok else "SELFCHECK FAIL: ball_static_dir_missing")
    return "\n".join(lines)


# ── parse_selfcheck ──────────────────────────────────────────


def test_parses_a_healthy_output():
    parsed = _mod.parse_selfcheck(_output())
    assert parsed["ok"] is True
    assert parsed["frozen"] is True
    assert parsed["env_file_present"] is True
    assert parsed["budget"] == {"hard_s": 1710.0, "ceiling_s": 1800.0}


def test_parses_failure_and_warn_lines():
    parsed = _mod.parse_selfcheck(_output(ok=False, env_present=False, warn=True))
    assert parsed["ok"] is False
    assert "ball_static_dir_missing" in (parsed["fail_line"] or "")
    assert parsed["warn_lines"], "WARN 行必须被收集（它承载 P0-4 的显式告警）"


def test_unparsable_budget_does_not_crash():
    """预算串坏掉时解析要返回 None 而不是抛 —— 冒烟脚本不能因此崩掉。"""
    parsed = _mod.parse_selfcheck(_output(budget="<not a dict>"))
    assert parsed["budget"] is None
    assert parsed["budget_raw"] == "<not a dict>"


# ── assert_release_healthy ───────────────────────────────────


def test_healthy_release_passes():
    parsed = _mod.parse_selfcheck(_output())
    assert _mod.assert_release_healthy(parsed, expect_hard=1710.0) == []


def test_missing_selfcheck_ok_is_a_problem():
    parsed = _mod.parse_selfcheck("frozen=True\n")
    problems = _mod.assert_release_healthy(parsed)
    assert any("selfcheck 未报告 OK" in p for p in problems)


def test_unreadable_budget_is_a_problem():
    """发布产物必须能自报它用的是哪套预算 —— 读不到就是问题（不是"无预算"）。"""
    parsed = _mod.parse_selfcheck(_output(budget="{}"))
    problems = _mod.assert_release_healthy(parsed)
    assert any("生效预算不可读" in p for p in problems)


def test_budget_mismatch_is_reported_as_the_p0_4_shape():
    """生效值与配置源不一致 = P0-4 的形状（产物没吃上 .env）。"""
    parsed = _mod.parse_selfcheck(
        _output(budget="{'hard_s': 570.0, 'ceiling_s': 600.0}")
    )
    problems = _mod.assert_release_healthy(parsed, expect_hard=1710.0)
    assert any("P0-4" in p for p in problems)


def test_negative_hard_budget_is_a_problem():
    parsed = _mod.parse_selfcheck(_output(budget="{'hard_s': -1.0}"))
    problems = _mod.assert_release_healthy(parsed)
    assert any("hard_s 非法" in p for p in problems)


def test_require_env_only_fails_when_asked():
    """缺 .env：默认只是 WARN（开源分发可能有意用默认值），门禁模式才失败。"""
    parsed = _mod.parse_selfcheck(_output(env_present=False, warn=True))

    assert _mod.assert_release_healthy(parsed) == [], (
        "非门禁模式不该因缺 .env 失败"
    )

    problems = _mod.assert_release_healthy(parsed, require_env=True)
    assert any("分发门禁" in p for p in problems)


def test_require_env_passes_when_env_present():
    parsed = _mod.parse_selfcheck(_output(env_present=True))
    assert _mod.assert_release_healthy(parsed, require_env=True) == []


def test_missing_artifact_exit_code_is_two(tmp_path):
    """产物不存在 → 退出码 2（CI 借此区分「没构建」与「构建坏了」）。"""
    rc = _mod.run_smoke(tmp_path / "nope.exe")
    assert rc == 2


# ── 审计 B1：分发门禁必须 fail-closed ────────────────────────


def test_require_env_fails_when_the_line_is_absent():
    """审计 B1：`frozen_env_file_present` **行缺失**时必须失败。

    修前的判据是 `env_file_present is False` —— 行缺失时该值为 None，
    `None is False → False`，门禁**静默通过**。而冒烟存在的意义之一正是抓
    "陈旧产物"，陈旧产物恰恰最可能不打印新行 → 门禁在最需要它时失效。
    """
    parsed = _mod.parse_selfcheck("frozen=True\neffective_budget={'hard_s': 1710.0}\nSELFCHECK OK")
    assert parsed["env_file_present"] is None, "前提：该行确实缺失"

    problems = _mod.assert_release_healthy(parsed, require_env=True)
    assert any("未能确证 .env 存在" in p for p in problems), (
        "行缺失必须判失败（fail-closed），不能因为'不是明确的 False'就放行"
    )


# ── 审计 M1：默认必须自己去找配置源 ──────────────────────────


def test_expected_hard_is_read_from_the_env_beside_the_artifact(tmp_path):
    """审计 M1：默认对照值应取自产物旁的 `.env`，否则 570 的静默回落也判 PASS。"""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "HIVEWEAVE_STREAM_HARD_TIMEOUT_S=1710\nOTHER=x\n", encoding="utf-8"
    )
    assert _mod.expected_hard_from_config_source(tmp_path / "HiveWeave.exe") == 1710.0


def test_expected_hard_is_none_without_a_config_source(tmp_path):
    assert _mod.expected_hard_from_config_source(tmp_path / "HiveWeave.exe") is None


def test_expected_hard_tolerates_quotes_and_bad_values(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text('HIVEWEAVE_STREAM_HARD_TIMEOUT_S="1710"\n', encoding="utf-8")
    assert _mod.expected_hard_from_config_source(tmp_path / "x.exe") == 1710.0

    env_file.write_text("HIVEWEAVE_STREAM_HARD_TIMEOUT_S=abc\n", encoding="utf-8")
    assert _mod.expected_hard_from_config_source(tmp_path / "x.exe") is None
