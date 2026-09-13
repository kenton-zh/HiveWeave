"""执行账本（run_steps）口径统计器 —— 「不修代码，修尺子」。

背景（2026-09-13 TEST_DSH_55 实证，两次翻车后立此存照）：

1. **`conversation_turns` 是幸存者样本，`run_steps` 才是账本。**
   `conversation_turns` 会被 prune 改写内容、被 compaction 整段 DELETE；
   实测全场 tool 结果幸存率 **85.0%**（现存 2850 条 vs 账本 3354 步），
   同一现象在 DESIGN.md 场景表现为「conversation_turns 口径 43 次 vs
   账本 52 次」。⇒ **凡问「发生过几次」，必须用 run_steps。**

2. **`tool_args_excerpt` 截断在 212 字符**（3354 行中 1228 行卡在该长度 = 37%）。
   被截断的主要是长参数工具（shell/write_file/apply_patch/commit_turn/browse）。
   `read_file` 的 `filePath` 位于参数最前 ⇒ **匹配文件路径可靠**；
   但**匹配正文或长参数（脚本内容、提交信息）不可靠**，会系统性漏检。
   ⇒ 结果大小一律读 `result_size`（数值列），**不要**用 `result_excerpt` 的长度。

3. **「被读 N 次」是口径歧义词。** 实测报告写的「DESIGN.md 被读 97 次」
   = `read_file` **52** + `grep` **45**。两个不同动作合成一个数，读者会误以为
   都是 read_file。⇒ 引用任何计数前先问：**含哪些工具？谁定义的？**

用法：
    python scripts/stat_from_ledger.py <项目库路径> [关键词]
    python scripts/stat_from_ledger.py "D:\\...\\TEST_DSH_55\\.hiveweave\\data.db" DESIGN.md

只读打开（mode=ro），不改任何库。
"""
from __future__ import annotations

import collections
import json
import sqlite3
import sys
from pathlib import Path

# 读类工具归组（口径命名用；改这里等于改口径，须同步文档）
READ_LIKE = ("read_file",)
SEARCH_LIKE = ("grep", "search_files", "list_files")


def _group(tool: str) -> str:
    t = (tool or "").lower()
    if any(k in t for k in READ_LIKE):
        return "read_file"
    if any(k in t for k in SEARCH_LIKE):
        return "search"
    if "shell" in t or t.endswith("_cmd") or t in ("bash", "sh", "terminal"):
        return "shell"
    return "other"


