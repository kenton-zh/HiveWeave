"""s3-clone_06 P0-3 / P0-4：失败签名的归因分类与自指抑制。

P0-3（误归因）：F10 把一条 **shell 方言不兼容** 错误归类成
`blocked: 平台护栏拒绝（权限/沙箱/安全）`，撞坑 Agent 拿到的是错的排查方向
（DSH postmortem 0004 同构：宽泛签名 → 误归因）。契约：归因按「信息量从具体
到笼统」判定，方言必须先于 blocked。

P0-3（自指）：F10 的 hook 是「先写签名、后取提示」——同一次失败写入的条目
会被自己立刻命中，而其内容 = 错误原文 + 占位根因（见错误原文）。提示它
「先读它」= 指它读一面镜子（TEST_DSH_38 实测 18/18、本项目 136/136）。
契约：条目未携带超出错误原文的信息时不广播。
"""

from __future__ import annotations

from hiveweave.services.failure_signature import (
    _ROOT_CAUSE_PLACEHOLDER,
    _signature_has_solution,
    attribution_of,
)


def test_dialect_is_not_reported_as_security_block():
    """方言不兼容 ≠ 安全拦截：两者排查方向完全不同。"""
    attr = attribution_of(
        {
            "dialect_failed": True,
            "runner_failed": True,
            "blocked": True,  # 方言门也置 blocked，不得让它抢先
        }
    )
    assert "方言" in attr
    assert "从未执行" in attr
    assert "平台护栏拒绝" not in attr


def test_runner_failed_precedes_command_failed():
    """命令没跑起来时只能记 runner（DSH 顺序：runner 优先于 denial）。"""
    attr = attribution_of({"runner_failed": True, "command_failed": True})
    assert attr.startswith("runner_failed:")
    assert "command_failed" not in attr


def test_command_failed_and_blocked_fall_through():
    assert attribution_of({"command_failed": True}).startswith("command_failed:")
    assert attribution_of({"blocked": True}).startswith("blocked:")


def test_empty_result_yields_empty_attribution():
    assert attribution_of({}) == ""


def test_signature_without_solution_is_a_mirror():
    """只有错误原文 + 占位根因的条目 = 自指镜子，不得广播。"""
    placeholder_entry = (
        "[失败签名] tool=bash | unix-only-abc123\n"
        f"根因提示: {_ROOT_CAUSE_PLACEHOLDER}\n"
        "首个撞到的 Agent: a1（撞到该签名后请先检索本项目共享空间是否已有解法）"
    )
    assert _signature_has_solution(placeholder_entry) is False
    assert _signature_has_solution("[失败签名] tool=x | y\n根因提示: \n") is False
    assert _signature_has_solution("[失败签名] tool=x | y") is False


def test_signature_with_real_root_cause_is_actionable():
    entry = (
        "[失败签名] tool=bash | unix-only-abc123\n"
        "根因提示: runner_failed: shell 方言不兼容 —— 命令从未执行\n"
        "首个撞到的 Agent: a1"
    )
    assert _signature_has_solution(entry) is True
