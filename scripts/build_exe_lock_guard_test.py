#!/usr/bin/env python
"""`build-exe.bat` 的 `[0/5]` 锁门禁 —— 阳性对照 + 结构性 lint。

为什么要这个脚本
----------------
`[0/5]` 是「产物目录被占用就立即中止」的门禁。它**只在真要构建时才起作用**，
所以光读代码判断"应该没问题"是本项目反复吃亏的地方（2026-09-15 实测：这份
未提交的重写把 `[0/5]` 整段改到**不可解析** —— 构建根本起不来，而读代码看不出来）。

本脚本把 `[0/5]` 区域（含 `[3/5]` 前的不变式）**从 `build-exe.bat` 里逐字切片**
跑四种情形 —— **不重写、不复刻**，所以测的就是仓库里那份代码。切片方式保证
任何一次改动都会立刻反映到测试里。

四个情形（每个都有明确的正/反预期）
----------------------------------
  1. 只有探针残留          ⇒ 自愈：`ren` 回产物目录，构建继续（rc=0）
  2. 残留与产物目录并存    ⇒ fail-loud（rc=1）。旧写法在此会因 `ren` **目标重名**
                             失败而误报"目录被占用"，并提示一个对此**完全无效**的
                             `taskkill` —— 本用例就是钉住这条
  3. 两者都没有（首次构建） ⇒ 照常继续（rc=0）
  4. 子文件被独占（＝产物在跑）⇒ 中止（rc=1），**且目录必须原样不动**
                             （探针不能把它留在 `.__lockprobe__`）

⚠ 运行环境的一个坑（踩过，会让人得出反向结论）
--------------------------------------------
必须用**干净的 Windows PATH** 跑：在 Git Bash 下 `find` 会解析到 GNU find，
于是 `tasklist | find /I ...` 那行报 `find: '/I': No such file or directory`，
门禁行为与真实构建**不一致**。本脚本已把 PATH 收成 System32，不继承 Git Bash。

用法
----
    python scripts/build_exe_lock_guard_test.py

退出码 0 = 全部通过；1 = 有用例失败；2 = 无法切片（`build-exe.bat` 结构变了
⇒ 本脚本必须跟着改，不许猜）。
"""

from __future__ import annotations

import ctypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BAT = REPO / "build-exe.bat"

#: Windows 自带工具所在的干净 PATH —— 见模块 docstring 的"运行环境的一个坑"。
CLEAN_PATH = r"C:\Windows\System32;C:\Windows;C:\Windows\System32\Wbem"

GENERIC_READ = 0x80000000
OPEN_EXISTING = 3
INVALID_HANDLE = ctypes.c_void_p(-1).value


def extract_probe_region() -> str:
    """逐字抽出 [0/5] 探针区（`set HW_PROBE=` → `:hw_lock_ok` 标签行）。"""
    lines = BAT.read_text(encoding="utf-8").splitlines()
    start = next(
        (i for i, l in enumerate(lines) if l.startswith("set HW_PROBE=")), None
    )
    end = next((i for i, l in enumerate(lines) if l.strip() == ":hw_lock_ok"), None)
    if start is None or end is None or end <= start:
        print("FATAL: cannot locate the [0/5] probe region in build-exe.bat")
        print("  (expected `set HW_PROBE=` ... `:hw_lock_ok`)")
        raise SystemExit(2)
    return "\n".join(lines[start : end + 1])


def extract_invariant_region() -> str:
    """逐字抽出 [3/5] 前的不变式块（扁平写法：以 `:hw_inv_ok` 标签收尾）。"""
    lines = BAT.read_text(encoding="utf-8").splitlines()
    start = next(
        (
            i
            for i, l in enumerate(lines)
            if l.startswith('if not "%HW_PROBED%"=="1" goto :hw_inv_ok')
        ),
        None,
    )
    if start is None:
        print("FATAL: cannot locate the [3/5] invariant block in build-exe.bat")
        print('  (expected `if not "%HW_PROBED%"=="1" goto :hw_inv_ok`)')
        raise SystemExit(2)
    end = next(
        (j for j in range(start, len(lines)) if lines[j].strip() == ":hw_inv_ok"),
        None,
    )
    if end is None:
        print("FATAL: invariant block has no `:hw_inv_ok` label")
        raise SystemExit(2)
    return "\n".join(lines[start : end + 1])


