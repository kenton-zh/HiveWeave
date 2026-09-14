"""文本判据棘轮的阳性对照（本仓纪律：每条新守卫都要证明"缺陷回来它会转红"）。

用法（任意工作目录）：

    python scripts/ratchet_positive_controls.py

对 `apps/hiveweave-py/tests/test_text_judge_ratchet.py` 的**五张网**各做一次
「改坏 → 跑 → 确认转红（**并确认报的是那条分支**）→ 恢复」，外加正面对照。
所有改动都在临时文件/备份里做，`finally` 里强制复原；**不修改任何真实源码**。

对应 fixplan 纪律：〇-4「每条新守卫都要做阳性对照」、§九 9.3「回滚也必须验」。

⚠ 两条踩过的坑（写对照组时要防）：
  · **分支冒充**：只断言「红了」不够 —— traceback 会把测试源码打出来，源码里的
    另一条 `pytest.fail("…")` 文案会被误当命中 ⇒ 必须断言**该分支特有的标记串**，
    且优先取 pytest 的 `E ` 行。
  · **期望值硬编码**：存量数字写死在脚本里，存量一变脚本就假红/假绿 ⇒ 从基线动态取。
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1] / "apps" / "hiveweave-py"
SRC = ROOT / "src" / "hiveweave"
TESTS = ROOT / "tests"
TEST_FILE = TESTS / "test_text_judge_ratchet.py"
BASE = TESTS / "_text_judge_baseline.json"

PROBE = SRC / "_ratchet_probe_tmp.py"
BROKEN = SRC / "_ratchet_probe_broken.py"

T = "tests/test_text_judge_ratchet.py"
TABLE_TEST = f"{T}::test_no_new_text_judgment_tables"
REGEX_TEST = f"{T}::test_no_new_module_regex_constants"
ENTRY_TEST = f"{T}::test_scanner_sees_every_baselined_entry"
VACUOUS_TEST = f"{T}::test_scanner_is_not_vacuous"
COVERAGE_TEST = f"{T}::test_scanner_covers_every_baselined_file"
RULER_TEST = f"{T}::test_scanner_ruler_is_frozen"

results: list[tuple[str, str, str]] = []


def run(target: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["uv", "run", "pytest", target, "-q", "-p", "no:randomly", "--no-header"],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", shell=True,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def err_line(text: str) -> str:
    """取 pytest 的 `E ` 行（断言/失败消息），避开 traceback 打印的测试源码。"""
    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith("E "):
            return s[2:].strip()
    return ""


def expect_red(name: str, target: str, marker: str) -> None:
    """期望转红，且**报的是 marker 那条分支**。"""
    code, out = run(target)
    line = err_line(out)
    ok = code != 0 and marker in line
    results.append((name, f"红 / 含「{marker}」",
                    ("✓ " if ok else "✗ ") + (line[:110] or "<无 E 行>")))


def expect_green(name: str, target: str) -> None:
    code, out = run(target)
    results.append((name, "绿", ("✓ " if code == 0 else "✗ ") + (err_line(out)[:110] or "passed")))


def write_probe(body: str) -> None:
    PROBE.write_text(body, encoding="utf-8")


def patch(path: pathlib.Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"补丁目标串失配（{path.name}）：{old[:70]!r}"
    path.write_text(text.replace(old, new), encoding="utf-8")


def load_base() -> dict:
    return json.loads(BASE.read_text(encoding="utf-8"))


def save_base(data: dict) -> None:
    BASE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def uv_run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["uv", "run", "python", "-m", "tests.test_text_judge_ratchet", *args],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", shell=True,
    )


def main() -> int:
    bk_base, bk_test = BASE.with_suffix(".json.bak"), TEST_FILE.with_suffix(".py.bak")
    shutil.copy2(BASE, bk_base)
    shutil.copy2(TEST_FILE, bk_test)
    orig = load_base()
    n_tables = orig["_by_tier"]["table"]
    n_regex = orig["_by_tier"]["regex"]
    try:
        # ==== C0 正面对照：基线态全绿（先证"平台是活的"） ====
        expect_green("C0 基线态（无改动）", T)

        # ==== C1/C2 新增后缀表 / 模块级正则 ⇒ 各自档位红 ====
        write_probe("_FOO_NEEDLES: tuple[str, ...] = ('done', '全部完成', 'termine')\n")
        expect_red("C1 新增 _FOO_NEEDLES", TABLE_TEST, "张名单型文本表")
        PROBE.unlink()

        write_probe("import re\n\n_FOO_GATE_RE = re.compile(r'^\\s*done\\s*$')\n")
        expect_red("C2 新增 _FOO_GATE_RE", REGEX_TEST, "个模块级正则常量")
        PROBE.unlink()

        # ==== C3 两种"改写法降档"⇒ 必须都落 **table** 档 ====
        write_probe("_FOO_PATTERNS = 'a|b|c'\n")
        expect_red("C3a _FOO_PATTERNS='a|b|c'", TABLE_TEST, "张名单型文本表")
        PROBE.unlink()

        write_probe("import re\n\n_FOO_PATTERNS = re.compile(r'a|b')\n")
        expect_red("C3b _FOO_PATTERNS=re.compile()", TABLE_TEST, "张名单型文本表")
        PROBE.unlink()

        # ==== C4 删除白名单里的表 ⇒ 允许（验收②：清债不该被挡） ====
        write_probe("_BAR_NEEDLES: tuple[str, ...] = ('x',)\n")
        data = load_base()
        data["entries"]["_ratchet_probe_tmp.py::_BAR_NEEDLES"] = {"tier": "table", "line": 1}
        data["_total"] = len(data["entries"])
        data["_by_tier"]["table"] += 1  # ← 必须一起改：自检断言 _by_tier 与 entries 相符
        data["_files"] = sorted([*data["_files"], "_ratchet_probe_tmp.py"])
        save_base(data)
        expect_green("C4a 表在源码+基线里", T)
        write_probe("")  # ← 清债：只删常量，文件保留
        expect_green("C4b 删常量（清债）", T)
        PROBE.unlink()
        shutil.copy2(bk_base, BASE)

        # ==== C5 清空白名单 ⇒ 存量全爆（证明扫描器真在扫；期望值动态取） ====
        data = load_base()
        data["entries"], data["_total"] = {}, 0
        save_base(data)
        expect_red(f"C5a 清空白名单⇒爆 {n_tables} 张表", TABLE_TEST, f"新增了 {n_tables} 张")
        expect_red(f"C5b 清空白名单⇒爆 {n_regex} 个正则", REGEX_TEST, f"新增了 {n_regex} 个")
        shutil.copy2(bk_base, BASE)

        # ==== C6 扫描器整体失效（路径指向不存在目录）⇒ 自检红 ====
        patch(TEST_FILE, 'parents[1] / "src" / "hiveweave"', 'parents[1] / "src" / "NOPE"')
        expect_red("C6 扫描器失效（路径写错）", VACUOUS_TEST, "低于下界")
        shutil.copy2(bk_test, TEST_FILE)

        # ==== C7 扫描面收窄到子目录 ⇒ 覆盖网红（**文件已不存在**分支） ====
        patch(TEST_FILE, 'parents[1] / "src" / "hiveweave"',
              'parents[1] / "src" / "hiveweave" / "tools"')
        expect_red("C7 扫描面收窄到 tools/", COVERAGE_TEST, "已不存在")
        shutil.copy2(bk_test, TEST_FILE)

        # ==== C8 基线文件变得解析不能 ⇒ 覆盖网红（**扫描失败**分支，与 C7 区分） ====
        BROKEN.write_text("def broken(:\n    pass\n", encoding="utf-8")
        data = load_base()
        data["_files"] = sorted([*data["_files"], "_ratchet_probe_broken.py"])
        save_base(data)
        expect_red("C8 基线文件解析失败", COVERAGE_TEST, "扫描失败")
        BROKEN.unlink()
        shutil.copy2(bk_base, BASE)

        # ==== C9 尺子被削弱 ⇒ 尺子冻结网红（两个方向各一条） ====
        patch(TEST_FILE, '    "_NEEDLES",\n', "")
        expect_red("C9a 删 _TABLE_SUFFIXES 一个后缀", RULER_TEST, "后缀清单被改动了")
        shutil.copy2(bk_test, TEST_FILE)

        patch(TEST_FILE, "_MIN_TABLES = 15", "_MIN_TABLES = 0")
        expect_red("C9b 把 _MIN_TABLES 归零", RULER_TEST, "扫描器下界被改动了")
        shutil.copy2(bk_test, TEST_FILE)

        # ==== C10 新增条目静默消失 ⇒ entry 级覆盖网红（审计第二轮的核心缺口） ====
        # 忠实复现审计手法：给 `_module_assign_targets` 加一层按名字过滤。
        patch(
            TEST_FILE,
            "    if value is None or not names:\n        return None\n    return names, value",
            "    if value is None or not names:\n        return None\n"
            "    names = [n for n in names if n.startswith('_')]\n"
            "    if not names:\n        return None\n    return names, value",
        )
        expect_red("C10 分类器少收名字", ENTRY_TEST, "却没被扫描器收录")
        shutil.copy2(bk_test, TEST_FILE)

        # ==== C11 --write 闸门：增长必须显式；且"删几条腾位置"不该攒额度 ====
        before = load_base()["_total"]
        write_probe("_FOO_NEEDLES: tuple[str, ...] = ('done',)\n")
        proc = uv_run("--write")
        grew = "拒绝生成" in (proc.stdout + proc.stderr) and load_base()["_total"] == before
        results.append(("C11a --write 拒绝增长", "拒绝且基线不变",
                        ("✓ " if grew else "✗ ") + f"{before}->{load_base()['_total']}"))
        # 「删 2 条 + 加 1 条」总数为**下降**，旧版闸门会放行；新版必须拒
        shutil.copy2(bk_test, TEST_FILE)
        data = load_base()
        victims = sorted(data["entries"])[:2]
        for v in victims:
            data["entries"].pop(v)
        data["_total"] = len(data["entries"])
        save_base(data)
        proc = uv_run("--write")
        net = "拒绝生成" in (proc.stdout + proc.stderr)
        results.append(("C11b 删2加1 仍须拒绝", "拒绝",
                        ("✓ " if net else "✗ ") + (proc.stdout + proc.stderr).strip()[:80]))
        proc = uv_run("--write", "--allow-grow")
        allowed = "wrote" in (proc.stdout + proc.stderr) and load_base()["_total"] > 0
        results.append(("C11c --allow-grow 放行", "写入成功",
                        ("✓ " if allowed else "✗ ") + (proc.stdout + proc.stderr).strip()[:60]))
        PROBE.unlink()
        shutil.copy2(bk_base, BASE)
    finally:
        shutil.copy2(bk_base, BASE)
        shutil.copy2(bk_test, TEST_FILE)
        for f in (PROBE, BROKEN):
            f.unlink(missing_ok=True)
        for f in (bk_base, bk_test):
            f.unlink(missing_ok=True)

    print("\n" + "=" * 108)
    print(f"{'对照项':<34} {'期望':<22} {'实际':<50}")
    print("-" * 108)
    for name, want, actual in results:
        print(f"{name:<34} {want:<22} {actual:<50}")
    print("=" * 108)
    bad = [r[0] for r in results if r[2].startswith("✗")]
    if bad:
        print(f"阳性对照**有 {len(bad)} 项不符合预期**：{bad}")
        return 1
    print(f"阳性对照：全部符合预期 ✓（{len(results)} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
