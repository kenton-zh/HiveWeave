"""Native off-turn jobs: spawn_subagent / bash(background=true) wait/wake/reap."""
from __future__ import annotations

import asyncio

import pytest

from hiveweave.agents.trigger import wake_source_for_pending
from hiveweave.services.inbox import (
    InboxService,
    should_exempt_from_park,
    should_spare_from_give_up_ack,
)
from hiveweave.services.offturn import (
    OFFTURN_STATE,
    agent_has_live_job_for_task,
    build_waiting_on,
    is_live_job,
    is_offturn_completion_text,
    job_bound_task_id,
    live_job_ids_for_agent,
    notify_completion,
    reap_offturn_for_task,
    reset_offturn_for_tests,
    start_offturn_job,
)
from hiveweave.services.wake_policy import (
    OFFTURN_COMPLETION_MESSAGE_TYPE,
    is_platform_reserved_inbox_identity,
)
from hiveweave.services.turn_exit import (
    ExitContext,
    assignee_must_submit,
    evaluate_turn_exit,
    waiting_on_live_external,
)
from hiveweave.services.turn_session import (
    clear_pending_turn_result,
    set_pending_turn_result,
)
from hiveweave.tools.bash import BashParams, bash_tool
from hiveweave.tools.executor import get_tool_schema_for_llm
from hiveweave.tools.subagent import SpawnSubagentParams, spawn_subagent_tool


TID = "aaaaaaaa-1111-2222-3333-444444444444"
TID2 = "bbbbbbbb-1111-2222-3333-444444444444"


@pytest.fixture
async def clean_offturn(monkeypatch: pytest.MonkeyPatch):
    async def noop_send(self, *args, **kwargs):
        return {"id": "m0", "should_wake": False}

    async def noop_n(*args, **kwargs):
        return 0

    async def noop_trigger(*args, **kwargs):
        return None

    async def fake_project(*_a, **_k):
        return "proj-1"

    monkeypatch.setattr(
        "hiveweave.services.inbox.InboxService.send_message", noop_send
    )
    from hiveweave.services.wait_contract import wait_contract_service

    monkeypatch.setattr(wait_contract_service, "clear_waits", noop_n)
    monkeypatch.setattr(
        wait_contract_service, "clear_waits_matching_ref", noop_n
    )
    monkeypatch.setattr("hiveweave.db.meta.get_agent_project_id", fake_project)
    monkeypatch.setattr(
        "hiveweave.agents.trigger.trigger_subordinate", noop_trigger
    )
    await reset_offturn_for_tests()
    yield
    await reset_offturn_for_tests()


def _patch_completion(monkeypatch: pytest.MonkeyPatch):
    inbox: list[str] = []
    cleared: list[tuple] = []

    async def fake_send(self, *args, **kwargs):
        msg = kwargs.get("message")
        if msg is None and len(args) >= 3:
            msg = args[2]
        inbox.append(str(msg or ""))
        return {"id": "m1", "should_wake": False}

    async def fake_clear_ref(project_id, agent_id, ref):
        cleared.append((project_id, agent_id, ref))
        return 1

    async def fake_clear(project_id, agent_id):
        cleared.append((project_id, agent_id, "*"))
        return 1

    async def fake_project(agent_id: str):
        return "proj-1"

    async def fake_trigger(agent_id: str):
        return None

    monkeypatch.setattr(
        "hiveweave.services.inbox.InboxService.send_message", fake_send
    )
    from hiveweave.services.wait_contract import wait_contract_service

    monkeypatch.setattr(
        wait_contract_service, "clear_waits_matching_ref", fake_clear_ref
    )
    monkeypatch.setattr(wait_contract_service, "clear_waits", fake_clear)
    monkeypatch.setattr("hiveweave.db.meta.get_agent_project_id", fake_project)
    monkeypatch.setattr(
        "hiveweave.agents.trigger.trigger_subordinate", fake_trigger
    )
    from hiveweave.agents.supervisor import agent_manager

    monkeypatch.setattr(agent_manager, "get_agent", lambda _aid: None)
    return inbox, cleared


class _FakeParent:
    def __init__(self) -> None:
        self.id = "agent-exec"
        self.project_id = "proj-1"
        self.config = {
            "name": "流火",
            "role": "签到排行榜工程师",
            "permission_type": "executor",
        }
        self.extended: list[float] = []

    def _extend_safety_timer(self, extra_s: float) -> None:
        self.extended.append(extra_s)


@pytest.mark.asyncio
async def test_spawn_returns_before_subagent_finishes(
    monkeypatch: pytest.MonkeyPatch, clean_offturn
) -> None:
    release = asyncio.Event()
    started = asyncio.Event()
    parent = _FakeParent()

    async def fake_run(*_a, **_k):
        started.set()
        await release.wait()
        return {"status": "ok", "content": "scouted"}

    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent",
        lambda _aid: parent,
    )
    monkeypatch.setattr("hiveweave.tools.subagent._run_subagent", fake_run)

    result = await spawn_subagent_tool(
        SpawnSubagentParams(subagent_type="readonly", prompt="scout files"),
        "agent-exec",
        "/tmp/ws",
    )
    assert result.success
    assert "waiting" in result.output.lower()
    job_id = result.extra["job_id"]
    assert job_id.startswith("bg-sub-")
    assert result.extra["waiting_on"][0] == {"kind": "external", "ref": job_id}
    assert parent.extended == []
    assert is_live_job(job_id)
    for _ in range(50):
        if started.is_set():
            break
        await asyncio.sleep(0.02)
    assert started.is_set()
    assert is_live_job(job_id)
    release.set()
    for _ in range(50):
        if not is_live_job(job_id):
            break
        await asyncio.sleep(0.02)
    assert not is_live_job(job_id)


