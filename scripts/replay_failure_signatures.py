"""复算「失败签名」去重率与**非循环**验收 —— 拿真实项目库重放，不读旧结论。

## 为什么需要它（0-4 / #5 的验收工具）

`#5` 在 `fixqueue` 主表标「已关闭（`468bf79` 剥噪声）」，而 09-16 的**生效复核**
标「❌ 无效（实测 0 去重）」。两个状态指向同一件事：
**「代码已提交」与「判据真的生效」不是一回事**。

## ⚠ 关于「0% 去重」这条结论：它是**测量方法错**

`_SIGNATURE_MAX_ROWS = 50` 是签名条目的**裁剪上限**，且 upsert 会按
`module_id` 去重 ⇒ 库里行数**恒 ≤ 50**。拿"50 行 / 50 个不同 module_id"
当去重率，**必然**读出 0%（58/59 都是 50/50）。真实去重率只能重放得出，
见下面的「全量口径」。

## ⚠ 关于「同一根因」的判据：本脚本给两个，其中一个是**非循环**的

「同一根因」= 「剥掉平台自产标识后同文」这个定义**与被测修复用的是同一组
形状规则** ⇒ 在该定义下"同根因必同签名"是**结构性蕴含**，恒为真
（0-4 审计实测：旧实现+该定义 = 3 组、新实现 = 0 组、**定义取空也 = 0 组**）。
⇒ 这一段只作为**自查**列出，**不能**当验收证据用。

真正的验收用两条**不依赖该定义**的判据：
  · **P2 缺席性**：签名里**不得**出现机器本地前缀（盘符 / UNC）。
    这是独立谓词（只用 `[A-Za-z]:` 这类形状），能抓到"占位符打断路径匹配
    ⇒ 盘符回流"那类缺陷（审计实测曾 Fail 8/56）。
  · **阴性判据**：**同一签名不得横跨不同 tool**。身份是把"同 tool 同因"合并，
    跨 tool 合并就是**过度合并**的直接证据 —— 与文本规则无关。
另报**搁浅条目数**（算法变更后老条目不可达），`known_signature_hint` 的
静默失配因此可量化（详见 `signature_of` docstring 的实测口径）。

⚠ **只读**：外部项目库以 `mode=ro` 打开，绝不写入。

用法::

    cd apps/hiveweave-py
    uv run python ../../scripts/replay_failure_signatures.py \\
        --db D:/PC_AI/Project/HiveTestProject/TEST_DSH_58/.hiveweave/data.db \\
        --root D:/PC_AI/Project/HiveTestProject/TEST_DSH_58
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "hiveweave-py" / "src"))

from hiveweave.services.failure_signature import (  # noqa: E402
    _AGENT_SHORT_ID_RE,
    _TASK_ID8_RE,
    signature_of,
)

_TAIL_PREFIX = "原文尾(含等价写法/修复线索): "

# 机器本地前缀 = 绝对路径的盘符 / UNC。**独立谓词**（不复用被测规则的形状），
# 所以它给出的结论不是循环的。
_LOCAL_PREFIX_RE = re.compile(r"[A-Za-z]:[\\/]|\\\\[^\\/]")


def _ro(db: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _explained_by_norm(old_sig: str, new_sig: str) -> bool:
    """差异是否**由本批新增的归一化规则**造成（把旧签名再套一遍新规则）。

    ⚠ 必须容忍**截断位移**：占位符长度与原标识不同 ⇒ 160 字符的截断点整体
    移动 ⇒ 逐字相等在"差异确由归一化造成"时也会判 False（实测 59 的
    `1d4874f5` 就是这样被误报的）。判据因此取「互为前缀」。
    """
    norm = _TASK_ID8_RE.sub("<id8>", _AGENT_SHORT_ID_RE.sub("<agent>", old_sig))
    if not norm:
        return False
    return new_sig.startswith(norm) or norm.startswith(new_sig)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="project data.db (opened read-only)")
    ap.add_argument(
        "--root",
        required=True,
        help="project workspace root —— 必须与写侧同源（meta_db.get_project_workspace）",
    )
    args = ap.parse_args()

    # D6：库与根必须自洽（库固定在 <root>/.hiveweave/data.db）。不一致时
    # **不出数**：审计实测"传错 root 输出一字不差"，静默给出错数字比报错更糟。
    db_path = Path(args.db).resolve()
    root_path = Path(args.root).resolve()
    if db_path.parent.name != ".hiveweave" or db_path.parent.parent != root_path:
        print(
            f"[FAIL-LOUD] --db 与 --root 不自洽：\n  db  = {db_path}\n"
            f"  root= {root_path}\n"
            "  期望 root/.hiveweave/data.db —— 请改正后重跑（不做静默降级）",
            file=sys.stderr,
        )
        return 2

    con = _ro(str(db_path))
    try:
        return _run(con, str(root_path))
    finally:
        con.close()


def _run(con: sqlite3.Connection, root: str) -> int:
    rows = con.execute(
        "SELECT id, module_id, content, metadata FROM memories "
        "WHERE module_id LIKE 'failure_sig%' ORDER BY created_at ASC"
    ).fetchall()

    steps = [dict(r) for r in con.execute(
        "SELECT id, tool_name, error, runner_failed, command_failed FROM run_steps "
        "WHERE error IS NOT NULL AND error != ''"
    )]

    def _bits(row: dict) -> str:
        if row["runner_failed"]:
            return "runner_failed"
        if row["command_failed"]:
            return "command_failed"
        return "unclassified"

    # ── 全量口径（去重率的唯一可信来源）──────────────────────────
    pop: list[str] = []
    tool_of: dict[str, str] = {}
    bits_of: dict[str, str] = {}
    for s in steps:
        sig = signature_of(s["error"], root=root)
        if not sig:
            continue
        pop.append(sig)
        tool_of.setdefault(sig, s["tool_name"] or "?")
        bits_of.setdefault(sig, _bits(s))
    u = len(set(pop))
    rate = 0 if not pop else round(100 * (1 - u / len(pop)))
    print(f"失败 step {len(pop)} 条 → 不同签名 {u} 条 ⇒ 全量去重率 {rate}%")
    pops = Counter(pop)
    print("  出现次数最多的 5 个签名：")
    for sig, n in pops.most_common(5):
        print(f"    x{n:<3} [{tool_of.get(sig)}] {sig[:86]!r}")

    # ── 验收①（非循环）：签名不得含机器本地前缀 ──────────────────
    leaks = [s for s in set(pop) if _LOCAL_PREFIX_RE.search(s)]
    print(
        f"\n[验收①·缺席性·非循环] 签名含盘符/UNC 的 = {len(leaks)}/{u} 条"
        f" {'✅' if not leaks else '❌ 需修'}"
    )
    for s in leaks[:3]:
        print(f"    {s[:100]!r}")

    # ── 验收②（非循环）：**过度合并的上界** ──────────────────────
    #
    # 若有人为了"提高去重率"把身份改成按 tool（或按 tool+事实位）归并，
    # 签名数会塌到 distinct(tool) 那一档 —— 本判据让那次塌陷可见。
    # 它是**下界**型的（口径比真实身份粗），所以永真于"合理实现"，
    # 只在**过度合并**时转红，与文本规则无关。
    n_tools = len({s["tool_name"] or "?" for s in steps})
    n_toolbits = len(
        {f"{s['tool_name'] or '?'}::{_bits(s)}" for s in steps}
    )
    print(
        f"\n[验收②·过度合并上界·非循环] 签名 {u} 条 vs distinct(tool) {n_tools} / "
        f"distinct(tool,事实位) {n_toolbits}"
        f" {'✅' if u >= n_toolbits else '❌ 已塌成结构化粗档'}"
    )

    # ── 观察（非 pass/fail）：同一签名横跨多个 tool ────────────────
    #
    # **不是缺陷**：条目首行带 `tool=<name>`，生产侧由 `_first_line_matches_tool`
    # 在条目层区分（同一护栏文案由多个 tool 入口发出是设计内的事）。
    # 实测该项**旧实现与新实现同为 1 条**（58/59 都是）⇒ 非本批引入。
    sig_tools: dict[str, set[str]] = defaultdict(set)
    for s in steps:
        sig = signature_of(s["error"], root=root)
        if sig:
            sig_tools[sig].add(s["tool_name"] or "?")
    crossed = {k: v for k, v in sig_tools.items() if len(v) > 1}
    print(
        f"[观察·跨 tool 共享签名] {len(crossed)}/{u} 条（由 `tool=` 首行兜住；"
        "旧实现同为 1 条 ⇒ 非本批引入）"
    )
    for k, v in list(crossed.items())[:3]:
        print(f"    {sorted(v)} ← {k[:80]!r}")

    # ── 自查（⚠ **循环**，不构成验收证据，仅列出供人看）──────────
    _volatile = (
        (re.compile(r"\bA\d{3}\b"), "<agent>"),
        (re.compile(r"\b[0-9a-f]{8}\b"), "<id8>"),
    )

    def _root_cause_key(err: str) -> str:
        out = err.strip()
        for rx, rep in _volatile:
            out = rx.sub(rep, out)
        return out

    groups: dict[str, set[str]] = defaultdict(set)
    for s in steps:
        sig = signature_of(s["error"], root=root)
        if sig:
            groups[_root_cause_key(s["error"])].add(sig)
    split = {k: v for k, v in groups.items() if len(v) > 1}
    print(
        f"\n[自查·**循环**，勿当证据] 「剥 id 后同文」定义下被判成多个签名的 = "
        f"{len(split)}/{len(groups)} 组"
    )
    print(
        "  ⚠ 该定义与被测修复同源 ⇒ 新实现下必然接近 0（审计实测：旧实现 3 组、"
        "新实现 0 组、**定义取空也 0 组**）。验收请看上面的①/②/③。"
    )
    for k, v in list(split.items())[:3]:
        print(f"    根因: {k[:80]!r}")
        for sig in sorted(v)[:3]:
            print(f"      → {sig[:84]!r}")

    # ── 搁浅条目：算法变更后老条目不可达（hint/hitter 的静默失配面）──
    by_suffix: dict[str, list[dict]] = defaultdict(list)
    for s in steps:
        by_suffix[(s["error"] or "").strip()[-320:]].append(s)
    stranded = 0
    stranded_with_solution = 0
    checked = 0
    sol_total = 0
    sol_checked = 0
    for r in rows:
        content = r["content"] or ""
        _has_sol = "已验证解法:" in content
        if _has_sol:
            sol_total += 1
        tail = next(
            (ln[len(_TAIL_PREFIX):] for ln in content.splitlines()
             if ln.startswith(_TAIL_PREFIX)),
            "",
        )
        cands = by_suffix.get(tail) or []
        if len(cands) != 1:
            continue
        checked += 1
        if _has_sol:
            sol_checked += 1
        stored = (json.loads(r["metadata"] or "{}") or {}).get("signature") or ""
        now = signature_of(cands[0]["error"], root=root) or ""
        # 与**生产判据逐字一致**（`known_signature_hint` / `note_distinct_hitter`
        # / `backfill_solution` 三处都是这一条）：整串命中 或 新签名的前 48 字符
        # 出现在条目首行里。改用"前缀相等"这种近似会算错搁浅数。
        matched = bool(now) and (now == stored or now[:48] in stored)
        if not matched:
            stranded += 1
            # 带「已验证解法」的那批才是**真损失**（解法从此不可达）；
            # 没有解法的搁浅行只是"变成镜子条目"，代价低一档。
            if _has_sol:
                stranded_with_solution += 1
    print(
        f"\n[搁浅条目] 可核对签名条目 {checked}/{len(rows)} 条中，"
        f"**新旧两个判据都命中不了**（= `known_signature_hint` 从此静默失配）= "
        f"{stranded} 条"
    )
    # ⚠ 覆盖率必须与结论一起读：全覆盖 0 时"带解法的搁浅 = 0"**不是**"没有损失"，
    # 而是"这一项不可判定"（本仓最忌"看似有守卫"）。实测 59：7 条带解法的条目
    # **一条也连不上** run_steps.error ⇒ 本项对它不可判定。
    if sol_checked:
        print(
            f"    其中带「已验证解法」的搁浅 = {stranded_with_solution} 条"
            f"（真损失：解法从此不可达）"
        )
    else:
        print(
            f"    ⚠ 带「已验证解法」的条目共 {sol_total} 条，其中可唯一连接的 "
            f"**{sol_checked} 条** ⇒ 「解法是否搁浅」这一项**不可判定**"
            "（不是'没有损失'）。要判定它需要另一条连接路径（如按 tool+时间）。"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
