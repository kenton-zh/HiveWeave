"""TOOL_CAPABILITY 全覆盖启动断言（42 轮双项目报告 P1）。

背景（2026-08-13 实锤）：``start_dev_server`` 注册后没进 ``TOOL_CAPABILITY``
映射，而消费点 ``policy.tool_hard_deny`` 对未映射工具一律放行
（``TOOL_CAPABILITY.get(tool_name) is None → None``）——CEO 无 BASH_SHELL
硬门被绕。「新工具注册但忘了做能力判定」默认就是绕过硬门，必须 fail-loud。

本模块在启动时断言：``tools.base.list_tool_names()`` 的**每一个**注册工具
要么 (a) 在 ``services.policy.TOOL_CAPABILITY`` 有能力映射，要么 (b) 在
下方 ``EXEMPT_TOOLS`` 显式豁免集里。断言失败 = 启动失败，报文带缺失清单。

豁免 ≠ 无门：豁免集是**有意不设能力硬门**的工具——只读检查类、通信类、
turn 生命周期类、任务自助类（属主校验在 TaskService）、告警类、文档/记忆/
花名册写类（路径 scope 在 policy.write_path_allowed / 各 service 内校验）。
它们对全 family 可见（``permission._BASE_TOOLS`` 及各 preset）且历史上
有意留在映射外。其中 ``look_at_image`` 有回归锁（tests/test_look_at_image.py：
"Must stay out of TOOL_CAPABILITY so HR (no BROWSE) can still call it"）。

新增工具时：默认应加 TOOL_CAPABILITY 映射；确实不该设能力硬门的，加进
EXEMPT_TOOLS 并写明理由——二选一，不允许沉默放行。
"""

from __future__ import annotations

import structlog

log = structlog.get_logger(__name__)


class ToolCapabilityMappingError(AssertionError):
    """Raised at startup when a registered tool has no capability decision."""


# 有意不设能力硬门的注册工具（新增请附一行理由）。断言只对「既不在
# TOOL_CAPABILITY、也不在此集合」的工具报错。
EXEMPT_TOOLS = frozenset({
    # ── 只读检查 / 状态观测：20 条已收编 TOOL_CAPABILITY→SOURCE_READ
    #    （45 轮批次6；五族均有 SOURCE_READ，零行为变化）──
    # ── 通信 / 协作（收件方由 org 关系约束，非能力门） ──
    "send_message",
    "message_superior",
    "message_subordinate",
    "message_peer",
    "message_team",
    "message_user",
    "ask_agent",
    "notify_agent",
    "question",
    # ── turn 生命周期（自身回合出口，与角色能力正交） ──
    "commit_turn",
    "defer_task_advance",
    # ── 任务自助（属主/状态机校验在 TaskService，非能力门） ──
    "claim_task",
    "update_task_status",
    "submit_task",
    "update_progress",
    "attest_doc_review",
    "review",
    # ── 告警（game_time 定时提醒，非执行类） ──
    "schedule_alarm",
    "cancel_alarm",
    # ── 文档 / 记忆 / 花名册写（路径 scope 走 policy.write_path_allowed 等） ──
    "save_charter",
    "update_goals",
    "update_roster",
    "write_memory",
    "consolidate_memories",
    "write_work_log",
    # ── 杂项只读/工具性 ──
    # （git_worktree_checkpoint 已收编 → SOURCE_WRITE，45 轮批次6）
    "calculate",
    "todowrite",
    "webfetch",
    "websearch",
    # ── 显式回归锁：必须留在映射外（HR 无 BROWSE 也要能看图）──
    # tests/test_look_at_image.py::test_look_at_image_not_bound_to_browse_capability
    "look_at_image",
    # ── 能力判定走路径 scope，不走工具级能力门 ──
    "write_file",  # tool_hard_deny 特判：scope 由 write_path_allowed/DOC_WRITE 判
    # ── 跨仓库补丁官方通道（F12 疏导出口）──
    # 沙箱禁直写平台仓库，deliver_patch 是唯一合法出口；落点限
    # .hiveweave/patch-deliveries/（下游人工评审后应用），非任意执行通道。
    "deliver_patch",
    # ── 团队开会（docs/spec/team-meeting.md）──
    # speak_in_meeting / continue_meeting_round / conclude_topic 只在
    # MeetingTurnRunner 执行期白名单内可达（runner 回调本地拦截，不落
    # executor）；普通路径由 policy.tool_hard_deny 的 MEETING_RUNNER_TOOLS
    # 一律硬拒（policy.py），无角色能越权 —— 能力门即「全员显式硬拒」。
    "speak_in_meeting",
    "continue_meeting_round",
    "conclude_topic",
    # 注：python_script / run_smoke 曾在此豁免，42 轮审计 P1 指出二者是
    # 执行通道（python_script native 路径不经 command_guard，可一行绕过
    # icacls/takeown 等 bash 硬门）→ 已改映射 TOOL_CAPABILITY：
    # python_script→BASH_SHELL、run_smoke→TEST_RUN（见 services/policy.py）。
})


def assert_all_tools_mapped() -> None:
    """Fail-loud: every registered tool must be mapped or explicitly exempt.

    Called from main.py lifespan (after the tool registry audit). Raises
    :class:`ToolCapabilityMappingError` listing all unmapped tools.
    """
    import hiveweave.tools  # noqa: F401 — 触发 @tool 装饰器填充注册表
    from hiveweave.services.policy import TOOL_CAPABILITY
    from hiveweave.tools.base import list_tool_names

    missing = [
        name
        for name in list_tool_names()
        if name not in TOOL_CAPABILITY and name not in EXEMPT_TOOLS
    ]
    if missing:
        raise ToolCapabilityMappingError(
            "TOOL_CAPABILITY 全覆盖断言失败：以下注册工具既无能力映射也不在 "
            f"豁免集（未映射=硬门放行，见 services.tool_capability_check 模块"
            f"说明）：{sorted(missing)}。请在 services.policy.TOOL_CAPABILITY "
            "加映射，或在 EXEMPT_TOOLS 显式豁免并写明理由。"
        )
    log.info(
        "tool_capability_mapping_ok",
        registered="all_mapped_or_exempt",
    )