@pytest.mark.asyncio
async def test_waiting_on_live_job_parks_assignee_must_submit(
    clean_offturn,
) -> None:
    gate = asyncio.Event()

    async def hang():
        await gate.wait()
        return True, "never"

    job_id = start_offturn_job(
        kind="subagent",
        agent_id="agent-exec",
        project_id="proj-1",
        work=hang,
        task_id=TID,
    )
    waiting = [{"kind": "external", "ref": job_id}]
    assert waiting_on_live_external(waiting)
    assert job_bound_task_id(job_id, agent_id="agent-exec") == TID
    assert agent_has_live_job_for_task("agent-exec", TID)
    assert not assignee_must_submit(
        "waiting", [TID], waiting, agent_id="agent-exec"
    )
    assert assignee_must_submit("done_slice", [TID], waiting)
    gate.set()


def test_fake_external_still_rejected() -> None:
    waiting = [{"kind": "external", "ref": "bg-sub-ghost"}]
    assert not waiting_on_live_external(waiting)
    assert assignee_must_submit("waiting", [TID], waiting)


@pytest.mark.asyncio
async def test_evaluate_turn_exit_parks_live_offturn(clean_offturn) -> None:
    gate = asyncio.Event()

    async def hang():
        await gate.wait()
        return True, "never"

    agent_id = "exec-offturn-park"
    job_id = start_offturn_job(
        kind="bash",
        agent_id=agent_id,
        project_id="proj-1",
        work=hang,
        task_id=TID,
    )
    set_pending_turn_result(
        agent_id,
        {
            "phase": "waiting",
            "summary": "bg bash in flight",
            "waiting_on": [{"kind": "external", "ref": job_id}],
        },
    )
    try:
        decision = evaluate_turn_exit(
            ExitContext(
                agent_id=agent_id,
                project_id="proj-1",
                tool_calls=[],
                open_task_obligations=[
                    {"id": TID, "role_hint": "assignee", "status": "running"}
                ],
            )
        )
        assert "ASSIGNEE_MUST_SUBMIT" not in decision.violations
        assert decision.ok
    finally:
        clear_pending_turn_result(agent_id)
        gate.set()


@pytest.mark.asyncio
async def test_evaluate_turn_exit_rejects_fake_offturn_wait() -> None:
    agent_id = "exec-offturn-fake"
    set_pending_turn_result(
        agent_id,
        {
            "phase": "waiting",
            "summary": "pretend bg job",
            "waiting_on": [{"kind": "external", "ref": "bg-bash-ghost"}],
        },
    )
    try:
        decision = evaluate_turn_exit(
            ExitContext(
                agent_id=agent_id,
                project_id="proj-1",
                tool_calls=[],
                open_task_obligations=[
                    {"id": TID, "role_hint": "assignee", "status": "running"}
                ],
            )
        )
        assert "ASSIGNEE_MUST_SUBMIT" in decision.violations
        assert not decision.ok
    finally:
        clear_pending_turn_result(agent_id)


@pytest.mark.asyncio
async def test_evaluate_turn_exit_live_offturn_does_not_skip_reviewer(
    clean_offturn,
) -> None:
    gate = asyncio.Event()

    async def hang():
        await gate.wait()
        return True, "never"

    agent_id = "exec-offturn-reviewer"
    job_id = start_offturn_job(
        kind="bash",
        agent_id=agent_id,
        project_id="proj-1",
        work=hang,
    )
    set_pending_turn_result(
        agent_id,
        {
            "phase": "waiting",
            "summary": "bg job running but review still due",
            "waiting_on": [{"kind": "external", "ref": job_id}],
        },
    )
    try:
        decision = evaluate_turn_exit(
            ExitContext(
                agent_id=agent_id,
                project_id="proj-1",
                tool_calls=[],
                open_task_obligations=[
                    {
                        "id": "dddddddd-1111-2222-3333-444444444444",
                        "role_hint": "reviewer",
                        "status": "submitted",
                    }
                ],
            )
        )
        assert "REVIEWER_MUST_START_REVIEW" in decision.violations
        assert not decision.ok
    finally:
        clear_pending_turn_result(agent_id)
        gate.set()


def test_offturn_redact_strips_secrets() -> None:
    from hiveweave.services.offturn import _redact

    text = _redact("done API_KEY=sk-abc123456789 token=secret")
    assert "sk-abc123456789" not in text
    assert "API_KEY=***" in text
    bearer = _redact("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc")
    assert "eyJhbGciOiJIUzI1NiJ9.abc" not in bearer
    assert "Authorization: Bearer ***" in bearer or "Authorization: ***" in bearer


def test_hw_ref_is_not_unbounded_external() -> None:
    from hiveweave.services.wait_contract import looks_unbounded_external

    assert not looks_unbounded_external("external", "hw-abc")
    assert looks_unbounded_external("external", "bg-bash-x")
    assert looks_unbounded_external("external", "bg-sub-x")


@pytest.mark.asyncio
async def test_bash_background_returns_immediately(
    monkeypatch: pytest.MonkeyPatch, clean_offturn
) -> None:
    release = asyncio.Event()
    started = asyncio.Event()

    async def fake_exec(**_k):
        started.set()
        await release.wait()
        return {
            "success": True,
            "output": "ok",
            "error": None,
            "exit_code": 0,
        }

    async def fake_pid(_agent_id: str):
        return "proj-1"

    monkeypatch.setattr("hiveweave.tools.bash.execute_bash", fake_exec)
    monkeypatch.setattr(
        "hiveweave.tools.bash._validate_command_safety",
        lambda _c: (False, ""),
    )
    monkeypatch.setattr(
        "hiveweave.tools.bash._detect_dev_server_command", lambda _c: None
    )
    monkeypatch.setattr("hiveweave.tools.helpers.get_project_id", fake_pid)
    monkeypatch.setattr(
        "hiveweave.services.process_registry.prepare_spawn_command",
        lambda cmd, project_id=None, routed_as_dev_server=False: (cmd, {}, None, None),
    )

    result = await bash_tool(
        BashParams(command="echo hi", background=True),
        "agent-exec",
        "/tmp/ws",
    )
    assert result.success
    job_id = result.extra["job_id"]
    assert job_id.startswith("bg-bash-")
    assert result.extra["waiting_on"][0]["ref"] == job_id
    assert "waiting" in result.output.lower()
    assert is_live_job(job_id)
    for _ in range(50):
        if started.is_set():
            break
        await asyncio.sleep(0.02)
    assert started.is_set()
    release.set()


