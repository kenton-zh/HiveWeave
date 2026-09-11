"""发布产物冒烟（fixlist L7 / P0-4）。

DSH ``docs/testing.md:41`` 原文：

    **"Real entry path" means the published artifact**: a package `bin` runs
    built `lib/bin.js` under plain `node`, exposing failures tsx masks (...).
    Keep the built-artifact smokes green ..., and **assert a genuinely-missing
    config exits non-zero**.

**为什么本项目必须有它**：我们的 `code_fingerprint` 对打包形态**无效** —— 它
记录的 `src_root` 是 `dist/HiveWeave/_internal`（打包内的冻结副本），所以
「源码改了没重打包」在它的设计里看不见（F14 只对源码模式有效）。发布产物必须
单独有一条冒烟（DSH 同旨：`AGENTS.md:120` **Source plane vs artifact plane,
never mixed**）。

**它兜住的两个已知事故**：
  1. P0-4 —— 打包 EXE 不带 `.env` → 预算静默回落到代码默认值（实测
     `timeout_s=570.0` 而 `.env` 写 1710，4 个 run 精确死在 600.0s）；
  2. L7 —— 产物平面没有独立的「启动 → 读生效配置 → 断言 = 配置源」链路。

用法::

    python scripts/smoke_release.py                    # 跑默认 dist 路径
    python scripts/smoke_release.py --exe <path>       # 指定产物
    python scripts/smoke_release.py --require-env      # 缺 .env 即失败（分发门禁）
    python scripts/smoke_release.py --expect-hard 1710 # 只断言 hard 预算 = 配置源
    python scripts/smoke_release.py --expect-config '{"hard_s": 1710}'  # 显式全量对照

退出码：0=通过 · 1=失败 · 2=产物不存在（便于 CI 区分「没构建」与「构建坏了」）。

**断言为什么必须做在「键级」**（批次 6 项 2 纪律）：`effective_budget()` 只在
返回**空 dict** 时才让 `assert_release_healthy` 报 `unreadable`；一旦它开始
返回**部分键**（本次改动正是如此），dict 非空 ⇒ **缺失的键静默通过**。
所以期望值从 `hard_s` 单键泛化为四键 dict，且校验按**每个键**逐一轮询 ——
少一个键就报那个键的名字，绝不静默。
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_EXE = _REPO_ROOT / "apps" / "desktop" / "dist" / "HiveWeave" / "HiveWeave.exe"

SELFCHECK_TIMEOUT_S = 180.0

#: 配置源 env 名 → `effective_budget()` 键名。**顺序即断言顺序。**
#:
#: 这张表是「键级泛化」的唯一真值 —— 加键只改这里 + `effective_budget()`，
#: 两边键名必须逐字一致（键名 `llm_concurrency` **不带 `_s` 后缀**，因为它
#: 单位不是秒，见 `code_fingerprint.effective_budget` docstring）。
CONFIG_KEYS: dict[str, str] = {
    "HIVEWEAVE_STREAM_HARD_TIMEOUT_S": "hard_s",
    "HIVEWEAVE_STREAM_TOTAL_TIMEOUT_S": "soft_s",
    "HIVEWEAVE_STREAM_AGENT_CEILING_S": "ceiling_s",
    "HIVEWEAVE_LLM_MAX_CONCURRENT": "llm_concurrency",
}


def parse_selfcheck(stdout: str) -> dict:
    """把 ``--selfcheck`` 的输出解析成结构化结果。

    纯函数（不依赖真进程），所以它自己可以被单测覆盖 —— 冒烟脚本的**断言
    逻辑**必须有回归保护，否则它会静默退化成"总是通过"。
    """
    out: dict = {
        "ok": False,
        "frozen": None,
        "budget": None,
        "budget_raw": None,
        "env_file_present": None,
        "warn_lines": [],
        "fail_line": None,
    }
    for raw in (stdout or "").splitlines():
        line = raw.strip()
        if line.startswith("SELFCHECK OK"):
            out["ok"] = True
        elif line.startswith("SELFCHECK FAIL"):
            out["fail_line"] = line
        elif line.startswith("frozen="):
            out["frozen"] = line.split("=", 1)[1].strip() == "True"
        elif line.startswith("effective_budget="):
            payload = line.split("=", 1)[1].strip()
            out["budget_raw"] = payload
            try:
                parsed = ast.literal_eval(payload)
                if isinstance(parsed, dict):
                    out["budget"] = parsed
            except (ValueError, SyntaxError):
                out["budget"] = None
        elif line.startswith("frozen_env_file_present="):
            out["env_file_present"] = line.split("=", 1)[1].strip() == "True"
        elif line.startswith("WARN:"):
            out["warn_lines"].append(line)
    return out


def assert_release_healthy(
    parsed: dict,
    *,
    require_env: bool = False,
    expect_hard: float | None = None,
    expect_config: dict | None = None,
) -> list[str]:
    """返回失败原因列表（空 = 通过）。纯函数，供单测覆盖。

    ``expect_config`` 是**键级**期望（键名 = `effective_budget()` 的键名）：
    遍历它每一个键，缺键 / 非法 / 不等都**点名报出该键** —— 这是批次 6 项 2
    的硬纪律：`effective_budget()` 只在**空 dict** 时才触发 `unreadable`，
    只补部分键时 dict 非空 ⇒ 缺失的键会**静默通过**，所以断言必须逐键做。

    ``expect_hard`` 是旧接口（只断言 `hard_s`），保留向后兼容；两者同时给出时
    `expect_config` 优先（更全的一方胜），但仍会照旧断言 `expect_hard`。

    ⚠️ `llm_concurrency` **单位不是秒**：只比值相等，不比较大小、不做正数校验
    （`expect_config` 里的值是「配置源写了什么」，与量纲无关）。
    """
    problems: list[str] = []

    if not parsed.get("ok"):
        problems.append(
            "selfcheck 未报告 OK"
            + (f"（{parsed['fail_line']}）" if parsed.get("fail_line") else "")
        )

    budget = parsed.get("budget")
    if not budget:
        problems.append(
            "生效预算不可读（effective_budget 缺失或无法解析："
            f"{parsed.get('budget_raw')!r}）—— 发布产物必须能自报它用的是哪套预算"
        )
    else:
        # ── 键级断言：期望里的每一个键都必须存在、可解析、且值相符 ──
        expected = expect_config or {}
        for key, want in expected.items():
            if key not in budget:
                # 点名缺哪个键 —— 只说「不可读」会让部分覆盖（补了 2 个缺 2 个）
                # 伪装成健康产物，这正是本批要堵的静默缺口。
                problems.append(
                    f"生效预算缺键 {key!r}（实得键：{sorted(budget)}）"
                    " —— effective_budget() 补了部分键时 dict 非空，缺失键会"
                    "静默通过；冒烟必须按键级断言（批次 6 项 2 纪律）"
                )
                continue
            got = budget.get(key)
            if not isinstance(got, (int, float)) or isinstance(got, bool):
                problems.append(f"生效预算里的 {key} 非法：{got!r}")
                continue
            # llm_concurrency 单位是「个」不是秒 —— 只比值，不比大小。
            if float(got) != float(want):
                shape = (
                    " —— 这正是 P0-4 的形状（产物没吃上 .env）"
                    if key == "hard_s"
                    else ""
                )
                problems.append(
                    f"生效 {key}={got} 与配置源不一致（期望 {want}）{shape}"
                )

        hard = budget.get("hard_s")
        if not isinstance(hard, (int, float)) or hard <= 0:
            problems.append(f"生效预算里的 hard_s 非法：{hard!r}")
        elif expect_hard is not None and float(hard) != float(expect_hard):
            problems.append(
                f"生效 hard_s={hard} 与配置源不一致（期望 {expect_hard}）"
                " —— 这正是 P0-4 的形状（产物没吃上 .env）"
            )

    if require_env and parsed.get("env_file_present") is not True:
        # **fail-closed**：只在"确证 .env 存在"时放行。
        # 若写成 `is False`，那么这行**缺失**（旧产物根本不打印它 / 编码吞掉）
        # 时 env_file_present=None → 门禁静默通过 —— 而冒烟存在的意义之一正是
        # 抓"陈旧产物"，陈旧产物恰恰最可能不打印新行，门禁会在最需要它时失效。
        missing_line = parsed.get("env_file_present") is None
        problems.append(
            "分发门禁：未能确证 .env 存在"
            + ("（产物未打印 frozen_env_file_present —— 可能是改造前的旧产物）"
               if missing_line else "（frozen 产物缺 .env）")
            + " —— 部署调优参数会静默回落到代码默认值。要么随包附一份 .env，"
            "要么显式声明默认值是有意为之（DSH docs/testing.md:41"
            "「assert a genuinely-missing config exits non-zero」）"
        )

    return problems


def _read_env_values(env_file: Path) -> dict[str, float]:
    """解析 `.env` 里 `CONFIG_KEYS` 相关的项 → `{budget 键名: 值}`。"""
    values: dict[str, float] = {}
    try:
        text = env_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        name, _, raw_value = line.partition("=")
        key = CONFIG_KEYS.get(name.strip())
        if key is None or key in values:
            continue
        value = raw_value.strip().strip('"').strip("'")
        try:
            values[key] = float(value)
        except ValueError:
            # 坏值不静默：让它以 `None` 的形式进入期望，断言会点名报出。
            values[key] = None  # type: ignore[assignment]
    return values


def expected_config_from_env(exe: Path) -> dict[str, float] | None:
    """从产物旁的 `.env` 读期望配置（四键，用于默认对照）。

    泛化自 `expected_hard_from_config_source`（批次 6 项 2）：只校验 `hard_s`
    时，新加的 `soft_s` / `llm_concurrency` **永远不被断言**（只取证、无门禁
    价值）。返回 `None` 表示「产物旁无 .env」(本次只做存活检查)；返回**非空
    dict** 表示有配置源，**有几个键就断几个键** —— 「部分覆盖」（只设 HARD
    没设 TOTAL/CEILING/CONCURRENCY）会落成**只含 `hard_s` 的 dict**，此时
    其余键**不做断言**（无从对照），而 `.env` 里**明确写了坏值**的键会以
    `None` 进入期望并在断言里点名 —— 两者语义必须分开，否则「部分覆盖」与
    「写错了」会被混成同一种失败。
    """
    env_file = exe.parent / ".env"
    if not env_file.is_file():
        return None
    values = _read_env_values(env_file)
    if not values:
        # 文件在、但没有一处 tunable（全注释掉）—— 与"文件不在"同义：
        # 没有可对照的 referent。返回 None 而非 `{}`，避免把"空期望"误当
        # "期望全为空"从而让校验退化成空转。
        return None
    return values


def run_smoke(
    exe: Path,
    *,
    require_env: bool = False,
    expect_hard: float | None = None,
    expect_config: dict | None = None,
) -> int:
    if not exe.is_file():
        print(f"[smoke] 产物不存在：{exe}")
        print("[smoke] 先跑 build-exe.bat（注意：重建前必须先 taskkill HiveWeave.exe）")
        return 2

    print(f"[smoke] {exe} --selfcheck")
    try:
        proc = subprocess.run(
            [str(exe), "--selfcheck"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=SELFCHECK_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        print(f"[smoke] FAIL: --selfcheck 超过 {SELFCHECK_TIMEOUT_S:.0f}s 未返回")
        return 1

    stdout = proc.stdout or ""
    print(stdout, end="" if stdout.endswith("\n") else "\n")
    if proc.stderr:
        print(proc.stderr, file=sys.stderr, end="")

    parsed = parse_selfcheck(stdout)
    parsed["returncode"] = proc.returncode

    problems = assert_release_healthy(
        parsed,
        require_env=require_env,
        expect_hard=expect_hard,
        expect_config=expect_config,
    )
    if proc.returncode != 0 and not problems:
        problems.append(f"--selfcheck 退出码 {proc.returncode}")

    if problems:
        print("[smoke] FAIL:")
        for p in problems:
            print(f"  - {p}")
        return 1

    if parsed.get("warn_lines"):
        print("[smoke] 带 WARN 通过（记录在案，未被门禁拦截）：")
        for w in parsed["warn_lines"]:
            print(f"  - {w}")

    print("[smoke] PASS: 发布产物可启动、能自报生效预算")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="发布产物冒烟（L7 / P0-4）")
    parser.add_argument("--exe", default=str(_DEFAULT_EXE), help="产物路径")
    parser.add_argument(
        "--require-env",
        action="store_true",
        help="缺 .env 即失败（分发包门禁用）",
    )
    parser.add_argument(
        "--expect-hard",
        type=float,
        default=None,
        help="只断言的生效 hard 预算（秒）—— 与配置源对照（向后兼容保留）",
    )
    parser.add_argument(
        "--expect-config",
        default=None,
        help=(
            "显式全量期望（JSON/字面 dict），如 "
            '\'{"hard_s": 1710, "soft_s": 1500, "ceiling_s": 1800, '
            '"llm_concurrency": 12}\' —— 键名 = effective_budget() 的键名。'
            "给了它就不再自动读产物旁 .env"
        ),
    )
    args = parser.parse_args(argv)
    exe = Path(args.exe)

    expect_config: dict | None = None
    if args.expect_config is not None:
        try:
            parsed_expect = ast.literal_eval(args.expect_config)
        except (ValueError, SyntaxError) as exc:
            print(f"[smoke] FAIL: --expect-config 无法解析：{exc}")
            return 1
        if not isinstance(parsed_expect, dict):
            print("[smoke] FAIL: --expect-config 必须是 dict 字面量")
            return 1
        expect_config = parsed_expect
        print(f"[smoke] 期望配置（--expect-config）：{expect_config}")

    # 默认自己去找配置源对照（审计 M1）—— 否则 `hard_s=570` 的静默回落
    # 也会被判 PASS，脚本就白写了。批次 6 项 2：对照从「hard 单键」泛化为
    # 「四键」，且断言按键做，缺键点名报出。
    expect_hard = args.expect_hard
    if expect_config is None:
        expect_config = expected_config_from_env(exe)
        if expect_config is not None:
            print(f"[smoke] 期望配置取自产物旁 .env：{expect_config}")
            if "hard_s" in expect_config:
                expect_hard = expect_config["hard_s"]
        else:
            print(
                "[smoke] 未找到配置源（产物旁无 .env）—— 本次只做存活检查，"
                "不校验预算是否等于配置值。要严格对照请传 --expect-config。"
            )

    return run_smoke(
        exe,
        require_env=args.require_env,
        expect_hard=expect_hard,
        expect_config=expect_config,
    )


if __name__ == "__main__":
    raise SystemExit(main())
