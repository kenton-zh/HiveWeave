r"""校验**冻结产物**里跑的到底是哪份代码 —— 构建后必跑（约 10 秒）。

## 为什么需要它

「我改了代码」与「用户双击的那个 exe 里跑的代码」是**两份东西**。以下三条都**不证明**
产物是新代码：`HiveWeave.exe` 的 mtime 变了 / 构建脚本打印了 DONE /
`smoke_release.py --require-env` 通过（后者只验环境与启动，不做代码内容校验）。

## 为什么不能直接 grep 产物

2026-09-23 实测：`grep -rla "<坏名字>" dist/HiveWeave/` 与 `grep -rla "<新名字>"`
**两向都 0 命中** —— `_internal/` 里**没有** `hiveweave` 包目录，真身是 exe 内嵌的
`PYZ.pyz`（**zlib 压缩**）⇒ 坏代码搜不出（漏报）、新代码也搜不出（假阴性）。
本脚本改为取出 **code object** 查名字池（`co_varnames / co_names / co_freevars /
co_cellvars`）—— 解压后是精确的状态量，不受压缩/编码影响。

## ⚠ 纪律：**永远不要只跑 `--forbid`**

`--forbid` 单独用时，"零命中"可能只是因为**解析失败**（拿不到模块、名字池为空），
而那种情形与"修复已上线"在输出上**长得一样**。
故每次都要同时给一个**必须存在**的 `--require`（例如改动引入的新方法名）：
它先失败，`--forbid` 的零命中才有意义。

## 用法

    apps\hiveweave-py\.venv\Scripts\python.exe scripts\verify_frozen_module.py \
        apps\desktop\dist\HiveWeave\HiveWeave.exe \
        --module hiveweave.agents.agent \
        --require _resume_upstream_attempt --require interrupted_run_id \
        --forbid restored_upstream_attempt

    # 看某个函数的签名/引用名（排查用）
    ... --show-func _run_llm

退出码：0 = 全部判据通过；1 = 有判据失败；2 = 用法/环境错误。
"""

from __future__ import annotations

import argparse
import sys
import types

POOLS = ("co_varnames", "co_names", "co_freevars", "co_cellvars")


def load_module_code(exe: str, module: str) -> types.CodeType:
    from PyInstaller.archive.readers import CArchiveReader

    car = CArchiveReader(exe)
    # ⚠ PyInstaller 6.22.2 **没有** get_archive_names() ⇒ 用 toc（实测 AttributeError）
    pyz_name = next((n for n in car.toc if str(n).endswith(".pyz")), None)
    if pyz_name is None:
        raise SystemExit(f"[FAIL] {exe} 里找不到 .pyz 归档（不是 PyInstaller 产物？）")
    pyz = car.open_embedded_archive(str(pyz_name))
    if module not in pyz.toc:
        raise SystemExit(
            f"[FAIL] 归档里没有模块 {module!r} —— 模块名写错，或它没被打进包"
        )
    top = pyz.extract(module)
    if not isinstance(top, types.CodeType):
        raise SystemExit(f"[FAIL] extract() 返回 {type(top)}，预期 code object")
    return top


def all_codes(top: types.CodeType) -> list[types.CodeType]:
    out = [top]
    for const in top.co_consts:
        if isinstance(const, types.CodeType):
            out.extend(all_codes(const))
    return out


def find_name(codes: list[types.CodeType], name: str) -> list[str]:
    """返回 name 出现的位置（`<func>.<pool>`），空 = 零命中。"""
    hits = []
    for co in codes:
        for pool in POOLS:
            if name in getattr(co, pool):
                hits.append(f"{co.co_name}.{pool}")
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description="校验冻结产物里的模块代码")
    ap.add_argument("exe")
    ap.add_argument("--module", required=True, help="如 hiveweave.agents.agent")
    ap.add_argument("--require", action="append", default=[],
                    help="必须出现在某个名字池里（可重复）")
    ap.add_argument("--forbid", action="append", default=[],
                    help="必须不出现在任何名字池里（可重复）")
    ap.add_argument("--show-func", default=None,
                    help="打印该函数的 varnames / names（排查用）")
    args = ap.parse_args()

    if args.forbid and not args.require:
        print("[FAIL] 只给 --forbid 不足以构成判据：零命中也可能只是解析失败。"
              "请同时给一个必须存在的 --require。")
        return 2

    top = load_module_code(args.exe, args.module)
    codes = all_codes(top)
    print(f"[ok] {args.exe}\n     模块 {args.module}：{len(codes)} 个 code object"
          f"（含嵌套）")

    failed = False

    for name in args.require:
        hits = find_name(codes, name)
        if hits:
            print(f"[PASS] 必须存在：{name!r} @ {hits[:4]}"
                  f"{' …' if len(hits) > 4 else ''}")
        else:
            print(f"[FAIL] 必须存在但零命中：{name!r} —— 修复没进产物，"
                  f"或名字写错（判据前提不成立）")
            failed = True

    for name in args.forbid:
        hits = find_name(codes, name)
        if hits:
            print(f"[FAIL] 必须不存在但命中：{name!r} @ {hits[:4]} —— 旧代码仍在产物里")
            failed = True
        else:
            print(f"[PASS] 必须不存在：{name!r} 零命中"
                  f"（前提：上面的 --require 已证明名字池可读）")

    if args.show_func:
        co = next((c for c in codes if c.co_name == args.show_func), None)
        if co is None:
            print(f"[FAIL] 找不到函数 {args.show_func!r}")
            failed = True
        else:
            print(f"[show] {co.co_name}()  varnames={co.co_varnames[:8]}")
            print(f"       names={co.co_names[:12]}")

    print("\n结论：", "PASS —— 产物里就是这份代码" if not failed else "FAIL")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
