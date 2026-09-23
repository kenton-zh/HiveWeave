r"""P1-5「承接判定接线」的 7 组阳性对照 —— 改坏必须让**对应那一格**转红。

用法：

    apps\hiveweave-py\.venv\Scripts\python.exe scripts\p15_carry_over_positive_controls.py

判据（全部状态/结构判据，无文本子串）：

| 改坏 | 期望红格 | 它在证明什么 |
| --- | --- | --- |
| M1 求值搬回 `chat()`（**2026-09-23 线上事故原形**） | G, H, J | 跨函数局部量不可见 ⇒ `_run_llm` 每轮 `NameError` |
| M2 同作用域、但把赋值挪到 `create_run` 之后 | G, H | `is_latest_run` 恒 False ⇒ 承接静默失效 |
| M3 归零点写死 `= 0`（退回旧行为） | F, G, H, I | 归零时机回归；I 红是"承接没生效 ⇒ 刹车不咬合"的连带 |
| M4 helper 照调、但 `interrupted_run_id` 不接（静默归零） | H, I | **H 是唯一直接抓这类"形态全对、语义全错"的**；I 红同上 |
| M5 **诱饵绕过**：`create_run` 前放一个不被使用的同名调用、
真赋值挪到其后（**审计第一轮给的**绕过形态） | G, H | **只有"绑到赋值右值"的判据 + H 的位置敏感桩能抓** |
| M6 删掉"自增后落库"那三行 | J | **审计第二轮实测此形态全仓 260 条全绿** —— 值算对了，
但没人验证它落盘；删掉后承接恒 0、P1-5 静默退回旧行为 |
| M7 刹车判据改读别的条件（`0 < 上限`，恒可重试） | I | **审计第二轮实测此类形态 F/G/H 全绿** —— 三条都只问
"值有没有落到属性"，没有一条问"那属性有没有被用来刹车" |

### 连带红格（都是合理后果，不是杂音 —— 逐条给理由）

- **M1 多一个 J**：赋值被搬出 `_run_llm` 后，**直接**调 `_run_llm` 的 J 会读到未赋值
  的属性 ⇒ 崩。J 同样基于"真执行"，抓这个只是顺手（它本来也抓不到 G 那种跨作用域）。
- **M3 / M4 多一个 I**：承接没生效（写死 0 / 参数不接）⇒ 起点恒 0 ⇒
  判据 `0 < 上限` 成立 ⇒ **刹车不咬合**、照样重试。这正是 M7 单独要证明的那件事，
  在 M3/M4 里只是被顺带暴露。
- **M3 的 G 红**是"helper 调用点被整体删掉"⇒ 拿不到供值者，不是"位置错"。

## 为什么值得固化

M1–M4 是本次修复自带的对照；**M5/M6/M7 都是独立审计给的**，且各自都曾让全部守卫
（含当时的宽集）**全绿**：

- M5 说明"存在一次同名调用排在前"这种判据**构不成约束** —— 加一个不被使用的诱饵
  就够了。修法：判据**绑到承重对象**（赋值右值里那次调用）。
- M6 说明"接了线但无人断言"是独立的失效面（`_run_ledger` 在夹具里是 `AsyncMock`，
  调用被执行、零断言）。修法：钉住写口被以**哪个 run id** 调用过。
- M7 说明"值落了地"与"值被消费"是两件事。修法：用**行为**证明承接值真的刹住了重试。

⚠ 可复用规则：**"某处有个同名调用"证明不了任何事；判据必须绑到承重对象。**
⚠ 可复用规则：**"值落到了属性上"证明不了"值被用了"；关键路径要用行为对照。**
⚠ 本脚本**会改写 `src/hiveweave/agents/agent.py`**（跑完必还原并按 md5 校验）；
不要在仓库正在跑测试/审计时并发执行。
"""

from __future__ import annotations

