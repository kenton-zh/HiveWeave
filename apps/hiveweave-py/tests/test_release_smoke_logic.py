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


def _output(*, ok=True,
            budget="{'hard_s': 1710.0, 'soft_s': 1500.0, "
                   "'ceiling_s': 1800.0, 'llm_concurrency': 12.0}",
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
    assert parsed["budget"] == {
        "hard_s": 1710.0,
        "soft_s": 1500.0,
        "ceiling_s": 1800.0,
        "llm_concurrency": 12.0,
    }


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


def test_expected_config_is_read_from_the_env_beside_the_artifact(tmp_path):
    """审计 M1（批次 6 项 2 泛化）：默认对照值应取产物旁 `.env` 的**四键**。"""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "HIVEWEAVE_STREAM_HARD_TIMEOUT_S=1710\n"
        "HIVEWEAVE_STREAM_TOTAL_TIMEOUT_S=1500\n"
        "HIVEWEAVE_STREAM_AGENT_CEILING_S=1800\n"
        "HIVEWEAVE_LLM_MAX_CONCURRENT=12\n"
        "OTHER=ignored\n",
        encoding="utf-8",
    )
    got = _mod.expected_config_from_env(tmp_path / "HiveWeave.exe")
    assert got == {
        "hard_s": 1710.0,
        "soft_s": 1500.0,
        "ceiling_s": 1800.0,
        "llm_concurrency": 12.0,
    }
    assert "OTHER" not in got and "ignored" not in got


def test_expected_config_is_none_without_a_config_source(tmp_path):
    assert _mod.expected_config_from_env(tmp_path / "HiveWeave.exe") is None


def test_expected_config_tolerates_quotes(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text('HIVEWEAVE_STREAM_HARD_TIMEOUT_S="1710"\n', encoding="utf-8")
    got = _mod.expected_config_from_env(tmp_path / "x.exe")
    assert got == {"hard_s": 1710.0}


# ── 批次 6 项 2：期望解析的「部分覆盖」与「全缺」两组 ─────────


def test_partial_env_yields_a_partial_expectation(tmp_path):
    """「部分覆盖」：`.env` 只设 HARD，没设 TOTAL/CEILING/CONCURRENCY。

    语义必须与「写错值」分开：没有的键**无从对照**，不进期望 dict（不做断言）；
    而**写了但坏掉**的键要进期望并在断言里点名。若把两者混成一样，就分不清
    "运维有意只调一项" 与 "运维写错了"。
    """
    env_file = tmp_path / ".env"
    env_file.write_text(
        "HIVEWEAVE_STREAM_HARD_TIMEOUT_S=1710\n# 其余 tunable 未设置\n",
        encoding="utf-8",
    )
    got = _mod.expected_config_from_env(tmp_path / "HiveWeave.exe")
    assert got == {"hard_s": 1710.0}, "只覆盖的键才进期望"


def test_partial_env_does_not_assert_the_absent_keys(tmp_path):
    """部分覆盖时，未覆盖的键**不做**断言（否则会把健康产物判死）。"""
    env_file = tmp_path / ".env"
    env_file.write_text("HIVEWEAVE_STREAM_HARD_TIMEOUT_S=1710\n", encoding="utf-8")
    expect = _mod.expected_config_from_env(tmp_path / "HiveWeave.exe")

    # 产物自报的是代码默认的 soft/ceiling/concurrency —— 与 .env 未覆盖项无关
    parsed = _mod.parse_selfcheck(
        _output(
            budget="{'hard_s': 1710.0, 'soft_s': 540.0, "
            "'ceiling_s': 600.0, 'llm_concurrency': 8.0}"
        )
    )
    assert _mod.assert_release_healthy(parsed, expect_config=expect) == []


def test_fully_missing_config_source_yields_none(tmp_path):
    """「全缺」两组之一：`.env` 根本不存在 → None（只做存活检查）。

    返回 None（而非 `{}`）是关键：`{}` 会被下游当成"期望全为空"从而让
    键级校验退化成空转 —— 那正是静默缺口。
    """
    assert _mod.expected_config_from_env(tmp_path / "HiveWeave.exe") is None


def test_all_commented_env_yields_none(tmp_path):
    """「全缺」两组之二：`.env` 在、但 tunable 全被注释掉。

    这正是 `.env.example` 的形状 —— 照抄它会"看似有配置、实际零对照"，
    分发行为仍随代码默认值漂移。故必须与"文件不在"同义（None）。
    """
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# HIVEWEAVE_STREAM_HARD_TIMEOUT_S=570\n"
        "# HIVEWEAVE_STREAM_TOTAL_TIMEOUT_S=540\n"
        "# HIVEWEAVE_LLM_MAX_CONCURRENT=12\n",
        encoding="utf-8",
    )
    assert _mod.expected_config_from_env(tmp_path / "HiveWeave.exe") is None


# ── 批次 6 项 2：断言必须在「键级」（部分覆盖时缺键不得静默）────


