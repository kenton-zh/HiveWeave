"""Default coding path is Hive tools (no nested second runtime)."""
import inspect

from hiveweave.prompts.coordinator import build_coordinator_script
from hiveweave.prompts.executor import build_executor_script
from hiveweave.prompts.identity import build_identity_prompt


def test_generic_executor_implements_with_hive_tools() -> None:
    text = build_executor_script("签到排行榜工程师", "流火")
    assert "run_dsh" not in text
    assert "write_file" in text
    assert "apply_patch" in text
    assert "spawn_subagent" in text
    assert "background=true" in text
    assert "Long coding: spawn_subagent" in text


def test_builder_coordinator_implements_in_own_worktree() -> None:
    text = build_coordinator_script("技术负责人", "潮汐")
    assert "run_dsh" not in text
    assert "edit_file" in text or "apply_patch" in text
    assert "git_worktree_checkpoint" in text
    assert "spawn_subagent" in text
    assert "background=true" in text
    assert "长实现用 spawn_subagent" in text


def test_ceo_does_not_dispatch_a_coding_runtime() -> None:
    text = build_coordinator_script("ceo", "归零")
    ident = build_identity_prompt("ceo", "coordinator", "", name="归零")
    assert "run_dsh" not in text
    assert "run_dsh" not in ident
    assert "spawn_subagent" not in text
    assert "background=true" not in text
    assert "不要告诉他们不能 read_file / write_file" in text


def test_hr_and_qa_scripts_stay_on_hive_tools() -> None:
    hr = build_coordinator_script("hr", "知远")
    assert "run_dsh" not in hr
    assert "spawn_subagent" not in hr
    assert "background=true" not in hr
    assert "run_dsh" not in build_executor_script("test_engineer", "探针")
    assert "run_dsh" not in build_executor_script("code_reviewer", "审")
    assert "run_dsh" not in build_identity_prompt(
        "test_engineer", "executor", "", name="探针"
    )
    assert "run_dsh" not in build_identity_prompt(
        "签到排行榜工程师", "executor", "", name="流火"
    )
    assert "run_dsh" not in build_identity_prompt(
        "技术负责人", "coordinator", "", name="潮汐"
    )


def test_assignee_hint_names_offturn_not_a_second_runtime() -> None:
    import hiveweave.services.turn_exit as turn_exit

    src = inspect.getsource(turn_exit)
    assert "DSH 进行中" not in src
    assert "off-turn 进行中" in src


def _llm_tool_text(name: str) -> str:
    from hiveweave.tools.executor import (
        get_tool_description,
        get_tool_schema_for_llm,
    )

    schema = get_tool_schema_for_llm(name)
    parts = [get_tool_description(name), schema.get("description") or ""]
    for prop in (schema.get("properties") or {}).values():
        parts.append(prop.get("description") or "")
    return "\n".join(parts)


def test_llm_tool_schemas_are_dsh_style_hive_wake() -> None:
    from hiveweave.tools.executor import get_tool_schema_for_llm

    bash = _llm_tool_text("bash")
    assert "fresh shell" in bash.lower()
    assert "Exit code" in bash
    assert "commit_turn" in bash
    assert "waiting_on" in bash
    assert "[BASH DONE]" in bash
    assert "[BASH FAILED]" in bash
    assert "job_output" not in bash
    assert "run_in_background" not in bash
    bash_schema = get_tool_schema_for_llm("bash")
    assert bash_schema.get("required") == ["command"]
    assert "description" not in (bash_schema.get("properties") or {})

    spawn = _llm_tool_text("spawn_subagent")
    assert "does not see this conversation" in spawn
    assert "[SUBAGENT DONE]" in spawn
    assert "commit_turn" in spawn
    assert "job_output" not in spawn

    edit = _llm_tool_text("edit_file")
    assert "exactly once" in edit
    assert "Must match exactly" in edit

    kill = _llm_tool_text("job_kill")
    assert "Returns immediately" in kill
    assert "job_output" not in kill