@pytest.mark.asyncio
async def test_bash_default_stays_foreground(
    monkeypatch: pytest.MonkeyPatch, clean_offturn
) -> None:
    called = {"n": 0}

    async def fake_exec(**_k):
        called["n"] += 1
        return {
            "success": True,
            "output": "hi",
            "error": None,
            "exit_code": 0,
        }

    async def fake_pid(_agent_id: str):
        return "proj-1"

    monkeypatch.setattr("hiveweave.tools.bash.execute_bash", fake_exec)
    monkeypatch.setattr("hiveweave.tools.helpers.get_project_id", fake_pid)
    monkeypatch.setattr(
        "hiveweave.services.process_registry.prepare_spawn_command",
        lambda cmd, project_id=None, routed_as_dev_server=False: (cmd, {}, None, None),
    )

    result = await bash_tool(
        BashParams(command="echo hi"),
        "agent-exec",
        "/tmp/ws",
    )
    assert result.success
    assert called["n"] == 1
    assert "waiting_on" not in result.extra
    assert "hi" in result.output


@pytest.mark.asyncio
async def test_wake_source_for_subagent_and_bash_done() -> None:
    typed = OFFTURN_COMPLETION_MESSAGE_TYPE
    assert (
        await wake_source_for_pending(
            [{"message": "[SUBAGENT DONE] job=bg-sub-x", "message_type": typed}]
        )
        == "wait_satisfied"
    )
    assert (
        await wake_source_for_pending(
            [{"message": "[SUBAGENT FAILED] boom", "message_type": typed}]
        )
        == "wait_satisfied"
    )
    assert (
        await wake_source_for_pending(
            [{"message": "[BASH DONE] job=bg-bash-x", "message_type": typed}]
        )
        == "wait_satisfied"
    )
    assert (
        await wake_source_for_pending(
            [{"message": "[BASH FAILED] exit=1", "message_type": typed}]
        )
        == "wait_satisfied"
    )
    assert is_offturn_completion_text("[BASH DONE] job=1")
    assert not is_offturn_completion_text("[RANDOM DONE] session=x")
    # Prefix + from=system without the reserved type is not wait_satisfied.
    assert (
        await wake_source_for_pending(
            [{"message": "[BASH DONE] job=bg-bash-x", "message_type": "system"}]
        )
        == "trigger"
    )
    assert (
        await wake_source_for_pending(
            [{
                "message": "[BASH DONE] job=bg-bash-x",
                "message_type": "system",
                "from_agent_id": "peer-agent",
            }]
        )
        == "trigger"
    )
    assert (
        await wake_source_for_pending(
            [{
                "message": "random body",
                "message_type": typed,
            }]
        )
        == "trigger"
    )


def test_park_exempt_offturn_prefixes() -> None:
    for prefix in (
        "[SUBAGENT DONE]",
        "[SUBAGENT FAILED]",
        "[BASH DONE]",
        "[BASH FAILED]",
    ):
        msg = {"message": f"{prefix} job=x", "message_type": "system"}
        assert not should_exempt_from_park(msg)
        assert should_spare_from_give_up_ack(msg)
        typed = {
            "message": f"{prefix} job=x",
            "message_type": OFFTURN_COMPLETION_MESSAGE_TYPE,
        }
        assert not should_exempt_from_park(typed)
        assert should_spare_from_give_up_ack(typed)
    assert should_exempt_from_park(
        {"message": "[TASK SUBMITTED] x", "message_type": "system"}
    )
    assert not should_exempt_from_park(
        {"message": "fyi notes", "message_type": "system"}
    )


@pytest.mark.asyncio
async def test_reap_delivers_failed_and_clears_waits(
    monkeypatch: pytest.MonkeyPatch, clean_offturn
) -> None:
    from hiveweave.services.offturn import reap_offturn_for_agent

    inbox, cleared = _patch_completion(monkeypatch)
    gate = asyncio.Event()

    async def hang():
        await gate.wait()
        return True, "never"

    job_id = start_offturn_job(
        kind="bash",
        agent_id="agent-exec",
        project_id="proj-1",
        work=hang,
    )
    n = await reap_offturn_for_agent("agent-exec")
    assert n == 1
    for _ in range(50):
        if any(m.startswith("[BASH FAILED]") for m in inbox):
            break
        await asyncio.sleep(0.02)
    assert any(m.startswith("[BASH FAILED]") for m in inbox)
    assert any(c[0] == "proj-1" and c[1] == "agent-exec" for c in cleared)
    assert not is_live_job(job_id)


def test_schemas_expose_background_and_subagent_type() -> None:
    spawn = get_tool_schema_for_llm("spawn_subagent")
    assert "subagent_type" in spawn["properties"]
    assert "subagent_type" in spawn["required"]
    bash = get_tool_schema_for_llm("bash")
    assert "background" in bash["properties"]
    assert bash["properties"]["background"]["type"] == "boolean"


