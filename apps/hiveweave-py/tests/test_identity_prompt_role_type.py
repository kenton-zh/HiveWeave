"""Identity prompt: hire config keys + literal {kind:agent} in f-strings."""

from __future__ import annotations

from hiveweave.prompts.coordinator import build_coordinator_script
from hiveweave.prompts.executor import build_executor_script
from hiveweave.prompts.identity import build_identity_prompt, resolve_prompt_role_type

ARCHITECT = "Python互动学习产品架构师"


def test_resolve_prefers_permission_type_then_role_type():
    assert resolve_prompt_role_type("coordinator", "executor") == "coordinator"
    assert resolve_prompt_role_type("", "coordinator") == "coordinator"
    assert resolve_prompt_role_type(None, "executor") == "executor"
    assert resolve_prompt_role_type(None, None) == "executor"


def test_normalize_mirrors_permission_type_and_role_type():
    from hiveweave.agents.supervisor import normalize_agent_runtime_config

    hired = normalize_agent_runtime_config({"permission_type": "coordinator"})
    assert hired["permission_type"] == "coordinator"
    assert hired["role_type"] == "coordinator"

    restarted = normalize_agent_runtime_config({"role_type": "coordinator"})
    assert restarted["permission_type"] == "coordinator"
    assert restarted["role_type"] == "coordinator"

    conflict = normalize_agent_runtime_config(
        {"role_type": "executor", "permission_type": "coordinator"}
    )
    assert conflict["permission_type"] == "coordinator"
    assert conflict["role_type"] == "coordinator"


def test_generic_executor_script_keeps_literal_kind_braces():
    text = build_executor_script(ARCHITECT, "青梧")
    assert "{kind:agent" in text
    assert "{kind:task" in text
    assert "{{kind" not in text
    assert "You are an EXECUTOR" in text


def test_generic_coordinator_script_builds():
    text = build_coordinator_script(ARCHITECT, "青梧")
    assert "You are a COORDINATOR" in text
    assert ARCHITECT in text
    assert "one `ask_agent` to HR" in text
    assert "Do not send a work letter plus a separate" in text
    assert "ASSIGNEE_MUST_SUBMIT" in text
    assert "park=delegated" in text


def test_ceo_hire_flow_is_one_ask_not_two_letters():
    text = build_coordinator_script("ceo", "归零")
    assert "You are the CEO" in text
    assert "ask_agent` to HR" in text
    assert "Two inbox items" in text
    assert "请回报招聘结果" in text
    assert (
        "Use `send_message` with recipients=[\"HR的花名\"] to send the hiring request"
        not in text
    )
    assert "message HR with specific hiring requests" not in text


def test_ceo_script_embeds_design_review_discipline():
    """设计稿审查纪律：Coverage + 提升 + 结论三选一 + 2 轮封顶；且不得退化为盖章机。"""
    text = build_coordinator_script("CEO", "归零")
    # 机制名与骨架
    assert "设计稿审查" in text
    assert "Coverage" in text
    assert "2 轮" in text
    # 「尽力提升」而非「必须提意见」
    assert "不是「必须提意见」" in text
    assert "在你能力范围内" in text
    # 「已达上限」必须附核对痕迹——防零成本放行（P0-2）
    assert "必须同时交出你实际核过什么" in text
    assert "不允许不做实际核对就整个放行" in text
    # 三种结论出口齐备（含「建议已提出，不强制本轮修改」）
    assert "已达当前合理上限" in text
    assert "建议修改后定稿" in text
    assert "建议已提出，不强制本轮修改" in text
    # 不得改变用户初衷
    assert "不能为了你自以为的提升而改变用户的初衷" in text


def test_ceo_design_review_terminology_is_gone():
    """旧术语「鞭策」/「Bottom-line」全量退场，避免双份口径漂移。"""
    for role in ("CEO", ARCHITECT):
        text = build_coordinator_script(role, "归零")
        assert "鞭策" not in text, role
        assert "Bottom-line" not in text, role


def test_ceo_script_requires_design_before_hiring():
    """设计先行：中层首次设计稿定稿前不得招人派活；Phase 0.5 走设计→审查→定稿→招人。"""
    text = build_coordinator_script("CEO", "归零")
    assert "design/plan document" in text
    assert "定稿前中层不得招人、不得派活" in text
    assert "Manager Design & Mobilization" in text


