"""批21 修复4 回归：恢复简报逐唤醒重放去重。

实锤（DSH 归零 ×2、木槿 ×3）：重启后桅杆 7 分钟收到 6 次逐字相同的
[RUN RECOVERY]（同失败步清单）——每次平台内唤醒都重新生成同一份
checkpoint 并全文注入。修法：同 agent 同内容指纹在一次唤醒周期内只注入
一次全文；重复触发改注入一行 stub；跨外部唤醒或内容变化则重新注入。
"""

from __future__ import annotations

from hiveweave.agents.agent import (
    _RECOVERY_BRIEF_FULL_HEAD,
    format_recovery_brief,
    recovery_brief_fingerprint,
)

_SUMMARY = (
    "Interrupted run had 3 LLM round(s) and 2 completed tool call(s).\n"
    "Completed tool calls:\n  - read_file (12ms) result_hash=abc\n"
    "Failed steps: 1\n  - apply_patch: stale view"
)

_FP = recovery_brief_fingerprint("run-1", _SUMMARY)


def _is_full(prefix: str) -> bool:
    return prefix.startswith(_RECOVERY_BRIEF_FULL_HEAD) and "Completed steps" in prefix


def test_same_fingerprint_six_wakes_one_full_five_stubs():
    """① 同指纹连续 6 次平台内唤醒 ⇒ 1 次全文 + 5 次 stub。"""
    state: dict | None = None
    prefixes: list[str] = []
    for _ in range(6):
        prefix, state = format_recovery_brief(
            state, _FP, external_wake=False, summary=_SUMMARY
        )
        prefixes.append(prefix)
    assert _is_full(prefixes[0])
    for i, p in enumerate(prefixes[1:], start=2):
        assert not _is_full(p), f"第 {i} 次不应注入全文：{p}"
        assert f"第 {i} 次" in p and "内容未变，勿重读" in p
    assert state == {"fingerprint": _FP, "count": 6}


def test_fingerprint_change_reinjects_full():
    """② 失败步集合变化（新指纹）⇒ 重新全文注入，计数重置。"""
    state: dict | None = None
    _, state = format_recovery_brief(
        state, _FP, external_wake=False, summary=_SUMMARY
    )
    _, state = format_recovery_brief(
        state, _FP, external_wake=False, summary=_SUMMARY
    )
    assert state["count"] == 2
    # 新中断 run（不同 run_id ⇒ 不同指纹）→ 全文 + 重置
    new_fp = recovery_brief_fingerprint("run-2", _SUMMARY)
    prefix, state = format_recovery_brief(
        state, new_fp, external_wake=False, summary=_SUMMARY
    )
    assert _is_full(prefix)
    assert state == {"fingerprint": new_fp, "count": 1}
    # 同一 run 内摘要内容变化（同 run_id，不同内容）⇒ 同样重新注入
    changed_fp = recovery_brief_fingerprint("run-2", _SUMMARY + "\n  - pwsh: timeout")
    prefix, state = format_recovery_brief(
        state, changed_fp, external_wake=False, summary=_SUMMARY + "\n  - pwsh: timeout"
    )
    assert _is_full(prefix)
    assert state == {"fingerprint": changed_fp, "count": 1}


def test_external_wake_resets_cycle():
    """③ 跨外部唤醒 ⇒ 重新注入全文并重置（用户到场 = 新上下文）。"""
    state: dict | None = None
    for _ in range(4):
        _, state = format_recovery_brief(
            state, _FP, external_wake=False, summary=_SUMMARY
        )
    assert state["count"] == 4
    prefix, state = format_recovery_brief(
        state, _FP, external_wake=True, summary=_SUMMARY
    )
    assert _is_full(prefix)
    assert state == {"fingerprint": _FP, "count": 1}
    # 重置后平台内重复唤醒从 stub 重新计数
    prefix, state = format_recovery_brief(
        state, _FP, external_wake=False, summary=_SUMMARY
    )
    assert "第 2 次" in prefix


def test_full_prefix_byte_identical_with_legacy():
    """全文注入形态与修前逐字一致（只有 stub 是新分支）。"""
    prefix, _ = format_recovery_brief(
        None, _FP, external_wake=False, summary=_SUMMARY
    )
    legacy = (
        "[RUN RECOVERY] Previous run was interrupted. "
        f"Completed steps:\n{_SUMMARY}\n"
        "Please continue from where you left off."
    )
    assert prefix == legacy


def test_run_id_participates_in_fingerprint():
    """不同中断 run 即使摘要凑巧相同也是新事件 ⇒ 重新全文注入。"""
    fp1 = recovery_brief_fingerprint("run-a", _SUMMARY)
    fp2 = recovery_brief_fingerprint("run-b", _SUMMARY)
    assert fp1 != fp2
    state: dict | None = {"fingerprint": fp1, "count": 3}
    prefix, state = format_recovery_brief(
        state, fp2, external_wake=False, summary=_SUMMARY
    )
    assert _is_full(prefix)
    assert state["count"] == 1


def test_first_injection_without_state_is_full():
    """进程冷启动（state=None）首注入必为全文（重启后第一条简报不丢）。"""
    prefix, state = format_recovery_brief(
        None, _FP, external_wake=False, summary=_SUMMARY
    )
    assert _is_full(prefix)
    assert state == {"fingerprint": _FP, "count": 1}