@pytest.mark.asyncio
async def test_sibling_job_does_not_wipe_other_wait(
    monkeypatch: pytest.MonkeyPatch, clean_offturn
) -> None:
    """Job A DONE must not clear_waits=* while job B is still live."""
    from hiveweave.agents.types import AgentState
    from hiveweave.services.offturn import has_live_jobs_for_agent

    inbox, cleared = _patch_completion(monkeypatch)
    wakes: list[dict] = []

    class _BusyAgent:
        status = AgentState.PROCESSING
        project_id = "proj-1"

        async def enqueue_wake(self, _message, opts=None):
            wakes.append(dict(opts or {}))

    busy = _BusyAgent()
    from hiveweave.agents.supervisor import agent_manager

    monkeypatch.setattr(agent_manager, "get_agent", lambda _aid: busy)

    gate_a = asyncio.Event()
    gate_b = asyncio.Event()

    async def work_a():
        await gate_a.wait()
        return True, "a-done"

    async def work_b():
        await gate_b.wait()
        return True, "b-done"

    job_a = start_offturn_job(
        kind="subagent",
        agent_id="agent-exec",
        project_id="proj-1",
        work=work_a,
    )
    job_b = start_offturn_job(
        kind="bash",
        agent_id="agent-exec",
        project_id="proj-1",
        work=work_b,
    )
    gate_a.set()
    for _ in range(50):
        if not is_live_job(job_a):
            break
        await asyncio.sleep(0.02)
    assert not is_live_job(job_a)
    assert is_live_job(job_b)
    assert has_live_jobs_for_agent("agent-exec")
    assert any(c == ("proj-1", "agent-exec", job_a) for c in cleared)
    assert not any(c[2] == "*" for c in cleared)
    assert wakes
    assert wakes[0].get("clear_waits") is False
    assert wakes[0].get("inbox_msg_ids") == ["m1"]
    assert any(m.startswith("[SUBAGENT DONE]") for m in inbox)
    gate_b.set()
    for _ in range(50):
        if not is_live_job(job_b):
            break
        await asyncio.sleep(0.02)
    assert any(c == ("proj-1", "agent-exec", job_b) for c in cleared)
    assert not any(c[2] == "*" for c in cleared)


def test_trigger_sibling_guard_sets_clear_waits_false() -> None:
    from hiveweave.agents.trigger import _guard_sibling_offturn_waits
    from hiveweave.services.offturn import _JOBS, OffturnJob

    class _DummyTask:
        def done(self) -> bool:
            return False

    _JOBS["bg-sub-live"] = OffturnJob(
        job_id="bg-sub-live",
        kind="subagent",
        agent_id="agent-exec",
        project_id="proj-1",
        worktree="",
        task=_DummyTask(),  # type: ignore[arg-type]
    )
    try:
        opts = {"trigger": True, "source": "wait_satisfied"}
        _guard_sibling_offturn_waits("agent-exec", opts)
        assert opts.get("clear_waits") is False
        opts2 = {"trigger": True, "source": "wait_satisfied"}
        _guard_sibling_offturn_waits("other-agent", opts2)
        assert opts2.get("clear_waits") is False
        opts3 = {"trigger": True, "source": "trigger"}
        _guard_sibling_offturn_waits("agent-exec", opts3)
        assert "clear_waits" not in opts3
    finally:
        _JOBS.pop("bg-sub-live", None)


@pytest.mark.asyncio
async def test_foreign_job_does_not_park_assignee(clean_offturn) -> None:
    gate = asyncio.Event()

    async def hang():
        await gate.wait()
        return True, "never"

    job_id = start_offturn_job(
        kind="bash",
        agent_id="agent-a",
        project_id="proj-1",
        work=hang,
        task_id=TID,
    )
    waiting = [{"kind": "external", "ref": job_id}]
    assert waiting_on_live_external(waiting, agent_id="agent-a")
    assert not waiting_on_live_external(waiting, agent_id="agent-b")
    assert assignee_must_submit(
        "waiting", [TID], waiting, agent_id="agent-b"
    )
    assert not assignee_must_submit(
        "waiting", [TID], waiting, agent_id="agent-a"
    )
    gate.set()


def test_cancel_reason_gates_offturn_reap() -> None:
    from hiveweave.services.offturn import cancel_should_reap_offturn

    assert cancel_should_reap_offturn("cancelled")
    assert cancel_should_reap_offturn("off_duty")
    assert cancel_should_reap_offturn("stop_agent")
    assert not cancel_should_reap_offturn("busy_reset")
    assert not cancel_should_reap_offturn("reset_processing")


@pytest.mark.asyncio
async def test_notify_completion_busy_enqueues_only(
    monkeypatch: pytest.MonkeyPatch, clean_offturn
) -> None:
    from hiveweave.agents.types import AgentState

    enqueued: list[dict] = []
    chats: list[dict] = []
    triggers: list[str] = []

    class Fake:
        status = AgentState.PROCESSING

        async def enqueue_wake(self, _message: str, opts: dict | None = None):
            enqueued.append(dict(opts or {}))

        async def chat(self, _message: str, opts: dict | None = None):
            chats.append(dict(opts or {}))

    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent",
        lambda _aid: Fake(),
    )
    monkeypatch.setattr(
        "hiveweave.agents.trigger.trigger_subordinate",
        lambda aid: triggers.append(aid) or asyncio.sleep(0),
    )
    await notify_completion(
        "agent-exec", "[BASH DONE] x", wake=True, clear_ref="bg-bash-1"
    )
    assert enqueued and not chats and not triggers
    assert enqueued[0].get("source") == "wait_satisfied"
    assert enqueued[0].get("clear_waits") is False