import hashlib
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
SRC = REPO / "apps/hiveweave-py/src/hiveweave/agents/agent.py"
PY = REPO / "apps/hiveweave-py/.venv/Scripts/python.exe"
TESTDIR = "tests/test_p1_5_upstream_retry_budget.py"

INLINE = (
    "            self._main_upstream_attempt = "
    "await self._resume_upstream_attempt(\n"
    "                interrupted_run_id\n"
    "            )\n"
)
CHAT_ANCHOR = "            # Create activation record\n"
CREATE_RUN_TAIL = (
    "            except Exception as e:\n"
    "                log.debug(\"run_ledger.create_run_failed\", error=str(e))\n"
    "            self._run_step_counter = 0\n"
)
WRITE_BACK = (
    "                        _rid = getattr(self, \"_current_run_id\", None)\n"
    "                        if _rid:\n"
    "                            await self._run_ledger.set_upstream_attempt(\n"
    "                                self.id, _rid, self._main_upstream_attempt\n"
    "                            )\n"
)

F = f"{TESTDIR}::test_reset_point_really_calls_the_resume_helper"
G = f"{TESTDIR}::test_carry_over_is_evaluated_before_create_run"
H = f"{TESTDIR}::test_reset_point_actually_receives_carried_budget"
I = f"{TESTDIR}::test_carried_budget_actually_brakes_the_retry"
J = f"{TESTDIR}::test_increment_is_persisted_to_the_run_row"
TESTS = [F, G, H, I, J]
SHORT = {F: "F", G: "G", H: "H", I: "I", J: "J"}

EXPECT: dict[str, set[str]] = {
    "M1_scope_moved_to_chat": {"G", "H", "J"},
    "M2_ordering_after_create_run": {"G", "H"},
    "M3_reset_point_literal_zero": {"F", "G", "H", "I"},
    "M4_param_not_wired": {"H", "I"},
    "M5_decoy_call_before_real_after": {"G", "H"},
    "M6_write_back_deleted": {"J"},
    "M7_gate_reads_other_counter": {"I"},
}

# 每个改坏必须**确实**用掉哪段原文（anchors 的命中次数断言 + 产物形态断言）
# ⚠ 2026-09-23 审计实测的教训：`str.replace` 锚点不匹配时**静默不替换**，
#   而"没改到"与"改了但守卫没抓到"在输出上**长得一样** ⇒ 会打印假 OK。
#   故：每个 mutation 都断言锚点命中数，且断言产物与原文**不同**。
ANCHOR_COUNTS: dict[str, tuple[str, int]] = {
    "M1_scope_moved_to_chat": (INLINE, 1),
    "M2_ordering_after_create_run": (INLINE, 1),
    "M3_reset_point_literal_zero": (INLINE, 1),
    "M4_param_not_wired": (INLINE, 1),
    "M5_decoy_call_before_real_after": (INLINE, 1),
    "M6_write_back_deleted": (WRITE_BACK, 1),
    "M7_gate_reads_other_counter": (
        "self._main_upstream_attempt < _MAIN_LOOP_STREAM_RETRIES", 1
    ),
}


def _move_inline_out(src: str) -> str:
    return src.replace(INLINE, "", 1)


