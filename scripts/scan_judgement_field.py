#!/usr/bin/env python
"""扫描「判据字段被消费时静默丢失」的耦合点（kind 一类的状态字段）。

背景
----
把某个判定从**文本判据**（标题子串）改成**状态判据**（DB 列，如 `kind`）之后，
真正的缺陷不是一处，而是**一类**：凡是把「行的某个子集」交给判定的环节，只要
那一步没带上该列，判定就会**恒为「不匹配」**，依赖它的门全部**静默失效**。

2026-09-14 实测（#11 把 VERIFY 判定从标题换成 `kind`）：同一形态撞见 4 处真缺陷，
其中最严重的一处让「VERIFY 的 reviewer 必须钉在 creator」这条规则**永不生效**
（等于开了自审的后门）。

本脚本用 AST 找出这三类载体 —— 判的是**数据流缺列**（状态），不是文案匹配：

  A. 窄 SELECT 缺列 × 同函数喂判定
     函数里有 `FROM tasks` 的 SQL 常量、字段表不含目标列，
     且同函数出现 `_is_verify_task(` / `is_verify_task(`。
     ⚠ 会出假阳性（窄行只用于读 status/assignee_id）⇒ **候选要人核**，不做自动放行。

  B. 中间视图缺键
     本函数里用 dict 字面量赋的局部变量被直接传给判定，且该字面量无目标键。
     这是**最隐蔽的一类**（`draft = {...}` 看着只是搬运字段）。

  C. 测试面 fixture 缺键
     dict 字面量里 `title` 以某个标记开头（默认 `VERIFY`）却没有目标键。
     这类会让测试**假绿**：断言走的是「非 VERIFY」分支，名字却写着 VERIFY。

用法
----
    python scripts/scan_judgement_field.py                       # 默认 kind / VERIFY
    python scripts/scan_judgement_field.py --field kind --marker VERIFY
    python scripts/scan_judgement_field.py --kinds A B C         # 只跑指定类别

退出码恒为 0（这是**盘点工具**，不是 gate）—— 见下方「为什么不做成 gate」。
"""
from __future__ import annotations

import argparse
import ast
import pathlib
import re
import sys

JUDGE_NAMES = {"_is_verify_task", "is_verify_task"}
DEFAULT_FIELD = "kind"
DEFAULT_MARKER = "VERIFY"

# 判定字段的默认扫描根（相对仓库根）
DEFAULT_ROOTS = ("apps/hiveweave-py/src", "apps/hiveweave-py/tests")


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[1]


def _iter_py(root: pathlib.Path):
    for p in sorted(root.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        yield p


def _sql_consts_without_field(
    fn: ast.AST, field: str
) -> list[tuple[int, str]]:
    out = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        sql = node.value
        if not re.search(r"\bFROM\s+tasks\b", sql, re.I):
            continue
        if "SELECT" not in sql.upper():
            continue
        if re.search(rf"\b{re.escape(field)}\b", sql, re.I):
            continue
        out.append((node.lineno, " ".join(sql.split())[:110]))
    return out


def _calls_judge(fn: ast.AST) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else (
                f.id if isinstance(f, ast.Name) else None
            )
            if name in JUDGE_NAMES:
                return True
    return False


def _local_dict_keys(fn: ast.AST) -> dict[str, set[str]]:
    """本函数里「名字 → dict 字面量的键集合」。"""
    out: dict[str, set[str]] = {}
    for node in ast.walk(fn):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if not isinstance(value, ast.Dict):
            continue
        keys = {
            k.value for k in value.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        }
        targets = (
            node.targets if isinstance(node, ast.Assign) else [node.target]
        )
        for t in targets:
            if isinstance(t, ast.Name):
                out[t.id] = keys
    return out


def scan_a(root: pathlib.Path, field: str) -> list[str]:
    hits = []
    for p in _iter_py(root):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not _calls_judge(fn):
                continue
            for line, sql in _sql_consts_without_field(fn, field):
                hits.append(
                    f"{p}:{line}: [A/{fn.name}] 窄 SELECT 无 '{field}': {sql}"
                )
    return hits


def scan_b(root: pathlib.Path, field: str) -> list[str]:
    hits = []
    for p in _iter_py(root):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            local = _local_dict_keys(fn)
            if not local:
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else (
                    f.id if isinstance(f, ast.Name) else None
                )
                if name not in JUDGE_NAMES:
                    continue
                args = [
                    a.id for a in node.args if isinstance(a, ast.Name)
                ] + [
                    kw.value.id for kw in node.keywords
                    if isinstance(kw.value, ast.Name)
                ]
                for vname in args:
                    keys = local.get(vname)
                    if keys is not None and field not in keys:
                        hits.append(
                            f"{p}:{node.lineno}: [B/{fn.name}] "
                            f"judge({vname}) — 局部 dict 缺 '{field}' "
                            f"keys={sorted(keys)}"
                        )
    return hits


def scan_c(root: pathlib.Path, field: str, marker: str) -> list[str]:
    hits = []
    for p in _iter_py(root):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            keys = [
                k.value for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            ]
            if field in keys:
                continue
            title = None
            for k, v in zip(node.keys, node.values):
                if (
                    isinstance(k, ast.Constant)
                    and k.value == "title"
                    and isinstance(v, ast.Constant)
                    and isinstance(v.value, str)
                ):
                    title = v.value
            if title and title.upper().startswith(marker.upper()):
                hits.append(f"{p}:{node.lineno}: [C] {title!r} 无 '{field}'")
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--field", default=DEFAULT_FIELD, help="判定字段名")
    ap.add_argument("--marker", default=DEFAULT_MARKER, help="标题标记前缀")
    ap.add_argument(
        "--kinds", nargs="*", default=["A", "B", "C"],
        help="要跑的类别（A/B/C）",
    )
    ap.add_argument("--root", action="append", default=None, help="扫描根（可多次）")
    args = ap.parse_args()

    base = _repo_root()
    roots = [
        (base / r) if not pathlib.Path(r).is_absolute() else pathlib.Path(r)
        for r in (args.root or DEFAULT_ROOTS)
    ]
    kinds = {k.upper() for k in args.kinds}

    all_hits: list[str] = []
    for root in roots:
        if not root.exists():
            print(f"skip (missing): {root}", file=sys.stderr)
            continue
        if "A" in kinds:
            all_hits += [f"[src] {h}" for h in scan_a(root, args.field)]
        if "B" in kinds:
            all_hits += [f"[src] {h}" for h in scan_b(root, args.field)]
        if "C" in kinds:
            all_hits += [f"[test] {h}" for h in scan_c(root, args.field, args.marker)]

    for line in all_hits:
        print(line)
    print(f"--- {len(all_hits)} candidate(s) ---")
    print(
        "note: A/C 类别含已知假阳性（窄行只用于其它列 / 测试显式 stub 了判定），"
        "**逐条人核**；本工具是盘点，不是 gate。",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