@pytest.mark.asyncio
async def test_notify_completion_busy_to_idle_chats_once(
    monkeypatch: pytest.MonkeyPatch, clean_offturn
) -> None:
    from hiveweave.agents.types import AgentState

    enqueued: list[dict] = []
    chats: list[dict] = []
    triggers: list[str] = []

    class Fake:
        def __init__(self) -> None:
            self.status = AgentState.PROCESSING

        async def enqueue_wake(self, _message: str, opts: dict | None = None):
            enqueued.append(dict(opts or {}))

        async def chat(self, _message: str, opts: dict | None = None):
            chats.append(dict(opts or {}))

    fake = Fake()

    async def send_and_idle(self, *args, **kwargs):
        fake.status = AgentState.IDLE
        return {"id": "m-flip", "should_wake": False}

    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent",
        lambda _aid: fake,
    )
    monkeypatch.setattr(
        "hiveweave.services.inbox.InboxService.send_message", send_and_idle
    )
    monkeypatch.setattr(
        "hiveweave.agents.trigger.trigger_subordinate",
        lambda aid: triggers.append(aid) or asyncio.sleep(0),
    )
    await notify_completion(
        "agent-exec", "[BASH DONE] x", wake=True, clear_ref="bg-bash-1"
    )
    assert chats and not enqueued and not triggers
    assert chats[0].get("inbox_msg_ids") == ["m-flip"]


@pytest.mark.asyncio
async def test_reap_offturn_for_task_skips_unbound(clean_offturn) -> None:
    gate = asyncio.Event()

    async def hang():
        await gate.wait()
        return True, "x"

    unbound = start_offturn_job(
        kind="bash",
        agent_id="agent-exec",
        project_id="proj-1",
        work=hang,
        task_id=None,
    )
    bound = start_offturn_job(
        kind="bash",
        agent_id="agent-exec",
        project_id="proj-1",
        work=hang,
        task_id=TID,
    )
    other = start_offturn_job(
        kind="subagent",
        agent_id="agent-exec",
        project_id="proj-1",
        work=hang,
        task_id=TID2,
    )
    n = await reap_offturn_for_task("agent-exec", TID)
    assert n == 1
    assert not is_live_job(bound)
    assert is_live_job(unbound)
    assert is_live_job(other)
    gate.set()


def test_merge_queued_triggers_unions_wait_satisfied() -> None:
    from hiveweave.agents.trigger import merge_queued_triggers

    msg, opts = merge_queued_triggers(
        [
            (
                "[BASH DONE] job=bg-bash-aaa",
                {
                    "trigger": True,
                    "source": "wait_satisfied",
                    "inbox_msg_ids": ["m1"],
                    "clear_waits": False,
                },
                1,
            ),
            (
                "[SUBAGENT DONE] job=bg-sub-bbb",
                {
                    "trigger": True,
                    "source": "wait_satisfied",
                    "inbox_msg_ids": ["m2"],
                    "clear_waits": False,
                },
                2,
            ),
        ]
    )
    assert "[BASH DONE]" in msg
    assert "[SUBAGENT DONE]" in msg
    assert opts["inbox_msg_ids"] == ["m1", "m2"]
    assert opts["source"] == "wait_satisfied"
    assert opts["clear_waits"] is False

    last_only, last_opts = merge_queued_triggers(
        [
            ("first digest", {"trigger": True, "inbox_msg_ids": ["a"]}, 1),
            ("second digest", {"trigger": True, "inbox_msg_ids": ["b"]}, 2),
        ]
    )
    assert last_only == "second digest"
    assert last_opts["inbox_msg_ids"] == ["b"]


@pytest.mark.asyncio
async def test_job_still_live_during_deliver(
    monkeypatch: pytest.MonkeyPatch, clean_offturn
) -> None:
    from hiveweave.services import offturn as ot

    live_during: list[bool] = []

    async def slow_deliver(agent_id, job_id, body, *, ok, wake=True, kind=None):
        live_during.append(ot.is_live_job(job_id))
        await asyncio.sleep(0.05)

    monkeypatch.setattr(ot, "deliver", slow_deliver)

    async def work():
        return True, "done"

    jid = start_offturn_job(
        kind="bash",
        agent_id="agent-1",
        project_id="proj-1",
        work=work,
        wake_on_complete=False,
    )
    for _ in range(80):
        job = ot._JOBS.get(jid)
        if job is None or job.task.done():
            break
        await asyncio.sleep(0.05)
    assert live_during and live_during[0] is True


@pytest.mark.asyncio
async def test_await_even_if_cancelled_joins_inner() -> None:
    from hiveweave.services.offturn import await_even_if_cancelled

    finished: list[str] = []

    async def slow() -> str:
        await asyncio.sleep(0.15)
        finished.append("ok")
        return "ok"

    async def wrapper() -> None:
        await await_even_if_cancelled(slow())

    task = asyncio.create_task(wrapper())
    await asyncio.sleep(0.03)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished == ["ok"]


@pytest.mark.asyncio
async def test_cancel_during_done_deliver_does_not_send_failed(
    monkeypatch: pytest.MonkeyPatch, clean_offturn
) -> None:
    from hiveweave.services import offturn as ot

    inbox, _cleared = _patch_completion(monkeypatch)
    entered = asyncio.Event()
    orig = ot.deliver

    async def slow_deliver(*args, **kwargs):
        if kwargs.get("ok"):
            entered.set()
        await asyncio.sleep(0.2)
        return await orig(*args, **kwargs)

    monkeypatch.setattr(ot, "deliver", slow_deliver)

    async def work():
        return True, "slice done"

    job_id = start_offturn_job(
        kind="subagent",
        agent_id="agent-exec",
        project_id="proj-1",
        work=work,
    )
    job = ot._JOBS[job_id].task
    await asyncio.wait_for(entered.wait(), timeout=3)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert any(m.startswith("[SUBAGENT DONE]") for m in inbox)
    assert not any(m.startswith("[SUBAGENT FAILED]") for m in inbox)
    assert not is_live_job(job_id)