def apply_mutation(name: str, src: str) -> str:
    if name == "M1_scope_moved_to_chat":
        src = _move_inline_out(src)
        assert src.count(CHAT_ANCHOR) == 1, "M1: chat() 锚点不唯一"
        return src.replace(CHAT_ANCHOR, INLINE + CHAT_ANCHOR, 1)

    if name == "M2_ordering_after_create_run":
        src = _move_inline_out(src)
        assert src.count(CREATE_RUN_TAIL) == 1, "M2: create_run 收尾锚点不唯一"
        return src.replace(CREATE_RUN_TAIL, CREATE_RUN_TAIL + INLINE, 1)

    if name == "M3_reset_point_literal_zero":
        src = _move_inline_out(src)
        anchor = "            # ── Durable Run Ledger: create run ──\n"
        assert src.count(anchor) == 1, "M3: create_run 注释锚点不唯一"
        return src.replace(
            anchor, "            self._main_upstream_attempt = 0\n" + anchor, 1
        )

    if name == "M4_param_not_wired":
        return src.replace(INLINE, INLINE.replace("interrupted_run_id", "None"), 1)

    if name == "M5_decoy_call_before_real_after":
        decoy = "            self._resume_upstream_attempt(interrupted_run_id)\n"
        src = src.replace(INLINE, decoy, 1)
        assert src.count(CREATE_RUN_TAIL) == 1, "M5: create_run 收尾锚点不唯一"
        return src.replace(CREATE_RUN_TAIL, CREATE_RUN_TAIL + INLINE, 1)

    if name == "M6_write_back_deleted":
        assert src.count(WRITE_BACK) == 1, "M6: 落库块锚点不唯一"
        return src.replace(WRITE_BACK, "", 1)

    if name == "M7_gate_reads_other_counter":
        old = "self._main_upstream_attempt < _MAIN_LOOP_STREAM_RETRIES"
        assert src.count(old) == 1, "M7: 刹车判据锚点不唯一"
        return src.replace(old, "0 < _MAIN_LOOP_STREAM_RETRIES", 1)

    raise AssertionError(f"未定义的改坏：{name}")


def run_tests() -> tuple[set[str], str]:
    proc = subprocess.run(
        [str(PY), "-m", "pytest", *TESTS, "-q", "--no-header",
         "-p", "no:cacheprovider"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(REPO / "apps/hiveweave-py"),
    )
    out = proc.stdout + proc.stderr
    red = {
        SHORT.get(ln[len("FAILED "):].split(" ")[0], "?")
        for ln in out.splitlines() if ln.startswith("FAILED ")
    }
    summary = ""
    for ln in out.splitlines():
        if "passed" in ln or "failed" in ln:
            summary = ln.strip()
    return red, summary


def main() -> int:
    if not SRC.exists() or not PY.exists():
        print(f"路径不对：SRC={SRC} PY={PY}")
        return 2
    original = SRC.read_text(encoding="utf-8")
    md5_before = hashlib.md5(original.encode("utf-8")).hexdigest()
    print(f"目标：{SRC.relative_to(REPO)}  md5={md5_before[:12]}")
    rc = 0
    try:
        for name in EXPECT:
            # ① 锚点命中数断言（防"静默没改"）
            needle, want = ANCHOR_COUNTS[name]
            got = original.count(needle)
            if got != want:
                print(f"!! {name}: 锚点命中 {got} 次（期望 {want}）—— 对照失真，未执行")
                rc = 1
                continue

            mutated = apply_mutation(name, original)
            # ② 产物断言（防"改了等于没改"）
            if mutated == original:
                print(f"!! {name}: 产物与原文相同 —— 改坏没生效，对照失真")
                rc = 1
                continue

            SRC.write_text(mutated, encoding="utf-8")
            red, summary = run_tests()
            ok = red == EXPECT[name]
            rc |= 0 if ok else 1
            print(
                f"{'OK ' if ok else '!! '}{name}: 红格={sorted(red) or '(无)'}"
                f" 期望={sorted(EXPECT[name])}  [{summary}]"
            )
            if not red:
                print("    !!! 该改坏未被任何守卫抓住")
    finally:
        SRC.write_text(original, encoding="utf-8")
        back = SRC.read_text(encoding="utf-8")
        md5_after = hashlib.md5(back.encode("utf-8")).hexdigest()
        if md5_after != md5_before:
            print(f"!!! 还原失败：md5 {md5_after[:12]} != {md5_before[:12]}"
                  f" —— 仓库里有脏文件，请手工核对 `git diff`")
            rc = 1
        else:
            print(f"[restored] agent.py 已还原（md5 校验一致 {md5_after[:12]}）")
    return rc


if __name__ == "__main__":
    sys.exit(main())