def test_missing_key_in_budget_is_named_not_silent():
    """**本批的核心回归**：产物只补了部分键时，缺的那个键必须被点名。

    修前 `assert_release_healthy` 只看 `hard_s`；`effective_budget()` 补了
    soft/ceiling/concurrency 后，若产物漏报 `llm_concurrency`，dict 非空 ⇒
    旧断言**静默通过** —— 新键只取证、无门禁价值。
    """
    parsed = _mod.parse_selfcheck(
        _output(budget="{'hard_s': 1710.0, 'ceiling_s': 1800.0}")
    )
    problems = _mod.assert_release_healthy(
        parsed,
        expect_config={
            "hard_s": 1710.0,
            "soft_s": 1500.0,
            "ceiling_s": 1800.0,
            "llm_concurrency": 12.0,
        },
    )
    assert any("缺键 'soft_s'" in p for p in problems), problems
    assert any("缺键 'llm_concurrency'" in p for p in problems), problems
    assert not any("缺键 'hard_s'" in p for p in problems), "存在的键不该被报缺"


def test_llm_concurrency_mismatch_is_reported():
    """`llm_concurrency` 单位不是秒：**只比值**，不符即报。"""
    parsed = _mod.parse_selfcheck(
        _output(
            budget="{'hard_s': 1710.0, 'soft_s': 1500.0, "
            "'ceiling_s': 1800.0, 'llm_concurrency': 8.0}"
        )
    )
    problems = _mod.assert_release_healthy(
        parsed, expect_config={"llm_concurrency": 12.0}
    )
    assert any("llm_concurrency" in p and "不一致" in p for p in problems), problems


def test_hard_mismatch_still_flagged_as_p0_4_shape():
    parsed = _mod.parse_selfcheck(
        _output(
            budget="{'hard_s': 570.0, 'soft_s': 540.0, "
            "'ceiling_s': 600.0, 'llm_concurrency': 8.0}"
        )
    )
    problems = _mod.assert_release_healthy(
        parsed,
        expect_config={
            "hard_s": 1710.0,
            "soft_s": 1500.0,
            "ceiling_s": 1800.0,
            "llm_concurrency": 12.0,
        },
    )
    assert any("P0-4" in p for p in problems), problems


def test_four_keys_all_correct_passes():
    """四键全对必须通过 —— 断言泛化不能把健康产物判死。"""
    budget = {
        "hard_s": 1710.0,
        "soft_s": 1500.0,
        "ceiling_s": 1800.0,
        "llm_concurrency": 12.0,
    }
    parsed = _mod.parse_selfcheck(_output(budget=repr(budget)))
    assert _mod.assert_release_healthy(parsed, expect_config=budget) == []


def test_config_keys_map_matches_effective_budget_keys():
    """`CONFIG_KEYS` 的键名必须与 `effective_budget()` **双向**逐字一致。

    这是"加键不同步"的**结构性防线**。⚠️ 判据必须取自**运行时**的
    `effective_budget()`，不能手抄一份常量 —— 手抄的那份会在别人改名时
    一起被改掉（或干脆不改），于是测试绿而冒烟永远比不中（"看起来很努力"）。
    这是本仓已栽过的形态：正典清单与测试各写一份 → 漂移。

    双向断言：
    - **冒烟期望的键** 必须都在 `effective_budget()` 里（否则断言永不命中）；
    - `effective_budget()` 产出的键也必须有冒烟覆盖（否则新加的键"只取证、
      无门禁价值"—— 即批次 6 项 2 要堵的静默缺口）。
    """
    from hiveweave.services.code_fingerprint import effective_budget

    live_keys = set(effective_budget().keys())
    smoke_keys = set(_mod.CONFIG_KEYS.values())
    assert smoke_keys == live_keys, (
        f"冒烟键与生效预算键不一致："
        f"仅在冒烟 {sorted(smoke_keys - live_keys)} / "
        f"仅在预算 {sorted(live_keys - smoke_keys)}（两边都必须一致）"
    )
    # 别让上面退化成空集相等（真值必须在）
    assert smoke_keys, "CONFIG_KEYS 不得为空"


def test_config_keys_env_names_are_hiveweave_prefixed():
    """env 名必须带 `HIVEWEAVE_` 前缀 —— 否则 `_bootstrap_dotenv` 与
    `config.py` 的 `env_prefix` 都看不到它（配置通道分裂的老坑）。"""
    for env_name in _mod.CONFIG_KEYS:
        assert env_name.startswith("HIVEWEAVE_"), env_name


def test_expect_hard_backward_compatible():
    """旧接口 `--expect-hard` 语义不得回归（向后兼容）。"""
    parsed = _mod.parse_selfcheck(_output(budget="{'hard_s': 1710.0}"))
    assert _mod.assert_release_healthy(parsed, expect_hard=1710.0) == []

    parsed = _mod.parse_selfcheck(_output(budget="{'hard_s': 570.0}"))
    assert any(
        "P0-4" in p
        for p in _mod.assert_release_healthy(parsed, expect_hard=1710.0)
    )