def test_offturn_scripts_tell_child_is_standalone() -> None:
    exec_text = build_executor_script("签到排行榜工程师", "流火")
    coord_text = build_coordinator_script("技术负责人", "潮汐")
    for text in (exec_text, coord_text):
        assert "does not see this conversation" in text
        assert "Check `Exit code:`" in text


def test_subagent_identity_forbids_background_bash() -> None:
    from hiveweave.tools.subagent import _subagent_identity

    class _Parent:
        id = "p"
        config = {"name": "流火", "role": "工程师"}

    text = _subagent_identity(_Parent(), "scout files", None, "readonly", None)
    assert "bash(background=true) is parent-only" in text


def test_bash_timeout_seconds_are_coerced_before_pydantic_min() -> None:
    from hiveweave.tools.bash import BashParams

    assert BashParams(command="true", timeout=30).timeout == 30_000
    assert BashParams(command="true", timeout=120000).timeout == 120_000


_BANNED_SCHEMA_PHRASES = (
    "use it to",
    "use this to",
    "use this tool",
    "job_output",
    "run_in_background",
    "also auto-set",
)


def test_every_registered_tool_has_dsh_style_schema() -> None:
    from hiveweave.tools import list_tool_names
    from hiveweave.tools.executor import (
        TOOL_PARAM_SCHEMAS,
        get_tool_description,
        get_tool_schema_for_llm,
    )

    missing = [n for n in list_tool_names() if n not in TOOL_PARAM_SCHEMAS]
    assert missing == [], missing

    lecture = []
    for name in list_tool_names():
        text = get_tool_description(name).lower()
        hits = [p for p in _BANNED_SCHEMA_PHRASES if p in text]
        if hits:
            lecture.append((name, hits))
    assert lecture == [], lecture

    for name in ("ask_agent", "send_message", "notify_agent"):
        props = get_tool_schema_for_llm(name).get("properties") or {}
        assert "replyTo" in props, name
        assert "job_output" not in get_tool_description(name)

    team = get_tool_schema_for_llm("message_team")
    assert list((team.get("properties") or {}).keys()) == ["message"]
    assert team.get("required") == ["message"]

    sub = get_tool_schema_for_llm("message_subordinate")
    assert "expectReport" not in (sub.get("properties") or {})
    assert "message" in (sub.get("required") or [])
    assert "recipient" not in (sub.get("required") or [])

    dispatch = get_tool_schema_for_llm("dispatch_task")
    assert "force" in (dispatch.get("properties") or {})

    submit = get_tool_schema_for_llm("submit_task")
    assert "failuresAcknowledged" in (submit.get("properties") or {})

    lookup = get_tool_schema_for_llm("lookup_dev_server")
    assert lookup.get("required") in (None, [])
    from hiveweave.tools.dev_server_tools import (
        LookupDevServerParams,
        StartDevServerParams,
    )

    assert LookupDevServerParams().preferred_port is None
    assert StartDevServerParams().preferred_port == 3000


async def test_run_ledger_elapsed_does_not_stop_a_live_run(monkeypatch) -> None:
    """Hour-long coding must not trip check_budget; call counts still can."""
    from hiveweave.services import run_ledger as rl

    captured: dict = {}
    row = {
        "actual_llm_calls": 1,
        "actual_tool_calls": 1,
        "started_at": 0,
        "budget_llm_calls": 50,
        "budget_tool_calls": 100,
        "budget_elapsed_ms": 600_000,
    }

    async def fake_query(_agent_id, sql, _params):
        captured["sql"] = sql
        return [row]

    monkeypatch.setattr(rl.project_db, "query", fake_query)
    exceeded, reason = await rl.RunLedger().check_budget("a", "run")
    assert exceeded is False
    assert reason == ""
    sql = captured["sql"]
    assert "started_at" not in sql
    assert "budget_elapsed_ms" not in sql

    row["actual_llm_calls"] = 50
    exceeded, reason = await rl.RunLedger().check_budget("a", "run")
    assert exceeded is True
    assert "llm_calls" in reason
