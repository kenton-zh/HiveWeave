"""`.vite/` 生成物判定收口的阳性对照（本仓纪律：每条新守卫都要证明"缺陷回来它会转红"）。

用法（任意工作目录）：

    python scripts/vite_generated_path_positive_controls.py

对本次 P0-2 修复的三处判据各做一次「改坏 → 跑 → 确认转红（**且报的是那条分支**）
→ 恢复」。改动只在内存 + 临时写盘里做，`finally` 强制复原并**校验逐字节一致**；
不改动任何其它文件。

对应三条守卫（apps/hiveweave-py/tests/test_checkpoint_dirty_contract.py）：
  ① `.vite/` 正则（REGENERABLE_PATTERNS）——门禁侧 + checkpoint 侧 + merge 入口
  ② vite 引擎种子（ENGINE_GITIGNORE_SEEDS）——`.gitignore` 补条目
  ③ `is_generated_path` 的 GENERATED_FILES 分支——lockfile 不再被门禁硬拒

⚠ 踩过的坑（照 scripts/ratchet_positive_controls.py 的警示）：
  · **分支冒充**：只断言「红了」不够，必须断言**该分支特有的标记串**，否则
    别的失败会冒充命中。本脚本每条对照都断言了独有的测试名/断言串。
  · **静默 no-op**：改坏后必须校验「目标串真的变了」，否则补丁失配会假装通过。
"""

from __future__ import annotations

import io
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1] / "apps" / "hiveweave-py"
CONSTANTS = ROOT / "src" / "hiveweave" / "services" / "git_worktree" / "constants.py"
TEST_FILE = ROOT / "tests" / "test_checkpoint_dirty_contract.py"
PY = ROOT / ".venv" / "Scripts" / "python.exe"


def _load(p: pathlib.Path) -> tuple[str, str]:
    s = io.open(p, encoding="utf-8", newline="").read()
    return s, ("\r\n" if "\r\n" in s else "\n")


def _run_tests() -> tuple[int, str]:
    r = subprocess.run(
        [str(PY), "-m", "pytest", str(TEST_FILE), "-q", "-p", "no:randomly"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def control(name: str, target: str, replacement: str, expect_tests: list[str], marker: str) -> bool:
    """禁用 *target* 一条 → 跑测试 → *expect_tests* 必须全红且输出含 *marker*。"""
    orig, nl = _load(CONSTANTS)
    if orig.count(target) != 1:
        print(f"  [{name}] 目标串命中 {orig.count(target)} 次（应为 1）⇒ 锚点漂移，对照作废")
        return False
    try:
        broken = orig.replace(target, replacement)
        if broken == orig:
            print(f"  [{name}] 改坏后内容未变 ⇒ 静默 no-op")
            return False
        io.open(CONSTANTS, "w", encoding="utf-8", newline="").write(broken)
        code, out = _run_tests()
        red = [t for t in expect_tests if f"FAILED {TEST_FILE.as_posix()}::{t}" in out
               or f"FAILED tests/test_checkpoint_dirty_contract.py::{t}" in out]
        ok = code != 0 and len(red) == len(expect_tests) and marker in out
        status = "OK" if ok else "FAIL"
        print(f"  [{name}] {status}: exit={code} 转红 {len(red)}/{len(expect_tests)}"
              f" 标记串出现={marker in out}")
        if not ok:
            for line in out.splitlines():
                if "FAILED" in line or "passed" in line or "failed" in line:
                    print("      ", line.strip()[:120])
        return ok
    finally:
        io.open(CONSTANTS, "w", encoding="utf-8", newline="").write(orig)
        back, _ = _load(CONSTANTS)
        print(f"  [{name}] 复原一致={back == orig}")


def main() -> int:
    results = []

    print("① 删除 REGENERABLE_PATTERNS 里的 .vite/ 正则")
    results.append(control(
        "vite-regex",
        '    re.compile(r"(?:^|/)\\.vite/(?:.*)$"),',
        '    # [POSITIVE-CONTROL] re.compile(r"(?:^|/)\\.vite/(?:.*)$"),',
        [
            "test_vite_tracked_dirt_is_not_a_hard_blocker",
            "test_checkpoint_never_commits_vite_cache",
            "test_merge_gate_restores_vite_dirt_instead_of_rejecting",
        ],
        ".vite/deps/_metadata.json",
    ))

    print("② 删除 ENGINE_GITIGNORE_SEEDS 里的 .vite/ 条目")
    results.append(control(
        "vite-seed",
        '        (".vite/",),',
        '        # [POSITIVE-CONTROL] (".vite/",),',
        ["test_vite_gitignore_seed_detected_from_vite_config"],
        "test_vite_gitignore_seed_detected",
    ))

    print("③ 把 is_generated_path 退化成只读 REGENERABLE_PATTERNS（= 改回两张表分裂态）")
    results.append(control(
        "single-source",
        "    return base in GENERATED_FILES or is_regenerable_path(norm)",
        "    return is_regenerable_path(norm)",
        ["test_lockfile_tracked_dirt_is_not_a_hard_blocker"],
        "package-lock.json",
    ))

    print("\n汇总：%d/%d 项对照通过" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