def test_generic_coordinator_is_designer_and_seamer():
    """中层职责收敛：设计文档 + 接缝；接缝时机自定；不再 player-coach 写骨架。"""
    text = build_coordinator_script(ARCHITECT, "青梧")
    assert "设计者 + 接缝工" in text
    assert "接缝时机你自定" in text
    assert "定稿前不得向 HR 招人、不得派活" in text
    assert "player-coach" not in text
    assert "自己写骨架" not in text
    # 接缝之外的实现必须下派；solo 单兵例外保留
    assert "接缝之外的实现必须派给下级 executor" in text
    assert "solo 单兵例外" in text
    # Phase 0.5 改为设计先行 + 分两段汇报
    assert "Domain Design" in text
    assert "待审查定稿" in text


def test_ceo_script_no_player_coach_leftover():
    """CEO 剧本也不残留 player-coach 语义（小 UI 由接缝带过，不是 tech lead 自己写）。"""
    text = build_coordinator_script("CEO", "归零")
    assert "player-coach" not in text
    assert "自己写骨架" not in text
    assert "由技术负责人写" not in text
    assert "以接缝方式带过" in text


def test_identity_prompt_no_player_coach_leftover():
    """共享 identity 块不残留 player-coach/骨架术语；worktree 描述含 seam work。"""
    for role in ("CEO", ARCHITECT, "HR", "前端工程师"):
        text = build_identity_prompt(
            role=role,
            role_type="coordinator" if role in ("CEO", ARCHITECT, "HR") else "executor",
            backstory="",
        )
        assert "player-coach" not in text, role
        assert "骨架任务" not in text, role
    coord_text = build_identity_prompt(role=ARCHITECT, role_type="coordinator", backstory="")
    assert "seam work" in coord_text


def test_identity_instruct_hr_uses_ask_agent():
    text = build_identity_prompt(
        role="ceo",
        role_type="coordinator",
        backstory="",
        name="归零",
        permission_type="coordinator",
    )
    assert "I will instruct HR" in text
    assert "call `ask_agent` to HR" in text
    assert "call `send_message` to HR in the same turn" not in text
    assert "One ask carries the work" in text
    assert "claimed ≠ idle" in text


def test_identity_hire_config_permission_type_only_uses_coordinator_script():
    """Live hire starts with permission_type, not the restart SQL role_type alias."""
    text = build_identity_prompt(
        role=ARCHITECT,
        role_type="",
        backstory="",
        name="青梧",
        permission_type="coordinator",
    )
    assert "You are a COORDINATOR" in text
    assert ARCHITECT in text
    assert "{kind:agent" in text
    assert "## Permission Level: coordinator" in text


def test_identity_executor_path_does_not_nameerror():
    text = build_identity_prompt(
        role="签到排行榜工程师",
        role_type="executor",
        backstory="",
        name="星野",
    )
    assert "You are an EXECUTOR" in text
    assert "{kind:agent" in text
    assert "{kind:task" in text
    assert "{{kind" not in text


def test_identity_permission_type_wins_over_stale_role_type():
    text = build_identity_prompt(
        role=ARCHITECT,
        role_type="executor",
        backstory="",
        name="青梧",
        permission_type="coordinator",
    )
    assert "You are a COORDINATOR" in text
    assert "You are an EXECUTOR" not in text


# ── 提示词纪律守卫（report TEST_DSH_54 #3 / #8-3，2026-09-12）──────────────
# 边界说明：这些断言只钉住「纪律文本在场且关键字句未被改回自由文本」，
# **不保证语义正确** —— 语义成立与否机器判不了，需人/LLM 复核。


def test_identity_known_issue_doc_provenance_rule_present():
    """#8-3：共享层「已知问题」文档的日期只是被观测时间，不是引入时间；
    再次撞上「已知旧问题」必须先核是否同一问题（git blame / git log -S），不能照抄文档结论。"""
    text = build_identity_prompt("开发工程师", "executor", "", name="阿蓝")
    assert "被观测到的时间" in text
    assert "不是引入时间" in text
    assert "先核它是不是同一个问题" in text
    assert "git blame" in text
    assert "git log -S" in text


def test_test_engineer_probe_positive_control_rule_present():
    """#3：项目自建探针须有正向对照（故障时会转红），且明示平台只给骨架模板与纪律、不给探针实现。"""
    script = build_executor_script("测试工程师", "鹿鸣")
    assert "探针自检" in script
    assert "故障时它会转红" in script
    assert "阳性样本" in script
    assert "阴性样本" in script
    assert "平台只给骨架模板与验收流程纪律，不给探针实现" in script


def test_generic_executor_probe_self_check_rule_present():
    script = build_executor_script("开发工程师", "阿蓝")
    assert "探针自检" in script
    assert "故障时它会转红" in script
    assert "平台只给骨架与纪律" in script
