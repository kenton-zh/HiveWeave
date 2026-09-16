#!/usr/bin/env python
"""用**真实审计样本**复算 #12 的 would_flip / 误拦率基线。

背景
----
`fixplan-16items-2026-09-14.md` §三 #12 的验收③要求：**"量误拦率（真实审计
样本上跑，给出基线数字）"**。而 #12 的「切真拦」一直卡在"等 `would_flip`
数据够"——但线上一条 shadow 事件都没有（修复落地后没再跑过真实审计）。

本脚本换一条路：**真实审计输出本来就存在库里**。`attestation.py` 的
`audit_cache.top_issues` 存的是**原审计的完整 issue 行列表**（JSON 数组），
由 `run_code_audit` 写入（`top_issues=issues`，完整列表；只在回执里截断）。
⇒ 拿这些历史样本 + **生产同款函数**复算，就能得到与 shadow 同一口径的数字，
**零 LLM 调用、不需要跑新项目**。

判据来源（全部来自生产，本脚本**不复制任何判据**）
--------------------------------------------------
2026-09-15 独立审计 P1 后收敛：判定式已抽成生产里的**单一定义点**
`services/code_audit.py::shadow_decision()`，日志与 `agent_events` 共用同一个
dict。本脚本直接导入它，因此**不再有任何"抄一份判据"的风险** ——
生产切「真拦」时本脚本会跟着变，不会算出假数字。

导入的符号：`parse_issue_severity` / `count_issue_severities` /
`shadow_decision` / `legacy_high_count` / `_SEVERITY_ANY_MARKER_RE`。
缺任一即 **fail-loud 退出**（不退回自造实现 —— 那正是"同一事实两处判"）。

⚠ `unparsed` 为什么还要拆 A/B（本脚本的分析维度，**不是判据**）
--------------------------------------------------------------
`_parse_issues` 把 verdict 行**之后的每一行**都当 issue（只剥前导 `-*•`）。
而 severity 正则只认 ASCII。于是 `unparsed` 里混着两类：

  A. **带 `file:line` 的行** —— 真 issue，档位没写或写成 `【high】`/`高`。
  B. **不带 `file:line` 的行** —— 前言/小结（"Checked independently: …"）。
     它们不是 issue，但同样落 `unparsed` ⇒ fail-safe 也会当 high。

⚠ **A/B 分类器有已知局限，只作展示，不进判据**（2026-09-15 审计实测）：
  · 假 A：`12:30 checked`、`a:1 x`、`备注：3 个问题`（全角冒号+数字即命中）；
  · 假 B：`see https://x.com/a/b:12`、`C:/Program Files/a b/x.py:3 e`（路径含空格）。
  真正的判据是 `shadow_decision()` —— 它对**所有** unparsed 行一视同仁地兜底，
  这正是它兜得住的原因（见报告 §三之二：**不可**按 `file:line` 收窄）。

用法
----
    cd apps/hiveweave-py
    uv run python ../../scripts/measure_audit_severity_baseline.py

可选：`--root D:/path/to/projects`（可重复）、`--out <path>`。

只读打开所有库（`mode=ro` URI）—— 绝不写被审计的数据（审计已实测：写入被拒、
不生成 `-journal`/`-wal`）。唯一写点是报告文件。
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

# 项目库搜索根：默认覆盖两处已知的实验场
DEFAULT_ROOTS = [
    r"D:\PC_AI\Project",
    r"D:\PC_AI\Project\HiveTestProject",
]

#: `file:line` 形态 —— **仅用于本脚本的 A/B 展示分类**，不是判据。
#: ⚠ 已知假阳性/假阴性见模块 docstring；任何判定都不要读它。
_FILELINE_RE = re.compile(r"^[^\s:]+[:：]\d+\b")


def _import_production():
    """导入生产判据；缺符号即 fail-loud（不静默退回自己实现一版）。

    退回自造实现的后果：数字看着有，判据却可能和线上不是同一份 —— 那正是
    本项目反复吃亏的"同一事实两处判"。所以宁可直接炸。
    """
    try:
        from hiveweave.services import code_audit as ca
    except Exception as exc:  # noqa: BLE001 — 必须让调用方看见原因
        print(f"FATAL: cannot import hiveweave.services.code_audit: {exc}")
        print("  hint: run from apps/hiveweave-py with `uv run python ...`")
        raise SystemExit(2)
    need = (
        "parse_issue_severity",
        "count_issue_severities",
        "shadow_decision",  # ← 单一定义点（审计 P1 后新增）
        "legacy_high_count",
        "_parse_cached_issues",  # ← 缓存行解码也走生产（第二轮复审 P1）
        "_SEVERITY_ANY_MARKER_RE",
    )
    missing = [n for n in need if not hasattr(ca, n)]
    if missing:
        print(f"FATAL: production module lacks expected symbols: {missing}")
        print("  (判据源已改名/重构 ⇒ 本脚本必须跟着改，不许猜)")
        raise SystemExit(2)
    return ca


def find_dbs(roots: list[str], max_depth: int = 4):
    """`**/.hiveweave/data.db` → ``(dbs, skipped_roots, walk_errors)``。

    ⚠ 审计 P2-10：`--root` 打错字时**不许静默忽略** —— 否则"只扫到一个根"
    看起来和"扫全了"一模一样（典型的静默失效）。故把跳过的根与遍历错误
    一并返回，由调用方写进报告。
    """
    out: list[Path] = []
    skipped_roots: list[str] = []
    walk_errors: list[str] = []
    seen: set[Path] = set()
    for raw in roots:
        root = Path(raw)
        if not root.is_dir():
            skipped_roots.append(raw)
            continue
        base_depth = len(root.parts)
        stack = [root]
        while stack:
            cur = stack.pop()
            if len(cur.parts) - base_depth > max_depth:
                continue
            try:
                entries = list(cur.iterdir())
            except OSError as exc:
                walk_errors.append(f"{cur}: {exc}")
                continue
            for ent in entries:
                try:
                    if ent.is_dir():
                        stack.append(ent)
                    elif ent.name == "data.db" and cur.name == ".hiveweave":
                        if ent not in seen:
                            seen.add(ent)
                            out.append(ent)
                except OSError as exc:
                    walk_errors.append(f"{ent}: {exc}")
                    continue
    return sorted(out), skipped_roots, walk_errors


def load_samples(dbs: list[Path], ca):
    """只读读出全部 `audit_cache` 行 → ``(samples, cov)``。

    ⚠ 审计 P0-1：原先 `except sqlite3.Error: continue` 把"结构不匹配"
    （旧库无 `top_issues` 列）当"读到了没数据"**静默丢弃**，报告仍写
    "148 次审计"，读者会以为扫了全量。现在按原因分类计数，全部写进报告
    的覆盖率一节 —— "被丢掉了多少"必须是可见事实。

    ⚠ 第二轮复审 P1：行解码（`top_issues` → 行列表）改走**生产**的
    `_parse_cached_issues`，本脚本不再自备一份（原先抄了一份，与模块
    docstring 自述的"不复制任何判据"自相矛盾）。
    """
    samples: list[dict] = []
    cov = {
        "dbs_found": len(dbs),
        "dbs_opened": 0,
        "skip_no_audit_cache": [],
        "skip_no_such_column": [],
        "skip_read_error": [],
        "rows_total": 0,
        "rows_dropped_bad_json": 0,
        "rows_used": 0,
    }
    for db in dbs:
        try:
            conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            cov["skip_read_error"].append(f"{db}: {exc}")
            continue
        try:
            tables = {
                r[0]
                for r in conn.execute(
                    "select name from sqlite_master where type='table'"
                ).fetchall()
            }
            if "audit_cache" not in tables:
                cov["skip_no_audit_cache"].append(str(db))
                continue
            try:
                rows = conn.execute(
                    "select agent_id, verdict, top_issues, created_at "
                    "from audit_cache"
                ).fetchall()
            except sqlite3.OperationalError as exc:
                # 旧 schema（无 top_issues 列）⇒ 单独归因，不与"读失败"混为一起
                if "no such column" in str(exc):
                    cov["skip_no_such_column"].append(f"{db}: {exc}")
                else:
                    cov["skip_read_error"].append(f"{db}: {exc}")
                continue
            cov["dbs_opened"] += 1
        except sqlite3.Error as exc:
            cov["skip_read_error"].append(f"{db}: {exc}")
            continue
        finally:
            conn.close()
        for agent_id, verdict, raw, created_at in rows:
            cov["rows_total"] += 1
            issues = ca._parse_cached_issues(raw)  # ← 生产判据（单一来源）
            if not issues and _is_dirty_payload(raw):
                cov["rows_dropped_bad_json"] += 1
                continue
            cov["rows_used"] += 1
            samples.append(
                {
                    "db": str(db),
                    "agent_id": agent_id,
                    "verdict": (verdict or "").strip().upper(),
                    "issues": issues,
                    "created_at": created_at,
                }
            )
    return samples, cov


def _is_dirty_payload(raw) -> bool:
    """这一行是**脏数据**（不可解析）还是天然空？—— **只用于覆盖率计数**。

    ⚠ 边界：issue 内容本身由生产 `_parse_cached_issues` 决定（单一判据源）；
    本函数只回答"要不要把它计进 `rows_dropped_bad_json`"这一个计数问题。
    故此处可以合法地做一次独立的 JSON 形状判断 —— 它不参与任何判定。
    """
    if isinstance(raw, list):
        return False
    if not isinstance(raw, str) or not raw.strip():
        return False  # NULL / 空串 = 合法的空缓存行，不算脏
    try:
        return not isinstance(json.loads(raw), list)
    except Exception:  # noqa: BLE001 — 不可解析即脏
        return True


def analyse(samples: list[dict], ca) -> dict:
    """逐样本算 legacy / shadow 两套判定，并把 `unparsed` 行分 A/B 两类。

    判定式**全部**来自生产 `shadow_decision()` —— 本函数只做聚合与分类。
    """
    agg = {
        "audits": 0,
        "audits_issues_verdict": 0,
        "issues_total": 0,
        "legacy_blocking": 0,
        "shadow_blocking": 0,
        "would_flip": 0,
        "would_unblock": 0,
        "sev": {"high": 0, "medium": 0, "low": 0, "unparsed": 0, "conflicts": 0},
        "unparsed_class": {
            "A_fileline_no_marker": 0,
            "A_fileline_nonascii_marker": 0,
            "B_no_fileline": 0,
        },
        "samples_A": [],
        "samples_B": [],
        # 按 verdict 分桶（第二轮复审 P2：报告里"B 类全落在 PASS 审计"原先
        # 是**硬编码断言**、不由数据推导 —— 数字一变就成了谎话）。
        "cls_by_verdict": {},
        # 误拦暴露面：verdict=ISSUES 且 legacy 本来**不拦**（issue 全为
        # medium/low）。这些审计只要模型多写**一行叙述**，fail-safe 就会把它
        # 判成 blocking ⇒ 纯误拦。度量"离误拦有多近"。
        "issues_verdict_no_legacy_high": 0,
        "issues_verdict_with_b": 0,
        "b_lines_in_issues_verdict": 0,
    }
    for s in samples:
        issues = s["issues"]
        verdict = s["verdict"]
        agg["audits"] += 1
        agg["issues_total"] += len(issues)

        # ── 判定：单一来源 = 生产 shadow_decision ──
        dec = ca.shadow_decision(verdict, issues)
        counts = dec["counts"]
        for k in agg["sev"]:
            agg["sev"][k] += counts[k]

        if verdict == "ISSUES":
            agg["audits_issues_verdict"] += 1
        if dec["legacy_blocking"]:
            agg["legacy_blocking"] += 1
        if dec["shadow_blocking"]:
            agg["shadow_blocking"] += 1
        if dec["would_flip"]:
            agg["would_flip"] += 1
        if dec["legacy_blocking"] and not dec["shadow_blocking"]:
            agg["would_unblock"] += 1

        # ── unparsed 行分类（**展示维度**，非判据）──
        b_in_this_audit = 0
        for issue in issues:
            if ca.parse_issue_severity(issue) is not None:
                continue
            if _FILELINE_RE.match(issue):
                if ca._SEVERITY_ANY_MARKER_RE.search(issue):
                    cls = "A_fileline_nonascii_marker"
                else:
                    cls = "A_fileline_no_marker"
            else:
                cls = "B_no_fileline"
                b_in_this_audit += 1
            agg["unparsed_class"][cls] += 1
            key = (verdict, cls)
            agg["cls_by_verdict"][key] = agg["cls_by_verdict"].get(key, 0) + 1
            bucket = "samples_A" if cls.startswith("A") else "samples_B"
            if len(agg[bucket]) < 40:
                agg[bucket].append(
                    {
                        "db": Path(s["db"]).parent.parent.name,
                        "verdict": verdict,
                        "cls": cls,
                        "line": issue[:160],
                    }
                )

        if verdict == "ISSUES":
            if not dec["legacy_blocking"]:
                agg["issues_verdict_no_legacy_high"] += 1
            if b_in_this_audit:
                agg["issues_verdict_with_b"] += 1
                agg["b_lines_in_issues_verdict"] += b_in_this_audit
    return agg


def _pct(num: int, den: int) -> str:
    """den==0 ⇒ "n/a"（**不要**在调用处写 `den or 1`，那会把"没数据"印成 0.0%）。"""
    return f"{(100.0 * num / den):.1f}%" if den else "n/a"


def render(agg: dict, samples: list[dict], roots: list[str], cov: dict,
           skipped_roots: list[str], walk_errors: list[str]) -> str:
    sev = agg["sev"]
    issues_total = agg["issues_total"]
    audits_iss = agg["audits_issues_verdict"]
    unparsed = sev["unparsed"]
    cls = agg["unparsed_class"]
    ts = sorted(s["created_at"] for s in samples if s.get("created_at"))
    span = "n/a" if not ts else f"{ts[0]} .. {ts[-1]} (epoch ms)"

    lines: list[str] = []
    add = lines.append
    add("# #12 质量门 severity 基线（真实审计样本复算）")
    add("")
    add("来源：各项目库 `audit_cache.top_issues`（原审计的完整 issue 行列表）。")
    add("判据：生产**单一定义点** `services/code_audit.py::shadow_decision()`")
    add("（日志与 `agent_events.code_audit_severity_shadow` 共用同一 dict）。")
    add("")
    add(f"- 扫描根：{', '.join(roots)}")
    add(f"- 样本：**{agg['audits']} 次审计** / **{agg['issues_total']} 条 issue 行**")
    add(f"- 时间跨度：{span}")
    add("")
    add("## 〇、覆盖率（审计 P0-1：丢弃必须是可见事实）")
    add("")
    add("| 项 | 值 |")
    add("|---|---|")
    add(f"| 候选库（`**/.hiveweave/data.db`） | {cov['dbs_found']} |")
    add(f"| 成功读取 `audit_cache` 的库 | {cov['dbs_opened']} |")
    add(f"| 无 `audit_cache` 表 ⇒ 跳过 | {len(cov['skip_no_audit_cache'])} |")
    add(
        f"| **旧 schema（无 `top_issues` 列）⇒ 跳过** | {len(cov['skip_no_such_column'])} |"
    )
    add(f"| 读错误 ⇒ 跳过 | {len(cov['skip_read_error'])} |")
    add(f"| audit_cache 行总数 | {cov['rows_total']} |")
    add(f"| JSON 脏数据 ⇒ 丢弃 | {cov['rows_dropped_bad_json']} |")
    add(f"| **实际使用** | **{cov['rows_used']}** |")
    add("")
    if cov["skip_no_such_column"]:
        add("⚠ 被跳过的库（**不是**「读到了没数据」，是结构不匹配）：")
        add("")
        for x in cov["skip_no_such_column"]:
            add(f"- `{x}`")
        add("")
        add(
            "⇒ 这些库的 audit_cache 行**未纳入**本次统计。它们的 issue 文本列"
            "历史上就不存在，所以对本指标无影响；但**库一旦迁移，数字会变**，"
            "需重跑本脚本。"
        )
        add("")
    if cov["skip_read_error"]:
        add("⚠ 读错误的库：")
        add("")
        for x in cov["skip_read_error"]:
            add(f"- `{x}`")
        add("")
    if skipped_roots or walk_errors:
        add("⚠ 输入侧问题（审计 P2-10：静默忽略会让「只扫了一个根」看起来像「扫全了」）：")
        add("")
        for x in skipped_roots:
            add(f"- 根不存在，已跳过：`{x}`")
        for x in walk_errors[:10]:
            add(f"- 遍历错误：`{x}`")
        add("")
    add("## 一、两套判定的对照（口径 = 每次审计一个样本）")
    add("")
    add("| 指标 | 值 | 说明 |")
    add("|---|---|---|")
    add(
        f"| verdict=ISSUES 的审计 | {agg['audits_issues_verdict']} / {agg['audits']} | "
        "只有它们可能拦门 |"
    )
    add(
        f"| **legacy 拦门**（现在生效的） | {agg['legacy_blocking']} / {audits_iss} "
        f"({_pct(agg['legacy_blocking'], audits_iss)}) | `\"[high]\" in issue` |"
    )
    add(
        f"| **shadow 拦门**（fail-safe 若真拦） | {agg['shadow_blocking']} / {audits_iss} "
        f"({_pct(agg['shadow_blocking'], audits_iss)}) | high + unparsed > 0 |"
    )
    add(
        f"| **would_flip**（新被拦） | **{agg['would_flip']}** | 切真拦后新增拦门数 = 误拦率分子 |"
    )
    add(
        f"| would_unblock（反而放行） | {agg['would_unblock']} | "
        "⚠ **非零是正常的**：同行既有 `SEVERITY:low` 又有 `[high]` ⇒ legacy 数 1、"
        "fail-safe 记 low。两套判据**不是**单调包含关系 |"
    )
    add("")
    add("## 二、severity 解析分布（按 issue 行）")
    add("")
    add("| 档位 | 条数 | 占比 |")
    add("|---|---|---|")
    for k in ("high", "medium", "low", "unparsed", "conflicts"):
        note = ""
        if k == "unparsed":
            note = " ← fail-safe 新兜的那批"
        elif k == "conflicts":
            note = "（仅留痕，不参与判定）"
        add(f"| {k} | {sev[k]} | {_pct(sev[k], issues_total)}{note} |")
    add("")
    add("## 三、⚠ `unparsed` 拆两类（**展示维度，不是判据**）")
    add("")
    add("| 类 | 条数 | 占 unparsed | 是什么 | fail-safe 拦它对不对 |")
    add("|---|---|---|---|---|")
    add(
        f"| **A1** 有 file:line、无 ASCII 档位 | {cls['A_fileline_no_marker']} | "
        f"{_pct(cls['A_fileline_no_marker'], unparsed)} | 真 issue，档位没写 | "
        "**对**（这就是要拦的） |"
    )
    add(
        f"| **A2** 有 file:line、有非 ASCII 档位 | {cls['A_fileline_nonascii_marker']} | "
        f"{_pct(cls['A_fileline_nonascii_marker'], unparsed)} | 如 `【high】`/`高` | "
        "**对**（旧 fail-open 漏掉的正它） |"
    )
    add(
        f"| **B** 无 file:line | {cls['B_no_fileline']} | "
        f"{_pct(cls['B_no_fileline'], unparsed)} | 前言/小结，**根本不是 issue** | "
        "**错**（纯误拦） |"
    )
    add("")
    add("⚠ 分类器有已知局限（假 A：`12:30 checked`/`备注：3 个问题`；"
        "假 B：含空格的路径 / URL）。**它只用来展示，任何判定都不要读它。**")
    add("")
    if agg["samples_A"]:
        add("### A 类样本（fail-safe 该兜的）")
        add("")
        for r in agg["samples_A"][:20]:
            add(f"- `[{r['cls']}] (verdict={r['verdict']})` {r['line']}")
        add("")
    if agg["samples_B"]:
        add("### B 类样本（fail-safe 会误兜的）")
        add("")
        for r in agg["samples_B"][:20]:
            add(f"- `[{r['cls']}] (verdict={r['verdict']})` {r['line']}")
        add("")
    add("## 三之二、误拦暴露面与「**不可采纳**的收窄方案」")
    add("")
    add(
        "现行判据 `shadow_blocking = verdict==ISSUES and (high + unparsed) > 0` —— "
        "**把无 `file:line` 的叙述行也算作 blocking**。"
    )
    add("")
    n_no_high = agg["issues_verdict_no_legacy_high"]
    n_with_b = agg["issues_verdict_with_b"]
    add("| 指标 | 值 | 含义 |")
    add("|---|---|---|")
    add(
        f"| verdict=ISSUES 且 legacy 本来不拦 | {n_no_high} / {audits_iss} "
        f"({_pct(n_no_high, audits_iss)}) | 这些审计**离误拦只差一行叙述** |"
    )
    add(
        f"| verdict=ISSUES 且含 B 类行 | {n_with_b} / {audits_iss} "
        f"({_pct(n_with_b, audits_iss)}) | 模型**是否会**在 ISSUES 里写叙述 |"
    )
    add(f"| B 类行总数（仅 ISSUES 审计内） | {agg['b_lines_in_issues_verdict']} | |")
    add(
        f"| 本次实测 would_flip | **{agg['would_flip']}** | "
        "若为 0 ⇒ 两者尚未同时命中 |"
    )
    add("")
    add("### ❌ 不可采纳：把 fail-safe 收窄为「只对带 `file:line` 的行按 high 兜底」")
    add("")
    add("提案理由本是「消除 B 类误拦」。但它有两个硬伤：")
    add("")
    add(
        "1. **会重新打开 #12 的洞。** fail-safe 的价值全在「未知 ⇒ 拦」这个**宽**口径；"
        "「档位没写、也没写路径」正是要堵的形态 —— 换一种说法描述问题即可绕过"
        "（模型完全可以整段用散文写 issue）。这正是本项目判据铁律说的"
        "「能否写一个绕过它的调用方？」——能绕，它就不是约束。"
    )
    add(
        "2. **`file:line` 本身不是可靠判据**（见 §三的假阳性/假阴性实测）："
        "`备注：3 个问题` 会被当成 A 类；含空格的 Windows 路径会被当成 B 类。"
        "用一个不可靠的判据去换掉一个宽但确定的兜底，是净损失。"
    )
    add("")
    add("### ✅ 正确处置")
    add("")
    add(
        "- fail-safe **保持宽口径不动**（它就是靠宽才兜得住）；"
        "切真拦的判定式仍是 `shadow_decision()`，不改结构。"
    )
    clsv = agg["cls_by_verdict"]
    b_issues = clsv.get(("ISSUES", "B_no_fileline"), 0)
    b_pass = clsv.get(("PASS", "B_no_fileline"), 0)
    b_other = cls["B_no_fileline"] - b_issues - b_pass
    add(
        f"- B 类误拦作为**已观测残余**登记：B 类行共 {cls['B_no_fileline']} 条，"
        f"按 verdict 分布 = ISSUES **{b_issues}** / PASS {b_pass} / 其它 {b_other}"
        "（**由数据推导**，不是断言）。"
    )
    add(
        f"  · 只有落在 **ISSUES** 里的 B 类行会真的加重 blocking（本次 {b_issues} 条）；"
        "落在 PASS 里的不参与判定。"
    )
    add(
        "- **重查条件**：一旦在 `agent_events.code_audit_severity_shadow` 里看到"
        " `would_flip=1` **且** `unparsed` 全部来自无 `file:line` 的行 ⇒ "
        "模型开始在 ISSUES 审计里写叙述，此时再谈处置（优先改 `_parse_issues` "
        "的过滤，而不是收窄 fail-safe）。"
    )
    add("")
    add("## 四、复算")
    add("")
    add("```bash")
    add("cd apps/hiveweave-py")
    add("uv run python ../../scripts/measure_audit_severity_baseline.py")
    add("```")
    add("")
    add("⚠ 样本是**历史**审计（修复落地前产出），代表模型当时的自然输出格式；")
    add("模型/上游换代后应重跑本脚本。")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", action="append", default=None, help="项目根，可重复")
    ap.add_argument(
        "--out",
        default=None,
        help=("报告输出路径（UTF-8）。⚠ 缺省时**带当天日期**命名 —— "
              "此前默认写死 2026-09-15，重跑会把上一份证据**覆盖掉**"
              "（09-16 已被覆盖过一次，原始证据不可恢复）"),
    )
    args = ap.parse_args()

    roots = args.root or DEFAULT_ROOTS
    ca = _import_production()

    dbs, skipped_roots, walk_errors = find_dbs(roots)
    samples, cov = load_samples(dbs, ca)
    if not samples:
        print("FATAL: no usable audit_cache samples found")
        print(f"  scanned {len(dbs)} project DB(s) under: {', '.join(roots)}")
        print(f"  skipped(no column)={len(cov['skip_no_such_column'])} "
              f"skipped(read error)={len(cov['skip_read_error'])}")
        return 3

    agg = analyse(samples, ca)
    report = render(agg, samples, roots, cov, skipped_roots, walk_errors)

    if args.out:
        out = Path(args.out)
    else:
        # ⚠ 默认名**必须带当天日期**（审计 D-5）：此前写死 `…-2026-09-15.txt`，
        # 09-16 重跑时把那份（离线 148 样本口径的）证据**直接覆盖掉**，
        # 原始证据不可恢复。日期化之后每次复核各留一份。
        from datetime import date

        out = (
            Path(__file__).resolve().parent.parent
            / "docs" / "platform-issue-research"
            / f"audit-severity-baseline-{date.today().isoformat()}.txt"
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")

    # stdout 用 ASCII 摘要，避免 Windows 控制台编码把中文糊掉；细节看报告文件。
    sev = agg["sev"]
    cls = agg["unparsed_class"]
    print(f"report -> {out}")
    print(
        f"coverage dbs_found={cov['dbs_found']} opened={cov['dbs_opened']} "
        f"no_column={len(cov['skip_no_such_column'])} read_err={len(cov['skip_read_error'])} "
        f"rows={cov['rows_total']} used={cov['rows_used']} dropped={cov['rows_dropped_bad_json']}"
    )
    print(f"samples={agg['audits']} issues={agg['issues_total']}")
    print(
        "legacy_blocking={} shadow_blocking={} would_flip={} would_unblock={}".format(
            agg["legacy_blocking"], agg["shadow_blocking"],
            agg["would_flip"], agg["would_unblock"],
        )
    )
    print(
        "sev high={} medium={} low={} unparsed={} conflicts={}".format(
            sev["high"], sev["medium"], sev["low"], sev["unparsed"], sev["conflicts"]
        )
    )
    print(
        "unparsed A_fileline_no_marker={} A_fileline_nonascii={} B_no_fileline={}".format(
            cls["A_fileline_no_marker"], cls["A_fileline_nonascii_marker"],
            cls["B_no_fileline"],
        )
    )
    print(
        "exposure issues_no_legacy_high={} issues_with_b={} b_lines_in_issues={}".format(
            agg["issues_verdict_no_legacy_high"], agg["issues_verdict_with_b"],
            agg["b_lines_in_issues_verdict"],
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