def make_bat(workdir: Path, out: Path, hw_procs: str) -> Path:
    """拼出只含 [0/5] + 不变式的测试脚本（区域逐字取自 build-exe.bat）。"""
    body = "\n".join(
        [
            "@echo off",
            "setlocal enabledelayedexpansion",
            f"set OUT={out}",
            f"set HW_PROCS={hw_procs}",
            extract_probe_region(),
            "echo __PROCEEDED__",
            extract_invariant_region(),
            "echo __REACHED_3_5__",
            "endlocal",
            "exit /b 0",
        ]
    )
    p = workdir / "probe_harness.bat"
    p.write_text(body, encoding="utf-8")
    return p


def run(bat: Path, cwd: Path) -> tuple[int, str]:
    env = dict(os.environ)
    env["PATH"] = CLEAN_PATH  # 见 docstring：别让 GNU find 把判据弄反
    r = subprocess.run(
        ["cmd", "/c", str(bat)],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def exclusive_open(path: Path):
    """以 dwShareMode=0 打开文件 ⇒ 独占。这是"产物在跑"的最小复现。"""
    h = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW(
        str(path), GENERIC_READ, 0, None, OPEN_EXISTING, 0, None
    )
    if h == INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    return h


def scenario(
    name: str,
    setup,
    expect_rc: int,
    expect_in: tuple[str, ...],
    expect_not_in: tuple[str, ...] = (),
    hw_procs: str = "0",
) -> bool:
    root = Path(tempfile.mkdtemp(prefix="hw_lock_guard_"))
    try:
        dist = root / "dist"
        dist.mkdir(parents=True)
        out = dist / "HiveWeave"
        postcheck = setup(dist, out) or (lambda d, o: [])
        bat = make_bat(root, out, hw_procs)
        rc, text = run(bat, root)

        problems: list[str] = []
        if rc != expect_rc:
            problems.append(f"exit code {rc} != expected {expect_rc}")
        for s in expect_in:
            if s not in text:
                problems.append(f"missing in output: {s!r}")
        for s in expect_not_in:
            if s in text:
                problems.append(f"unexpectedly present: {s!r}")
        problems += postcheck(dist, out)

        print(f"[{'PASS' if not problems else 'FAIL'}] {name}  (rc={rc})")
        for p in problems:
            print(f"       - {p}")
        return not problems
    finally:
        shutil.rmtree(root, ignore_errors=True)


def check_block_echo_parens() -> list[str]:
    """结构性 lint：块内 echo/REM 文本里**不成对**的 ASCII 右括号 ⇒ 报错。

    机制（2026-09-15 独立复核实测，**不是**推测）：
      · 块内 echo 里**配平**的括号是安全的；
      · 危险的是**不配平**的右括号、且其后还有文本 —— cmd 会在那里闭合块，
        把该行剩下的部分当顶层命令执行（本文件 [0/5] 因此整段不可解析过一次）。
      · 另一条同形陷阱：`%VAR%` 展开后的**值**里含右括号（例如路径落在
        `Program Files (x86)` 下）—— 展开先于解析，效果相同。那条本 lint 查不到，
        靠"路径要么引号包住、要么不假设不含括号"来防。

    保守实现：只判"块深度 ≥1 且以 echo/REM 开头且该行括号不配平"。
    """
    lines = BAT.read_text(encoding="utf-8").splitlines()
    depth = 0
    bad: list[str] = []
    for i, raw in enumerate(lines, 1):
        s = raw.strip()
        if s.startswith(")"):
            depth = max(0, depth - 1)
            if s.endswith("("):  # `) else (`
                depth += 1
            continue
        if s.endswith("("):
            depth += 1
            continue
        if depth >= 1 and (s.lower().startswith(("echo", "rem"))):
            body = raw.replace("^(", "").replace("^)", "")
            # 双引号内的括号不算（cmd 也不把它们当块分隔）
            out_chars, in_q = [], False
            for ch in body:
                if ch == '"':
                    in_q = not in_q
                    continue
                if not in_q:
                    out_chars.append(ch)
            plain = "".join(out_chars)
            if plain.count("(") != plain.count(")"):
                bad.append(f"  line {i} (block depth {depth}): {s[:96]}")
    return bad


def main() -> int:
    ok: list[bool] = []
    problems: list[str] = []

    def s1(dist: Path, out: Path):
        lp = dist / "HiveWeave.__lockprobe__"
        lp.mkdir()
        (lp / "MARKER.txt").write_text("stand-in for user data")

        def postcheck(dist: Path, out: Path) -> list[str]:
            errs = []
            if not out.is_dir():
                errs.append("product dir was NOT restored")
            if not (out / "MARKER.txt").exists():
                errs.append("marker did not come back with the dir")
            if (dist / "HiveWeave.__lockprobe__").exists():
                errs.append("probe leftover still present")
            return errs

        return postcheck

    ok.append(
        scenario(
            "1. only probe leftover -> self-heal, then proceed",
            s1,
            0,
            ("recovering probe leftover", "__REACHED_3_5__"),
        )
    )

    def s2(dist: Path, out: Path):
        (dist / "HiveWeave.__lockprobe__").mkdir()
        out.mkdir()

        def postcheck(dist: Path, out: Path) -> list[str]:
            errs = []
            if not out.is_dir() or not (dist / "HiveWeave.__lockprobe__").is_dir():
                errs.append("nothing may be moved/deleted on the fail-loud path")
            return errs

        return postcheck

    ok.append(
        scenario(
            "2. leftover + product dir both exist -> fail-loud (NOT a lock)",
            s2,
            1,
            ("BOTH exist", "taskkill will not help"),
            ("prod dir: locked=1", "__PROCEEDED__"),
        )
    )

    ok.append(
        scenario(
            "3. fresh build, neither exists -> proceed",
            lambda d, o: None,
            0,
            ("__REACHED_3_5__",),
        )
    )

    def s4(dist: Path, out: Path):
        (out / "data").mkdir(parents=True)
        target = out / "data" / "hiveweave.db"
        target.write_bytes(b"stand-in for the meta DB")
        handle = exclusive_open(target)

        def postcheck(dist: Path, out: Path) -> list[str]:
            ctypes.WinDLL("kernel32").CloseHandle(ctypes.c_void_p(handle))
            errs = []
            if not out.is_dir():
                errs.append("dir must be untouched when the probe reports locked")
            if (dist / "HiveWeave.__lockprobe__").exists():
                errs.append("probe dir left behind after a failed ren-out")
            return errs

        return postcheck

    ok.append(
        scenario(
            "4. child file exclusively held (== running exe) -> abort, dir intact",
            s4,
            1,
            ("BUILD ABORTED", "product directory is in use", "locked=1"),
            ("__PROCEEDED__",),
        )
    )

    print()
    print("--- structural lint: unbalanced ASCII parens in block echo/REM ---")
    problems = check_block_echo_parens()
    if problems:
        print(f"FAIL: {len(problems)} line(s) with unbalanced parens inside a block")
        for p in problems:
            print(p)
    else:
        print("PASS: no unbalanced ASCII parens in block echo/REM text")

    passed = sum(ok)
    print()
    print(f"scenarios: {passed}/{len(ok)} passed; lint: {'FAIL' if problems else 'PASS'}")
    return 0 if (passed == len(ok) and not problems) else 1


if __name__ == "__main__":
    sys.exit(main())