@pytest.mark.asyncio
async def test_unbound_live_job_does_not_park_assignee(clean_offturn) -> None:
    gate = asyncio.Event()

    async def hang():
        await gate.wait()
        return True, "never"

    job_id = start_offturn_job(
        kind="bash",
        agent_id="agent-exec",
        project_id="proj-1",
        work=hang,
    )
    waiting = [{"kind": "external", "ref": job_id}]
    assert waiting_on_live_external(waiting, agent_id="agent-exec")
    assert job_bound_task_id(job_id, agent_id="agent-exec") is None
    assert assignee_must_submit(
        "waiting", [TID], waiting, agent_id="agent-exec"
    )
    gate.set()


@pytest.mark.asyncio
async def test_live_job_for_other_task_does_not_park(clean_offturn) -> None:
    gate = asyncio.Event()

    async def hang():
        await gate.wait()
        return True, "never"

    job_id = start_offturn_job(
        kind="bash",
        agent_id="agent-exec",
        project_id="proj-1",
        work=hang,
        task_id=TID,
    )
    waiting = [{"kind": "external", "ref": job_id}]
    assert not assignee_must_submit(
        "waiting", [TID], waiting, agent_id="agent-exec"
    )
    assert assignee_must_submit(
        "waiting", [TID2], waiting, agent_id="agent-exec"
    )
    assert assignee_must_submit(
        "waiting", [TID, TID2], waiting, agent_id="agent-exec"
    )
    gate.set()


@pytest.mark.asyncio
async def test_build_waiting_on_includes_sibling_jobs(clean_offturn) -> None:
    gate = asyncio.Event()

    async def hang():
        await gate.wait()
        return True, "x"

    first = start_offturn_job(
        kind="bash",
        agent_id="agent-exec",
        project_id="proj-1",
        work=hang,
        task_id=TID,
    )
    second = start_offturn_job(
        kind="subagent",
        agent_id="agent-exec",
        project_id="proj-1",
        work=hang,
        task_id=TID2,
    )
    items = build_waiting_on(second, TID2, agent_id="agent-exec")
    refs = {(it["kind"], it["ref"]) for it in items}
    assert ("external", second) in refs
    assert ("external", first) in refs
    assert ("task", TID2) in refs
    assert ("task", TID) in refs
    assert items[0] == {"kind": "external", "ref": second}
    assert set(live_job_ids_for_agent("agent-exec")) == {first, second}
    gate.set()


@pytest.mark.asyncio
async def test_replace_waits_preserves_omitted_live_job(
    clean_offturn, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hiveweave.services.wait_contract import WaitContractService

    gate = asyncio.Event()

    async def hang():
        await gate.wait()
        return True, "x"

    live = start_offturn_job(
        kind="bash",
        agent_id="agent-a",
        project_id="proj",
        work=hang,
        task_id=TID,
    )
    tx: list = []

    async def fake_tx(_pid, statements):
        tx.extend(statements)

    async def fake_ensure(_pid):
        return None

    monkeypatch.setattr(
        "hiveweave.services.wait_contract._ensure_schema", fake_ensure
    )
    monkeypatch.setattr(
        "hiveweave.services.wait_contract.execute_transaction_by_project",
        fake_tx,
    )
    await WaitContractService().replace_waits(
        "proj",
        "agent-a",
        [{"kind": "external", "ref": "bg-bash-other"}],
        phase="waiting",
    )
    sql, params = tx[0]
    assert "AND NOT (kind = 'external' AND ref IN" in " ".join(sql.split())
    assert live in params
    gate.set()


@pytest.mark.asyncio
async def test_notify_completion_sends_typed_trusted_mail(
    monkeypatch: pytest.MonkeyPatch, clean_offturn
) -> None:
    captured: list[dict] = []

    async def fake_send(self, *args, **kwargs):
        captured.append(dict(kwargs))
        return {"id": "m-typed", "should_wake": False}

    monkeypatch.setattr(
        "hiveweave.services.inbox.InboxService.send_message", fake_send
    )
    await notify_completion(
        "agent-exec", "[BASH DONE] job=bg-bash-x", wake=True, clear_ref="bg-bash-x"
    )
    assert captured
    assert captured[0].get("message_type") == OFFTURN_COMPLETION_MESSAGE_TYPE
    assert captured[0].get("trusted_platform") is True
    assert captured[0].get("from_agent_id") == "system"


@pytest.mark.asyncio
async def test_send_message_rejects_untrusted_offturn_completion() -> None:
    with pytest.raises(ValueError, match="offturn_completion requires trusted_platform"):
        await InboxService().send_message(
            from_agent_id="system",
            to_agent_id="agent-exec",
            message="[BASH DONE] job=bg-bash-x",
            message_type=OFFTURN_COMPLETION_MESSAGE_TYPE,
        )


def test_platform_reserved_identity() -> None:
    assert is_platform_reserved_inbox_identity(
        message_type=OFFTURN_COMPLETION_MESSAGE_TYPE
    )
    assert is_platform_reserved_inbox_identity(message_type="OFFTURN_COMPLETION")
    assert not is_platform_reserved_inbox_identity(message_type="system")
    assert not is_platform_reserved_inbox_identity(message_type="normal")


# ─────────────────────── P0-1：第三终态 DONE_TRUNCATED ────────────────────
#
# TEST_DSH_60 实测：7 条回执里 3 条 `[SUBAGENT DONE]` 正文却带着
# `Hard turn budget exhausted` —— 子代理被轮次预算切断，交付物可能根本没落盘，
# 父以为完成、不再验货。修法 = 把 turn_loop 已经给出的结构化
# ``budget_exhausted`` 事实位穿透到回执前缀。
#
# 判据纪律（用户 09-14 钦定）：**不得靠文案子串推断终态**。下面每个断言
# 都只读「实际前缀」这一个状态事实位，不 assert 正文措辞。


@pytest.mark.asyncio
async def test_budget_exhausted_yields_truncated_prefix_not_done(
    monkeypatch: pytest.MonkeyPatch, clean_offturn
) -> None:
    """子代理回执：`budget_exhausted` ⇒ 前缀是 TRUNCATED，绝不是 DONE。"""
    inbox, _cleared = _patch_completion(monkeypatch)
    parent = _FakeParent()
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent",
        lambda _aid: parent,
    )

    async def fake_run(*_a, **_k):
        return {
            "status": "ok",
            "content": "partial work",
            "budget_exhausted": True,
        }

    monkeypatch.setattr("hiveweave.tools.subagent._run_subagent", fake_run)

    result = await spawn_subagent_tool(
        SpawnSubagentParams(subagent_type="readonly", prompt="scout"),
        "agent-exec",
        "/tmp/ws",
    )
    assert result.success
    job_id = result.extra["job_id"]
    for _ in range(80):
        if not is_live_job(job_id):
            break
        await asyncio.sleep(0.02)
    assert inbox, "completion receipt must be delivered"
    head = inbox[0].lstrip()
    assert head.startswith("[SUBAGENT DONE_TRUNCATED]"), head[:60]
    # 反向：不得同时被当成 DONE 或 FAILED
    assert not head.startswith("[SUBAGENT DONE]")
    assert not head.startswith("[SUBAGENT FAILED]")