def main() -> None:
    # Windows 控制台默认 GBK，本文件 docstring 含 U+21D2（⇒）等字符 ——
    # 直接 print(__doc__) 会抛 UnicodeEncodeError（实测）。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if len(sys.argv) < 2:
        print(__doc__)
        return
    db = Path(sys.argv[1])
    kw = sys.argv[2] if len(sys.argv) > 2 else None
    if not db.exists():
        print(f"[!] 库不存在: {db}")
        return

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    tabs = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "run_steps" not in tabs:
        print("[!] 该库无 run_steps —— 不是 HiveWeave per-project 库")
        return

    print(f"库: {db.name}  ({db.stat().st_size:,} bytes)")

    # ── A. 两表总量与幸存率 ──
    tot = con.execute("SELECT COUNT(*) FROM run_steps").fetchone()[0]
    by_status = dict(
        con.execute("SELECT status, COUNT(*) FROM run_steps GROUP BY status")
    )
    ntool = 0
    if "conversation_turns" in tabs:
        for (raw,) in con.execute("SELECT raw_messages FROM conversation_turns"):
            try:
                ms = json.loads(raw or "[]")
            except Exception:
                continue
            if isinstance(ms, list):
                ntool += sum(
                    1 for m in ms if m.get("role") == "tool" or m.get("tool_call_id")
                )
    print(f"\nA. 账本 vs 幸存样本")
    print(f"   run_steps 步数      : {tot}   {by_status}")
    print(f"   conversation_turns 现存 tool 结果: {ntool}")
    if tot:
        print(f"   ⇒ 幸存率 {ntool * 100.0 / tot:.1f}%"
              f"（差额 = 被 compaction DELETE 吞掉 + 未落库者）")

    # ── B. 关键词命中按工具名分组 ──
    if kw:
        rows = con.execute(
            "SELECT tool_name, COUNT(*) n FROM run_steps "
            "WHERE tool_args_excerpt LIKE ? GROUP BY tool_name ORDER BY n DESC",
            [f"%{kw}%"],
        ).fetchall()
        print(f"\nB. 参数含「{kw}」的调用（按工具名，窄口径=按工具名精确）")
        grp = collections.Counter()
        for r in rows:
            print(f"   {r['tool_name']:<24} {r['n']}")
            grp[_group(r['tool_name'])] += r['n']
        total = sum(grp.values())
        print(f"   —— 合计 {total}")
        print(f"   ⚠ 口径对照（引用时必须写明是哪一个）：")
        print(f"      read_file only        = {grp['read_file']}")
        print(f"      read+search（报告口径）= {grp['read_file'] + grp['search']}")
        print(f"      全部工具             = {total}")

        # ── C. 按 agent 分组（join agent_runs / agents）──
        if "agent_runs" in tabs and "agents" in tabs:
            run2agent = {
                r["id"]: r["agent_id"]
                for r in con.execute("SELECT id, agent_id FROM agent_runs")
            }
            names = {r[0]: r[1] for r in con.execute("SELECT id, name FROM agents")}
            per = collections.defaultdict(collections.Counter)
            for r in con.execute(
                "SELECT run_id, tool_name FROM run_steps "
                "WHERE tool_args_excerpt LIKE ?",
                [f"%{kw}%"],
            ):
                per[names.get(run2agent.get(r["run_id"]), "?")][_group(r["tool_name"])] += 1
            print(f"\nC. 按 agent（口径：read + search）")
            for a, c in sorted(per.items(), key=lambda kv: -sum(kv[1].values())):
                print(f"   {a:<14} read={c['read_file']:<4} search={c['search']:<4} "
                      f"合计={c['read_file'] + c['search']}")

    # ── D. 同 run / 跨 run 拆分（判「重复」还是「分页」）──
    if kw:
        rows = con.execute(
            "SELECT run_id, tool_name, tool_args_excerpt FROM run_steps "
            "WHERE tool_args_excerpt LIKE ? ORDER BY run_id",
            [f"%{kw}%"],
        ).fetchall()
        per_run = collections.defaultdict(list)
        for r in rows:
            if _group(r["tool_name"]) in ("read_file", "search"):
                per_run[r["run_id"]].append(r)
        multi = {k: v for k, v in per_run.items() if len(v) > 1}
        inner = sum(len(v) - 1 for v in multi.values())
        print(f"\nD. 同 run / 跨 run 拆分（口径 read+search）")
        print(f"   agent×run 组合数（= 跨 run 反复消费的规模）: {len(per_run)}")
        print(f"   同一 run 内读取 >1 次的 run 数: {len(multi)}；"
              f"其中第 2 次及以后共 {inner} 次")
        print(f"   ⚠ 这 {inner} 次 **不等于「重复」** —— 必须再看区间：")
        print(f"      offset 递增 = 分页读完（与裁剪无关）；"
              f"区间重叠 = 内容不可见而回看（才可能归因裁剪）")
        print(f"      判据：读 tool_args_excerpt 的 limit/offset 逐对比较")

    # ── E. 截断告警 ──
    lens = collections.Counter(
        len(r[0] or "") for r in con.execute("SELECT tool_args_excerpt FROM run_steps")
    )
    if lens:
        top_len, top_n = lens.most_common(1)[0]
        # 判据：某长度至少 10 行，且占全表 ≥1/3 —— 才认定是截断点。
        # ⚠ 不能写 `top_n > len(lens)`：左边是**计数**（该长度的行数）、右边是
        # **distinct 长度数**，量纲不同（审计指出；原写法靠"尖峰刚好压过种类数"
        # 偶然成立）。
        if top_n >= 10 and top_n * 3 >= sum(lens.values()):
            print(f"\nE. ⚠ tool_args_excerpt 截断点 = {top_len} 字符，"
                  f"{top_n} 行卡在该长度（占 {top_n * 100.0 / sum(lens.values()):.0f}%）"
                  f" ⇒ 用 LIKE 匹配「正文/长参数」会漏检；匹配文件路径可靠")

    con.close()


if __name__ == "__main__":
    main()
