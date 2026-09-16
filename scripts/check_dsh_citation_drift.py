"""③ DSH 引用复核（两用）—— `0d1f50007f` 升级后跑一次。

用法::

    uv run python scripts/check_dsh_citation_drift.py            # 全量分类
    uv run python scripts/check_dsh_citation_drift.py --cases    # 现役面逐条（src/ + skills/）

## 为什么需要它
DSH 是**参照源**：我们大量判据/纪律的出处是它的 `file:line`。它一升级，那些坐标
就可能漂移，而**漂移是静默的**（注释照旧看着有理）。本脚本把"漂移"变成可核验的。

⚠ **`docs/platform-issue-research/**` 是 gitignore 的** ⇒ 不能用 `git grep` 找引用
（会静默漏掉它们，实测只找到 7/30 个文件）。这里显式**扫盘**。

⚠ **历史快照 vs 现役手册**（本仓约定）：`docs/platform-issue-research/*.md` 的 pass
报告是**历史快照**（记的是"当时看到的 DSH"）⇒ **不逐条改写**（那等于伪造历史）；
`skills/` 与 `src/` 是**现役**面 ⇒ 漂移必须处置。故 `--cases` 只覆盖现役面。

只读：不动任何仓库。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

HW = Path(r"D:\PC_AI\Project\HiveWeave")
DSH = Path(r"D:\PC_AI\Project\deepseek-harness")
OLD = "c291e7961a515f6d7af9304e7fd1d257929aef26"   # dsh-v0.1.5-rc.2 时代
NEW = "0d1f50007f9bca3f52b06e1c3074fa14d5fb0720"   # 0.1.6-alpha.1

PAT = re.compile(
    r"(?:^|[\s`(])((?:packages|apps|docs)/[A-Za-z0-9_./-]+\.(?:ts|md|json|yaml))"
)

#: 现役面（src/ + skills/）里那些**裸文件名 + 行号**的引用：**已人工解析并核实**
#: 的 DSH 路径（2026-09-16 于 `0d1f50007f` 逐一核过）。形态如 `render.ts:85-92`
#: —— 不解析就判不了漂移。下次升级重跑本表即可逐条看出"哪条又漂了"。
#: ⚠ 解析依据是**内容**（注释里那句判据/常量在不在），不是行号。
CASES: list[tuple[str, str, tuple[int, int]]] = [
    # 我们引在哪儿                     DSH 实际路径                                              行区间
    ("db/project.py:89", "packages/session/session-projection-cache/src/spec.ts", (33, 40)),
    ("db/schema.py:663", "packages/core/session/src/repair.ts", (14, 18)),
    # ⚠ 2026-09-16 修正：原引 :107 ⇒ 漂到 :117（同一句在 pwsh-sandbox:124 也有）
    ("llm/streamer/doom_loop.py:289", "packages/shell/bash-sandbox/src/index.ts", (117, 117)),
    ("services/worktree_review.py:856", "packages/shell/tool-bash/src/render.ts", (85, 92)),
    # ⚠ 2026-09-16 修正：`GoalBlockReason` 的定义已从 index.ts 挪到 types.ts
    ("services/worktree_review.py:990", "packages/goal/goal/src/types.ts", (52, 52)),
    ("tools/bash.py:22", "packages/shell/pwsh-local/src/index.ts", (48, 49)),
    ("tools/fact_positions.py:9", "packages/sandbox/sandbox/src/index.ts", (74, 88)),
    ("tools/fact_positions.py:313", "packages/sandbox/sandbox/src/index.ts", (109, 115)),
    ("tools/fact_positions.py:323", "packages/guard/timeout-policy/src/index.ts", (69, 73)),
    ("tools/file.py:144", "packages/fs/fs-observation-policy/src/index.ts", (61, 88)),
    ("tools/file.py:57", "packages/fs/fs-local/src/fsio.ts", (74, 76)),
    # ⚠ 2026-09-16 修正：常量区间现在从 :48 起
    ("services/mcp_supervisor.py:36", "packages/mcp/mcp-client/src/tools.ts", (48, 54)),
    ("services/host_env/registry.py:3",
     "packages/runtime-diagnostics/invariants/src/index.ts", (136, 136)),
    ("tools/pipeline.py:187", "packages/core/tools/src/index.ts", (493, 505)),
    ("services/health_notice.py:16",
     ".agents/notes/archived/feature/2026-07-08-repeat-tool-guard.md", (58, 58)),
]


def git(*args: str) -> str:
    r = subprocess.run(
        ["git", "-C", str(DSH), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return (r.stdout or "").strip()


def dsh_mentioning_files() -> list[Path]:
    out: list[Path] = []
    for root in (HW / "apps/hiveweave-py/src", HW / "docs", HW / "skills"):
        for p in root.rglob("*"):
            if p.suffix not in (".py", ".md"):
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "deepseek-harness" in text:
                out.append(p)
    return out


def full_scan() -> None:
    files = dsh_mentioning_files()
    hits: dict[str, set[str]] = {}
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in PAT.finditer(text):
            p = m.group(1)
            if (DSH / p).exists():
                hits.setdefault(p, set()).add(f.relative_to(HW).as_posix())
    safe, drifted = [], []
    for p in sorted(hits):
        n = len(git("log", "--oneline", f"{OLD}..{NEW}", "--", p).splitlines())
        (drifted if n else safe).append((p, n, hits[p]))
    print(f"提及 DSH 的文件 {len(files)} 个；引用的 DSH 路径 {len(hits)} 条")
    print(f"  ✅ 未变 {len(safe)} 条 / ⚠️ 已变 {len(drifted)} 条\n")
    for p, _n, _w in safe:
        print(f"    ✅ {p}")
    print()
    for p, n, where in drifted:
        only_docs = all(w.startswith("docs/") for w in where)
        tag = "（全在历史快照 ⇒ 按约定不改写，只在索引里标注）" if only_docs else "⚠️ 现役面，必须处置"
        print(f"    ⚠️ {p}  ← {n} 次提交   {tag}")
        print(f"        被引用于: {', '.join(sorted(where))}")


def cases() -> None:
    print("现役面（src/ + skills/）逐条复核：\n")
    for where, pattern, (lo, hi) in CASES:
        matches = sorted(
            p.relative_to(DSH).as_posix()
            for p in DSH.glob(pattern)
            if p.is_file() and "node_modules" not in p.parts
        )
        if not matches:
            print(f"❓ {where} —— glob 未命中（{pattern}），需人工解析")
            continue
        for m in matches[:3]:
            n = len(git("log", "--oneline", f"{OLD}..{NEW}", "--", m).splitlines())
            lines = (DSH / m).read_text(encoding="utf-8", errors="replace").splitlines()
            inrange = hi <= len(lines)
            print(f"{'⚠️ 已变' if n else '✅ 未变'} {where}  →  {m}")
            print(f"        行 {lo}-{hi}：{'区间在' if inrange else f'❌ 超界（文件 {len(lines)} 行）'}")
            if inrange:
                print(f"        现 L{lo}: {lines[lo - 1].strip()[:92]}")


if __name__ == "__main__":
    cases() if "--cases" in sys.argv else full_scan()