@pytest.mark.asyncio
async def test_normal_subagent_still_receives_plain_done(
    monkeypatch: pytest.MonkeyPatch, clean_offturn
) -> None:
    """阴性对照：没被切断时前缀仍是纯 DONE（防守卫恒绿/过度泛化）。"""
    inbox, _cleared = _patch_completion(monkeypatch)
    parent = _FakeParent()
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent",
        lambda _aid: parent,
    )

    async def fake_run(*_a, **_k):
        return {"status": "ok", "content": "scout report"}

    monkeypatch.setattr("hiveweave.tools.subagent._run_subagent", fake_run)

    result = await spawn_subagent_tool(
        SpawnSubagentParams(subagent_type="readonly", prompt="scout"),
        "agent-exec",
        "/tmp/ws",
    )
    job_id = result.extra["job_id"]
    for _ in range(80):
        if not is_live_job(job_id):
            break
        await asyncio.sleep(0.02)
    assert inbox
    head = inbox[0].lstrip()
    assert head.startswith("[SUBAGENT DONE]"), head[:60]
    assert not head.startswith("[SUBAGENT DONE_TRUNCATED]")


@pytest.mark.asyncio
async def test_truncated_receipt_still_satisfies_parent_wait(
    clean_offturn,
) -> None:
    """TRUNCATED **也是回执** ⇒ 父的 kind=agent wait 必须被满足。

    它读作「干过但没收尾」，不是失败。若它不被 is_offturn_completion_text
    识别，父会留在 wait 上等一个永不再来的唤醒（静默停泊）。
    """
    body = "[SUBAGENT DONE_TRUNCATED] job=bg-sub-x\npartial"
    assert is_offturn_completion_text(body)
    assert (
        await wake_source_for_pending(
            [{"message": body, "message_type": OFFTURN_COMPLETION_MESSAGE_TYPE}]
        )
        == "wait_satisfied"
    )


def test_truncated_prefix_park_and_ack_semantics_match_done() -> None:
    """park / give-up-ACK 语义必须与 DONE **同族**，不得单独造例外。

    inbox 的两张表都不含任何 DONE 类前缀（`[TASK SUBMITTED]` 等才 exempt），
    目的是不让未完的唤醒被 park 掉。TRUNCATED 与 DONE 同族 ⇒ 两张表里的
    归属也必须相同，否则会造出「TRUNCATED 被 park 就永久丢」的新不一致。
    """
    done = {"message": "[SUBAGENT DONE] job=x", "message_type": "system"}
    trunc = {"message": "[SUBAGENT DONE_TRUNCATED] job=x", "message_type": "system"}
    assert should_exempt_from_park(done) == should_exempt_from_park(trunc)
    assert should_spare_from_give_up_ack(done) == should_spare_from_give_up_ack(
        trunc
    )
    for mt in (None, "system", OFFTURN_COMPLETION_MESSAGE_TYPE):
        d = {"message": "[SUBAGENT DONE] job=x", "message_type": mt}
        t = {"message": "[SUBAGENT DONE_TRUNCATED] job=x", "message_type": mt}
        assert should_exempt_from_park(d) == should_exempt_from_park(t)
        assert should_spare_from_give_up_ack(
            d
        ) == should_spare_from_give_up_ack(t)


def test_bash_has_no_truncated_kind() -> None:
    """bash 无第三值：只有 subagent 会报「非失败的未完成」。

    这条守的是「不造死成员」——`OFFTURN_STATE` 里没有 BASH_TRUNCATED，
    所以 `_STATES_BY_KIND["bash"]` 也不该有 `truncated` 槽位。若将来 bash
    也要第三值，本测试会失败，提醒同步改 docs 与提示词，而不是静默漂移。
    """
    from hiveweave.services import offturn as _ot

    assert "truncated" not in _ot._STATES_BY_KIND["bash"]
    assert "truncated" in _ot._STATES_BY_KIND["subagent"]
    assert not hasattr(_ot.OFFTURN_STATE, "BASH_TRUNCATED")
    # 协议全集必须含 TRUNCATED——否则 is_offturn_completion_text 认不出它，
    # 父的 kind=agent wait 会永远等不到唤醒（静默停泊）
    assert _ot.OFFTURN_STATE.SUBAGENT_DONE_TRUNCATED.prefix in (
        _ot._COMPLETION_PREFIXES
    )


