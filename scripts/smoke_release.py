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
    python scripts/smoke_release.py --expect-hard 1710 # 断言预算 = 配置源

退出码：0=通过 · 1=失败 · 2=产物不存在（便于 CI 区分「没构建」与「构建坏了」）。
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
) -> list[str]:
    """返回失败原因列表（空 = 通过）。纯函数，供单测覆盖。"""
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


def expected_hard_from_config_source(exe: Path) -> float | None:
    """从产物旁的 `.env` 读期望的 hard 预算（用于默认对照）。

    审计 M1：`--expect-hard` 默认 ``None`` 时，断言只校验 ``hard_s > 0`` ——
    于是**静默回落到 570**（正是 P0-4 的形状）也会 PASS，与"这条冒烟兜住
    P0-4"的声明不符。默认必须自己去找配置源，而不是靠人记得加参数。
    """
    env_file = exe.parent / ".env"
    if not env_file.is_file():
        return None
    try:
        text = env_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("HIVEWEAVE_STREAM_HARD_TIMEOUT_S="):
            continue
        value = line.split("=", 1)[1].strip().strip('"').strip("'")
        try:
            return float(value)
        except ValueError:
            return None
    return None


def run_smoke(
    exe: Path,
    *,
    require_env: bool = False,
    expect_hard: float | None = None,
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
        parsed, require_env=require_env, expect_hard=expect_hard
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
        help="断言的生效 hard 预算（秒）—— 与配置源对照",
    )
    args = parser.parse_args(argv)
    exe = Path(args.exe)

    # 默认自己去找配置源对照（审计 M1）—— 否则 `hard_s=570` 的静默回落
    # 也会被判 PASS，脚本就白写了。
    expect_hard = args.expect_hard
    if expect_hard is None:
        expect_hard = expected_hard_from_config_source(exe)
        if expect_hard is not None:
            print(f"[smoke] 期望 hard 预算取自产物旁 .env：{expect_hard}")
        else:
            print(
                "[smoke] 未找到配置源（产物旁无 .env）—— 本次只做存活检查，"
                "不校验预算是否等于配置值。要严格对照请传 --expect-hard。"
            )

    return run_smoke(
        exe,
        require_env=args.require_env,
        expect_hard=expect_hard,
    )


if __name__ == "__main__":
    raise SystemExit(main())