def test_terminal_state_rejects_illegal_combinations() -> None:
    """非法/矛盾终态必须**抛**，不许静默兜底成 DONE 或 FAILED。

    这是「回执如实」的边界守卫：如果一个 kind 不允许的 state 被静默接受，
    父代理会拿到一个语义错误的前缀——比崩溃更难查。
    """
    from hiveweave.services.offturn import (
        OFFTURN_STATE,
        _terminal_state,
    )

    # 合法：二值默认 + 显式第三值
    assert (
        _terminal_state("subagent", True, None)
        is OFFTURN_STATE.SUBAGENT_DONE
    )
    assert (
        _terminal_state("subagent", False, None)
        is OFFTURN_STATE.SUBAGENT_FAILED
    )
    assert (
        _terminal_state("subagent", True, OFFTURN_STATE.SUBAGENT_DONE_TRUNCATED)
        is OFFTURN_STATE.SUBAGENT_DONE_TRUNCATED
    )
    # 矛盾：ok=True 却声明 FAILED；ok=False 却声明 TRUNCATED
    for kind, ok, state in (
        ("subagent", True, OFFTURN_STATE.SUBAGENT_FAILED),
        ("subagent", False, OFFTURN_STATE.SUBAGENT_DONE_TRUNCATED),
        ("subagent", True, OFFTURN_STATE.BASH_DONE),  # 跨 kind 也不许
        ("bash", True, OFFTURN_STATE.SUBAGENT_DONE_TRUNCATED),  # bash 无第三值
    ):
        with pytest.raises(ValueError):
            _terminal_state(kind, ok, state)


@pytest.mark.asyncio
async def test_work_declaring_state_drives_the_prefix(
    monkeypatch: pytest.MonkeyPatch, clean_offturn
) -> None:
    """端到端：`work()` 显式返回的 state 决定前缀，与 payload 文本无关。

    ⚠ 反过来也要成立：payload 里**写着** `[SUBAGENT DONE_TRUNCATED]` 但
    `work()` 声明 DONE 时，前缀必须是 DONE —— 证明框架**不读**正文
    （用户 09-14 钦定：禁用文本判据）。
    """
    from hiveweave.services.offturn import OFFTURN_STATE

    inbox, _cleared = _patch_completion(monkeypatch)

    async def work_declared() -> tuple:
        return True, "partial", OFFTURN_STATE.SUBAGENT_DONE_TRUNCATED

    job = start_offturn_job(
        kind="subagent",
        agent_id="agent-exec",
        project_id="proj-1",
        work=work_declared,
    )
    for _ in range(80):
        if not is_live_job(job):
            break
        await asyncio.sleep(0.02)
    assert inbox and inbox[0].lstrip().startswith("[SUBAGENT DONE_TRUNCATED]")

    inbox.clear()

    async def work_text_only() -> tuple:
        # 正文里**模仿**截断态文案，但终态声明是 DONE
        return (
            True,
            "[SUBAGENT TRUNCATED] text looks truncated but state says done",
        )

    job2 = start_offturn_job(
        kind="subagent",
        agent_id="agent-exec",
        project_id="proj-1",
        work=work_text_only,
    )
    for _ in range(80):
        if not is_live_job(job2):
            break
        await asyncio.sleep(0.02)
    assert inbox, "second receipt must be delivered"
    assert inbox[0].lstrip().startswith("[SUBAGENT DONE]")
    assert not inbox[0].lstrip().startswith("[SUBAGENT DONE_TRUNCATED]")


def test_unrelated_same_named_prefix_is_not_a_receipt() -> None:
    """防误认：只有**行首**的完整协议前缀才算回执。"""
    assert not is_offturn_completion_text("[RANDOM] [SUBAGENT DONE_TRUNCATED] x")
    assert not is_offturn_completion_text("see [SUBAGENT DONE_TRUNCATED] above")


async def test_truncated_wake_needs_both_type_gate_and_prefix_start() -> None:
    """TRUNCATED 唤醒父代理需**两个条件同时**成立（缺一即静默停泊）。

    `wake_source_for_pending` 的 docstring 明说 `message_type` 是 trust gate、
    前缀是 body contract，**不是互相替代**。既有测试只验了单条完整消息，
    没验「两个条件各自失效时是否真的降级」——本测试补这一半（阴性对照）。

    阳性对照实测（2026-09-18，手工）：把 `DONE_TRUNCATED` 移出
    `_COMPLETION_PREFIXES` 后，本测试的第 3 例**转红**且其余 5 例不变红
    ⇒ 探针精确命中、无假阳。
    """
    trunc = OFFTURN_STATE.SUBAGENT_DONE_TRUNCATED.prefix

    def m(text: str, mt: str | None) -> dict:
        return {"message": text, "message_type": mt, "from_agent_id": "sys"}

    # 阳性：类型对 + 前缀在行首 ⇒ 满足 wait
    assert (
        await wake_source_for_pending([m(f"{trunc} job=x", OFFTURN_COMPLETION_MESSAGE_TYPE)])
        == "wait_satisfied"
    )
    # 阴性①：前缀对但类型**不是** offturn_completion ⇒ 不得满足
    # （类型闸失效 ⇒ 任何人伪造一段带前缀的文本都能唤醒父代理）
    assert await wake_source_for_pending([m(trunc, "normal")]) == "trigger"
    # 阴性②：类型对但前缀不在行首 ⇒ 不得满足
    assert (
        await wake_source_for_pending([m(f"先说一句话\n{trunc} job=x", OFFTURN_COMPLETION_MESSAGE_TYPE)])
        == "trigger"
    )
    # 阴性③：两条件都缺 ⇒ 不得满足
    assert await wake_source_for_pending([m("子代理跑完了", None)]) == "trigger"
    # 阴性④：空 / 非 dict 输入不得抛异常也不得误判为满足
    assert await wake_source_for_pending([]) == "trigger"
    assert await wake_source_for_pending([None, "not-a-dict"]) == "trigger"  # type: ignore[list-item]

